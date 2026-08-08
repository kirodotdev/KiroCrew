"""CLI chat and TUI subcommands."""

from __future__ import annotations

import argparse
import asyncio
import gc
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from kiro_crew.acp.client import AcpError, AcpTimeoutError
from kiro_crew.config import KiroCrewConfig
from kiro_crew.config.loader import (
    ConfigReadError,
    build_provider_factory,
    config_path,
    read_config_for_update,
    write_config_atomically,
)
from kiro_crew.constants import BANNER, DATA_WARNING
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMProvider

logger = logging.getLogger(__name__)


def _tui(args: argparse.Namespace) -> None:
    """Launch the Ink TUI, replacing the current process."""
    cfg = KiroCrewConfig.load()
    port = getattr(args, "port", None) or cfg.to_dict().get("dashboard", {}).get("port", 5476)

    # Find TUI — prefer self-contained bundle, fall back to source tree
    base = Path(__file__).resolve().parent.parent.parent
    tui_js = None

    # 1. Bundled (no node_modules needed) — check tui_dist/ and source tree
    for candidate in [
        Path(__file__).resolve().parent / "tui_dist" / "bundle.mjs",
        base / "tui" / "dist" / "bundle.mjs",
    ]:
        if candidate.is_file():
            tui_js = candidate
            break

    # 2. Walk up to workspace src tree for bundle.mjs or index.js+node_modules
    if not tui_js:
        p = Path(__file__).resolve()
        for _ in range(15):
            p = p.parent
            bundle = p / "src" / "KiroCrew" / "tui" / "dist" / "bundle.mjs"
            if bundle.is_file():
                tui_js = bundle
                break
            idx = p / "src" / "KiroCrew" / "tui" / "dist" / "index.js"
            if idx.is_file() and (p / "src" / "KiroCrew" / "tui" / "node_modules").is_dir():
                tui_js = idx
                break

    if not tui_js:
        print("TUI not built. Run: cd tui && npm install && npm run build")
        print("  (or use: kirocrew chat  /  kirocrew gateway)")
        sys.exit(1)

    # Check node >= 20
    if not shutil.which("node"):
        print("Node.js not found. Install Node.js >= 20.")
        sys.exit(1)
    try:
        ret = subprocess.call(
            [
                "node",
                "-e",
                "process.exit(Number(process.version.slice(1).split('.')[0]) < 20 ? 1 : 0)",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if ret != 0:
            print("Node.js >= 20 required. Current version is too old.")
            sys.exit(1)
    except FileNotFoundError:
        print("Node.js not found.")
        sys.exit(1)

    cmd = ["node", str(tui_js), "--port", str(port), "--cwd", os.getcwd()]
    if getattr(args, "yolo", False):
        cmd.append("--yolo")
    if getattr(args, "session", None):
        cmd.extend(["--session", args.session])
    if getattr(args, "workspace", None):
        cmd.extend(["--workspace", args.workspace])
    if getattr(args, "agent", None):
        cmd.extend(["--agent", args.agent])
    home_override = getattr(args, "home", None) or os.environ.get("KIROCREW_HOME", "")
    if home_override:
        cmd.extend(["--home", home_override])

    os.execvp("node", cmd)


async def _chat(message: str | None, model: str | None, agent: str | None = None) -> None:
    """Run a single message or interactive chat session."""
    cfg = KiroCrewConfig.load()
    if model:
        cfg.agent.model = model
    channel_id = os.environ.get("KIROCREW_CHANNEL_ID") or None
    agent_name = agent or cfg.agent.default_agent or None
    provider: LLMProvider = build_provider_factory(cfg)(
        "cli_chat", agent=agent_name, channel_id=channel_id
    )
    try:
        await provider.start()

        if message:
            await _send_and_print(provider, message)
        else:
            await _interactive(provider, cfg)
    finally:
        try:
            await provider.shutdown()
        finally:
            # Force GC so subprocess transports are collected while the loop is
            # still open, avoiding "Event loop is closed" noise on exit.
            gc.collect()


def _run_chat(message: str | None, model: str | None, agent: str | None = None) -> None:
    """Run chat at the sync CLI boundary and render SIGINT as a clean exit."""
    try:
        asyncio.run(_chat(message, model, agent=agent))
    except KeyboardInterrupt:
        print("\nBye! 👻")


async def _send_and_print(provider: LLMProvider, message: str) -> None:
    """Stream a single message to stdout, handling errors and timeouts."""
    try:
        async for event in provider.stream(message):
            if event.kind == EVENT_TEXT_CHUNK:
                print(event.text, end="", flush=True)
            elif event.kind == EVENT_COMPLETE:
                break
        print()  # final newline
    except AcpTimeoutError as e:
        if e.partial_output:
            print(e.partial_output)
        print("\n⏱️  Response timed out.", file=sys.stderr)
        sys.exit(1)
    except AcpError as e:
        print(f"\n❌ {e}", file=sys.stderr)
        sys.exit(1)


async def _interactive(provider: LLMProvider, cfg: KiroCrewConfig) -> None:
    """REPL loop — read user input, stream responses, auto-compact at configured threshold."""
    print(BANNER)
    print(DATA_WARNING)
    print()

    print("Type your message (Ctrl+D or 'exit' to quit)\n")

    prompt_count = 0

    while True:
        try:
            message = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye! 👻")
            break

        if not message:
            continue
        if message.lower() in ("exit", "quit", "/exit", "/quit", ":q"):
            print("Bye! 👻")
            break

        await _send_and_print(provider, message)
        prompt_count += 1

        # Check context usage — compact and restart if needed
        pct = provider.context_usage_pct()
        needs_compact = pct >= cfg.session.autocompact_pct

        if needs_compact:
            reason = f"context at {pct:.0f}%"
            print(f"\n🔄 Compacting — {reason}", file=sys.stderr)
            try:
                await provider.compact()
            except Exception:
                pass
            await provider.shutdown()
            await provider.start()
            prompt_count = 0
        elif pct >= 75.0:
            print(f"\n⚠️  Context at {pct:.0f}%", file=sys.stderr)

        print()


def _ensure_config_key(section: str, key: str, default: object) -> None:
    """Write a default value to config.json if the key is missing.

    Seeding a default is never worth destroying real settings, so an unreadable
    config skips the write entirely rather than seeding onto ``{}``.
    """
    p = config_path()
    try:
        data = read_config_for_update(p)
    except ConfigReadError:
        logger.warning("Skipping config seed for %s.%s: config unreadable", section, key)
        return
    if key not in data.get(section, {}):
        data.setdefault(section, {})[key] = default
        write_config_atomically(p, data)


def _ensure_default_agent_in_config() -> None:
    """Ensure config.json includes a default KiroCrew agent for fresh installs."""
    p = config_path()
    try:
        data = read_config_for_update(p)
    except ConfigReadError:
        logger.warning("Skipping default-agent seed: config unreadable")
        return
    if not data.get("agents"):
        data["agents"] = {
            "default": {
                "kiro_agent": "kirocrew",
                "workspace": "default",
                "memory_store": "default",
            }
        }
        data["default_agent"] = "default"
        write_config_atomically(p, data)

"""CLI setup subcommand — interactive credential and config wizard."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
from importlib.resources import files as _pkg_files
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from kiro_crew import platform_compat
from kiro_crew.acp.client import KIRO_CLI_BIN
from kiro_crew.browser.setup import (
    browser_mode_enabled,
    generate_playwright_config,
    refresh_storage_state,
    register_playwright_proxy,
)
from kiro_crew.cli_chat import _ensure_default_agent_in_config
from kiro_crew.conductor_skill import generate_conductor_skill
from kiro_crew.config import KiroCrewConfig
from kiro_crew.config.loader import (
    _WORKSPACE_DIR_NAME,
    CRED_OWNER_ID,
    CRED_SLACK_APP_TOKEN,
    CRED_SLACK_BOT_TOKEN,
    _default_workspace_base,
    _workspace_dir_file,
    config_local_path,
    config_path,
    env_path,
    write_config_atomically,
)
from kiro_crew.constants import DATA_WARNING
from kiro_crew.sandbox import unavailable_kind
from kiro_crew.sel import sel
from kiro_crew.skills import SkillsLoader


def _get_alias() -> str:
    """Return the user's login name (used to name the Slack app)."""
    alias = os.environ.get("USER") or ""
    if not alias:
        try:
            alias = os.getlogin()
        except OSError:
            pass
    if not alias:
        alias = input("  Your username (e.g. johndoe): ").strip()
    if not alias:
        print(
            "❌ Cannot determine username. Set $USER or re-run with "
            "`kirocrew manifest --alias <alias>`.",
            file=sys.stderr,
        )
        sys.exit(1)
    return alias


def _manifest(alias: str | None = None, output: str | None = None, url: bool = False) -> None:
    """Render slack-manifest.yaml with the user's alias substituted."""

    alias = alias or _get_alias()
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", alias):
        print(
            "❌ Invalid alias — must be alphanumeric, hyphens, or underscores only.",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        template_text = (
            _pkg_files("kiro_crew").joinpath("slack-manifest.yaml").read_text(encoding="utf-8")
        )
    except FileNotFoundError:
        print("❌ Cannot find slack-manifest.yaml", file=sys.stderr)
        sys.exit(1)
    rendered = template_text.replace("{{ALIAS}}", alias)
    if url:
        # Strip comment lines to shorten the URL
        lines = [ln for ln in rendered.splitlines() if not ln.lstrip().startswith("#")]
        encoded = quote("\n".join(lines).strip() + "\n", safe="")
        print("\n🔗 Click to create your Slack app:\n")
        print(f"https://api.slack.com/apps?new_app=1&manifest_yaml={encoded}\n")
    elif output:
        out = Path(output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(rendered, encoding="utf-8")
        print(f"✅ Manifest written to {output} (name: KiroCrew-{alias})")
    else:
        print(rendered)


def _fix_shell_profiles() -> None:
    """Remove stale KiroCrew PATH entries from shell profiles."""
    home = Path.home()
    profiles = [
        home / ".zshrc",
        home / ".bashrc",
        home / ".bash_profile",
        home / ".profile",
    ]
    stale_markers = [
        ".kirocrew-app",
    ]
    cleaned_profiles: list[str] = []
    for profile in profiles:
        if not profile.is_file():
            continue
        try:
            lines = profile.read_text(encoding="utf-8").splitlines(keepends=True)
            cleaned = []
            removed = False
            for line in lines:
                if any(m in line for m in stale_markers) and "PATH" in line:
                    removed = True
                    continue
                cleaned.append(line)
            if removed:
                profile.write_text("".join(cleaned), encoding="utf-8")
                print(f"  🔧 Cleaned stale Kiro Crew PATH from {profile.name}")
                cleaned_profiles.append(profile.name)
        except OSError:
            pass
    if cleaned_profiles:
        sources = " or ".join(f"`source ~/{p}`" for p in cleaned_profiles)
        print(f"  ⚠️  Run {sources} or open a new terminal for PATH changes to take effect.")


def _ensure_prerequisites() -> bool:
    """Report on optional prerequisites resolved from PATH.

    The public build's agent backend is ``kiro-cli``. This performs no installs
    and never blocks setup — it only prints guidance for tooling that is missing
    from PATH. Always returns True so setup proceeds.
    """
    header_printed = False

    def _header() -> None:
        nonlocal header_printed
        if not header_printed:
            print("── Prerequisites ──\n")
            header_printed = True

    # Node is required for the dashboard build (and for MCP servers shipped as
    # npm packages, e.g. the Playwright browser MCP).
    if not shutil.which("node"):
        _header()
        print("  ⚠️  node not found on PATH — install Node.js >= 16 from https://nodejs.org\n")

    # kiro-cli is the agent backend. Note its absence so the user can install it.
    if not shutil.which(KIRO_CLI_BIN):
        _header()
        print(
            "  ℹ️  kiro-cli not found on PATH — install it (the agent backend) "
            "and run 'kiro-cli login'.\n"
        )

    return True


def _find_electron_dir() -> Path | None:
    """Locate the in-repo electron app sources (website/electron).

    The pip-installed package does NOT ship the desktop-app sources, so this
    only resolves from a source checkout: first via KIROCREW_PROJECT_DIR, then
    by walking up from this module's location and the cwd.
    """
    candidates = []
    proj = os.environ.get("KIROCREW_PROJECT_DIR")
    if proj:
        candidates.append(Path(proj))
    for base in (Path(__file__).resolve(), Path.cwd()):
        for parent in [base] + list(base.parents):
            candidates.append(parent)
    seen = set()
    for root in candidates:
        if root in seen:
            continue
        seen.add(root)
        electron_dir = root / "website" / "electron"
        if electron_dir.is_dir():
            return electron_dir
    return None


def _setup_electron() -> None:
    """Build and install the KiroCrew desktop app (macOS only)."""
    if platform.system() != "Darwin":
        print("  ⚠️  Kiro Crew desktop app is only available on macOS.")
        return

    if not shutil.which("node"):
        print("  ❌ Node.js not found — required to build the desktop app.")
        print("     Install Node.js and re-run: kirocrew setup --electron-only")
        return

    electron_dir = _find_electron_dir()
    if electron_dir is None:
        print("  ❌ Desktop app sources not found.")
        print(
            "     The desktop app is built from a source checkout — clone the "
            "repo and run `make desktop` (or run setup from the checkout)."
        )
        return

    print("  🔨 Building Kiro Crew desktop app…")
    npm_install = subprocess.run(
        ["npm", "install", "--no-audit", "--no-fund", "--loglevel=error"],
        cwd=str(electron_dir),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if npm_install.returncode != 0:
        print(f"  ❌ npm install failed: {npm_install.stderr.strip()[:200]}")
        return

    build = subprocess.run(
        ["npx", "electron-builder", "--mac", "--dir"],
        cwd=str(electron_dir),
        capture_output=True,
        text=True,
        timeout=300,
    )
    if build.returncode != 0:
        print(f"  ❌ Electron build failed: {build.stderr.strip()[:200]}")
        return

    arch = "mac-arm64" if platform.machine() == "arm64" else "mac"
    app_src = electron_dir / "dist" / arch / "KiroCrew.app"
    if not app_src.is_dir():
        for candidate in ("mac-arm64", "mac", "mac-x64"):
            app_src = electron_dir / "dist" / candidate / "KiroCrew.app"
            if app_src.is_dir():
                break
    if not app_src.is_dir():
        print("  ❌ Build succeeded but KiroCrew.app not found in dist/")
        return

    app_dest = Path.home() / "Applications" / "KiroCrew.app"
    app_dest.parent.mkdir(parents=True, exist_ok=True)
    if app_dest.is_dir():
        shutil.rmtree(app_dest)
    shutil.copytree(str(app_src), str(app_dest))
    print("  ✅ KiroCrew.app installed to ~/Applications")
    print("     Launch via Spotlight (⌘+Space → KiroCrew) or Finder → ~/Applications")


def _setup(agent_only: bool = False, electron_only: bool = False, clean: bool = False) -> None:
    """Install agent config and optionally configure credentials."""
    try:
        _setup_impl(agent_only=agent_only, electron_only=electron_only, clean=clean)
    except _SetupAborted as exc:
        # A closed/piped stdin mid-wizard. One clean line instead of a stack
        # trace at whichever prompt hit it first — every guarded prompt raises,
        # so the exit point is deterministic. Bare `input()` calls still exist
        # in some steps and traceback the old way; converting them all is a
        # sweep for its own change.
        print(f"\n⏭  Setup aborted: {exc}. Re-run interactively to finish.")


def _setup_impl(agent_only: bool = False, electron_only: bool = False, clean: bool = False) -> None:
    from kiro_crew.agent import install_agent  # circular import: agent imports cli
    from kiro_crew.cli import _project_dir_file  # circular import: cli -> cli_setup -> cli

    if electron_only:
        print("── Desktop App ──\n")
        _setup_electron()
        return

    print("Kiro Crew Setup 👻\n")
    print(f"  {DATA_WARNING.replace(chr(10), chr(10) + '  ')}\n")

    # Report on optional prerequisites.
    _ensure_prerequisites()

    # 0. Save project dir so kirocrew works from anywhere
    proj = os.environ.get("KIROCREW_PROJECT_DIR")
    if proj:
        _project_dir_file().parent.mkdir(parents=True, exist_ok=True)
        _project_dir_file().write_text(proj + "\n", encoding="utf-8")
        print(f"  ✅ Project dir saved: {proj}")

    # 1. Choose workspace directory (skip for agent-only — not relevant)
    if not agent_only:
        _setup_workspace_dir()

    # 2. Install the agent config
    print("Installing agent config...")
    agent_path = install_agent(clean=clean)
    print(f"  ✅ Agent installed: {agent_path}")

    # 2a. Ensure `kirocrew` is reachable on PATH (the packaged Electron app
    #     doesn't run install.sh) and purge stale predecessor MCP entries left
    #     in the user's global provider config by the rename from the upstream
    #     project. Both run only here, on the explicit setup/migration path.
    from kiro_crew.agent import ensure_kirocrew_on_path
    from kiro_crew.mcp_cleanup import clean_stale_managed_mcp

    shim = ensure_kirocrew_on_path()
    if shim:
        print(f"  ✅ Linked kirocrew on PATH: {shim}")
    removed_mcp = clean_stale_managed_mcp()
    if removed_mcp:
        print(f"  ✅ Removed stale MCP entries: {', '.join(removed_mcp)}")

    # 2b. Ensure config.json has default KiroCrew agent for fresh installs
    _ensure_default_agent_in_config()

    # 2c. Generate conductor skill if enabled (agent delegation).
    try:
        cfg = KiroCrewConfig.load()
        if cfg.agent.conductor_skill:
            generate_conductor_skill(SkillsLoader())
            print("  ✅ Conductor skill generated")
        else:
            # Clean up stale skill if previously enabled then disabled.
            skill_path = SkillsLoader()._dir / "conductor" / "SKILL.md"
            if skill_path.exists():
                skill_path.unlink()
    except Exception as exc:
        print(f"  ⚠️  Conductor skill generation failed: {exc}")

    # 2d. Offer the unconfined-exec opt-in on a host with no sandbox backend.
    #     Runs BEFORE the agent-only early return: the servers this unblocks are
    #     the ones install_agent() just wrote, so `--agent-only` needs it too.
    _setup_sandbox_consent()

    if agent_only:
        print("\n👻 Done! Try: kirocrew gateway")
        return

    # 3. Slack credentials
    _setup_slack_tokens()

    # 3b. Slash command name
    _setup_slash_command()

    # 4. Timezone
    _setup_timezone()

    # 5. Dashboard URL (remote access)
    _maybe_setup_dashboard_url()

    _maybe_setup_custom_domain()

    # ── Browser (Playwright MCP) ──
    # Browser Mode is a deliberate, durably-persisted capability the user turns
    # on from Settings -> Browser (registration = authorization now that there is
    # no per-message marker). The wizard does NOT auto-register it: doing so would
    # mount the browser_* tools for an agent whose owner never enabled Browser
    # Mode. We only refresh the storage state if it is already set up.
    print("\n── Browser (Playwright MCP) ──")
    if browser_mode_enabled():
        try:
            generate_playwright_config()
            refresh_storage_state()
            _, status = register_playwright_proxy()
            if status == "kept-user-entry":
                print("  Kept your existing playwright-mcp entry in mcp.json (left untouched)")
            else:
                print("  Browser Mode is on — proxy registered in mcp.json")
        except Exception:
            pass  # Non-fatal: browser still works without pre-loaded cookies
    else:
        print("  Browser Mode is off. Turn it on in Settings -> Browser to let the")
        print("  agent operate a browser; it downloads Playwright and wires the proxy.")

    # 6. Desktop app (macOS only)
    if platform.system() == "Darwin":
        print("── Desktop App ──\n")
        answer = input("  Install KiroCrew desktop app to ~/Applications? [Y/n]: ").strip().lower()
        if answer in ("", "y", "yes"):
            _setup_electron()
        else:
            print("  ⏭  Skipped. Install later: kirocrew setup --electron-only\n")

    # 7. Cloud (run KiroCrew on the user's own AWS EC2) — optional, delegated.
    _maybe_setup_cloud()

    print("\n👻 Done! Try: kirocrew doctor && kirocrew gateway")


def _maybe_setup_cloud() -> None:
    """Offer to launch KiroCrew on the user's own AWS EC2.

    A thin delegating step — all AWS/CloudFormation/SSM logic lives in the
    testable ``kiro_crew.cloud`` module. This just asks and hands off to the
    launcher wizard (``kirocrew cloud launch``).
    """
    print("\n── Run on AWS (optional) ──\n")
    print("  Kiro Crew can run 24/7 on your own AWS EC2 instance (bring your own")
    print("  AWS account; credentials stay in the aws CLI — never stored here).")
    try:
        answer = input("  Launch KiroCrew on AWS now? [y/N]: ").strip().lower()
    except EOFError:
        # Piped/non-interactive setup — take the default (skip).
        answer = ""
    if answer not in ("y", "yes"):
        print("  ⏭  Skipped. Launch later: kirocrew cloud launch\n")
        return
    try:
        import argparse

        from kiro_crew.cli_cloud import handle_cloud

        # hold_tunnel=False: setup still has steps to print after this one, so
        # the wizard must return instead of blocking on the SSM tunnel.
        args = argparse.Namespace(
            cloud_action="launch",
            profile="",
            region="",
            size="",
            yes=False,
            hold_tunnel=False,
        )
        handle_cloud(args)
    except Exception as exc:  # pragma: no cover - non-fatal, informative
        print(f"  Cloud launch could not start: {exc}")
        print("  Run it directly: kirocrew cloud launch\n")


def _setup_workspace_dir() -> None:
    """Prompt user for workspace directory, falling back to platform default."""
    platform_default = _default_workspace_base() / _WORKSPACE_DIR_NAME
    default = platform_default
    label = "Default"
    if _workspace_dir_file().is_file():
        configured = _workspace_dir_file().read_text(encoding="utf-8").strip()
        if configured:
            default = Path(configured)
            label = "Configured"
    print("── Workspace Directory ──\n")
    print("  LLM sessions and task output are stored in a workspace directory.")
    print(f"  {label}: {default}\n")
    # EOF (piped / closed stdin) keeps the default rather than raising a
    # traceback out of the wizard — this step runs FIRST, so a bare input() here
    # made `kirocrew setup < /dev/null` fail before any later guard could help.
    answer = _input_or_skip(f"  Workspace path [{default}]: ") or ""
    chosen = default if answer.lower() in ("", "y", "yes") else Path(answer).expanduser()
    try:
        chosen.mkdir(parents=True, exist_ok=True)
        _workspace_dir_file().parent.mkdir(parents=True, exist_ok=True)
        _workspace_dir_file().write_text(str(chosen) + "\n", encoding="utf-8")
        print(f"  ✅ Workspace: {chosen}\n")
    except OSError as e:
        print(f"  ❌ Cannot create {chosen}: {e}")
        print(f"  Falling back to platform default: {platform_default}\n")


def _setup_slack_tokens() -> None:
    """Prompt for Slack tokens and owner ID, write to config_dir/.env."""
    cred_path = env_path()
    existing: dict[str, str] = {}
    if cred_path.exists():
        for line in cred_path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                existing[k.strip()] = v.strip()

    print("── Slack Credentials ──\n")
    print("  See docs/guides/slack-setup.md for how to create a Slack app.\n")

    answer = input("  Configure Slack tokens? [Y/n]: ").strip().lower()
    if answer in ("n", "no"):
        print("  ⏭  Skipped. Slack integration will be disabled.\n")
        return

    def _mask(val: str) -> str:
        return val[:8] + "…" if len(val) > 12 else val

    cur_app = existing.get(CRED_SLACK_APP_TOKEN, "")
    cur_bot = existing.get(CRED_SLACK_BOT_TOKEN, "")
    cur_owner = existing.get(CRED_OWNER_ID, "")

    hint_app = f" [{_mask(cur_app)}]" if cur_app else ""
    hint_bot = f" [{_mask(cur_bot)}]" if cur_bot else ""
    hint_owner = f" [{cur_owner}]" if cur_owner else ""

    app_token = input(f"  App Token (xapp-...){hint_app}: ").strip() or cur_app
    bot_token = input(f"  Bot Token (xoxb-...){hint_bot}: ").strip() or cur_bot
    owner_id = input(f"  Your Slack Member ID{hint_owner}: ").strip() or cur_owner

    if not app_token or not bot_token:
        print("  ⚠️  Missing tokens — Slack integration will be disabled.\n")
        return

    # Preserve any extra keys already in .env
    existing[CRED_SLACK_APP_TOKEN] = app_token
    existing[CRED_SLACK_BOT_TOKEN] = bot_token
    if owner_id:
        existing[CRED_OWNER_ID] = owner_id

    cred_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{k}={v}" for k, v in existing.items()]
    cred_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    cred_path.chmod(0o600)
    print(f"  ✅ Credentials saved to {cred_path}\n")


_CUSTOM_DOMAIN = "kirocrew.localhost"


def _configured_port() -> int:
    """Return the dashboard port using the same resolution as other CLI commands."""
    from kiro_crew.cli_server import (
        resolve_client_port,  # local import keeps cli_server (heavy import graph) out of cli_setup's import time
    )

    return resolve_client_port(None)


def _detect_system_timezone() -> str:
    """Return the system IANA tz name, or empty string if it can't be detected.

    ``TZ`` and ``/etc/localtime`` are POSIX-only. On Windows the zone lives in
    the registry as a Windows zone id ("Pacific Standard Time") that needs a
    Windows->IANA mapping, so without a Windows branch this returned "" there and
    the whole product silently fell back to UTC — cron schedules and the agent's
    "today" were off by the local offset. ``tzlocal`` (a Windows-marked dep whose
    only requirement, ``tzdata``, we already ship) resolves it via the CLDR
    windowsZones table; it is imported lazily so a source checkout without it
    degrades to the old skip-and-ask flow rather than crashing.
    """
    tz_env = os.environ.get("TZ", "").lstrip(":")
    if tz_env and not tz_env.startswith("/"):
        return tz_env
    try:
        p = Path("/etc/localtime")
        if p.is_symlink():
            target = str(p.resolve())
            if "zoneinfo/" in target:
                return target.split("zoneinfo/", 1)[1]
    except Exception:
        pass
    if platform_compat.IS_WINDOWS:
        try:
            # tzlocal ships no type stubs; it is a Windows-only lazy import, so
            # ignore the missing-stub error rather than add a types-* dep.
            import tzlocal  # type: ignore[import-untyped]

            name = tzlocal.get_localzone_name()
            if name:
                return str(name)
        except Exception:
            # No tzlocal / unmappable zone: fall through to the ask-or-skip path
            # rather than crash the setup wizard.
            pass
    return ""


class _SetupAborted(Exception):
    """Raised when stdin is closed mid-wizard.

    The wizard is a sequential chain of interactive prompts; if stdin closes at
    ANY prompt, every subsequent bare ``input()`` would traceback. Callers use
    ``_input_or_skip`` for guarded prompts (workspace, slash-command, timezone);
    the top-level ``_setup`` catches this and exits cleanly, so a
    ``kirocrew setup < /dev/null`` — or a Windows console quirk that closes
    stdin — surfaces one clean message instead of a stack trace.
    """


def _input_or_skip(prompt: str) -> str | None:
    """``input(prompt).strip()``, or raise ``_SetupAborted`` on EOF.

    Returns ``None`` when the user hit Enter with no input, which callers treat
    as "keep the default / skip this step". A closed/piped stdin is a different
    condition and must not be silently coerced to ``""`` (that used to admit an
    empty default and cascade the failure into the NEXT step's bare
    ``input()``) — see ``_SetupAborted``.
    """

    try:
        answer = input(prompt).strip()
    except EOFError as exc:
        raise _SetupAborted("stdin closed; setup cannot continue") from exc
    return answer or None


def _setup_slash_command() -> None:
    """Prompt for custom slash command name, save to config.json."""
    cfg_file = config_path()
    cfg: dict = {}
    if cfg_file.exists():
        try:
            cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"  ⚠️  Could not read {cfg_file}: {exc}")
            return

    print("── Slash Command ──\n")
    current = cfg.get("slack", {}).get("command", "kirocrew")
    # EOF keeps the current value (same reasoning as the workspace step).
    raw = _input_or_skip(f"  Slash command name [{current}]: ") or ""
    if raw:
        raw = raw.lstrip("/").strip()
    if not raw:
        raw = current
    if not all(c.isalnum() or c in "-_" for c in raw):
        print("  ⚠️  Command name should only contain letters, numbers, hyphens, or underscores.")
        raw = current
    if len(raw) > 32:
        print("  ⚠️  Command name too long (max 32 chars).")
        raw = current

    cfg.setdefault("slack", {})["command"] = raw
    write_config_atomically(cfg_file, cfg)
    print(f"  ✅ Slash command: /{raw}\n")


def _setup_sandbox_consent() -> None:
    """Offer the unconfined-exec opt-in when this host has NO sandbox backend.

    Fail-closed is the shipped posture: with no backend ``wrap_argv`` refuses
    every agent subprocess, so a fresh install on such a host — any Windows host,
    or a Linux kernel that refuses user namespaces — has no working MCP tooling
    until an operator declares the opt-in. Leaving them to discover that from a
    probe error is a bad first run, but defaulting the opt-in ON by platform
    would delete a deny-by-default authorization and put nothing in its place:
    an agent-selected repo's ``include.path`` reaches ``~/.aws/credentials``, and
    a crafted ``.tex`` typesets a secret into a PDF.

    So the wizard ASKS, and writes the key only on an explicit yes. That keeps
    the decision operator-declared exactly as
    ``docs/system-specs/modules/security.md`` requires while making it
    discoverable instead of hidden behind a spawn failure.

    Only a genuine ``"no_backend"`` classification reaches the prompt.
    ``detect_backend() == "none"`` alone is not sufficient: it also covers a
    momentary fork/resource failure, which self-heals on the next spawn and must
    never buy a permanent bypass, and a foreign outer sandbox, where this host's
    sandbox works and the remedy hands isolation back to Kiro Crew rather than
    disabling it. The prompt is also skipped when stdin/stdout are not both a
    terminal, because an unseen question is a hang rather than consent.

    Silent no-op when a backend exists (the Linux/macOS norm) or when the key is
    already declared in either state, in ``config.json`` OR the
    ``config.local.json`` overlay that deep-merges over it — the overlay wins at
    load time, so ignoring it would let this step prompt a user who already
    decided and then report a grant the effective config contradicts. Declining —
    including a non-interactive EOF, which :func:`_input_or_skip` reports as
    ``None`` — leaves the config untouched, so the effective default stays
    fail-closed.
    """
    try:
        kind = unavailable_kind()
    except Exception as exc:  # pragma: no cover — defensive
        print(f"  ⚠️  Could not probe for a sandbox backend: {exc}")
        return
    if kind == "transient":
        # A momentary fork/resource failure is NOT grounds for a permanent
        # bypass: it is not cached, the next spawn re-probes, and the sandbox
        # layer's own guidance says explicitly not to disable the sandbox for it.
        print("── Sandbox ──\n")
        print("  The sandbox probe failed for what looks like a TRANSIENT reason")
        print("  (momentary resource pressure). It re-probes automatically, so no")
        print("  opt-in is offered. Retry the operation instead.\n")
        return
    if kind != "no_backend":
        # "" (a backend exists — the Linux/macOS norm) or "foreign_sandbox",
        # where this host's sandbox works and the remedy hands isolation back to
        # Kiro Crew rather than disabling it. Neither warrants this opt-in.
        return
    # A prompt nobody can see is a hang, not consent: `kirocrew update` runs
    # setup with its output captured while stdin is still inherited, so an
    # invisible question would block until that path's timeout aborts the update.
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("  ⚠️  No sandbox backend on this host, so agent subprocesses are")
        print("     refused. Run `kirocrew setup` from a terminal to decide, or set")
        print("     agent.sandbox_allow_unsandboxed_exec=true by hand to opt in.\n")
        return

    def _declared(path: Path) -> bool:
        """Whether *path* explicitly sets the key, in either state."""
        if not path.exists():
            return False
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return False
        agent = doc.get("agent") if isinstance(doc, dict) else None
        return isinstance(agent, dict) and "sandbox_allow_unsandboxed_exec" in agent

    cfg_file = config_path()
    cfg: dict = {}
    if cfg_file.exists():
        try:
            loaded = json.loads(cfg_file.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"  ⚠️  Could not read {cfg_file}: {exc}")
            return
        # A top-level non-object (`[]`, `"x"`, `null`) is not something this step
        # may repair: writing our own object over it would destroy whatever the
        # operator meant, and `.get()` on it raises. Report and leave it alone —
        # the effective default stays fail-closed either way.
        if not isinstance(loaded, dict):
            print(f"  ⚠️  {cfg_file} does not contain a JSON object; skipping.\n")
            return
        cfg = loaded
    if _declared(cfg_file) or _declared(config_local_path()):
        return

    print("── Sandbox ──\n")
    print("  This host offers no OS-level sandbox backend (Linux user namespaces")
    print("  or macOS sandbox-exec), so Kiro Crew currently REFUSES to run agent")
    print("  subprocesses at all — MCP servers, Dev Fleet and the Papyrus")
    print("  compiler will report a sandbox error until you decide.")
    print()
    print("  Allowing them to run unconfined means an agent-driven subprocess can")
    print("  read your home directory, including ~/.aws and ~/.ssh, with no OS")
    print("  confinement. Kiro Crew still scrubs credential environment variables,")
    print("  but it cannot stop a hostile repo or document from reading files.")
    print()
    answer = _input_or_skip("  Allow unsandboxed execution? [y/N]: ")
    if not answer or answer.lower() not in ("y", "yes"):
        print("  ⏭  Left fail-closed — MCP tooling stays disabled on this host.")
        print("     To opt in later, set agent.sandbox_allow_unsandboxed_exec=true")
        print(f"     in {cfg_file}\n")
        return

    if not isinstance(cfg.get("agent"), dict):
        if "agent" in cfg:
            print("  ⚠️  'agent' section is not an object; leaving config untouched.\n")
            return
        cfg["agent"] = {}

    # Audit-or-deny, BEFORE the write: this persists an execution permission, so
    # it belongs in the tamper-evident log next to the ``denied`` event
    # ``wrap_argv`` emits when it refuses a spawn — otherwise the refusals are
    # recorded and the grant that silences them is not. ``critical=True`` makes
    # SEL write synchronously and re-raise on a filesystem failure, and the grant
    # is refused rather than persisted unaudited. Audit-then-write is the safe
    # ordering: a failure between the two leaves a record without a grant, never
    # a grant without a record. The documented manual ``config.json`` edit remains
    # available; it is outside this wizard's control and is not a bypass this
    # step introduces.
    try:
        sel().log_tool_invocation(
            session_key="setup",
            agent="system",
            source="cli_setup._setup_sandbox_consent",
            tool_name="sandbox_allow_unsandboxed_exec",
            tool_kind="config",
            outcome="allowed",
            resources=str(cfg_file),
            metadata={"reason": "operator_consent_at_setup", "probe_kind": kind},
            critical=True,
        )
    except Exception as exc:
        print(f"  ⚠️  Could not record the security audit event: {exc}")
        print("     Refusing to grant unsandboxed execution unaudited —")
        print("     left fail-closed. Fix the audit log, then re-run setup.\n")
        return

    cfg["agent"]["sandbox_allow_unsandboxed_exec"] = True
    try:
        write_config_atomically(cfg_file, cfg)
    except OSError as exc:
        # A locked or read-only config (common on Windows when another process
        # holds it) must not abort the whole wizard after the user has already
        # answered. Report it and continue: nothing was granted, so the host
        # stays fail-closed.
        print(f"  ⚠️  Could not write {cfg_file}: {exc}")
        print("     Nothing was granted — the host stays fail-closed. Set")
        print("     agent.sandbox_allow_unsandboxed_exec=true by hand to opt in.\n")
        return
    print("  ✅ Recorded: agent.sandbox_allow_unsandboxed_exec = true\n")


def _setup_timezone() -> None:
    """Auto-detect timezone and save to config.json."""
    cfg_file = config_path()

    # Check if already configured
    data: dict = {}
    if cfg_file.exists():
        try:
            data = json.loads(cfg_file.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"  ⚠️  Could not read {cfg_file}: {exc}")
            return
    current = data.get("timezone", "")

    # Auto-detect from system
    detected = _detect_system_timezone()

    print("── Timezone ──\n")
    if current:
        print(f"  Current: {current}")
        answer = _input_or_skip(f"  Timezone [{current}]: ")
        if not answer:
            print(f"  ✅ Keeping: {current}\n")
            return
        tz_val = answer
    elif detected:
        print(f"  Detected: {detected}")
        answer = _input_or_skip(f"  Timezone [{detected}]: ")
        tz_val = answer or detected
    else:
        raw = _input_or_skip("  IANA timezone (e.g. America/Los_Angeles): ")
        if not raw:
            print("  ⏭  Skipped. Cron schedules will show UTC.\n")
            return
        tz_val = raw

    # Validate with retry
    abbrev_to_iana: dict[str, str] = {
        "PST": "America/Los_Angeles",
        "PDT": "America/Los_Angeles",
        "MST": "America/Denver",
        "MDT": "America/Denver",
        "CST": "America/Chicago",
        "CDT": "America/Chicago",
        "EST": "America/New_York",
        "EDT": "America/New_York",
        "GMT": "Etc/GMT",
        "BST": "Europe/London",
        "CET": "Europe/Berlin",
        "CEST": "Europe/Berlin",
        "IST": "Asia/Kolkata",
        "JST": "Asia/Tokyo",
        "AEST": "Australia/Sydney",
        "AEDT": "Australia/Sydney",
        "NZST": "Pacific/Auckland",
        "NZDT": "Pacific/Auckland",
    }
    max_retries = 3
    for attempt in range(max_retries):
        try:
            ZoneInfo(tz_val)
            break  # valid
        except (KeyError, Exception):
            suggestion = abbrev_to_iana.get(tz_val.upper())
            if suggestion:
                print(f"  ❌ '{tz_val}' is an abbreviation, not an IANA timezone.")
                print(f"     Did you mean: {suggestion}?")
            else:
                print(f"  ❌ Unknown timezone '{tz_val}'.")
                print("     Use IANA format, e.g. America/Los_Angeles, Europe/London")
            if attempt < max_retries - 1:
                retry = _input_or_skip("  Timezone: ")
                if not retry:
                    print("  ⏭  Skipped.\n")
                    return
                tz_val = retry
            else:
                print("  ⏭  Skipped after too many attempts.\n")
                return

    data["timezone"] = tz_val
    write_config_atomically(cfg_file, data)
    print(f"  ✅ Timezone saved: {tz_val}\n")


def _maybe_setup_dashboard_url() -> None:
    """Prompt for dashboard.url when running on a remote host with Slack configured."""

    cfg_file = config_path()
    cfg = KiroCrewConfig.load()
    creds = cfg.load_credentials()
    has_slack = bool(creds.get("SLACK_APP_TOKEN") and creds.get("SLACK_BOT_TOKEN"))

    if not has_slack:
        return  # No Slack → local-only, no URL needed

    # Detect if this looks like a remote host
    try:
        ip = socket.gethostbyname(socket.gethostname())
        is_remote = not ip.startswith("127.")
    except OSError:
        is_remote = False

    if not is_remote and not cfg.dashboard.url:
        return  # Localhost machine with no existing URL config — skip

    current = cfg.dashboard.url
    hostname = socket.gethostname()

    print("── Dashboard URL (remote access) ──\n")
    if is_remote:
        print(f"  This host ({hostname}) appears to be a remote machine.")
        print("  Setting a dashboard URL enables direct browser access with token auth.")
        print("  Leave blank for localhost-only (SSH tunnel required).\n")
    else:
        print("  Configure a custom dashboard URL for remote access.")
        print("  Leave blank for localhost-only.\n")

    hint = f" [{current}]" if current else ""
    answer = input(f"  Dashboard URL (e.g. http://{hostname}:{_configured_port()}){hint}: ").strip()

    if answer == "" and current:
        print(f"  ✅ Keeping: {current}\n")
        return
    if answer == "" and not current:
        print("  ⏭  Skipped. Dashboard will bind to localhost only.\n")
        return

    # Persist to config.json
    try:
        data: dict = {}
        if cfg_file.exists():
            data = json.loads(cfg_file.read_text(encoding="utf-8"))
        dashboard = data.setdefault("dashboard", {})
        dashboard["url"] = answer
        write_config_atomically(cfg_file, data)
        print(f"  ✅ Dashboard URL saved: {answer}")
        print("  Token auth will be required for all requests.\n")
    except Exception as e:
        print(f"  ❌ Failed to save: {e}\n")


def _maybe_setup_custom_domain() -> None:
    """Inform user of the dashboard URL and clean up legacy mesh.claw from /etc/hosts."""
    port = _configured_port()
    print("\n── Dashboard URL ──\n")
    print(f"  Dashboard available at http://localhost:{port}")
    print(f"  Optional friendlier alias: http://{_CUSTOM_DOMAIN}:{port}")
    print(
        "  (the alias needs no /etc/hosts edit only on browsers that honor RFC 6761\n"
        "   *.localhost — Chrome/Firefox/Linux. Safari and the macOS resolver do NOT\n"
        "   map *.localhost, so prefer plain localhost, especially when tunneling in.)\n"
    )

    # Advise removal of legacy "mesh.claw" entry from /etc/hosts if present
    try:
        if "mesh.claw" in Path("/etc/hosts").read_text(encoding="utf-8"):
            print("  ⚠  Legacy mesh.claw entry found in /etc/hosts.")
            print(
                "  To remove it: sudo grep -v 'mesh\\.claw' /etc/hosts > /tmp/hosts.clean"
                " && sudo mv /tmp/hosts.clean /etc/hosts\n"
            )
    except Exception:
        pass

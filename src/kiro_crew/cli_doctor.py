"""CLI doctor subcommand — verify KiroCrew setup and diagnose issues."""

from __future__ import annotations

import asyncio
import json
import os
import platform as _plat
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from kiro_crew import __version__ as _mc_version
from kiro_crew.acp.client import KIRO_CLI_BIN
from kiro_crew.agent import AGENT_FILENAME, KIRO_AGENTS_DIR
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config import KiroCrewConfig
from kiro_crew.config.loader import config_dir
from kiro_crew.constants import OLLAMA_DOCKER_CONTAINER
from kiro_crew.dashboard.crash_dump_store import (
    dump_age_seconds,
    dump_first_stack_lines,
    get_dumps_dir,
    newest_dump_with_stacks,
)
from kiro_crew.dashboard.origin import (
    is_local_only,
    machine_hostname,
    parse_dashboard_url,
)
from kiro_crew.mcp_cleanup import KIROCREW_BIN_MCP_SERVERS as _MANAGED_MCPS
from kiro_crew.mcp_discovery import McpServerInfo, probe_server
from kiro_crew.platform import (
    PlatformCompositionError,
    current_context,
    safe_context_call,
)
from kiro_crew.transcribe import _find_whisper, ensure_ffmpeg_in_path

_MIN_NODE_VERSION = 16


def _os_fix_hint(mac: str, linux: str) -> str:
    """Return the OS-appropriate Fix hint (brew on macOS, else Linux guidance)."""
    return mac if _plat.system() == "Darwin" else linux


# KiroCrew's agent backend is kiro-cli (the sole public ACP backend). The
# claude-agent-acp binary below is only the dormant protocol seam an internal
# companion re-registers (see acp/client.py) — report it, when present, as that
# optional seam rather than as a user-facing backend.
_CLAUDE_ACP_BIN = "claude-agent-acp"


def _detect_docker_ollama() -> str | None:
    """Return display string if Ollama Docker container exists, else None."""
    docker = shutil.which("docker")
    if not docker:
        return None
    try:
        result = subprocess.run(
            [docker, "inspect", "--format", "{{.State.Status}}", OLLAMA_DOCKER_CONTAINER],
            capture_output=True,
            text=True,
            timeout=5,
        )
        status = result.stdout.strip()
        if result.returncode == 0 and status in ("running", "exited", "paused", "created"):
            return f"Docker ({OLLAMA_DOCKER_CONTAINER}) [{status}]"
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError):
        pass
    return None


def _doctor_mcp_tools(agent_path: Path, issues: list[str]) -> None:
    """Render the `MCP Tools` section of `kirocrew doctor`.

    Two passes scoped to the managed servers (`kirocrew-core`,
    `kirocrew-cron`):

    1. Static sanity check of the agent config: each server must be present
       in ``mcpServers``, ``tools`` and ``allowedTools``. Missing ``tools``
       / ``allowedTools`` entries are auto-appended and the file is
       rewritten atomically. A missing ``mcpServers`` entry cannot be
       auto-added because the command path is install-specific.
    2. Live handshake probe via :func:`mcp_discovery.probe_server`. Reports
       per-server status with tool count on success, and on failure shows
       the error head plus any captured stderr tail from the child — which
       usually contains the real cause (FindupException, ImportError, etc.)
       that would otherwise only exist in kiro-cli's per-session log.
    """
    try:
        agent_data = json.loads(agent_path.read_text(encoding="utf-8"))
    except Exception:
        agent_data = {}

    tools = agent_data.get("tools", [])
    allowed = agent_data.get("allowedTools", [])
    mcps = agent_data.get("mcpServers", {})
    config_changed = False

    probe_targets = []
    for name in _MANAGED_MCPS:
        ref = f"@{name}"
        if name not in mcps:
            print(f"  {ref}: ❌ missing from mcpServers (re-run `kirocrew setup`)")
            issues.append(f"{ref} config")
            continue
        if ref not in tools:
            tools.append(ref)
            config_changed = True
        if ref not in allowed:
            allowed.append(ref)
            config_changed = True

        spec = mcps[name]
        probe_targets.append(
            McpServerInfo(
                name=name,
                command=spec.get("command", ""),
                args=list(spec.get("args", []) or []),
                env=dict(spec.get("env", {}) or {}),
            )
        )

    if config_changed:
        agent_data["tools"] = tools
        agent_data["allowedTools"] = allowed
        agent_data["mcpServers"] = mcps
        atomic_write(agent_path, json.dumps(agent_data, indent=2) + "\n")
        print("  → Auto-fixed agent config")

    if not probe_targets:
        return

    try:

        async def _probe_all() -> list:
            return await asyncio.gather(*(probe_server(t) for t in probe_targets))

        probed = asyncio.run(_probe_all())
    except Exception as exc:
        print(f"  ⚠️  probe failed: {exc}")
        return

    for server in probed:
        ref = f"@{server.name}"
        if server.status == "ok":
            count = len(server.tools)
            noun = "tool" if count == 1 else "tools"
            print(f"  {ref}: ✅ {count} {noun}")
            continue
        head, _, detail = (server.error or "unknown error").partition("\n")
        print(f"  {ref}: ❌ {head or 'unknown error'}")
        if detail:
            for line in detail.splitlines():
                print(f"      {line}")
        issues.append(f"{ref} probe")


def _doctor_ollama_install(issues: list[str]) -> None:
    """Diagnose why Ollama auto-install would fail."""

    system = _plat.system()

    if system == "Darwin":
        brew = shutil.which("brew")
        if brew:
            try:
                result = subprocess.run(
                    [brew, "info", "ollama"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            except subprocess.TimeoutExpired:
                # Homebrew can exceed the timeout during its periodic
                # auto-update (a network tap fetch on the first brew call).
                # Ollama is optional, so degrade to a warning instead of
                # crashing doctor (mirrors the Linux branch below).
                print("  brew info:   ⚠️  timed out checking formula")
            else:
                if result.returncode == 0:
                    print(
                        "  brew info:   ✅ formula available"
                        " (enable will run: brew install ollama)"
                    )
                else:
                    err = result.stderr.strip()[:200]
                    print(f"  brew info:   ❌ {err}")
                    issues.append("ollama (brew cannot resolve formula)")
        else:
            print("  brew:        ⚠️  not found" " — enable will try direct download")
        print("               Install: brew install ollama")
    elif system == "Linux":
        brew = shutil.which("brew")
        brew_formula_ok = False
        if brew:
            try:
                result = subprocess.run(
                    [brew, "info", "ollama"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                brew_formula_ok = result.returncode == 0
            except subprocess.TimeoutExpired:
                print("  brew:        ⚠️  timed out checking formula")
            else:
                if brew_formula_ok:
                    print("  brew:        ✅ (enable will run: brew install ollama)")
                else:
                    print("  brew:        ⚠️  formula not available; will try curl fallback")
        if not brew_formula_ok:
            curl = shutil.which("curl")
            if curl:
                print(
                    "  curl:        ✅ (enable will run:"
                    " curl -fsSL https://ollama.com/install.sh | sh)"
                )
            else:
                print("  curl:        ❌ not found — auto-install will fail")
                issues.append("ollama (curl missing)")
        if brew_formula_ok:
            print("               Install: brew install ollama")
        else:
            print("               Install: curl -fsSL https://ollama.com/install.sh | sh")
    else:
        print(f"  platform:    ❌ auto-install unsupported on {system}")
        issues.append("ollama (unsupported platform)")


def _doctor(platform_boot_error: "Exception | None" = None) -> None:
    """Verify KiroCrew setup — check dependencies, config, credentials, connectivity.

    ``platform_boot_error`` carries a :class:`PlatformCompositionError` from
    ``cli.main`` when the platform context failed to compose (e.g. a profile
    resolved to a non-standalone edition whose companion is missing).  The
    doctor is deliberately allowed to run in that state — diagnosing a broken
    setup is its job — and reports the failure here instead of aborting.
    """

    print("KiroCrew Doctor 🐾\n")
    issues: list[str] = []

    # ── Platform edition ──
    # Report the composed profile, and surface a boot-composition failure as a
    # blocking issue with the remediation hint rather than letting it abort the
    # whole CLI before the doctor can run.
    if platform_boot_error is not None:
        print("Platform")
        print(f"  edition:     ❌ composition failed: {platform_boot_error}")
        issues.append(f"platform composition failed: {platform_boot_error}")
    else:
        print("Platform")
        # Bind the context ONCE for the whole block so the edition line and the
        # jail line describe the same PlatformContext.  A late
        # PlatformCompositionError (boot succeeded, but a lazily-composing adapter
        # or a context swap fails now) is REPORTED as a blocking issue — never
        # swallowed (which would hide it) and never re-raised (which would crash
        # the one command meant to survive a broken setup).  This keeps the
        # edition report and the jail probe consistent on what a composition error
        # means.
        try:
            ctx = current_context()
        except PlatformCompositionError as exc:
            print(f"  edition:     ❌ composition failed: {exc}")
            issues.append(f"platform composition failed: {exc}")
            ctx = None
        except Exception:
            # Never let edition reporting itself break the doctor.
            ctx = None
        if ctx is not None:
            print(f"  edition:     ✅ {ctx.profile}")
            # Process-isolation jail (CPP JailProvider seam).  The public Default
            # has no backend; a companion reports its real status.  Each probe
            # fails OPEN to a safe placeholder so a transient adapter error keeps
            # the doctor non-fatal.  ``safe_context_call`` re-raises a
            # PlatformCompositionError (its fail-closed contract), so wrap the
            # block to REPORT a late composition error as an issue rather than
            # crash the triage command — consistent with the ctx probe above.
            try:
                _jail = ctx.jail
                _jail_status = safe_context_call(
                    lambda: _jail.status_detail(), fallback="status unavailable"
                )
                _jail_on = safe_context_call(lambda: _jail.available(), fallback=False)
                print(f"  jail:        {'✅' if _jail_on else '⏭ '} {_jail_status}")
            except PlatformCompositionError as exc:
                print(f"  jail:        ❌ composition failed: {exc}")
                issues.append(f"jail provider composition failed: {exc}")

    # ── Dependencies ──
    print("Dependencies")
    # kiro-cli is THE agent backend for the public build. claude-agent-acp is
    # only the dormant protocol seam (re-registered by an internal companion),
    # so report it as optional and report kiro-cli as the backend.
    kiro = shutil.which(KIRO_CLI_BIN)
    if kiro:
        print(f"  kiro-cli:    ✅ {kiro}")
        # Check login status — best-effort, never a hard failure
        try:
            r = subprocess.run(
                [KIRO_CLI_BIN, "whoami"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if r.returncode == 0:
                print("  kiro login:  ✅")
            else:
                print("  kiro login:  ⏹ not logged in (run: kiro-cli login)")
        except Exception:
            print("  kiro login:  ⚠️  could not check")
    else:
        print("  kiro-cli:    ⏭  not found (the agent backend)")
        print("               Install kiro-cli per its docs, then: kiro-cli login")

    claude_acp = shutil.which(_CLAUDE_ACP_BIN)
    if claude_acp:
        print(f"  claude-acp:  ✅ {claude_acp} (dormant seam — not used by the public core)")

    git = shutil.which("git")
    if git:
        print(f"  git:         ✅ {git}")
    else:
        print("  git:         ❌ not found (needed for kirocrew update)")
        issues.append("git")

    node = shutil.which("node")
    if node:
        try:
            node_ver_result = subprocess.run(
                ["node", "-v"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            major = int(node_ver_result.stdout.strip().lstrip("v").split(".")[0])
            if major >= _MIN_NODE_VERSION:
                print(f"  node:        ✅ {node} (v{major})")
            else:
                print(
                    f"  node:        ⚠️  v{major} < {_MIN_NODE_VERSION} (frontend needs Node {_MIN_NODE_VERSION}+)"
                )
                print("               Fix: install Node.js >= 16")
        except Exception:
            print(f"  node:        ✅ {node}")
    else:
        print(f"  node:        ⚠️  not found (frontend needs Node {_MIN_NODE_VERSION}+)")
        print("               Fix: install Node.js >= 16")

    # venv detection — used by the runtime section below
    venv_py = Path(__file__).resolve().parents[2] / ".venv" / "bin" / "python3"
    is_venv_install = venv_py.is_file()

    # ── Project ──
    print("\nProject")
    proj = os.environ.get("KIROCREW_PROJECT_DIR", "")
    stale_project = False
    if not proj:
        # Check saved project_dir file
        saved_proj = config_dir() / "project_dir"
        if saved_proj.is_file():
            saved = saved_proj.read_text(encoding="utf-8").strip()
            if saved and Path(saved).is_dir():
                proj = saved
            else:
                print(f"  project dir: ❌ stale — points to deleted {saved}")
                print(f"               Fix: rm {config_dir() / 'project_dir'}")
                issues.append("stale project_dir")
                stale_project = True
    if proj and Path(proj).is_dir():
        print(f"  project dir: ✅ {proj}")
        git_dir = Path(proj) / ".git"
        if git_dir.is_dir():
            print("  git repo:    ✅")
        else:
            print("  git repo:    ⚠️  not a git repo")
    elif not stale_project:
        print("  project dir: ⚠️  not set (run kirocrew setup from project root)")

    # ── Agent config ──
    print("\nAgent")
    agent_path = KIRO_AGENTS_DIR / AGENT_FILENAME
    if agent_path.exists():
        print(f"  config:      ✅ {agent_path}")
    else:
        print("  config:      ❌ not found (run kirocrew setup)")
        issues.append("agent config")

    # ── Config ──
    print("\nConfiguration")
    cfg_dir = config_dir()
    cfg = KiroCrewConfig.load()
    if cfg_dir.exists():
        print(f"  config dir:  ✅ {cfg_dir}")
    else:
        print(f"  config dir:  📁 {cfg_dir} (will be created)")
    print(f"  provider:    {cfg.agent.provider}")
    print(f"  model:       {cfg.agent.model}")
    print(f"  approval:    {cfg.agent.approval_mode}")
    _host: str = ""
    _port: int | None = None
    try:
        _host, _port = parse_dashboard_url(cfg.dashboard.url)
    except Exception:
        print("  dashboard:   ⚠️  cannot parse dashboard URL from config")
        issues.append("dashboard URL misconfigured")
    _display_host = _host or "localhost"
    if _port:
        print(f"  dashboard:   http://{_display_host}:{_port}")

    # Dashboard auth mode
    creds = cfg.load_credentials()
    _has_slack = bool(creds.get("SLACK_APP_TOKEN") and creds.get("SLACK_BOT_TOKEN"))
    _local = is_local_only(_host, _has_slack)
    if _local:
        print("  bind:        127.0.0.1 (local-only, SSH tunnel for remote)")
        print("  auth:        loopback trusted (no token required)")
    else:
        print("  bind:        0.0.0.0 (all interfaces)")
        print("  auth:        ✅ token auth required (via !dashboard)")
        if not _has_slack:
            print("  auth:        ⚠️  Slack not configured — token generation unavailable")
            issues.append("dashboard auth: remote bind without Slack")

    # ── MCP Tools ──
    print("\nMCP Tools")
    if agent_path.exists():
        _doctor_mcp_tools(agent_path, issues)

    # ── Python Runtime ──
    print("\nRuntime")
    # Prefer venv install (pip install -e); otherwise verify the running Python.
    if is_venv_install:
        try:
            py_result = subprocess.run(
                [str(venv_py), "--version"], capture_output=True, text=True, timeout=5
            )
            py_result.check_returncode()
            ver = py_result.stdout.strip()
            print(f"  python:      ✅ {venv_py} ({ver})")
        except Exception as exc:
            print(f"  python:      ❌ venv python broken: {exc}")
            issues.append("venv python")
        else:
            try:
                subprocess.run(
                    [str(venv_py), "-c", "import websockets, slack_sdk, aiohttp"],
                    capture_output=True,
                    timeout=5,
                ).check_returncode()
                print("  deps:        ✅ websockets, slack_sdk, aiohttp available")
            except Exception:
                print("  deps:        ❌ missing modules (websockets/slack_sdk/aiohttp)")
                issues.append("python deps")
    else:
        print(f"  python:      ✅ {sys.executable} ({sys.version.split()[0]})")
        print(f"  kiro_crew:   ✅ {_mc_version}")
        try:
            import aiohttp  # noqa: F401
            import slack_sdk  # noqa: F401
            import websockets  # noqa: F401

            print("  deps:        ✅ websockets, slack_sdk, aiohttp available")
        except ImportError:
            print("  deps:        ❌ missing modules (websockets/slack_sdk/aiohttp)")
            print("               Fix: pip install -e .")
            issues.append("python deps")

    # SQLite FTS5 — required by memory + knowledge full-text search. On macOS
    # and Linux aarch64 we rely on the host sqlite3 build (pysqlite3-binary is
    # x86_64-Linux only); a build without FTS5 breaks memory init.
    try:
        from kiro_crew._sqlite_compat import fts5_available

        if fts5_available():
            print("  sqlite fts5: ✅ available")
        else:
            print("  sqlite fts5: ❌ missing (memory/knowledge search will fail)")
            print("               Fix: pip install pysqlite3-binary, or use a")
            print("               Python whose SQLite was built with FTS5.")
            issues.append("sqlite fts5")
    except Exception as exc:  # pragma: no cover - defensive
        print(f"  sqlite fts5: ⚠️  could not check ({exc})")

    # ── Ollama / Vector Memory ──
    print("\nVector Memory (Ollama)")

    ollama = shutil.which("ollama") or _detect_docker_ollama()
    if ollama:
        print(f"  ollama:      ✅ {ollama}")
    else:
        print("  ollama:      ⏹ not installed (optional — vector memory)")
        _doctor_ollama_install(issues)

    if ollama:
        try:
            with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2) as resp:
                print("  server:      ✅ running")
        except Exception:
            print("  server:      ⏹ not running (will auto-start on enable)")

    # Check embedding config
    if cfg.memory.embedding_provider == "ollama":
        print("  embeddings:  ✅ enabled")
    else:
        print("  embeddings:  ⏹ disabled (enable from dashboard → Overview → Memory)")

    # ── Speech-to-Text (optional) ──
    print("\nSpeech-to-Text")
    stt_active = cfg.stt.enabled
    needs_whisper = stt_active and cfg.stt.provider == "whisper"
    needs_ffmpeg = stt_active  # both providers use ffmpeg

    if not stt_active:
        print("  status:      ⏹ disabled (enable from dashboard → Overview → Slack)")
    else:
        print(f"  provider:    ✅ {cfg.stt.provider}")

    whisper_bin = _find_whisper(cfg.stt.whisper_path)
    if whisper_bin:
        print(f"  whisper:     ✅ {whisper_bin}")
    elif needs_whisper:
        print("  whisper:     ❌ not found")
        print(
            "               Fix: "
            + _os_fix_hint(
                "brew install openai-whisper",
                "pipx install openai-whisper  (or pip install --user openai-whisper)",
            )
        )
        issues.append("whisper")
    else:
        print("  whisper:     ⏭  not installed (not needed)")

    ensure_ffmpeg_in_path()
    ffmpeg_bin = shutil.which("ffmpeg")
    if ffmpeg_bin:
        print(f"  ffmpeg:      ✅ {ffmpeg_bin}")
    elif needs_ffmpeg:
        print("  ffmpeg:      ❌ not found")
        print(
            "               Fix: "
            + _os_fix_hint(
                "brew install ffmpeg",
                "drop a static ffmpeg build into ~/.local/bin "
                "(not in AL2023 repos; KiroCrew auto-detects it)",
            )
        )
        issues.append("ffmpeg")
    else:
        print("  ffmpeg:      ⏭  not installed (not needed)")

    # Cloud transcription (AWS Transcribe) is an OPTIONAL feature requiring
    # user-provided AWS credentials and the `amazon-transcribe`/`boto3` extras.
    # It is never a hard failure on a standard install — report gracefully.
    if stt_active and cfg.stt.provider == "transcribe":
        try:
            import amazon_transcribe.client  # noqa: F401

            print("  transcribe:  ✅ amazon_transcribe importable (optional)")
        except ImportError:
            print("  transcribe:  ⏹ optional cloud STT not installed")
            print("               Install: pip install 'kirocrew[voice]'")

        try:
            import boto3  # noqa: F401

            print("  boto3:       ✅ importable (optional)")
        except ImportError:
            print("  boto3:       ⏹ optional AWS SDK not installed")
            print("               Install: pip install 'kirocrew[voice]'")

    # ── Slack (optional) ──
    print("\nSlack Integration")
    creds = cfg.load_credentials()
    has_slack = bool(creds.get("SLACK_APP_TOKEN") and creds.get("SLACK_BOT_TOKEN"))
    if has_slack:
        has_owner = bool(creds.get("KIROCREW_OWNER_ID"))
        print("  tokens:      ✅ configured")
        if has_owner:
            print(f"  owner:       ✅ {creds['KIROCREW_OWNER_ID']}")
        else:
            print("  owner:       ⚠️  KIROCREW_OWNER_ID not set")

        # Optional workspace allowlist validation (default-open unless the user
        # configured slack.allowed_enterprise_ids).
        bot_token = creds.get("SLACK_BOT_TOKEN", "")
        if bot_token:
            extra_ids = cfg.slack_enterprise_ids
            # Route through the active PlatformContext's Slack gate so the doctor
            # reports the SAME enterprise-gate decision the gateway enforces
            # (slack/events.py uses the context gate). The Default gate delegates
            # to enterprise.validate_enterprise, so standalone is unchanged.
            if current_context().slack_gate.validate_enterprise(bot_token, extra_ids=extra_ids):
                print("  workspace:   ✅ allowed")
            else:
                print("  workspace:   ❌ not in configured workspace allowlist")
                print("               The gateway will refuse to connect.")
                issues.append("slack workspace: not in allowlist")
    else:
        print("  status:      ⏭  not configured (dashboard-only mode)")
        print("  setup:       run 'kirocrew setup' to add Slack tokens")

    # ── Loop-stall crash dumps ──
    print("\nLoop-stall Crash Dumps")
    try:
        dumps_dir = get_dumps_dir()
        _latest = newest_dump_with_stacks(dumps_dir)
        if _latest is not None:
            _age_s = dump_age_seconds(_latest)
            if _age_s < 7 * 86400:  # Less than 7 days old
                _age_h = _age_s / 3600
                print(f"  last dump:   ⚠️  {_latest.name} ({_age_h:.1f}h ago)")
                _stack = dump_first_stack_lines(_latest, max_lines=5)
                if _stack:
                    print("  MainThread stuck at:")
                    for _line in _stack:
                        print(f"    {_line}")
                issues.append(f"recent loop-stall crash dump ({_age_h:.0f}h ago)")
            else:
                print(f"  last dump:   ✅ oldest only ({_age_s / 86400:.0f}d ago, no recent stalls)")
        else:
            print("  dumps:       ✅ no crash dumps found (healthy)")
        print(f"  dump dir:    {dumps_dir}")
    except Exception as exc:
        print(f"  crash dumps: ⚠️  check failed ({exc})")

    # ── Connectivity ──
    print("\nConnectivity")
    if kiro:
        kiro_result = subprocess.run(
            [KIRO_CLI_BIN, "--version"], capture_output=True, text=True, timeout=5
        )
        if kiro_result.returncode == 0:
            ver = kiro_result.stdout.strip() or kiro_result.stderr.strip()
            print(f"  kiro-cli:    ✅ {ver}")
        else:
            print("  kiro-cli:    ⚠️  exits with error (optional backend)")
    else:
        print("  kiro-cli:    ⏭  skipped (not installed)")

    # Check if gateway is running — connect to 127.0.0.1 (loopback)
    # to avoid DNS resolution issues with the configured hostname.
    # Any HTTP response (even 401/403 from token auth) means the gateway is up.
    is_remote = bool(os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_CLIENT"))

    if _port:
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{_port}/api/status")
            with urllib.request.urlopen(req, timeout=2) as resp:
                data = json.loads(resp.read())
            print(f"  gateway:     ✅ running (uptime {data.get('uptime', '?')})")
        except urllib.error.HTTPError as he:
            # 401/403 means gateway is running but requires token auth
            if he.code in (401, 403):
                print("  gateway:     ✅ running (token auth enabled)")
            else:
                print(f"  gateway:     ⚠️  HTTP {he.code}")
        except (urllib.error.URLError, OSError):
            print("  gateway:     ⏹  not running")
        except Exception:
            print("  gateway:     ⚠️  running but returned unexpected response")

        # SSH tunnel hint for remote hosts
        if is_remote:
            mh = machine_hostname() or "this-host"
            print("\n  💡 Remote access: Run on your LOCAL machine:")
            print(f"     ssh -NL {_port}:localhost:{_port} {mh}")
            print("     Then run: kirocrew token")

    # Verify token auth is enforced on non-loopback (security check)
    if _port and not _local:
        if not _host:
            issues.append("cannot verify dashboard auth (host unknown)")
        else:
            try:
                ext_req = urllib.request.Request(f"http://{_host}:{_port}/api/status")
                try:
                    with urllib.request.urlopen(ext_req, timeout=2) as resp:
                        # 200 without token = auth is NOT enforced
                        print("  auth check:  ❌ external access allowed without token!")
                        issues.append("dashboard auth: no token required on external interface")
                except urllib.error.HTTPError as he:
                    if he.code in (401, 403):
                        print("  auth check:  ✅ token required on external interface")
                    else:
                        print(f"  auth check:  ⚠️  HTTP {he.code}")
            except Exception:
                print("  auth check:  ⏭  could not reach external interface")

    # ── Summary ──
    print()
    if issues:
        print(f"❌ Fix these issues: {', '.join(issues)}")
        sys.exit(1)
    else:
        print("✅ KiroCrew is ready!")

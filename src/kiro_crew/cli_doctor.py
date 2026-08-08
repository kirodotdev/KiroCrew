"""CLI doctor subcommand — verify KiroCrew setup and diagnose issues."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform as _plat
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from kiro_crew import __version__ as _mc_version
from kiro_crew import diagnostics, platform_compat
from kiro_crew.acp.client import KIRO_CLI_BIN
from kiro_crew.agent import AGENT_FILENAME
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config import KiroCrewConfig
from kiro_crew.config.loader import config_dir
from kiro_crew.config.paths import (
    LEGACY_CONFIG_DIR_NAME,
    MIGRATION_MARKER_NAME,
    _valid_override_home,
    detect_data_home_conflict,
    kiro_agents_dir,
    preserved_entries,
)
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
from kiro_crew.embeddings import (
    _LIB_PATH_ENV,
    _load_llama_class,
    _platform_libs_dirname,
    _resolve_model_url,
    default_model_path,
    model_file_present,
    resolve_custom_model,
    verify_vendored_libs,
)
from kiro_crew.mcp_cleanup import KIROCREW_BIN_MCP_SERVERS as _MANAGED_MCPS
from kiro_crew.mcp_discovery import McpServerInfo, probe_server
from kiro_crew.platform import (
    PlatformCompositionError,
    current_context,
    safe_context_call,
)
from kiro_crew.platform.governance import CU_MCP_SERVER, may_skip_gate_now
from kiro_crew.sandbox import warm_backend
from kiro_crew.sel import sel
from kiro_crew.transcribe import _find_whisper, ensure_ffmpeg_in_path

logger = logging.getLogger(__name__)

_MIN_NODE_VERSION = 16


# ``KIRO_AGENTS_DIR`` is an import-time override hook, NOT a frozen path.
# ``None`` means "resolve from the live data home"; tests patch this
# attribute directly (``patch("kiro_crew.cli_doctor.KIRO_AGENTS_DIR", tmp)``),
# so the name is kept and read through ``_agents_dir()``.
KIRO_AGENTS_DIR: Path | None = None


def _agents_dir() -> Path:
    """Kiro agents directory, honoring the override hook, else the live home."""
    return KIRO_AGENTS_DIR if KIRO_AGENTS_DIR is not None else kiro_agents_dir()


def _os_fix_hint(mac: str, linux: str, windows: str | None = None) -> str:
    """Return the OS-appropriate Fix hint (brew on macOS, winget on Windows,
    else Linux guidance).

    Without a Windows arm, Windows fell through to the Linux text — telling a
    Windows user to ``pipx``/drop a static build in ``~/.local/bin``, neither of
    which applies. When *windows* is omitted the Linux text is still used, so
    callers only pass it where a Windows-specific remedy exists.
    """
    if _plat.system() == "Darwin":
        return mac
    if windows is not None and _plat.system() == "Windows":
        return windows
    return linux


# KiroCrew's agent backend is kiro-cli (the sole public ACP backend). The
# claude-agent-acp binary below is only the dormant protocol seam an internal
# companion re-registers (see acp/client.py) — report it, when present, as that
# optional seam rather than as a user-facing backend.
_CLAUDE_ACP_BIN = "claude-agent-acp"

# Managed servers doctor must NEVER add to ``allowedTools``.
#
# ``allowedTools`` is kiro-cli's blanket auto-approve list, and an auto-approved
# MCP tool is approved LOCALLY by kiro-cli: it emits no permission request and
# therefore NEVER reaches ``hooks.on_tool_call`` — the PreToolUse plane that
# carries the always-on deny floor, the sensitive-path check and the governance
# ceiling.  ``agent.py``'s managed spec deliberately omits ``autoApprove`` for
# exactly this reason (a tool that can click and type into an
# already-authenticated application must stay behind a prompt), and a diagnostic
# command must not silently undo that.  Doctor still repairs the ``tools`` entry,
# which only makes the server's tools *reachable*, never pre-approved.
_NO_BLANKET_ALLOW_MCPS = frozenset({CU_MCP_SERVER})


def _doctor_mcp_tools(agent_path: Path, issues: list[str]) -> None:
    """Render the `MCP Tools` section of `kirocrew doctor`.

    Two passes scoped to the managed servers (`kirocrew-core`,
    `kirocrew-cron`, `kirocrew-computer`):

    1. Static sanity check of the agent config: each server must be present
       in ``mcpServers`` and ``tools``. Missing ``tools`` entries — and
       ``allowedTools`` entries for every server outside
       :data:`_NO_BLANKET_ALLOW_MCPS` — are auto-appended and the file is
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
        # `allowedTools` auto-approves, which is the one path that never reaches
        # the PreToolUse gate — so what the ceiling says about this server decides
        # both whether doctor may mint a grant and whether an existing one stands.
        if not may_skip_gate_now(ref):
            # REVOKE, not merely "do not add". A grant can predate the ceiling —
            # the policy arrives on a host whose config was written while it was
            # ungoverned — and leaving it in place means the ceiling applies only
            # to installs that were governed before their first launch. Every
            # other writer of this list revokes here too (agent.py's shared sync,
            # both dashboard enable paths); doctor was the one that only declined
            # to mint, which left `kirocrew doctor` reporting a repaired config
            # that still carried the exemption.
            #
            # This is the one case where doctor removes something from
            # `allowedTools`: the note below about never removing a user's
            # decision holds for user preference, and a ceiling is not one.
            if ref in allowed:
                allowed.remove(ref)
                config_changed = True
                # Revoking a grant is a permission DECISION; every other writer of
                # this list emits this SEL event when it withholds, and doctor
                # revoking silently would be the one path with no audit trail.
                try:
                    sel().log_api_access(
                        caller="system",
                        operation="mcp_auto_approve_withheld",
                        outcome="ok",
                        source="cli_doctor",
                        resources=(
                            f"{ref} auto-approve revoked (governance ceiling); "
                            "calls go through the approval gate"
                        ),
                    )
                except Exception:  # noqa: BLE001 — the audit must not break doctor
                    logger.debug("SEL audit unavailable for doctor revoke", exc_info=True)
            # Governed hosts otherwise give no reason why a server the user
            # enabled still prompts on every call — say it once, here, so
            # `kirocrew doctor` explains it.
            print(f"  {ref}: 🔒 auto-approve withheld by security policy — calls will prompt")
        elif ref not in allowed and name not in _NO_BLANKET_ALLOW_MCPS:
            # Computer use is never blanket-allowed here: see _NO_BLANKET_ALLOW_MCPS.
            # A pre-existing user-made grant is left alone (doctor never REMOVES a
            # decision the user owns); doctor simply never mints one.
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

    # Every probe below spawns its server through the sandbox chokepoint, and
    # asyncio.gather releases them together. On a cold cache the first arrivals
    # therefore land on the on-loop deferral path simultaneously and each logs a
    # transient probe failure — noise that reads as a real sandbox fault during a
    # health check whose subject is MCP, not the sandbox. Warm the cache here,
    # off any loop, so the probes see a settled verdict.
    #
    # The chokepoint helper is deliberately NOT named here: test_spawn_audit
    # classifies a spawn as sandbox-routed by substring-scanning the enclosing
    # function's source, so spelling that identifier even in a comment flips this
    # function's classification and then demands a resource-limit preexec_fn it
    # does not own. The routing genuinely happens inside
    # mcp_discovery.probe_server, not here.
    #
    # Failing to warm is non-fatal BY DESIGN (the cache stays cold and the
    # self-healing transient path applies), so it must not be able to abort the
    # command. `warm_backend` starts a thread, and `Thread.start()` raises when
    # the process is out of threads — precisely the degraded state someone runs
    # `doctor` to diagnose, which is the worst moment for the diagnostic itself
    # to die. Swallow it here rather than inside the probe `try` below, so a warm
    # failure is never misreported as an MCP probe failure.
    try:
        warm_backend()
    except Exception:
        logger.debug("sandbox probe warm failed; probes will re-probe", exc_info=True)

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


def _doctor_data_home() -> None:
    """Report the data home and any leftover pre-move legacy home.

    The one-time ``~/.kirocrew`` -> ``~/.kiro/crew`` migration force-copies the
    old home into the new one (overwriting anything already there), writes a
    completion marker, and then deletes ``~/.kirocrew``'s DATA — there is no
    rollback copy. A leftover ``~/.kirocrew`` here is rendered as one of several
    states: a **conflict** (marker present + non-preserved leftovers → resurrection
    debris that is never used and needs manual cleanup), **IGNORED** (a valid
    ``KIROCREW_HOME`` override is active, so migration is disabled), a **retained
    venv** (marker present + only a preserved virtual environment → expected, and
    explicitly NOT safe to delete, since it is the live interpreter), **UNUSED**
    (marker present + empty legacy → migration already completed, harmless
    leftover), or a genuine **pending** migration (no marker yet → it retries on
    the next cold start). Purely informational — doctor never deletes it itself.
    """
    print("\nData Home")
    home = config_dir()
    print(f"  location:    ✅ {home}")

    conflict = detect_data_home_conflict()
    legacy = Path.home() / LEGACY_CONFIG_DIR_NAME
    if conflict:
        # marker present + non-empty legacy → the legacy is debris, NOT a
        # pending migration; it is never used and needs manual cleanup.
        print(f"  ⚠ conflict:  {legacy} exists but is NOT used (migration already completed).")
        print(f"               {conflict}")
    elif legacy.is_dir():
        override_home = _valid_override_home()
        if override_home is not None:
            try:
                points_at_legacy = override_home == legacy.resolve()
            except OSError:  # pragma: no cover - defensive
                points_at_legacy = override_home == legacy
            if points_at_legacy:
                # The override points AT the legacy dir, so legacy IS the active
                # data home — not ignored debris (don't mislabel the home the
                # process is actually using).
                print(
                    f"  legacy:      ✅ {legacy} is the ACTIVE data home "
                    f"(KIROCREW_HOME override points to it)"
                )
            else:
                # A valid KIROCREW_HOME override elsewhere bypasses migration on
                # every start, so this legacy dir will NOT be migrated — don't
                # imply a retry.
                print(
                    f"  legacy:      ⏹ {legacy} present but IGNORED "
                    f"(KIROCREW_HOME override active — migration disabled until it is unset)"
                )
        elif (home / MIGRATION_MARKER_NAME).exists():
            # Marker present + a legacy dir that detect_data_home_conflict did not
            # flag. Either it is empty leftover, or it survives ONLY to hold a
            # preserved virtual environment — which must NOT be described as safe
            # to delete, since that is the user's live interpreter.
            preserved = preserved_entries(legacy)
            if preserved:
                print(
                    f"  legacy:      ✅ {legacy} retained to hold the KiroCrew "
                    f"virtual environment ({', '.join(preserved)})"
                )
                print(
                    f"               Data was migrated to {home}; the venv stays "
                    f"here because moving it would break the interpreter."
                )
                print(
                    "               Do NOT delete it while it is your active "
                    "install (`which kirocrew` resolves through it)."
                )
            else:
                print(
                    f"  legacy:      ⏹ {legacy} present but UNUSED "
                    f"(migration already completed; empty leftover, safe to delete)"
                )
        else:
            print(
                f"  legacy:      ⏹ {legacy} still present (migration will retry on next cold start)"
            )


def _linger_enabled(user: str) -> bool | None:
    """Whether ``user``'s systemd instance lingers past logout.

    ``None`` when it cannot be determined (no ``loginctl``, unknown user, or an
    unrecognised value) so the caller can stay quiet rather than guess.
    """
    if shutil.which("loginctl") is None:
        return None
    try:
        res = subprocess.run(
            ["loginctl", "show-user", user, "-p", "Linger", "--value"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    val = res.stdout.strip().lower()
    if val in ("yes", "true", "1"):
        return True
    if val in ("no", "false", "0"):
        return False
    return None


def _doctor_pod_session_bus(issues: list[str]) -> None:
    """Report whether pods have a reachable ``systemd --user`` session bus.

    Pods are ``systemd --user`` units, so ``systemctl --user`` must be able to
    reach the per-user systemd instance. A gateway started from a systemd SYSTEM
    unit (``kirocrew service install``) inherits no login-session environment,
    and if the per-user instance is not running at all there is nothing for
    KiroCrew to point at — every pod verb then fails with "Failed to connect to
    bus: No medium found". Diagnosing that belongs here.

    Three outcomes: socket present → pass; absent → ❌ with the remediation;
    present but ``Linger=no`` → warn, because pods work now and will die on
    logout.

    Advisory only (never appended to ``issues``, like the embedding-model URL
    probe): a host with no per-user systemd instance — a container, a CI runner,
    a headless server — is not a broken install, it is one where an optional dev
    feature is unavailable. macOS and Windows already report that as "not
    applicable" and block nothing, so blocking on Linux would be inconsistent as
    well as a false alarm for everyone who never runs a pod.

    Doctor only reports: enabling linger changes the user's login-session
    lifetime and is theirs to choose, never a side effect of installing a
    service.
    """
    del issues  # advisory-only diagnostic; keeps the call-site signature uniform
    print("\nPods")
    if not sys.platform.startswith("linux"):
        print(
            f"  session bus: ⏹ not applicable ({sys.platform} — pods are "
            "Linux `systemd --user` only)"
        )
        return
    if shutil.which("systemctl") is None:
        print("  session bus: ⏹ not applicable (no `systemctl` on PATH)")
        return

    # Local import: keeps the pod package out of the CLI's import graph for
    # every other command (circular-safe — pod.runtime imports no CLI module).
    from kiro_crew.pod.runtime import has_session_bus, session_bus_socket

    uid = getattr(os, "getuid", lambda: -1)()
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or str(uid)
    sock = session_bus_socket()
    if not has_session_bus():
        print(f"  session bus: ❌ none for uid {uid} (looked for {sock})")
        print("               Pods are systemd --user units, so `kirocrew pod` is")
        print("               unavailable until one exists. Everything else works.")
        print(f"               Fix: loginctl enable-linger {user}")
        return
    print(f"  session bus: ✅ {sock}")
    if _linger_enabled(user) is False:
        print("  linger:      ⚠️  disabled — the per-user systemd instance exits on " "logout,")
        print("               taking running pods with it. " f"Fix: loginctl enable-linger {user}")


def _doctor_model_url_reachable(issues: list[str]) -> None:
    """Light HTTPS-reachability probe of the resolved embedding-model URL.

    Only runs when the model file is absent (a present model needs no
    download). A HEAD request bounded to 5s — reports the endpoint's
    reachability so a blocked/misconfigured CDN or mirror is diagnosed here
    instead of as a silent background-download failure loop. Advisory only
    (never appended to ``issues``): an absent model is a normal transient
    state — the background download retries with backoff on every boot.
    """
    del issues  # advisory-only diagnostic; keeps the call-site signature uniform
    from kiro_crew.embeddings import redact_model_url  # circular-safe (no loader)

    url = _resolve_model_url()
    safe = redact_model_url(url)
    try:
        req = urllib.request.Request(url, method="HEAD")
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- _resolve_model_url enforces https://; HEAD-only reachability probe
        with urllib.request.urlopen(req, timeout=5) as resp:
            print(f"  model url:   ✅ reachable ({resp.status}) {safe}")
    except urllib.error.HTTPError as exc:
        print(f"  model url:   ❌ HTTP {exc.code} from {safe}")
        print("               Fix: set KIROCREW_EMBED_MODEL_URL (or memory.embed_model_url)")
        print("               to a mirror hosting the GGUF; the sha256 pin still verifies it.")
    except Exception as exc:
        print(f"  model url:   ❌ unreachable ({exc}) {safe}")
        print("               Check network connectivity; the background download will")
        print("               keep retrying with backoff on every gateway boot.")


def _doctor(platform_boot_error: "Exception | None" = None, bundle: bool = False) -> None:
    """Verify KiroCrew setup — check dependencies, config, credentials, connectivity.

    ``platform_boot_error`` carries a :class:`PlatformCompositionError` from
    ``cli.main`` when the platform context failed to compose (e.g. a profile
    resolved to a non-standalone edition whose companion is missing).  The
    doctor is deliberately allowed to run in that state — diagnosing a broken
    setup is its job — and reports the failure here instead of aborting.
    """

    print("Kiro Crew Doctor 👻\n")
    issues: list[str] = []

    # ── Diagnostics bundle (--bundle) ──
    # Short-circuit: collect logs + crash reports into a redacted zip and print
    # the local path plus a pre-filled GitHub issue URL, then exit. Shares the
    # exact collector the dashboard "Report a Problem" button uses.
    if bundle:
        print("Collecting diagnostics bundle (secrets are redacted)...\n")
        # The collector touches the filesystem in several places that can fail for
        # ordinary reasons — an unwritable data home, a plain FILE sitting where
        # `diagnostics/` should be, a full disk. Letting OSError escape prints a
        # traceback at the one moment the user is already trying to report a
        # failure, so fail with a readable message and a nonzero status instead.
        try:
            result = diagnostics.collect_bundle()
        except OSError as exc:
            print(f"  ❌ could not write the diagnostics bundle: {exc}")
            print("     Check that ~/.kiro/crew is writable and has free space.")
            sys.exit(1)
        print(f"  ✅ bundle: {result.zip_path}")
        print(
            f"     {len(result.included)} file(s) · "
            f"{result.total_redactions} secret(s) redacted"
        )
        if result.skipped:
            print(f"     skipped (not found): {', '.join(result.skipped)}")
        print("\n  Open a pre-filled GitHub issue (then drag the zip in):")
        print(f"  {result.github_issue_url}")
        return

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

    # venv detection — used by the runtime section below. Windows venvs put the
    # interpreter under .venv\Scripts\python.exe, not .venv/bin/python3, so a
    # hardcoded POSIX layout misreported the venv (and the runtime section) on
    # every Windows install.
    if platform_compat.IS_WINDOWS:
        venv_py = Path(__file__).resolve().parents[2] / ".venv" / "Scripts" / "python.exe"
    else:
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
        # A git worktree or submodule stores ``.git`` as a FILE holding a
        # ``gitdir:`` pointer, not a directory, so accept both forms.
        git_marker = Path(proj) / ".git"
        if git_marker.exists():
            print("  git repo:    ✅")
        else:
            print("  git repo:    ⚠️  not a git repo")
    elif not stale_project:
        print("  project dir: ⚠️  not set (run kirocrew setup from project root)")

    # ── Agent config ──
    print("\nAgent")
    agent_path = _agents_dir() / AGENT_FILENAME
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

    # ── Data Home (+ leftover migration archive) ──
    _doctor_data_home()

    # ── Pods (systemd --user session bus) ──
    _doctor_pod_session_bus(issues)

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

    # ── Vector Memory (in-process embeddings) ──
    print("\nVector Memory (in-process embeddings)")

    # Read BEFORE _load_llama_class(): the loader `setdefault`s this var to its
    # OWN bundled libs dir, so after the call an unset var is indistinguishable
    # from an operator override pointing at the bundle.
    _lib_path_override = os.environ.get(_LIB_PATH_ENV, "")

    if _load_llama_class() is not None:
        print("  runtime:     ✅ vendored llama-cpp-python importable")
    elif _platform_libs_dirname() is None:
        # Designed degradation, not a defect: no vendored native libs exist for
        # this platform (e.g. darwin/x86_64) and embeddings.py documents the
        # keyword-search fallback. Nothing for the user to fix — don't fail.
        print(
            "  runtime:     ⏹ unsupported platform "
            f"({sys.platform}/{_plat.machine()}) — memory uses keyword search"
        )
    else:
        print("  runtime:     ❌ vendored runtime failed to load")
        # Distinguish an incomplete SHIPPED payload from a load failure on a
        # complete one. Both surface as the same ctypes "base name 'llama' not
        # found", but only the former is a packaging defect the user cannot fix
        # by configuration — and naming the absent files is what stops the
        # diagnosis from being misread as an unsupported architecture.
        #
        # Mirrors the loader's LLAMA_CPP_LIB_PATH exemption: under an override
        # the libs load from the operator's directory, so blaming the bundled
        # tree would send them to reinstall a package they are deliberately not
        # loading from, while saying nothing about the dir that actually failed.
        _plat_dir = _platform_libs_dirname()
        _absent = (
            [] if _lib_path_override else verify_vendored_libs().get(_plat_dir or "", [])
        )
        if _absent:
            print(f"               Missing native libs for {_plat_dir}: {', '.join(_absent)}")
            print("               This install's vendored llama.cpp is incomplete (packaging")
            print("               defect, not an unsupported platform) — reinstall Kiro Crew")
            print("               from a current release to restore vector memory.")
        elif _lib_path_override:
            print(f"               {_LIB_PATH_ENV} is set — the libs load from")
            print(f"               {_lib_path_override}, not the bundled tree.")
            print("               Verify that directory holds a complete llama.cpp closure.")
        issues.append("embedding runtime")

    # FAISS is an optional accelerator — never a dependency, on any platform.
    # Without it, episodic recall uses the stdlib cosine fallback (correct, just
    # slower on a large store). Report it as an informational note, never an
    # issue, so the user knows the speed-up exists without doctor failing.
    try:
        import faiss  # noqa: F401

        print("  faiss:       ✅ vector-search accelerator installed")
    except ImportError:
        print(
            "  faiss:       ⏹ not installed (optional) — episodic recall uses "
            "the stdlib fallback; `pip install faiss-cpu` to accelerate it"
        )

    _custom = resolve_custom_model()
    if _custom is not None:
        # A custom model is configured. Never suggest the CDN here: the default
        # model is deliberately not downloaded in this mode, so its reachability
        # is irrelevant and pointing at it would be misleading advice.
        if _custom.error:
            print(f"  model:       ❌ custom model unusable — {_custom.error}")
            issues.append("custom embedding model unusable")
        elif model_file_present():
            print(f"  model:       ✅ {_custom.path} (custom)")
            print(f"  vector space: {_custom.model_id} @ {_custom.dim}d")
        else:
            print(f"  model:       ❌ custom model not readable: {_custom.path}")
            issues.append("custom embedding model unreadable")
    elif model_file_present():
        print(f"  model:       ✅ {default_model_path()}")
    else:
        print("  model:       ⏹ not downloaded yet (downloads in background on gateway start)")
        _doctor_model_url_reachable(issues)

    print("  embeddings:  ✅ always-on")

    # ── Speech-to-Text (optional) ──
    print("\nSpeech-to-Text")
    stt_active = cfg.stt.enabled
    needs_whisper = stt_active and cfg.stt.provider == "whisper"
    needs_ffmpeg = stt_active  # both providers use ffmpeg

    if not stt_active:
        print("  status:      ⏹ disabled (enable from dashboard → Overview → Slack)")
    else:
        print(f"  provider:    ✅ {cfg.stt.provider}")

    # STT ships enabled-by-default, but neither whisper nor ffmpeg is on a stock
    # Windows box and neither is a KiroCrew dependency there. Reporting them as
    # hard issues made `kirocrew doctor` exit 1 on a healthy first install, and
    # the guide's `kirocrew doctor && kirocrew gateway` then never launched the
    # gateway. On Windows treat them as non-fatal notes; POSIX keeps failing so
    # a real STT setup gap is still surfaced.
    stt_fatal = not platform_compat.IS_WINDOWS

    whisper_bin = _find_whisper(cfg.stt.whisper_path)
    if whisper_bin:
        print(f"  whisper:     ✅ {whisper_bin}")
    elif needs_whisper:
        mark = "❌" if stt_fatal else "⚠️ "
        print(f"  whisper:     {mark} not found")
        print(
            "               Fix: "
            + _os_fix_hint(
                "brew install openai-whisper",
                "pipx install openai-whisper  (or pip install --user openai-whisper)",
                windows="pip install openai-whisper",
            )
        )
        if stt_fatal:
            issues.append("whisper")
    else:
        print("  whisper:     ⏭  not installed (not needed)")

    ensure_ffmpeg_in_path()
    ffmpeg_bin = shutil.which("ffmpeg")
    if ffmpeg_bin:
        print(f"  ffmpeg:      ✅ {ffmpeg_bin}")
    elif needs_ffmpeg:
        mark = "❌" if stt_fatal else "⚠️ "
        print(f"  ffmpeg:      {mark} not found")
        print(
            "               Fix: "
            + _os_fix_hint(
                "brew install ffmpeg",
                "drop a static ffmpeg build into ~/.local/bin "
                "(not in AL2023 repos; KiroCrew auto-detects it)",
                windows="winget install Gyan.FFmpeg",
            )
        )
        if stt_fatal:
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
                print(
                    f"  last dump:   ✅ oldest only ({_age_s / 86400:.0f}d ago, no recent stalls)"
                )
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
        print("✅ Kiro Crew is ready!")

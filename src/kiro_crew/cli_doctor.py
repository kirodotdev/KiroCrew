"""CLI doctor subcommand — verify KiroCrew setup and diagnose issues."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform as _plat
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import urllib.error
import urllib.request
from pathlib import Path

from kiro_crew import __version__ as _mc_version
from kiro_crew import diagnostics, platform_compat, sandbox
from kiro_crew._bootstrap import _source_checkout_root
from kiro_crew.acp import kas_assets, kas_auth
from kiro_crew.acp.client import KIRO_CLI_BIN
from kiro_crew.acp.types import ACP_BACKEND_KAS
from kiro_crew.agent import AGENT_FILENAME
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config import KiroCrewConfig
from kiro_crew.config.loader import config_dir
from kiro_crew.config.paths import (
    LEGACY_CONFIG_DIR_NAME,
    _valid_override_home,
    kiro_agents_dir,
)
from kiro_crew.constants import MIN_NODE_MAJOR
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
from kiro_crew.kiro_cli import mcp_governance_may_apply
from kiro_crew.mcp_cleanup import ALWAYS_ON_BIN_MCP_SERVERS as _ALWAYS_ON_MCPS
from kiro_crew.mcp_cleanup import KIROCREW_BIN_MCP_SERVERS as _MANAGED_MCPS
from kiro_crew.mcp_cleanup import OPT_IN_BIN_MCP_SERVERS as _OPT_IN_MCPS
from kiro_crew.mcp_discovery import McpServerInfo, probe_server
from kiro_crew.platform import (
    PlatformCompositionError,
    current_context,
    safe_context_call,
)
from kiro_crew.platform.governance import CU_MCP_SERVER, may_skip_gate_now
from kiro_crew.sandbox import warm_backend
from kiro_crew.sel import sel
from kiro_crew.service import apparmor
from kiro_crew.service import common as common_service
from kiro_crew.service import controller as service_controller
from kiro_crew.service import linux as service_linux
from kiro_crew.session_pid_sig import signing_health
from kiro_crew.transcribe import _find_parakeet_mlx, _find_whisper, ensure_ffmpeg_in_path

logger = logging.getLogger(__name__)

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
_NO_BLANKET_ALLOW_MCPS = frozenset({CU_MCP_SERVER}) | frozenset(_OPT_IN_MCPS)


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
            # An opt-in set is granted per agent, so its absence from THIS spec is
            # the normal state, not a broken install. Say nothing and probe
            # nothing; the always-on servers below are the ones whose absence
            # means `kirocrew setup` did not finish.
            if name in _OPT_IN_MCPS:
                if ref in tools:
                    # Half a grant: the ref mounts a server the spec never
                    # defines, so kiro-cli has nothing to launch. Report it —
                    # repairing it either way would decide a grant for the user.
                    print(
                        f"  {ref}: ⚠️  referenced in tools but absent from mcpServers "
                        "— add the server entry, or drop the ref"
                    )
                continue
            print(f"  {ref}: ❌ missing from mcpServers (re-run `kirocrew setup`)")
            issues.append(f"{ref} config")
            continue
        if not isinstance(mcps.get(name), dict):
            # A hand-written entry that is not an object. Every read below —
            # command, args, env — would raise on it, and doctor exists to
            # diagnose a broken config rather than die on one. An opt-in name is
            # the one a human types, so say what is wrong and move on; a
            # malformed ALWAYS-ON entry is a broken install and counts as an issue.
            print(f"  {ref}: ❌ malformed entry in mcpServers (expected an object)")
            if name not in _OPT_IN_MCPS:
                issues.append(f"{ref} config")
            continue
        if ref not in tools and name not in _OPT_IN_MCPS:
            # Mounting an opt-in server IS granting it: the `@` ref is what makes
            # kiro-cli load it. Doctor repairs a broken always-on mount, but it
            # must never hand an agent a set the user did not assign.
            tools.append(ref)
            config_changed = True
        elif ref not in tools:
            # The other half: an entry with no ref. kiro-cli loads a server only
            # when something references it, so the tools are unreachable and
            # every other check here would still read clean — the same silent
            # unreachability this opt-in shape exists to avoid. Warn without
            # adding an issue: a deliberately staged entry is a legitimate state,
            # and doctor must not mount it to make itself green.
            print(
                f"  {ref}: ⚠️  defined in mcpServers but not referenced in tools "
                "— unreachable until the ref is added"
            )
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


# Non-secret rows kiro-cli writes when the signed-in identity came from IAM
# Identity Center. Presence is the signal; the values (a start URL and a region)
# are never read into a message, and no token key is touched.
def _doctor_mcp_governance(agent_path: Path, issues: list[str]) -> None:
    """Render the `MCP Governance` section of `kirocrew doctor`.

    Speaks up in two situations: governance can reach this identity (Identity
    Center or an API key), where an administrator's registry may be in force, and
    the registry declaration or its markers are present on an identity governance
    CANNOT reach, which is the inverse failure and just as silent. Stays quiet on
    an ordinary personal install, where a governance warning would be pure noise.

    This exists because the section above cannot detect either failure.
    Governance is enforced inside kiro-cli when it assembles a session: it drops
    every ``mcpServers`` entry whose registry marker does not match the account's
    access mode. Kiro Crew's own handshake probe spawns each server directly and
    therefore still reports it healthy, so an affected host reads green here
    while `spawn_run`, `cron_add` and `learn_add` are absent from every session.
    """
    try:
        declared = KiroCrewConfig.load().agent.mcp_registry_mode
    except Exception:
        logger.debug("config load failed in governance check", exc_info=True)
        declared = False

    try:
        spec = json.loads(agent_path.read_text(encoding="utf-8"))
        servers = spec.get("mcpServers") or {}
    except Exception:
        servers = {}
    if not isinstance(servers, dict):
        # `or {}` only replaces a FALSY value, so a string or list here survives
        # and the membership walk below would raise, aborting the whole doctor
        # run — on exactly the malformed spec someone is running doctor to find.
        servers = {}

    # What a governed spec OUGHT to declare: every always-on server, plus the
    # opt-in sets this spec actually grants. Counting an unassigned opt-in server
    # would report every governed install as half-marked; dropping the always-on
    # ones from the denominator would make a spec that declares NOTHING — a
    # malformed or emptied ``mcpServers`` — read as fully marked, which is the
    # exact failure this section exists to catch.
    expected = list(_ALWAYS_ON_MCPS) + [
        name for name in _OPT_IN_MCPS if isinstance(servers.get(name), dict)
    ]
    marked = sorted(
        name
        for name in expected
        if isinstance(servers.get(name), dict) and servers[name].get("type") == "registry"
    )
    names = ", ".join(sorted(expected))
    governed_capable = mcp_governance_may_apply()

    # Nothing to say: an identity governance cannot reach, with no registry
    # declaration and no leftover markers, is the ordinary case.
    if not governed_capable and not declared and not marked:
        return

    print("\nMCP Governance (enterprise):")

    if not governed_capable:
        # The inverse filter. Outside registry mode a MARKED entry is the one the
        # client drops, so this state breaks the same servers, equally silently —
        # reachable by copying the guide onto a personal account, or by leaving an
        # enterprise account with the declaration still set. Safe to assert only
        # because neither governance-capable signal is present: no Identity Center
        # rows AND no API key, which leaves Builder ID or social sign-in.
        print("  identity: not Identity Center or API key — an admin MCP registry cannot apply")
        if declared:
            print(
                "  ❌ registry mode is declared, so kiro-cli treats these servers as "
                "registry-provided and drops them on an ungoverned account"
            )
        else:
            print("  ❌ registry markers are present on the spec without the declaration")
        print(f"      affected: {', '.join(marked) if marked else names}")
        print("      fix:  kirocrew config set agent.mcp_registry_mode false")
        issues.append("MCP registry mode on non-IDC account")
        return

    print("  identity: Identity Center or API key — an admin MCP registry can apply")
    if declared:
        print(f"  registry mode: on — {len(marked)}/{len(expected)} managed servers marked")
        if len(marked) < len(expected):
            print("  ❌ markers missing — re-run `kirocrew setup --agent-only`")
            issues.append("MCP registry markers")
            return
        # Deliberately not a success line. Whether the administrator actually
        # allow-listed these names is not knowable locally, so claiming green
        # here would repeat the overstatement this section exists to correct.
        print("  cannot verify the registry itself — that lives with your administrator")
        print(f"      these names must be allow-listed, exactly: {names}")
        print(
            "      if tools are still missing in sessions, the account may no longer be "
            "registry-governed — try `kirocrew config set agent.mcp_registry_mode false`"
        )
        return

    print("  registry mode: off")
    print(
        "  ⚠️  If MCP tools are missing in sessions while probing OK above, your "
        "administrator has configured an MCP Registry URL. In that mode kiro-cli "
        "connects only to servers marked 'type': \"registry\"."
    )
    print("      Declare it:  kirocrew config set agent.mcp_registry_mode true")
    print(f"      Then have your admin allow-list, by these exact names: {names}")


# Top-level entries that hold a Python virtual environment rather than user
# data. An older wheel install could nest its managed venv INSIDE the legacy
# ``~/.kirocrew`` home, so a leftover legacy dir may still contain the running
# interpreter — deleting it would break the live install.
_LEGACY_VENV_DIR_NAMES = ("venv", ".venv", "venvs")


def _legacy_venv_entries(home: Path) -> list[str]:
    """Names of virtual-environment entries at the top of *home* (best-effort)."""
    try:
        return sorted(name for name in _LEGACY_VENV_DIR_NAMES if (home / name).is_dir())
    except OSError:  # pragma: no cover - defensive
        return []


def _doctor_data_home() -> None:
    """Report the data home and any leftover top-level ``~/.kirocrew`` directory.

    The data root is ``~/.kiro/crew`` (or a valid ``KIROCREW_HOME`` override). A
    leftover top-level ``~/.kirocrew`` is not the data home unless an override
    points at it; a leftover that still holds a virtual environment is flagged as
    UNSAFE to delete (it may be the live interpreter), otherwise it is reported as
    an unused directory. Purely informational — doctor never deletes it itself.
    """
    print("\nData Home")
    home = config_dir()
    print(f"  location:    ✅ {home}")

    legacy = Path.home() / LEGACY_CONFIG_DIR_NAME
    if not legacy.is_dir():
        return
    override_home = _valid_override_home()
    if override_home is not None:
        try:
            points_at_legacy = override_home == legacy.resolve()
        except OSError:  # pragma: no cover - defensive
            points_at_legacy = override_home == legacy
        if points_at_legacy:
            # The override points AT the legacy dir, so it IS the active data
            # home — don't mislabel the home the process is actually using.
            print(
                f"  legacy:      ✅ {legacy} is the ACTIVE data home "
                f"(KIROCREW_HOME override points to it)"
            )
            return
    venvs = _legacy_venv_entries(legacy)
    if venvs:
        # A wheel install could nest its managed venv here; the dir survives to
        # hold it. Never advise deleting it — removing it takes the running
        # interpreter with it (`which kirocrew` may resolve through it).
        print(
            f"  legacy:      ✅ {legacy} retained to hold a Kiro Crew "
            f"virtual environment ({', '.join(venvs)})"
        )
        print(
            "               Do NOT delete it while it is your active install "
            "— removing it would delete the running interpreter."
        )
        return
    print(
        f"  legacy:      ⏹ {legacy} present but not the data home — safe to "
        f"delete once you have confirmed it holds nothing you need"
    )


def _doctor_path_launcher() -> None:
    """Report which install the ``kirocrew`` command on PATH actually belongs to.

    A gateway never takes the name from another install's working launcher (see
    ``agent.ensure_kirocrew_on_path``), which is the right call — but it leaves a
    gap the user cannot see from anywhere else. The documented Linux pairing puts
    a cli.sh wheel and a deb/rpm desktop install on ONE machine, so typing
    ``kirocrew`` can run a different install, at a different version or channel,
    than the app that is running. The desktop app has no terminal, so the decline
    is logged where nobody reads it; this is the surface someone checks when a
    version looks wrong.

    Read-only: it resolves and compares paths, and never writes or relinks.
    """
    from kiro_crew.agent import _resolve_kirocrew_bin

    on_path = shutil.which("kirocrew")
    if not on_path:
        # Not an error on its own: the desktop app runs its bundled backend
        # directly, and a user who never wanted a terminal command is fine.
        print("  kirocrew CLI: ⏹ not on PATH (run `kirocrew setup` to link it)")
        return
    running = _resolve_kirocrew_bin()
    if not os.path.isabs(running) or os.path.realpath(on_path) == os.path.realpath(running):
        print(f"  kirocrew CLI: ✅ {on_path}")
        return
    print("  ⚠ kirocrew CLI on PATH belongs to a different install than this one.")
    # Paths are printed UNWRAPPED, one per line: a wrapped path cannot be copied
    # or pasted into a command, which is the first thing someone does with it.
    print(f"{_INDENT}on PATH:      {os.path.realpath(on_path)}")
    print(f"{_INDENT}this install: {os.path.realpath(running)}")
    _print_wrapped(
        "Both can coexist — the wheel keeps its own updates — but `kirocrew` in a "
        "terminal runs the one on PATH, which may be a different version or "
        "channel. Run `kirocrew setup` from the install you want to own the name."
    )


def _doctor_trust_root() -> None:
    """Report whether session identities can be signed, and from which file.

    A gateway whose SEL trust root stops resolving keeps signing its audit
    chain from bytes cached at init, so nothing looks wrong — while every
    ``session_pid`` mapping goes out unsigned and the MCP tools that need a
    verified session are refused. Publication logs that once per process, but
    only once a session is actually claimed; asking here needs no claim.

    Read-only on purpose: it never constructs ``SecurityEventLog``, so a
    missing key is reported rather than created as a side effect of the
    question.
    """
    ok, key_path = signing_health()
    if ok:
        print(f"  trust root:  ✅ {key_path}")
        return
    if not key_path.parent.is_dir():
        # The trust dir and the key are created together, on the first
        # SecurityEventLog init. Neither present means no instance has ever run
        # against this home — a fresh install, not a broken one.
        print(f"  trust root:  ⏹ {key_path} not created yet (the gateway writes it on first start)")
        return
    print(f"  ⚠ trust root: {key_path} is unreadable or shorter than 32 bytes.")
    print(
        "               Session identities go out unsigned, so sub-agent "
        "dispatch and memory"
    )
    print(
        "               writes are refused in sandboxed sessions. Restore the "
        "key file, or"
    )
    print("               restart the gateway if another process relocated it.")


_INDENT = "               "


def _print_wrapped(text: str) -> None:
    """Print ``text`` wrapped to the doctor's detail indent."""
    for line in textwrap.wrap(text, width=80):
        print(f"{_INDENT}{line}")


def _process_apparmor_confinement() -> str:
    """AppArmor confinement label of THIS process, ``""`` when unreadable.

    Reads the kernel's own answer, e.g. ``unconfined`` or
    ``kirocrew-userns (enforce)``. The per-LSM path is tried first; the bare
    ``attr/current`` covers older kernels (where it may also carry an SELinux
    context — which is fine, since callers only compare against a profile name).
    """
    for attr in ("/proc/self/attr/apparmor/current", "/proc/self/attr/current"):
        try:
            raw = Path(attr).read_text(encoding="utf-8")
        except OSError:
            continue
        return raw.replace("\x00", "").strip()
    return ""


def _service_unit_applies_profile(unit_path: Path, profile_name: str) -> bool:
    """True when the installed systemd unit transitions the service into the profile.

    The unit is rendered with ``AppArmorProfile=-<name>`` (the leading dash keeps
    the unit startable while the profile is temporarily unloaded); the dashless
    form is accepted too so a hand-edited unit still counts.
    """
    try:
        text = unit_path.read_text(encoding="utf-8")
    except OSError:
        return False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("AppArmorProfile="):
            value = stripped.split("=", 1)[1].strip().lstrip("-")
            if value == profile_name:
                return True
    return False


def _doctor_sandbox_apparmor(reason: str, issues: list[str]) -> None:
    """Verdict for the Ubuntu AppArmor userns-restriction denial (EPERM on NEWNS).

    Three honest verdicts, decided from real signals rather than the happy path:

    * profile absent → broken, with the install command;
    * profile installed but nothing applies it, or the probe failed even though
      THIS process is confined by the profile → broken, with the repair command;
    * profile installed, the service unit applies it, and this process is
      unconfined → the probe's failure says nothing about the service, so the
      verdict is "cannot be verified from this shell" plus how to verify — NOT
      a claim that the sandbox works, and NOT counted as an issue.
    """
    if not apparmor.PROFILE_PATH.is_file():
        print(f"  backend:     ❌ none — {reason}")
        _print_wrapped(
            f"This host restricts unprivileged user namespaces and the "
            f"{apparmor.PROFILE_NAME} AppArmor profile is not installed, so no context "
            f"on this host can build the sandbox. Run `kirocrew service install` to "
            f"install the profile and confine the gateway service with it."
        )
        issues.append("sandbox: AppArmor profile not installed")
        return

    confinement = _process_apparmor_confinement()
    if confinement and confinement.split(" ")[0] == apparmor.PROFILE_NAME:
        # The one context that SHOULD be able to build the sandbox refused to:
        # this is a genuine fault, not a vantage-point artifact.
        print(f"  backend:     ❌ broken — {reason}")
        _print_wrapped(
            f"This process already runs confined by {apparmor.PROFILE_NAME}, which "
            f"should grant user namespaces, yet the probe still failed. Re-run "
            f"`kirocrew service install` to re-render and reload the profile."
        )
        issues.append("sandbox: probe failed under the AppArmor profile")
        return

    if not _service_unit_applies_profile(service_linux.UNIT_PATH, apparmor.PROFILE_NAME):
        print(f"  backend:     ❌ none — {reason}")
        _print_wrapped(
            f"The {apparmor.PROFILE_NAME} AppArmor profile is installed, but no systemd "
            f"unit applies it, so nothing on this host runs confined by it. Run "
            f"`kirocrew service install` to (re)install the gateway service with the "
            f"profile applied."
        )
        issues.append("sandbox: AppArmor profile installed but not applied")
        return

    # Unverifiable from here — deliberately NOT an issue, and deliberately NOT a
    # success claim either.
    print("  backend:     ⏭  cannot be verified from this shell")
    _print_wrapped(
        f"The {apparmor.PROFILE_NAME} AppArmor profile is installed and the gateway "
        f"service unit is configured to run under it, but this shell is unconfined "
        f"and aa_change_onexec() into a named profile is not permitted for an "
        f"unconfined user — so this probe cannot succeed here no matter how healthy "
        f"the service's sandbox is. To verify the sandbox in the confined context "
        f"the service uses, run:"
    )
    # The interpreter path is quoted for the shell: the recipe is meant to be
    # pasted, so an install path containing spaces or shell metacharacters must
    # arrive as one argument, not execute.
    quoted_python = shlex.quote(sys.executable)
    print(f"{_INDENT}  sudo systemd-run --pipe --unit=kirocrew-sandbox-test \\")
    print(f"{_INDENT}    --property=AppArmorProfile=-{apparmor.PROFILE_NAME} \\")
    print(f"{_INDENT}    --uid=$(id -u) --gid=$(id -g) \\")
    print(f'{_INDENT}    {quoted_python} -c "import kiro_crew.sandbox as sb; \\')
    print(f'{_INDENT}      sb.reset_backend(); print(sb.detect_backend())"')
    _print_wrapped("A healthy sandbox prints: namespace")


def _doctor_sandbox(issues: list[str]) -> None:
    """Render the ``Sandbox`` section — an honest verdict about the agent sandbox.

    The hard rule: report only what THIS process can observe.
    :func:`sandbox.detect_backend` answers for the probing process, not for the
    gateway service — on a host that restricts unprivileged user namespaces the
    profile is applied by systemd to the SERVICE, so from an interactive shell
    the probe fails with EPERM no matter how healthy the service's sandbox is.
    Reporting that failure as the sandbox being broken is a false negative; the
    fix must not swing to the false positive of claiming the sandbox works when
    all that is known is that it cannot be checked from here.
    """
    print("\nSandbox")
    try:
        # ONE probe decision: ``unavailable_kind()`` probes internally and
        # returns "" for a working backend. Probing twice (a detect_backend
        # read followed by a classifying call) would let a transient failure
        # heal between the two reads and report a now-working backend as
        # broken.
        kind = sandbox.unavailable_kind()
    except Exception as exc:  # noqa: BLE001 — doctor must survive a broken probe
        print(f"  backend:     ⚠️  could not probe ({exc})")
        return
    if not kind:
        # The probe just succeeded, so this read serves the cached positive
        # result rather than probing again.
        print(f"  backend:     ✅ {sandbox.detect_backend()}")
        return

    reason = sandbox.unavailable_reason() or "no probe detail recorded"
    if kind == "transient":
        print("  backend:     ⚠️  probe failed transiently — not cached; the next spawn re-probes")
        print(f"{_INDENT}({reason})")
        return
    if kind == "foreign_sandbox":
        print("  backend:     ⚠️  an outer sandbox already confines this process")
        _print_wrapped(
            "Kiro Crew cannot nest its own sandbox inside it. Launch the gateway "
            "outside that sandbox to hand isolation back to Kiro Crew's own profile."
        )
        return

    remedy = sandbox.unavailable_remedy()
    if remedy == sandbox.REMEDY_APPARMOR_USERNS:
        _doctor_sandbox_apparmor(reason, issues)
        return
    if sys.platform.startswith("linux"):
        # A permanent, named kernel refusal (user.max_user_namespaces=0, a kernel
        # without CONFIG_USER_NS, ...) — genuinely broken, with the mechanism's
        # own guidance when the probe identified one.
        print(f"  backend:     ❌ none — {reason}")
        guidance = sandbox.remedy_guidance(remedy)
        if guidance:
            _print_wrapped(guidance)
        issues.append("sandbox backend")
        return
    # Platforms with no OS-level backend to offer (Windows; macOS builds without
    # sandbox-exec) — a fact about the platform, not a fault of this install.
    print("  backend:     ⏭  no OS-level sandbox backend on this platform")


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


# Git for Windows never lives in the system directories the trusted resolver
# pins Windows lookups to — it installs under Program Files. Fixed literal
# roots, not ``%ProgramFiles%``: doctor runs with operator privileges, and
# reading the environment would let a poisoned variable redirect the lookup to
# an agent-writable directory — the exact hole the pin exists to close.
_WINDOWS_GIT_DIRS = (
    r"C:\Program Files\Git\cmd",
    r"C:\Program Files (x86)\Git\cmd",
)


def _windows_git_bin() -> str | None:
    """``git.exe`` from the fixed Git for Windows install roots, else ``None``.

    Without this, every supported Windows source install reported "could not
    check" — :func:`platform_compat.trusted_system_bin` only probes the system
    directories, where git never is. A non-default-drive install still misses
    and degrades to "could not check", which is honest: this fallback widens
    the pin only to paths an unprivileged attacker cannot write.
    """
    for directory in _WINDOWS_GIT_DIRS:
        candidate = os.path.join(directory, "git.exe")
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _git_line(repo: Path, *args: str) -> str | None:
    """First stdout line of ``git -C repo *args``, ``None`` on any failure.

    A module-level seam (not inlined) so tests can drive the checkout probe
    without a real repository. Failures are expected states here — a tarball
    install has no ``.git``, a fresh clone may lack ``origin/HEAD`` — so every
    error collapses to ``None`` and the caller renders "could not check".

    ``git`` is resolved through :func:`platform_compat.trusted_system_bin`
    rather than a bare ``PATH`` lookup: doctor runs with operator privileges,
    and an agent-writable directory leading ``PATH`` could plant a ``git``
    shim. On Windows a resolver miss falls back to the fixed Git for Windows
    install roots (:func:`_windows_git_bin`); any remaining miss collapses to
    ``None`` like every other failure here — no spawn at all.
    """
    git = platform_compat.trusted_system_bin("git")
    if git is None and platform_compat.IS_WINDOWS:
        git = _windows_git_bin()
    if git is None:
        return None
    try:
        res = subprocess.run(
            [git, "-C", str(repo), *args],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    return res.stdout.strip().splitlines()[0].strip() if res.stdout.strip() else None


def _doctor_source_checkout(repo: Path) -> None:
    """Report whether an editable install's source tree is current.

    An editable install (``pip install -e``) runs whatever the source checkout
    happens to be at process start. A checkout parked on a stale feature branch
    is invisible at runtime: the gateway starts fine, serves traffic, and every
    fix merged upstream since the branch diverged — security gates included —
    is silently absent. Nothing else surfaces this (a real incident ran a
    9-day-stale branch through a restart while doctor reported healthy), so
    doctor names the branch and how far behind the default branch it is.

    Advisory only (never appended to ``issues``, matching the linger and
    model-url probes): running a feature branch is a legitimate developer
    state, so doctor's job is to make it visible, not to block on it.

    Offline by design: no ``git fetch`` — doctor must not touch the network or
    mutate the repo. "behind" therefore means behind the LAST-FETCHED default
    branch; a checkout that never fetches reports current. That bound is
    acceptable because the failure mode being caught is a checkout parked on
    an old branch while fetches happen around it (e.g. by update checks), not
    a host that never talks to the remote.
    """
    print("\nSource Checkout")
    if not (repo / ".git").exists():
        print(f"  source:      ⏹ not a git checkout ({repo})")
        return

    branch = _git_line(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if branch is None:
        print("  branch:      ⚠️  could not check (git failed)")
        return

    # Default branch as recorded at clone time (refs/remotes/origin/HEAD).
    # `git remote show` would be authoritative but hits the network.
    default_ref = _git_line(repo, "rev-parse", "--abbrev-ref", "origin/HEAD")
    default_branch = default_ref.split("/", 1)[1] if default_ref and "/" in default_ref else None

    if default_branch is None:
        # Fresh clones always have origin/HEAD; only manual remote surgery
        # loses it. Report the branch we ARE on and stop — guessing "main"
        # could mislabel a repo whose default genuinely differs.
        print(f"  branch:      ⚠️  {branch} (could not determine default branch)")
        return

    if branch == default_branch:
        behind = _git_line(repo, "rev-list", "--count", f"HEAD..origin/{default_branch}")
        if behind is None or not behind.isdigit():
            # A failed count must not masquerade as a verified-fresh checkout:
            # "up to date" is a claim this probe could not actually establish.
            print(f"  branch:      ⚠️  {default_branch} (could not count commits behind origin)")
            return
        if int(behind) > 0:
            print(f"  branch:      ⚠️  {default_branch}, {behind} commit(s) behind origin (as of last fetch)")
            print("               The running gateway predates those commits until an")
            print("               update + restart.")
        else:
            print(f"  branch:      ✅ {default_branch} (up to date as of last fetch)")
        return

    behind = _git_line(repo, "rev-list", "--count", f"HEAD..origin/{default_branch}")
    detail = (
        f", {behind} commit(s) behind origin/{default_branch}"
        if behind and behind.isdigit() and int(behind) > 0
        else ""
    )
    print(f"  branch:      ⚠️  on '{branch}' — not the default branch{detail}")
    print("               The gateway runs this checkout as-is: fixes merged to")
    print(f"               {default_branch} since divergence are NOT active, and update")
    print(f"               pulls this branch, not {default_branch}.")
    # Remediation stays prose, never a rendered command: branch and path come
    # from the repository (agent-writable), and a ref named e.g.
    # ``$(touch${IFS}/tmp/pwn)`` pasted from a suggested command line would
    # execute in the operator's shell.
    print("               Fix: check out the default branch in the source checkout,")
    print("               then update + restart.")


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


# Where SwapTotal is read from. A module attribute (not inlined) so tests can
# point it at a fabricated meminfo file.
_PROC_MEMINFO = Path("/proc/meminfo")

# Userspace OOM killers doctor knows how to detect, in probe order:
# systemd-oomd ships with systemd (the common case), earlyoom is the usual
# add-on daemon.
_OOM_KILLER_UNITS = ("systemd-oomd", "earlyoom")


def _swap_total_kib() -> int | None:
    """``SwapTotal`` from ``/proc/meminfo`` in KiB, ``None`` when unreadable.

    Read from procfs directly rather than shelling out to ``free``/``swapon``:
    the file is world-readable and parsing it cannot hang or prompt.
    """
    try:
        text = _PROC_MEMINFO.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        return None
    for line in text.splitlines():
        if line.startswith("SwapTotal:"):
            parts = line.split()
            try:
                return int(parts[1])
            except (IndexError, ValueError):
                return None
    return None


def _detect_userspace_oom_killer() -> str | bool | None:
    """Which userspace OOM killer is active, if any.

    Returns the unit name (``"systemd-oomd"`` / ``"earlyoom"``) when one is
    active, ``False`` when every probe completed and none is active, and
    ``None`` when it cannot be determined (no ``systemctl``, probe timeout or
    failure) so the caller reports "unknown" rather than guessing. ``True`` is
    never returned — the truthy arm carries the unit name.

    Non-privileged and bounded: ``systemctl is-active`` needs no root and each
    probe is capped at 5s, so this can never hang the doctor.
    """
    systemctl = platform_compat.trusted_system_bin("systemctl")
    if systemctl is None:
        return None
    determined = True
    for unit in _OOM_KILLER_UNITS:
        try:
            res = subprocess.run(
                [systemctl, "is-active", unit],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            determined = False
            continue
        if res.returncode == 0 and res.stdout.strip() == "active":
            return unit
    return False if determined else None


def _doctor_memory_pressure(issues: list[str]) -> None:
    """Report whether the host can degrade gracefully under memory pressure.

    A Linux host with zero swap and no userspace OOM killer has no pressure
    release valve: sustained memory pressure evicts file-backed pages (running
    code included) faster than they re-fault in, and the host livelocks —
    unresponsive for minutes, sometimes until a power cycle — before the kernel
    OOM killer's conservative heuristics fire. Either protection alone (swap to
    absorb the spike, or earlyoom/systemd-oomd to kill a hog early) prevents
    the freeze, so this warns only when BOTH are absent. When detection is
    inconclusive it reports "unknown" instead of warning.

    Advisory only (never appended to ``issues``): swap sizing and OOM-killer
    policy are host configuration the user owns — doctor reports the exposure,
    it does not fail the install over it. Linux-only: the freeze mode and both
    detection sources are Linux-specific.
    """
    del issues  # advisory-only diagnostic; keeps the call-site signature uniform
    print("\nMemory Pressure")
    if not sys.platform.startswith("linux"):
        print(
            f"  freeze risk: ⏹ not applicable ({sys.platform} — the swap/OOM-killer "
            "check reads Linux procfs)"
        )
        return

    swap_kib = _swap_total_kib()
    if swap_kib is None:
        print("  swap:        ⚠️  could not read SwapTotal from /proc/meminfo — check skipped")
        return
    if swap_kib > 0:
        print(f"  swap:        ✅ {swap_kib / 1048576:.1f} GiB configured")
    else:
        print("  swap:        ⏹ none (SwapTotal = 0)")

    killer = _detect_userspace_oom_killer()
    if isinstance(killer, str):
        print(f"  oom killer:  ✅ {killer} active")
    elif killer is False:
        print("  oom killer:  ⏹ none active (checked: " + ", ".join(_OOM_KILLER_UNITS) + ")")
    else:
        print("  oom killer:  ⏹ could not determine (no systemctl, or the probe failed)")

    if swap_kib > 0 or isinstance(killer, str):
        return
    if killer is None:
        # Uncertain detection must not warn — a container or non-systemd host
        # may run a killer doctor cannot see.
        print("  freeze risk: ⏹ unknown — no swap, and OOM-killer detection was inconclusive")
        return
    print("  freeze risk: ⚠️  host can freeze under sustained memory pressure")
    print("               With no swap and no userspace OOM killer, memory pressure")
    print("               thrashes file-backed pages and the host can livelock before")
    print("               the kernel OOM killer intervenes.")
    print("               Fix: add swap, enable systemd-oomd, or install earlyoom.")


# ── kiro-cli installer residue ────────────────────────────────────────────────
# kiro-cli runs its auto-update check on STARTUP — the ``app.disableAutoupdates``
# setting is documented as "Disable automatic updates on startup" — and Crew
# spawns a FRESH kiro-cli per session (``AcpRuntime`` is constructed per session
# in ``providers/acp.py`` and ``session.py``, and again per Code Review Sage
# worker). So that check runs once per process START, not once per host per
# release.
#
# On Windows the running executable cannot be replaced, so the downloaded
# installer can never be applied while a Crew ACP child holds the binary — and
# the "update pending" state is not cleared after an upgrade either
# (kirodotdev/Kiro#9825). Nothing in that loop is self-limiting: one installer is
# left behind per process start. A reporting user cleared ~80 GB of them.
#
# Crew cannot fix the updater, and must NOT disable updates on the user's behalf:
# ``app.disableAutoupdates`` is a per-user setting shared with their own
# interactive CLI, so setting it silently would suppress their security updates.
# What Crew can do is stop the residue being invisible, since it is Crew's
# per-session spawning that turns a stale flag into tens of gigabytes.
# Upstream fix requested in kirodotdev/Kiro#10970.
_CLI_INSTALLER_GLOB = "kiro-installer*"

# One file can be a download still in flight; two or more is residue, because a
# failed apply leaves the file behind and the next process start fetches another.
_CLI_INSTALLER_RESIDUE_MIN = 2

# The temp dir is shared with every other process on the host and can hold a very
# large number of entries, so a diagnostic must not walk it unbounded.
# Non-recursive by design: the installer lands at the top level.
_CLI_INSTALLER_SCAN_CAP = 512


def _scan_cli_installer_residue(temp_dir: Path) -> tuple[int, int]:
    """Return ``(count, total_bytes)`` for leftover kiro-cli installers in *temp_dir*.

    Bounded and non-raising: the scan stops at :data:`_CLI_INSTALLER_SCAN_CAP`
    matches, and an entry that vanishes mid-scan — another process cleaning up,
    or the updater itself — is skipped rather than aborting the whole doctor run.
    An unreadable temp dir reports "nothing found" for the same reason.
    """
    count = 0
    total = 0
    try:
        for entry in temp_dir.glob(_CLI_INSTALLER_GLOB):
            try:
                if not entry.is_file():
                    continue
                total += entry.stat().st_size
            except OSError:
                # Raced with a delete, or unreadable: one bad entry must not
                # abort a diagnostic.
                continue
            count += 1
            if count >= _CLI_INSTALLER_SCAN_CAP:
                break
    except OSError:
        return (0, 0)
    return (count, total)


def _doctor_cli_installer_residue(issues: list[str]) -> None:
    """Report leftover kiro-cli auto-update installers piling up in the temp dir.

    Silent on a healthy host — the common case, and every case on a platform that
    can replace a running binary — so a normal doctor run gains no noise. This
    speaks only when residue is actually present, which is why it is not gated on
    ``platform.system() == "Windows"``: the gate is the evidence on disk, so the
    check still fires if this failure mode ever appears on another platform.
    """
    # gettempdir() itself probes candidate directories and raises when none is
    # usable, so it must be inside the guard too: a host with a full or
    # unwritable temp volume is exactly the host most in need of the rest of the
    # doctor run, and must not get a traceback instead of it.
    try:
        temp_dir = Path(tempfile.gettempdir())
    except OSError:
        return
    count, total = _scan_cli_installer_residue(temp_dir)
    if count < _CLI_INSTALLER_RESIDUE_MIN:
        return

    # Capped scans undercount, so say so rather than printing a precise-looking
    # number that is actually a floor. This applies to the SIZE as well: the scan
    # stopped summing at the cap, so the total is a floor exactly as the count is,
    # and rendering it as exact next to a "512+" count would contradict itself.
    capped = count >= _CLI_INSTALLER_SCAN_CAP
    count_label = f"{count}+" if capped else str(count)
    if total >= 1073741824:
        size_label = f"{total / 1073741824:.2f} GiB"
    else:
        size_label = f"{total / 1048576:.1f} MiB"
    if capped:
        size_label = f"≥ {size_label}"

    print("\nkiro-cli installer residue")
    print(f"  files:       ⚠️  {count_label} in {temp_dir}")
    print(f"  reclaimable: {size_label}")
    print("               Auto-update downloads that could not be applied while")
    print("               kiro-cli was running, and are not cleaned up. Crew starts")
    print("               a kiro-cli per session, so one accumulates per start.")
    print(f"               Fix: delete {_CLI_INSTALLER_GLOB} from {temp_dir}, then stop")
    print("               the gateway and run `kiro-cli update` deliberately.")
    print("               To stop the downloads: `kiro-cli settings")
    print("               app.disableAutoupdates true` — note this is per-user, so it")
    print("               also pauses updates for your own interactive kiro-cli.")
    issues.append("kiro-cli installer residue in temp")


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


def _doctor_headless_auth(issues: list[str]) -> None:
    """Report an API-key credential the INSTALLED service cannot see.

    This is the one place the contradiction is visible in a single output: the
    ``kiro login`` line above runs ``whoami`` with the inherited environment and
    reports signed in, while the dashboard's readiness gate reads the gateway's
    own environment and reports signed out. Install-time is too early to be the
    only report — the symptom surfaces when the service is ALREADY installed (a
    key added to a shell profile afterwards, a host re-provisioned from a
    snapshot, an operator who reaches the docs only after hitting the wall), and
    none of those orderings run ``service install`` again.

    Gated on a service definition existing, which is what keeps the report
    plausible. Without one the gateway runs in the foreground and inherits this
    very shell, so the credential DOES reach it and warning here would be a
    false positive on a working host.

    Advisory only (never appended to ``issues``, like the pod-session-bus and
    memory-pressure probes): ``issues`` is doctor's exit-code channel, so an
    entry here makes the verdict ❌ and exits non-zero — a claim this shell
    cannot establish. ``service_environment()`` bakes ``HOME``, so a service on
    a host that ran ``kiro-cli login`` before the key was exported resolves that
    credential store and is healthy while the check still fires; and a unit path
    proves a definition exists on disk, not that the unit is the gateway
    currently serving, so a stopped unit beside a foreground ``kirocrew gateway``
    also reads as broken. Reporting the exposure is right; failing doctor on a
    host where sign-in works is the same contradiction-with-reality this
    diagnostic exists to surface, one layer up.

    Best-effort like the probes around it: a failure to read the environment or
    the unit path must not fail ``doctor``, whose job is to report.
    """
    del issues  # advisory-only diagnostic; keeps the call-site signature uniform
    try:
        if service_controller.installed_unit_path() is None:
            return
        warning = common_service.headless_auth_warning()
    except Exception:
        return
    if not warning:
        return
    print("  kiro key:    ⚠️  set here, but the installed service cannot see it")
    for line in warning.splitlines():
        print(f"{_INDENT}{line.strip()}" if line.strip() else "")


def _kas_version_label(script: "Path") -> str:
    """Derive the extracted-bundle version label from the KAS script path.

    Bundles live at ``{data_dir}/kas/{version}/.../acp-server.js`` (see
    :mod:`kiro_crew.acp.kas_assets`), so the version is the path component whose
    parent directory is named ``kas``. Returns ``"unknown"`` for an unexpected
    layout rather than raising — this is a diagnostic, not a gate.
    """
    for parent in script.parents:
        if parent.parent is not None and parent.parent.name == "kas":
            return parent.name
    return "unknown"


def _doctor_kas(issues: list[str]) -> None:
    """Report KAS backend readiness, but only when KAS is the selected backend.

    KAS is opt-in (``agent.acp_backend = "kas"``); when it is not selected this
    is silent so a kiro-cli / Claude Code install sees no KAS noise. When it IS
    selected, KAS runs from kiro-cli's extracted bundle and obtains its token
    from kiro-cli, so this surfaces the two things that make a selected KAS
    backend fail at session-create time: missing assets and an unobtainable
    token. The token probe prints only the expiry, never the token bytes.
    """
    # Positive backend test (not ``!= ACP_BACKEND_KAS``): an inequality would
    # silently capture every harness added later — see the harness-parity gate.
    if KiroCrewConfig.load().agent.acp_backend == ACP_BACKEND_KAS:
        _report_kas_backend(issues)


def _report_kas_backend(issues: list[str]) -> None:
    """Print the KAS diagnostic block (assets + bundle version + token probe).

    Split from :func:`_doctor_kas` so the backend-selection check there stays a
    positive ``== ACP_BACKEND_KAS`` rather than an early-return on inequality.
    """
    print("\nKAS backend")
    node = kas_assets.find_kas_node()
    script = kas_assets.find_kas_server_script()
    print(f"  node:        {'✅ ' + str(node) if node else '❌ not found'}")
    if script:
        print(f"  bundle:      ✅ {_kas_version_label(script)}")
    else:
        print("  bundle:      ❌ KAS server script not found")
    if not (node and script):
        print("               Fix: install kiro-cli and run it once so it unpacks its")
        print("               KAS bundle (or set KIROCREW_KAS_NODE / KIROCREW_KAS_SCRIPT).")
        issues.append("KAS backend selected but assets missing")

    # Token status — a bounded live probe through kiro-cli. Advisory: an
    # unobtainable token is usually a transient login state, not a broken
    # install, so it warns rather than failing the doctor run.
    try:
        resp = asyncio.run(kas_auth.resolve_kas_access_token(timeout=8.0))
    except kas_auth.KasAuthCallbackError as exc:
        print(f"  token:       ⚠️  not obtainable: {exc}")
        print("               Fix: sign in with `kiro-cli login`.")
    except Exception as exc:  # noqa: BLE001 - diagnostic must never abort the run
        print(f"  token:       ⚠️  probe error: {exc}")
    else:
        expires = resp.get("expiresAt")
        print(f"  token:       ✅ obtained via kiro-cli (expires {expires})")


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
    # the local path plus a GitHub issue URL, then exit. Shares the exact
    # collector the dashboard "Report a Problem" button uses, but prints the
    # short link variant: the dashboard's pre-filled URL carries a ~600-char
    # query that the exfil query-length heuristic redacts on any surface that
    # scans printed output.
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
        print("\n  Open a GitHub issue (then drag the zip in):")
        print(f"  {diagnostics.terminal_issue_url(result)}")
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
        _doctor_headless_auth(issues)
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
            if major >= MIN_NODE_MAJOR:
                print(f"  node:        ✅ {node} (v{major})")
            else:
                print(
                    f"  node:        ⚠️  v{major} < {MIN_NODE_MAJOR} (frontend needs Node {MIN_NODE_MAJOR}+)"
                )
                print(f"               Fix: install Node.js >= {MIN_NODE_MAJOR}")
        except Exception:
            print(f"  node:        ✅ {node}")
    else:
        print(f"  node:        ⚠️  not found (frontend needs Node {MIN_NODE_MAJOR}+)")
        print(f"               Fix: install Node.js >= {MIN_NODE_MAJOR}")

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

    # ── Data Home (+ leftover legacy home) ──
    _doctor_data_home()
    _doctor_path_launcher()
    _doctor_trust_root()

    # ── KAS backend (only when selected) ──
    _doctor_kas(issues)

    # ── Pods (systemd --user session bus) ──
    _doctor_pod_session_bus(issues)

    # ── Sandbox ──
    # Ahead of MCP Tools: the probes below spawn through the sandbox chokepoint,
    # so this verdict is the context for any probe failure they report.
    _doctor_sandbox(issues)

    # ── Memory pressure preparedness (swap / userspace OOM killer) ──
    _doctor_memory_pressure(issues)

    # ── kiro-cli installer residue (silent unless residue is on disk) ──
    _doctor_cli_installer_residue(issues)

    # ── MCP Tools ──
    print("\nMCP Tools")
    if agent_path.exists():
        _doctor_mcp_tools(agent_path, issues)
        # After the probe, deliberately: the probe reporting green is the exact
        # condition this section exists to explain.
        _doctor_mcp_governance(agent_path, issues)

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

    # ── Source Checkout (source/editable installs only) ──
    # Gated on the checkout markers themselves (setup.cfg + src/kiro_crew, via
    # _bootstrap), not on ./.venv existing: an editable install driven by an
    # external virtualenv or a documented ``PYTHONPATH=src`` invocation runs
    # stale source exactly the same way and was silently skipped by the venv
    # gate. A wheel install resolves inside site-packages, has no markers two
    # levels up, and correctly gets no section.
    source_root = _source_checkout_root()
    if source_root is not None:
        _doctor_source_checkout(source_root)

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

    # Parakeet (NVIDIA Parakeet via parakeet-mlx) is Apple-Silicon-only and, like
    # mlx_whisper, installed out-of-band — so report its CLI the same way.
    if stt_active and cfg.stt.provider == "parakeet":
        parakeet_bin = _find_parakeet_mlx()
        if parakeet_bin:
            print(f"  parakeet:    ✅ {parakeet_bin}")
        else:
            mark = "❌" if stt_fatal else "⚠️ "
            print(f"  parakeet:    {mark} parakeet-mlx not found")
            print("               Fix: pipx install parakeet-mlx  (Apple Silicon only)")
            if stt_fatal:
                issues.append("parakeet-mlx")

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
        print("  status:      ⏭  not configured (optional)")
        print("  setup:       run 'kirocrew setup --slack', or connect any channel")
        print("               (Slack, Discord, Telegram, …) from the dashboard")

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
                # 8 lines = preamble + thread header + ~6 frames: enough to
                # reach past the asyncio plumbing into the Kiro Crew frame
                # that identifies WHERE the loop wedged.
                _stack = dump_first_stack_lines(_latest, max_lines=8)
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

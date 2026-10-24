"""CLI doctor subcommand — verify Kiro Crew setup and diagnose issues.

:func:`_doctor` is the orchestrator: it prints the report's sections in one fixed
order, threads one ``issues`` list through them, and turns that list into the
closing verdict and the exit status. Most sections live in the
:mod:`kiro_crew.doctor_checks` families. Two kinds of row stay here.

The rows the orchestrator composes itself: the Platform edition and jail rows it
reads off the context ``cli.main`` could not compose, and the Dependencies, Agent,
Runtime and Connectivity rows it builds from the values it threads onward.

The rows a repository gate pins to this file: the venv-interpreter and
``kiro-cli --version`` probes ``_doctor`` spawns itself, keyed by
``cli_doctor.py::_doctor`` in ``test/test_spawn_audit.py``, and the embedding-model
URL probe, whose redactor call ``security_posture`` registers under this file.
The Model section and the model-pin audit, MCP Tools and MCP Governance, the host
probes and the KAS relay rows live in the :mod:`kiro_crew.cli_doctor_parts` owners,
whose functions run on this module's globals (see the foot of this module). The
imports every row reads stay here, the ACP imports
``.github/agent-sdk-boundary-baseline.txt`` counts for this file among them, and so
do the constants the rows share.

This module is also the doctor's one patch target. A family reads every function,
class and value this module binds through this module at call time (a module is one
shared object, so a family imports it directly; a section that imported a name inside
its own body keeps doing so), every family section stays reachable here under its
old name, with a write forwarded to the family (see the facade at the end of this
module), and a composed row reads this module's globals, so a patch here reaches it.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import logging
import os
import platform as _plat  # noqa: F401
import shlex  # noqa: F401
import shutil
import subprocess
import sys
import tempfile  # noqa: F401
import textwrap  # noqa: F401
import urllib.error
import urllib.request
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

from kiro_crew import __version__ as _mc_version
from kiro_crew import agent as _agent
from kiro_crew import (  # noqa: F401
    agent_state,
    dep_sync,
    diagnostics,
    platform_compat,
    sandbox,
    session_pid,
    stdlib_shadow,
    stt,
    user_json,
)
from kiro_crew._bootstrap import _source_checkout_root
from kiro_crew._facade_compose import compose as _facade_compose
from kiro_crew._llama_lib_path import (  # noqa: F401
    dropped_inherited_lib_path,
)
from kiro_crew.acp.client import KIRO_CLI_BIN
from kiro_crew.acp.kas_transport import (
    KAS_RELAY_ENGINE,
    build_kas_argv,
)

# The one skill-view count ceiling, enforced in the projection and reused as the
# doctor's backlog-warn threshold. Imported from the ``agent_spec_format`` leaf
# (above, beside the alias prefix) rather than the ACP projection, so this facade
# does not grow the agent-SDK import boundary. The doctor_checks.resources family
# reads it as ``cli_doctor.SKILL_VIEW_PROJECTION_CEILING`` -- see
# test_cli_doctor_refactor_family_reads (the family binds no project module by name).
from kiro_crew.acp.types import ACP_BACKEND_KAS
from kiro_crew.agent import AGENT_FILENAME, agent_spec_path
from kiro_crew.agent_discovery import (
    _read_agent_spec,
    project_agent_files,
    project_agent_name,
)
from kiro_crew.agent_sdk import kas_engine_gap
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO
from kiro_crew.agent_sdk.provider_identity import is_claude_code
from kiro_crew.agent_spec_format import (  # noqa: F401
    NATIVE_SKILL_ALIAS_PREFIX,
    SKILL_VIEW_PROJECTION_CEILING,
    is_agent_spec_name,
    shadowed_markdown_specs,
)
from kiro_crew.agents_janitor import sweep_agents_dir  # noqa: F401
from kiro_crew.atomic_write import atomic_write
from kiro_crew.cli_doctor_parts.host_probes import (
    _aws_auto_refreshes,
    _aws_probe_env,
    _aws_profile_names,
    _credential_vendor_line,
    _detect_userspace_oom_killer,
    _discord_intent_grants,
    _gateway_rss_bytes,
    _git_line,
    _kiro_cli_signed_in,
    _linger_enabled,
    _report_node,
    _venv_deps_ok,
)
from kiro_crew.cli_doctor_parts.kas_relay import (
    _doctor_kas,
    _kas_relay_help,
    _report_kas_backend,
    _report_kas_bundle,
    _report_kas_spec_permissions,
    _report_kas_tool_vocabulary,
)
from kiro_crew.cli_doctor_parts.mcp_sections import (
    _doctor_gated_off_mcps,
    _doctor_mcp_governance,
    _doctor_mcp_tools,
    _doctor_whoami_governance,
    _spec_gate_closed,
)
from kiro_crew.cli_doctor_parts.model_section import (
    _agent_spec_model_problems,
    _doctor_effective_model,
    _format_model_pin_problem,
    _strict_agent_json_specs,
)
from kiro_crew.cli_perf import _read_gateway_pid  # noqa: F401
from kiro_crew.config import KiroCrewConfig
from kiro_crew.config.loader import (  # noqa: F401
    CRED_DISCORD_BOT_TOKEN,
    config_dir,
    env_path,
    normalize_agent_model,
    resolve_agent_bindings,
    resolve_effective_model,
    unsandboxed_exec_declared,
)
from kiro_crew.config.paths import (  # noqa: F401
    LEGACY_CONFIG_DIR_NAME,
    _valid_override_home,
    data_home,
    kiro_agents_dir,
    project_agents_dir,
)
from kiro_crew.config.superseded_defaults import render_doctor_section
from kiro_crew.constants import (
    MIN_NODE_VERSION,
    format_node_version,
    node_too_old_message,
    node_version_meets_floor,
    parse_node_version,
)
from kiro_crew.cron import (  # noqa: F401
    cron_store_quarantine_copies,
    job_pause_state_from_disk,
    unhealthy_jobs_from_disk,
)
from kiro_crew.dashboard.crash_dump_store import (  # noqa: F401
    dump_age_seconds,
    dump_first_stack_lines,
    dump_superseded,
    dumps_with_stacks,
    get_dumps_dir,
    newest_dump_with_stacks,
)
from kiro_crew.dashboard.origin import (  # noqa: F401
    is_local_only,
    machine_hostname,
    parse_dashboard_url,
)
from kiro_crew.deny_guidance import credential_vendor_server_ids
from kiro_crew.discord import install_url, intent_probe  # noqa: F401
from kiro_crew.doctor_deadpath import doctor_dead_paths
from kiro_crew.embeddings import (  # noqa: F401
    _LIB_PATH_ENV,
    _load_llama_class,
    _platform_libs_dirname,
    _resolve_model_url,
    default_model_path,
    model_file_present,
    operator_lib_path_override,
    resolve_custom_model,
    verify_vendored_libs,
)
from kiro_crew.extras import (  # noqa: F401
    pip_install_channel_available,
    pip_install_command,
    pip_install_command_for,
)
from kiro_crew.kiro_cli import (
    PATH_ONLY_INSTALL_NOTE,
    SPEC_PERMISSIONS_MIN_VERSION,
    installed_kiro_cli_version,
    kas_relay_help,
    mcp_governance_may_apply,
    resolve_kiro_cli,
    spec_permissions_supported,
)
from kiro_crew.mcp_cleanup import ALWAYS_ON_BIN_MCP_SERVERS as _ALWAYS_ON_MCPS
from kiro_crew.mcp_cleanup import KIROCREW_BIN_MCP_SERVERS as _MANAGED_MCPS
from kiro_crew.mcp_cleanup import OPT_IN_BIN_MCP_SERVERS as _OPT_IN_MCPS
from kiro_crew.mcp_discovery import McpServerInfo, probe_server
from kiro_crew.mcp_utils import mcp_ref_owned_by, without_mcp_refs
from kiro_crew.members import is_dispatchable_member_name  # noqa: F401
from kiro_crew.model_registry import acp_id_correction
from kiro_crew.platform import (
    PlatformCompositionError,
)
from kiro_crew.platform import context as platform_context
from kiro_crew.platform import (
    current_context,
    safe_context_call,
)
from kiro_crew.platform.capability_bound import bind_capability_manager
from kiro_crew.platform.defaults import DefaultCapabilityManager
from kiro_crew.platform.governance import (
    CU_MCP_SERVER,
    may_skip_gate_now,
    withheld_by_confirm_tools,
)
from kiro_crew.platform.tool_names import (
    kas_verified_releases,
    kas_vocabulary_drift,
)
from kiro_crew.sandbox import _MOUNT_SOURCE_PREFIX, warm_backend  # noqa: F401
from kiro_crew.security import is_sensitive_path
from kiro_crew.sel import sel
from kiro_crew.service import apparmor  # noqa: F401
from kiro_crew.service import common as common_service  # noqa: F401
from kiro_crew.service import controller as service_controller  # noqa: F401
from kiro_crew.service import linux as service_linux  # noqa: F401
from kiro_crew.session_pid_sig import signing_health  # noqa: F401
from kiro_crew.stall_attribution import attribute_dump, describe  # noqa: F401
from kiro_crew.stt.decoder import bundle_carries_decoder  # noqa: F401
from kiro_crew.subprocess_utf8 import UTF8_TEXT
from kiro_crew.transcribe import (  # noqa: F401
    _find_ffmpeg,
    availability_detail,
    ensure_ffmpeg_in_path,
)
from kiro_crew.validation import is_registered_agent_name

if TYPE_CHECKING:  # served by ``__getattr__`` at runtime; named here for mypy
    from kiro_crew.doctor_checks.access import (  # noqa: F401
        _BLOCKED_COMMANDS_DOC_URL,
        _doctor_credentials,
        _doctor_name_grant_platform_scope,
        _doctor_trust_root,
    )
    from kiro_crew.doctor_checks.agents import (  # noqa: F401
        _CLAUDE_ACP_BIN,
        _MEMBER_NAMES_NOT_CHECKED,
        _backend_policy_label,
        _doctor_agent_auth,
        _doctor_claude_backend,
        _doctor_deprecated_agent_specs,
        _doctor_member_dispatchability,
        _doctor_member_memory_bindings,
        _member_dispatchability,
        _open_slot_agent_names,
    )
    from kiro_crew.doctor_checks.channels import (  # noqa: F401
        _discord_install_line,
        _discord_live_state,
        _discord_msg_content_line,
        _discord_unused_intent_line,
        _doctor_discord,
        _doctor_whatsapp,
    )
    from kiro_crew.doctor_checks.confinement import (  # noqa: F401
        _doctor_kiro_internal_sandbox,
        _doctor_live_target_pointer,
        _doctor_masked_credential_aliases,
        _doctor_sandbox,
        _doctor_sandbox_apparmor,
        _doctor_sandbox_backend,
        _process_apparmor_confinement,
        _process_userns_vantage_confined,
        _read_linux_proc_self,
        _service_profile_applies,
    )
    from kiro_crew.doctor_checks.features import (  # noqa: F401
        _FFMPEG_LINUX_HINT,
        _os_fix_hint,
    )
    from kiro_crew.doctor_checks.install import (  # noqa: F401
        _LEGACY_VENV_DIR_NAMES,
        _doctor_cron_script_sources,
        _doctor_data_home,
        _doctor_import_path,
        _doctor_path_launcher,
        _doctor_skill_currency,
        _doctor_source_checkout,
        _legacy_venv_entries,
    )
    from kiro_crew.doctor_checks.mcp import (  # noqa: F401
        _MAIN_AGENT_NAME,
        _STRICT_IDENTITY_SERVERS,
        _doctor_backend_ability_cards,
        _doctor_mcp_gateway_daemon,
        _doctor_selected_backend_projection,
        _doctor_strict_identity,
        _doctor_unresolved_mcp_refs,
    )
    from kiro_crew.doctor_checks.render import (  # noqa: F401
        _INDENT,
        _print_wrapped,
        _safe_display,
    )
    from kiro_crew.doctor_checks.resources import (  # noqa: F401
        _CLI_INSTALLER_GLOB,
        _CLI_INSTALLER_RESIDUE_MIN,
        _CLI_INSTALLER_SCAN_CAP,
        _PROC_MEMINFO,
        _RUN_DIR_BACKLOG_WARN,
        _SKILL_VIEW_BACKLOG_WARN,
        _TMPFS_FREE_INODES_FLOOR,
        _TMPFS_FREE_PCT_WARN,
        _doctor_agents_janitor,
        _doctor_cli_installer_residue,
        _doctor_memory_pressure,
        _doctor_run_dirs,
        _doctor_runtime_tmpfs,
        _doctor_skill_view_census,
        _gateway_memory_lines,
        _runtime_tmpfs_roots,
        _scan_cli_installer_residue,
        _swap_total_kib,
        _tmpfs_usage,
    )
    from kiro_crew.doctor_checks.services import (  # noqa: F401
        _doctor_headless_auth,
        _doctor_managed_service_policy,
        _doctor_pod_session_bus,
    )
    from kiro_crew.doctor_checks.workload import (  # noqa: F401
        _CRON_REPORT_CAP,
        _STALL_CURRENT_SECS,
        _doctor_cron_health,
        _doctor_overload_resilience,
        _doctor_task_store,
        _format_job_labels,
        _liveness_platform_line,
    )

logger = logging.getLogger(__name__)

# ``KIRO_AGENTS_DIR`` is an import-time override hook, NOT a frozen path.
# ``None`` means "resolve from the live data home"; tests patch this
# attribute directly (``patch("kiro_crew.cli_doctor.KIRO_AGENTS_DIR", tmp)``),
# so the name is kept and read through ``_agents_dir()``.
KIRO_AGENTS_DIR: Path | None = None


def _agents_dir() -> Path:
    """Kiro agents directory, honoring the override hook, else the live home."""
    return KIRO_AGENTS_DIR if KIRO_AGENTS_DIR is not None else kiro_agents_dir()


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
# ``kirocrew-guide`` and ``kirocrew-mate`` are always on but are granted per
# tool (``agent._GUIDE_AUTO_GRANTS``, ``agent._MATE_AUTO_GRANTS``); a
# whole-server grant would also pre-approve any tool added to either later,
# before anyone reviewed it.
_NO_BLANKET_ALLOW_MCPS = frozenset({CU_MCP_SERVER, "kirocrew-guide", "kirocrew-mate"}) | frozenset(
    _OPT_IN_MCPS
)


# Userspace OOM killers doctor knows how to detect, in probe order:
# systemd-oomd ships with systemd (the common case), earlyoom is the usual
# add-on daemon.
_OOM_KILLER_UNITS = ("systemd-oomd", "earlyoom")


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


#: Ceiling on each ``aws configure`` probe in the Credentials section. Doctor is
#: interactive, and an AWS CLI that stalls on a network-backed credential source
#: must cost a bounded pause rather than hanging the whole run.
_AWS_PROBE_TIMEOUT_SECS = 10


def _doctor(platform_boot_error: "Exception | None" = None, bundle: bool = False) -> None:
    """Verify KiroCrew setup — check dependencies, config, credentials, connectivity.

    ``platform_boot_error`` carries a :class:`PlatformCompositionError` from
    ``cli.main`` when the platform context failed to compose (e.g. a profile
    resolved to a non-standalone edition whose companion is missing).  The
    doctor is deliberately allowed to run in that state — diagnosing a broken
    setup is its job — and reports the failure here instead of aborting.

    The order of the sections below IS the report: each section prints as it runs
    and appends to one ``issues`` list, and the closing line and exit status are
    decided from that list alone. The families are imported here, once the
    ``--bundle`` mode has had its turn, so the ``kiro_crew.cli`` import every command
    pays never loads them.
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
        issue_url = diagnostics.terminal_issue_url(result)
        if issue_url:
            print("\n  Open a GitHub issue (then drag the zip in):")
            print(f"  {issue_url}")
        else:
            print("\n  Issue link skipped: the platform companion does not compose.")
        return

    from kiro_crew.doctor_checks import (
        access,
        agents,
        channels,
        confinement,
        features,
        install,
        mcp,
        render,
        resources,
        services,
        workload,
    )

    # ── Platform edition ──
    # Report the composed profile, and surface a boot-composition failure as a
    # blocking issue with the remediation hint rather than letting it abort the
    # whole CLI before the doctor can run.
    print("Platform")
    if platform_boot_error is not None:
        print(f"  edition:     ❌ composition failed: {platform_boot_error}")
        issues.append(f"platform composition failed: {platform_boot_error}")
    else:
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
    # kiro-cli is the DEFAULT agent backend and the floor every deployment keeps.
    # Claude Code is selectable too (``BASELINE_SELECTABLE_BACKENDS``), so it is
    # reported as a real optional backend -- present or absent -- rather than only
    # when it happens to be installed. The verdict comes from the same owner the
    # dashboard asks, so doctor and the panel cannot disagree.
    # Resolved the way the gateway resolves it -- KIROCREW_KIRO_BIN, then the
    # desktop app's bundled copy, then the known install dirs, then PATH -- so
    # this row names the binary a session actually spawns. ``shutil.which`` would
    # name the user's own install on a bundled app, which is not the one running.
    kiro = resolve_kiro_cli()
    if kiro:
        print(f"  kiro-cli:    ✅ {kiro}")
        services._doctor_headless_auth(issues)
    else:
        print("  kiro-cli:    ⏭  not found (the default agent backend)")
        print("               Install kiro-cli per its docs, then: kiro-cli login")

    agents._doctor_claude_backend()
    # After the install rows, and per harness rather than per provider: sign-in is a
    # different question from install with a different remedy, and every harness's
    # answer now comes from one declaration instead of a block written per backend.
    agents._doctor_agent_auth()

    git = shutil.which("git")
    if git:
        print(f"  git:         ✅ {git}")
    else:
        print("  git:         ❌ not found (needed for kirocrew update)")
        issues.append("git")

    _report_node(issues)
    install._doctor_browser_bootstrap()

    # venv detection — used by the runtime section below. Windows venvs put the
    # interpreter under .venv\Scripts\python.exe, not .venv/bin/python3, so a
    # hardcoded POSIX layout misreports the venv (and the runtime section) on
    # every Windows install.
    venv_root = Path(__file__).resolve().parents[2] / ".venv"
    if platform_compat.IS_WINDOWS:
        venv_py = venv_root / "Scripts" / "python.exe"
    else:
        venv_py = venv_root / "bin" / "python3"
    is_venv_install = venv_py.is_file()

    # ── Project ──
    proj = install._doctor_project(issues)

    cfg = KiroCrewConfig.load()

    # ── Agent config ──
    print("\nAgent")
    agent_path = _agents_dir() / AGENT_FILENAME
    if agent_path.exists():
        print(f"  config:      ✅ {agent_path}")
    else:
        print("  config:      ❌ not found (run kirocrew setup)")
        issues.append("agent config")

    # Model pins across ALL specs, not just the default one. A pin kiro-cli
    # cannot serve kills every session and subagent using that agent seconds
    # after startup, and nothing else reports it before something spawns: the
    # entitlement guards all sit behind session init, while kiro-cli reads this
    # field when the child starts.
    #
    # The project dir is threaded through because a project spec SHADOWS a
    # user-level agent of the same name — scanning only the global scope would
    # miss the very spec a session in this project actually runs, and report a
    # clean bill of health for it.
    _bad_pins = _agent_spec_model_problems(project_dir=proj or None, provider=cfg.agent.provider)
    if _bad_pins is None:
        print("  model pins:  ⚠️  could not check (agent specs unreadable)")
        issues.append("agent model pins unchecked")
    elif _bad_pins:
        for _agent_name, _pin, _correction in _bad_pins:
            for _line in _format_model_pin_problem(_agent_name, _pin, _correction):
                print(_line)
        issues.append("agent model pin")
    else:
        print("  model pins:  ✅ no unusable spellings in agent specs")

    # ── Config ──
    dashboard = services._doctor_configuration(cfg, issues)

    # ── Effective model (+ which tier decided it) ──
    # After Configuration, deliberately: that section prints the global
    # agent.model, and the whole point here is that the global is not
    # necessarily what a new session gets.
    _doctor_effective_model(cfg, proj, issues)
    agents._doctor_member_dispatchability(cfg, issues)
    agents._doctor_member_memory_bindings(cfg, issues)

    # ── Stored defaults a release has since changed ──
    render_doctor_section(issues)

    # ── Installed services must carry the launch-class marker ──
    services._doctor_managed_service_policy(issues)

    # ── Data Home (+ leftover legacy home) ──
    install._doctor_data_home()
    install._doctor_cron_script_sources(issues)
    install._doctor_skill_currency(issues)
    agents._doctor_deprecated_agent_specs(cfg, issues)
    install._doctor_path_launcher()
    access._doctor_trust_root()
    access._doctor_signing_key(issues)
    access._doctor_name_grant_platform_scope()
    mcp._doctor_strict_identity(cfg)
    mcp._doctor_mcp_gateway_daemon(issues)
    mcp._doctor_unresolved_mcp_refs()
    mcp._doctor_backend_ability_cards(cfg)
    mcp._doctor_selected_backend_projection(cfg)

    # ── Credentials (AWS / credential-vending MCP) ──
    # After identity, before the agent-facing sections: this is the answer to
    # "the agent says it cannot reach AWS", which is a credential-posture
    # question rather than an agent one.
    access._doctor_credentials(issues)

    # ── Agents dir janitor (orphaned atomic-write temps + stale backups) ──
    resources._doctor_agents_janitor(issues, cfg.agent.sweep_agents_backups)

    # ── KAS backend (only when selected) ──
    _doctor_kas(issues)

    # ── Pods (systemd --user session bus) ──
    services._doctor_pod_session_bus(issues)

    # ── Sandbox ──
    # Ahead of MCP Tools: the probes below spawn through the sandbox chokepoint,
    # so this verdict is the context for any probe failure they report.
    confinement._doctor_sandbox(issues)

    # ── Live-target pointer (silent unless it will refuse the next spawn) ──
    # Immediately after Sandbox: the condition IS a sandbox refusal, and an operator
    # who just read the backend verdict is the one who needs to know a spawn will be
    # refused for a reason the backend line cannot express.
    confinement._doctor_live_target_pointer(issues)
    confinement._doctor_masked_credential_aliases(issues)

    # ── Memory pressure preparedness (swap / userspace OOM killer) ──
    resources._doctor_memory_pressure(issues)

    # ── ACP runtimes: live count, owner and backend CPU (advisory; Linux) ──
    resources._doctor_acp_runtimes(issues)

    # ── Runtime tmpfs headroom (sandbox mount-source roots; Linux only) ──
    resources._doctor_runtime_tmpfs(issues)

    # ── kiro-cli installer residue (silent unless residue is on disk) ──
    resources._doctor_cli_installer_residue(issues)

    # ── Cron job health (silent unless a job auto-paused or errored) ──
    # Reads crons.json off disk, not the gateway API: the gateway's own
    # per-job badge and hourly failure re-alert cannot report a wedged gateway.
    workload._doctor_cron_health(issues)

    # ── Durable task queue (silent when no tasks.db exists yet) ──
    with render._unreadable_skips_section(
        issues, "task store", confined=confinement._doctor_vantage_confined
    ):
        workload._doctor_task_store(issues)

    # ── Overload resilience: configured bounds + platform liveness evidence ──
    workload._doctor_overload_resilience(cfg)
    workload._doctor_ledger_backlog()  # count of what `kirocrew ledger-sweep` lists

    # ── Agent Spec Paths (dead command/args/env paths) ──
    # Own module + single call so a sibling sweep wiring into doctor rebases
    # trivially. Walks EVERY spec in the agents dir (not just kirocrew.json),
    # so it runs unconditionally rather than under the agent_path guard below.
    # Pass doctor's OWN resolved agents dir so the scan — and any managed repair
    # it triggers — operate on the same directory doctor is inspecting, never a
    # re-resolved live home while doctor is pointed elsewhere.
    #
    # BEFORE the MCP probe, deliberately: the managed repair rewrites a spec
    # whose command went dead, and the probe should observe the repaired spec.
    # Ordered the other way round, the probe records the stale command as a
    # failure first and a successful repair still exits nonzero.
    doctor_dead_paths(issues, agents_dir=_agents_dir())

    # ── MCP Tools ──
    print("\nMCP Tools")
    if agent_path.exists():
        # One gate snapshot for both sections, so a keystone flip landing
        # between them cannot make the report contradict itself (see
        # _doctor_gated_off_mcps).
        gated_off = _doctor_gated_off_mcps()
        _doctor_mcp_tools(agent_path, issues, gated_off=gated_off)
        # After the probe, deliberately: the probe reporting green is the exact
        # condition this section exists to explain.
        _doctor_mcp_governance(agent_path, issues, gated_off=gated_off)

    # ── Python Runtime ──
    print("\nRuntime")
    # Prefer venv install (pip install -e); otherwise verify the running Python.
    if is_venv_install:
        try:
            py_result = subprocess.run(
                [str(venv_py), "--version"],
                capture_output=True,
                timeout=5,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                **UTF8_TEXT,
            )
            py_result.check_returncode()
            ver = py_result.stdout.strip()
            print(f"  python:      ✅ {venv_py} ({ver})")
        except Exception as exc:
            print(f"  python:      ❌ venv python broken: {exc}")
            issues.append("venv python")
        else:
            if _venv_deps_ok(venv_py):
                print("  deps:        ✅ websockets, slack_sdk, aiohttp available")
            else:
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
            if pip_install_channel_available():
                print(f"               Fix: {pip_install_command_for('-e', '.')}")
            issues.append("python deps")

    install._doctor_import_path(issues)
    install._doctor_sqlite_fts5(issues)
    install._doctor_tz_data(issues)

    # ── Source Checkout (source/editable installs only) ──
    # Gated on the checkout markers themselves (setup.cfg + src/kiro_crew, via
    # _bootstrap), not on ./.venv existing: an editable install driven by an
    # external virtualenv or a documented ``PYTHONPATH=src`` invocation runs
    # stale source exactly the same way and was silently skipped by the venv
    # gate. A wheel install resolves inside site-packages, has no markers two
    # levels up, and correctly gets no section.
    source_root = _source_checkout_root()
    if source_root is not None:
        install._doctor_source_checkout(source_root)

    # ── Vector Memory (in-process embeddings) ──
    features._doctor_vector_memory(issues)

    # ── Speech-to-Text (optional) ──
    features._doctor_speech_to_text(cfg, issues)

    # ── Slack (optional) ──
    channels._doctor_slack(cfg, dashboard.creds, dashboard.has_slack, issues)

    # ── Discord (optional) ──
    channels._doctor_discord(cfg, dashboard.creds, dashboard.port, issues)

    # ── WhatsApp (optional) ──
    # Its own section rather than a line in the Slack one: WhatsApp's
    # prerequisites are an optional wheel and a local credential store, neither of
    # which any other channel has, and both of which fail silently.
    channels._doctor_whatsapp(cfg, issues)

    # ── Every other channel (optional) ──
    channels._doctor_other_channels(cfg, dashboard.creds, issues)

    # ── Loop-stall crash dumps ──
    workload._doctor_crash_dumps(issues)

    # ── Connectivity ──
    print("\nConnectivity")
    if kiro:
        kiro_result = subprocess.run(
            [kiro, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        if kiro_result.returncode == 0:
            ver = kiro_result.stdout.strip() or kiro_result.stderr.strip()
            print(f"  kiro-cli:    ✅ {ver}")
        else:
            print("  kiro-cli:    ⚠️  exits with error (optional backend)")
    else:
        print("  kiro-cli:    ⏭  skipped (not installed)")
    # The launcher answers ``--version`` itself; this row asks the chat binary
    # every session actually execs, which is what a stale install breaks. It is
    # fatal only when a harness that spawns kiro-cli is selected for either role:
    # the Kiro CLI backend, or KAS served through kiro-cli's relay, as the main
    # backend or as the crewmate backend (the loader already reads a member
    # "kiro" as KAS). Positive tests, so a harness added later does not inherit
    # a fatal row for a binary it never runs.
    relay_kas = not cfg.agent.kas_bundle
    services._doctor_kiro_chat_delegate(
        kiro,
        issues,
        required=cfg.agent.acp_backend == ACP_BACKEND_KIRO
        or (relay_kas and cfg.agent.acp_backend == ACP_BACKEND_KAS)
        or (relay_kas and cfg.agent.member_acp_backend == ACP_BACKEND_KAS),
    )
    services._doctor_gateway_reachability(dashboard, issues)

    # ── Summary ──
    print()
    if issues:
        print(f"❌ Fix these issues: {', '.join(issues)}")
        sys.exit(1)
    else:
        print("✅ Kiro Crew is ready!")


# ── The facade ────────────────────────────────────────────────────────────────
# Every section that neither stays here nor runs here from a cli_doctor_parts owner
# lives in a ``kiro_crew.doctor_checks`` family. The names this module held before the
# sections moved stay reachable here; a section extracted from ``_doctor`` by that
# move never lived here and is patched on its family. Two properties keep the
# moved names true:
#
# 1. A read here answers with the object the family holds, resolved on each
#    access, so there is no second copy to drift from it.
# 2. Setting the attribute HERE reaches the family. A section calls its own
#    helpers through its own globals, so a patch that landed only on this module
#    would leave the section running the unpatched helper -- the test passes
#    while testing nothing. ``_ReExportModule`` below forwards the write.
#
# Both halves resolve the family from ``sys.modules`` on each access, importing it
# only on a miss, so a purged and re-imported family is seen at once. The
# names this module binds itself -- the imports above and the sections defined
# here -- are not in the table; a family reads those through this module
# (``cli_doctor.config_dir()``), which is what keeps a patch here reaching them.
# A module the doctor imports is one shared object in every family, so patch its
# attributes (``cli_doctor.sandbox.detect_backend``) rather than rebinding it.
# ``mock.patch(..., create=True)`` must not target a name in the table: its undo
# deletes the family's binding.

#: Re-exported name -> the dotted name of the family module that DEFINES it.
_EXPORTS: dict[str, str] = {
    **dict.fromkeys(
        ("_INDENT", "_print_wrapped", "_safe_display"),
        "kiro_crew.doctor_checks.render",
    ),
    **dict.fromkeys(
        (
            "_CLAUDE_ACP_BIN",
            "_MEMBER_NAMES_NOT_CHECKED",
            "_backend_policy_label",
            "_doctor_agent_auth",
            "_doctor_claude_backend",
            "_doctor_deprecated_agent_specs",
            "_doctor_member_dispatchability",
            "_doctor_member_memory_bindings",
            "_member_dispatchability",
            "_open_slot_agent_names",
        ),
        "kiro_crew.doctor_checks.agents",
    ),
    **dict.fromkeys(
        (
            "_MAIN_AGENT_NAME",
            "_STRICT_IDENTITY_SERVERS",
            "_doctor_backend_ability_cards",
            "_doctor_mcp_gateway_daemon",
            "_doctor_selected_backend_projection",
            "_doctor_strict_identity",
            "_doctor_unresolved_mcp_refs",
        ),
        "kiro_crew.doctor_checks.mcp",
    ),
    **dict.fromkeys(
        (
            "_doctor_kiro_internal_sandbox",
            "_doctor_live_target_pointer",
            "_doctor_masked_credential_aliases",
            "_doctor_sandbox",
            "_doctor_sandbox_apparmor",
            "_doctor_sandbox_backend",
            "_process_apparmor_confinement",
            "_process_userns_vantage_confined",
            "_read_linux_proc_self",
            "_service_profile_applies",
        ),
        "kiro_crew.doctor_checks.confinement",
    ),
    **dict.fromkeys(
        (
            "_BLOCKED_COMMANDS_DOC_URL",
            "_doctor_credentials",
            "_doctor_name_grant_platform_scope",
            "_doctor_trust_root",
        ),
        "kiro_crew.doctor_checks.access",
    ),
    **dict.fromkeys(
        (
            "_LEGACY_VENV_DIR_NAMES",
            "_doctor_cron_script_sources",
            "_doctor_data_home",
            "_doctor_import_path",
            "_doctor_path_launcher",
            "_doctor_skill_currency",
            "_doctor_source_checkout",
            "_legacy_venv_entries",
        ),
        "kiro_crew.doctor_checks.install",
    ),
    **dict.fromkeys(
        (
            "_doctor_headless_auth",
            "_doctor_managed_service_policy",
            "_doctor_pod_session_bus",
        ),
        "kiro_crew.doctor_checks.services",
    ),
    **dict.fromkeys(
        (
            "_CLI_INSTALLER_GLOB",
            "_CLI_INSTALLER_RESIDUE_MIN",
            "_CLI_INSTALLER_SCAN_CAP",
            "_PROC_MEMINFO",
            "_RUN_DIR_BACKLOG_WARN",
            "_SKILL_VIEW_BACKLOG_WARN",
            "_TMPFS_FREE_INODES_FLOOR",
            "_TMPFS_FREE_PCT_WARN",
            "_doctor_agents_janitor",
            "_doctor_cli_installer_residue",
            "_doctor_memory_pressure",
            "_doctor_run_dirs",
            "_doctor_runtime_tmpfs",
            "_doctor_skill_view_census",
            "_gateway_memory_lines",
            "_runtime_tmpfs_roots",
            "_scan_cli_installer_residue",
            "_swap_total_kib",
            "_tmpfs_usage",
        ),
        "kiro_crew.doctor_checks.resources",
    ),
    **dict.fromkeys(
        (
            "_CRON_REPORT_CAP",
            "_STALL_CURRENT_SECS",
            "_doctor_cron_health",
            "_doctor_overload_resilience",
            "_doctor_task_store",
            "_format_job_labels",
            "_liveness_platform_line",
        ),
        "kiro_crew.doctor_checks.workload",
    ),
    **dict.fromkeys(
        (
            "_discord_install_line",
            "_discord_live_state",
            "_discord_msg_content_line",
            "_discord_unused_intent_line",
            "_doctor_discord",
            "_doctor_whatsapp",
        ),
        "kiro_crew.doctor_checks.channels",
    ),
    **dict.fromkeys(
        (
            "_FFMPEG_LINUX_HINT",
            "_os_fix_hint",
        ),
        "kiro_crew.doctor_checks.features",
    ),
}


def _family(module_name: str) -> ModuleType:
    """Return a family module, read from where modules are stored.

    :data:`sys.modules` IS the one place a module is stored, so the read goes
    there and a purged or replaced family is seen at once; ``import_module``
    answers only the miss, which also keeps a test that patches it for its own
    reasons from rerouting every read here.
    """
    try:
        return sys.modules[module_name]
    except KeyError:
        return importlib.import_module(module_name)


def __getattr__(name: str) -> Any:
    """Read a re-exported name from the family that owns it (:pep:`562`)."""
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(_family(_EXPORTS[name]), name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))


class _ReExportModule(ModuleType):
    """Send a write to a re-exported name to the family that owns it.

    Binding the name here instead would shadow the family permanently, because
    ``__getattr__`` runs only for a name this module does not hold. Forwarding
    leaves one value to remember and one to put back, so ``monkeypatch`` and
    ``mock.patch`` restore exactly what they found.
    """

    def __setattr__(self, name: str, value: Any) -> None:
        if name in _EXPORTS:
            setattr(_family(_EXPORTS[name]), name, value)
        else:
            super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        if name in _EXPORTS:
            delattr(_family(_EXPORTS[name]), name)
        else:
            super().__delattr__(name)


# Installed last, so the forwarding is live for every caller but never runs while
# this module is still binding its own names.
sys.modules[__name__].__class__ = _ReExportModule

# ``from kiro_crew.cli_doctor import *`` consults this list and never reaches
# ``__getattr__``. Derived from the two authorities -- what this module binds and
# the table -- minus the private names a star import never carried.
__all__ = sorted(name for name in set(globals()) | set(_EXPORTS) if not name.startswith("_"))


# The ``kiro_crew.cli_doctor_parts`` owners' functions run on this module's globals, so a
# patch of ``kiro_crew.cli_doctor.<name>`` reaches them wherever they live; see
# :mod:`kiro_crew.cli_doctor_parts`. Run once, after this body has bound every name.
#: The composed owners, in the order this module imports them.
_PARTS = (
    "kiro_crew.cli_doctor_parts.host_probes",
    "kiro_crew.cli_doctor_parts.kas_relay",
    "kiro_crew.cli_doctor_parts.mcp_sections",
    "kiro_crew.cli_doctor_parts.model_section",
)
_facade_compose(globals(), [sys.modules[part] for part in _PARTS])

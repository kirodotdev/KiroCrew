"""CLI doctor subcommand — verify Kiro Crew setup and diagnose issues.

:func:`_doctor` is the orchestrator: it prints the report's sections in one fixed
order, threads one ``issues`` list through them, and turns that list into the
closing verdict and the exit status. Most sections live in the
:mod:`kiro_crew.doctor_checks` families. Two kinds of row stay here.

The rows the orchestrator composes itself: the Platform edition and jail rows it
reads off the context ``cli.main`` could not compose, and the Dependencies, Agent,
Runtime and Connectivity rows it builds from the values it threads onward.

The rows repository gates pin to this file:

* the probes that spawn a process or run an event loop, keyed by
  ``cli_doctor.py::<function>`` in ``test/test_spawn_audit.py`` -- the host probes
  below, the MCP Tools probe, and the node, venv-interpreter and
  ``kiro-cli --version`` rows ``_doctor`` spawns itself;
* the agent-spec reads ``test/test_agent_spec_hardened_reads.py`` inventories for
  this file: the Model section, the model-pin audit and MCP Governance;
* the MCP Tools repair, whose revoke ``test/test_app_mcp_scoping.py`` reads from this
  module's source;
* the KAS relay rows, whose ACP imports ``.github/agent-sdk-boundary-baseline.txt``
  counts for this file;
* the embedding-model URL probe, whose redactor call ``security_posture`` registers
  under this file.

This module is also the doctor's one patch target. A family reads every function,
class and value this module binds through this module at call time (a module is one
shared object, so a family imports it directly; a section that imported a name inside
its own body keeps doing so), and every family section stays reachable here under its
old name, with a write forwarded to the family (see the facade at the end of this
module).
"""

from __future__ import annotations

import asyncio
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
    stdlib_shadow,
    stt,
    user_json,
)
from kiro_crew._bootstrap import _source_checkout_root
from kiro_crew.acp.client import KIRO_CLI_BIN
from kiro_crew.acp.kas_transport import (
    KAS_RELAY_ENGINE,
    KAS_RELAY_ENGINE_FLAG,
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
from kiro_crew.agent_sdk.provider_identity import is_claude_code
from kiro_crew.agent_spec_format import (  # noqa: F401
    NATIVE_SKILL_ALIAS_PREFIX,
    SKILL_VIEW_PROJECTION_CEILING,
    is_agent_spec_name,
)
from kiro_crew.agents_janitor import sweep_agents_dir  # noqa: F401
from kiro_crew.atomic_write import atomic_write
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
from kiro_crew.platform.governance import CU_MCP_SERVER, may_skip_gate_now
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


def _doctor_effective_model(cfg: KiroCrewConfig, project_dir: str, issues: list[str]) -> None:
    """Report which model a new session starts on, and which tier decided it.

    The precedence is real and four tiers deep, and the tier that wins is not
    visible from any single file, so a surprising model -- the wrong one, or a
    stale one that outlived the setting that created it -- is otherwise only
    diagnosable by hand-reading config.json, two agent-spec directories and the
    sidecar.

    The tiers are listed as DATA and the first non-deferring one is marked, which
    is ``resolve_effective_model``'s own rule. The marked value is then
    cross-checked against what that function actually returns and a disagreement
    is REPORTED rather than hidden, so this report cannot quietly drift into a
    second, wrong copy of the precedence.

    Read-only: this section never repairs anything, because a spec's ``model``
    cannot be attributed -- a value an older build's propagation wrote and one
    the user typed in are identical on disk -- so the repair has to be the
    user's explicit call (``kirocrew agent reset-model``).
    """
    from kiro_crew.doctor_checks import render

    print("\nModel")
    try:
        effective = resolve_effective_model(cfg)
    except Exception as exc:  # noqa: BLE001 -- diagnostics must not crash the report
        print(f"  effective:   ⚠️  could not resolve ({exc})")
        issues.append("effective model unresolvable")
        return

    def _spec_model(path: Path) -> tuple[str, bool]:
        """Return (normalized model, usable) for a kiro spec file.

        Routed through ``agent_discovery._read_agent_spec``, which the module
        documents as the ONE reader for both agent scopes so every guard applies
        uniformly: it goes through the hardened size-capped read gate (a
        multi-gigabyte "agent config" is refused rather than slurped), and it
        rejects a symlink whose resolved target is sensitive, non-UTF-8 bytes,
        AppleDouble sidecars and JSON that is not an object. Hand-rolling those
        checks here would be a second, weaker copy of a reader that already
        exists.
        """
        data = _read_agent_spec(path, operation="doctor", source="cli")
        if data is None:
            # An ABSENT spec is not a fault -- a clean install has none, and the
            # resolver simply falls through to the bundled default. Only a file
            # that exists and the hardened reader still refuses is reported.
            try:
                exists = path.exists() or path.is_symlink()
            except OSError:
                exists = True
            return "", not exists
        return normalize_agent_model(data.get("model")), True

    # Deliberately kiro_agents_dir() and not _agents_dir(): this section compares
    # tiers against what resolve_effective_model returned, so it has to read the
    # very directory that function reads. Reporting a different directory's spec
    # beside its verdict is how a report starts contradicting itself.
    agents_dir = kiro_agents_dir()

    # The DEFAULT alias may bind a kiro agent other than the built-in one, and
    # the resolver treats those two differently: a non-default bound agent's own
    # pin is consulted ABOVE the global (tier 2), while the built-in spec is read
    # only after the global defers (tier 4). Reading kirocrew.json in both cases
    # would attribute a custom agent's pin to the wrong file and print a reset
    # command for the wrong agent.
    try:
        bindings = resolve_agent_bindings(cfg)
        override = normalize_agent_model(bindings.model)
        bound = bindings.kiro_agent or "kirocrew"
    except Exception as exc:  # noqa: BLE001 -- a broken alias must not kill the report
        print(f"  binding:     unavailable ({render._safe_display(str(exc))})")
        print("               See the member memory binding diagnostics below.")
        issues.append("default agent binding unavailable")
        override = ""
        bound = "kirocrew"
    # kiro_agent is free text in config.json and this name reaches a path join.
    # An ABSOLUTE value would make pathlib discard the directory on the left
    # (`base / "/etc/passwd.json"` is `/etc/passwd.json`), so anything outside
    # the registered agent grammar is reported and treated as unbound.
    if not is_registered_agent_name(bound):
        print(f"  bound agent: ⚠️  {render._safe_display(bound)} is not a valid agent name")
        issues.append("configured kiro_agent is not a valid agent name")
        bound = "kirocrew"

    default_spec = agents_dir / AGENT_FILENAME
    default_model, default_readable = _spec_model(default_spec)
    if not default_readable:
        print(f"  user spec:   ⚠️  unreadable ({default_spec})")
        issues.append("agent spec unreadable")

    bound_model = ""
    bound_spec: Path | None = None
    bound_spec_missing = False
    if bound != "kirocrew":
        # Display only, through the same resolver the writers use, so the path
        # shown is the file that holds the agent -- whichever form (``.json``
        # or ``.md``) and whichever filename declares the name -- rather than a
        # ``.json`` join that names a file a markdown agent does not have.
        try:
            bound_spec = agent_spec_path(bound, agents_dir=agents_dir)
        except ValueError:
            # Two safe specs declare the name, so no single file IS the bound
            # spec; the model resolver below refuses for the same reason and
            # its tier shows as deferring.
            bound_spec = None
        bound_spec_missing = bound_spec is None
        # Read through the resolver's own accessor: it matches on the spec's
        # ``name`` field as well as the filename, which a bare path join misses.
        try:
            bound_model = normalize_agent_model(cfg._resolve_named_agent_model(bound))
        except Exception:  # noqa: BLE001
            bound_model = ""

    # Labelled in resolve_effective_model's own order. Tier 2 is present only
    # when it applies, so the list never shows a tier the resolver skipped.
    tiers: list[tuple[str, str]] = [("agent override", override)]
    if bound != "kirocrew":
        tiers.append((f"bound agent pin ({render._safe_display(bound)})", bound_model))
    tiers.append(("global agent.model", normalize_agent_model(cfg.agent.model)))
    tiers.append(("default spec pin", default_model))

    # Label and value come out of the SAME tier by construction; a second lookup
    # for the value could be filtered differently and mis-attribute the decision.
    decided = next(((label, value) for label, value in tiers if value), None)
    if decided is not None:
        decided_by, decided_value = decided
    else:
        decided_by = "bundled defaults.json"
        # Nothing pinned anything, so the bundled default answered and the
        # resolver's value is legitimately ours -- unless a spec read was
        # REFUSED, in which case the resolver may have followed a link this
        # report would not, and adopting its answer would hide exactly that.
        decided_value = effective if default_readable else ""

    print(
        f"  effective:   {render._safe_display(effective) if effective else 'auto (backend picks)'}"
    )
    print(f"  decided by:  {decided_by}")
    for label, value in tiers:
        print(f"    {label + ':':<26} {render._safe_display(value) if value else '(defers)'}")
    print(f"  spec file:   {render._safe_display(str(default_spec))}")
    if bound_spec is not None:
        print(f"  bound spec:  {render._safe_display(str(bound_spec))}")
    elif bound_spec_missing:
        print(
            f"  bound spec:  ⚠️  no spec for {render._safe_display(bound)} under {render._safe_display(str(agents_dir))}"
        )

    # Self-check: the marked tier must be what the resolver actually returned.
    if decided_value != effective:
        if not default_readable:
            # Not drift. The resolver reads the spec through its own path, which
            # FOLLOWS a symlink, while this report refuses to; so it can resolve
            # a value this section declined to attribute. Say that, rather than
            # accusing the tier list of being stale.
            print(
                "  ⚠️  the resolver read a spec this report refused to follow, so the "
                "deciding tier above is not attributed"
            )
        else:
            print(
                f"  ⚠️  this report says {decided_value!r} but the resolver returned "
                f"{effective!r} — the precedence shown here is out of date"
            )
            issues.append("doctor model precedence disagrees with the resolver")

    # Which spec is actually deciding, so the tracking state and the repair below
    # describe THAT agent rather than always the built-in one.
    if decided_by.startswith("bound agent pin"):
        pinned_agent, pinned_value = bound, bound_model
    elif decided_by == "default spec pin":
        pinned_agent, pinned_value = "kirocrew", default_model
    else:
        pinned_agent, pinned_value = bound, ""

    try:
        managed = agent_state.get_model_managed(pinned_agent)
    except Exception:  # noqa: BLE001 -- an unreadable sidecar is not fatal here
        managed = None
    if managed is None:
        tracking = "not recorded"
    else:
        tracking = "shipped default" if managed else "frozen (explicit pick)"
    print(f"  tracking:    {tracking} ({render._safe_display(pinned_agent)})")

    # kiro-cli resolves --agent against <project>/.kiro/agents FIRST, with no
    # upward walk, and Kiro Crew's own resolver never reads that directory. So a
    # project-local spec can decide what actually RUNS while every Kiro Crew
    # surface reports something else -- worth naming even though it is rare.
    # *project_dir* is the caller's already-resolved value (env, else the saved
    # project_dir file), so this agrees with the Project section above.
    if project_dir:
        # Resolved the way kiro-cli itself resolves --agent, via the existing
        # helper: the DECLARED name wins and the filename is only the fallback,
        # so a project spec that declares this agent under some other filename is
        # still found. Matching on `<bound>.json` alone would miss exactly that
        # and under-report the shadow.
        proj_spec = next(
            (
                p
                for p in project_agent_files(project_dir, operation="doctor", source="cli")
                if project_agent_name(p) == bound
            ),
            None,
        )
        if proj_spec is not None:
            proj_model, proj_usable = _spec_model(proj_spec)
            if proj_model:
                shown = render._safe_display(proj_model)
            elif not proj_usable:
                shown = "(unreadable)"
            else:
                shown = "(no model)"
            print(f"  project spec: ⚠️  {render._safe_display(str(proj_spec))} -> {shown}")
            print("                kiro-cli loads this one first; not read above")
            issues.append("project-local agent spec shadows the user-level one")

    if pinned_value:
        # pinned_agent is either the literal "kirocrew" or a configured kiro
        # agent name; the flag form is only emitted for a name that matched a
        # spec file on disk, so it is a real agent rather than free text.
        # The name is escaped like every other value read out of config: a
        # control-bearing kiro_agent would otherwise reach the terminal on the
        # one line the user is most likely to copy and run.
        flag = (
            "" if pinned_agent == "kirocrew" else f" --agent {render._safe_display(pinned_agent)}"
        )
        global_shown = render._safe_display(cfg.agent.model) if cfg.agent.model else "unset"
        print(f"  ⚠️  the spec pin decides because the global is {global_shown}")
        print(f"      Fix: kirocrew agent reset-model{flag}   (clears the pin, tracks the default)")


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
# ``kirocrew-guide`` is always on but is granted per tool
# (``agent._GUIDE_AUTO_GRANTS``); a whole-server grant would also pre-approve any
# tool added to it later, before anyone reviewed it.
_NO_BLANKET_ALLOW_MCPS = frozenset({CU_MCP_SERVER, "kirocrew-guide"}) | frozenset(_OPT_IN_MCPS)


def _strict_agent_json_specs(directory: Path) -> list[Path]:
    """Enumerate real spec candidates while preserving directory-read failures."""
    try:
        with os.scandir(directory) as entries:
            return sorted(
                (
                    Path(entry.path)
                    for entry in entries
                    if is_agent_spec_name(entry.name) and not entry.name.startswith("._")
                ),
                key=lambda path: path.stem,
            )
    except (FileNotFoundError, NotADirectoryError):
        return []


def _agent_spec_model_problems(
    agents_dir: Path | None = None,
    project_dir: str | Path | None = None,
    provider: str = "acp",
) -> list[tuple[str, str, str]] | None:
    """Agent specs whose ``model`` names a model kiro-cli does not serve.

    Returns ``(agent name, pinned value, correct id)`` for each spec the registry
    can positively correct, an EMPTY list when every pin checked out, or ``None``
    when the check could not run at all. That third state is deliberate: a
    diagnostic that reports green for a check it never performed is worse than
    one that admits it could not look, which is the whole failure class this
    audit exists to close.

    *project_dir* is forwarded so project-scoped specs are audited too. A project
    spec SHADOWS a user-level agent of the same name, so a global-only scan can
    miss the exact spec a session in that project runs.

    Read through the hardened spec reader rather than opening files here, so a
    spec symlinked at something sensitive is refused the same way every other
    consumer refuses it.

    Reports only ids the registry recognizes under a different spelling. An
    unrecognized id is deliberately NOT reported: a real-but-unregistered id (a
    regional profile, or a model newer than this build's registry) is
    legitimate, and entitlement cannot be judged offline at all — that needs a
    live session's advertised set.
    """
    # The retained claude_code seam accepts its own registered wire ids. The
    # correction below is specifically an ACP/kiro-cli spelling audit.
    if is_claude_code(provider):
        return []

    problems: list[tuple[str, str, str]] = []
    try:
        global_dir = agents_dir or _agents_dir()
        global_specs = _strict_agent_json_specs(global_dir)
        if project_dir:
            if is_sensitive_path(str(project_dir)):
                return None
            project_specs = _strict_agent_json_specs(project_agents_dir(project_dir))
        else:
            project_specs = []

        # Normal discovery deliberately skips malformed or denied specs so one
        # bad file cannot break the agent picker. Doctor has the opposite
        # contract: a skipped candidate makes the audit incomplete, so read each
        # candidate directly through discovery's hardened reader and fail the
        # check to UNKNOWN when any one is refused.
        for path, project_scoped in (
            *((path, False) for path in global_specs),
            *((path, True) for path in project_specs),
        ):
            data = _read_agent_spec(path, operation="doctor", source="cli")
            if data is None:
                return None
            model = normalize_agent_model(data.get("model"))
            correction = acp_id_correction(model)
            if not correction:
                continue
            if project_scoped:
                raw_name = data.get("name")
                name = raw_name if isinstance(raw_name, str) and raw_name else path.stem
            else:
                raw_name = data.get("name")
                name = raw_name if isinstance(raw_name, str) else path.stem
            problems.append((name, model, correction))
    except Exception:
        return None
    return problems


def _format_model_pin_problem(name: str, pin: str, correction: str) -> tuple[str, str]:
    """The two report lines for one unusable pin.

    Every field is repr'd, including the NAME: all three come from an agent
    spec's own contents, so a planted or packaged spec could otherwise carry
    terminal control sequences (cursor moves, screen clears, OSC) and rewrite or
    hide this report. ``repr`` escapes every control character, and is what the
    pin and correction already relied on.

    Separated from the printing so the escaping is a testable contract rather
    than a property of how far ``doctor()`` happens to get.
    """
    return (
        f"  model pin:   ❌ {name!r}: {pin!r} is not a model kiro-cli serves",
        f"                  the registry maps that spelling to {correction!r}",
    )


def _spec_gate_closed(name: str) -> bool:
    """Whether *name*'s spec-emission gate reports CLOSED right now.

    Spec emission consults each managed server's ``spec_gate``
    (``agent._MANAGED_MCP_SERVERS``): a closed gate means the ``mcpServers``
    entry is deliberately omitted from every emitted spec — and retracted from
    an existing one on refresh — so on such a host the entry's absence is the
    HEALTHY state, not a broken install. Doctor's static checks must consult
    the same predicate or the two sides drift apart, producing the unfixable
    "missing from mcpServers (re-run `kirocrew setup`)" loop on every host
    where the gate is closed. Resolving the gate through the registry
    keeps them pinned together: a future server gaining a gate needs no edit
    here, and a server without one reports open, exactly as emission treats it.

    The ``except`` covers gate-CONTRACT failures only — a registry entry that
    is not a dict, or a gate callable that raises past its own handling. For
    those, the fail direction is deliberately the OPPOSITE of emission's
    ``agent._gated_off_servers()``: there, a gate that raises is treated as
    closed, because the open position hands out a backend the operator may not
    want running; here it reports NOT closed, because "closed" is what
    silences the missing-entry error. Each side fails toward its own safe
    state. Note the scope honestly: the shipped computer-use gate catches its
    own internal errors and ANSWERS ``False`` (its documented fail-closed
    posture — an unreadable keystone must never hand out the desktop), so an
    unreadable keystone is indistinguishable from policy-closed through the
    boolean, by the gate's own design. That answer is still the
    emission-CONSISTENT one to report: in that state the entry genuinely is
    omitted from every emitted spec, so the ℹ️ line describes what the system
    actually does, even when the underlying cause is a broken enable-state
    read rather than a decision.

    Never loads a native driver: the computer-use gate reads only the enable
    keystone and platform flags (see ``agent._computer_use_spec_gate``), which
    is what makes it safe to evaluate on doctor's diagnostic path.
    """
    try:
        spec = _agent._MANAGED_MCP_SERVERS.get(name) or {}
        gate = spec.get("spec_gate")
        if gate is None:
            return False
        return not gate()
    except Exception:
        logger.debug("spec gate for %s unreadable; doctor treats it as open", name, exc_info=True)
        return False


def _doctor_gated_off_mcps() -> frozenset[str]:
    """Doctor's per-run snapshot of managed servers whose spec gate is closed.

    Evaluated ONCE per doctor run and threaded into both MCP sections, for the
    same reason ``agent._gated_off_servers()`` snapshots once per rebuild: the
    reads are cheap, agreeing is the point. A keystone flip landing between
    the `MCP Tools` and `MCP Governance` sections would otherwise produce a
    self-contradicting report — one saying "gated off by design", the other
    "markers missing — re-run `kirocrew setup --agent-only`". Not reused from
    ``_gated_off_servers()`` itself because the two snapshots fail in opposite
    directions on an unreadable gate (see :func:`_spec_gate_closed`).
    """
    return frozenset(name for name in _MANAGED_MCPS if _spec_gate_closed(name))


def _doctor_mcp_tools(
    agent_path: Path, issues: list[str], *, gated_off: "frozenset[str] | None" = None
) -> None:
    """Render the `MCP Tools` section of `kirocrew doctor`.

    Two passes scoped to the managed servers (`kirocrew-core`,
    `kirocrew-cron`, `kirocrew-computer`):

    1. Static coherence check of the agent config: each always-on server whose
       ``spec_gate`` is open — or that has no gate — must be present in
       ``mcpServers`` and ``tools``. A gated-off server (feature disabled, or
       no driver for this platform) is deliberately absent from every emitted
       spec, so its absence is reported as informational, never as an issue —
       and a stale entry left from when the gate was open is neither mounted
       into ``tools`` nor probed (see :func:`_spec_gate_closed`). Missing
       ``tools`` entries — and ``allowedTools`` entries for every server
       outside :data:`_NO_BLANKET_ALLOW_MCPS` — are auto-appended and the file
       is rewritten atomically; an instance that must not own the shared agent
       home writes nothing and reports the repairs as issues. A missing ``mcpServers`` entry cannot be
       auto-added because the command path is install-specific.
    2. Live handshake probe via :func:`mcp_discovery.probe_server`. Reports
       per-server status with tool count on success, and on failure shows
       the error head plus any captured stderr tail from the child — which
       usually contains the real cause (FindupException, ImportError, etc.)
       that would otherwise only exist in kiro-cli's per-session log.

    A spec that cannot be read as a JSON object — unreadable, unparseable,
    or valid JSON that is not an object — degrades to an empty config: every
    managed server then reports as missing and the file is never rewritten.
    """
    try:
        agent_data = user_json.loads_user_json(agent_path.read_text(encoding="utf-8"))
    except Exception:
        agent_data = {}
    if not isinstance(agent_data, dict):
        # Valid JSON that is not an object (a list, a scalar) parses fine but
        # every .get() below would raise. Doctor exists to diagnose a broken
        # config, not die on one — treat it like the unparseable case, but say
        # what is actually wrong so the missing-server lines below make sense.
        print("  ❌ agent spec is not a JSON object — re-run `kirocrew setup`")
        agent_data = {}

    tools = agent_data.get("tools", [])
    allowed = agent_data.get("allowedTools", [])
    mcps = agent_data.get("mcpServers", {})
    config_changed = False
    # Read-only probe (no SEL write): a declined instance never writes the shared spec.
    declined = _agent._decline_shared_agent_home(audit=False) is not None

    probe_targets = []
    if gated_off is None:
        gated_off = _doctor_gated_off_mcps()
    for name in _MANAGED_MCPS:
        ref = f"@{name}"
        gate_closed = name in gated_off
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
            if gate_closed:
                # Spec emission consults this same gate and deliberately omits
                # the entry, so absence is the healthy state here — the hard
                # error below would be unfixable ("re-run setup" writes the
                # same gated spec back). Informational, never an issue. A stale
                # `@ref` in ``tools`` is NOT the opt-in "half a grant" warning:
                # emission deliberately leaves the ref alone when it retracts
                # the entry (a dangling ref mounts nothing, and dropping it
                # would destroy a grant the user may have narrowed by hand), so
                # ref-present-entry-absent is the designed steady state on a
                # gated-off host and advising "add the server entry" would
                # defeat the gate. No governance-ceiling revoke is needed on
                # this path either: with no ``mcpServers`` entry kiro-cli has
                # nothing to launch, so a leftover ``allowedTools`` ref cannot
                # auto-approve anything — the stale-ENTRY branch below is the
                # one window where a grant is live, and the revoke runs there.
                print(
                    f"  {ref}: ℹ️  gated off on this host (feature disabled or "
                    "no driver for this platform) — absent from mcpServers by design"
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
        if gate_closed:
            # A stale entry from before the gate closed (feature turned off, or
            # a config copied from a host that has a driver). The next config
            # refresh retracts it; until then doctor must not deepen the hole:
            # no mounting the ref (kiro-cli would spawn a backend emission
            # decided against), no minting `allowedTools`, no probe (nothing
            # SHOULD launch). The governance-ceiling revoke below still runs —
            # the entry is live in this spec until the retraction, so an
            # auto-approve exemption would be real for exactly that window.
            print(
                f"  {ref}: ℹ️  gated off on this host (feature disabled or no "
                "driver for this platform) — stale mcpServers entry is "
                "retracted on the next `kirocrew setup` or gateway start"
            )
        elif ref not in tools and name not in _OPT_IN_MCPS:
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
        # A grant is the bare ref or any per-tool ``@server/tool`` spelling of it.
        granted = any(mcp_ref_owned_by(t, (name,)) for t in allowed)
        if not may_skip_gate_now(ref) and granted and declined:
            config_changed = True
            issues.append(
                f"{ref} auto-approve forbidden by ceiling (repair from the owning install)"
            )
        elif not may_skip_gate_now(ref):
            # REVOKE, not merely "do not add". A grant can predate the ceiling —
            # the policy arrives on a host whose config was written while it was
            # ungoverned — and leaving it in place means the ceiling applies only
            # to installs that were governed before their first launch. Every
            # other writer of this list revokes here too (agent.py's shared sync,
            # both dashboard enable paths); declining to MINT without also
            # revoking would leave `kirocrew doctor` reporting a repaired config
            # that still carried the exemption.
            #
            # This is the one case where doctor removes something from
            # `allowedTools`: the note below about never removing a user's
            # decision holds for user preference, and a ceiling is not one.
            if granted:
                allowed[:] = without_mcp_refs(allowed, (name,))
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
        elif ref not in allowed and name not in _NO_BLANKET_ALLOW_MCPS and not gate_closed:
            # Computer use is never blanket-allowed here: see _NO_BLANKET_ALLOW_MCPS.
            # A pre-existing user-made grant is left alone (doctor never REMOVES a
            # decision the user owns); doctor simply never mints one. A gated-off
            # server never gets one minted either: granting auto-approve to a
            # server emission has decided against is the wrong direction.
            allowed.append(ref)
            config_changed = True

        if gate_closed:
            # Nothing should launch: no emitted spec defines this server, so a
            # handshake probe would spawn a backend for a capability that is off
            # or has no driver here — and report its result either way.
            continue
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
        try:  # both verdicts on the shared spec are audited; the audit must not break doctor
            sel().log_api_access(
                caller="system",
                operation="agent_home_write",
                outcome="denied" if declined else "allowed",
                source="cli_doctor",
                resources=str(agent_path),
            )
        except Exception:  # noqa: BLE001
            logger.debug("SEL audit unavailable for doctor spec write", exc_info=True)
    if config_changed and declined:
        print("  → Auto-fix skipped: shared home")
        issues.append("agent config (auto-fix skipped: shared home)")
    elif config_changed:
        agent_data["tools"] = tools
        agent_data["allowedTools"] = allowed
        agent_data["mcpServers"] = mcps
        atomic_write(agent_path, json.dumps(agent_data, indent=2) + "\n")
        print("  → Auto-fixed agent config")

    if not probe_targets:
        return

    print("  MCP host probe — session tool loading is not verified by this check.")

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
def _doctor_mcp_governance(
    agent_path: Path, issues: list[str], *, gated_off: "frozenset[str] | None" = None
) -> None:
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

    # Same hardened reader as this file's other spec reads: the agents dir is
    # user-writable, so an oversized or sensitively-symlinked spec is refused
    # (and audited) rather than parsed. No try/except: the reader's contract is
    # return-``None``-never-raise, which the sibling sites also rely on bare.
    # ``None`` degrades to no declared servers, which is what a blanket
    # ``except`` here would do.
    spec = _read_agent_spec(agent_path, operation="doctor", source="cli")
    servers = (spec or {}).get("mcpServers") or {}
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
    # exact failure this section exists to catch. One exception, same rule as
    # the MCP Tools section above: an always-on server whose spec gate is
    # closed is deliberately absent from every emitted spec, so demanding a
    # registry marker for it would re-create the unfixable "re-run setup" loop.
    # A STALE entry still counts while it exists — kiro-cli drops an
    # unmarked entry at session assembly, so the marker matters for exactly as
    # long as the entry does.
    if gated_off is None:
        gated_off = _doctor_gated_off_mcps()
    expected = [
        name
        for name in _ALWAYS_ON_MCPS
        if isinstance(servers.get(name), dict) or name not in gated_off
    ] + [name for name in _OPT_IN_MCPS if isinstance(servers.get(name), dict)]
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


def _kiro_cli_signed_in() -> bool | None:
    """Whether the HOST identity store holds a credential. ``None`` when unknown.

    Three-valued on purpose, mirroring the install probe: a spawn that failed says
    nothing about the store, and reporting that as signed-out would tell an
    operator to re-run a login they already completed.

    Absent binary is ``None`` rather than ``False`` for the same reason -- the
    kiro-cli row above already reports the install, and "not signed in" would send
    someone to ``kiro-cli login`` before there is a ``kiro-cli`` to run it.
    """
    binary = resolve_kiro_cli()
    if not binary:
        return None
    # The same binary the gateway spawns: the desktop app's bundled copy ranks
    # above PATH, so ``shutil.which`` could name a copy no session runs.
    try:
        result = subprocess.run(  # noqa: S603 - argv list, no shell, local binary
            [binary, "whoami"],
            capture_output=True,
            timeout=10,
            **UTF8_TEXT,
        )
    except Exception:
        return None
    return result.returncode == 0


def _linger_enabled(user: str) -> bool | None:
    """Whether ``user``'s systemd instance lingers past logout.

    ``None`` when it cannot be determined (no ``loginctl``, unknown user, or an
    unrecognised value) so the caller can stay quiet rather than guess. Thin
    delegate to the package's one linger probe,
    :func:`kiro_crew.service.linux._linger_enabled`, so the ``loginctl`` argv and
    its parsing live in a single place.
    """
    return service_linux._linger_enabled(user)


def _git_line(repo: Path, *args: str) -> str | None:
    """First stdout line of ``git -C repo *args``, ``None`` on any failure.

    A module-level seam (not inlined) so tests can drive the checkout probe
    without a real repository. Failures are expected states here — a tarball
    install has no ``.git``, a fresh clone may lack ``origin/HEAD`` — so every
    error collapses to ``None`` and the caller renders "could not check".

    ``git`` is resolved through :func:`platform_compat.trusted_git_bin` rather
    than a bare ``PATH`` lookup: doctor runs with operator privileges, and an
    agent-writable directory leading ``PATH`` could plant a ``git`` shim. That
    helper carries the Windows install-root fallback; a miss collapses to
    ``None`` like every other failure here — no spawn at all.
    """
    git = platform_compat.trusted_git_bin()
    if git is None:
        return None
    try:
        res = subprocess.run(
            [git, "-C", str(repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    return res.stdout.strip().splitlines()[0].strip() if res.stdout.strip() else None


# Userspace OOM killers doctor knows how to detect, in probe order:
# systemd-oomd ships with systemd (the common case), earlyoom is the usual
# add-on daemon.
_OOM_KILLER_UNITS = ("systemd-oomd", "earlyoom")


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
                encoding="utf-8",
                errors="replace",
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            determined = False
            continue
        if res.returncode == 0 and res.stdout.strip() == "active":
            return unit
    return False if determined else None


def _gateway_rss_bytes(pid: int) -> int | None:
    """Resident set size of *pid* in bytes, or None when no route can read it.

    ``platform_compat.proc_rss_bytes_for_pid`` serves Linux (``/proc``) and
    Windows (``GetProcessMemoryInfo``) but has no ctypes-only path on macOS and
    answers None there, so this falls through to ``ps -o rss=`` resolved via
    ``trusted_system_bin`` (ps reports KiB). Without the fallback the doctor
    line reads "RSS unreadable" on every Mac.
    """
    rss = platform_compat.proc_rss_bytes_for_pid(pid)
    if rss is not None or platform_compat.IS_WINDOWS:
        return rss
    ps_bin = platform_compat.trusted_system_bin("ps")
    if ps_bin is None:
        return None
    try:
        out = subprocess.check_output([ps_bin, "-o", "rss=", "-p", str(pid)], timeout=2)
        return int(out.decode().strip()) * 1024
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


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


def _aws_probe_env() -> dict[str, str]:
    """Child environment for the ``aws configure`` probes.

    Drops the two variables that relocate the CLI's files. This section decides
    WHETHER to report from ``~/.aws`` existence but asks the CLI for the profile
    NAMES, and a subprocess inherits the environment — so with
    ``AWS_CONFIG_FILE`` / ``AWS_SHARED_CREDENTIALS_FILE`` set, the two halves
    describe DIFFERENT files. That is not hypothetical: the agent sandbox points
    both at a per-session directory, so ``doctor`` run from such a shell reported
    a profile that is absent from the operator's own config while its own probe
    said that config file does not exist. Dropping them makes the CLI resolve the
    same default locations the existence probe checks, so the halves cannot
    disagree. Everything else is inherited — ``PATH`` still has to work.
    """
    env = dict(os.environ)
    for relocator in ("AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE"):
        env.pop(relocator, None)
    return env


def _aws_profile_names() -> list[str] | None:
    """Profiles as the SANCTIONED path reports them. ``None`` = could not ask.

    Deliberately NOT a parse of ``~/.aws/config``. That file sits inside a
    directory ``security._SENSITIVE_HOME_DIRS`` fences from the agent, and
    ``kirocrew doctor`` is reachable from a tool call — so opening it here would
    hand back through a diagnostic exactly what the floor refuses directly,
    which is the "just use a different reader" move this whole feature exists to
    talk the agent out of. ``aws configure list-profiles`` is the command the
    remediation text names and ``test_deny_guidance`` pins as allowed, so the
    report now comes through the same door the guidance points at.

    ``None`` rather than ``[]`` when the CLI is absent or fails, because "cannot
    ask" and "asked, and there are none" are different things to tell an
    operator.

    Resolved through :func:`platform_compat.trusted_aws_bin` rather than ``PATH``: a gateway's
    ``PATH`` can lead with a directory the agent itself can write (a worktree
    venv's ``bin``), and this runs when an OPERATOR types ``kirocrew doctor`` —
    outside the agent's sandbox. A miss degrades to the same "cannot ask" answer
    as an absent CLI, which is the honest reading either way.
    """
    aws_bin = platform_compat.trusted_aws_bin()
    if not aws_bin:
        return None
    try:
        proc = subprocess.run(
            [aws_bin, "configure", "list-profiles"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_AWS_PROBE_TIMEOUT_SECS,
            env=_aws_probe_env(),
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    names: list[str] = []
    for line in (proc.stdout or "").splitlines():
        name = line.strip()
        if name and name not in names:
            names.append(name)
    return names


def _aws_auto_refreshes() -> bool | None:
    """Whether the profile the agent will ACTUALLY use auto-refreshes.

    Asked of the CLI rather than by looking for the string ``credential_process``
    somewhere in the config file — that substring test answered "yes" when the
    key belonged to any other profile, so the effective-profile question is both
    more accurate and reachable without a fenced read. One invocation, resolving
    the same default profile the agent's own AWS calls will resolve.

    ``None`` rather than ``False`` when there is no CLI to ask, for the same
    reason :func:`_aws_profile_names` returns it: "asked, and there is no
    ``credential_process``" is a finding, while "could not ask" is not, and
    collapsing them made the report tell an operator their credentials may expire
    mid-task on the strength of a question nobody put.

    Resolved through :func:`platform_compat.trusted_aws_bin` for the same reason as the profile
    probe: this runs under an operator's ``kirocrew doctor``, and a ``PATH`` that
    leads with an agent-writable directory would let a planted shim answer.
    """
    aws_bin = platform_compat.trusted_aws_bin()
    if not aws_bin:
        return None
    try:
        proc = subprocess.run(
            [aws_bin, "configure", "get", "credential_process"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_AWS_PROBE_TIMEOUT_SECS,
            env=_aws_probe_env(),
        )
    except Exception:
        # The CLI resolved but could not be run (timeout, OS error) — still
        # "could not ask", not an answered "no".
        return None
    return proc.returncode == 0 and bool((proc.stdout or "").strip())


def _credential_vendor_line() -> str:
    """The edition's credential-vending MCP servers, or "" when there are none.

    Phrased for the OPERATOR, not reused from the agent's refusal hint: that hint
    tells its reader to prefer the vendor and says it supersedes "the guidance
    above", neither of which is true for a human reading a terminal. Only the
    server ids are shared with the refusal path.

    Runs the capability-manager lookup on its own event loop because ``doctor`` is
    synchronous. Degrades to "" on any failure — including an already-running loop
    — since the absence of this line is indistinguishable from the public
    edition's normal state and must never fail the run.
    """
    from kiro_crew.doctor_checks import render

    try:
        manager = platform_context.safe_context_call(
            lambda: platform_context.current_context().capability_manager,
            fallback_factory=lambda: bind_capability_manager(DefaultCapabilityManager()),
            log_message=None,
        )
        if not manager.available():
            return ""
        ids = credential_vendor_server_ids(asyncio.run(manager.list_mcp()))
        if not ids:
            return ""
        listed = ", ".join(render._safe_display(name) for name in ids)
        return (
            f"agents mint credentials through {listed} rather than reading these "
            "files, so the files being unreadable to them is expected, not a fault."
        )
    except Exception:
        return ""


#: Bare flag name (no leading dashes) used to tell "this kiro-cli predates engine
#: selection" apart from "it offers engines but not ours". Derived from the
#: transport constant so the two can never drift.
_KAS_ENGINE_FLAG_NAME = KAS_RELAY_ENGINE_FLAG.lstrip("-")


def _kas_relay_help(binary: str) -> str | None:
    """``acp --help`` text for this kiro-cli, or ``None`` when the probe FAILED.

    Read from help output because there is no machine-readable capability surface
    for the engine selector. ``None`` means only one thing — the probe could not
    run (spawn error, timeout) — so the caller reports genuinely-unknown as
    unknown. Help text that RAN and simply lacks the engine selector is returned
    as-is, not as ``None``: a kiro-cli too old to offer ``--agent-engine`` cannot
    serve KAS at all, and reporting that as "unknown" would let a broken
    configuration pass the readiness check and fail later at spawn instead.

    Local binary, argv list, no shell, no credential involved.
    """
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, no shell, local binary
            [binary, "acp", "--help"],
            capture_output=True,
            timeout=15,
            check=False,
            # Pinned UTF-8 rather than bare text=True: help output is decoded
            # here, and a platform-locale decode could mangle the flag name this
            # probe searches for and report a supported kiro-cli as unreadable.
            **UTF8_TEXT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return f"{proc.stdout}\n{proc.stderr}"


def _doctor_kas(issues: list[str]) -> None:
    """Report KAS backend readiness, but only when KAS is the selected backend.

    KAS is opt-in (``agent.acp_backend = "kas"``); when it is not selected this
    is silent so a kiro-cli / Claude Code install sees no KAS noise. When it IS
    selected, KAS is served by kiro-cli's own ACP relay (see
    :mod:`kiro_crew.acp.kas_transport`), so the thing that makes a selected KAS
    backend fail at session-create time is a kiro-cli whose ``acp`` subcommand
    cannot select the KAS engine. The only credential read here is Crew's own
    vault -- the same read the runtime makes to pick the spawn's auth owner:
    the relay resolves tokens from the vault when it holds a usable identity
    and from kiro-cli's own store otherwise, and the sign-in rows above report
    that same decision.
    """
    # Positive backend test (not ``!= ACP_BACKEND_KAS``): an inequality would
    # silently capture every harness added later — see the harness-parity gate.
    if KiroCrewConfig.load().agent.acp_backend == ACP_BACKEND_KAS:
        _report_kas_backend(issues)


def _report_kas_backend(issues: list[str]) -> None:
    """Print the KAS diagnostic block (relay binary + engine support).

    Split from :func:`_doctor_kas` so the backend-selection check there stays a
    positive ``== ACP_BACKEND_KAS`` rather than an early-return on inequality.
    """
    print("\nKAS backend")
    binary = resolve_kiro_cli()
    if not binary:
        print(f"  relay:       ❌ {KIRO_CLI_BIN} not found")
        print("               Fix: install kiro-cli; it serves KAS over its acp relay.")
        issues.append("KAS backend selected but kiro-cli is not installed")
        return

    # Same decision the runtime makes at spawn: Crew owns auth when its own
    # vault holds an identity, kiro-cli otherwise. Reported so the operator sees
    # which credential the next KAS process will actually draw on. Deferred
    # import: this module is on the dashboard's boot path and kiro_crew.auth
    # brings the cryptography wheel with it (see kas_host_auth's module doc).
    # An import or probe failure leaves the diagnostic on the kiro-cli path
    # rather than ending the whole doctor report.
    try:
        from kiro_crew.auth.bridge import describe_vault_identity, vault_holds_identity

        host_auth = vault_holds_identity()
        identity_line = describe_vault_identity()
    except Exception:
        host_auth = False
        identity_line = None

    print(f"  relay:       ✅ {' '.join(build_kas_argv(binary, host_auth=host_auth))}")
    print(
        "  auth owner:  "
        + (
            "Kiro Crew vault (signed in through Kiro Crew)"
            if host_auth
            else "kiro-cli credential store (--auth-method cli)"
        )
    )
    # The fields the owner decision reads, so a vault that will fail its first
    # callback (expired, nothing to renew it) is visible here rather than as a
    # broken spawn. Printed whenever something is stored, including the case the
    # probe rejected -- that is exactly the one worth seeing.
    if identity_line:
        print(f"  crew vault:  {identity_line}")
    # Before the help probe, not after: the row reads ``--version``, which is a
    # different spawn from ``acp --help``, so a failed help probe establishes
    # nothing about it. The early ``return`` below is for the ENGINE rows alone;
    # letting it swallow this row would hide a withheld auto-approve on exactly
    # the host where kiro-cli is misbehaving.
    _report_kas_spec_permissions(issues)
    help_text = _kas_relay_help(binary)
    if help_text is None:
        # The probe itself failed, so nothing is known either way. Advisory: a
        # diagnostic must not invent a verdict it could not establish.
        print("  engine:      ⚠️  could not read `acp --help`; engine support unknown")
        return
    # Two distinct failures, both definite: the flag is absent entirely (a
    # kiro-cli predating engine selection) or it is present without this engine.
    if f"--{_KAS_ENGINE_FLAG_NAME}" not in help_text:
        print(f"  engine:      ❌ this kiro-cli has no --{_KAS_ENGINE_FLAG_NAME} flag")
        print("               Fix: update kiro-cli, or switch agent.acp_backend to kiro.")
        issues.append(
            f"kiro-cli is too old to select the KAS engine (no --{_KAS_ENGINE_FLAG_NAME})"
        )
    elif KAS_RELAY_ENGINE in help_text:
        print(f"  engine:      ✅ {KAS_RELAY_ENGINE} supported")
    else:
        print(f"  engine:      ❌ this kiro-cli does not offer engine {KAS_RELAY_ENGINE}")
        print("               Fix: update kiro-cli, or switch agent.acp_backend to kiro.")
        issues.append(f"kiro-cli does not support the KAS engine ({KAS_RELAY_ENGINE})")
    # Reported from the SAME decision as the ``auth owner:`` line above, so the
    # two cannot disagree: the relay resolves every access token from whichever
    # store owns the spawn -- Crew's vault when it holds a usable identity,
    # kiro-cli's own store otherwise. The detail for each store lives in the
    # sign-in rows and the ``crew vault:`` line rather than being restated here.
    if host_auth:
        print("  token:       ➖ Kiro Crew vault sign-in (see the auth owner line above)")
    else:
        from kiro_crew.agent_sdk import entitlement_label

        print(
            f"  token:       ➖ {entitlement_label(ACP_BACKEND_KAS)} "
            "(see the sign-in rows above)"
        )


def _report_kas_spec_permissions(issues: list[str]) -> None:
    """Whether this kiro-cli can carry the spec ``permissions`` block KAS reads.

    The block is how Crew's auto-approve list reaches KAS's policy engine, and it
    is written only when the installed kiro-cli accepts the field: that binary
    validates specs with serde ``deny_unknown_fields``, so a release predating the
    field refuses the WHOLE spec and drops every Crew MCP server from the session.
    Withholding it is the smaller loss, but it IS a loss, and this is the only
    place it is visible. Reported inside the KAS block rather than
    beside the model rows because it costs nothing until KAS is the selected
    backend -- which is exactly when this block prints.
    """
    version = installed_kiro_cli_version()
    if spec_permissions_supported(version):
        print("  auto-approve: ✅ spec `permissions` block written (KAS reads it)")
        return
    floor = ".".join(str(part) for part in SPEC_PERMISSIONS_MIN_VERSION)
    if version is None:
        # Not "too old": the version could not be read at all, most often because
        # kiro-cli resolves only through PATH and the probe spawns pinned paths
        # only. The writer withholds a NEW block here but keeps one already on
        # disk, so the remedy is to make the binary probeable, not to update it.
        print("  auto-approve: ⚠️  spec `permissions` block not seeded: kiro-cli version unknown")
        print(f"               ({PATH_ONLY_INSTALL_NOTE}). A block already on disk is kept.")
        issues.append("kiro-cli version unknown, so the KAS `permissions` block is not seeded")
        return
    shown = ".".join(str(part) for part in version)
    print(f"  auto-approve: ❌ withheld: this kiro-cli ({shown}) refuses the field")
    print("               It validates specs with deny_unknown_fields, so writing " "`permissions`")
    print("               would make the whole spec unreadable and drop every Kiro " "Crew MCP")
    print(f"               server. Fix: update kiro-cli to {floor} or newer. If the spec")
    print("               already carries the block, `kirocrew setup --agent-only --clean`")
    print("               rebuilds it without the key.")
    issues.append("kiro-cli is too old to carry the KAS `permissions` block")


def _discord_intent_grants(token: str) -> intent_probe.IntentGrants:
    """Read Discord's privileged-intent grants on a throwaway event loop.

    ``asyncio.run`` gives the probe its own loop: the doctor is a separate
    process from the gateway, so the probe never shares a loop with live
    message traffic. Every failure is already folded into the result by
    :func:`~kiro_crew.discord.intent_probe.probe_intent_grants`; the guard here
    covers the loop itself failing to start, because a diagnostic that raises
    prints no report at all.
    """
    try:
        return asyncio.run(intent_probe.probe_intent_grants(token))
    except Exception as exc:  # noqa: BLE001 - a diagnostic must always answer
        return intent_probe.IntentGrants(error=type(exc).__name__)


def _venv_deps_ok(venv_py: Path) -> bool:
    """True when *venv_py* ITSELF can import the gateway's core dependencies.

    Routed through :func:`dep_sync._probe_interpreter` (``-I -X utf8`` plus a
    neutral ``cwd``) because the question is about the venv, not the process
    asking: an unisolated ``python -c`` puts the doctor's CWD at
    ``sys.path[0]`` and inherits ``PYTHONPATH``, so a decoy package on either
    route makes the check answer for the caller -- reporting the modules
    available in a venv that cannot actually serve them, a false-healthy from
    the diagnostic whose job is to catch exactly that install.
    """
    try:
        # Windows process creation and first-time Defender scans can consume
        # most of a five-second budget when the host is busy (including during
        # the parallel test suite). Keep the probe bounded, but allow enough
        # time for a healthy interpreter to start and import its dependencies.
        proc = dep_sync._probe_interpreter(
            venv_py, "import websockets, slack_sdk, aiohttp", timeout=15
        )
    except Exception:
        return False
    return proc.returncode == 0


def _report_node(issues: list[str]) -> None:
    """Print the ``node:`` line, judging the full version against ``MIN_NODE_VERSION``.

    A Node below the floor is a failure recorded in *issues*: it lacks APIs the
    code needs. The same constant drives the startup warning in ``cli.py``. An
    unreadable version is shown as present rather than guessed at.
    """
    node = shutil.which("node")
    if node:
        try:
            node_ver_result = subprocess.run(
                # The RESOLVED path, as ``cli._node_ok`` does: on Windows ``which``
                # can answer ``node.CMD``, which a bare ``node`` cannot spawn.
                [node, "-v"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
            )
            version = parse_node_version(node_ver_result.stdout)
            if version is None:
                print(f"  node:        ✅ {node}")
            elif node_version_meets_floor(version, MIN_NODE_VERSION):
                print(f"  node:        ✅ {node} (v{format_node_version(version)})")
            else:
                print(
                    f"  node:        ❌ v{format_node_version(version)} < "
                    f"v{format_node_version(MIN_NODE_VERSION)}"
                )
                print(f"               Fix: {node_too_old_message(version, MIN_NODE_VERSION)}")
                issues.append("node")
        except Exception:
            print(f"  node:        ✅ {node}")
    else:
        floor = format_node_version(MIN_NODE_VERSION)
        print(f"  node:        ⚠️  not found (Kiro Crew needs Node v{floor}+)")
        print(
            f"               Fix: install Node.js >= v{floor} (24 LTS recommended) from https://nodejs.org"
        )


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
        print("\n  Open a GitHub issue (then drag the zip in):")
        print(f"  {diagnostics.terminal_issue_url(result)}")
        return

    from kiro_crew.doctor_checks import (
        access,
        agents,
        channels,
        confinement,
        features,
        install,
        mcp,
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

    # ── Runtime tmpfs headroom (sandbox mount-source roots; Linux only) ──
    resources._doctor_runtime_tmpfs(issues)

    # ── kiro-cli installer residue (silent unless residue is on disk) ──
    resources._doctor_cli_installer_residue(issues)

    # ── Cron job health (silent unless a job auto-paused or errored) ──
    # Reads crons.json off disk, not the gateway API: the gateway's own
    # per-job badge and hourly failure re-alert cannot report a wedged gateway.
    workload._doctor_cron_health(issues)

    # ── Durable task queue (silent when no tasks.db exists yet) ──
    workload._doctor_task_store(issues)

    # ── Overload resilience: configured bounds + platform liveness evidence ──
    workload._doctor_overload_resilience(cfg)

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
    services._doctor_gateway_reachability(dashboard, issues)

    # ── Summary ──
    print()
    if issues:
        print(f"❌ Fix these issues: {', '.join(issues)}")
        sys.exit(1)
    else:
        print("✅ Kiro Crew is ready!")


# ── The facade ────────────────────────────────────────────────────────────────
# Every section that no repository gate pins to this file lives in a
# ``kiro_crew.doctor_checks`` family. The names this module held before the
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

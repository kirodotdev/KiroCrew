"""Build the namespace-launcher and Seatbelt argv for one spawn.

Gathers the live host a plan is made from, writes the launcher script or profile into
the run dir, and reclaims stale artifacts there. One of the owners ``kiro_crew.sandbox``
is carved into: every name here stays readable and patchable as
``kiro_crew.sandbox.<name>``.
"""

from __future__ import annotations

import errno
import functools
import logging
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew import (
    platform_compat,
    sandbox_launcher,
    sandbox_mount_sweep,
    sandbox_plan,
    sandbox_seatbelt,
)
from kiro_crew.config.paths import config_dir
from kiro_crew.platform import current_context
from kiro_crew.sandbox_aliases import (
    _referent_identity,
    _refuse_aliased_masked_leaves,
    _refuse_multilinked_credential_leaves,
)
from kiro_crew.sandbox_leaves import (
    _CREW_HIDDEN_DIRS,
    _CREW_HIDDEN_LEAVES,
    _CREW_READONLY_LEAVES,
    _CREW_READONLY_TARGETS,
    _CREW_UNREADABLE_MASK_LEAVES,
    _RELOCATED_CREW_HIDDEN_LEAVES,
    _VOICE_RUNTIME_LEAF,
)
from kiro_crew.sandbox_plan import scrub_prefixes as _agent_scrub_prefixes
from kiro_crew.sandbox_seals import (
    SandboxCeilingUnsealable,
    _materialize_sealable_ceilings,
    _refuse_linked_models_leaf,
)
from kiro_crew.sandbox_staging import (
    _materialize_live_target_mask_target,
    _materialize_maskable_dirs,
    _materialize_md_notebook_mask_targets,
    _md_notebook_degraded_mask_dirs,
    _member_records_move,
    _migrate_md_notebook_legacy_state,
    _reconcile_auth_store_staging_links,
    _sweep_legacy_auth_store_temps,
    _sweep_legacy_md_notebook_temps,
)
from kiro_crew.sandbox_tiers import (
    _AGENT_DENIED_ENV_KEYS,
    _CC_EXPOSE_FILES,
    _CC_FILES,
    _IN_SANDBOX_LEVEL_VAR,
    _IN_SANDBOX_MARKER,
    _POD_OS_HOME_GRANT_STORE_LEAVES,
    _POD_OS_HOME_MASKED_SUBLEAVES,
    _PYTHON_ENV_PREFIXES,
    _SENSITIVE_ENV_PREFIXES,
    _STANDARD_DIRS,
    _crew_home_alias_roots,
    _pod_os_home_targets,
    _relocated_crew_targets,
    _relocated_policy_cache_dirs,
    _resolved_install_root_targets,
    _resolved_kiro_agents_targets,
    _sandbox_policy,
)
from kiro_crew.sandbox_workspace import (
    _literal_ancestor_guards,
    _voice_runtime_ancestor_guards,
    _voice_runtime_parent_paths,
    _voice_runtime_sandbox_paths,
)
from kiro_crew.terminal_safe import safe_terminal_line

if TYPE_CHECKING:
    from collections.abc import Mapping

# Logged under the sandbox's name, which operator log filters, level settings
# and the tests' caplog scopes key on.
logger = logging.getLogger("kiro_crew.sandbox")


# Launcher scripts and seatbelt profiles are read exactly once at child exec.
# Any file older than this threshold is garbage regardless of PID liveness.
_LAUNCHER_MAX_AGE_SECONDS = 3600

#: Artifact families the sweep reclaims, by filename prefix -> accepted
#: suffixes. Every family tags the writing process's PID after the prefix.
_SANDBOX_ARTIFACT_PREFIX = "kirocrew_sandbox_"
#: The two artifacts written under that prefix, each named once. Three readers depend on
#: these agreeing: the ``mkstemp`` calls that write them, the run-dir sweep's accepted
#: suffixes below, and ``session_pid``'s managed-agent gate, which imports both to tell
#: the Linux launcher apart from the macOS seatbelt profile sitting beside it under the
#: same prefix.
_LAUNCHER_SCRIPT_SUFFIX = ".py"
_SEATBELT_PROFILE_SUFFIX = ".sb"
_PI_GATE_ARTIFACT_PREFIX = "kirocrew_pi_gate_"
# Named ONCE because two sweeps accept this family: the run dir still holds artifacts
# written before they moved, and the gate dir holds the current ones. Two spellings could
# drift and leave one of those directories unswept. ``.tmp`` is the mkstemp stage both pi
# artifacts pass through before publication. ``.json`` is the per-session MCP handoff
# (``kirocrew_pi_gate_<pid>_mcp_<nonce>.json``), removed once its session is established;
# the sweep reclaims one a dead gateway left behind.
_PI_GATE_ARTIFACT_SUFFIXES: tuple[str, ...] = (".sh", ".cmd", ".ts", ".tmp", ".json")
_RUN_DIR_ARTIFACTS: dict[str, tuple[str, ...]] = {
    _SANDBOX_ARTIFACT_PREFIX: (_SEATBELT_PROFILE_SUFFIX, _LAUNCHER_SCRIPT_SUFFIX),
    # The run sweep accepts pi artifacts as well as sandbox launchers.
    _PI_GATE_ARTIFACT_PREFIX: _PI_GATE_ARTIFACT_SUFFIXES,
}
# The DeepSeek Harness gate shares the pi-gate leaf: the sealed plugin
# (``kirocrew_dsh_gate_<pid>.mjs``), its per-launch patch
# (``kirocrew_dsh_gate_<pid>.patch.yml``) and the mkstemp stages both are
# renamed from (``kirocrew_dsh_gate_<pid>_*.tmp`` / ``kirocrew_dsh_patch_<pid>_*.tmp``).
# Like the pi artifacts they are written once per gateway process and reused, so
# the PID in the name is the owner's own and liveness, not age, decides staleness.
_DSH_GATE_ARTIFACT_PREFIX = "kirocrew_dsh_gate_"
_DSH_PATCH_ARTIFACT_PREFIX = "kirocrew_dsh_patch_"
_DSH_GATE_ARTIFACT_SUFFIXES: tuple[str, ...] = (".mjs", ".patch.yml", ".tmp")
_PI_GATE_DIR_ARTIFACTS: dict[str, tuple[str, ...]] = {
    _PI_GATE_ARTIFACT_PREFIX: _PI_GATE_ARTIFACT_SUFFIXES,
    _DSH_GATE_ARTIFACT_PREFIX: _DSH_GATE_ARTIFACT_SUFFIXES,
    _DSH_PATCH_ARTIFACT_PREFIX: (".tmp",),
}

# Legacy sandbox launcher directory (before migration to <config_dir>/run/).
_LEGACY_LAUNCHER_DIR = "/tmp"


# ── Backend: Linux namespace sandbox ──


def _resolve_agent_executable(executable: str) -> str:
    """Resolve *executable* through the active edition before sandboxing.

    The public adapter is identity. An edition companion may replace a managed
    launcher with the direct executable it ultimately invokes so Kiro Crew can
    apply exactly one OS-level sandbox. A transient adapter failure degrades to
    the original executable, which preserves the secure behavior: the outer
    sandbox still applies and a launcher that cannot run nested fails closed.
    Platform composition failures always propagate through ``safe_context_call``.
    """
    from kiro_crew.platform import safe_context_call

    return safe_context_call(
        lambda: current_context().agent_executable.resolve_executable(executable),
        fallback=executable,
        log_message="Agent executable resolver failed; using the original executable",
    )


@functools.lru_cache(maxsize=None)
def _ssh_supports_accept_new() -> bool:
    """Return True if the installed ssh supports StrictHostKeyChecking=accept-new (OpenSSH >= 7.6)."""
    try:
        r = subprocess.run(["ssh", "-V"], capture_output=True, timeout=5)
        m = re.search(r"OpenSSH_(\d+)\.(\d+)", r.stderr.decode())
        if m:
            return (int(m.group(1)), int(m.group(2))) >= (7, 6)
    except Exception:
        pass
    return False


def namespace_launcher_script_dir() -> str:
    """The directory :func:`namespace_argv` writes its launcher script into.

    ``<config_dir>/run``, normalized, and named here because two readers need the one
    answer: :func:`_ensure_run_dir`, which creates it, and ``session_pid``'s
    managed-agent gate, which has to recognise a launcher path it did not build. Normalized
    HERE so both sides of that comparison see one spelling: ``mkstemp`` returns an
    ``abspath`` of its ``dir`` on 3.12+, so an uncollapsed path reaches ``/proc``
    collapsed anyway. The case that needs it is the DEFAULT home under a ``HOME``
    containing ``..``; an explicit ``KIROCREW_HOME`` is already ``.resolve()``d by
    ``config.paths``.

    A function rather than a constant because it reads the live data home, and a NAMED one
    rather than two spellings of ``config_dir() / "run"`` because the writer and the
    recogniser have to agree about it exactly. The launcher's other three shape values are
    plain module constants, so the gate imports those directly instead.

    Deliberately only the PREFERRED spelling. ``_ensure_run_dir`` degrades to the system
    tmpdir when this directory cannot be created or chmod'd, and that directory is shared
    with every other user of the host -- accepting it as a launcher location on a KILL
    path would let a path anyone can write decide which process trees are reclaimable.
    RESIDUAL, stated rather than closed: on a host where the crew ``run/`` directory
    cannot be created, a sandboxed agent root is not recognised and its tracking entry is
    retained instead of reaped, exactly as it was before the gate learned this shape.
    Closing that needs identity the tmpdir path cannot supply.

    This is also the only part of the launcher's shape that touches the filesystem:
    ``config_dir()`` creates the data home if it is missing and can raise, so the gate
    asks for it LAST, after every free test on the command line has already matched.
    """
    return os.path.normpath(os.path.abspath(str(config_dir() / "run")))


def _ensure_run_dir() -> str:
    """Create ``<config_dir>/run/`` with mode 0o700, falling back to tmpdir on failure."""
    run_dir = namespace_launcher_script_dir()
    try:
        os.makedirs(run_dir, mode=0o700, exist_ok=True)
        # exist_ok does not re-apply mode on existing dirs — enforce explicitly.
        # 0o700 (owner-only rwx) is deliberately restrictive: this dir holds
        # per-session sandbox launcher scripts and sockets that must NOT be
        # world-readable. Semgrep's 0o644 suggestion is wrong for a directory
        # (needs the execute/traverse bit) and would loosen, not tighten, access.
        os.chmod(run_dir, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # noqa: E501  # fmt: skip
    except OSError:
        # ``gettempdir()`` probes candidate directories for writability, so it is called
        # HERE and not beside the preferred spelling: on a read-only-root container with
        # no writable tmp it raises, and an eager call would have turned every namespace
        # and seatbelt spawn into a failure instead of a degraded one.
        logger.warning("Cannot create %s; falling back to system tmpdir", run_dir)
        run_dir = tempfile.gettempdir()
    return run_dir


# Interpreter flags for the namespace launcher. These matter for CONFINEMENT
# ORDERING, not tidiness: the launcher IS a Python process, and everything the
# interpreter does at startup happens BEFORE the script reaches ``unshare``. With
# site processing enabled, ``site`` executes code from env-derived paths at startup
# -- user-site ``.pth`` files (whose location comes from ``PYTHONUSERBASE``, else
# ``HOME``) and ``sitecustomize`` (from ``PYTHONPATH``). For a config-declared
# server ``env`` block that is externally authorable text, so it is arbitrary code
# running unconfined. No argv[0] pin helps: the interpreter is the pinned binary.
#   -I (isolated) ignores PYTHON* startup vars and drops the script dir from
#      sys.path; implies -E and -s.
#   -S skips ``site`` altogether, which is what closes the class rather than
#      individual keys -- no .pth and no sitecustomize run at all.
# Safe because the generated launcher imports stdlib only (sys, os, stat, struct,
# tempfile, platform, ctypes) and never needs site-packages. The namespace probe
# and spawn shims already start their interpreters with these exact flags; this
# launcher was the one Python entrypoint that did not.
_LAUNCHER_INTERPRETER_FLAGS: tuple[str, ...] = ("-I", "-S")


def _launcher_script_of(launcher_argv: list[str]) -> str:
    """The generated launcher script inside a ``namespace_argv`` result.

    Derived from the flag count rather than hardcoded, so adding a flag cannot
    silently return a flag token where a path is expected — which would both leak
    the tempfile and hand the caller ``"-I"`` to ``unlink``.
    """
    return launcher_argv[1 + len(_LAUNCHER_INTERPRETER_FLAGS)]


def _log_refusals(refusals: tuple[sandbox_plan.Refusal, ...] | list[sandbox_plan.Refusal]) -> None:
    """Log each refusal a plan made, in the order the plan made them."""
    for refusal in refusals:
        logger.warning(refusal.message, *refusal.args)


def _carveout_probes(candidates: tuple[str, ...]) -> dict[str, sandbox_plan.CarveoutProbe]:
    """What this host says about each absolute write carve-out candidate.

    ``realpath`` and ``isdir`` are the only filesystem calls a carve-out decision needs;
    taking them here keeps :func:`kiro_crew.sandbox_plan.plan_confinement` pure. Safe on
    the spawn path: every async caller reaches the builders through
    ``shielded_prepare_off_loop``'s worker thread.
    """
    probes: dict[str, sandbox_plan.CarveoutProbe] = {}
    for raw in candidates:
        if not raw or not os.path.isabs(raw) or raw in probes:
            continue
        lexical = os.path.normpath(os.path.abspath(raw))
        canonical = os.path.realpath(lexical)
        probes[raw] = sandbox_plan.CarveoutProbe(
            raw=raw, lexical=lexical, canonical=canonical, is_dir=os.path.isdir(canonical)
        )
    return probes


def _plan_cwd(request: sandbox_plan.SandboxRequest, host_paths: list[str]) -> str:
    """The working directory a relative path in the plan resolves against.

    Read only when a path the planner joins or compares is relative -- one the request
    names, or one the host supplies (``$HOME``, the pod's home, the relocated data-home
    spellings) -- as ``os.path.abspath`` would: a process whose working directory was
    deleted keeps planning an all-absolute layout.
    """
    paths = (
        *request.extra_hidden_dirs,
        *request.extra_visible_dirs,
        *request.extra_private_dirs,
        *request.extra_expose_files,
        *host_paths,
    )
    return os.getcwd() if any(not os.path.isabs(path) for path in paths) else os.sep


def _live_plan_host(request: sandbox_plan.SandboxRequest) -> sandbox_plan.PlanHost:
    """The production :class:`~kiro_crew.sandbox_plan.PlanHost`: this host, read now.

    Every fact is read from this module at call time -- the tier tables, the platform
    policy, the relocated data-home spellings, the voice-runtime cache, the pod
    environment -- so a test that rebinds one of them here reaches the plan. Only the
    facts the request's backend renders are read, in the order the builders always read
    them: the namespace backend alone asks the process for its uid and gid and the host
    ssh for ``accept-new`` support.
    """
    namespace = request.backend == sandbox_plan.BACKEND_NAMESPACE
    home = str(Path.home())
    uid = os.getuid() if namespace else 0
    gid = os.getgid() if namespace else 0
    # The tier lists come from the active PlatformContext, so an internal companion can
    # extend them; the Default adapter returns ``_STRICT_DIRS`` / ``_CC_DIRS``.
    # ``_STANDARD_DIRS`` is not an extension point and stays on the module global.
    if request.tier == "standard":
        tier_dirs = tuple(_STANDARD_DIRS)
    elif request.tier == "cc":
        tier_dirs = tuple(_sandbox_policy().cc_dirs())
    else:
        tier_dirs = tuple(_sandbox_policy().strict_dirs())
    relocated_crew_hidden = tuple(
        _relocated_crew_targets((*_CREW_HIDDEN_LEAVES, *_RELOCATED_CREW_HIDDEN_LEAVES))
    )
    # The pod's remapped home, gated on ``KIROCREW_POD == "1"`` exactly as
    # ``config.paths`` gates the resolver, so a non-pod session's mask is unchanged.
    pod_os_home = (
        os.environ.get("KIROCREW_OS_HOME") if os.environ.get("KIROCREW_POD") == "1" else None
    )
    relocated_policy_cache_dirs = tuple(_relocated_policy_cache_dirs())
    md_notebook_degraded_dirs = tuple(_md_notebook_degraded_mask_dirs())
    voice_runtime_roots = tuple(_voice_runtime_sandbox_paths())
    voice_runtime_parents = tuple(_voice_runtime_parent_paths())
    voice_runtime_ancestor_guards = () if namespace else tuple(_voice_runtime_ancestor_guards())
    relocated_crew_readonly = tuple(_relocated_crew_targets(_CREW_READONLY_LEAVES))
    kiro_agents_targets = tuple(_resolved_kiro_agents_targets())
    install_root_targets = tuple(_resolved_install_root_targets())
    # The namespace launcher binds the package directory itself, so a renamed parent
    # carries the read-only mount with it; Seatbelt matches paths and needs the guards.
    install_root_ancestor_guards = (
        ()
        if namespace
        else _literal_ancestor_guards(tuple(os.path.dirname(t) for t in install_root_targets))
    )
    carveout_probes = tuple(_carveout_probes(sandbox_plan.carveout_candidates(request)).values())
    host_paths = [
        home,
        *([pod_os_home] if pod_os_home else []),
        *relocated_policy_cache_dirs,
        *relocated_crew_hidden,
        *relocated_crew_readonly,
        *md_notebook_degraded_dirs,
        *voice_runtime_roots,
        *voice_runtime_parents,
        *voice_runtime_ancestor_guards,
        *kiro_agents_targets,
        *install_root_targets,
        *install_root_ancestor_guards,
    ]
    return sandbox_plan.PlanHost(
        home=home,
        cwd=_plan_cwd(request, host_paths),
        tier_dirs=tier_dirs,
        cc_files=tuple(_CC_FILES),
        cc_expose_files=tuple(_CC_EXPOSE_FILES),
        crew_readonly_targets=tuple(_CREW_READONLY_TARGETS),
        crew_hidden_dirs=tuple(_CREW_HIDDEN_DIRS),
        unreadable_mask_leaves=tuple(_CREW_UNREADABLE_MASK_LEAVES),
        sensitive_env_prefixes=tuple(_SENSITIVE_ENV_PREFIXES),
        agent_denied_env_keys=tuple(_AGENT_DENIED_ENV_KEYS),
        python_env_prefixes=tuple(_PYTHON_ENV_PREFIXES),
        pod_os_home=pod_os_home,
        pod_grant_store_leaves=frozenset(_POD_OS_HOME_GRANT_STORE_LEAVES),
        pod_masked_subleaves=tuple(_POD_OS_HOME_MASKED_SUBLEAVES),
        relocated_policy_cache_dirs=relocated_policy_cache_dirs,
        relocated_crew_hidden=relocated_crew_hidden,
        relocated_crew_readonly=relocated_crew_readonly,
        md_notebook_degraded_dirs=md_notebook_degraded_dirs,
        voice_runtime_roots=voice_runtime_roots,
        voice_runtime_parents=voice_runtime_parents,
        voice_runtime_ancestor_guards=voice_runtime_ancestor_guards,
        kiro_agents_targets=kiro_agents_targets,
        install_root_targets=install_root_targets,
        install_root_ancestor_guards=install_root_ancestor_guards,
        carveout_probes=carveout_probes,
        uid=uid,
        gid=gid,
        ssh_accept_new=_ssh_supports_accept_new() if namespace else False,
    )


def sandbox_may_hide_path(path: str) -> bool:
    """Whether a child Kiro Crew's sandbox wraps can be denied a read of *path* on this host.

    For a gateway-side read that stands in for a sandboxed child's own read of the same
    file. The caller is the native persona snapshot
    (``member_essential_context.native_spec_prompt_shape``): Crew withholds its block
    only when the harness loaded the same text, and a harness drops a ``file://`` prompt
    it cannot read without a word, so a file the gateway reads and the child cannot
    would leave the session with neither copy.

    Every tier's masks count, not one spawn's. The answer has to hold whatever tier,
    renderer or delegation the spawn ends up with, which is decided later and in more
    than one place (see :func:`wrapped_by_crew_sandbox`), and the two errors are not
    alike: a wrong "visible" costs the persona, a wrong "hidden" one repeated block. The
    trees are the ones the planner masks, pinned against both renderers by
    ``test_sandbox_plan``: each tier's directory list under ``$HOME`` and re-anchored
    under a pod's home, the relocated governance cache and crew-home leaves, the
    md-notebook directories a planted link degraded, the voice runtime, the single
    masked files and ``~/.ssh``. A caller's ``extra_hidden_dirs`` are not among them:
    the harness whose spec prompt is read this way passes none. Both the given and the
    resolved spelling of *path* are compared with both spellings of each tree, since a
    mask over a directory also hides a link into it.

    Reads what the mask builders read and changes nothing: the voice runtime is named
    from the data home and never primed here (priming creates it). Never raises: a host
    it cannot read reports the path hidden.
    """
    try:
        home = str(Path.home())
        policy = _sandbox_policy()
        tier_dirs = tuple(
            dict.fromkeys([*_STANDARD_DIRS, *policy.cc_dirs(), *policy.strict_dirs()])
        )
        trees = [os.path.join(home, rel) for rel in (*tier_dirs, *_CC_FILES, ".ssh")]
        trees.extend(_pod_os_home_targets(tier_dirs))
        trees.extend(_relocated_policy_cache_dirs())
        trees.extend(
            _relocated_crew_targets((*_CREW_HIDDEN_LEAVES, *_RELOCATED_CREW_HIDDEN_LEAVES))
        )
        trees.extend(_md_notebook_degraded_mask_dirs())
        trees.append(os.path.join(str(config_dir()), _VOICE_RUNTIME_LEAF))
        spellings = {os.path.abspath(path), os.path.realpath(path)}
        for tree in dict.fromkeys(trees):
            for root in {os.path.abspath(tree), os.path.realpath(tree)}:
                inside = root.rstrip(os.sep) + os.sep
                if any(s == root or s.startswith(inside) for s in spellings):
                    return True
    except Exception:
        logger.debug("could not tell whether the sandbox hides %s", path, exc_info=True)
        return True
    return False


def _spawn_plan(
    backend: str,
    sandbox_level: str = "strict",
    *,
    strip_python_env: bool = False,
    forward_ssh_auth_sock: bool = False,
    extra_hidden_dirs: tuple[str, ...] = (),
    seal_hidden_dirs: bool = False,
    extra_hidden_dir_ids: tuple[tuple[str, int, int], ...] = (),
    extra_alias_credential_ids: tuple[tuple[int, int], ...] = (),
    extra_visible_dirs: tuple[str, ...] = (),
    extra_private_dirs: tuple[str, ...] = (),
    extra_private_dir_ids: tuple[tuple[str, int, int], ...] = (),
    extra_writable_dirs: tuple[str, ...] = (),
    extra_expose_files: tuple[str, ...] = (),
    fail_closed_file_masks: tuple[tuple[str, int, int], ...] = (),
    required_mask_targets: tuple[str, ...] = (),
    mask_occupants: "Mapping[str, tuple[int, ...]] | None" = None,
    crew_home_aliases: tuple[tuple[str, str, int, int], ...] = (),
    exec_cwd: str = "",
) -> sandbox_plan.ConfinementPlan:
    """Plan one spawn for *backend* against this host, logging what the plan refused.

    The keyword arguments are the ones the builders below and their callers already
    take, gathered into one :class:`~kiro_crew.sandbox_plan.SandboxRequest`.
    """
    request = sandbox_plan.SandboxRequest(
        tier=sandbox_level,
        backend=backend,
        strip_python_env=strip_python_env,
        forward_ssh_auth_sock=forward_ssh_auth_sock,
        extra_hidden_dirs=tuple(extra_hidden_dirs),
        seal_hidden_dirs=bool(seal_hidden_dirs),
        extra_hidden_dir_ids=tuple(extra_hidden_dir_ids),
        extra_alias_credential_ids=tuple(extra_alias_credential_ids),
        extra_visible_dirs=tuple(extra_visible_dirs),
        extra_private_dirs=tuple(extra_private_dirs),
        extra_private_dir_ids=tuple(extra_private_dir_ids),
        extra_writable_dirs=tuple(extra_writable_dirs),
        extra_expose_files=tuple(extra_expose_files),
        fail_closed_file_masks=tuple(fail_closed_file_masks),
        required_mask_targets=tuple(required_mask_targets),
        mask_occupants=tuple((mask_occupants or {}).items()),
        crew_home_aliases=tuple(crew_home_aliases),
        exec_cwd=exec_cwd,
    )
    plan = sandbox_plan.plan_confinement(request, _live_plan_host(request))
    _log_refusals(plan.refusals)
    return plan


def _build_launcher_script(
    sandbox_level: str = "strict",
    *,
    strip_python_env: bool = False,
    forward_ssh_auth_sock: bool = False,
    extra_hidden_dirs: tuple[str, ...] = (),
    seal_hidden_dirs: bool = False,
    extra_hidden_dir_ids: tuple[tuple[str, int, int], ...] = (),
    extra_alias_credential_ids: tuple[tuple[int, int], ...] = (),
    extra_visible_dirs: tuple[str, ...] = (),
    extra_private_dirs: tuple[str, ...] = (),
    extra_private_dir_ids: tuple[tuple[str, int, int], ...] = (),
    extra_writable_dirs: tuple[str, ...] = (),
    extra_expose_files: tuple[str, ...] = (),
    fail_closed_file_masks: tuple[tuple[str, int, int], ...] = (),
    required_mask_targets: tuple[str, ...] = (),
    mask_occupants: "Mapping[str, tuple[int, ...]] | None" = None,
    crew_home_aliases: tuple[tuple[str, str, int, int], ...] = (),
    exec_cwd: str = "",
) -> str:
    """The Linux namespace launcher program for one spawn, as Python source text.

    Plans the spawn (:func:`_spawn_plan`) and renders the plan with
    :func:`kiro_crew.sandbox_launcher.render_namespace_launcher`. No filesystem probe of a
    masked path happens here: every identity the launcher checks was observed by the
    pre-spawn passes in :func:`namespace_argv` and arrives as data.
    """
    return sandbox_launcher.render_namespace_launcher(
        _spawn_plan(
            sandbox_plan.BACKEND_NAMESPACE,
            sandbox_level,
            strip_python_env=strip_python_env,
            forward_ssh_auth_sock=forward_ssh_auth_sock,
            extra_hidden_dirs=extra_hidden_dirs,
            seal_hidden_dirs=seal_hidden_dirs,
            extra_hidden_dir_ids=extra_hidden_dir_ids,
            extra_alias_credential_ids=extra_alias_credential_ids,
            extra_visible_dirs=extra_visible_dirs,
            extra_private_dirs=extra_private_dirs,
            extra_private_dir_ids=extra_private_dir_ids,
            extra_writable_dirs=extra_writable_dirs,
            extra_expose_files=extra_expose_files,
            fail_closed_file_masks=fail_closed_file_masks,
            required_mask_targets=required_mask_targets,
            mask_occupants=mask_occupants,
            crew_home_aliases=crew_home_aliases,
            exec_cwd=exec_cwd,
        )
    )


def _build_seatbelt_profile(
    sandbox_level: str = "strict",
    *,
    extra_hidden_dirs: tuple[str, ...] = (),
    extra_visible_dirs: tuple[str, ...] = (),
    extra_private_dirs: tuple[str, ...] = (),
    extra_writable_dirs: tuple[str, ...] = (),
    extra_expose_files: tuple[str, ...] = (),
) -> str:
    """The macOS Seatbelt profile for one spawn, as ``sandbox-exec`` profile text.

    Plans the spawn (:func:`_spawn_plan`) and renders the plan with
    :func:`kiro_crew.sandbox_seatbelt.render_seatbelt_profile`.
    """
    return sandbox_seatbelt.render_seatbelt_profile(
        _spawn_plan(
            sandbox_plan.BACKEND_SEATBELT,
            sandbox_level,
            extra_hidden_dirs=extra_hidden_dirs,
            extra_visible_dirs=extra_visible_dirs,
            extra_private_dirs=extra_private_dirs,
            extra_writable_dirs=extra_writable_dirs,
            extra_expose_files=extra_expose_files,
        )
    )


def namespace_argv(
    argv: list[str],
    sandbox_level: str = "strict",
    *,
    strip_python_env: bool = False,
    forward_ssh_auth_sock: bool = False,
    extra_hidden_dirs: tuple[str, ...] = (),
    seal_hidden_dirs: bool = False,
    extra_hidden_dir_ids: tuple[tuple[str, int, int], ...] = (),
    extra_alias_credential_ids: tuple[tuple[int, int], ...] = (),
    extra_visible_dirs: tuple[str, ...] = (),
    extra_private_dirs: tuple[str, ...] = (),
    extra_private_dir_ids: tuple[tuple[str, int, int], ...] = (),
    extra_writable_dirs: tuple[str, ...] = (),
    extra_expose_files: tuple[str, ...] = (),
    exec_cwd: str = "",
) -> list[str]:
    """Wrap *argv* via the Python namespace launcher.

    The launcher forks, the parent writes identity UID/GID maps, and the
    child bind-mounts empty dirs over credential paths before exec.
    The child retains the real UID/GID.
    """
    resolved_argv = list(argv)
    if resolved_argv:
        resolved_argv[0] = _resolve_agent_executable(resolved_argv[0])

    # Give the seal something to mount ON, or refuse the spawn. ``readonly_dirs`` is
    # guarded on
    # ``os.path.exists`` in the launcher (a ceiling may be a plain file, so the guard
    # cannot be ``isdir``), and an absent ceiling therefore gets no bind + remount pair
    # at all — leaving the data home writable at that name for the whole sandbox.
    # Materialising the sealable subset first is what makes the seal non-vacuous on a
    # default install. Runs before the script is built so the paths exist by the time
    # the child mounts, and raises ``SandboxCeilingUnsealable`` rather than launching
    # with a keystone the seal could not cover.
    _required_targets: list[str] = []
    _materialize_sealable_ceilings(_required_targets)
    # The mask loop has the same guard (``isdir``), so the on-demand hidden
    # directories get the same treatment for the same reason.
    _materialize_maskable_dirs(_required_targets)
    # The Notes move runs BEFORE the stubs below: it reads real state at the bare names
    # (and empties the legacy PAT in place), and the materialiser then plants the stub
    # the launcher masks at any bare name left absent. A failed move is logged, not raised.
    _migrate_md_notebook_legacy_state()
    # The crewmate records' one-time move out of ``trust/`` runs here too, so it is
    # done before an agent of this build starts. Unlike the Notes move, one that did not
    # finish while old records remain REFUSES the spawn (``SandboxCeilingUnsealable``).
    _member_records_move()
    # And the ``sensitive_files`` loop is guarded on ``isfile``, so md-notebook's state
    # leaves — creatable on a sandboxed host now that the backend carve-out exists —
    # need a mount target too.
    _materialize_md_notebook_mask_targets(_required_targets)
    # The live-target pointer needs one for a stronger reason than a leak: it names the
    # checkout the gateway execs into, and there is no carve-out for it at all — it is
    # creatable from any sandbox simply because the data-home ROOT is writable there and
    # an absent name has no mask. Publishing the stub first makes the mask non-vacuous.
    _materialize_live_target_mask_target(_required_targets)
    # CLEANUP BEFORE THE REFUSAL, and this order is a contract rather than a preference.
    # Both sweeps and the reconciliation remove names that are themselves hard links to a
    # masked credential leaf -- a pre-upgrade orphan is a link to the signing key by
    # construction, and so is a staging name the publisher kept when its directory sync
    # failed. The refusal below reads ``st_nlink``, so running it first would refuse on a
    # link count that the very next statement was about to fix, and refuse again on every
    # later spawn: the cleanup written for that state would sit permanently behind the gate
    # it is supposed to open. Neither sweep needs the refusal to have run, because each
    # descends by pinned descriptor and skips a root it cannot open.
    _sweep_legacy_md_notebook_temps()
    _sweep_legacy_auth_store_temps()
    _reconcile_auth_store_staging_links()
    # LAST of the pre-spawn checks, and last on purpose: every masked leaf's NAME must be
    # the name the mask binds, and the leaves above have already answered for themselves
    # with sentences tailored to what they hold. This pass covers the rest -- the masked
    # leaves nothing materialises, whose alias went unreported entirely -- and creates
    # nothing, so an unused store stays absent.
    # The identities the passes above SAW, carried into the launcher so the child can
    # require a match before it follows a link at one of these names. The alias pass
    # records them from the ``lstat`` it already performs, so the common case costs no
    # syscall; the established targets are added here because a materialiser that just
    # created one has not classified it, and they are warm. NOT every masked target: the
    # launcher masks hundreds, and statting them all here would put the loop-blocking
    # probe that ``test_the_builder_does_not_stat_the_hidden_paths`` documents back on
    # the gateway's single loop. A name with no entry keeps its previous behaviour, and
    # the module spec records that boundary.
    #
    # The same pass ALSO returns the path-only aliases this spawn must hide (the hard-link
    # alias defence): one call fills ``_mask_occupants`` for the launcher's
    # no-follow occupant check AND returns ``alias_masks`` for ``fail_closed_file_masks``.
    _mask_occupants: dict[str, tuple[int, ...]] = {}
    alias_masks = _refuse_aliased_masked_leaves(_mask_occupants)
    for _target in dict.fromkeys(_required_targets):
        if _target in _mask_occupants:
            continue
        try:
            _info = os.lstat(_target)
        except OSError as exc:
            # FAIL CLOSED. ``_required_targets`` are names a pre-spawn materialiser just
            # established as PRESENT; an ``lstat`` that now fails means the name changed
            # between then and here -- renamed aside, or its permissions revoked -- which
            # is the very substitution window this identity is carried to catch. Skipping
            # it would carry NO identity, so ``_carried_occupant`` returns ``None`` in the
            # launcher, the mismatch refusal never fires, and a decoy left at the name is
            # sealed while the original stays writable at whatever name it moved to. Refuse
            # the spawn instead, the same way an unsealable ceiling refuses.
            raise SandboxCeilingUnsealable(
                f"cannot re-stat the established mask target {safe_terminal_line(_target)} "
                f"to carry its identity into the launcher: {safe_terminal_line(str(exc))}. "
                "A pre-spawn pass saw it present, so a stat that now fails means the name "
                "changed under this launch; refusing rather than masking whatever took its "
                "place."
            ) from exc
        _mask_occupants[_target] = (
            _info.st_dev,
            _info.st_ino,
            int(stat.S_ISLNK(_info.st_mode)),
            *_referent_identity(_target, _info),
        )
    # ``~/.ssh`` is not a crew leaf, so no pass above sees it, and the strict tier masks
    # it. One named ``lstat`` here is what lets the launcher require a match instead of
    # taking its own first look after the fork -- a look that arrives on the far side of
    # script build, ``mkstemp`` and ``unshare``, which is the window being closed.
    if sandbox_level == "strict":
        _ssh_target = os.path.join(os.path.expanduser("~"), ".ssh")
        try:
            _ssh_info = os.lstat(_ssh_target)
        except OSError:
            pass
        else:
            _mask_occupants[_ssh_target] = (
                _ssh_info.st_dev,
                _ssh_info.st_ino,
                int(stat.S_ISLNK(_ssh_info.st_mode)),
                *_referent_identity(_ssh_target, _ssh_info),
            )

    # Carry the alias-mask identities into the occupant map too. The alias pass
    # already discovered each hard-linked credential alias and its device/inode
    # (``_AliasMask``); handing those paths to the launcher only via
    # ``fail_closed_file_masks``/``extra_hidden_dirs`` leaves ``_carried_occupant``
    # with nothing to enforce for them, so a rename BETWEEN the alias check and the
    # bind would let the pin mask a decoy left at the old name while the credential
    # stays readable under its moved name. An alias is a regular-file hard link, so
    # its recorded link flag is not-a-link (0), what the name reaches is a regular
    # file (2), and the referent is the entry itself; the pin then refuses if the object at the name is not the one the
    # pass saw, the same fail-closed rule every other masked target already gets.
    for _am in alias_masks:
        _mask_occupants.setdefault(_am.path, (_am.dev, _am.ino, 0, 2, _am.dev, _am.ino))

    script = _build_launcher_script(
        sandbox_level,
        strip_python_env=strip_python_env,
        forward_ssh_auth_sock=forward_ssh_auth_sock,
        extra_hidden_dirs=extra_hidden_dirs + tuple(m.path for m in alias_masks),
        seal_hidden_dirs=seal_hidden_dirs,
        extra_hidden_dir_ids=extra_hidden_dir_ids,
        extra_alias_credential_ids=extra_alias_credential_ids,
        # The same paths again WITH the inode each one was at discovery, as the set whose
        # absence or changed identity at mask time is a fault. Discovery and the bind are two
        # separate acts: the mask loop's ordinary policy is to skip a target that is absent,
        # which for a credential alias would mean a rename between the two silently leaves the
        # bytes readable under the new name -- and a decoy left at the old name is one ``ln``
        # from two links, so only the identity tells it apart.
        fail_closed_file_masks=tuple((m.path, m.dev, m.ino) for m in alias_masks),
        extra_visible_dirs=extra_visible_dirs,
        extra_private_dirs=extra_private_dirs,
        extra_private_dir_ids=extra_private_dir_ids,
        extra_writable_dirs=extra_writable_dirs,
        extra_expose_files=extra_expose_files,
        required_mask_targets=tuple(_required_targets),
        mask_occupants=_mask_occupants,
        # Decided here, beside the passes that already stat the crew roots, so the
        # builder stays free of filesystem calls and still hands the launcher one
        # name per crew directory on a symlinked home.
        crew_home_aliases=_crew_home_alias_roots(),
        exec_cwd=exec_cwd,
    )
    run_dir = _ensure_run_dir()
    # Prefix and suffix from the module constants, not a second spelling of them: the
    # run-dir sweep reclaims this family by prefix and ``session_pid``'s managed-agent
    # gate RECOGNISES the launcher by prefix and suffix, so a literal here that drifted
    # from either would leak the tempfile and cost every sandboxed root its reclaim.
    fd, path = tempfile.mkstemp(
        suffix=_LAUNCHER_SCRIPT_SUFFIX,
        prefix=f"{_SANDBOX_ARTIFACT_PREFIX}{os.getpid()}_",
        dir=run_dir,
    )
    os.write(fd, script.encode())
    os.close(fd)
    platform_compat.chmod_safe(path, 0o700)

    return [sys.executable, *_LAUNCHER_INTERPRETER_FLAGS, path, *resolved_argv]


def _pinned_env_bin() -> str:
    """Absolute path to ``env``, resolved WITHOUT consulting PATH.

    ``env`` is the process that applies the credential scrub (``env -u KEY ...``)
    on the delegation paths, where no Seatbelt/namespace layer wraps the child.
    A bare ``"env"`` token is resolved by the OS through the PATH in the
    environment we hand ``Popen`` -- and on the script-cron MCP path that PATH can
    come from a config-declared server ``env`` block. Redirecting ``env`` does not
    merely run an attacker binary: it means the scrub NEVER RUNS, so the child
    receives the very credentials (Slack tokens, owner id) the ``-u`` flags exist
    to strip, and can exfiltrate them. Pinning is therefore load-bearing on these
    paths even though they intentionally apply no OS confinement of our own.

    ``trusted_system_bin`` ignores ``os.environ`` PATH entirely (fixed system dirs
    only); the ``/usr/bin/env`` fallback matches the idiom already used by
    ``sandbox_exec_argv`` and ``cgroup_scope_argv`` so an unusual host layout
    still yields an absolute path rather than a redirectable bare name.

    Deliberately does NOT pin the inner command: that is the operator's agent or
    server binary (``kiro-cli``, ``npx``, ...), which legitimately must be found on
    PATH, and it carries the same config-file trust level as the ``env`` block
    itself -- an author who can set PATH can already set ``command`` directly, so
    pinning it would buy nothing while breaking normal installs.
    """
    return platform_compat.trusted_system_bin("env") or "/usr/bin/env"


def sandbox_exec_argv(
    argv: list[str],
    sandbox_level: str = "strict",
    *,
    strip_python_env: bool = False,
    forward_ssh_auth_sock: bool = False,
    extra_hidden_dirs: tuple[str, ...] = (),
    extra_hidden_dir_ids: tuple[tuple[str, int, int], ...] = (),
    extra_visible_dirs: tuple[str, ...] = (),
    extra_private_dirs: tuple[str, ...] = (),
    extra_private_dir_ids: tuple[tuple[str, int, int], ...] = (),
    extra_writable_dirs: tuple[str, ...] = (),
    extra_expose_files: tuple[str, ...] = (),
) -> tuple[list[str], str | None]:
    """Wrap *argv* with ``sandbox-exec -f <profile>``.

    Also scrubs sensitive env vars via ``env -u`` since Seatbelt only
    handles file-level deny rules, not environment variables.

    Returns (new_argv, tmp_profile_path).  Caller should delete the
    profile file after the child exits.
    """
    resolved_argv = list(argv)
    if resolved_argv:
        resolved_argv[0] = _resolve_agent_executable(resolved_argv[0])

    # A Seatbelt deny on a linked models name covers the link, not the tree it points at.
    _refuse_linked_models_leaf()
    # A pre-upgrade md-notebook staging temp holding the PAT sits at a name this profile
    # never denies (it names the state leaves and the staging directory, not an arbitrary
    # ``*.tmp`` sibling), so removing it is NOT Linux-only work. File materialisation
    # stays on the namespace path — a Seatbelt deny is a path rule that holds for a name
    # that does not exist yet — but an orphan already on disk needs sweeping here too.
    _sweep_legacy_md_notebook_temps()
    _migrate_md_notebook_legacy_state()
    _member_records_move()
    _sweep_legacy_auth_store_temps()
    # Reconciled here as well. The stale name is a second name for the signing key either
    # way, and leaving it to accumulate on a macOS host means the first Linux spawn on a
    # shared data home meets a backlog of them.
    _reconcile_auth_store_staging_links()
    # A Seatbelt deny is path-shaped -- ``deny file-read* (subpath ...)`` names the leaf, not
    # its inode -- so a second hard link on a credential leaf is read straight through the
    # profile. That is the same exposure the namespace path refuses on, so this pass belongs
    # on both launch paths and not only where a bind mask is what does the hiding. It runs
    # AFTER the cleanup above, which is the ordering the namespace path also establishes: a
    # link the cleanup would have removed must not be what refuses.
    alias_masks = _refuse_multilinked_credential_leaves(masks_are_path_only=True)

    # A caller that pins a window to an inode is asking for a guarantee no path-rule
    # profile can make in full: the allow names a MUTABLE pathname, and a same-UID peer
    # renaming an app onto it is read through that allow. The Linux child compares the
    # descriptor it pinned; this backend has no counterpart at exec time. What it DOES have
    # is the check the mask roots below already get -- read the name's own identity
    # immediately before the profile is written, refuse on a mismatch, and state the
    # residual instead of implying it. A window gets that same treatment, rather than being
    # dropped: dropping a pinned window would deny the caller the directory it pinned.
    for path_, dev, ino in extra_private_dir_ids:
        if path_ not in extra_private_dirs:
            continue
        try:
            win_st = os.lstat(path_)
        except OSError as exc:
            raise SandboxCeilingUnsealable(
                f"cannot confirm the data window {safe_terminal_line(path_)} is still the "
                f"one this spawn approved: {safe_terminal_line(str(exc))}. Refusing rather "
                "than opening a window at a name whose identity cannot be read."
            ) from exc
        # NO-FOLLOW and a real directory, the two rules the producer recorded the approval
        # under and the two the Linux child re-reads through
        # ``O_PATH|O_NOFOLLOW|O_DIRECTORY``. A rename preserves the inode, so a followed
        # lookup would compare the wrong object.
        if not stat.S_ISDIR(win_st.st_mode):
            raise SandboxCeilingUnsealable(
                f"cannot open a data window at {safe_terminal_line(path_)}: the name is no "
                "longer a directory, so the allow would carve out a link rather than the "
                "tree this spawn approved. Refusing the spawn."
            )
        if (win_st.st_dev, win_st.st_ino) != (dev, ino):
            raise SandboxCeilingUnsealable(
                f"the data window {safe_terminal_line(path_)} is not the one this spawn "
                "approved -- it was replaced after approval, so the allow would expose a "
                "substitute. Refusing the spawn."
            )
    # A pinned MASK ROOT is the opposite direction and cannot be answered the same way.
    # Withholding a WINDOW leaves the tree masked, so it costs a data view; withholding a
    # MASK would leave the tree open, which is the exposure the pin was taken against --
    # so the fail-closed answer here is to refuse the spawn, exactly as the Linux child
    # does when its own comparison fails. Dropping the argument was the real defect: the
    # approval was taken and then discarded, so the profile masked a pathname while the
    # caller believed an inode had been settled.
    #
    # Checked as late as this backend can check anything -- immediately before the profile
    # is written -- and honestly NOT race-free: a same-UID peer can still rename the tree
    # between this ``lstat`` and the ``exec``, and no path-rule profile can close that,
    # because the peer is outside the sandbox and Seatbelt has no inode predicate. What
    # this does close is the case where the rename already happened, and it ends the
    # silence in the case it cannot: the residual is stated here rather than implied by an
    # argument that went nowhere.
    for path_, dev, ino in extra_hidden_dir_ids:
        try:
            st = os.lstat(path_)
        except OSError as exc:
            raise SandboxCeilingUnsealable(
                f"cannot confirm the masked directory {safe_terminal_line(path_)} is still "
                f"the one this spawn approved: {safe_terminal_line(str(exc))}. Refusing "
                "rather than masking a name whose identity cannot be read."
            ) from exc
        # NO-FOLLOW, and a real directory or nothing -- the two rules a caller's approval
        # is recorded under, and the same pair the Linux child gets from ``O_PATH|O_NOFOLLOW|O_DIRECTORY``. A followed lookup
        # compares the wrong object: a rename PRESERVES the inode, so moving the tree
        # aside and leaving a symlink at the name resolves back to the approved
        # ``(dev, ino)`` and passes, while the profile rule covers only the link's name
        # and the tree stays readable where it was moved to. Checking the name's OWN
        # identity makes the link a mismatch, and the explicit directory test says so in
        # its own words rather than relying on the inodes differing.
        if not stat.S_ISDIR(st.st_mode):
            raise SandboxCeilingUnsealable(
                f"cannot mask {safe_terminal_line(path_)}: the name is no longer a "
                "directory, so masking it would cover a link while the directory this "
                "spawn approved stays readable elsewhere. Refusing the spawn."
            )
        if (st.st_dev, st.st_ino) != (dev, ino):
            raise SandboxCeilingUnsealable(
                f"the masked directory {safe_terminal_line(path_)} is not the one this "
                "spawn approved -- it was replaced after approval, so masking the name "
                "would cover a substitute while the original stays readable under its new "
                "one. Refusing the spawn."
            )
    profile = _build_seatbelt_profile(
        sandbox_level,
        # These entries become path RULES, not binds over an inode. That is weaker than it
        # first appears: the rule keeps naming a path, and nothing here denies a write to the
        # alias's parent or its ancestors while the data home root stays writable in-sandbox,
        # so the governed process can rename a parent and read the bytes under a name no rule
        # covers -- without needing to win any race. The seal for the voice runtime exists for
        # exactly this reason. So a located alias on a CREDENTIAL leaf in the live home now
        # refuses above rather than arriving here, and what still arrives is the case where a
        # refusal would be disproportionate: a home the install does not use.
        extra_hidden_dirs=extra_hidden_dirs + tuple(m.path for m in alias_masks),
        extra_visible_dirs=extra_visible_dirs,
        extra_private_dirs=extra_private_dirs,
        extra_writable_dirs=extra_writable_dirs,
        extra_expose_files=extra_expose_files,
    )
    run_dir = _ensure_run_dir()
    # Constants, not a second spelling: the run-dir sweep reclaims this family by prefix
    # and suffix, and a literal here that drifted from either would leak the profile.
    fd, path = tempfile.mkstemp(
        suffix=_SEATBELT_PROFILE_SUFFIX,
        prefix=f"{_SANDBOX_ARTIFACT_PREFIX}{os.getpid()}_",
        dir=run_dir,
    )
    os.write(fd, profile.encode())
    os.close(fd)
    # Build env -u flags for sensitive vars present in current env. cc/strict
    # additionally scrub agent-denied credential keys (Slack tokens, owner id)
    # since config/loader_config.py seeds them into os.environ for trusted children only.
    unset_args = _sandbox_env_unset_args(sandbox_level, strip_python_env, forward_ssh_auth_sock)
    # Mark the sandboxed tree, exactly as the Linux namespace launcher does after
    # its own env scrub (see ``scrub_env`` in ``sandbox_launcher_parts/exec_lockdown.py``, the
    # program ``_build_launcher_script`` renders). Without
    # this, an in-sandbox ``wrap_argv`` call cannot tell that Kiro Crew's own
    # sandbox already confines it, tries to nest, and gets EPERM — which then
    # fail-closes every app-backend and MCP spawn on the host. Set as an ``env``
    # assignment so it lands AFTER the ``-u`` flags and cannot be dropped by them.
    marker = f"{_IN_SANDBOX_MARKER}=1"
    # Record the tier beside the marker, in the same after-the-``-u``-flags
    # position so the scrub cannot drop it — an in-sandbox wrap_argv
    # passthrough compares it against the requested tier to detect downgrades.
    level_assign = f"{_IN_SANDBOX_LEVEL_VAR}={sandbox_level}"
    # SECURITY: BOTH wrappers this function prepends are pinned here, at the layer
    # that prepends them, so no spawn site has to remember to re-pin (the caller's
    # ``env`` may carry a config-declared PATH, and CPython resolves a slash-less
    # argv[0] through THAT PATH via os.get_exec_path):
    #   * the outer ``env`` (argv[0]), which runs first of all;
    #   * the inner ``sandbox-exec``, which ``env`` itself resolves through the
    #     PATH in the environment it is handed -- BEFORE the Seatbelt profile is
    #     applied, so a hostile PATH there is a pre-confinement escape.
    # ``trusted_system_bin`` ignores PATH entirely rather than reading os.environ:
    # a gateway's PATH can legitimately lead with agent-writable directories
    # (a worktree venv's bin, ~/.local/bin), so resolving through it would leave
    # the hole half-open. Both fall back to their canonical macOS locations,
    # matching the _probe_sandbox_exec probe, so a host with an unusual layout
    # still gets an absolute path rather than a redirectable bare name.
    outer_env = _pinned_env_bin()
    sandbox_exec = platform_compat.trusted_system_bin("sandbox-exec") or "/usr/bin/sandbox-exec"
    return (
        [
            outer_env,
            *unset_args,
            marker,
            level_assign,
            sandbox_exec,
            "-f",
            path,
            *resolved_argv,
        ],
        path,
    )


def _sandbox_env_scrub_keys(
    sandbox_level: str, strip_python_env: bool, forward_ssh_auth_sock: bool = False
) -> list[str]:
    """Names of the live environment keys to scrub for a given sandbox level.

    The single source of the per-level scrub set, shared by
    :func:`_sandbox_env_unset_args` (which renders it as ``env -u`` flags) and
    the first-party no-backend carve-out in :func:`wrap_argv` (which hands the
    keys to :func:`_unset_env_argv` for a trusted-absolute-path ``env`` prefix),
    so the two paths can never scrub different sets.
    """
    prefixes = list(_SENSITIVE_ENV_PREFIXES)
    if sandbox_level in ("cc", "strict"):
        prefixes.extend(_AGENT_DENIED_ENV_KEYS)
    if strip_python_env:
        prefixes.extend(_PYTHON_ENV_PREFIXES)
    # Honour the SSH_AUTH_SOCK forward opt-in on the seatbelt
    # ``env -u`` path (macOS) exactly as on the Linux launcher. The decision is
    # passed in (resolved off-loop on the agent path) and defaults False, so a
    # generic caller keeps the socket in the unset flags.
    prefixes = _agent_scrub_prefixes(prefixes, forward_ssh_auth_sock)
    return [key for key in os.environ if any(key.startswith(p) for p in prefixes)]


def _sandbox_env_unset_args(
    sandbox_level: str, strip_python_env: bool, forward_ssh_auth_sock: bool = False
) -> list[str]:
    """``env -u`` flags scrubbing sensitive vars for a sandboxed/delegated spawn.

    Shared by ``sandbox_exec_argv`` (seatbelt wrap) and
    ``_delegate_to_kiro_internal_sandbox`` (macOS mutual-exclusion path) so the
    env-scrub guarantee is identical whether or not Kiro Crew's own seatbelt is
    the active isolation layer.
    """
    unset_args: list[str] = []
    for key in _sandbox_env_scrub_keys(sandbox_level, strip_python_env, forward_ssh_auth_sock):
        unset_args.extend(["-u", key])
    return unset_args


def cleanup_stale_sandbox_profiles(*, data_home: Path, legacy_dir: str | None = None) -> int:
    """Remove orphan sandbox files from runtime artifact directories and legacy /tmp.

    A file is removed when EITHER:
      - The tagged PID is dead (os.kill probe fails), OR
      - The file mtime is older than _LAUNCHER_MAX_AGE_SECONDS (the launcher
        is consumed exactly once at child exec, so old files are garbage
        regardless of PID liveness — this handles the spawner-PID design
        where the gateway PID is always alive for current-generation files).

    Also sweeps legacy /tmp/kirocrew_sandbox_*.py files that predate the
    migration to <config_dir>/run/ — these have no PID segment, so only the
    age threshold applies — plus the orphaned bind-mount sources the namespace
    launcher stages on tmpfs (see _cleanup_stale_sandbox_mount_sources).

    Called from the periodic cleanup sweep in session_parts/sweeps.py, offloaded to the
    maintenance executor (blocking I/O).  Safe to call from sync contexts too.

    *data_home* is the data home every path below is rooted at. The sweep runs on
    a pool thread, and a pool thread resolves ``config_dir()`` whenever it happens
    to be scheduled, which, under the test suite, is routinely AFTER the test
    that queued it has torn down its ``KIROCREW_HOME`` pin, so the sweep then
    walked (and could stamp or remove under) the operator's real ``~/.kiro/crew``.
    The caller that knows the home resolves it on ITS thread and passes it in;
    there is deliberately no default, so no future caller can reopen that path.

    Returns:
        Number of stale files removed.
    """
    now = time.time()
    if legacy_dir is None:
        legacy_dir = _LEGACY_LAUNCHER_DIR
    artifact_dirs = (
        (str(data_home / "run"), _RUN_DIR_ARTIFACTS),
        (str(data_home / "pi-gate"), _PI_GATE_DIR_ARTIFACTS),
    )
    removed = 0

    # ── Sweep runtime artifact directories (PID + age) ──
    for artifact_dir, families in artifact_dirs:
        artifact_fd: int | None = None
        try:
            if platform_compat.IS_WINDOWS:
                # Windows has no os-level pinned-directory primitive, so this
                # check-then-act arm retains an accepted replacement window.
                if platform_compat.is_link_or_junction(artifact_dir):
                    logger.warning(
                        "Refusing to sweep linked or non-directory artifact directory: %s",
                        artifact_dir,
                    )
                    continue
                if not os.path.isdir(artifact_dir):
                    continue
                entries = os.listdir(artifact_dir)
            else:
                try:
                    artifact_fd = os.open(
                        artifact_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                    )
                except OSError as exc:
                    if exc.errno == errno.ENOENT:
                        continue
                    if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                        logger.warning(
                            "Refusing to sweep linked or non-directory artifact directory: %s",
                            artifact_dir,
                        )
                        continue
                    raise
                with os.scandir(artifact_fd) as iterator:
                    entries = [entry.name for entry in iterator]

            for entry in entries:
                prefix = next((p for p in families if entry.startswith(p)), None)
                if prefix is None:
                    continue
                suffix = next((x for x in families[prefix] if entry.endswith(x)), None)
                if suffix is None:
                    continue
                filepath = os.path.join(artifact_dir, entry)
                # Age check first — handles the spawner-PID design flaw. Not for the
                # gate artifacts (pi and dsh): those are written once per gateway
                # process and REUSED by every later spawn of that process, so their
                # age says nothing, and the PID in their name is the owner's own.
                try:
                    if artifact_fd is None:
                        mtime = os.stat(filepath).st_mtime
                    else:
                        mtime = os.stat(entry, dir_fd=artifact_fd, follow_symlinks=False).st_mtime
                except OSError:
                    continue
                if prefix == _SANDBOX_ARTIFACT_PREFIX and (now - mtime) > _LAUNCHER_MAX_AGE_SECONDS:
                    try:
                        if artifact_fd is None:
                            os.remove(filepath)
                        else:
                            os.unlink(entry, dir_fd=artifact_fd)
                        removed += 1
                    except OSError:
                        pass
                    continue
                # Fresh file — fall back to PID liveness check
                middle = entry[len(prefix) : -len(suffix)]
                pid = sandbox_mount_sweep._parse_pid_segment(middle.split("_", 1)[0])
                if pid is None:
                    continue
                # Liveness probe via the shim — NEVER raw os.kill(pid, 0), which
                # TERMINATES the target process on Windows (see platform_compat).
                try:
                    alive = platform_compat.pid_exists(pid)
                except OverflowError:
                    alive = False  # absurd pid digits from a corrupt filename — stale
                if not alive:
                    try:
                        if artifact_fd is None:
                            os.remove(filepath)
                        else:
                            os.unlink(entry, dir_fd=artifact_fd)
                        removed += 1
                    except OSError:
                        pass
        finally:
            if artifact_fd is not None:
                os.close(artifact_fd)

    # ── Sweep legacy /tmp/kirocrew_sandbox_*.py (age only, no PID segment) ──
    if os.path.isdir(legacy_dir):
        try:
            with os.scandir(legacy_dir) as it:
                for dentry in it:
                    if not dentry.name.startswith(_SANDBOX_ARTIFACT_PREFIX):
                        continue
                    if not dentry.name.endswith(_LAUNCHER_SCRIPT_SUFFIX):
                        continue
                    try:
                        mtime = dentry.stat().st_mtime
                    except OSError:
                        continue
                    if (now - mtime) > _LAUNCHER_MAX_AGE_SECONDS:
                        try:
                            os.remove(dentry.path)
                            removed += 1
                        except OSError:
                            pass
        except OSError:
            pass

    removed += sandbox_mount_sweep._cleanup_stale_sandbox_mount_sources()
    removed += sandbox_mount_sweep._cleanup_legacy_mount_source_residue(data_home)
    removed += sandbox_mount_sweep._cleanup_retired_acp_snapshot_dir(data_home)
    return removed


# Absolute paths only: the shim that drops the locators again must not be
# resolvable through a caller- or agent-influenced PATH.
_ENV_BINARY_CANDIDATES = ("/usr/bin/env", "/bin/env")


def _unset_env_argv(keys: tuple[str, ...]) -> list[str] | None:
    """Return an ``env -u KEY …`` prefix dropping *keys*, or None if impossible.

    ``env`` ``exec``s its target in place (it does not fork), so prepending this
    inside the scope leaves PID tracking, ``killpg`` and descendant scans intact
    — the eventual PID is still the real child.

    Returns None when no ``env`` binary exists at a trusted absolute path, which
    callers must treat as "do not forward the bus locators at all".
    """
    for candidate in _ENV_BINARY_CANDIDATES:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            argv = [candidate]
            for key in keys:
                argv += ["-u", key]
            return argv
    return None

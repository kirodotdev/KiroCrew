"""The single chokepoint for an agent-influenced spawn: wrapped argv plus scrubbed env.

:func:`sandboxed_spawn_argv` and its async form, the environment scrubs, and the
masking queries callers use. One of the owners ``kiro_crew.sandbox`` is carved into:
every name here stays readable and patchable as ``kiro_crew.sandbox.<name>``.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew import platform_compat
from kiro_crew.constants import (
    KIROCREW_SANDBOX_TOOL_ENV,
    KIROCREW_SANDBOX_TOOL_VALUE,
    KIROCREW_SPAWNED_ENV,
    KIROCREW_SPAWNED_VALUE,
)
from kiro_crew.sandbox_argv import (
    _LAUNCHER_INTERPRETER_FLAGS,
    _SANDBOX_ARTIFACT_PREFIX,
    _unset_env_argv,
)
from kiro_crew.sandbox_cgroup import _probe_cgroup_scope
from kiro_crew.sandbox_delegation import kiro_internal_sandbox_enabled
from kiro_crew.sandbox_probe import _inside_kirocrew_sandbox, _macos_sandbox_state
from kiro_crew.sandbox_rlimit import run_limited
from kiro_crew.sandbox_slice import cgroup_scope_argv
from kiro_crew.sandbox_tiers import (
    _AGENT_DENIED_ENV_KEYS,
    _PYTHON_ENV_PREFIXES,
    _SENSITIVE_ENV_PREFIXES,
    _clamp_sandbox_mode_to_floor,
    _governance_sandbox_floor,
)
from kiro_crew.sandbox_unavailable import (
    _TRUSTED_LAUNCHER_TIMEOUT_SECS,
    SandboxUnavailableError,
    launcher_refusal,
)
from kiro_crew.sandbox_wrap import detect_backend, wrap_argv

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence
    from concurrent.futures import ThreadPoolExecutor
    from typing import Any

# Logged under the sandbox's name, which operator log filters, level settings
# and the tests' caplog scopes key on.
logger = logging.getLogger("kiro_crew.sandbox")


def corroborate_launcher_refusal(
    output: str,
    *,
    mode: str = "strict",
    extra_hidden_dirs: tuple[str, ...] = (),
    extra_visible_dirs: tuple[str, ...] = (),
) -> tuple[str, str, str] | None:
    """A post-spawn sandbox verdict for a refused child -- from a TRUSTED launcher run.

    The launcher's refusal line (:func:`launcher_refusal`) is the only signal a
    post-spawn refusal leaves, and it cannot be trusted on its own: the child
    was the unverified candidate, and a planted ``kiro-cli`` can print the same
    line and exit 1. So the candidate's line decides only WHETHER to look
    further. The verdict comes from re-running the REAL launcher around a
    trusted no-op (``true``), under the refused spawn's own ``mode`` and
    hidden/visible dirs, and classifying THAT run's own stderr -- text no child
    wrote.

    Running the whole launcher (not the boot probe) is what makes this cover
    EVERY launcher step. The probe mirrors only the first three -- the two
    unshares and the propagation mount -- so a policy that refused a LATER step
    (a hiding bind mount, ``NO_NEW_PRIVS``, the seccomp-BPF install) left the
    probe building the sandbox while every real spawn died, and the refusal was
    reported as ``installed=False``. The no-op runs every step, so its stderr
    carries whichever one this host refuses.

    * No launcher line in *output*: ``None``, nothing run.
    * Off Linux: ``None``, nothing run. The prefixes are the Linux launcher's,
      so elsewhere a matching line can only be the child's own text.
    * No trusted ``true`` on the host: ``None`` -- nothing safe to run.
    * The sandbox cannot even be built for the no-op: the pre-spawn
      :class:`SandboxUnavailableError`'s own ``(kind, detail, remedy)``.
    * The no-op run times out: ``None``.
    * The no-op run exits 0: ``None``. The candidate's line was forged, or the
      refusal was momentary; either way the text reaches the operator as the
      child's own complaint, never as a sandbox verdict.
    * The no-op run exits non-zero: :func:`launcher_refusal` of ITS stderr -- a
      real launcher refusal classified into the triple, or ``None`` when that
      trusted line is host state (a hardlinked credential) rather than a sandbox
      failure.

    The candidate's own text can never become the verdict on any branch. The
    real launcher blocks on a subprocess, so a caller on an event loop runs this
    in a thread. It touches the backend cache for nothing: a spawn's report must
    not move a process-wide verdict, even by way of a real launcher run.
    """
    if launcher_refusal(output) is None:
        return None
    if sys.platform != "linux":
        return None
    noop = platform_compat.trusted_system_bin("true")
    if noop is None:
        return None
    try:
        argv, env, cleanup_path = sandboxed_spawn_argv(
            [noop],
            mode=mode,
            strip_python_env=True,
            extra_hidden_dirs=extra_hidden_dirs,
            extra_visible_dirs=extra_visible_dirs,
        )
    except SandboxUnavailableError as exc:
        # The launcher could not be built at all for the trusted no-op, so the
        # host offers the same verdict a pre-spawn refusal would have carried.
        return (exc.kind, exc.detail, exc.remedy)
    try:
        # The spawn stays in this function, next to the ``sandboxed_spawn_argv``
        # call that built its argv, so the spawn audit can see the routing and
        # the resource ceiling together. ``stdin`` is closed so the child
        # cannot block on input, and stderr is decoded leniently because it is
        # the launcher's diagnostic, not structured data.
        completed = run_limited(
            argv,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TRUSTED_LAUNCHER_TIMEOUT_SECS,
        )
    except subprocess.TimeoutExpired:
        # A launcher that will not return in the budget says nothing certain
        # about the host; the operator still has the candidate's own line.
        return None
    finally:
        if cleanup_path is not None:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(cleanup_path)
    if completed.returncode == 0:
        return None
    return launcher_refusal(completed.stderr)


def credential_mask_applies(mode: str) -> bool:
    """Whether :func:`wrap_argv` would actually APPLY ``extra_hidden_dirs`` for *mode*.

    Exactly two outcomes hand back an UNWRAPPED child, dropping the mask: the ``off``
    tier, and a host with no backend where unsandboxed exec is opted in and no
    governance floor mandates a sandbox (the ``return argv, None`` after
    ``_warn_no_isolation``). Every other path either wraps the argv -- ``namespace``
    and ``sandbox-exec`` both thread ``extra_hidden_dirs`` through -- or REFUSES the
    spawn outright with :class:`SandboxUnavailableError`, and a refusal needs no guard
    because nothing starts.

    Note this predicate is STRICTER than that inventory on one path: with no
    backend it never consults the unsandboxed-exec opt-in, so the opted-in host
    answers False rather than tracking that mutable value. See the branch below.

    This lives here, beside those branches, so a caller whose security argument
    depends on the mask cannot drift from them: a future branch that skips the mask
    is a change to this function, not a silent hole in some other module's copy of
    the reasoning.
    """
    floor = _governance_sandbox_floor()
    effective = _clamp_sandbox_mode_to_floor(mode, floor)
    if effective == "off":
        return False
    # Already inside a Kiro Crew sandbox: a nested re-wrap is impossible by design,
    # wrap_argv passes the argv through (at most an env scrub) and never reaches a
    # backend that could apply the mask. The OUTER sandbox confines the child, but it
    # was built for the tier's own hidden dirs -- which deliberately leave ~/.aws,
    # ~/.ssh and ~/.kube readable for kiro-cli's sake -- so it is NOT a substitute for
    # an adapter-specific credential mask.
    if _inside_kirocrew_sandbox() and _macos_sandbox_state() is not False:
        return False
    if detect_backend(config_mode=effective) != "none":
        return True
    # backend == "none": FAIL CLOSED, reading NO policy value to decide it.
    #
    # Nothing on a host without a backend can carry ``extra_hidden_dirs``, so the
    # only question was whether the spawn would be refused instead -- and every
    # answer to THAT is mutable config read here at preflight and acted on at the
    # spawn. The opt-in was the first such value (an operator opting in between the
    # two let a session that had been told "the mask applies" hand back an unwrapped
    # child); the governance floor is the second, because a ceiling LOOSENED in the
    # same window drops the very refusal that made True safe to report. Both windows
    # close only by refusing to derive this from policy at all: no backend, no mask,
    # so no enforced adapter starts here regardless of how policy moves.
    #
    # The cost is that a no-backend host cannot run an enforced adapter even under a
    # governance floor that forbids unsandboxed execution. That host could not run
    # one anyway -- ``wrap_argv`` cannot satisfy the floor without a backend and
    # raises -- so this changes which layer reports it, not whether it works. Only an
    # ENFORCED adapter reaches here at all: ``enforce_sandbox_floor`` returns early
    # for every harness this core does not enforce, so no first-class path changes.
    return False


def spawn_delegates_masking() -> bool:
    """Whether the agent spawn is DELEGATED, so Crew's own hidden-dir mask never runs.

    :func:`credential_mask_applies` answers whether ``wrap_argv`` would thread
    ``extra_hidden_dirs`` through the backend it selects. That is the right
    question for a mode/backend hole and the wrong one for a DELEGATION hole: on
    macOS with kiro-cli's internal sandbox enabled, a backend is present, so that
    predicate answers True, and yet the spawn is handed to kiro-cli
    (``_delegate_to_kiro_internal_sandbox``) and Crew's mask is not applied at
    all. Native Windows delegates for the same reason with no Crew backend to
    apply.

    Kept here rather than in a caller, for the reason
    :func:`credential_mask_applies` states about itself: a control whose security
    argument depends on the mask must not carry its own copy of when the mask is
    skipped. It is a SEPARATE predicate rather than a widening of that one
    because the two answer different questions, and their existing callers depend
    on the narrower answer -- a caller that only needs "would the backend carry
    the mask" must not start refusing a delegated spawn it never cared about.

    Read-only, and never raises: an unreadable delegation setting reads as
    DELEGATED, which is the fail-closed direction -- a mask that may not run is
    not trusted.
    """
    try:
        if sys.platform == "win32":
            return True
        return bool(sys.platform == "darwin" and kiro_internal_sandbox_enabled())
    except Exception:  # noqa: BLE001 -- an unverifiable setting is delegated, not trusted
        return True


def unconfined_live_agent_pid(pids: "Iterable[int]") -> int | None:
    """The first pid among *pids* that is NOT actually confined, or ``None``.

    Confinement is decided at SPAWN by :func:`wrap_argv`, and ``agent.sandbox``
    is a live setting -- it carries no ``restart=True`` marker, so it reaches the
    running gateway the moment it is saved. A session spawned while the tier was
    ``off`` therefore stays unconfined after the config flips, and a control that
    reads only :func:`configured_sandbox_mode` is asking about the NEXT spawn
    while the hazard is a process already running. This asks about the processes.

    Reuses the platform predicates ``member_memory_auth`` uses for the same
    question rather than inventing a second answer: on Linux a confined child
    holds different user/mount namespaces than the gateway, so MATCHING
    namespaces mean unconfined; on macOS Seatbelt membership is read directly.
    Both return ``None`` when the answer cannot be read, and an unreadable
    process counts as UNCONFINED -- "cannot verify" is not "is confined".

    Native Windows has no Crew confinement to verify, so every pid there answers
    unconfined and a caller whose security argument needs the mask refuses on
    that platform, which is the same posture ``spawn_delegates_masking`` takes.

    On Linux the pid a session records for a namespace-confined spawn is Crew's
    sandbox launcher, and the launcher STAYS in the host namespaces by design: it
    forks, writes the child's uid/gid maps from outside, and waits. Only its child
    unshares and execs the agent. So a pid whose namespaces match the gateway's is
    judged by :func:`_namespace_launcher_child_is_confined` before it counts as
    unconfined; anything that check cannot positively confirm stays unconfined.
    """
    for pid in pids:
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
            return pid if isinstance(pid, int) and not isinstance(pid, bool) else -1
        try:
            if sys.platform == "linux":
                gateway_pid = os.getpid()
                shared = platform_compat.process_namespaces_match(pid, gateway_pid)
                if shared is None:
                    return pid
                if shared and not _namespace_launcher_child_is_confined(pid, gateway_pid):
                    return pid
            elif sys.platform == "darwin":
                if platform_compat.process_is_sandboxed(pid) is not True:
                    return pid
            else:
                return pid
        except Exception:  # noqa: BLE001 -- unreadable is unconfined, never confined
            return pid
    return None


#: Incarnations (pid -> start id) THIS gateway spawned through its Linux
#: namespace launcher, written by the spawn itself. The confinement check below
#: trusts a launcher only from this record: nothing on a process's own command
#: line or image proves Crew launched it, because an unconfined process can exec
#: the same interpreter on a script shaped like the launcher.
_NAMESPACE_LAUNCHERS: dict[int, str] = {}
_NAMESPACE_LAUNCHERS_LOCK = threading.Lock()


def remember_namespace_launcher(
    pid: int, wrapped_argv: "Sequence[str]", launcher_script: str | None
) -> None:
    """Record *pid* when this gateway spawned it through its own namespace launcher.

    Called by the agent spawn with the pid it created, the argv ``wrap_argv``
    returned and the cleanup path ``wrap_argv`` returned beside it. Records only
    when that path is a launcher script the wrap just generated AND the argv runs
    exactly it (``<this interpreter> -I -S <that script> ...``). Both values come
    from the wrap's own return, never from a configured command: an argv token
    that merely looks like a launcher name, with no generated script behind it,
    records nothing.

    Entries whose process is gone or replaced are dropped on each call, so the
    record stays as small as the live set. Never raises: a spawn must not fail on
    bookkeeping, and an unrecorded launcher only reads as unconfined, which is the
    refusing direction.
    """
    try:
        if sys.platform != "linux" or type(pid) is not int or pid <= 1:
            return
        if not isinstance(launcher_script, str) or not os.path.basename(launcher_script).startswith(
            _SANDBOX_ARTIFACT_PREFIX
        ):
            return
        expected = [sys.executable, *_LAUNCHER_INTERPRETER_FLAGS, launcher_script]
        if list(wrapped_argv[: len(expected)]) != expected or len(wrapped_argv) <= len(expected):
            return
        start = platform_compat.get_process_start_id(pid)
        if not start:
            return
        with _NAMESPACE_LAUNCHERS_LOCK:
            for known, known_start in list(_NAMESPACE_LAUNCHERS.items()):
                if platform_compat.get_process_start_id(known) != known_start:
                    del _NAMESPACE_LAUNCHERS[known]
            _NAMESPACE_LAUNCHERS[pid] = start
    except Exception:  # noqa: BLE001 -- unrecorded is unconfined, never confined
        logger.debug("could not record namespace launcher pid %s", pid, exc_info=True)


def _namespace_launcher_child_is_confined(pid: int, gateway_pid: int) -> bool:
    """Whether *pid* is a namespace launcher this gateway spawned, with ONE confined child.

    Every fact must hold, and none is one an agent can supply for itself:

    * this gateway recorded *pid*, at this same incarnation, as a namespace
      launcher it spawned (:func:`remember_namespace_launcher`). A session started
      unconfined records its harness pid, never a launcher, so it cannot pass by
      exec-ing anything later;
    * *pid* has exactly one direct child, and that child names *pid* as its
      parent. A launcher only ever forks once, so a second child refuses;
    * that child's namespaces are not the gateway's
      (``process_namespaces_match`` is ``False``);
    * both incarnations are unchanged after the reads, so pid reuse cannot join
      one process's parentage to another's namespaces.

    A child that has forked but not yet unshared still shares the host
    namespaces, so the launcher reads as unconfined for that moment. Any error
    or unreadable value answers ``False``, which keeps the pid unconfined.
    """
    try:
        launcher_start = platform_compat.get_process_start_id(pid)
        if not launcher_start:
            return False
        with _NAMESPACE_LAUNCHERS_LOCK:
            recorded = _NAMESPACE_LAUNCHERS.get(pid)
        if recorded != launcher_start:
            return False
        children = platform_compat._linux_direct_child_pids(pid, Path("/proc"))
        if children is None or len(children) != 1:
            return False
        child = children[0]
        child_identity = platform_compat.get_process_start_identity(child)
        if child_identity is None or child_identity.ppid != pid:
            return False
        if platform_compat.process_namespaces_match(child, gateway_pid) is not False:
            return False
        if platform_compat.get_process_start_identity(child) != child_identity:
            return False
        return platform_compat.get_process_start_id(pid) == launcher_start
    except Exception:  # noqa: BLE001 -- an unprovable launcher is not a confined one
        return False


async def wrap_argv_async(
    argv: list[str],
    mode: str = "auto",
    *,
    strip_python_env: bool = False,
    forward_ssh_auth_sock: bool = False,
    extra_hidden_dirs: tuple[str, ...] = (),
    extra_visible_dirs: tuple[str, ...] = (),
    extra_private_dirs: tuple[str, ...] = (),
    extra_writable_dirs: tuple[str, ...] = (),
    extra_expose_files: tuple[str, ...] = (),
    exec_cwd: str = "",
    is_kiro_cli: bool | None = None,
    first_party_fixed_argv: bool = False,
    _prepare: Callable[..., tuple[list[str], str | None]] | None = None,
) -> tuple[list[str], str | None]:
    """Cancellation-safe, off-loop sandbox preparation for async spawn paths.

    Sandbox construction probes the host and creates a launcher/profile. It also
    resolves the protected voice-runtime paths on the first call. None of that
    filesystem work may run on a gateway event loop. If the caller is cancelled
    while the worker is finishing, settle it and remove any newly-created
    launcher/profile before propagating cancellation. ``_prepare`` preserves
    each caller's module-local test seam; production callers pass their imported
    :func:`wrap_argv`, and the default is this module's implementation.
    """
    options: dict[str, Any] = {"mode": mode}
    if strip_python_env:
        options["strip_python_env"] = True
    if forward_ssh_auth_sock:
        options["forward_ssh_auth_sock"] = True
    if extra_hidden_dirs:
        options["extra_hidden_dirs"] = extra_hidden_dirs
    if extra_visible_dirs:
        options["extra_visible_dirs"] = extra_visible_dirs
    if extra_private_dirs:
        options["extra_private_dirs"] = extra_private_dirs
    if extra_writable_dirs:
        options["extra_writable_dirs"] = extra_writable_dirs
    if extra_expose_files:
        options["extra_expose_files"] = extra_expose_files
    if exec_cwd:
        options["exec_cwd"] = exec_cwd
    if is_kiro_cli is not None:
        options["is_kiro_cli"] = is_kiro_cli
    if first_party_fixed_argv:
        options["first_party_fixed_argv"] = True
    prepare = functools.partial(wrap_argv if _prepare is None else _prepare, argv, **options)

    def _prepare_wrapped() -> tuple[list[str], dict[str, str], str | None]:
        wrapped, cleanup = prepare()
        return wrapped, {}, cleanup

    wrapped, _unused_env, cleanup = await shielded_prepare_off_loop(_prepare_wrapped)
    return wrapped, cleanup


# Environment keys always scrubbed from an agent-influenced subprocess'
# environment, regardless of sandbox backend. These are the credential-bearing
# names that must never reach a spawn whose command, arguments, or working
# directory the agent (or a hostile MCP-config / repo) can influence. The OS
# sandbox launcher already drops these when a backend is present (see
# ``env_prefixes`` in ``namespace_argv`` / ``sandbox_exec_argv``), but scrubbing
# at the parent level too means the guarantee holds even on the opted-in
# ``sandbox_allow_unsandboxed_exec`` fail-open path where no launcher runs.
# Prefix match via ``startswith`` (mirrors the launcher's env_prefixes check).
_SPAWN_SCRUB_ENV_PREFIXES: list[str] = list(_SENSITIVE_ENV_PREFIXES) + list(_AGENT_DENIED_ENV_KEYS)


def scrub_env(
    env: dict[str, str] | None = None,
    *,
    extra_prefixes: list[str] | None = None,
) -> dict[str, str]:
    """Return a copy of *env* (default ``os.environ``) with credential-bearing
    keys removed.

    Drops every key whose name starts with one of ``_SPAWN_SCRUB_ENV_PREFIXES``
    (AWS secret/session vars, SSH_AUTH_SOCK, GNUPGHOME, GIT_ASKPASS, and the
    Slack/owner tokens seeded into ``os.environ`` for trusted children). Callers use it to
    build the environment for agent-influenced spawns so a spawned process
    cannot read secrets straight out of the inherited environment.

    *extra_prefixes* adds more name prefixes to drop (e.g.
    ``_PYTHON_ENV_PREFIXES`` when the spawn is a foreign Python child).
    """
    prefixes = _SPAWN_SCRUB_ENV_PREFIXES + (extra_prefixes or [])
    src = os.environ if env is None else env
    return {k: v for k, v in src.items() if not any(k.startswith(p) for p in prefixes)}


def without_python_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a copy of *env* (default ``os.environ``) without Kiro Crew's own
    Python interpreter settings, and with every other variable kept.

    Drops each key that starts with one of ``_PYTHON_ENV_PREFIXES``, compared
    case-insensitively. Nothing else is touched: ``SSH_AUTH_SOCK``, the ``AWS_*``
    variables, ``GNUPGHOME`` and ``GIT_ASKPASS`` all survive.

    This is the policy for a child that is NOT an agent but still must not load
    Kiro Crew's interpreter packages instead of its own: the user's own dashboard
    terminal, a third-party pooled MCP backend, and the ``aws`` CLI the voice list
    runs. Those children need the user's credentials, so they must not use
    :func:`scrub_agent_subprocess_env`, which also strips them.
    """
    src = os.environ if env is None else env
    return {
        key: value
        for key, value in src.items()
        if not any(key.upper().startswith(prefix) for prefix in _PYTHON_ENV_PREFIXES)
    }


def agent_env_scrub_prefixes() -> tuple[str, ...]:
    """Name prefixes :func:`scrub_agent_subprocess_env` drops from an agent child.

    DERIVED from the two lists that scrub actually composes, so a caller checking
    "would this variable survive the scrub?" cannot drift from the scrub itself.
    That question has one caller today: the ``agent.deepseek_env`` validator in
    ``acp/client.py`` refuses to inject a name the scrub would strip, because the
    injection happens BEFORE the scrub and the operator would otherwise get a
    harness with no provider key and no error naming why.

    Deliberately NOT the whole truth about a given spawn: ``forward_ssh_auth_sock``
    re-admits ``SSH_AUTH_SOCK`` for an opted-in agent child, so a name matching
    that prefix is reported as scrubbed here even though one spawn shape keeps it.
    The caller's use is a refusal, so answering conservatively refuses a name that
    might have worked rather than accepting one that silently would not.
    """
    return tuple(_SPAWN_SCRUB_ENV_PREFIXES) + tuple(_PYTHON_ENV_PREFIXES)


def scrub_agent_denied_env(env: dict[str, str]) -> dict[str, str]:
    """Return a copy of *env* with gateway-owned channel credentials removed.

    Drops every key matching ``_AGENT_DENIED_ENV_KEYS`` — the Slack/WeCom/
    Telegram tokens and owner id that ``config/loader.load_credentials()`` seeds
    into ``os.environ`` for trusted children only.

    This is the PARENT-level complement to the OS-sandbox launcher scrub. The
    launcher (``namespace_argv`` / ``sandbox_exec_argv``) only strips these keys
    for the ``cc``/``strict`` tiers; on the default ``auto``/``standard`` tier
    they are left in place. The production ACP spawn paths
    (:meth:`AcpRuntime._spawn` / :meth:`AcpClient._spawn`) copy a raw
    ``os.environ`` and call :func:`wrap_argv` directly (not
    :func:`sandboxed_spawn_argv`), so without this scrub the channel credentials
    would be inherited by the agent subprocess on the default tier — reachable
    via ``env`` / ``os.environ`` and usable to control those channel identities
    outside Kiro Crew.

    Unlike :func:`scrub_env`, this deliberately does NOT strip
    ``_SENSITIVE_ENV_PREFIXES`` (AWS/SSH/GPG): the ``standard`` sandbox is
    designed to leave git-over-SSH, the AWS CLI and kubectl usable, so those
    vars must survive the parent scrub. Prefix match via ``startswith`` mirrors
    the launcher's env_prefixes check.
    """
    return {
        k: v for k, v in env.items() if not any(k.startswith(p) for p in _AGENT_DENIED_ENV_KEYS)
    }


def scrub_agent_subprocess_env(
    env: dict[str, str] | None = None, *, forward_ssh_auth_sock: bool = False
) -> dict[str, str]:
    """Return the full environment scrub required for a Kiro/ACP child.

    This is the parent-side equivalent of the OS launchers' sensitive-variable
    removal plus ``strip_python_env=True``. It is mandatory for Windows Kiro
    delegation because Windows cannot express the POSIX ``env -u`` prefix, and
    keeping it on every platform makes delegated and wrapped ACP spawns inherit
    the same environment policy.

    This is the AGENT enforcement point, so the SSH_AUTH_SOCK
    forward opt-in is applied HERE rather than in the generic :func:`scrub_env`,
    which also serves non-agent callers (tailscale host children,
    ``sandboxed_spawn_argv`` spawns) that must keep the socket scrubbed. The
    decision is passed in as an already-resolved boolean (the caller resolves
    :func:`_forward_ssh_auth_sock` once in its off-loop environment-prep hop, so
    no synchronous config read runs on the asyncio event loop here); it defaults
    False, so a caller that does not opt in scrubs the socket as before. When
    set, the socket value is preserved across the scrub for the agent child
    alone.
    """
    scrubbed = scrub_env(env, extra_prefixes=_PYTHON_ENV_PREFIXES)
    src = os.environ if env is None else env
    if forward_ssh_auth_sock and "SSH_AUTH_SOCK" in src:
        # scrub_env removed it unconditionally; re-add the socket for the agent
        # child only. Restricted to the exact key, so nothing else the generic
        # scrub dropped is reintroduced.
        scrubbed["SSH_AUTH_SOCK"] = src["SSH_AUTH_SOCK"]
    return scrubbed


def sandboxed_spawn_argv(
    argv: list[str],
    mode: str = "standard",
    *,
    env: dict[str, str] | None = None,
    strip_python_env: bool = False,
    extra_hidden_dirs: tuple[str, ...] = (),
    extra_visible_dirs: tuple[str, ...] = (),
    extra_private_dirs: tuple[str, ...] = (),
    extra_writable_dirs: tuple[str, ...] = (),
    first_party_fixed_argv: bool = False,
    is_kiro_cli: bool | None = None,
) -> tuple[list[str], dict[str, str], str | None]:
    """Single chokepoint for agent-influenced subprocess spawns.

    Wraps *argv* with the OS-level sandbox (:func:`wrap_argv`) AND returns a
    credential-scrubbed environment (:func:`scrub_env`), so every caller gets
    both the filesystem-isolation and the environment-hiding layer without
    having to remember to apply each separately. This is the wrapper the
    subprocess-spawn audit test (``test/test_spawn_audit.py``) requires every
    agent-influenced spawn in ``src/kiro_crew`` to route through.

    Args:
        argv: Original command + args.
        mode: Sandbox mode passed to :func:`wrap_argv` (default ``"standard"``:
            hides non-workflow credential dirs while leaving git-over-SSH and
            the AWS CLI usable).
        env: Base environment to scrub (default ``os.environ``). Pass a
            pre-augmented env (e.g. with a resolved ``PATH``) to have the scrub
            applied on top of it.
        strip_python_env: Strip ``PYTHONPATH``/``PYTHONHOME`` so a foreign
            Python child does not inherit Kiro Crew's interpreter paths. Applied
            BOTH inside :func:`wrap_argv`'s launcher AND to the returned env, so
            the strip holds even on the fail-open path where no launcher runs.
        extra_hidden_dirs: Additional absolute directory trees the caller needs
            hidden in both the macOS Seatbelt and Linux namespace profiles.
        extra_visible_dirs: Trusted paths that must remain visible when an
            otherwise-hidden parent contains them (the whole parent's mask is lifted).
        extra_private_dirs: The spawn's OWN directories inside a hidden tree
            (its ``agent_scratch`` dir under the masked scratch root). Re-exposed
            read-write as a window; the parent's mask and every sibling stay hidden.
        extra_writable_dirs: Self-derived scratch directories inside the sealed
            runtime parent that the child must be able to write — see
            :func:`wrap_argv`. Validated; refused candidates degrade to the
            sealed behavior rather than blocking the spawn.
        is_kiro_cli: Threaded to :func:`wrap_argv`. Whether the child carries
            its own internal sandbox, which cannot nest inside Crew's. Exposed
            here so a DELEGATING spawn can route through this chokepoint instead
            of calling :func:`wrap_argv` directly: without it, such a caller had
            to choose between the chokepoint (and silently lose the delegation
            decision, asking for a tier the child cannot honour) and its own
            hand-rolled wrap+scrub+scope (and drift from the contract every other
            spawn gets). ``None`` keeps ``wrap_argv``'s own classification.
        first_party_fixed_argv: Threaded to :func:`wrap_argv`. True ONLY for
            spawns whose full argv is derived inside this package with zero
            agent/repo/user-config influence; every passing site must be
            allowlisted in ``test/test_spawn_audit.py::FIRST_PARTY_SPAWNS``.
            See :func:`wrap_argv` for the no-backend carve-out it gates.

    Returns:
        ``(wrapped_argv, scrubbed_env, cleanup_path_or_None)``. The caller MUST
        pass *scrubbed_env* as the subprocess ``env=`` and unlink *cleanup_path*
        (a temp launcher/profile) after the child exits.
    """
    if extra_hidden_dirs or extra_visible_dirs or extra_private_dirs or extra_writable_dirs:
        wrapped, cleanup = wrap_argv(
            argv,
            mode=mode,
            strip_python_env=strip_python_env,
            extra_hidden_dirs=extra_hidden_dirs,
            extra_visible_dirs=extra_visible_dirs,
            extra_private_dirs=extra_private_dirs,
            extra_writable_dirs=extra_writable_dirs,
            first_party_fixed_argv=first_party_fixed_argv,
            is_kiro_cli=is_kiro_cli,
        )
    else:
        wrapped, cleanup = wrap_argv(
            argv,
            mode=mode,
            strip_python_env=strip_python_env,
            first_party_fixed_argv=first_party_fixed_argv,
            is_kiro_cli=is_kiro_cli,
        )
    # ``wrap_argv`` only strips PYTHONPATH/PYTHONHOME inside the launcher script,
    # so on the fail-open path (no sandbox backend, opted-in unsandboxed exec) it
    # returns argv unmodified and the strip never happens. Apply the same strip
    # to the scrubbed env here so ``strip_python_env=True`` holds regardless of
    # whether a backend is available.
    extra = _PYTHON_ENV_PREFIXES if strip_python_env else None
    scrubbed = scrub_env(env, extra_prefixes=extra)
    # The cgroup wrapper prepended below needs the user session bus in the
    # environment it is spawned with, so restore its locator vars after the
    # scrub. Callers that pass a strict allowlist env (e.g. the source-provider
    # CLI) otherwise hand systemd-run an environment it cannot reach the bus
    # from and the spawn dies before exec'ing the real command.
    patched, injected = cgroup_scope_bus_env(scrubbed)
    if injected:
        # The locators are the WRAPPER's capability, never the child's: a bus
        # address in the sandboxed process is a sandbox escape (it can ask the
        # user systemd manager to start a unit that runs outside the namespace).
        # So they live only until systemd-run has used them — an ``env -u`` shim
        # inside the scope drops them again before the real command execs. If no
        # ``env`` binary is available we fail CLOSED: keep the scrubbed env, let
        # the wrapper fail loudly, and never hand the child a live bus.
        unset = _unset_env_argv(injected)
        if unset is None:
            logger.warning(
                "SECURITY: no `env` binary to drop %s inside the cgroup scope; "
                "not forwarding the bus locators (systemd-run will fail rather "
                "than leak a user-bus address into the sandboxed child).",
                ", ".join(injected),
            )
        else:
            scrubbed = patched
            wrapped = [*unset, *wrapped]
    # cgroup v2 scope (OUTERMOST layer): bound the spawned process tree with
    # pids.max + memory.max. Applied here so every sandboxed_spawn_argv caller
    # gets the fork-bomb / memory-DoS ceiling without threading it through each
    # site. No-op (with a one-time loud warning) where cgroup delegation is
    # unavailable. Safe re: the cleanup path — that is returned separately, not
    # re-derived from an argv index, so prepending systemd-run does not disturb
    # it. See docs/architecture/resource-protection.md.
    wrapped = cgroup_scope_argv(wrapped)
    # Positive-identity marker for the orphan sweep: every tree spawned through
    # this chokepoint (and its descendants, via env inheritance) is identifiable
    # as spawned by Kiro Crew even when its cmdline carries no Kiro Crew fingerprint
    # (e.g. ``npx @playwright/mcp``).
    scrubbed[KIROCREW_SPAWNED_ENV] = KIROCREW_SPAWNED_VALUE
    # Marks this tree as TOOL work -- a build, an ``npx`` install, a ``git``/``gh``
    # read, a provisioning run. The runtime reconciler reads it back from the kernel's
    # exec-time copy to leave such a tree out of its kill-candidate population: a tool
    # subprocess lands in the agent slice that reconciler compares against, carries the
    # inherited KIROCREW_SPAWNED marker, and is in no membership record, so once it
    # outlives the age floor the argv0 basename test is the only thing between it and a
    # signal. This marker is exec-time evidence instead: a same-uid process can set its
    # own argv or write any file, and cannot alter a running process's environment.
    #
    # It describes the TREE, not each process in it, and this function is not limited
    # to non-harness argv: ``is_kiro_cli`` exists precisely so a DELEGATING spawn can
    # route here, and callers do (a pod child probe, an unattended fix-authoring
    # agent). Since the marker is inherited, a harness can carry it without being tool
    # work, so the reconciler's exclusion requires this marker AND a non-harness argv0
    # -- see ``runtime_reconcile.RuntimeReconciler._unowned``. Stamping it here is
    # therefore safe for any argv: it never decides an exclusion on its own.
    scrubbed[KIROCREW_SANDBOX_TOOL_ENV] = KIROCREW_SANDBOX_TOOL_VALUE
    return wrapped, scrubbed, cleanup


async def shielded_prepare_off_loop(
    prepare: Callable[[], tuple[list[str], dict[str, str], str | None]],
    *,
    executor: ThreadPoolExecutor | None = None,
) -> tuple[list[str], dict[str, str], str | None]:
    """Run a spawn-preparation callable off the loop, shielded from cancellation.

    ``prepare`` must follow the :func:`sandboxed_spawn_argv` contract: it returns
    ``(wrapped_argv, scrubbed_env, cleanup_path_or_None)`` where the third element
    is a temp launcher/profile the CALLER must unlink after the child exits.

    Every async caller reaches the sync chokepoint through a worker hop.
    Cancelling that hop abandons the returned tuple while the worker still
    materializes the launcher/profile, leaking the temp file forever.  Shielding
    the hop keeps the worker's result recoverable: on cancellation we wait for
    the thread to settle, unlink the launcher it created, and re-raise.

    A REPEAT cancellation landing on a bare recovery ``await`` is a
    ``BaseException`` that would abandon the recovery before the unlink runs,
    leaking the materialized launcher.  The settle-then-unlink therefore
    runs as its own task, shielded from cancellations aimed at this caller; each
    absorbed repeat is ``uncancel()``-ed so an enclosing ``asyncio.timeout``
    still reports ``TimeoutError``, and the ORIGINAL cancellation is re-raised
    once the launcher is gone.

    ``executor`` keeps pool choice with the CALLER, because which pool absorbs a
    wedged preparation is per-site policy, not a shield concern: the chokepoint
    can cold-probe the sandbox backend with a synchronous subprocess, and
    :mod:`kiro_crew.executors` partitions blocking work into named pools so such
    a probe cannot occupy the workers another subsystem (the orphan-reaping
    sweep) needs.  Defaulting it to ``None`` — the loop's default pool, via
    ``asyncio.to_thread`` — would silently collapse that partition for callers
    that had chosen a pool, so every site that had one passes it explicitly.
    """

    Prepared = tuple[list[str], dict[str, str], str | None]
    task: asyncio.Future[Prepared]
    if executor is None:
        task = asyncio.ensure_future(asyncio.to_thread(prepare))
    else:
        task = asyncio.get_running_loop().run_in_executor(executor, prepare)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:

        async def _settle_then_unlink() -> None:
            cleanup: str | None = None
            with contextlib.suppress(Exception, asyncio.CancelledError):
                _, _, cleanup = await task
            if not cleanup:
                return
            target = cleanup

            def _unlink() -> None:
                with contextlib.suppress(OSError):
                    os.unlink(target)

            if executor is None:
                await asyncio.to_thread(_unlink)
            else:
                await asyncio.get_running_loop().run_in_executor(executor, _unlink)

        current = asyncio.current_task()
        recovery = asyncio.create_task(_settle_then_unlink())
        while not recovery.done():
            try:
                await asyncio.shield(recovery)
            except asyncio.CancelledError:
                uncancel = getattr(current, "uncancel", None)  # 3.11+
                if uncancel is not None:
                    uncancel()
            except Exception:
                logger.warning(
                    "sandbox launcher cleanup failed after cancellation",
                    exc_info=True,
                )
        raise


async def sandboxed_spawn_argv_async(
    argv: list[str],
    mode: str | None = None,
    *,
    env: dict[str, str] | None = None,
    strip_python_env: bool = False,
    extra_hidden_dirs: tuple[str, ...] = (),
    extra_visible_dirs: tuple[str, ...] = (),
    extra_private_dirs: tuple[str, ...] = (),
    extra_writable_dirs: tuple[str, ...] = (),
    first_party_fixed_argv: bool = False,
    executor: ThreadPoolExecutor | None = None,
    _prepare: Callable[..., tuple[list[str], dict[str, str], str | None]] | None = None,
) -> tuple[list[str], dict[str, str], str | None]:
    """Prepare a sandboxed spawn safely off-loop, retaining caller test seams."""
    # Preserve the long-standing injectable preparation seam: many focused
    # callers replace ``sandboxed_spawn_argv`` with a narrow ``(argv, *, env)``
    # test double. ``None`` means the caller omitted the argument, in which case
    # the synchronous function supplies its own ``standard`` default. An
    # explicitly supplied value -- including ``standard`` -- is forwarded.
    options: dict[str, Any] = {}
    if mode is not None:
        options["mode"] = mode
    if env is not None:
        options["env"] = env
    if strip_python_env:
        options["strip_python_env"] = True
    if extra_hidden_dirs:
        options["extra_hidden_dirs"] = extra_hidden_dirs
    if extra_visible_dirs:
        options["extra_visible_dirs"] = extra_visible_dirs
    if extra_private_dirs:
        options["extra_private_dirs"] = extra_private_dirs
    if extra_writable_dirs:
        options["extra_writable_dirs"] = extra_writable_dirs
    if first_party_fixed_argv:
        options["first_party_fixed_argv"] = True
    return await shielded_prepare_off_loop(
        functools.partial(
            sandboxed_spawn_argv if _prepare is None else _prepare,
            argv,
            **options,
        ),
        executor=executor,
    )


# ``systemd-run --user`` finds the caller's session bus through these two
# variables. They hold a socket path owned by the current user, not a
# credential, and they are a dependency of the WRAPPER rather than of the
# sandboxed child — which is why they are restored after the credential scrub
# and then dropped again inside the scope (see :func:`_unset_env_argv`) instead
# of being added to any caller's env allowlist.
_CGROUP_SCOPE_BUS_ENV_KEYS = ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")


def cgroup_scope_bus_env(env: dict[str, str]) -> tuple[dict[str, str], tuple[str, ...]]:
    """Return ``(env_with_bus_locators, keys_this_call_added)``.

    Only applied when :func:`cgroup_scope_argv` actually wraps the spawn (same
    :func:`_probe_cgroup_scope` gate), so hosts that never see a ``systemd-run``
    prefix keep the exact environment their caller asked for and the returned
    key tuple is empty.

    Values are taken from the gateway's own environment and only fill keys the
    caller left unset — an explicit value in *env* always wins and is NOT
    reported as injected, so a caller that deliberately passes a bus address
    keeps it end to end. The probe requires ``XDG_RUNTIME_DIR`` in
    ``os.environ``, so whenever wrapping is applied at least that locator is
    available to forward.

    The returned key tuple is what lets the caller drop exactly what it added
    again *inside* the scope: the locators must reach ``systemd-run``, but must
    not reach the sandboxed child — a live user-bus address there is enough to
    ask the user systemd manager to start a unit that runs outside the sandbox.

    Without this, a caller that builds its child environment from a strict
    allowlist (``dashboard/handlers/source_providers.py`` is the live example)
    hands ``systemd-run`` an environment with no reachable bus; it exits 1 with
    ``Failed to connect to bus: No medium found`` and the wrapped command never
    runs at all.
    """
    available, _ = _probe_cgroup_scope()
    if not available:
        return env, ()
    patched = dict(env)
    injected: list[str] = []
    for key in _CGROUP_SCOPE_BUS_ENV_KEYS:
        if patched.get(key):
            continue
        value = os.environ.get(key)
        if value:
            patched[key] = value
            injected.append(key)
    return patched, tuple(injected)

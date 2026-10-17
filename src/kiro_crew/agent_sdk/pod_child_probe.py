"""Can this environment actually start an agent child? -- asked through the SDK.

``pod.runtime`` needs one answer before a pod declares itself up: would the agent
backend bootstrap under the environment this pod just built? Answering it requires
the real spawn path (executable resolution, the pod ``HOME`` remap, the
bundle-binary substitution), all of which live in the ACP layer -- and application
code may not import that layer directly (``scripts/check_agent_sdk_boundary.py``;
``kiro_crew.agent_sdk`` is the one sanctioned surface). So the probe lives HERE,
where reaching into ``kiro_crew.acp`` is allowed, and the pod side consumes a
verdict instead of a transport.

The verdict is data, not an exception: refusing a boot is a POD concept
(``PodError`` + a recorded terminal exit), so the classification is returned and
the caller owns the policy.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
from typing import TYPE_CHECKING, Mapping, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Iterator

from kiro_crew.subprocess_utf8 import UTF8_TEXT

#: The harness started and is serving (or reached its own login gate). Boot on.
PROBE_VIABLE = "viable"
#: The harness started but has no credential. Boot on -- seeding is best-effort and
#: a signed-out host must still get a pod it can sign in inside.
PROBE_SIGNED_OUT = "signed_out"
#: No agent backend installed on this host. NOT a failure of this pod.
PROBE_UNAVAILABLE = "unavailable"
#: The child could not bootstrap. The caller must refuse the boot.
PROBE_DEAD = "dead"

#: Stderr markers for a harness that started and reached its own login gate.
SIGNED_OUT_MARKERS = ("not logged in", "kiro-cli login")


@contextlib.contextmanager
def _process_env(env: Mapping[str, str]) -> "Iterator[None]":
    """Run the block with *env* as the process environment, then restore.

    The pod's config context, borrowed for the length of one spawn PREPARATION.
    Everything the sandbox chokepoint decides -- the tier from ``agent.sandbox``,
    the ``sandbox_allow_unsandboxed_exec`` opt-in -- is read from config, and
    config resolves through ``KIROCREW_HOME`` in ``os.environ`` at call time. The
    real spawn reads that inside the pod gateway process; this probe runs in the
    process about to exec it, so without the swap the two read DIFFERENT configs
    and the probe's verdict is about the host rather than the pod.

    Deliberately narrow: it wraps preparation only, never the child's lifetime, so
    the window is a few filesystem reads rather than the whole probe. The pod boot
    path is single-threaded at this point (the gateway is not up yet), which is
    what makes mutating the process environment safe here and would not make it
    safe in the served gateway.
    """
    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update(env)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _release(cleanup: object) -> None:
    """Drop whatever ``wrap_argv`` allocated (a launcher temp file), never raising."""
    if callable(cleanup):
        try:
            cleanup()
        except Exception:  # pragma: no cover - defensive: cleanup must not fail a boot
            pass


class ProbeResult(NamedTuple):
    """One probe outcome: what happened, what the child said, and who it was."""

    verdict: str
    detail: str
    child: str
    home: str


def probe_pod_child_bootstrap(pod_env: Mapping[str, str], *, timeout_secs: float) -> ProbeResult:
    """Spawn the pod's agent child once and classify whether it bootstrapped.

    Applies the SAME legs production applies, so the probe is a faithful rehearsal
    rather than a privileged shortcut: the executable is resolved the same way and
    passed through ``apply_pod_bundle_spawn`` (so the bundle-binary substitution a
    pod child depends on is exercised), the argv is then wrapped by
    ``sandbox.wrap_argv`` and the environment is scrubbed by
    ``scrub_agent_subprocess_env``. An earlier revision spawned the resolved binary
    raw, which meant an UNCONFINED child holding the gateway's own ``AWS_*``
    material ran on the boot path -- and its docstring still claimed to be "the
    real spawn path" while skipping the two legs that make that path safe.
    Confinement degrades per ``wrap_argv``'s own contract on a host with no sandbox
    backend; it is never silently dropped here.

    Anything cheaper than the real subcommand (a bare ``--version``) short-circuits
    before the harness bootstraps and would report a broken revision as healthy.

    Four outcomes, and only ``PROBE_DEAD`` should stop a boot:

    * still running when *timeout_secs* expires -- a healthy child waiting on
      stdin. Killed and reported viable.
    * exited ``0`` -- also viable. The child is handed a CLOSED stdin, so a harness
      that bootstrapped fine reads EOF and shuts down tidily; treating that as a
      failure is a false refusal that blocks a perfectly good pod.
    * exited non-zero naming its own login gate -- started fine, no credential.
    * exited non-zero any other way -- could not bootstrap.
    """
    from kiro_crew.acp.client import (
        KIRO_CLI_SUBCMD,
        _apply_pod_home_remap,
        _resolve_kiro_bin,
        apply_pod_bundle_spawn,
    )
    from kiro_crew.acp_backends import ACP_BACKEND_KIRO
    from kiro_crew.sandbox import (
        RLIMIT_PROFILE_SESSION_HOST,
        SandboxCeilingUnsealable,
        SandboxUnavailableError,
        configured_sandbox_mode,
        popen_limited,
        sandboxed_spawn_argv,
    )

    resolved = _resolve_kiro_bin(environ=pod_env)
    if resolved is None:
        return ProbeResult(PROBE_UNAVAILABLE, "", "", "")
    child_env = _apply_pod_home_remap(dict(pod_env), pod_home_remap=True)
    argv, delegate = apply_pod_bundle_spawn(
        [resolved, KIRO_CLI_SUBCMD], backend=ACP_BACKEND_KIRO, environ=child_env
    )
    home = child_env.get("HOME", "")
    # ONE spawn contract, prepared in the POD's config context.
    #
    # This routes through ``sandbox.sandboxed_spawn_argv`` -- the same documented
    # chokepoint the audit requires of every agent-influenced spawn -- rather than
    # re-applying wrap + scrub + cgroup scope by hand. Hand-rolling them was not
    # merely duplication: the chokepoint also decides the sandbox TIER and reads the
    # ``sandbox_allow_unsandboxed_exec`` opt-in from CONFIG, and config resolves
    # through ``KIROCREW_HOME`` in the AMBIENT environment at call time. This probe
    # runs in the process that is about to ``exec`` the pod gateway, so its ambient
    # ``KIROCREW_HOME`` is still the HOST's while the real spawn -- which happens
    # inside the pod gateway process -- reads the POD's. An operator with the
    # unsandboxed opt-in set in HOST config and not in POD config therefore got a
    # probe that succeeded and an agent that could never spawn: healthy status, dead
    # pod. Applying ``pod_env`` for the duration of the preparation is what makes the
    # probe's answer a statement about the pod.
    with _process_env(pod_env):
        try:
            mode = configured_sandbox_mode()
            argv, child_env, cleanup = sandboxed_spawn_argv(
                argv,
                mode,
                env=child_env,
                strip_python_env=True,
                is_kiro_cli=delegate,
            )
        except (SandboxUnavailableError, SandboxCeilingUnsealable, OSError) as exc:
            # A sandbox that cannot be PREPARED is a DEAD verdict, not a crash and not
            # a skip. The real ACP spawn reaches ``wrap_argv_async`` with these same
            # options, so a host that cannot build a sandbox fails the child's spawn
            # identically -- every agent turn in the pod would fail while ``/health``
            # answered 200, which is the exact condition this probe exists to catch.
            # Returning DEAD routes it through the caller's existing refusal, so the
            # boot exits with a RECORDED terminal code instead of the unhandled
            # exit 1 that made the service manager restart into the same failure every
            # few seconds -- the loop the terminal-exit work was built to prevent.
            #
            # ``kind`` is carried through because it is the operator's decision input:
            # "permanent" means this host needs ``sandbox_allow_unsandboxed_exec`` or a
            # backend installed, while "transient" (namespace exhaustion, ENOSPC) means
            # the same ``pod up`` can succeed later. Both refuse -- serving a pod whose
            # agent cannot spawn is the failure mode, not the retry budget -- but the
            # recorded reason says which one happened rather than leaving the operator
            # to guess from a bare exit status.
            kind = getattr(exc, "kind", "") or type(exc).__name__
            detail = getattr(exc, "detail", "") or str(exc)
            _release(None)
            return ProbeResult(
                PROBE_DEAD,
                f"could not be sandboxed on this host ({kind}): {detail}",
                argv[0],
                home,
            )
    # Resource limits come from ``popen_limited``, which applies them AFTER exec.
    # A synchronous ``preexec_fn`` forks the threaded gateway and runs Python in
    # the child before exec, which the repo's fork-safety guard forbids outright --
    # and the cgroup v2 scope the audit wants is already applied INSIDE the
    # chokepoint above, so the two guards that looked opposed are satisfied by the
    # same move rather than by a compromise.
    try:
        proc = popen_limited(
            argv,
            profile=RLIMIT_PROFILE_SESSION_HOST,
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **UTF8_TEXT,
        )
    except OSError as exc:
        _release(cleanup)
        return ProbeResult(PROBE_DEAD, f"could not be started: {exc}", argv[0], home)
    try:
        _out, err = proc.communicate(timeout=timeout_secs)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive reap
            pass
        _release(cleanup)
        return ProbeResult(PROBE_VIABLE, "", argv[0], home)
    _release(cleanup)
    tail = " ".join((err or "").split())[-500:]
    if proc.returncode == 0:
        return ProbeResult(PROBE_VIABLE, tail, argv[0], home)
    if any(marker in tail for marker in SIGNED_OUT_MARKERS):
        return ProbeResult(PROBE_SIGNED_OUT, tail, argv[0], home)
    return ProbeResult(
        PROBE_DEAD,
        f"exited immediately (rc={proc.returncode}) instead of serving ACP. "
        f"stderr: {tail or '<empty>'}",
        argv[0],
        home,
    )

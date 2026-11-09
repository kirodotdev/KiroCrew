"""``kirocrew sandbox explain -- COMMAND``: run one command the way an agent child runs it.

The command goes through the same gates an agent's shell command meets on this host:
the harness preflight (an enforced harness is refused on a tier that would spawn it
unmasked, otherwise its credential mask applies), the command deny gate (the platform
security authority over the effective rule set, the operator's ``auto_deny_tools`` and
the companion overlay) and the OS sandbox at the operator's ``agent.sandbox`` tier,
clamped by any governance floor. The harness is the one ``agent.acp_backend`` selects.
The mask and the files it re-exposes come from
:mod:`kiro_crew.agent_sdk.tool_gate`. The ssh-agent forwarding verdict, the wrap, the
cgroup scope and the environment scrub come from the functions the agent launch calls,
in its order (``_forward_ssh_auth_sock``, :func:`kiro_crew.sandbox.wrap_argv`,
``cgroup_scope_argv``, ``scrub_agent_subprocess_env``), and the environment starts from
the agent's own base (``agent.env`` and the contributed PATH directories). The command's stderr streams as
it runs, its stdout and stdin pass straight through, and a short report follows,
listing what the tier plan and the harness mask hide, seal read-only and refuse.

The report's path lists come from the confinement plan (:class:`ConfinementPlan`) for
this host, the harness mask included. A spawn may add its own masks on top (a session's
scratch windows, an app's declared targets); those are per-caller and do not appear here.
"""

from __future__ import annotations

import codecs
import contextlib
import os
import shlex
import subprocess
import sys
import threading
from collections import deque
from collections.abc import Callable
from typing import IO, Any

from kiro_crew import sandbox, sandbox_plan
from kiro_crew.agent_sdk.agent_env import agent_env_overlay
from kiro_crew.agent_sdk.tool_gate import (
    ToolGateUnroutable,
    adapter_expose_files,
    adapter_hidden_credential_dirs,
    enforce_sandbox_floor,
    label_for,
)
from kiro_crew.config import KiroCrewConfig
from kiro_crew.env import append_mcp_path_dirs, augmented_path
from kiro_crew.hooks import HookManager, hooks_config_from_config_dict
from kiro_crew.platform import current_context
from kiro_crew.sandbox import (
    _forward_ssh_auth_sock,
    cgroup_scope_argv,
    configured_sandbox_mode,
    corroborate_launcher_refusal,
    detect_backend,
    effective_sandbox_mode,
    popen_limited,
    scrub_agent_subprocess_env,
    wrap_argv,
)

#: Exit status when the command never started: the harness preflight or a deny rule
#: refused it, or the sandbox (or the OS) refused the launch. 126 is the shell's
#: "found but cannot execute".
REFUSED_EXIT = 126

#: Bytes of the command's stderr kept for the launcher-refusal check; the live stream
#: is not capped.
STDERR_TAIL_BYTES = 64 * 1024

#: Seconds the stderr copy may run on after the command exits, for output still in
#: the pipe. It ends at the pipe's EOF, which normally comes with the exit; a process
#: the command left running in the background can hold the pipe open, and explain
#: does not wait for it.
STDERR_DRAIN_SECS = 2.0

_USAGE = "Usage: kirocrew sandbox explain -- COMMAND [ARGS...]"

# ``detect_backend`` names the OS mechanism; the planner names the renderer.
_PLAN_BACKENDS = {
    "namespace": sandbox_plan.BACKEND_NAMESPACE,
    "sandbox-exec": sandbox_plan.BACKEND_SEATBELT,
}


def _configured_harness() -> str:
    return KiroCrewConfig.load().agent.acp_backend


def harness_mask(harness: str, mode: str) -> tuple[tuple[str, ...], tuple[str, ...], str]:
    """``(hidden, re-exposed, refusal)`` for *harness*'s own child at *mode*.

    The agent launch's preflight for that harness: its credential mask, the files it
    keeps readable inside the mask, and the refusal its session start gives when the
    tier would spawn it unmasked (``""`` when it starts). All empty for a harness the
    core does not enforce.
    """
    hidden = tuple(adapter_hidden_credential_dirs(harness))
    expose = tuple(adapter_expose_files(harness, hidden))
    try:
        enforce_sandbox_floor(harness, mode)
    except ToolGateUnroutable as exc:
        return hidden, expose, str(exc)
    return hidden, expose, ""


def spawn_plan(
    backend: str,
    level: str,
    forward: bool,
    hidden: tuple[str, ...] = (),
    expose: tuple[str, ...] = (),
) -> sandbox_plan.ConfinementPlan:
    """The plan an agent spawn at *level* gets from *backend* on this host.

    Planned the way the agent wrap plans it: the Python-env strip on, the ssh-agent
    verdict *forward* it applied, and the harness mask *hidden* with the files
    *expose* keeps readable inside it.
    """
    return sandbox._spawn_plan(
        _PLAN_BACKENDS[backend],
        level,
        strip_python_env=True,
        forward_ssh_auth_sock=forward,
        extra_hidden_dirs=hidden,
        extra_expose_files=expose,
    )


def _plan_lines(
    backend: str, level: str, forward: bool, hidden: tuple[str, ...], expose: tuple[str, ...]
) -> list[str]:
    if backend not in _PLAN_BACKENDS:
        return ["confinement: none (no sandbox backend for this tier)"]
    plan = spawn_plan(backend, level, forward, hidden, expose)
    masks = [mask for mask in plan.masks if not mask.cancelled]
    tier = list(dict.fromkeys([*(m.path for m in masks if m.origin != "caller"), *plan.files]))
    harness = [
        path
        for path in dict.fromkeys(m.path for m in masks if m.origin == "caller")
        if path not in tier
    ]
    lines = [f"hidden: {path}" for path in tier]
    # The strict tier hides ``~/.ssh`` through ``hide_ssh`` rather than a mask, and both
    # backends keep ``known_hosts`` readable. A mask over the same path hides it whole.
    if plan.hide_ssh and plan.ssh_dir not in {*tier, *harness}:
        lines.append(f"hidden: {plan.ssh_dir} (known_hosts stays readable)")
    lines += [f"hidden (harness): {path}" for path in harness]
    lines += [f"readable inside a mask: {path}" for path, _name in plan.expose]
    # Seatbelt seals the runtime parents and a lifted read-only mask outside
    # ``plan.readonly``; the namespace plan lists them there too.
    sealed = [
        *plan.readonly,
        *plan.runtime_parents,
        *(m.path for m in plan.masks if m.cancelled and m.read_only_when_cancelled),
    ]
    lines += [f"read-only: {path}" for path in dict.fromkeys(sealed)]
    if plan.denied_socket_patterns:
        lines += [f"network: refused socket {p}" for p in plan.denied_socket_patterns]
    elif backend == "namespace":
        lines.append("network: not restricted (the namespace sandbox shares the host network)")
    else:
        lines.append("network: not restricted")
    for refusal in plan.refusals:
        try:
            text = refusal.message % refusal.args if refusal.args else refusal.message
        except (TypeError, ValueError):
            text = refusal.message
        lines.append(f"plan refused: {text}")
    return lines


def _hooks_section() -> dict:
    return KiroCrewConfig.load().hooks or {}


def _deny_reason(command: str) -> str | None:
    """The refusal the agent's tool gate would give *command*, or ``None``.

    The same authority and inputs the gate uses: the platform security authority (so
    the companion overlay applies), the operator's ``auto_deny_tools`` globs, and the
    effective regex tier with governance pins.
    """
    config = hooks_config_from_config_dict(_hooks_section())
    return current_context().security.is_denied(
        command,
        config.auto_deny_tools,
        denied_regexes=HookManager(config).effective_denied_regexes(),
    )


def _agent_base_env() -> dict[str, str]:
    """The environment an agent child starts from, before the sandbox scrub."""
    env = {**os.environ, **agent_env_overlay()}
    env["PATH"] = append_mcp_path_dirs(augmented_path(env.get("PATH", "")))
    return env


def _pump_stderr(proc: Any, sink: IO[str] | None = None) -> tuple[int, str, bool]:
    """Copy *proc*'s stderr to *sink* until *proc* exits; ``(exit code, tail, still held)``.

    A reader thread copies each chunk as it arrives, so a prompt on stderr is visible
    while the command waits. The command's exit ends the wait, not the pipe's EOF: a
    background process it started can hold the pipe open, so after the exit the copy
    gets :data:`STDERR_DRAIN_SECS` and then stops writing. *still held* says the pipe
    was still open then. The tail is the last :data:`STDERR_TAIL_BYTES` bytes, so a
    chatty command cannot grow memory.
    """
    out = sys.stderr if sink is None else sink
    tail: deque[int] = deque(maxlen=STDERR_TAIL_BYTES)
    lock = threading.Lock()
    copying = True
    # The reader owns this descriptor and alone closes it, so closing proc.stderr can
    # never hand it a reused descriptor number.
    fd = os.dup(proc.stderr.fileno())

    def _copy() -> None:
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            while chunk := os.read(fd, 4096):
                with lock:
                    tail.extend(chunk)
                    if copying:
                        # A failed terminal write must not stop the draining, or the
                        # command would block on a full pipe.
                        with contextlib.suppress(OSError, ValueError):
                            out.write(decoder.decode(chunk))
                            out.flush()
        finally:
            os.close(fd)

    reader = threading.Thread(target=_copy, name="sandbox-explain-stderr", daemon=True)
    reader.start()
    code = proc.wait()
    reader.join(STDERR_DRAIN_SECS)
    with lock:
        copying = False
        return code, bytes(tail).decode("utf-8", "replace"), reader.is_alive()


def _launch(
    argv: list[str],
    mode: str,
    popen: Callable[..., Any],
    forwarded: list[bool],
    hidden: tuple[str, ...],
    expose: tuple[str, ...],
) -> tuple[int | None, str]:
    """Spawn *argv* under the sandbox; ``(exit code, note)`` or ``(None, why it did not run)``.

    Built in the order the agent launch (``acp/launch.py``) builds a child: the
    forwarding verdict from ``_forward_ssh_auth_sock`` over the harness mask *hidden*,
    ``wrap_argv`` with that mask, the files *expose* keeps readable and the Python-env
    strip, the ``cgroup_scope_argv`` scope, then ``scrub_agent_subprocess_env`` with the
    verdict the wrap applied, which is also appended to *forwarded*. A non-zero exit
    whose stderr tail carries a launcher refusal counts as a refusal only when the
    trusted no-op re-run confirms it, so the command's own text can never pose as the
    sandbox's.
    """
    forward_applied = [_forward_ssh_auth_sock(mode, hidden)]
    try:
        # ``is_kiro_cli=False``: an arbitrary command is never the trusted harness, so
        # a basename of ``kiro-cli`` earns no delegation to the harness's own sandbox.
        wrapped, cleanup = wrap_argv(
            argv,
            mode=mode,
            strip_python_env=True,
            forward_ssh_auth_sock=forward_applied[0],
            forward_ssh_auth_sock_out=forward_applied,
            extra_hidden_dirs=hidden,
            extra_expose_files=expose,
            is_kiro_cli=False,
        )
    except (RuntimeError, OSError) as exc:
        return None, f"sandbox refused the launch: {exc}"
    forwarded.append(forward_applied[-1])
    try:
        wrapped = cgroup_scope_argv(wrapped)
        env = scrub_agent_subprocess_env(
            _agent_base_env(), forward_ssh_auth_sock=forward_applied[-1]
        )
        with popen(wrapped, env=env, stderr=subprocess.PIPE) as proc:
            code, stderr, held = _pump_stderr(proc)
        if code != 0:
            refusal = corroborate_launcher_refusal(stderr, mode=mode, extra_hidden_dirs=hidden)
            if refusal is not None:
                return None, f"sandbox refused the launch: {refusal[1]}"
        note = "a process it started still holds its stderr; that output is not shown"
        return (128 - code if code < 0 else code), note if held else ""
    except OSError as exc:
        return None, f"could not start: {exc}"
    finally:
        if cleanup:
            with contextlib.suppress(OSError):
                os.unlink(cleanup)


def explain(argv: list[str], *, popen: Callable[..., Any] = popen_limited) -> int:
    """Run *argv* as an agent child would, print the report, return the child's status."""
    if not argv:
        print(_USAGE, file=sys.stderr)
        return 2
    command = shlex.join(argv)
    configured = configured_sandbox_mode()
    mode = effective_sandbox_mode(configured)
    backend = detect_backend(mode)
    level = "off" if mode == "off" else sandbox._mode_to_level(mode)
    harness = _configured_harness()
    hidden, expose, unroutable = harness_mask(harness, mode)
    denial = _deny_reason(command)
    forwarded: list[bool] = []

    rc: int | None = None
    if unroutable:
        ran = f"ran: no (the agent's harness does not start on this tier: {unroutable})"
    elif denial:
        ran = "ran: no (refused by a deny rule)"
    else:
        rc, why = _launch(list(argv), mode, popen, forwarded, hidden, expose)
        if rc is None:
            ran = f"ran: no ({why})"
        else:
            ran = f"ran: yes, exit code {rc}" + (f" ({why})" if why else "")

    forward = forwarded[-1] if forwarded else _forward_ssh_auth_sock(mode, hidden)
    mask_note = "its credential mask applies" if hidden else "no harness credential mask"
    lines = [
        f"sandbox explain: {command}",
        f"tier: {level} (agent.sandbox={configured})",
        f"backend: {backend}",
        f"harness: {label_for(harness)} ({mask_note})",
        ran,
        f"deny rule: {denial}" if denial else "deny rules: none matched",
        f"ssh-agent socket: {'forwarded' if forward else 'withheld'}",
        *_plan_lines(backend, level, forward, hidden, expose),
    ]
    print("\n".join(lines[:1] + [f"  {line}" for line in lines[1:]]))
    return REFUSED_EXIT if rc is None else rc

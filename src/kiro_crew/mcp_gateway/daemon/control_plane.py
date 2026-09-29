"""The token fence: which pooled backend is Kiro Crew's own control plane.

A reserved server name earns the per-session ``X-Session-Token`` only when the
command about to be spawned IS the managed spec's launcher, run with an
environment and import roots that cannot change the code it executes.
:func:`_spawns_own_control_plane` decides that once, before the fork;
:func:`_caller_for_backend` then attaches the token per frame, reading the verdict
off the backend that receives the frame.
"""

from __future__ import annotations

import dataclasses
import importlib.machinery
import os
import site
from typing import TYPE_CHECKING, Collection, Mapping, Optional

import kiro_crew
from kiro_crew.env import _SPEC_ENV_DENIED_PREFIXES
from kiro_crew.mcp_caller import CallerContext
from kiro_crew.mcp_cleanup import KIROCREW_BIN_MCP_SERVERS
from kiro_crew.mcp_gateway.daemon import logger

if TYPE_CHECKING:
    from kiro_crew.mcp_gateway.backend import Backend
    from kiro_crew.mcp_gateway.daemon.identity import _StubConn


#: Kiro Crew's own pooled backends handed the per-session token: every managed
#: Crew server, because each reads the session's tool policy through
#: ``mcp_shared`` and posts back to the gateway for the session it acts on
#: behalf of, and the gateway reads a declared session key only behind that
#: attestation. Not only the two always-on control planes: an opt-in server
#: (``kirocrew-dashboard``, ``kirocrew-work``, ...) that is routed here but
#: denied the token comes up present-but-unusable, refusing every call as
#: ``identity_unattested``.
#:
#: This is NOT a mirror of ``acp.session_mcp.CONTROL_PLANE_SERVERS`` -- the two
#: answer different questions. That set decides which servers every session
#: mounts and which survive a ``disabledTools`` entry (``session_mcp`` subtracts
#: it from the disabled set); this one decides who is handed a bearer token. An
#: opt-in server belongs in the second and NOT the first: naming it there would
#: mount it in every session and make an operator's decision to switch its tools
#: off unenforceable. Read from :mod:`kiro_crew.mcp_cleanup`, a leaf that imports
#: nothing heavier than ``config.paths`` (``kiro_crew.agent`` stays off the
#: daemon's boot path); a ratchet test pins it equal to
#: ``acp.session_mcp.IDENTITY_BOUND_SERVERS``.
#:
#: Membership is necessary and NOT sufficient. ``_spawns_own_control_plane`` still
#: compares the spawned binary by realpath and the argv exactly against the
#: managed spec for this name, and refuses a child carrying non-empty ``LD_*``,
#: ``DYLD_*``, or ``PYTHON*`` env, or an import root that shadows
#: ``kiro_crew``, so the name alone hands over nothing.
CONTROL_PLANE_BACKENDS = frozenset(KIROCREW_BIN_MCP_SERVERS)


def _spawns_own_control_plane(
    server_name: str,
    command: str,
    args: Collection[str],
    *,
    env: Mapping[str, str] | None = None,
    work_dir: str | os.PathLike[str] | None = None,
    denial: list[str] | None = None,
) -> bool:
    """Whether spawning ``command args`` for *server_name* runs Kiro Crew's own control plane.

    ``denial`` collects the reason a reserved name is refused (see
    :func:`_deny_control_plane`); the verdict itself is the return value.

    The server NAME is not proof: it arrives in the stub's register frame and
    the spawn target resolves separately from the spec-derived
    ``KIROCREW_MCP_TARGET_<NAME>`` mapping, so a spec that declares a
    third-party command under a reserved name would otherwise be handed the
    session's bearer token. The binary is compared by real path (a launcher
    and its symlink are the same program) and the args exactly, against the
    invocation the managed spec emits for that name -- the one source both the
    spec writer and this check read. Anything unresolvable is not ours.

    The invocation being ours is still not proof of what RUNS: the managed
    spec's module fallback excludes the child's working directory, but a
    hand-declared environment can still change the code the launcher executes.
    The dynamic-loader ``LD_*`` / ``DYLD_*`` namespaces and Python's ``PYTHON*``
    namespace are extensible execution surfaces. Any non-empty entry therefore
    denies the token instead of enumerating current variables or modelling
    platform- and version-specific semantics. The managed resolver removes all
    three namespaces from its inherited environment, so a value at this point is
    a hand-declared overlay or a non-managed resolver. The remaining fixed root --
    the CWD for module form, or the launcher's directory for script form -- must
    hold either nothing named ``kiro_crew`` or THIS process's package (a dev
    checkout running from its own ``src/``).

    A denial for a reserved name is logged once, at spawn, naming the condition
    that failed: a legitimate install that trips it (a stray ``kiro_crew.py`` on
    ``PYTHONPATH``, a declared ``LD_PRELOAD``, a spec edited by hand) otherwise
    presents only as every cron tool answering 403 with nothing in the daemon log
    to point at.

    Reads config and the filesystem: call it off the event loop, and BEFORE the
    child is spawned. The spawn site still launches an accepted control plane
    with ``PYTHONSAFEPATH`` as defense in depth and re-applies Kiro Crew's own
    UTF-8 pinning (``platform_compat._UTF8_PROCESS_ENV``) to every pooled
    backend once the verdict is fixed, but the verdict never depends on those
    variables or on version-specific interpreter flags.
    """
    if server_name not in CONTROL_PLANE_BACKENDS:
        # DEBUG, not the WARNING ``_deny_control_plane`` raises: this is the
        # ordinary answer for a third-party backend, which is most of them.
        # Logged at all because the two exits are otherwise indistinguishable
        # from outside -- a control plane MISSING from the set above produces
        # exactly this silence, so reading "no denial was logged" as "the check
        # passed" is wrong: such a backend is invisible here while every one of
        # its tools answers 409.
        logger.debug(
            "mcp-gateway: backend %r gets no session token: not in CONTROL_PLANE_BACKENDS",
            server_name,
        )
        return False
    # Lazy: ``kiro_crew.agent`` is not on the daemon's boot path and this runs
    # once per spawn, not per call.
    from kiro_crew.agent import managed_mcp_spec_entry

    def deny(reason: str) -> bool:
        return _deny_control_plane(server_name, reason, denial)

    expected = managed_mcp_spec_entry(server_name, include_opt_in=True)
    if not expected:
        return deny("no managed spec entry resolves for this name")
    expected_command = str(expected.get("command") or "")
    if not expected_command or not command:
        return deny("spec or spawn command is empty")
    try:
        same_binary = os.path.realpath(command) == os.path.realpath(expected_command)
    except (OSError, ValueError):
        return deny(f"command {command!r} is unresolvable")
    if not same_binary:
        return deny(f"spawned {command!r} is not the spec's {expected_command!r}")
    argv = [str(a) for a in args]
    expected_argv = [str(a) for a in expected.get("args", [])]
    if argv != expected_argv:
        # The spawned argv is spec-derived and may carry a token; this reason
        # travels into the identity_unattested refusal, so it names the count
        # and the managed argv (ours), never the spawned values.
        return deny(f"args ({len(argv)}) differ from spec {expected_argv!r}")
    child_env = env if env is not None else os.environ
    loader_env = next(
        (
            str(key).upper()
            for key, value in child_env.items()
            if any(str(key).upper().startswith(prefix) for prefix in _SPEC_ENV_DENIED_PREFIXES)
            and value
        ),
        "",
    )
    if loader_env:
        return deny(f"child environment carries non-empty {loader_env}")
    shadow = _kiro_crew_import_is_shadowed(command, argv, work_dir)
    if shadow:
        return deny(f"import root {shadow!r} shadows kiro_crew")
    return True


def _deny_control_plane(server_name: str, reason: str, denial: list[str] | None = None) -> bool:
    """Record why a reserved-name backend gets no session token; always False.

    ``denial``, when given, receives the reason so the spawn site can pin it on
    the backend and forward it to that backend's frames
    (``Backend.control_plane_denial`` -> ``CallerContext.identity_denial``): the
    log line below is otherwise the ONLY record, and it lands in
    ``logs/mcp-gatewayd.stdout``, which no session ever shows the operator.
    """
    logger.warning(
        "mcp-gateway: backend %r spawned under a control-plane name but is denied the "
        "session token: %s; its tools that post back to the gateway for the calling "
        "session will answer 403",
        server_name,
        reason,
    )
    if denial is not None:
        denial.append(reason)
    return False


def _caller_for_backend(
    backend: "Backend",
    caller: Optional[CallerContext],
    conn: Optional["_StubConn"],
) -> Optional[CallerContext]:
    """The caller to forward to ``backend``: ``caller`` plus the session token
    only when ``backend`` is one of Kiro Crew's own control planes.

    Those pooled control planes post back to the gateway over loopback and must
    prove the session they act for with ``X-Session-Token``. gatewayd spawned
    them from its own environment, so the per-session token is not in their
    env; it is handed over per frame. The decision is read from the backend
    that receives the frame -- ``control_plane`` was fixed at spawn from the
    resolved command, never from the name the stub registered -- so the token
    never reaches a third party, not even a replacement spawned under a
    reserved name after the original died. ``caller`` is never mutated: every
    call site re-derives from the tokenless base, so a token attached for one
    backend cannot ride along to another.
    """
    if caller is None or conn is None or not conn.stub_session_token:
        return caller
    if not backend.control_plane:
        # No token. A backend under a RESERVED name is told why, so its refusal
        # can say what the daemon saw instead of "no token arrived"; a
        # third-party backend has no denial and gets the bare caller.
        if backend.control_plane_denial:
            return dataclasses.replace(caller, identity_denial=backend.control_plane_denial)
        return caller
    return dataclasses.replace(caller, session_token=conn.stub_session_token)


def _user_site_roots() -> list[str]:
    """Per-user site-packages directories this interpreter would add, if enabled.

    Raises when the location cannot be resolved, so a caller that must fail
    closed can; an empty list means user-site is switched off entirely.
    """
    if not site.ENABLE_USER_SITE:
        return []
    user_site = site.getusersitepackages()
    return [user_site] if isinstance(user_site, str) else list(user_site)


def _user_site_holds_our_package() -> bool:
    """Whether per-user site-packages holds the package this process is running.

    True identifies a ``--user`` install, whose own ``kiro_crew`` lives there:
    user-site must stay enabled for the control plane to import itself at all.
    False means nothing there is load-bearing, so the child can be launched with
    user-site off. Unresolvable counts as holding it, which keeps the child's
    import behaviour unchanged rather than guessing.
    """
    try:
        roots = _user_site_roots()
    except Exception:
        return True
    if not roots:
        return False
    ours = os.path.realpath(os.path.dirname(kiro_crew.__file__))
    for root in roots:
        try:
            if os.path.realpath(os.path.join(root, "kiro_crew")) == ours:
                return True
        except (OSError, ValueError):
            return True
    return False


def _kiro_crew_import_is_shadowed(
    command: str,
    argv: list[str],
    work_dir: str | os.PathLike[str] | None,
) -> str:
    """A root the child searches that holds a foreign ``kiro_crew``, else ``""``.

    Control-plane spawns whose environment carries any non-empty ``PYTHON*``
    variable are denied before this check, which removes every import root an
    environment variable can introduce or relocate. Two roots survive that and
    are inspected here.

    The first is the fixed local root: the working directory for module form, or
    the launcher's directory for script form.

    The second is per-user site-packages, which needs no variable to take
    effect -- the interpreter adds it from a default location, ahead of the
    install's own site-packages, and neither ``PYTHONSAFEPATH`` nor the absence
    of ``PYTHONUSERBASE`` disables it. It is read from THIS process because the
    spawn is already pinned to the managed spec's own launcher by realpath, so
    the child runs this install's interpreter. A ``--user`` install keeps
    working: its user-site holds the very package this process is running, which
    matches and therefore does not shadow. Disabling user-site instead would
    break that install shape outright.

    Interpreter flags do not relax this token fence; checking an inert root is a
    safe false-deny, while modelling every Python path rule could become a
    false-grant when those rules change.

    A root shadows when it holds an importable regular ``kiro_crew`` package or
    module file that is not the package this process is running. A namespace
    directory without ``__init__.py`` does not shadow.
    """
    module_form = any(
        arg == "-m" and i + 1 < len(argv) and argv[i + 1] == "kiro_crew"
        for i, arg in enumerate(argv)
    )
    cwd = os.fspath(work_dir) if work_dir else os.getcwd()
    if module_form:
        root = cwd
    else:
        try:
            root = os.path.dirname(os.path.realpath(command))
        except (OSError, ValueError):
            return command
    roots = [root]
    try:
        roots.extend(_user_site_roots())
    except Exception:
        return "per-user site-packages (unresolvable)"
    ours = os.path.realpath(os.path.dirname(kiro_crew.__file__))
    for candidate in roots:
        try:
            package = os.path.join(candidate, "kiro_crew")
            if os.path.isfile(os.path.join(package, "__init__.py")):
                if os.path.realpath(package) != ours:
                    return candidate
            elif any(
                os.path.isfile(package + suffix) for suffix in importlib.machinery.all_suffixes()
            ):
                return candidate
        except (OSError, ValueError):
            return candidate
    return ""

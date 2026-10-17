"""The process-isolation jail gate that ``kiro_crew.cli.main`` runs before dispatch.

``_jail_reexec_gate`` gives the active edition one chance to re-exec an agent-bearing command
(``_JAILED_COMMANDS``) into its isolation jail (the CPP ``JailProvider`` seam) before any
agent or credential work starts. The public edition's provider has no backend, so the gate
falls through and the command runs in-process.

Split out of ``kiro_crew.cli``, which stays the import path and the patch target: every name
here is reached as ``kiro_crew.cli.<name>``, and a patch through that facade reaches this
module (``kiro_crew._facade_mirror``). Nothing here imports the facade. New jail-gate work
goes here.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import NoReturn

from kiro_crew import platform_compat
from kiro_crew.constants import env_flag_enabled
from kiro_crew.platform import PlatformCompositionError, current_context

# Commands that run agent work in-process and so are candidates for the
# process-isolation jail (CPP JailProvider seam).  The RULE: every command that
# builds an LLM provider factory (``build_provider_factory`` /
# ``SessionManager``) and runs in-process agent/LLM work belongs here — so a
# companion's ``jail=on`` isolation guarantee covers all of them, not just the
# interactive ones.  Today that is ``chat``/``run`` plus ``consolidate``
# (history consolidation LLM inference) and ``eval`` (eval turns + judge).
# ``gateway`` is deliberately EXCLUDED despite being agent-bearing: it is the
# long-lived service whose own execv-based self-update / restart path must not be
# nested inside a jail re-exec (that would exhaust user namespaces); it composes
# isolation by other means.  The public edition's JailProvider has no backend, so
# this set only matters once a companion supplies a real one.
_JAILED_COMMANDS = frozenset({"chat", "run", "consolidate", "eval"})

# Env marker the gate sets BEFORE a successful re-exec into the jail.  The jailed
# CHILD re-runs ``main`` (and so the gate) for the same command; without this
# marker the child would probe the backend again, get ``None`` ("already jailed",
# per the JailProvider contract), and the on-mode floor would refuse to run it —
# a re-entry deadlock.  The gate short-circuits when the marker is PRESENT (it is
# already inside the jail).  A companion's ``maybe_reexec_into_jail`` may rely on
# the core setting this; it is also free to set its own marker — re-entry is
# detected by PRESENCE of a non-empty value (NOT truthiness), so a descriptive
# value like ``"jailed"`` or a namespace id works just as well as the core's "1".
_JAILED_ENV_MARKER = "KIROCREW_JAILED"


def _already_jailed() -> bool:
    """True if we are running inside an already-established jail (marker present).

    Presence-based (any non-empty value), NOT truthiness — this matches both the
    core's own ``"1"`` write and a companion that sets a descriptive marker value,
    so the re-entry guard cannot be defeated by a non-truthy marker.
    """
    return bool(os.environ.get(_JAILED_ENV_MARKER, "").strip())


def _child_argv() -> "list[str]":
    """The argv the jail should re-exec — this same kirocrew invocation.

    Reuses ``agent._resolve_kirocrew_bin`` (the same self-invocation resolver
    kirocrew-core/kirocrew-cron use) so the jailed child inherits its venv
    ``bin/`` walk and ``os.access(X_OK)`` validation — rather than
    re-implementing a bare ``shutil.which`` that misses those cases.
    The resolver returns the bare ``"kirocrew"`` sentinel when it finds no usable
    binary; in that case fall back to ``python -m kiro_crew`` so a non-PATH /
    editable run still re-execs correctly.
    """
    from kiro_crew.agent import _resolve_kirocrew_bin

    exe = _resolve_kirocrew_bin()
    if exe != "kirocrew":  # a resolved, validated absolute path
        return [exe, *sys.argv[1:]]
    return platform_compat.isolated_python_argv("-P", "-m", "kiro_crew", *sys.argv[1:])


def _refuse_unjailed(command: str, reason: str) -> NoReturn:
    """Fail closed: ``agent.jail=on`` could not be satisfied → log + ``exit 2``.

    The single on-mode refusal site, so the error code / message / any future
    audit hook stay identical regardless of WHICH on-mode failure tripped it
    (availability probe error vs no re-exec).
    """
    logging.getLogger("kiro_crew").error(
        "agent.jail=on but %s for %r; refusing to run un-jailed", reason, command
    )
    sys.exit(2)


def _jail_reexec_gate(command: str, no_jail_flag: bool) -> None:
    """Give the active edition a chance to re-exec into a process-isolation jail.

    Called once per agent-bearing command (``_JAILED_COMMANDS``) after platform
    boot, before dispatch.  Either returns (run in-process) or terminates the
    process via :func:`sys.exit` — with the jailed child's exit code on a
    successful re-exec, or ``2`` when ``agent.jail=on`` could not be satisfied.

    Fail-closed discipline (``mode == "on"`` means the operator DEMANDED
    isolation, so anything short of a real re-exec must refuse to run un-jailed):

    * Already inside the jail (``KIROCREW_JAILED`` marker set by a prior re-exec)
      → return.  Without this the jailed CHILD would re-probe, get ``None``
      ("already jailed"), and the on-mode floor would deadlock the child.
    * ``off`` (``--no-jail`` or ``KIROCREW_NO_JAIL`` truthy) → early return, no
      probe, run in-process.
    * ``available()`` is the backend-presence probe.  When it returns ``False``
      cleanly (the public Default) the gate is a NO-OP even under ``mode == "on"``
      — exactly as the ``agent.jail`` help text promises, and we never build the
      re-exec argv.  A :class:`PlatformCompositionError` always propagates.  A
      *transient* probe error degrades to "no backend" under ``auto`` but FAILS
      CLOSED under ``on`` (``exit 2``): an on-mode host must not run un-jailed
      just because the presence probe was flaky (availability unknown ≠ absent).
    * With a backend present + ``mode != "off"``, a single fail-closed floor
      governs the re-exec: under ``on`` anything other than a real re-exec (a
      ``None`` return OR a swallowed backend error, both leaving ``rc is None``)
      refuses to run un-jailed.  One check, so the error path and the
      ``None``-return path cannot diverge.

    The mode is re-normalized here (``_normalize_jail``) so a context whose
    ``agent.jail`` was set programmatically (a companion) to an off-spec value is
    handled identically to the load-time path.
    """
    from kiro_crew.config.loader import _normalize_jail

    log = logging.getLogger("kiro_crew")

    # Re-entry guard: if we are already the jailed child (a prior re-exec set the
    # marker), do not re-probe / re-jail — that would deadlock under on-mode.
    # Presence-based (see _already_jailed) so a companion's non-truthy marker
    # value still short-circuits.
    if _already_jailed():
        return

    ctx = current_context()
    jail = ctx.jail

    # ``off`` per-invocation: --no-jail OR the KIROCREW_NO_JAIL env hatch (the
    # documented env-only bypass for wrapper / cron / systemd / CI hosts that
    # cannot inject a flag into argv).  Uses the shared truthy convention
    # (``env_flag_enabled``) — a bare ``=0``/``=false`` does NOT disable the jail,
    # so a typo can't silently bypass isolation.
    if no_jail_flag or env_flag_enabled("KIROCREW_NO_JAIL"):
        return  # disabled this invocation → run in-process (no probe needed)

    mode = _normalize_jail(ctx.cfg.agent.jail)
    if mode == "off":
        return

    # Backend-presence probe.  Tell "returned False cleanly" (no backend → no-op)
    # apart from "probe raised" (availability unknown): the latter must fail
    # CLOSED under on-mode rather than degrade to a no-op.
    try:
        available = jail.available()
    except PlatformCompositionError:
        raise
    except Exception:
        log.debug("jail.available() probe failed", exc_info=True)
        if mode == "on":
            _refuse_unjailed(command, "the jail availability probe errored")
        return  # auto: availability unknown → degrade to in-process
    if not available:
        return  # no backend → clean no-op even under on-mode (public Default)

    # Mark the env BEFORE invoking the backend so the re-exec'd CHILD (which
    # inherits the current environment at exec time) sees the marker and the
    # re-entrant gate short-circuits — without it the child would re-probe, get
    # an "already jailed" None, and deadlock under on-mode.  The ``finally``
    # restores the prior value: if the backend re-execs, this process is replaced
    # and the restore never runs (the child keeps the marker); if it RETURNS
    # (degrade / no-op), we must not leave the marker set, or a later in-process
    # check would wrongly believe it is already jailed.
    _prior_marker = os.environ.get(_JAILED_ENV_MARKER)
    os.environ[_JAILED_ENV_MARKER] = "1"
    try:
        rc = jail.maybe_reexec_into_jail(_child_argv(), mode)
    except PlatformCompositionError:
        raise
    except Exception:
        # A jail backend failure must not brick the CLI in the degrading mode;
        # fall through and run in-process (the always-on sandbox / security
        # controls still apply).  An exception counts as "did not jail" (rc=None)
        # for the single fail-closed check below.
        log.debug("jail re-exec check failed", exc_info=True)
        rc = None
    finally:
        # Only reached when the backend RETURNED (no re-exec replaced us).
        if _prior_marker is None:
            os.environ.pop(_JAILED_ENV_MARKER, None)
        else:
            os.environ[_JAILED_ENV_MARKER] = _prior_marker
    if rc is None:
        if mode == "on":
            _refuse_unjailed(command, "no jail re-exec occurred")
        return
    sys.exit(rc)

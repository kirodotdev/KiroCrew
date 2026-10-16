"""What Dev Fleet tells the operator about a Make Live cutover.

The dry-run plan of a cutover, the operator-facing text for a service that cannot
be driven and for a cutover that staged but could not restart, the manual restart
command, and whether a target is still the pointer's one-level undo target. Reached
through ``live``, its facade.
"""

from __future__ import annotations

from pathlib import Path

from kiro_crew.apps.builtins.dev_fleet import gateway_service, repository
from kiro_crew.apps.builtins.dev_fleet.live_service import _LIVE_GATEWAY_LABEL, _LIVE_GATEWAY_UNIT
from kiro_crew.service import live_target


def _staged_notice(name: str, unit_status: str) -> str:
    """Operator-facing message for a cutover that staged but could not restart.

    Leads with the remedy: the actionable command is what the operator needs
    first, and the reason is diagnostic context after it. The reason string
    already carries the "Dev Fleet cannot restart it" clause, so this must not
    restate it.
    """
    return (
        f"{name} is staged as the live target. Run "
        f"`{_manual_restart_command()}` to finish the cutover — the gateway "
        f"will come up on it. To back out instead, use the live row's "
        f"Cancel staged cutover in Dev Fleet. It was not automatic because "
        f"{_make_live_status_error(unit_status)}."
    )


def _make_live_status_error(code: str) -> str:
    """Operator-facing message for a non-``ok`` service status.

    Every message names the concrete remedy: an unmanageable service is the one
    make-live failure a user cannot diagnose from the UI alone, and the macOS
    variants are only reachable on a host where the previous behaviour was to
    hide the control entirely.
    """
    return {
        "no_systemd": (
            "the gateway does not run as a systemd --user service, so Dev Fleet "
            "cannot restart it for you"
        ),
        "no_user_unit": (
            f"the live gateway is not running as the user service "
            f"{_LIVE_GATEWAY_UNIT} — Dev Fleet cannot restart it for you (a "
            "`kirocrew service install` system unit needs root to bounce)"
        ),
        "user_unit_inactive": (
            f"the user service {_LIVE_GATEWAY_UNIT} exists but is not running, so "
            "this gateway is not it — restarting that unit would leave the "
            "gateway you are talking to untouched"
        ),
        "no_launchd": (
            "the gateway does not run as a launchd user agent, so Dev Fleet "
            "cannot restart it for you"
        ),
        "no_agent": (
            f"the live gateway is not running as the launchd agent "
            f"{_LIVE_GATEWAY_LABEL} — it was most likely started by the "
            "packaged app or from a terminal, so Dev Fleet cannot restart it "
            "for you"
        ),
        "agent_not_indirected": (
            f"the launchd agent {_LIVE_GATEWAY_LABEL} does not run through the "
            "live-gateway launcher, so Dev Fleet does not treat it as one it "
            "can safely bounce. Re-run `kirocrew service install` to refresh "
            "the agent definition"
        ),
        "agent_restart_contract_outdated": (
            f"the launchd agent {_LIVE_GATEWAY_LABEL} lacks the bounded graceful "
            "restart contract required by Dev Fleet. Re-run "
            "`kirocrew service install` to refresh the agent definition"
        ),
        "live_program_missing": (
            f"the launchd agent {_LIVE_GATEWAY_LABEL} is loaded but its "
            "live-gateway launcher is missing (deleted application-support "
            "directory?), so it has nothing to execute. Make live onto a "
            "worktree to rewrite it, or start a gateway from your source "
            "checkout — either restores the launcher without touching the "
            "agent definition, whereas kirocrew service install would rewrite "
            "the whole plist and discard any environment you added to it"
        ),
    }.get(code, f"the live gateway cannot be repointed ({code})")


def _manual_restart_command() -> str:
    """The command an operator runs to finish a staged cutover themselves.

    Always the service-aware ``kirocrew restart``: it resolves whatever manager
    owns the gateway (or a foreground process) at run time. Naming a specific
    ``systemctl`` invocation here would guess, and guessing wrong hands the
    operator a command that fails while the staged pointer stays unapplied — a
    Linux host with ``systemctl`` present may still be running the gateway from a
    terminal with no unit to bounce.
    """
    return "kirocrew restart"


def _make_live_plan(
    worktree: Path,
    kcbin: Path,
    *,
    svc: "gateway_service.GatewayServiceBackend | None",
    foreground: "gateway_service.ForegroundBackend | None" = None,
) -> dict:
    """Describe — without mutating anything — what making *worktree* live does.

    Validates the target the same way the real cutover does, so a dry run
    reports an unusable worktree instead of promising a cutover that would then
    be refused. When the service is drivable the backend's own plan is folded in,
    because the cutover restages that definition too. *foreground* is the
    last-resort restart that will be ATTEMPTED when no manager is drivable
    (see ``_make_live``): a dry run must report that restart as automatic, or
    the preview would promise a manual step the real call then performs itself.
    """
    live_target.validate(str(worktree))
    plan: dict = {
        "mechanism": "live-target pointer",
        "pointer_path": str(live_target.pointer_path()),
        "exec": str(kcbin),
        "restart": "automatic" if (svc is not None or foreground is not None) else "manual",
    }
    if svc is not None:
        plan.update(svc.plan(worktree, kcbin))
    elif foreground is not None:
        plan.update(foreground.plan(worktree, kcbin))
    else:
        plan["manual_restart"] = _manual_restart_command()
    return plan


def _undo_binding_holds(real: Path) -> bool:
    """``real`` is still the pointer's validated one-level undo target.

    Synchronous (pointer read + path comparison): callers hop it to the executor.
    Checked before validation and again under the single-flight lock, so a banner
    rendered for an older cutover can never repoint a newer live target.
    """
    previous = live_target.read_previous_target()
    return previous is not None and repository._same_path(str(real), str(previous))

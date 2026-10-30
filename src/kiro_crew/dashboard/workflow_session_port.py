"""The workflow ``ctx`` session port: open, message and set the mode of sessions.

A workflow script runs inside the gateway, so it has no credential and no HTTP
request of its own. These three verbs give it none: each one acts AS the session
that launched the run (the run's ``session_key``) and goes through the same
session-control verbs the ``session_create`` / ``session_send`` MCP tools reach,
so the caller-side refusals, the ``agent.session_control`` switch, the workspace
boundary and the SEL audit are the ones that session already lives under.

What this module adds on top, and nothing else:

* **A launching session is required.** A run with no session key, a ``cron:`` key
  or an unattended slot (``cron-`` / ``workflow-``) is refused
  ``no_launching_session``: there is no person's session for it to act as.
* **A mode can stay or drop, never rise.** ``set_session_mode`` ranks ``normal``
  below ``trust_reads`` below ``trust`` and refuses ``mode_raise_refused`` for a
  mode above the target session's current mode or above the launching session's
  own. The target must be a session the launching session created
  (``not_creator``) and hold no scoped grant (``trust_scope_active``); asking for
  the mode it already has changes nothing.
* **A send reaches only sessions the launcher opened.** ``send_to_session`` runs
  creator-fenced for every launcher, the owner included, so it is refused
  ``not_creator`` on the person's own tabs and any session the launching session
  neither created nor was handed.
* **A ``workflow-opened`` sidebar label** on every session ``open_session`` makes,
  the way a cron-opened session is labelled by its origin.

Every refusal and every admitted call is also recorded under the launching
session with the run id, so an audit reader can tell a workflow's call from the
session's own.

Spec: ``docs/system-specs/modules/workflows.md`` (ports native to Kiro Crew).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.approval_posture import trust_carries

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.dashboard.state import DashboardState


#: The sidebar tag every session a workflow opens carries.
WORKFLOW_OPENED_TAG = "workflow-opened"

#: The envelope verb a workflow's message carries, so the target reads
#: ``[sent by session <launcher> via workflow]`` rather than a person's turn.
WORKFLOW_SEND_VIA = "workflow"

#: Approval modes a workflow may set, lowest posture first.
_MODE_RANK = {"normal": 0, "trust_reads": 1, "trust": 2}
_RANK_MODE = {rank: name for name, rank in _MODE_RANK.items()}


class WorkflowSessionError(RuntimeError):
    """A refused workflow session call; ``code`` is the machine-readable reason."""

    def __init__(self, verb: str, message: str, code: str) -> None:
        super().__init__(f"{verb}() failed: {code}: {message}")
        self.code = code


def _posture_rank(slot: Any) -> int:
    """The approval posture *slot* holds now, on the ``_MODE_RANK`` scale."""
    if getattr(slot, "_trust", False):
        return _MODE_RANK["trust"]
    if getattr(slot, "_trust_reads", False):
        return _MODE_RANK["trust_reads"]
    return _MODE_RANK["normal"]


class WorkflowSessionPort:
    """The three session verbs a workflow run is wired with (one per gateway)."""

    def __init__(self, state: "DashboardState") -> None:
        self._state = state

    # --- shared guards ---
    def _ceiling(self, launching: Any) -> int:
        """The posture the launching session can pass on NOW; ``normal`` once it is gone.

        A Crew Mode card trust (``_trust_card_scoped``) covers that session's own
        tools only and does not carry to what it starts (``trust_carries``), so it
        sets no ``trust`` ceiling for a workflow it launched.
        """
        live = self._state.get_slot(launching.key)
        if live is not launching:
            return _MODE_RANK["normal"]
        if getattr(live, "_trust", False) and not trust_carries(live):
            if getattr(live, "_trust_reads", False):
                return _MODE_RANK["trust_reads"]
            return _MODE_RANK["normal"]
        return _posture_rank(live)

    def _refuse_raise(
        self,
        verb: str,
        caller: str,
        run_id: str,
        slot_key: str,
        mode: str,
        launching: Any,
        target: Any = None,
    ) -> None:
        """Refuse *mode* when it would raise the target or exceed the launcher.

        The guard this verb exists to keep: a model-written script may keep or
        lower a session's auto-approval, never grant more, neither above what the
        session holds now (so a mode the person lowered stays lowered) nor above
        what the person's own launching session holds.
        """
        above_target = target is not None and _MODE_RANK[mode] > _posture_rank(target)
        if above_target or _MODE_RANK[mode] > self._ceiling(launching):
            raise self._refuse(
                verb,
                caller=caller,
                run_id=run_id,
                slot_key=slot_key,
                message="a workflow can keep or lower a session's mode, never raise it",
                code="mode_raise_refused",
            )

    def _audit(
        self, *, caller: str, verb: str, run_id: str, slot_key: str, outcome: str, **detail: str
    ) -> None:
        sc._audit(
            caller_session_key=caller or f"workflow:{run_id}",
            operation=f"workflow_{verb}",
            slot_key=slot_key,
            outcome=outcome,
            detail={"run_id": run_id, **detail},
        )

    def _refuse(
        self, verb: str, *, caller: str, run_id: str, slot_key: str, message: str, code: str
    ) -> WorkflowSessionError:
        self._audit(
            caller=caller, verb=verb, run_id=run_id, slot_key=slot_key, outcome="denied", code=code
        )
        return WorkflowSessionError(verb, message, code)

    def _refuse_restricted_run(
        self, verb: str, caller: str, run_id: str, memory_mode: str, slot_key: str = ""
    ) -> None:
        """Refuse when the run's captured memory mode is not ``persistent``.

        The mode is the strictest one the run captured (an Incognito or Temporary
        launch, or a rerun of one), not the caller's: a persistent session that
        reruns a restricted run must not carry its content into a persistent
        transcript. Anything else, including an unknown mode, is refused.
        """
        if memory_mode != "persistent":
            raise self._refuse(
                verb,
                caller=caller,
                run_id=run_id,
                slot_key=slot_key,
                message=(
                    "this run's memory mode is not persistent, so it cannot open or "
                    "message a dashboard session"
                ),
                code="restricted_memory_mode",
            )

    def _launching_slot(self, verb: str, caller: str, run_id: str, slot_key: str = "") -> Any:
        """The live slot of the session that launched the run, or a refusal."""
        key = ""
        if caller and not caller.startswith(sc.CRON_LINK_PREFIX):
            key = sc.caller_slot_key(self._state, caller)
        if not key or key.startswith(sc.UNATTENDED_SLOT_PREFIXES):
            raise self._refuse(
                verb,
                caller=caller,
                run_id=run_id,
                slot_key=slot_key,
                message=(
                    "this run has no launching dashboard session (it was started by "
                    "the system or a cron), so it cannot act as one"
                ),
                code="no_launching_session",
            )
        slot = self._state.get_slot(key)
        if slot is None:
            raise self._refuse(
                verb,
                caller=caller,
                run_id=run_id,
                slot_key=slot_key,
                message="the launching session is no longer open",
                code="no_launching_session",
            )
        return slot

    async def _scope_fence(self, verb: str, caller: str, run_id: str, slot_key: str) -> bool | None:
        """The session-control HTTP scope gate, applied to the launching session.

        ``_private_caller_refusal`` stands in front of ``/api/session-control/*``;
        these verbs reach the same functions in-process, so they apply its rule to
        the same caller: an owner / Global caller passes (``None``, fenced inline as
        on that route), a crew member reaching a member-open surface is admitted
        creator-fenced (``True``, carried as ``_carried_fence`` carries it), and any
        other caller bound to a private store is refused ``member_scope_denied``.
        An unreadable execution record is refused, never treated as Global.
        """

        def _resolve() -> tuple[str | None, bool]:
            try:
                execution = sc.read_session_execution(caller)
            except (OSError, ValueError):
                return None, False
            store = execution.store.legacy_name if execution is not None else ""
            if not store:
                return "", False
            return store, sc.member_admitted_to_scoped_surface(caller, store)

        store, admitted = await asyncio.to_thread(_resolve)
        if store is None:
            raise self._refuse(
                verb,
                caller=caller,
                run_id=run_id,
                slot_key=slot_key,
                message="the launching session's execution identity is unavailable",
                code="member_identity_unavailable",
            )
        if not store:
            return None
        if admitted:
            return True
        raise self._refuse(
            verb,
            caller=caller,
            run_id=run_id,
            slot_key=slot_key,
            message="the launching session's memory scope may not use session control",
            code="member_scope_denied",
        )

    async def open_session(
        self,
        *,
        run_id: str,
        memory_mode: str,
        caller_session_key: str,
        name: str = "",
        folder_id: str = "",
        agent: str = "",
        model: str = "",
    ) -> str:
        """Open a session as the launching session; return its slot key."""
        verb = "open_session"
        self._refuse_restricted_run(verb, caller_session_key, run_id, memory_mode)
        self._launching_slot(verb, caller_session_key, run_id)
        fenced = await self._scope_fence(verb, caller_session_key, run_id, "")
        await sc.prewarm_enabled_check()
        try:
            result = await sc.create_session(
                self._state,
                caller_session_key=caller_session_key,
                title=str(name or ""),
                agent=str(agent or ""),
                folder_id=str(folder_id or ""),
                model=str(model or ""),
                caller_fenced=fenced,
            )
        except sc.SessionControlError as exc:
            raise self._refuse(
                verb,
                caller=caller_session_key,
                run_id=run_id,
                slot_key="",
                message=exc.message,
                code=exc.code,
            ) from None
        key = str(result.get("target") or "")
        slot = self._state.get_slot(key)
        labelled = False
        if slot is not None:
            from kiro_crew.dashboard.chat_auto_tag import apply_label_tag

            labelled = await apply_label_tag(self._state, slot, WORKFLOW_OPENED_TAG)
        self._audit(
            caller=caller_session_key,
            verb=verb,
            run_id=run_id,
            slot_key=key,
            outcome="allowed",
            labelled="true" if labelled else "false",
        )
        return key

    async def send_to_session(
        self, *, run_id: str, memory_mode: str, caller_session_key: str, slot: str, message: str
    ) -> dict:
        """Deliver *message* to *slot* as the launching session's ``session_send``."""
        verb = "send_to_session"
        self._refuse_restricted_run(verb, caller_session_key, run_id, memory_mode, str(slot))
        self._launching_slot(verb, caller_session_key, run_id, str(slot))
        # The scope gate still refuses a member it does not admit, but its verdict is
        # not the reach: a send always runs creator-fenced, so a model-written
        # script starts turns only in sessions the launching session opened (or was
        # handed), never in the person's own tabs, even for an owner launcher.
        await self._scope_fence(verb, caller_session_key, run_id, str(slot))
        try:
            result = await sc.send_to_target(
                self._state,
                caller_session_key=caller_session_key,
                target=str(slot),
                message=str(message),
                caller_fenced=True,
                via=WORKFLOW_SEND_VIA,
            )
        except sc.SessionControlError as exc:
            raise self._refuse(
                verb,
                caller=caller_session_key,
                run_id=run_id,
                slot_key=str(slot),
                message=exc.message,
                code=exc.code,
            ) from None
        self._audit(
            caller=caller_session_key,
            verb=verb,
            run_id=run_id,
            slot_key=str(result.get("target") or slot),
            outcome="allowed",
        )
        return result

    async def set_session_mode(
        self, *, run_id: str, caller_session_key: str, slot: str, mode: str
    ) -> dict:
        """Set *slot*'s approval mode, no higher than the launching session's own."""
        verb = "set_session_mode"
        target = str(slot)
        launching = self._launching_slot(verb, caller_session_key, run_id, target)
        if not isinstance(mode, str) or mode not in _MODE_RANK:
            raise self._refuse(
                verb,
                caller=caller_session_key,
                run_id=run_id,
                slot_key=target,
                message=f"mode must be one of {sorted(_MODE_RANK)}",
                code="mode_not_allowed",
            )
        # Checked here to refuse early, and again below with no await before the
        # mutation and once after it: the launcher's posture can drop at any await.
        self._refuse_raise(verb, caller_session_key, run_id, target, mode, launching)
        fenced = await self._scope_fence(verb, caller_session_key, run_id, target)
        await sc.prewarm_enabled_check()
        # From here to the ``apply_approval_mode`` call nothing suspends, so the
        # target, its creator and the launcher's ceiling are judged together.
        try:
            resolved = sc.authorize_target(
                self._state,
                caller_session_key=caller_session_key,
                target=target,
                operation="set_mode",
                precomputed_ownership_fenced=fenced,
            )
        except sc.SessionControlError as exc:
            raise self._refuse(
                verb,
                caller=caller_session_key,
                run_id=run_id,
                slot_key=target,
                message=exc.message,
                code=exc.code,
            ) from None
        if sc._created_by_other(resolved, launching.key):
            raise self._refuse(
                verb,
                caller=caller_session_key,
                run_id=run_id,
                slot_key=resolved.key,
                message="only a session the launching session opened can have its mode set",
                code="not_creator",
            )
        self._refuse_raise(
            verb, caller_session_key, run_id, resolved.key, mode, launching, resolved
        )
        if str(getattr(resolved, "_trust_scope", "") or ""):
            # A scoped grant is armed by a person or an app; ending it is theirs.
            raise self._refuse(
                verb,
                caller=caller_session_key,
                run_id=run_id,
                slot_key=resolved.key,
                message="the session holds a scoped trust grant a workflow cannot change",
                code="trust_scope_active",
            )
        if _MODE_RANK[mode] == _posture_rank(resolved):
            # Nothing to change, and the mode write is not run: its ``trust`` arm
            # also approves the session's pending tool calls.
            self._audit(
                caller=caller_session_key,
                verb=verb,
                run_id=run_id,
                slot_key=resolved.key,
                outcome="allowed",
                mode=mode,
                unchanged="true",
            )
            return {"ok": True, "mode": mode, "slot": resolved.key, "unchanged": True}
        from kiro_crew.dashboard.chat_handlers import apply_approval_mode

        async def _apply(to_mode: str) -> Any:
            # The slot-scoped mutation the mode route runs. ``cron_creator`` is the
            # route's name for "an automated caller acting on one slot": with it
            # set, the operator's process-global override and any channel trust are
            # left alone, which is what a workflow's change to one session must do.
            return await apply_approval_mode(
                self._state,
                mode=to_mode,
                slot=resolved,
                slot_key=resolved.key,
                request_app="",
                cron_creator=launching.key,
                audit_caller=lambda _label: f"workflow:{run_id}",
            )

        response = await _apply(mode)
        if getattr(response, "status", 500) != 200:
            raise self._refuse(
                verb,
                caller=caller_session_key,
                run_id=run_id,
                slot_key=resolved.key,
                message=f"mode change refused (HTTP {getattr(response, 'status', '?')})",
                code="mode_change_failed",
            )
        # ``apply_approval_mode`` suspends before its write on the lowering paths
        # (it ends scoped grants off the loop), so a launcher lowered in that window
        # is caught here: the session is put back to the launcher's mode now.
        ceiling = self._ceiling(launching)
        if _MODE_RANK[mode] > ceiling:
            await _apply(_RANK_MODE[ceiling])
            raise self._refuse(
                verb,
                caller=caller_session_key,
                run_id=run_id,
                slot_key=resolved.key,
                message="the launching session's mode dropped while the change was applied",
                code="mode_raise_refused",
            )
        self._audit(
            caller=caller_session_key,
            verb=verb,
            run_id=run_id,
            slot_key=resolved.key,
            outcome="allowed",
            mode=mode,
        )
        return {"ok": True, "mode": mode, "slot": resolved.key}

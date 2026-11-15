"""Agent Turn SDK -- one gated agent turn on a session the app owns.

An app that drives background agents (a meeting's note taker, a one-line
translation) needs three things from the gateway: its shared session manager, the
PreToolUse gate every other turn passes through, and Shared Crew fair admission.
Each is private to the gateway. This SDK is the one public door to all three, and
it fixes the parts an app must not choose:

* **Identity.** Every turn carries ``app=<this app>``, so the gate resolves
  ``POLICY ∩ PROFILE`` with this app's profile. The app passes no app name, so it
  cannot borrow another app's (looser) profile.
* **Approval.** :meth:`AgentTurnSDK.run` approves tools only through the hook
  gate (``HOOK_BASED``); :meth:`AgentTurnSDK.run_oneshot` rejects every tool
  (``REJECT_ALL``). No method takes an approval policy, so an app cannot ask for
  auto-approval. A call the gate neither denies nor grants goes to the owner's
  approval prompt, the one cron jobs use (:func:`set_turn_approver`), never
  straight to an approve.
* **Admission.** Every turn is admitted as Crew automation
  (``OperationKind.BACKGROUND``); a refusal raises ``TurnRefused`` and the work
  never runs.
* **One agent identity.** Every turn resolves its agent to one name (no agent
  means today's default template), asks ``capabilities.spawn`` under
  ``app=<this app>`` the two questions every other spawn entrypoint asks (is
  spawning on for this app, is that agent in its ``agents`` scope), and opens the
  session BY that name, so the agent that starts is the agent asked about. A
  refusal raises :class:`AgentTurnRefused` before any session is acquired. Once
  the session is held, the agent the turn carries is read back from the session
  manager's own record, on every path: a reused session answers with the agent it
  started as, which is asked about again when it differs, and a session whose
  record names no agent is refused. The hook gate is handed that recorded agent,
  so the ceiling, the process and the gate's profile never name different agents.
* **Session keys.** A key must be ``<app>-<id>``, where *id* is 1 to 400 of
  ``A-Z a-z 0-9 . _ -``. Any other key is refused with :class:`ValueError` before
  the session manager is touched, so an app cannot address a person's chat slot
  or a core session. The separator is a plain ``-``, so an app named ``x`` can
  still spell a key of an app named ``x-y``; the SDK is not a boundary against
  the app's own in-gateway Python, which could reach the manager anyway.

Present on ``ctx.turns`` only when the manifest declares
``permissions.agentTurns``. The session manager and the gate are looked up on
every call, so a context built before the gateway's state is ready still works
once it is.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from typing import Any, Callable

logger = logging.getLogger(__name__)

#: Seconds :meth:`AgentTurnSDK.run` waits for the provider to acknowledge a
#: cancellation before it resets the session instead.
DEFAULT_CANCEL_ACK_SECS = 5.0

#: The part of a session key after ``<app>-``.
_KEY_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,400}")
#: The middle part of a one-shot key, ``<app>-<label>-<hex>``.
_ONESHOT_LABEL_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
#: The tool-less, cheaper agent a one-shot turn runs on (``tools: []``).
_ONESHOT_AGENT = "kirocrew-lite"


class AgentTurnRefused(PermissionError):
    """The app's ``capabilities.spawn`` ceiling refuses the agent this turn runs."""


#: The gateway's approval prompt for app turns: called with the app's name, it
#: returns the ``on_tool_approval`` callback a turn of that app asks. ``None``
#: until a gateway registers one (a bare test application).
_turn_approver: Callable[[str], Any] | None = None


def set_turn_approver(factory: Callable[[str], Any] | None) -> None:
    """Register the gateway's approval prompt for every app turn (``None`` clears it).

    Without one, a call the hook gate neither denies nor grants would fall to
    ``stream_and_collect``'s caller-less default and be approved unseen; with one
    it asks the owner, as a cron job's call does.
    """
    global _turn_approver
    _turn_approver = factory


def _approver_for(app: str) -> Any:
    factory = _turn_approver
    return factory(app) if factory is not None else None


def _default_agent() -> str:
    """The agent a session created with no agent starts as, as kiro-cli declares it.

    Called off the event loop: :func:`dispatch_kiro_agent` reads the materialized
    snapshot and only warms a cold one off-loop.
    """
    from kiro_crew.config.loader import KiroCrewConfig, default_template, dispatch_kiro_agent

    return dispatch_kiro_agent(default_template(KiroCrewConfig.load()))


def _spawn_denial(key: str, agent: str, app: str) -> tuple[str, str | None]:
    """The agent asked about for *agent*, and why ``capabilities.spawn`` refuses it.

    The denial is None when the ceiling permits. Blocking (a config load for an
    empty *agent*); run it in a thread. Fails closed: a default agent that cannot
    be resolved is a refusal, and ``_vet_spawn_governance`` already denies on an
    evaluation error.
    """
    from kiro_crew.subagent import _vet_spawn_governance

    try:
        policy_agent = agent or _default_agent()
    except Exception:
        logger.warning("%s: default agent unresolvable; refusing the turn", app, exc_info=True)
        return "", "the default agent could not be resolved for the spawn policy"
    if not policy_agent:
        return "", "the default agent could not be resolved for the spawn policy"
    return policy_agent, _vet_spawn_governance(key, policy_agent, app=app)


def _state_attr(http_app: Any, name: str) -> Any:
    getter = getattr(http_app, "get", None)
    state = getter("state") if callable(getter) else None
    return getattr(state, name, None) if state is not None else None


def _hooks_of(http_app: Any) -> Any:
    builder = _state_attr(http_app, "context_builder")
    return getattr(builder, "hooks", None) if builder is not None else None


class AgentTurnSDK:
    """Run agent turns for one app, on sessions under that app's own key prefix."""

    __slots__ = ("_app", "_sessions", "_hooks")

    def __init__(
        self,
        app_name: str,
        sessions: Callable[[], Any],
        hooks: Callable[[], Any] = lambda: None,
    ) -> None:
        """*sessions* and *hooks* return the gateway's session manager and hook manager.

        The hook manager may be ``None`` (a bare test application); the turn then
        falls back to the always-enforced deny checks of ``stream_and_collect``.
        """
        if not isinstance(app_name, str) or not app_name:
            raise ValueError("an agent turn SDK needs the app's name")
        self._app = app_name
        self._sessions = sessions
        self._hooks = hooks

    @classmethod
    def for_http_app(cls, app_name: str, http_app: Any) -> AgentTurnSDK:
        """An SDK that reads the managers off the gateway Application's state."""
        return cls(
            app_name,
            lambda: _state_attr(http_app, "sessions"),
            lambda: _hooks_of(http_app),
        )

    @property
    def app_name(self) -> str:
        return self._app

    def check_key(self, key: object) -> str:
        """*key* when it is one of this app's session keys; else :class:`ValueError`."""
        prefix = f"{self._app}-"
        if (
            isinstance(key, str)
            and key.startswith(prefix)
            and _KEY_ID_RE.fullmatch(key[len(prefix) :])
        ):
            return key
        raise ValueError(f"session key {key!r} is not a {self._app!r} key ({prefix}<id>)")

    def _refuse(self, agent: str, denial: str) -> AgentTurnRefused:
        from kiro_crew.apps.spawn_sdk import _audit_spawn_denied

        _audit_spawn_denied(self._app, agent, denial, tool_name="agent_turn")
        return AgentTurnRefused(
            f"agent turn for app {self._app!r} refused by spawn policy: {denial}"
        )

    async def _require_spawn_permit(self, key: str, agent: str) -> str:
        """The agent asked about for *agent* (``""``: the default), once the ceiling permits it.

        Raises :class:`AgentTurnRefused` when it does not.
        """
        policy_agent, denial = await asyncio.to_thread(_spawn_denial, key, agent, self._app)
        if denial:
            raise self._refuse(agent, denial)
        return policy_agent

    async def _require_recorded_agent(self, sessions: Any, key: str, asked: str) -> str:
        """The agent the held session *key* runs, by the manager's record, once permitted.

        *asked* is the agent already permitted and passed to ``get_or_create``. A
        session the call reused may run another; it is asked about in turn. A
        session whose record names no agent is refused: what it runs cannot be asked.
        """
        get_agent = getattr(sessions, "get_agent", None)
        running = get_agent(key) if callable(get_agent) else ""
        if not isinstance(running, str) or not running:
            raise self._refuse("", "the agent this session runs is not recorded")
        if running != asked:
            await self._require_spawn_permit(key, running)
        return running

    def _manager(self) -> Any:
        sessions = self._sessions()
        if sessions is None:
            raise RuntimeError("session manager unavailable")
        return sessions

    def has_session(self, key: str) -> bool:
        """Whether a live session exists for *key*, one of this app's keys."""
        sessions = self._sessions()
        return bool(sessions is not None and sessions.has_session(self.check_key(key)))

    async def run(
        self,
        key: str,
        text: str,
        *,
        agent: str = "",
        timeout_secs: float | None = None,
        cancel_ack_secs: float = DEFAULT_CANCEL_ACK_SECS,
    ) -> str:
        """Send *text* to the session *key* and return the reply.

        The session is created on first use and kept: each call adds to the same
        conversation, and the turn lease is released, never the session. *agent*
        is the agent a new session starts as (``""`` for today's default, resolved
        to its name before the session opens). Tools are approved only by the
        hook gate, under this app's identity and the agent the session manager
        records for the session: a reused session's own agent, never *agent*. A
        directive only a person's turn may issue (``set_project``, ...) is refused,
        and its refusal leads the reply.

        Session acquisition is outside *timeout_secs*: a cold start cancelled
        half way would leave a session that never received the first message.
        When the budget runs out, or the caller is cancelled, the provider is
        asked to cancel (waiting *cancel_ack_secs* for its acknowledgement) and
        the session is reset when it does not acknowledge. Raises on failure,
        :class:`AgentTurnRefused` for the spawn ceiling, ``asyncio.TimeoutError``
        for the budget and ``TurnRefused`` for admission.
        """
        key = self.check_key(key)
        sessions = self._manager()
        asked = await self._require_spawn_permit(key, agent)
        # ``owning_app`` is the app the gate below judges this session's calls
        # under, so the session's own governance reads (its KAS projection among
        # them) judge under the same app.
        provider, _is_new, _resumed = await sessions.get_or_create(
            key, agent=asked, owning_app=self._app
        )
        try:
            running = await self._require_recorded_agent(sessions, key, asked)
        except BaseException:
            sessions.release(key)
            raise
        app = self._app
        hooks = self._hooks()
        approver = _approver_for(app)

        async def _run_turn() -> str:
            from kiro_crew.dashboard.session_directive_apply import person_only_directive_refuser
            from kiro_crew.llm_helpers import ToolApprovalPolicy, stream_and_collect

            # An app turn is not a person's, and no session applier reads its
            # stream: a person-only directive it calls is refused out loud.
            refusals: list[str] = []
            reply = await stream_and_collect(
                provider,
                text,
                approval_policy=ToolApprovalPolicy.HOOK_BASED,
                hooks=hooks,
                session_key=key,
                agent=running,
                app=app,
                on_tool_approval=approver,
                on_event=person_only_directive_refuser(key, refusals),
            )
            return "\n\n".join([*refusals, reply])

        from kiro_crew.shared_crew.admission import OperationKind
        from kiro_crew.shared_crew.turn_gate import run_admitted_or_raise

        turn_task = asyncio.create_task(
            run_admitted_or_raise(
                _run_turn(),
                session_key=key,
                text=text,
                automation=True,
                operation=OperationKind.BACKGROUND,
            )
        )
        try:
            if timeout_secs is None:
                return await asyncio.shield(turn_task)
            # Keep consuming ACP's terminal response while cancel() waits for it.
            # Unwinding the consumer first marks the turn done and makes cancel()
            # return no_turn without sending the native session/cancel.
            return await asyncio.wait_for(asyncio.shield(turn_task), timeout=timeout_secs)
        except asyncio.TimeoutError:
            if timeout_secs is None:
                # A provider-originated TimeoutError on an ordinary turn is a regular
                # failure. Only an explicit caller budget asks for native cancellation.
                raise
            await self._cancel_or_reset(provider, sessions, key, cancel_ack_secs)
            raise asyncio.TimeoutError(f"agent turn exceeded {timeout_secs:.0f}s") from None
        except asyncio.CancelledError:
            # A caller can cancel an otherwise unbounded turn. Keep the consumer
            # alive until native cancellation is requested, as on the budget path.
            await self._cancel_or_reset(provider, sessions, key, cancel_ack_secs)
            raise
        finally:
            # After the acknowledgement window, retire any remaining consumer before
            # releasing its turn lease, including when the caller itself was cancelled.
            if not turn_task.done():
                turn_task.cancel()
            await asyncio.gather(turn_task, return_exceptions=True)
            try:
                sessions.release(key)
            except Exception:
                logger.debug("%s: session release failed for %s", app, key, exc_info=True)

    async def _cancel_or_reset(
        self, provider: Any, sessions: Any, key: str, cancel_ack_secs: float
    ) -> None:
        """Cancel the live provider turn, resetting its session unless acknowledged."""
        cancel_outcome = "error"
        try:
            cancel_outcome = await provider.cancel(wait_ack_timeout=cancel_ack_secs)
        except Exception:
            logger.debug("%s: agent turn cancellation failed for %s", self._app, key, exc_info=True)
        if cancel_outcome != "acked":
            try:
                await sessions.reset(key)
            except Exception:
                logger.warning(
                    "%s: could not reset unacknowledged agent turn %s",
                    self._app,
                    key,
                    exc_info=True,
                )

    async def run_oneshot(self, prompt: str, *, label: str = "oneshot") -> str:
        """One tool-less model call in a throwaway session; the raw reply.

        The session ``<app>-<label>-<random hex>`` runs the ``kirocrew-lite``
        agent, which has no tools and a cheaper model than the interactive
        default, and every tool request is rejected even if one were offered. It
        is released and destroyed afterwards, so no ``kiro-cli`` child outlives
        the call. *label* is 1 to 32 of ``a-z 0-9 _ -``, starting with a letter
        or digit; anything else is :class:`ValueError`. The spawn ceiling is asked
        about ``kirocrew-lite``, and the session's recorded agent checked, as for
        :meth:`run`.
        """
        if not isinstance(label, str) or not _ONESHOT_LABEL_RE.fullmatch(label):
            raise ValueError(f"one-shot label {label!r} is not 1-32 of a-z 0-9 _ -")
        key = self.check_key(f"{self._app}-{label}-{uuid.uuid4().hex}")
        sessions = self._manager()
        await self._require_spawn_permit(key, _ONESHOT_AGENT)

        async def _call() -> str:
            from kiro_crew.llm_helpers import ToolApprovalPolicy, stream_and_collect

            provider, _is_new, _resumed = await sessions.get_or_create(key, agent=_ONESHOT_AGENT)
            await self._require_recorded_agent(sessions, key, _ONESHOT_AGENT)
            return await stream_and_collect(
                provider, prompt, approval_policy=ToolApprovalPolicy.REJECT_ALL
            )

        from kiro_crew.shared_crew.admission import OperationKind
        from kiro_crew.shared_crew.turn_gate import run_admitted_or_raise

        try:
            return await run_admitted_or_raise(
                _call(),
                session_key=key,
                text=prompt,
                automation=True,
                operation=OperationKind.BACKGROUND,
            )
        finally:
            try:
                sessions.release(key)
            except Exception:
                logger.debug("%s: one-shot session release failed", self._app, exc_info=True)
            try:
                await sessions.destroy(key)
            except Exception:
                logger.debug("%s: one-shot session destroy failed", self._app, exc_info=True)

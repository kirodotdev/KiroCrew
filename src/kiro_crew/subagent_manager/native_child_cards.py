"""A Crew run's own harness-native children, drawn as cards under the run's card.

On a backend whose capability record says ``draws_run_native_child_cards`` (claude,
through ``ACP_BACKENDS_RUN_NATIVE_CHILD_CARDS``), a Crew-spawned run has its own
``AcpClient``, which declares ``subagents`` as a chat slot's does, so the run's turn
yields the same ``EVENT_SUBAGENT_LIST`` roster and ``EVENT_SUBAGENT_ACTIVITY`` a chat
turn's does (harness-parity H16): the run's Agent-tool children and, because
:meth:`RunNativeChildCards.begin_prompt` marks each of the run's prompts
(``expect_dashboard_turn``), the agents of its Workflow runs. ``_run_inner_impl`` in
``run_turn`` hands both kinds to :meth:`RunNativeChildCards.on_event`.

The cards are reconciled by the dashboard's own card owners, reached through
``kiro_crew.dashboard.chat_runner`` (``_native_subagent_sync``,
``_native_subagent_close_all`` and ``_append_native_output``), over a frame sink that
stands in for the dashboard state. So redaction, the skip of a task-less child, a
Workflow agent's label, model and result, failure clipping, a Stop's ``stopped`` end and
the staleness rule are the chat's. Each frame the reconcilers send is relayed to the
gateway as one :data:`RELAY_EVENT` through the manager's ``_fire_event``; the gateway
draws it in the run tree's tab (``dashboard.run_native_card_relay``). A card's id
names its run, ``native:<run id>:<child>`` (:meth:`RunNativeChildCards.wire_id`), which
is what a Stop routes by and what the Subagents panel nests the card under.

Bounds: :data:`MAX_RUN_NATIVE_CARDS` children per run attempt (a child past it gets no
card, is counted once and logged at the close), a card's output collapsed to its newest
:data:`RUN_CARD_OUTPUT_TAIL` characters past :data:`RUN_CARD_OUTPUT_HARD`, a tool line
cut at :data:`TOOL_LINE_MAX`; the reconcilers bound every string they keep. Display only:
a grandchild is never admitted, counted, charged or continued as a Crew run.

The run's end closes its open cards before the run's own ``subagent_done`` goes out
(:func:`close_for`, from ``run_lifecycle._fire_event_impl``): with no error after a
run that completed, else as interrupted, and a Workflow agent's card as interrupted
always, since Crew stops following the run's process. A new attempt of the same run
closes what the last attempt left open (:meth:`RunNativeChildCards.start`).

A child's permission request waits on the run's own ladder, which reads nothing while a
person is asked, so it never sees the harness withdraw the request of a child a Stop
ended. The run's approver is awaited through :func:`unless_withdrawn`, and the Stop
ends that wait (:meth:`RunNativeChildCards.withdraw`): the approver is cancelled, which
retires its card as a Stop of the whole run does, and that call alone is rejected.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable
from types import SimpleNamespace
from typing import Any

from kiro_crew.agent_sdk.capabilities import capabilities_of
from kiro_crew.agent_sdk.drivers.acp_vocab import (
    EVENT_SUBAGENT_ACTIVITY,
    EVENT_SUBAGENT_LIST,
    NATIVE_CHILD_ROSTER_CAP,
    expect_dashboard_turn,
)
from kiro_crew.security import redact, redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)

#: The event kinds ``run_turn`` hands to :meth:`RunNativeChildCards.on_event`.
EVENTS = (EVENT_SUBAGENT_LIST, EVENT_SUBAGENT_ACTIVITY)

#: The manager event one card frame rides to the gateway in. Never put on the wire.
RELAY_EVENT = "run_native_card"

#: Children one run attempt draws a card for.
MAX_RUN_NATIVE_CARDS = 64

#: A card's output kept past :data:`RUN_CARD_OUTPUT_HARD`: the result a closing card
#: shows, ``NATIVE_SUBAGENT_DONE_RESULT_CAP``.
RUN_CARD_OUTPUT_TAIL = 8_000
RUN_CARD_OUTPUT_HARD = 16_000

#: Characters of a child's tool title its card's tool line shows.
TOOL_LINE_MAX = 80

#: Refused children remembered so each is counted once: as many as the run's client
#: tracks, so no child it lists can be refused twice.
MAX_REFUSED_IDS = NATIVE_CHILD_ROSTER_CAP

_NATIVE = "native:"


class _FrameSink:
    """The dashboard state the card reconcilers draw on, keeping each frame they send."""

    def __init__(self) -> None:
        # The reconcilers register an open card here; the gateway keeps the real one.
        self._native_cards: dict[str, dict[str, Any]] = {}
        self._frames: list[tuple[str, dict[str, Any]]] = []

    def broadcast_ws(self, etype: str, payload: dict[str, Any]) -> None:
        self._frames.append((etype, payload))

    def drain(self) -> list[tuple[str, dict[str, Any]]]:
        frames, self._frames = self._frames, []
        return frames


class RunNativeChildCards:
    """The cards of one run attempt's harness-native children."""

    def __init__(self, run_id: str, provider: Any) -> None:
        self.on = capabilities_of(provider).draws_run_native_child_cards
        self._provider = provider
        self._run_id = run_id
        self.closed = False
        #: ``{child session id: record}``, the record shape of a chat slot's tracker.
        self.tracker: dict[str, dict[str, Any]] = {}
        self.output: dict[str, list[str]] = {}
        self.output_len: dict[str, int] = {}
        self.refused = 0
        self._refused_ids: set[str] = set()
        self._sink = _FrameSink()
        self._slot = SimpleNamespace(key=f"subagent:{run_id}")
        #: The child's request the run waits on a person for, and the future a Stop
        #: on that child's card sets; one at most, since the run asks one at a time.
        self._asking: tuple[str, asyncio.Future[None]] | None = None

    @property
    def client(self) -> Any:
        """The run's own client, read at use: a recovery may replace the provider's."""
        return getattr(self._provider, "client", None) if self.on else None

    @classmethod
    async def start(cls, manager: Any, info: Any, provider: Any) -> RunNativeChildCards:
        """The cards of a new attempt of *info*, after closing the last attempt's."""
        prior = getattr(info, "_native_child_cards", None)
        if isinstance(prior, cls):
            await prior.close(manager, info, completed=False)
        cards = cls(info.id, provider)
        info._native_child_cards = cards
        return cards

    def begin_prompt(self) -> None:
        """Mark the prompt about to go out as the stream that draws the run's Workflow
        agents (``raw_sdk_tasks.begin_draw``)."""
        if self.on and not self.closed:
            self._mark(True)

    async def on_event(self, manager: Any, info: Any, event: Any) -> None:
        """Draw one roster or activity event of the run's own children. Never raises:
        a card that cannot be drawn leaves the run unchanged."""
        if not self.on or self.closed:
            return
        try:
            if event.kind == EVENT_SUBAGENT_LIST:
                self._sync(event.subagents)
            elif event.kind == EVENT_SUBAGENT_ACTIVITY:
                self._activity(event)
            await self._relay(manager, info)
        except Exception:  # noqa: BLE001 - a card is display; the run goes on
            logger.debug("run %s: drawing a native child card failed", self._run_id, exc_info=True)

    async def close(self, manager: Any, info: Any, *, completed: bool) -> None:
        """Close every open card, once: with no error after a run that *completed*, else
        as interrupted. The text a child's redactor still holds goes on its card first."""
        if self.closed:
            return
        self.closed = True
        if not self.on:
            return
        try:
            self._mark(False)
            for event in self._held_text():
                record = self.open_record(event.sub_session_id or "")
                if record is not None and event.text:
                    self._chunk(record["id"], redact(event.text))
            from kiro_crew.dashboard import chat_runner

            chat_runner._native_subagent_close_all(
                self._sink,
                self._slot,
                self.tracker,
                self.output,
                error=None if completed else chat_runner.NATIVE_SUBAGENT_INTERRUPTED_ERROR,
            )
            await self._relay(manager, info)
        except Exception:  # noqa: BLE001 - the run's own end goes out whatever its cards do
            logger.debug("run %s: closing native child cards failed", self._run_id, exc_info=True)
        finally:
            if self.refused:
                logger.warning(
                    "run %s: %d harness-native child(ren) got no card at the bound of %d",
                    self._run_id,
                    self.refused,
                    MAX_RUN_NATIVE_CARDS,
                )
            self.output.clear()
            self.output_len.clear()

    def open_record(self, child_sid: str) -> dict[str, Any] | None:
        """The record of the child *child_sid* while its card is open."""
        record = self.tracker.get(child_sid)
        if isinstance(record, dict) and record.get("id") and not record.get("done"):
            return record
        return None

    def mark_stopped(self, child_sid: str) -> None:
        """Record the card-only Stop of the child *child_sid*, which closed its card, so
        no later roster or close draws it again and a reconnect replays it stopped."""
        record = self.open_record(child_sid)
        if record is None:
            return
        now = time.time()
        record.update(
            done=True,
            done_at=now,
            elapsed=now - float(record.get("started") or now),
            error=None,
            stopped=True,
            outcome="stopped",
            result="(cancelled)",
        )
        self._sink._native_cards.pop(record["id"], None)

    def withdraw(self, child_sid: str) -> bool:
        """End the run's wait on a person for a request the child *child_sid* asked,
        which the harness withdrew when that child was stopped; whether one was waiting.

        Only that child's requests on the run's own client count
        (``native_child_requests``), so the run's own and another child's stay asked.
        """
        asking = self._asking
        requests = getattr(self.client, "native_child_requests", None)
        if asking is None or asking[1].done() or not callable(requests):
            return False
        if asking[0] not in requests(child_sid):
            return False
        asking[1].set_result(None)
        return True

    async def _ask(self, request_id: str, approval: Awaitable[object]) -> object:
        withdrawn: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        approval_wait = asyncio.ensure_future(approval)
        self._asking = (request_id, withdrawn)
        try:
            await asyncio.wait((approval_wait, withdrawn), return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            # The run is being stopped: the approver takes the cancel, as it does
            # when the run awaits it directly.
            approval_wait.cancel()
            return await approval_wait
        finally:
            if self._asking is not None and self._asking[1] is withdrawn:
                self._asking = None
        if approval_wait.done():
            return approval_wait.result()
        approval_wait.cancel()
        # Its own cleanup retires its card; what it then answers is moot.
        await asyncio.wait((approval_wait,))
        if not approval_wait.cancelled() and approval_wait.exception() is not None:
            logger.debug(
                "run %s: a withdrawn request's approver raised",
                self._run_id,
                exc_info=approval_wait.exception(),
            )
        return False

    def wire_id(self, card_id: str) -> str:
        """The id a card goes out under: ``native:<run id>:<child>``."""
        child = card_id[len(_NATIVE) :] if card_id.startswith(_NATIVE) else card_id
        return f"{_NATIVE}{self._run_id}:{child}"

    def replay(self) -> list[tuple[str, dict[str, Any], list[str]]]:
        """Each card drawn: its wire id, its record and the output it still holds."""
        return [
            (self.wire_id(record["id"]), record, self.output.get(record["id"], []))
            for record in list(self.tracker.values())
            if record.get("id")
        ]

    def _sync(self, rows: object) -> None:
        if not isinstance(rows, list):
            return
        room = MAX_RUN_NATIVE_CARDS - len(self.tracker)
        admitted: list[Any] = []
        new: set[str] = set()
        for row in rows:
            sid = str((row.get("sessionId") if isinstance(row, dict) else "") or "")
            if sid and sid not in self.tracker and sid not in new:
                if len(new) >= room:
                    self._refuse(sid)
                    continue
                new.add(sid)
            admitted.append(row)
        from kiro_crew.dashboard import chat_runner

        chat_runner._native_subagent_sync(
            self._sink, self._slot, admitted, self.tracker, self.output
        )

    def _refuse(self, sid: str) -> None:
        if sid in self._refused_ids:
            return
        self.refused += 1
        if len(self._refused_ids) < MAX_REFUSED_IDS:
            self._refused_ids.add(sid)

    def _activity(self, event: Any) -> None:
        record = self.open_record(event.sub_session_id or "")
        if record is None:
            return
        if event.text:
            self._chunk(record["id"], redact(event.text))
        elif event.tool_call_id and event.title:
            tool, _ = redact_exfiltration_urls(event.title)
            tool, _ = redact_credentials(tool)
            self._chunk(record["id"], f"→ {tool[:TOOL_LINE_MAX]}\n")

    def _chunk(self, card_id: str, text: str) -> None:
        if not text:
            return
        from kiro_crew.dashboard import chat_runner

        self.output_len[card_id] = chat_runner._append_native_output(
            self.output.setdefault(card_id, []),
            text,
            self.output_len.get(card_id, 0),
            cap=RUN_CARD_OUTPUT_TAIL,
            hard=RUN_CARD_OUTPUT_HARD,
        )
        self._sink.broadcast_ws("subagent_chunk", {"id": card_id, "text": text})

    def _held_text(self) -> list[Any]:
        flush = getattr(self.client, "flush_native_child_text", None)
        return list(flush()) if callable(flush) else []

    def _mark(self, expected: bool) -> None:
        try:
            expect_dashboard_turn(self.client, expected)
        except Exception:  # noqa: BLE001 - an unmarked prompt draws no Workflow agent
            logger.debug("run %s: marking the prompt's draw failed", self._run_id, exc_info=True)

    async def _relay(self, manager: Any, info: Any) -> None:
        for etype, payload in self._sink.drain():
            card_id = str(payload.get("id") or "")
            frame = {key: value for key, value in payload.items() if key != "slot"}
            frame["id"] = self.wire_id(card_id)
            child = next((s for s, r in self.tracker.items() if r.get("id") == card_id), "")
            await manager._fire_event(
                RELAY_EVENT, info, {"etype": etype, "frame": frame, "child": child}
            )


async def unless_withdrawn(info: Any, event: Any, approval: Awaitable[object]) -> object:
    """What *approval*, a person asked about the request *event* of the run *info*,
    answers; False once a Stop on the card of the native child that asked withdrew it
    (:meth:`RunNativeChildCards.withdraw`). The run's own requests are awaited as is."""
    cards = getattr(info, "_native_child_cards", None)
    if (
        isinstance(cards, RunNativeChildCards)
        and cards.on
        and not cards.closed
        and getattr(event, "native_child_call", False) is True
    ):
        return await cards._ask(str(event.request_id), approval)
    return await approval


async def close_for(manager: Any, info: Any) -> None:
    """Close *info*'s open child cards, ahead of its own ``subagent_done``."""
    cards = getattr(info, "_native_child_cards", None)
    if isinstance(cards, RunNativeChildCards):
        await cards.close(manager, info, completed=info.outcome == "completed")

"""The channel-neutral halves of the shared chat commands (``messaging.commands``).

``/stop``, ``/yolo`` and the dashboard-link TTL vocabulary existed as
near-verbatim copies in three dispatchers. These tests pin the behaviour that used
to be asserted per channel (where the copies could drift), plus the two structural
guarantees that make the extraction safe: the module accepts no address, and
``kiro_crew.messaging`` still imports nothing from the surfaces built on it.
"""

from __future__ import annotations

import ast
import asyncio
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import kiro_crew.messaging.commands as commands
from kiro_crew.dashboard.token_auth import parse_duration
from kiro_crew.messaging.commands import (
    DEFAULT_DASHBOARD_TTL_SECS,
    STOP_REPLY_CANCELLED,
    STOP_REPLY_IDLE,
    YOLO_PHRASING_MARKDOWN,
    YOLO_PHRASING_PLAIN,
    format_ttl,
    parse_dashboard_ttl,
    parse_yolo_action,
    run_yolo_command,
    stop_running_turn,
)
from kiro_crew.messaging.queue_receipt import ReceiptQueue, receipt_text


class _Surface:
    """A receipt surface with its address already bound (what a channel supplies)."""

    label = "fake"

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.edits: list[tuple[Any, str]] = []

    async def send_receipt(self, body: str) -> Any | None:
        self.sent.append(body)
        return 42

    async def edit_receipt(self, msg_id: Any, body: str) -> None:
        self.edits.append((msg_id, body))


class _Provider:
    def __init__(self, *, raises: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self._raises = raises

    async def cancel(self, **kw: Any) -> None:
        self.calls.append(kw)
        if self._raises:
            raise RuntimeError("the agent process is gone")


class _NoCancelProvider:
    """A provider that predates the cancel ext-method."""


class _Sessions:
    """The narrow session-manager surface :func:`stop_running_turn` touches."""

    def __init__(self, *, busy: bool, provider: Any = None, queue: ReceiptQueue | None = None):
        self._busy = busy
        self._provider = provider
        self._queue = queue
        self.cleared: list[str] = []
        #: Whether the receipt lock was held while the queue was cleared.
        self.locked_during_clear: list[bool] = []

    def is_busy(self, key: str) -> bool:
        return self._busy

    def get_provider(self, key: str) -> Any:
        return self._provider

    def clear_queue(self, key: str) -> None:
        self.cleared.append(key)
        if self._queue is not None:
            self.locked_during_clear.append(self._queue.lock.locked())


def _stop(sessions: _Sessions, queue: ReceiptQueue, surface: _Surface) -> str:
    async def go() -> str:
        # A live receipt so the finalize has a bubble to flip, exactly as a
        # mid-turn burst would have left one.
        async with queue.lock:
            await queue.create_or_grow_locked("s", surface, "what time is it")
        return await stop_running_turn(sessions, "s", queue=queue, surface=surface)

    return asyncio.run(go())


class TestStopRunningTurn:
    def test_a_live_turn_is_cancelled_cooperatively(self) -> None:
        """``wait_ack_timeout=0`` is the contract, not an optimisation.

        The write returns without waiting so the acknowledgement to the user is
        immediate and the turn stops at its next safe point; on a shared runtime
        that is the only path that cannot take a co-tenant process down with it.
        """
        provider = _Provider()
        queue, surface = ReceiptQueue(), _Surface()
        sessions = _Sessions(busy=True, provider=provider, queue=queue)
        assert _stop(sessions, queue, surface) == STOP_REPLY_CANCELLED
        assert provider.calls == [{"wait_ack_timeout": 0}]

    def test_the_queue_is_dropped_and_the_receipt_finalized_in_place(self) -> None:
        # The receipt is the durable record that a held message was accepted, so
        # it is EDITED to "🛑 Cancelled" rather than deleted.
        queue, surface = ReceiptQueue(), _Surface()
        sessions = _Sessions(busy=True, provider=_Provider(), queue=queue)
        _stop(sessions, queue, surface)
        assert sessions.cleared == ["s"]
        assert surface.edits == [(42, receipt_text(["what time is it"], cancelled=True))]
        assert not queue.has_receipt("s")

    def test_clear_queue_and_the_finalize_share_one_lock_hold(self) -> None:
        """The drain takes the same lock across dequeue + flip.

        Splitting the two would let a drain observe an already-emptied queue while
        its bubble still said "⏳ Queued", orphaning it.
        """
        queue, surface = ReceiptQueue(), _Surface()
        sessions = _Sessions(busy=True, provider=_Provider(), queue=queue)
        _stop(sessions, queue, surface)
        assert sessions.locked_during_clear == [True]

    def test_an_idle_session_still_clears_the_queue(self) -> None:
        queue, surface = ReceiptQueue(), _Surface()
        sessions = _Sessions(busy=False, provider=_Provider(), queue=queue)
        assert _stop(sessions, queue, surface) == STOP_REPLY_IDLE
        assert sessions.cleared == ["s"]

    def test_a_provider_with_no_cancel_degrades_instead_of_raising(self) -> None:
        queue, surface = ReceiptQueue(), _Surface()
        sessions = _Sessions(busy=True, provider=_NoCancelProvider(), queue=queue)
        assert _stop(sessions, queue, surface) == STOP_REPLY_IDLE
        assert sessions.cleared == ["s"]

    def test_a_failed_cancel_never_claims_a_stop_that_did_not_happen(self) -> None:
        provider = _Provider(raises=True)
        queue, surface = ReceiptQueue(), _Surface()
        sessions = _Sessions(busy=True, provider=provider, queue=queue)
        assert _stop(sessions, queue, surface) == STOP_REPLY_IDLE
        assert provider.calls == [{"wait_ack_timeout": 0}]
        assert sessions.cleared == ["s"], "the queue must be cleared either way"


def _reset_grant() -> Any:
    from kiro_crew.safety_override import safety_override

    so = safety_override()
    if so.is_active():
        so.deactivate("test")
    return so


class _Sel:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def log_api_access(self, **kw: Any) -> None:
        self.rows.append(kw)


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> _Sel:
    rec = _Sel()
    monkeypatch.setattr(commands, "sel", lambda: rec)
    return rec


@pytest.fixture
def grant() -> Iterator[Any]:
    so = _reset_grant()
    try:
        yield so
    finally:
        _reset_grant()


class TestParseYoloAction:
    def test_the_first_word_wins_and_case_does_not_matter(self) -> None:
        assert parse_yolo_action(" ON please ") == "on"
        assert parse_yolo_action("") == ""


class TestRunYoloCommand:
    """One ladder, one set of replies, one SEL row shape for every channel."""

    def test_on_arms_the_shared_grant_and_audits_it(self, grant: Any, audit: _Sel) -> None:
        reply = asyncio.run(
            run_yolo_command("on", source="telegram", caller="7", phrasing=YOLO_PHRASING_PLAIN)
        )
        assert grant.is_active() is True
        assert reply.startswith("🟢 YOLO ON (")
        assert "Denied-by-policy tools are still blocked." in reply
        assert audit.rows == [
            {
                "caller": "7",
                "operation": "telegram.yolo_mode",
                "outcome": "allowed",
                "source": "telegram",
                "resources": "yolo_on",
            }
        ]

    def test_on_while_already_on_reports_instead_of_re_arming(
        self, grant: Any, audit: _Sel
    ) -> None:
        grant.activate("test")
        reply = asyncio.run(
            run_yolo_command(
                "on", source="teams", caller="u@example.com", phrasing=YOLO_PHRASING_MARKDOWN
            )
        )
        assert reply.startswith("🟢 YOLO is already ON (")
        assert audit.rows[0]["operation"] == "teams.yolo_mode"
        assert audit.rows[0]["source"] == "teams"

    def test_off_revokes_and_reports(self, grant: Any, audit: _Sel) -> None:
        grant.activate("test")
        reply = asyncio.run(
            run_yolo_command("off", source="telegram", caller="7", phrasing=YOLO_PHRASING_PLAIN)
        )
        assert reply == "🔴 YOLO OFF — tools ask for approval again."
        assert grant.is_active() is False
        assert audit.rows[0]["resources"] == "yolo_off"

    def test_renew_on_an_inactive_grant_names_the_channels_own_command(
        self, grant: Any, audit: _Sel
    ) -> None:
        plain = asyncio.run(
            run_yolo_command("renew", source="telegram", caller="7", phrasing=YOLO_PHRASING_PLAIN)
        )
        coded = asyncio.run(
            run_yolo_command("renew", source="teams", caller="u", phrasing=YOLO_PHRASING_MARKDOWN)
        )
        assert plain == "🔴 YOLO is not active — use /yolo on first."
        assert coded == "🔴 YOLO is not active — use `/yolo on` first."
        assert grant.is_active() is False

    def test_a_failed_activation_is_audited_as_denied(
        self, grant: Any, audit: _Sel, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.safety_override import ActivationResult

        monkeypatch.setattr(
            grant,
            "activate",
            lambda source: ActivationResult(
                active=False, ttl=0, source=source, activated_at_iso=""
            ),
        )
        reply = asyncio.run(
            run_yolo_command("on", source="telegram", caller="7", phrasing=YOLO_PHRASING_PLAIN)
        )
        assert reply == "❌ Couldn't turn YOLO on (audit system unavailable)."
        assert audit.rows[0]["outcome"] == "denied"

    @pytest.mark.parametrize("arg", ["", "maybe", "   "])
    def test_anything_but_a_verb_reports_status_and_audits_nothing(
        self, arg: str, grant: Any, audit: _Sel
    ) -> None:
        # An unrecognised word must never arm the grant, and a status read is not
        # a security event, so it writes no row.
        reply = asyncio.run(
            run_yolo_command(arg, source="telegram", caller="7", phrasing=YOLO_PHRASING_PLAIN)
        )
        assert reply == "YOLO is OFF 🔴.\nUsage: /yolo on | off | renew"
        assert grant.is_active() is False
        assert audit.rows == []

    def test_status_reports_the_live_lifetime_when_armed(self, grant: Any, audit: _Sel) -> None:
        grant.activate("test")
        reply = asyncio.run(
            run_yolo_command("", source="teams", caller="u", phrasing=YOLO_PHRASING_MARKDOWN)
        )
        assert reply.startswith("YOLO is ON 🟢 (")
        assert reply.endswith("Usage: `/yolo on` | `off` | `renew`")

    def test_the_mutators_run_off_the_event_loop(
        self, grant: Any, audit: _Sel, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``activate`` reads config and writes a SEL record.

        Both are filesystem work, and the gateway runs one loop for every
        conversation and heartbeat task, so an inline call stalls all of them on a
        slow disk.
        """
        ran_on: list[int] = []
        original = grant.activate

        def _recording(source: str) -> Any:
            ran_on.append(threading.get_ident())
            return original(source)

        monkeypatch.setattr(grant, "activate", _recording)

        async def go() -> None:
            loop_thread = threading.get_ident()
            await run_yolo_command(
                "on", source="telegram", caller="7", phrasing=YOLO_PHRASING_PLAIN
            )
            assert ran_on and loop_thread not in ran_on

        asyncio.run(go())


class TestParseDashboardTtl:
    """The TTL comes from the command's ARGUMENT, never from a word index."""

    def test_no_duration_falls_back_to_the_default(self) -> None:
        assert parse_dashboard_ttl("", parse_duration=parse_duration) == 3600
        assert DEFAULT_DASHBOARD_TTL_SECS == 3600

    def test_hours_and_minutes_in_either_case(self) -> None:
        assert parse_dashboard_ttl("2h", parse_duration=parse_duration) == 7200
        assert parse_dashboard_ttl("5H", parse_duration=parse_duration) == 18000
        assert parse_dashboard_ttl("30m", parse_duration=parse_duration) == 1800
        assert parse_dashboard_ttl("90M", parse_duration=parse_duration) == 5400

    def test_an_unparseable_duration_still_yields_a_working_link(self) -> None:
        assert parse_dashboard_ttl("xyz", parse_duration=parse_duration) == 3600

    def test_only_the_first_word_is_read(self) -> None:
        # Trailing words are ignored rather than making the argument unparseable,
        # which is what Telegram's third-word slice did.
        seen: list[str] = []

        def _parse(token: str) -> int | None:
            seen.append(token)
            return parse_duration(token)

        assert parse_dashboard_ttl("  2h and hurry ", parse_duration=_parse) == 7200
        assert seen == ["2h"], "the parser saw more than the duration token"


class TestFormatTtl:
    def test_exact_hours(self) -> None:
        assert format_ttl(3600) == "1h"
        assert format_ttl(7200) == "2h"

    def test_minutes_only(self) -> None:
        assert format_ttl(1800) == "30m"
        assert format_ttl(60) == "1m"

    def test_mixed_never_truncates(self) -> None:
        """90m must NOT display as '1h' -- the link lives 1.5h."""
        assert format_ttl(5400) == "1h 30m"
        assert format_ttl(3660) == "1h 1m"

    def test_sub_minute_floors_to_zero_minutes(self) -> None:
        # parse_duration never yields <60s, but the formatter stays total.
        assert format_ttl(0) == "0m"
        assert format_ttl(59) == "0m"


class TestLayering:
    def test_nothing_shared_accepts_an_address(self) -> None:
        """The reason forum routing and service URLs stay channel-local.

        Every send in these handlers is address-shaped, so the shared half returns
        reply TEXT and reaches a receipt bubble only through the already-bound
        ``ReceiptSurface``. A parameter named for one channel's address is how the
        module would acquire the first per-channel branch.
        """
        import inspect

        banned = {"chat_id", "channel_id", "conversation_id", "thread", "thread_id", "service_url"}
        offenders = {}
        for name, obj in vars(commands).items():
            if (
                name.startswith("_")
                or not callable(obj)
                or getattr(obj, "__module__", "") != (commands.__name__)
            ):
                continue
            params = set(inspect.signature(obj).parameters) & banned
            if params:
                offenders[name] = sorted(params)
        assert not offenders, offenders

    #: The ONE edge from ``messaging`` into a surface, and why it is allowed:
    #: ``build_directive_consumer`` applies a decoded session directive through
    #: ``dashboard.session_directive_apply``, which is the SHARED applier the
    #: dashboard's own consumer uses — so the dashboard-only denial and the
    #: monitor-trio authorization chokepoint live in exactly one place. Passing the
    #: applier in as a parameter (the pattern ``parse_dashboard_ttl`` uses) would put
    #: that security boundary behind a caller-supplied callable, which is a worse
    #: trade than one documented deferred import. Recorded as a pair so a NEW edge
    #: still fails, and so removing this one does not silently widen the gate.
    _ALLOWED_SURFACE_EDGES = {
        ("dispatch.py", "kiro_crew.dashboard.session_directive_apply"),
    }

    def test_messaging_imports_nothing_from_the_surfaces_built_on_it(self) -> None:
        """``<channel>/dashboard -> messaging``, never the reverse.

        Scanned at any nesting depth, because a deferred in-function import is
        still an edge in the graph — that is exactly how the dashboard-link TTL
        helper would have dragged ``dashboard.token_auth`` in here, and why it
        takes its duration parser as a parameter instead. The single recorded
        exception is :data:`_ALLOWED_SURFACE_EDGES`.
        """
        pkg = Path(commands.__file__).resolve().parent
        offenders: list[str] = []
        for path in sorted(pkg.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                elif isinstance(node, ast.Import):
                    module = ",".join(a.name for a in node.names)
                else:
                    continue
                if "kiro_crew.dashboard" not in module and "kiro_crew.slack" not in module:
                    continue
                if (path.name, module) in self._ALLOWED_SURFACE_EDGES:
                    continue
                offenders.append(f"{path.name}:{node.lineno} -> {module}")
        assert not offenders, offenders

    def test_the_allowed_edge_list_has_no_stale_entries(self) -> None:
        """An exception that no longer exists must be deleted, not left to rot.

        Without this the list only ever grows, and a stale entry silently
        pre-authorizes an edge a future change might reintroduce for a different
        and much worse reason.
        """
        pkg = Path(commands.__file__).resolve().parent
        present: set[tuple[str, str]] = set()
        for path in sorted(pkg.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    present.add((path.name, node.module or ""))
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        present.add((path.name, alias.name))
        stale = sorted(self._ALLOWED_SURFACE_EDGES - present)
        assert not stale, f"remove these from _ALLOWED_SURFACE_EDGES: {stale}"

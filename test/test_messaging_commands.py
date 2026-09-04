"""The channel-neutral halves of the shared chat commands (``messaging.commands``).

Two families, one module, so one test file. ``/stop``, ``/yolo`` and the
dashboard-link TTL vocabulary existed as near-verbatim copies in three dispatchers;
the ``spawn`` / ``cron`` / ``task run`` keyword replies existed only inside
``slack/handler.py``. These tests pin the behaviour that used to be asserted per
channel (where the copies could drift), the CONTRACT the hoist has to preserve --
the ``None`` sentinel meaning "not this command, keep routing", the retryable busy
answer, and the redaction every reply owes an external surface -- and the two
structural guarantees that make the extraction safe: the module accepts no address,
and ``kiro_crew.messaging`` still imports nothing from the surfaces built on it.
"""

from __future__ import annotations

import ast
import asyncio
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import kiro_crew.messaging.commands as commands
from kiro_crew.cron import CronJob, CronSchedule, CronStoreBusy
from kiro_crew.dashboard.token_auth import parse_duration
from kiro_crew.messaging.commands import (
    _CRON_BUSY,
    DEFAULT_DASHBOARD_TTL_SECS,
    MIN_DASHBOARD_TTL_SECS,
    STOP_REPLY_CANCELLED,
    STOP_REPLY_IDLE,
    YOLO_PHRASING_MARKDOWN,
    YOLO_PHRASING_PLAIN,
    cron_command_reply,
    cron_remove_all_reply,
    format_ttl,
    parse_dashboard_ttl,
    parse_yolo_action,
    run_yolo_command,
    spawn_command_reply,
    spawn_task_reply,
    stop_running_turn,
    task_arg_reply,
    task_command_reply,
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

    def test_an_explicit_zero_is_clamped_to_the_floor(self) -> None:
        """A zero PARSES, so every "did it parse" check passes it through.

        `parse_duration("0h")` answers 0 -- a real int, not None -- so an unclamped
        parser hands the token minter a lifetime of zero and the user gets a link
        that is already expired, with nothing in the reply saying why. The floor is
        also what keeps that reply honest: the granted value is rendered with
        `format_ttl`, which would otherwise print `0m`.
        """
        for arg in ("0h", "0m", "0H", "0M"):
            assert parse_dashboard_ttl(arg, parse_duration=parse_duration) == (
                MIN_DASHBOARD_TTL_SECS
            ), arg
        assert MIN_DASHBOARD_TTL_SECS > 0
        assert format_ttl(MIN_DASHBOARD_TTL_SECS) == "1m"

    def test_a_duration_above_the_floor_is_untouched(self) -> None:
        """The clamp must reject exactly one input, not raise every short link."""
        assert parse_dashboard_ttl("1m", parse_duration=parse_duration) == 60
        assert parse_dashboard_ttl("2m", parse_duration=parse_duration) == 120

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


_AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"

#: A non-Slack conversation key. Namespaced rather than a bare id because this is
#: the shape every channel's session key has, and the value is forwarded verbatim.
_TG_KEY = "telegram:kirocrew:direct:U9"


def _job(job_id: str = "j1", **kw: Any) -> CronJob:
    """A real ``CronJob``, not a namespace.

    ``compute_next_run_ts`` reads ``schedule.kind``/``.cron_expr``, so a duck-typed
    stand-in passes the ``list_jobs`` boundary and then fails inside the relative-time
    arithmetic — which is exactly the row the listing has to render.
    """
    return CronJob(
        id=job_id,
        name=kw.get("name", "nightly digest"),
        message=kw.get("message", "summarize the day"),
        schedule=CronSchedule(kind="cron", cron_expr=kw.get("cron_expr", "0 9 * * *")),
        enabled=kw.get("enabled", True),
        last_status=kw.get("last_status", "ok"),
    )


class TestNotThisCommand:
    """``None`` is the sentinel that keeps normal routing going."""

    @pytest.mark.parametrize("text", ["", "hello", "spawnish thing", "  ", "bgone"])
    def test_spawn_declines_text_that_is_not_a_spawn(self, text: str) -> None:
        assert spawn_command_reply(text, MagicMock()) is None

    @pytest.mark.parametrize("text", ["", "cron", "crond list", "not cron list"])
    @pytest.mark.asyncio
    async def test_cron_declines_text_that_is_not_a_cron_command(self, text: str) -> None:
        assert await cron_command_reply(text, MagicMock()) is None

    @pytest.mark.parametrize("text", ["", "task", "task runner", "run the thing"])
    @pytest.mark.asyncio
    async def test_task_declines_text_that_is_not_a_task_command(self, text: str) -> None:
        assert await task_command_reply(text, MagicMock()) is None

    @pytest.mark.asyncio
    async def test_an_unknown_cron_verb_declines_rather_than_guessing(self) -> None:
        assert await cron_command_reply("cron obliterate j1", MagicMock()) is None


class TestSpawn:
    def test_both_prefixes_reach_the_same_spawn(self) -> None:
        manager = MagicMock(max_concurrent=2)
        manager.spawn.return_value = SimpleNamespace(id="z9")
        for text in ("spawn do it", "bg do it", "SPAWN do it"):
            manager.spawn.reset_mock()
            assert "z9" in (spawn_command_reply(text, manager) or "")
            assert manager.spawn.call_args.args[0] == "do it"

    def test_the_parsed_form_is_public_for_a_prefixed_command_grammar(self) -> None:
        # A channel whose own grammar carries the prefix (/spawn, !spawn) has the
        # argument already; it must not have to rebuild "spawn " + arg.
        manager = MagicMock(max_concurrent=2)
        manager.spawn.return_value = SimpleNamespace(id="q1")
        assert "q1" in (spawn_task_reply("do it", manager) or "")

    def test_an_empty_argument_declines(self) -> None:
        assert spawn_task_reply("", MagicMock()) is None
        assert spawn_command_reply("spawn    ", MagicMock()) is None

    @pytest.mark.parametrize("verb", ["list", "status", "LIST"])
    def test_the_list_verbs_report_an_empty_roster(self, verb: str) -> None:
        assert spawn_task_reply(verb, MagicMock(running=[])) == "No subagents running."

    def test_a_running_subagent_is_listed_with_its_elapsed_time(self) -> None:
        agent = SimpleNamespace(id="a7", started=time.time() - 5, task="reindex the corpus")
        out = spawn_task_reply("list", MagicMock(running=[agent])) or ""
        assert "a7" in out and "reindex the corpus" in out

    def test_capacity_is_reported_with_the_limit_that_was_reached(self) -> None:
        manager = MagicMock(max_concurrent=3)
        manager.spawn.return_value = None
        assert "capacity reached (3)" in (spawn_task_reply("work", manager) or "")

    def test_the_echoed_task_is_redacted(self) -> None:
        # The echo goes to an external surface and into the persisted log, and the
        # task is free-form text a user typed or an LLM proposed.
        manager = MagicMock(max_concurrent=2)
        manager.spawn.return_value = SimpleNamespace(id="r1")
        out = spawn_task_reply(f"push with {_AWS_KEY}", manager) or ""
        assert _AWS_KEY not in out

    def test_a_listed_task_is_redacted(self) -> None:
        agent = SimpleNamespace(id="a1", started=time.time(), task=f"key {_AWS_KEY}")
        out = spawn_task_reply("list", MagicMock(running=[agent])) or ""
        assert _AWS_KEY not in out


class TestCron:
    @pytest.mark.asyncio
    async def test_an_empty_roster_says_so(self) -> None:
        svc = MagicMock()
        svc.list_jobs.return_value = []
        assert await cron_command_reply("cron list", svc) == "No cron jobs scheduled."

    @pytest.mark.asyncio
    async def test_a_disabled_job_is_marked_and_still_listed(self) -> None:
        svc = MagicMock()
        svc.list_jobs.return_value = [_job("j1", enabled=False)]
        out = await cron_command_reply("cron list", svc) or ""
        assert "⏸️" in out and "`j1`" in out
        # include_disabled is what makes a paused job visible enough to resume.
        assert svc.list_jobs.call_args.kwargs == {"include_disabled": True}

    @pytest.mark.asyncio
    async def test_a_job_message_is_redacted_in_the_listing(self) -> None:
        svc = MagicMock()
        svc.list_jobs.return_value = [_job("j1", message=f"post {_AWS_KEY}")]
        assert _AWS_KEY not in (await cron_command_reply("cron list", svc) or "")

    @pytest.mark.parametrize(
        "verb,enabled,mark",
        [("pause", False, "⏸️"), ("resume", True, "▶️")],
    )
    @pytest.mark.asyncio
    async def test_pause_and_resume_pass_the_right_enabled_flag(
        self, verb: str, enabled: bool, mark: str
    ) -> None:
        svc = MagicMock()
        svc.enable_job_async = AsyncMock(return_value=True)
        out = await cron_command_reply(f"cron {verb} j1", svc) or ""
        assert mark in out and "`j1`" in out
        assert svc.enable_job_async.await_args.kwargs == {"enabled": enabled}

    @pytest.mark.asyncio
    async def test_a_missing_job_is_reported_not_claimed_as_done(self) -> None:
        svc = MagicMock()
        svc.remove_job_async = AsyncMock(return_value=False)
        assert "not found" in (await cron_command_reply("cron remove j9", svc) or "")

    @pytest.mark.parametrize("text", ["cron remove j1", "cron pause j1", "cron resume j1"])
    @pytest.mark.asyncio
    async def test_a_contended_store_answers_retryably(self, text: str) -> None:
        # One wording for every verb: a caller who sees a different string per verb
        # cannot tell "retry this" from "this failed".
        svc = MagicMock()
        svc.remove_job_async = AsyncMock(side_effect=CronStoreBusy())
        svc.enable_job_async = AsyncMock(side_effect=CronStoreBusy())
        assert await cron_command_reply(text, svc) == _CRON_BUSY

    @pytest.mark.asyncio
    async def test_remove_all_reports_each_job_and_batches_one_write(self) -> None:
        svc = MagicMock()
        svc.list_jobs.return_value = [_job("j1"), _job("j2")]
        # The command forwards the whole batch to the service's mutation/audit seam.
        svc.remove_jobs = AsyncMock(return_value=(["j1", "j2"], []))
        out = await cron_command_reply("cron remove all", svc) or ""
        assert "Removed 2 cron job(s)" in out and "`j1`" in out and "`j2`" in out
        svc.remove_jobs.assert_awaited_once_with(["j1", "j2"], actor="system", source="messaging")

    @pytest.mark.asyncio
    async def test_remove_all_redacts_each_job_name(self) -> None:
        svc = MagicMock()
        svc.list_jobs.return_value = [_job("j1", name=f"leak {_AWS_KEY}")]
        svc.remove_jobs = AsyncMock(return_value=(["j1"], []))
        assert _AWS_KEY not in (await cron_remove_all_reply(svc) or "")

    @pytest.mark.asyncio
    async def test_every_channel_forwards_delete_attribution_not_only_slack(self) -> None:
        """Every shared command caller reaches the service-owned audit seam.

        Actor and source arrived on Slack's old copy of `cron remove`; hoisting the
        command must retain them for every channel without a duplicate command-level
        audit.
        """
        svc = MagicMock()
        svc.list_jobs.return_value = [_job("j1")]
        svc.remove_jobs = AsyncMock(return_value=(["j1"], []))
        svc.remove_job_async = AsyncMock(return_value=True)

        with patch("kiro_crew.messaging.commands.sel") as mock_sel:
            await cron_command_reply("cron remove all", svc, source="telegram", caller="7")
            await cron_command_reply("cron remove j1", svc, source="telegram", caller="7")

        svc.remove_jobs.assert_awaited_once_with(["j1"], actor="7", source="telegram")
        svc.remove_job_async.assert_awaited_once_with("j1", actor="7", source="telegram")
        mock_sel.return_value.log_api_access.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_caller_that_names_nobody_still_leaves_a_record(self) -> None:
        # A channel with no user id in hand gets the record with the surface as the
        # subject. Worth more than no record: it is the only way to tell a deliberate
        # remove-all from data loss once the jobs are gone.
        svc = MagicMock()
        svc.list_jobs.return_value = [_job("j1")]
        svc.remove_jobs = AsyncMock(return_value=(["j1"], []))

        with patch("kiro_crew.messaging.commands.sel") as mock_sel:
            await cron_command_reply("cron remove all", svc, source="webex")

        svc.remove_jobs.assert_awaited_once_with(["j1"], actor="webex", source="webex")
        mock_sel.return_value.log_api_access.assert_not_called()

    @pytest.mark.asyncio
    async def test_remove_all_on_an_empty_roster_touches_nothing(self) -> None:
        svc = MagicMock()
        svc.list_jobs.return_value = []
        svc.remove_jobs = AsyncMock()
        assert await cron_remove_all_reply(svc) == "No cron jobs to remove."
        svc.remove_jobs.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_remove_all_survives_a_contended_store(self) -> None:
        svc = MagicMock()
        svc.list_jobs.return_value = [_job()]
        svc.remove_jobs = AsyncMock(side_effect=CronStoreBusy())
        assert await cron_remove_all_reply(svc) == _CRON_BUSY


class TestTaskRunner:
    @pytest.mark.asyncio
    async def test_project_run_is_accepted_as_an_alias(self, tmp_path: Any) -> None:
        spec = tmp_path / "plan.yaml"
        spec.write_text("steps: []", encoding="utf-8")
        runner = MagicMock(running=False)
        runner.start_background = AsyncMock()
        assert "plan.yaml" in (await task_command_reply(f"project run {spec}", runner) or "")

    @pytest.mark.asyncio
    async def test_an_absent_spec_is_refused_before_the_runner_is_touched(
        self, tmp_path: Any
    ) -> None:
        runner = MagicMock(running=False)
        runner.start_background = AsyncMock()
        out = await task_command_reply(f"task run {tmp_path / 'nope.yaml'}", runner) or ""
        assert "not found" in out
        runner.start_background.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_second_run_is_refused_while_one_is_live(self) -> None:
        out = await task_command_reply("task run /tmp/x.yaml", MagicMock(running=True)) or ""
        assert "already running" in out

    @pytest.mark.asyncio
    async def test_status_reports_the_live_run_not_the_first_one(self) -> None:
        runner = MagicMock()
        runner.status.return_value = {
            "running": True,
            "runs": [
                {"running": False, "status": "done", "completed": 3, "tasks": 3},
                {
                    "running": True,
                    "status": "working",
                    "completed": 1,
                    "tasks": 4,
                    "current_task": 2,
                },
            ],
        }
        out = await task_command_reply("task run status", runner) or ""
        assert "working" in out and "1/4" in out and "step 2" in out

    @pytest.mark.asyncio
    async def test_status_with_nothing_running_says_so(self) -> None:
        runner = MagicMock()
        runner.status.return_value = {"running": False}
        assert await task_command_reply("task run status", runner) == "No task running."

    @pytest.mark.asyncio
    async def test_cancel_only_cancels_when_something_runs(self) -> None:
        idle = MagicMock(running=False)
        assert await task_command_reply("task run cancel", idle) == "No task running."
        idle.cancel.assert_not_called()
        live = MagicMock(running=True)
        assert "cancelled" in (await task_command_reply("task run cancel", live) or "")
        live.cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_start_failure_is_reported_redacted_not_raised(self, tmp_path: Any) -> None:
        spec = tmp_path / "plan.yaml"
        spec.write_text("steps: []", encoding="utf-8")
        runner = MagicMock(running=False)
        runner.start_background = AsyncMock(side_effect=RuntimeError(f"bad {_AWS_KEY}"))
        out = await task_command_reply(f"task run {spec}", runner) or ""
        assert "Failed to start" in out and _AWS_KEY not in out

    @pytest.mark.asyncio
    async def test_the_keyword_grammar_carries_the_session_key(self, tmp_path: Any) -> None:
        """``task run <spec>`` must escalate where ``/task run <spec>`` does.

        The runner hands ``session_key`` to its notify sink, which is what sends an
        approval notice back to the conversation the operator is watching instead
        of only to the Slack owner DM. Both grammars reach the same runner, so a
        key carried on one entry point and dropped on the other means the same run
        escalates to a different place depending on how it was typed.
        """
        spec = tmp_path / "plan.yaml"
        spec.write_text("steps: []", encoding="utf-8")
        runner = MagicMock(running=False)
        runner.start_background = AsyncMock()
        out = await task_command_reply(f"task run {spec}", runner, session_key=_TG_KEY) or ""
        assert "plan.yaml" in out
        assert runner.start_background.await_args.kwargs["session_key"] == _TG_KEY

    @pytest.mark.asyncio
    async def test_omitting_the_session_key_keeps_the_narrow_runner_call(
        self, tmp_path: Any
    ) -> None:
        """A stand-in accepting only ``(path, source=)`` must still start.

        ``runner`` is duck-typed here — this module may not import ``TaskRunner``
        at runtime — so widening the call unconditionally would turn a working
        command into "Failed to start" for every narrower runner.
        """
        spec = tmp_path / "plan.yaml"
        spec.write_text("steps: []", encoding="utf-8")
        runner = MagicMock(running=False)
        runner.start_background = AsyncMock()
        assert "plan.yaml" in (await task_command_reply(f"task run {spec}", runner) or "")
        assert "session_key" not in runner.start_background.await_args.kwargs


class TestTaskArgReply:
    """The already-parsed entry point, which is where the ``run`` verb is absorbed.

    The keyword grammar spells the verb ``task run``, but a channel whose command
    IS ``/task`` receives ``run <spec>`` as its argument — re-composing
    ``"task run " + arg`` handed the runner a spec named ``run <spec>``.
    """

    @pytest.mark.asyncio
    async def test_a_leading_run_verb_is_absorbed(self, tmp_path: Any) -> None:
        spec = tmp_path / "plan.yaml"
        spec.write_text("steps: []", encoding="utf-8")
        runner = MagicMock(running=False)
        runner.start_background = AsyncMock()
        assert "plan.yaml" in (await task_arg_reply(f"run {spec}", runner) or "")
        assert runner.start_background.await_args.args[0].name == "plan.yaml"

    @pytest.mark.asyncio
    async def test_a_bare_spec_works_too(self, tmp_path: Any) -> None:
        spec = tmp_path / "plan.yaml"
        spec.write_text("steps: []", encoding="utf-8")
        runner = MagicMock(running=False)
        runner.start_background = AsyncMock()
        assert "plan.yaml" in (await task_arg_reply(str(spec), runner) or "")

    @pytest.mark.parametrize("arg", ["", "   ", "run", "run   "])
    @pytest.mark.asyncio
    async def test_a_verb_with_no_argument_declines(self, arg: str) -> None:
        runner = MagicMock(running=False)
        runner.start_background = AsyncMock()
        assert await task_arg_reply(arg, runner) is None
        runner.start_background.assert_not_awaited()

    @pytest.mark.parametrize("arg", ["status", "cancel"])
    @pytest.mark.asyncio
    async def test_the_bare_verbs_reach_their_branches(self, arg: str) -> None:
        runner = MagicMock(running=False)
        runner.status.return_value = {"running": False}
        assert await task_arg_reply(arg, runner) == "No task running."


class TestTaskSpecPathIsGated:
    """A task spec is READ and its contents reach the model.

    So an unvalidated path is an exfiltration primitive rather than a usability
    question: ``task run ~/.ssh/id_rsa`` would hand a private key to a third-party
    LLM. Both grammars route through this one module, so Slack's ``task run`` and a
    channel's ``/task`` argument are covered by the same gate.
    """

    @pytest.mark.asyncio
    async def test_a_sensitive_path_is_refused_before_the_runner_sees_it(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        runner = MagicMock(running=False)
        runner.start_background = AsyncMock()
        # Point the sensitive-root check at a real directory this test owns, so
        # the assertion does not depend on the host having ~/.ssh.
        secret_dir = tmp_path / "dot-ssh"
        secret_dir.mkdir()
        key = secret_dir / "id_rsa"
        key.write_text("PRIVATE KEY", encoding="utf-8")
        monkeypatch.setattr(
            "kiro_crew.hooks.is_sensitive_path",
            lambda p: str(secret_dir) in str(p),
        )

        reply = await task_arg_reply(f"run {key}", runner)

        runner.start_background.assert_not_awaited()
        assert reply is not None
        # The refusal must not echo the path back into the channel, and must not
        # say WHY: distinguishing "sensitive" from "missing" is a probing oracle.
        assert str(key) not in reply
        assert "id_rsa" not in reply

    @pytest.mark.asyncio
    async def test_a_symlink_into_a_sensitive_root_is_refused_through_the_link(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        """The reason the shared helper is used instead of a prefix test.

        A path that looks innocent resolves into a blocked root, so the check has
        to run on the RESOLVED target.
        """
        runner = MagicMock(running=False)
        runner.start_background = AsyncMock()
        secret_dir = tmp_path / "dot-ssh"
        secret_dir.mkdir()
        (secret_dir / "id_rsa").write_text("PRIVATE KEY", encoding="utf-8")
        link = tmp_path / "plan.yaml"
        link.symlink_to(secret_dir / "id_rsa")
        monkeypatch.setattr(
            "kiro_crew.hooks.is_sensitive_path",
            lambda p: str(secret_dir) in str(p),
        )

        assert await task_arg_reply(f"run {link}", runner) is not None
        runner.start_background.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_runner_receives_the_canonical_path_not_the_raw_argument(
        self, tmp_path: Any
    ) -> None:
        """Validating one string and acting on another is an ornamental guard."""
        runner = MagicMock(running=False)
        runner.start_background = AsyncMock()
        real = tmp_path / "real.yaml"
        real.write_text("steps: []", encoding="utf-8")
        link = tmp_path / "alias.yaml"
        link.symlink_to(real)

        await task_arg_reply(f"run {link}", runner)

        handed = runner.start_background.await_args.args[0]
        assert handed.name == "real.yaml"

    @pytest.mark.asyncio
    async def test_an_ordinary_spec_still_runs(self, tmp_path: Any) -> None:
        """Non-vacuity: the gate must not refuse everything."""
        runner = MagicMock(running=False)
        runner.start_background = AsyncMock()
        spec = tmp_path / "plan.yaml"
        spec.write_text("steps: []", encoding="utf-8")
        assert "plan.yaml" in (await task_arg_reply(f"run {spec}", runner) or "")
        runner.start_background.assert_awaited_once()


class TestSlackKeywordCarriesTheSessionKey:
    """The `task run <spec>` KEYWORD grammar carries the session key too.

    ``task_arg_reply`` (the ``/task`` slash grammar) gained ``session_key`` so a
    blocked task could report back to the conversation the operator is watching.
    ``task_command_reply`` (the bare ``task run …`` keyword grammar, which is Slack's
    route) did not, so the same task typed as a keyword still escalated only to the
    owner DM — the kwarg was passed by one of its two callers.
    """

    @pytest.mark.asyncio
    async def test_the_slack_handler_forwards_the_session_key(self) -> None:
        from kiro_crew.slack import handler as sh

        seen: list[dict] = []

        async def _reply(text: str, runner: Any, *, session_key: str = "") -> str:
            seen.append({"text": text, "session_key": session_key})
            return "started"

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(sh, "task_command_reply", _reply)
            out = await sh._handle_run_command(
                "task run spec.md",
                object(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
                "C1",
                "1.2",
                session_key="slack:kirocrew:direct:U1",
            )
        assert out == "started"
        assert seen and seen[0]["session_key"] == "slack:kirocrew:direct:U1"

    @pytest.mark.asyncio
    async def test_omitting_it_reproduces_the_old_behaviour(self) -> None:
        # Keyword-only with a default, so the ~25 existing positional call sites are
        # unchanged and an omitted key means owner-DM-only exactly as before.
        from kiro_crew.slack import handler as sh

        seen: list[str] = []

        async def _reply(text: str, runner: Any, *, session_key: str = "") -> str:
            seen.append(session_key)
            return "started"

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(sh, "task_command_reply", _reply)
            await sh._handle_run_command(
                "task run spec.md", object(), object(), "C1", "1.2"  # type: ignore[arg-type]
            )
        assert seen == [""]


class TestListsHostState:
    """Which arguments make a reply host-wide, asked where those replies are built."""

    @pytest.mark.parametrize("arg", ["list", "status", "LIST", " Status ", "sTaTuS"])
    def test_the_listing_arguments_are_recognized_however_typed(self, arg: str) -> None:
        # The argument parser does not normalize case or trim, and a user types what
        # they type. A case-sensitive test here would let `/spawn LIST` through.
        assert commands.lists_host_state("spawn", arg) is True

    @pytest.mark.parametrize(
        "arg",
        ["", "run ~/spec.md", "summarise the log", "cancel", "listing the files", "statuses"],
    )
    def test_a_conversation_scoped_argument_is_not(self, arg: str) -> None:
        # `cancel` acts on the global runner but its reply names nothing, and
        # refusing it would stop a forum operator halting a run they can see.
        # The last two pin that the match is the WHOLE argument, not a prefix.
        assert commands.lists_host_state("spawn", arg) is False

    @pytest.mark.parametrize("arg", ["run status", "RUN Status", "  run   status  ", "status"])
    def test_task_absorbs_its_run_prefix_before_classifying(self, arg: str) -> None:
        """`/task run status` IS `status`, so the scope check has to see it that way.

        `task_arg_reply` strips a leading `run` before dispatching, so a predicate
        that skipped that strip would classify `run status` as a task named
        "status" and let a host-wide listing answer in a shared audience.
        """
        assert commands.lists_host_state("task", arg) is True

    def test_the_run_prefix_is_absorbed_for_task_only(self) -> None:
        # `spawn` has no `run` verb, so `spawn run status` is a task description that
        # happens to start with the word run. Absorbing it there would refuse a
        # legitimate spawn in a Topic.
        assert commands.lists_host_state("spawn", "run status") is False

    @pytest.mark.parametrize("arg", ["run ~/spec.md", "run", "run cancel"])
    def test_a_run_prefixed_non_listing_is_still_not_one(self, arg: str) -> None:
        assert commands.lists_host_state("task", arg) is False

    def test_the_normalizer_is_the_one_the_dispatcher_uses(self) -> None:
        """Pins that the strip has ONE implementation, not two that agree today.

        Two copies is how the dispatch string and the classified string came apart in
        the first place, so this asserts the shared helper produces exactly what the
        reply path dispatches on.
        """
        assert commands.normalize_task_arg("run status") == "status"
        assert commands.normalize_task_arg("  RUN   ~/spec.md ") == "~/spec.md"
        assert commands.normalize_task_arg("running the tests") == "running the tests"
        assert commands.normalize_task_arg("run") == ""

    def test_the_spawn_listing_really_does_ignore_the_session(self) -> None:
        """The premise, asserted rather than assumed.

        The gate exists because `spawn list` renders every subagent on the box. If it
        ever started filtering on the session key, the gate would be pure cost -- so
        pin the reason, not just the reaction.
        """
        agents = [
            SimpleNamespace(id="a1", started=0.0, task="mine"),
            SimpleNamespace(id="a2", started=0.0, task="somebody elses"),
        ]
        manager = SimpleNamespace(running=agents, max_concurrent=4)
        out = commands.spawn_task_reply("list", manager, "telegram:kirocrew:direct:7")
        assert out is not None
        assert "mine" in out and "somebody elses" in out


# ── the manual-/compact capability gate (#8156) ───────────────────────────────


class TestCompactUnsupportedBackend:
    """The channel half of the dashboard's manual-/compact capability gate."""

    def test_a_named_unsupported_backend_is_returned(self) -> None:
        provider = SimpleNamespace(manual_compact_unsupported_backend="kas")
        assert commands.compact_unsupported_backend(provider) == "kas"

    def test_an_absent_property_reads_as_supported(self) -> None:
        assert commands.compact_unsupported_backend(SimpleNamespace()) is None

    def test_a_none_value_reads_as_supported(self) -> None:
        provider = SimpleNamespace(manual_compact_unsupported_backend=None)
        assert commands.compact_unsupported_backend(provider) is None

    def test_an_empty_string_reads_as_supported(self) -> None:
        provider = SimpleNamespace(manual_compact_unsupported_backend="")
        assert commands.compact_unsupported_backend(provider) is None

    def test_a_mocked_truthy_non_str_never_reads_as_a_refusal(self) -> None:
        # A MagicMock answers every attribute with a truthy mock; the ABC's
        # contract is a non-empty str, so anything else must pass through.
        assert commands.compact_unsupported_backend(MagicMock()) is None

    def test_the_reply_names_the_backend_and_reads_as_information(self) -> None:
        reply = commands.compact_unsupported_reply("kas")
        assert "kas" in reply
        assert "automatically" in reply
        # Informational, never an error.
        assert "❌" not in reply and "⚠️" not in reply

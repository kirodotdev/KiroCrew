"""The Crew Members roster previews SPEECH only.

A member's chat draws only what the member says (crew-mode.md, "A crewmate's
chat"), so the roster row beside it must quote the same thing. A patroller
whose newest rows are an auto-nudge turn, a shell call and a say-nothing reply
would otherwise sit under a row quoting `gh issue list …` while its chat says
it has not spoken yet -- the blind reader concluded the conversation was lost.

Two paths produce that preview and both are pinned here: the cold roster read
(`ConversationLog.last_speech_info`) and the live
`member/message` projection, which must keep the last thing SAID when a
machinery row bumps recency.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew import members
from kiro_crew.config.sections import KiroCrewAgentConfig
from kiro_crew.dashboard.system_notices import is_speech_row
from kiro_crew.eventlog import types
from kiro_crew.eventlog.members_projections import RosterProjection
from kiro_crew.eventlog.service import get_service, set_service
from kiro_crew.eventlog.types import Event
from kiro_crew.history import ConversationLog

KEY = "dashboard:member-radar"


def _patrol_log(tmp_path: Path) -> ConversationLog:
    log = ConversationLog(base_dir=tmp_path)
    log.append(KEY, "user", "Anything from the weekend queue?")
    log.append(KEY, "assistant", "Triaged 7 new issues; 2 need your call.")
    log.append(KEY, "nudge", "[auto-nudge cycle 41]\nPatrol the issue queue.")
    log.append(KEY, "tool", "🔧 gh issue list --label needs-triage")
    log.append(KEY, "tool", "✅ done")
    log.append(KEY, "assistant", "\u200b")  # the say-nothing reply
    return ConversationLog(base_dir=tmp_path)  # fresh: no warm cache


class TestRosterPreviewIsSpeechOnly:
    def test_speech_only_skips_machinery_and_the_empty_reply(self, tmp_path: Path) -> None:
        log = _patrol_log(tmp_path)
        preview, _, stopped = log.last_speech_info(KEY)[:3]
        assert preview == "Triaged 7 new issues; 2 need your call."
        assert stopped is False

    def test_default_read_is_unchanged(self, tmp_path: Path) -> None:
        """The Sessions sidebar keeps its preview rule: newest row with text."""
        log = _patrol_log(tmp_path)
        preview, _, _ = log.last_message_info(KEY)
        assert preview.startswith("✅ done")

    def test_recency_still_reads_the_newest_row(self, tmp_path: Path) -> None:
        """A patrol IS activity: the roster orders by the newest row's epoch."""
        log = _patrol_log(tmp_path)
        _, speech_epoch, _ = log.last_speech_info(KEY)[:3]
        _, default_epoch, _ = log.last_message_info(KEY)
        # The speech-only walk records the newest row's epoch on the very first
        # skipped row (the say-nothing reply); the default walk only reaches the
        # "✅ done" row, one row older. Newer or equal, never older.
        assert speech_epoch >= default_epoch

    def test_assistant_role_status_rows_are_not_speech(self, tmp_path: Path) -> None:
        """A compaction notice and a workflow envelope ride the assistant role
        but are status; the roster must not quote them over the last real reply."""
        log = ConversationLog(base_dir=tmp_path)
        log.append(KEY, "assistant", "Triaged 7 new issues.")
        # `append` has no meta kwarg; the gateway's flush writes these rows with
        # `meta` inline, so write them the way the file holds them.
        with open(log._path(KEY), "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "role": "assistant",
                        "content": "Context summary: …",
                        "ts": "2026-09-22T06:00:00+00:00",
                        "meta": {"kind": "compaction"},
                    }
                )
                + "\n"
            )
            fh.write(
                json.dumps(
                    {
                        "role": "assistant",
                        "content": "[Workflow completion event]\nWorkflow `x` (wf_1) → **finished**",
                        "ts": "2026-09-22T06:00:01+00:00",
                    }
                )
                + "\n"
            )
        fresh = ConversationLog(base_dir=tmp_path)
        preview, _, _ = fresh.last_speech_info(KEY)[:3]
        assert preview == "Triaged 7 new issues."

    def test_all_machinery_previews_empty(self, tmp_path: Path) -> None:
        log = ConversationLog(base_dir=tmp_path)
        log.append(KEY, "nudge", "[auto-nudge cycle 1]\nPatrol.")
        log.append(KEY, "tool", "🔧 gh issue list")
        log.append(KEY, "assistant", "\u200b")
        fresh = ConversationLog(base_dir=tmp_path)
        preview, epoch, _ = fresh.last_speech_info(KEY)[:3]
        assert preview == ""
        assert epoch > 0


FIXTURE = Path(__file__).parent / "fixtures" / "crewmate_speech_rows.json"


class TestSpeechTwinsAgree:
    """The backend half of the shared pin: one verdict per fixture row.

    ``website/src/components/chat/crewmateBubbles.test.ts`` reads the same file
    and asserts the frontend twin (``isCrewmateSpeech``) reaches the same
    ``speech`` verdict, so a status kind learned on one side fails the other.
    """

    def test_every_row_matches_the_fixture_verdict(self) -> None:
        cases = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
        assert len(cases) >= 10
        for case in cases:
            if case.get("frontend_only"):
                continue
            got = is_speech_row(case["role"], case["content"], case.get("meta"))
            assert got is case["speech"], case["name"]


def _message_event(data: dict) -> Event:
    return {"type": types.MEMBER_MESSAGE, "seq": 1, "time": 0, "data": data}


class TestLiveProjectionKeepsTheLastThingSaid:
    def test_event_without_preview_bumps_recency_only(self) -> None:
        proj = RosterProjection()
        state = {"last_message": "Triaged 7 new issues.", "last_active_ts": 100.0}
        # A machinery row's event: recency only, no preview key.
        out = proj.apply(state, _message_event({"ts": 200.0}))
        assert out["last_active_ts"] == 200.0
        assert out["last_message"] == "Triaged 7 new issues."

    def test_event_with_preview_replaces_it(self) -> None:
        proj = RosterProjection()
        state = {"last_message": "old", "last_active_ts": 100.0}
        out = proj.apply(state, _message_event({"ts": 200.0, "preview": "new words"}))
        assert out["last_message"] == "new words"
        assert out["last_active_ts"] == 200.0


# ---------------------------------------------------------------------------
# Legacy machinery previews are corrected on the roster read
# ---------------------------------------------------------------------------
CREW = "radar"


@pytest.fixture(autouse=True)
def _fresh_eventlog(tmp_path, monkeypatch):
    monkeypatch.setattr(members, "data_home", lambda: tmp_path)
    set_service(None)
    yield
    set_service(None)


def _members_app(state) -> web.Application:
    from kiro_crew.dashboard.handlers.members import api_members

    @web.middleware
    async def _auth(request, handler):
        request["app"] = ""
        request["user"] = "local-app"
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = state
    app.router.add_get("/api/members", api_members)
    return app


class TestLegacyMachineryPreviewIsCorrectedOnRead:
    """A `member/message` event written BEFORE the preview became speech-only
    (or by any writer that skipped `is_speech_row`) folds a tool line into the
    roster's `last_message`. The roster read reconciles the fold against the
    transcript's speech-only answer -- including an explicit EMPTY correction
    for a member that has never spoken -- and appends nothing on a second read.
    """

    @pytest.mark.asyncio
    async def test_never_spoken_patroller_is_corrected_to_blank(self, tmp_path, monkeypatch):
        from types import SimpleNamespace

        cfg = SimpleNamespace(
            agents={CREW: KiroCrewAgentConfig(kiro_agent="kirocrew")},
            default_agent="kirocrew",
            memory_stores={},
            degraded_sections=frozenset(),
        )
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        state = _make_state(tmp_path)
        slug = members.slug_for_name(CREW)
        members.write_dm_binding(slug, member=CREW, slot_key=f"member-{slug}")
        key = members.member_thread_session_alias(slug)
        # The thread: machinery only, never a word to the user.
        state.conversation_log.append(key, "nudge", "[auto-nudge cycle 1]\nPatrol.")
        state.conversation_log.append(key, "tool", "\U0001f527 gh issue list --label needs-triage")
        state.conversation_log.append(key, "assistant", "\u200b")
        # The legacy fold: a pre-speech-only writer stamped the tool line.
        svc = get_service()
        svc.ensure(slug, CREW)
        svc.append(
            slug,
            types.MEMBER_MESSAGE,
            {"ts": 1.0, "preview": "\U0001f527 gh issue list --label needs-triage"},
        )
        assert svc.snapshot(slug)["values"]["roster"]["last_message"].startswith("\U0001f527")

        app = _members_app(state)
        async with TestClient(TestServer(app)) as client:
            data = await (await client.get("/api/members")).json()
        row = next(r for r in data["members"] if r["name"] == CREW)
        assert row["last_message"] == ""
        assert row["projections"]["values"]["roster"]["last_message"] == ""

        seq = svc.last_seq(slug)
        async with TestClient(TestServer(app)) as client:
            await client.get("/api/members")
        assert svc.last_seq(slug) == seq, "preview reconcile is not idempotent"

    @pytest.mark.asyncio
    async def test_stale_preview_is_corrected_to_the_last_speech(self, tmp_path, monkeypatch):
        from types import SimpleNamespace

        cfg = SimpleNamespace(
            agents={CREW: KiroCrewAgentConfig(kiro_agent="kirocrew")},
            default_agent="kirocrew",
            memory_stores={},
            degraded_sections=frozenset(),
        )
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        state = _make_state(tmp_path)
        slug = members.slug_for_name(CREW)
        members.write_dm_binding(slug, member=CREW, slot_key=f"member-{slug}")
        key = members.member_thread_session_alias(slug)
        state.conversation_log.append(key, "assistant", "Triaged 7 new issues.")
        state.conversation_log.append(key, "tool", "\U0001f527 gh issue list")
        svc = get_service()
        svc.ensure(slug, CREW)
        svc.append(slug, types.MEMBER_MESSAGE, {"ts": 1.0, "preview": "\U0001f527 gh issue list"})

        app = _members_app(state)
        async with TestClient(TestServer(app)) as client:
            data = await (await client.get("/api/members")).json()
        row = next(r for r in data["members"] if r["name"] == CREW)
        assert row["last_message"] == "Triaged 7 new issues."
        assert row["projections"]["values"]["roster"]["last_message"] == "Triaged 7 new issues."

    @pytest.mark.asyncio
    async def test_a_message_that_lands_during_the_read_is_not_overwritten(
        self, tmp_path, monkeypatch
    ):
        """The race the correction must lose on purpose: the roster is observed,
        the transcript is read (speech A), and BEFORE the correction is written
        the member speaks again (B: transcript row + live `member/message`). The
        fold now says B; a correction back to A would durably regress the quote
        and the recency in an append-only log. It is refused, and the next read
        -- which sees B in the transcript -- appends nothing."""
        from types import SimpleNamespace

        cfg = SimpleNamespace(
            agents={CREW: KiroCrewAgentConfig(kiro_agent="kirocrew")},
            default_agent="kirocrew",
            memory_stores={},
            degraded_sections=frozenset(),
        )
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        state = _make_state(tmp_path)
        slug = members.slug_for_name(CREW)
        members.write_dm_binding(slug, member=CREW, slot_key=f"member-{slug}")
        key = members.member_thread_session_alias(slug)
        state.conversation_log.append(key, "assistant", "Speech A.")
        svc = get_service()
        svc.ensure(slug, CREW)
        # Legacy fold: a machinery line, so the read WANTS to correct it.
        svc.append(slug, types.MEMBER_MESSAGE, {"ts": 1.0, "preview": "\U0001f527 gh api ..."})

        original = state.conversation_log.last_speech_info

        def _read_then_speak(*args, **kwargs):
            result = original(*args, **kwargs)  # sees A
            # The member speaks while the correction is still being decided:
            # the row lands in the transcript AND the live path folds it.
            state.conversation_log.append(key, "assistant", "Speech B.")
            svc.append(slug, types.MEMBER_MESSAGE, {"ts": 2.0, "preview": "Speech B."})
            return result

        monkeypatch.setattr(state.conversation_log, "last_speech_info", _read_then_speak)

        app = _members_app(state)
        async with TestClient(TestServer(app)) as client:
            await client.get("/api/members")
        roster = svc.snapshot(slug)["values"]["roster"]
        assert roster["last_message"] == "Speech B.", "stale correction overwrote live speech"
        assert roster["last_active_ts"] == 2.0

        monkeypatch.setattr(state.conversation_log, "last_speech_info", original)
        seq = svc.last_seq(slug)
        async with TestClient(TestServer(app)) as client:
            data = await (await client.get("/api/members")).json()
        row = next(r for r in data["members"] if r["name"] == CREW)
        assert row["last_message"] == "Speech B."
        assert svc.last_seq(slug) == seq, "the read that agrees appended anyway"


class TestReconcileIsCompareAndAppend:
    """`reconcile_member_preview` writes through `append_closer_if_still_applies`
    with `_preview_is_still_at`: the two roster fields it rewrites (`last_message`,
    `last_active_ts`) must still read as they did when the correction was
    decided, re-checked under the per-slug write lock."""

    def test_predicate_compares_only_the_two_fields_it_rewrites(self) -> None:
        from kiro_crew.eventlog_hooks import _preview_is_still_at

        then = {types.PROJ_ROSTER: {"last_message": "a", "last_active_ts": 1.0, "status": "idle"}}
        same = {types.PROJ_ROSTER: {"last_message": "a", "last_active_ts": 1.0, "status": "busy"}}
        assert _preview_is_still_at(same, then), "an unrelated field must not starve it"
        assert not _preview_is_still_at(
            {types.PROJ_ROSTER: {"last_message": "b", "last_active_ts": 1.0}}, then
        )
        assert not _preview_is_still_at(
            {types.PROJ_ROSTER: {"last_message": "a", "last_active_ts": 2.0}}, then
        )
        # A member with no log yet observes an empty block and appends into one.
        assert _preview_is_still_at({}, {})
        assert _preview_is_still_at({types.PROJ_ROSTER: {"name": "x"}}, {})

    def test_refused_when_the_roster_moved_and_applied_when_it_did_not(self) -> None:
        from kiro_crew.eventlog_hooks import reconcile_member_preview

        slug = members.slug_for_name(CREW)
        svc = get_service()
        svc.ensure(slug, CREW)
        svc.append(slug, types.MEMBER_MESSAGE, {"ts": 1.0, "preview": "\U0001f527 tool"})
        observed = dict(svc.snapshot(slug)["values"]["roster"])
        # Live speech after the observation.
        svc.append(slug, types.MEMBER_MESSAGE, {"ts": 2.0, "preview": "Hello."})
        seq = svc.last_seq(slug)
        assert reconcile_member_preview(slug, CREW, "Older speech.", 1.5, observed) is False
        assert svc.last_seq(slug) == seq
        roster = svc.snapshot(slug)["values"]["roster"]
        assert (roster["last_message"], roster["last_active_ts"]) == ("Hello.", 2.0)
        # Unchanged since the observation: the correction lands.
        observed = dict(roster)
        assert reconcile_member_preview(slug, CREW, "Newer speech.", 3.0, observed) is True
        roster = svc.snapshot(slug)["values"]["roster"]
        assert (roster["last_message"], roster["last_active_ts"]) == ("Newer speech.", 3.0)


def _redaction_chain(text: str) -> str:
    from kiro_crew import history as _h

    text, _ = _h.redact_exfiltration_urls(text)
    text, _ = _h.redact_credentials(text)
    return text


class TestLivePreviewEqualsTheRosterRead:
    """The live `member/message` payload (state.py, via `member_message_payload`)
    and the cold roster read (`last_speech_info`) build the
    preview through ONE function, `speech_preview`; a formatted or long message
    must fold to exactly the string the read returns, or the read would append a
    correction after every such message."""

    @pytest.mark.parametrize(
        "content",
        [
            "**Bold** and `code` with a [link](https://example.com) — done.",
            "## Heading\n\n- one\n- two\n\n> quoted",
            "a long reply " * 40,
            # The AWS-documented example key, as test_members_dm_thread.py spells it.
            "Token AKIAIOSFODNN7EXAMPLE in the middle of " + "padding " * 30,
        ],
    )
    def test_formatted_message_folds_to_the_read(self, tmp_path: Path, content) -> None:
        from kiro_crew.eventlog_hooks import member_message_payload

        log = ConversationLog(tmp_path / "hist")
        log.append("k", "assistant", content)
        read, _ts, _stopped, _exhaustive = log.last_speech_info("k", sanitize=_redaction_chain)
        payload = member_message_payload("assistant", content, None, 5.0, sanitize=_redaction_chain)
        assert payload == {"ts": 5.0, "preview": read}
        assert read and "AKIA" not in read

    def test_machinery_and_say_nothing_rows_carry_no_preview(self) -> None:
        from kiro_crew.eventlog_hooks import member_message_payload

        for role, content, meta in [
            ("tool", "\U0001f527 gh api ...", None),
            ("nudge", "[auto-nudge cycle 3]", None),
            ("assistant", "\u200b", None),
            ("assistant", "Context compacted.", {"kind": "compaction"}),
        ]:
            assert member_message_payload(role, content, meta, 7.0, sanitize=_redaction_chain) == {
                "ts": 7.0
            }, (role, content)


def _write_rows(log: ConversationLog, key: str, rows: list[dict]) -> None:
    """Write transcript rows as the file holds them (structured content, big tails)."""
    path = log._path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"_type": "metadata"}) + "\n")
        for r in rows:
            fh.write(json.dumps(r) + "\n")


class TestStructuredLegacySpeechIsSpeech:
    """A legacy row whose `content` is a list of text blocks is what the slot
    detail renders as text; the speech gate must judge that text, not the raw
    list, or the roster read would call it machinery and blank the quote."""

    def test_cold_read_and_live_payload_quote_the_text_blocks(self, tmp_path: Path) -> None:
        from kiro_crew.eventlog_hooks import member_message_payload

        log = ConversationLog(tmp_path / "hist")
        content = [
            {"type": "text", "text": "Structured **markdown** part"},
            {"type": "text", "text": "two"},
        ]
        _write_rows(
            log, "k", [{"role": "assistant", "content": content, "ts": "2026-09-22T06:00:00Z"}]
        )
        read, _ts, _stopped, exhaustive = log.last_speech_info("k", sanitize=_redaction_chain)
        assert read == "Structured markdown part two" and exhaustive
        payload = member_message_payload("assistant", content, None, 5.0, sanitize=_redaction_chain)
        assert payload == {"ts": 5.0, "preview": read}

    def test_structured_content_with_no_text_is_not_speech(self) -> None:
        from kiro_crew.eventlog_hooks import member_message_payload

        assert member_message_payload(
            "assistant", [{"type": "image", "url": "x"}], None, 5.0, sanitize=_redaction_chain
        ) == {"ts": 5.0}


class TestAnEmptyReadThatRanOutOfWindowIsNotAuthority:
    """A patroller can write more machinery than the widest tail window holds
    since it last spoke. The speech-only read then returns "" without reaching
    the log's start; `exhaustive` is False and the roster read must NOT write
    that "" into the append-only member log as the correction -- the quote the
    transcript still holds would be erased for good."""

    def _big_machinery_tail(self, log: ConversationLog, key: str) -> None:
        rows = [
            {"role": "assistant", "content": "Triaged 7 new issues.", "ts": "2026-09-22T06:00:00Z"}
        ]
        filler = "\U0001f527 gh api repos/x/y/issues --paginate " + "x" * 2000
        widest = log._PREVIEW_TAIL_BYTES * 16
        n = widest // 1500 + 8
        for i in range(n):
            rows.append(
                {"role": "tool", "content": filler, "ts": f"2026-09-22T07:{i % 60:02d}:00Z"}
            )
        _write_rows(log, key, rows)
        assert log._path(key).stat().st_size > widest

    def test_read_reports_not_exhaustive(self, tmp_path: Path) -> None:
        log = ConversationLog(tmp_path / "hist")
        self._big_machinery_tail(log, "k")
        preview, epoch, _stopped, exhaustive = log.last_speech_info("k")
        assert preview == "" and not exhaustive and epoch > 0
        # The same walk on a small file that truly has no speech IS exhaustive.
        _write_rows(
            log, "q", [{"role": "tool", "content": "\U0001f527 x", "ts": "2026-09-22T06:00:00Z"}]
        )
        assert log.last_speech_info("q")[3] is True

    @pytest.mark.asyncio
    async def test_roster_read_keeps_the_folded_quote(self, tmp_path, monkeypatch):
        from types import SimpleNamespace

        cfg = SimpleNamespace(
            agents={CREW: KiroCrewAgentConfig(kiro_agent="kirocrew")},
            default_agent="kirocrew",
            memory_stores={},
            degraded_sections=frozenset(),
        )
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        state = _make_state(tmp_path)
        slug = members.slug_for_name(CREW)
        members.write_dm_binding(slug, member=CREW, slot_key=f"member-{slug}")
        key = members.member_thread_session_alias(slug)
        self._big_machinery_tail(state.conversation_log, key)
        svc = get_service()
        svc.ensure(slug, CREW)
        # The live path folded the real quote when it was spoken.
        svc.append(slug, types.MEMBER_MESSAGE, {"ts": 1.0, "preview": "Triaged 7 new issues."})

        app = _members_app(state)
        async with TestClient(TestServer(app)) as client:
            data = await (await client.get("/api/members")).json()
        row = next(r for r in data["members"] if r["name"] == CREW)
        # No preview correction: the fold still quotes the speech the read could
        # not reach (the first read may append the config reconcile's own event,
        # which touches no message field).
        assert row["projections"]["values"]["roster"]["last_message"] == "Triaged 7 new issues."
        assert not any(
            e["type"] == types.MEMBER_MESSAGE and e["data"].get("preview") == ""
            for e in svc.history(slug)
        )
        seq = svc.last_seq(slug)
        async with TestClient(TestServer(app)) as client:
            await client.get("/api/members")
        assert svc.last_seq(slug) == seq
        assert svc.snapshot(slug)["values"]["roster"]["last_message"] == "Triaged 7 new issues."


class TestEnvelopeClassifiersFollowTheRealWriters:
    """The fixture rows are hand-written; these rows come from the writers
    themselves, so a header reword in `workflow_inject._summarize` (or the
    sub-agent gateway's meta stamp) fails here instead of silently turning the
    envelope back into speech."""

    @pytest.mark.parametrize("status", ["finished", "failed", "cancelled"])
    def test_workflow_completion_envelope_as_written_is_not_speech(self, status: str) -> None:
        from kiro_crew.dashboard.workflow_inject import _summarize

        snapshot = {
            "name": "deep-research",
            "run_id": "wf_20260922_abc123",
            "status": status,
            "result": {"summary": "three findings", "artifact": "/tmp/out/report.md"},
            "error": "boom" if status == "failed" else None,
        }
        text = _summarize(snapshot)
        assert text.startswith("[Workflow completion event]")
        assert is_speech_row("assistant", text, None) is False
        # …while the same words with a broken header are drawn and quoted.
        assert is_speech_row("assistant", text.replace("Workflow `", "Workflow ", 1), None) is True
        # …and the intact envelope PASTED by the user is the user speaking: the
        # chat draws a user row whatever it says, so the roster must quote it.
        assert is_speech_row("user", text, None) is True

    def test_subagent_completion_meta_stamp_as_written_is_not_speech(self) -> None:
        # The gateway stamps SUBAGENT_COMPLETION_META_KEY with these fields
        # (slack/gateway.py, "Structured header facts for the dashboard card");
        # the classifier keys on the stamp before any header regex.
        from kiro_crew.constants import SUBAGENT_COMPLETION_META_KEY, SUBAGENT_COMPLETION_PREFIX

        meta = {SUBAGENT_COMPLETION_META_KEY: {"kind": "single", "agentId": "w-1", "outcome": "ok"}}
        text = f"{SUBAGENT_COMPLETION_PREFIX}\nreworded header line\n\nDone."
        assert is_speech_row("assistant", text, meta) is False
        assert is_speech_row("user", text, meta) is False
        # No stamp and no parsable header: drawn, so quoted.
        assert is_speech_row("assistant", text, None) is True


class TestAnUnflushedSlotIsNotCorrectedFromDisk:
    """Live speech reaches the member log at in-memory append time (the emit in
    state.py) while the transcript copy lands at slot flush. A roster read in
    between sees the NEW quote on the roster and the OLD speech on disk; with the
    roster observed before the read, the compare-and-append would accept the
    stale correction. `_slot_has_unflushed_rows` refuses it."""

    def test_predicate_mirrors_the_slot_window_gates(self) -> None:
        from types import SimpleNamespace

        from kiro_crew.dashboard.handlers.members import _slot_has_unflushed_rows

        clean = SimpleNamespace(
            messages=[1, 2], _disk_window_len=2, _pending_rewrite=False, _dirty_flag=False
        )
        assert _slot_has_unflushed_rows(clean) is False
        assert _slot_has_unflushed_rows(None) is False
        assert (
            _slot_has_unflushed_rows(SimpleNamespace(messages=[1, 2, 3], _disk_window_len=2))
            is True
        )
        assert (
            _slot_has_unflushed_rows(
                SimpleNamespace(messages=[1], _disk_window_len=1, _pending_rewrite=True)
            )
            is True
        )
        assert (
            _slot_has_unflushed_rows(
                SimpleNamespace(messages=[1], _disk_window_len=1, _dirty_flag=True)
            )
            is True
        )

    @pytest.mark.asyncio
    async def test_roster_read_skips_the_correction_while_rows_are_unflushed(
        self, tmp_path, monkeypatch
    ):
        from types import SimpleNamespace

        cfg = SimpleNamespace(
            agents={CREW: KiroCrewAgentConfig(kiro_agent="kirocrew")},
            default_agent="kirocrew",
            memory_stores={},
            degraded_sections=frozenset(),
        )
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        state = _make_state(tmp_path)
        slug = members.slug_for_name(CREW)
        slot_key = f"member-{slug}"
        members.write_dm_binding(slug, member=CREW, slot_key=slot_key)
        key = members.member_thread_session_alias(slug)
        # Disk holds the OLD speech only.
        state.conversation_log.append(key, "assistant", "Older speech.")
        # The live slot holds one more row than the last flush persisted: the
        # NEW speech, whose member/message event has already been emitted.
        from kiro_crew.members import DM_SLOT_MODE

        slot = state.get_or_create_slot(slot_key, agent=CREW, mode=DM_SLOT_MODE)
        slot.append("assistant", "Newer speech.", "msg msg-a")
        slot._disk_window_len = 0
        assert len(slot.messages) > slot._disk_window_len
        svc = get_service()
        svc.ensure(slug, CREW)
        svc.append(slug, types.MEMBER_MESSAGE, {"ts": 2.0, "preview": "Newer speech."})

        # `slot.append` above also fires the REAL live emit (queued, best-effort),
        # which lands whenever the executor runs it -- before or after the reads
        # below, platform-dependent. Both orders are fine; the property under
        # test is that no read ever appends the stale disk quote.
        def _stale_corrections() -> list[dict]:
            return [
                e
                for e in svc.history(slug, limit=None)
                if e["type"] == types.MEMBER_MESSAGE and e["data"].get("preview") == "Older speech."
            ]

        app = _members_app(state)
        async with TestClient(TestServer(app)) as client:
            await client.get("/api/members")
        roster = svc.snapshot(slug)["values"]["roster"]
        assert (
            roster["last_message"] == "Newer speech."
        ), "the stale disk quote was appended over live speech"
        assert _stale_corrections() == []
        # Once the flush has caught up, the read agrees and still writes no correction.
        state.conversation_log.append(key, "assistant", "Newer speech.")
        slot._disk_window_len = len(slot.messages)
        async with TestClient(TestServer(app)) as client:
            data = await (await client.get("/api/members")).json()
        row = next(r for r in data["members"] if r["name"] == CREW)
        assert row["last_message"] == "Newer speech."
        assert svc.snapshot(slug)["values"]["roster"]["last_message"] == "Newer speech."
        assert _stale_corrections() == []

    @pytest.mark.asyncio
    async def test_a_reply_that_lands_during_the_read_window_is_not_corrected_from_disk(
        self, tmp_path, monkeypatch
    ):
        """The slot is CLEAN at the pre-await sample; the reply lands while the
        roster is observed / the disk is read. The re-check after the read (state
        and generation) must refuse the stale disk quote."""
        from types import SimpleNamespace

        from kiro_crew.members import DM_SLOT_MODE

        cfg = SimpleNamespace(
            agents={CREW: KiroCrewAgentConfig(kiro_agent="kirocrew")},
            default_agent="kirocrew",
            memory_stores={},
            degraded_sections=frozenset(),
        )
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        state = _make_state(tmp_path)
        slug = members.slug_for_name(CREW)
        slot_key = f"member-{slug}"
        members.write_dm_binding(slug, member=CREW, slot_key=slot_key)
        key = members.member_thread_session_alias(slug)
        state.conversation_log.append(key, "assistant", "Older speech.")
        slot = state.get_or_create_slot(slot_key, agent=CREW, mode=DM_SLOT_MODE)
        slot._disk_window_len = len(slot.messages)  # clean at the sample
        svc = get_service()
        svc.ensure(slug, CREW)
        svc.append(slug, types.MEMBER_MESSAGE, {"ts": 1.0, "preview": "Older speech."})

        original = state.conversation_log.last_speech_info

        def _read_while_the_member_speaks(*args, **kwargs):
            result = original(*args, **kwargs)  # disk: Older speech.
            slot.append("assistant", "Newer speech.", "msg msg-a")  # unflushed now
            svc.append(slug, types.MEMBER_MESSAGE, {"ts": 2.0, "preview": "Newer speech."})
            return result

        monkeypatch.setattr(
            state.conversation_log, "last_speech_info", _read_while_the_member_speaks
        )

        app = _members_app(state)
        async with TestClient(TestServer(app)) as client:
            await client.get("/api/members")
        assert svc.snapshot(slug)["values"]["roster"]["last_message"] == "Newer speech."
        assert not any(
            e["type"] == types.MEMBER_MESSAGE
            and e["data"].get("preview") == "Older speech."
            and e["data"].get("ts") != 1.0
            for e in svc.history(slug, limit=None)
        ), "the stale disk quote was appended over speech that landed mid-read"

    def test_generation_moves_on_append_flush_and_edit(self) -> None:
        from types import SimpleNamespace

        from kiro_crew.dashboard.handlers.members import _slot_flush_generation

        assert _slot_flush_generation(None) is None
        base = SimpleNamespace(messages=[1, 2], _disk_window_len=2, _dirty_gen=0)
        g0 = _slot_flush_generation(base)
        assert g0 == (2, 2, 0)
        assert (
            _slot_flush_generation(
                SimpleNamespace(messages=[1, 2, 3], _disk_window_len=2, _dirty_gen=0)
            )
            != g0
        )
        assert (
            _slot_flush_generation(
                SimpleNamespace(messages=[1, 2], _disk_window_len=1, _dirty_gen=0)
            )
            != g0
        )
        assert (
            _slot_flush_generation(
                SimpleNamespace(messages=[1, 2], _disk_window_len=2, _dirty_gen=1)
            )
            != g0
        )

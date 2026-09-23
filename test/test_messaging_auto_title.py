"""Contract tests for ``messaging/auto_title.py``.

Auto-titling was Slack-only, and dead on Slack's own default path:
``_maybe_auto_title_slack`` was called from the native loop and nowhere else,
while ``messaging.use_transport`` defaults True — so a default install titled
nothing and every surface fell back to a deterministic truncation.

These tests pin the hoisted core: the claim that makes a session titled exactly
once even with two channels racing, the two guards that stop a generated name
from replacing a name a person chose, and the tool-free turn.

Every test is written so that reverting the guard it names turns it red — see the
per-test notes on what to break.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_PERMISSION_REQUEST, EVENT_TEXT_CHUNK
from kiro_crew.messaging import auto_title

_KEY = "telegram:kirocrew:direct:4242"


def _ev(kind: str, **kw):
    return SimpleNamespace(kind=kind, text=kw.get("text", ""), request_id=kw.get("request_id"))


class _Provider:
    """Yields a scripted event list, recording every tool it was refused."""

    def __init__(self, events=None, raises: BaseException | None = None, delay: float = 0.0):
        self._events = events or []
        self._raises = raises
        self._delay = delay
        self.rejected: list = []
        self.prompts: list[str] = []

    async def stream(self, message, timeout=120.0):
        self.prompts.append(message)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raises is not None:
            raise self._raises
        for event in self._events:
            yield event

    @staticmethod
    def slow(title: str, delay: float) -> "_Provider":
        """A provider that WOULD produce a usable title, but only eventually.

        The delay has to sit ahead of a real title: a slow provider that ends up
        yielding nothing is indistinguishable from a SKIP, so a test built on one
        passes with the timeout deleted.
        """
        return _Provider([_ev(EVENT_TEXT_CHUNK, text=title), _ev(EVENT_COMPLETE)], delay=delay)

    async def reject_tool(self, request_id):
        self.rejected.append(request_id)


class _Sessions:
    """Minimal ``SessionManager`` surface ``background_turn`` needs."""

    def __init__(self, provider: _Provider | None = None):
        self._provider = provider or _Provider()
        self.acquired: list[str] = []
        self.released: list[str] = []
        self.recycled = 0

    async def get_or_create(self, key, agent=None, channel_id=None):
        self.acquired.append(key)
        return self._provider, True, False

    def release(self, key):
        self.released.append(key)

    async def recycle_background(self):
        self.recycled += 1


class _Log:
    """``ConversationLog`` stand-in over one in-memory metadata dict.

    Implements the real ``update_metadata_if`` contract: the guard is evaluated
    against the record as it stands at write time, the return value says whether
    the merge was applied, and ``require_existing`` refuses a session with no
    file at all. *exists* stands for that file, which is a separate question from
    what the metadata dict holds -- in the real store an absent session and an
    untitled one both reach the guard as ``{}``.

    *becomes* models the record being REPLACED during the naming turn: the first
    status read (the caller's own, before the turn) sees the original, and
    everything after it sees the replacement. That is what a deletion plus a new
    message on the same thread does, because the session key is derived from the
    thread rather than from the record. An empty *becomes* means the record is
    GONE, which is a different state from a record that is merely stamp-less.

    *becomes_unreadable* models the first line being damaged during the turn: the
    real store answers ``({}, False)`` for that, without raising, and refuses the
    write. An unreadable record is evidence of neither presence nor absence.

    A default record carries a creation stamp, because a real one always does --
    the append path mints ``created_at`` when it creates the session file. "No
    record at all" is therefore spelled ``exists=False``, and a record written in
    an older shape is spelled by passing a dict without a stamp.
    """

    #: Stands for the stamp the append path mints; any fixed instant will do.
    STAMP = "2026-09-20T05:00:00.000000+00:00"

    def __init__(
        self,
        meta: dict | None = None,
        raises: BaseException | None = None,
        *,
        exists: bool = True,
        becomes: dict | None = None,
        becomes_unreadable: bool = False,
    ):
        self.meta = dict(meta) if meta is not None else {"created_at": self.STAMP}
        self.raises = raises
        self.exists = exists
        self.becomes = becomes
        self.becomes_unreadable = becomes_unreadable
        self.readable = True
        self.guarded_calls: list[tuple[str, dict]] = []
        self.required_existing: list[bool] = []
        self.metadata_reads: list[str] = []

    def get_metadata_status(self, key: str) -> tuple[dict, bool]:
        self.metadata_reads.append(key)
        current = (dict(self.meta) if self.exists else {}, self.readable)
        if len(self.metadata_reads) == 1:
            if self.becomes is not None:
                self.meta = dict(self.becomes)
                self.exists = bool(self.becomes)
            if self.becomes_unreadable:
                self.readable = False
        return current

    def get_metadata(self, key: str) -> dict:
        return self.get_metadata_status(key)[0]

    def update_metadata_if(self, key, fields, guard, *, require_existing: bool = False):
        if self.raises is not None:
            raise self.raises
        self.guarded_calls.append((key, dict(fields)))
        self.required_existing.append(require_existing)
        if require_existing and not self.exists:
            return False
        # The real store refuses an unreadable record before it consults the
        # guard, so a damaged first line is a refusal rather than an exception.
        if not self.readable:
            return False
        if not guard(self.meta):
            return False
        self.meta.update(fields)
        return True


def _title_provider(title: str = "Deploy the gateway") -> _Provider:
    return _Provider([_ev(EVENT_TEXT_CHUNK, text=title), _ev(EVENT_COMPLETE)])


async def _call(sessions, log, key, user_text, assistant_text, **kw):
    """Drive the turn the way production does: pin the record, THEN start it.

    Production captures the pin next to ``try_claim``, before the task is
    scheduled, so these tests capture it the same way. That also makes the
    stand-in's FIRST metadata read the pin's read, which is what it is in
    production -- a test that pinned later would be measuring a window the
    shipped callers do not have.
    """
    pin = await auto_title.pin_record(log, key)
    return await auto_title.maybe_auto_title(
        sessions, log, key, user_text, assistant_text, pin=pin, **kw
    )


@pytest.fixture(autouse=True)
def _isolate_claims():
    """The claim tracker is a process global; make every test hermetic."""
    auto_title.reset()
    yield
    auto_title.reset()


@pytest.fixture()
def audits(monkeypatch):
    events: list[dict] = []
    fake = MagicMock()
    fake.log_api_access = lambda **kw: events.append(kw)
    monkeypatch.setattr(auto_title, "sel", lambda: fake)
    return events


# ──────────────────────────────────────────────────────────────────────
# The claim
# ──────────────────────────────────────────────────────────────────────
class TestClaim:
    def test_only_the_first_caller_gets_the_claim(self):
        """Mutation: make ``try_claim`` always return True — red.

        Without check-and-mark in ONE synchronous step, two turns that resolved
        to the same session each fire a naming task and the conversation is
        titled twice (and billed twice).
        """
        assert auto_title.try_claim(_KEY) is True
        assert auto_title.try_claim(_KEY) is False
        assert auto_title.is_titled(_KEY) is True

    def test_releasing_the_claim_allows_a_retry(self):
        auto_title.try_claim(_KEY)
        auto_title.release_claim(_KEY)
        assert auto_title.try_claim(_KEY) is True

    def test_the_lru_evicts_the_least_recently_marked(self, monkeypatch):
        """Mutation: drop the ``popitem`` in ``mark_titled`` — red."""
        monkeypatch.setattr(auto_title, "TITLE_LRU_MAX", 1)
        auto_title.mark_titled("a", auto_title.TITLE_KIND_AUTO)
        auto_title.mark_titled("b", auto_title.TITLE_KIND_MANUAL)
        assert auto_title.is_titled("a") is False
        assert auto_title.titled_kind("b") == auto_title.TITLE_KIND_MANUAL

    @pytest.mark.asyncio
    async def test_two_concurrent_turns_title_the_session_once(self, audits):
        """The claim-early race, driven through the real entry point.

        Both turns arrive together; whoever loses ``try_claim`` must not run a
        naming turn at all. Mutation: replace the ``try_claim`` calls below with
        an unguarded ``mark_titled`` — red, because both would title.
        """
        provider = _title_provider()
        sessions = _Sessions(provider)
        log = _Log()
        applied: list[str] = []

        async def _one_turn() -> None:
            if not auto_title.try_claim(_KEY):
                return
            title = await _call(sessions, log, _KEY, "user", "assistant", source="telegram")
            if title:
                applied.append(title)

        await asyncio.gather(_one_turn(), _one_turn())
        assert applied == ["Deploy the gateway"]
        assert len(provider.prompts) == 1  # one naming turn, so one bill
        assert log.meta["title"] == "Deploy the gateway"


# ──────────────────────────────────────────────────────────────────────
# A person's name always wins
# ──────────────────────────────────────────────────────────────────────
class TestManualTitleWins:
    @pytest.mark.asyncio
    async def test_a_manual_rename_landing_mid_stream_is_not_overwritten(self, audits):
        """The in-process guard.

        Mutation: delete the ``titled_kind(...) == TITLE_KIND_MANUAL`` check —
        red, because the generated name replaces the one the user just typed.
        """
        auto_title.mark_titled(_KEY, auto_title.TITLE_KIND_MANUAL)
        renamed: list[str] = []
        log = _Log()
        title = await _call(
            _Sessions(_title_provider()),
            log,
            _KEY,
            "user",
            "assistant",
            source="telegram",
            set_channel_title=lambda t: _append(renamed, t),
        )
        assert title == ""
        assert renamed == []
        assert log.guarded_calls == []

    @pytest.mark.asyncio
    async def test_a_title_from_before_a_restart_is_not_overwritten(self, audits):
        """The PERSISTED guard, which is the one that survives a restart.

        After a restart the claim tracker is empty, so the in-process guard above
        is blind and the claim is taken again. The record itself still carries the
        name, and ``update_metadata_if``'s guard refuses under the lock.

        Mutation: write with an unguarded ``set_title``/``update_metadata`` (or
        ignore the returned ``applied``) — red, because a manual title made in an
        earlier process is silently replaced, on the transcript AND on the
        channel.
        """
        log = _Log({"title": "Quarterly review"})
        renamed: list[str] = []
        assert auto_title.try_claim(_KEY) is True  # ← the restart: no memory of it
        title = await _call(
            _Sessions(_title_provider()),
            log,
            _KEY,
            "user",
            "assistant",
            source="telegram",
            set_channel_title=lambda t: _append(renamed, t),
        )
        assert title == ""
        assert log.meta["title"] == "Quarterly review"
        assert renamed == []  # the channel keeps the user's name too

    @pytest.mark.asyncio
    async def test_a_deterministic_fallback_record_is_titled(self, audits):
        """The other side of the same guard: no title on the record means the
        surface is still showing its deterministic fallback, so name it."""
        log = _Log({"agent": "kirocrew"})
        renamed: list[str] = []
        title = await _call(
            _Sessions(_title_provider()),
            log,
            _KEY,
            "user",
            "assistant",
            source="telegram",
            set_channel_title=lambda t: _append(renamed, t),
        )
        assert title == "Deploy the gateway"
        assert log.meta["title"] == "Deploy the gateway"
        assert renamed == ["Deploy the gateway"]

    @pytest.mark.asyncio
    async def test_a_blank_title_on_the_record_does_not_block_naming(self, audits):
        log = _Log({"title": "   "})
        title = await _call(_Sessions(_title_provider()), log, _KEY, "u", "a", source="telegram")
        assert title == "Deploy the gateway"


# ──────────────────────────────────────────────────────────────────────
# A conversation deleted during the naming turn stays deleted
# ──────────────────────────────────────────────────────────────────────
class TestDeletedDuringTheTurn:
    """The naming turn is a whole LLM round trip, so a deletion can land inside
    it. The guard alone cannot refuse that: an absent record and an untitled one
    both reach it as an empty dict, and the merge upserts. Nor can existence
    alone, because the session key is derived from the thread and outlives the
    record it named, so the deleted conversation can be replaced under it. And
    the claim, which lives in a process-wide LRU, must not outlive either."""

    @pytest.mark.asyncio
    async def test_a_session_deleted_mid_turn_is_not_recreated_as_a_title(self, audits):
        """Mutation: drop ``require_existing=True`` at the write, AND accept a
        non-present pre-read in the guard -- red only when both are removed.

        Two independent refusals cover this now, which is deliberate: the store
        refuses absence inside the write's own lock, and the guard refuses a state
        it could not pin. The tests that isolate each one are
        ``test_the_write_asks_the_store_to_refuse_absence`` for the request and
        ``test_a_record_appearing_during_the_turn_is_refused`` for the guard.
        """
        log = _Log({}, exists=False)  # deleted while the title was being generated
        renamed: list[str] = []
        title = await _call(
            _Sessions(_title_provider()),
            log,
            _KEY,
            "user",
            "assistant",
            source="telegram",
            set_channel_title=lambda t: _append(renamed, t),
        )
        assert title == ""
        assert log.meta == {}  # nothing was written back
        assert renamed == []  # and the channel is not named either

    @pytest.mark.asyncio
    async def test_the_write_asks_the_store_to_refuse_absence(self, audits):
        """The opt-in reaches the store on the ordinary path too.

        Mutation: drop the keyword, or pass ``require_existing=False`` -- red.
        Asserted separately from the behaviour above because the fake could
        refuse for its own reasons and leave that test green with the real
        request never made.
        """
        log = _Log({"agent": "kirocrew"})
        title = await _call(_Sessions(_title_provider()), log, _KEY, "u", "a", source="telegram")
        assert title == "Deploy the gateway"
        assert log.required_existing == [True]

    @pytest.mark.asyncio
    async def test_a_replacement_under_the_same_key_is_not_given_the_old_title(self, audits):
        """Existence alone is not enough, because the key outlives the record.

        A channel session key is derived from the thread, so deleting the
        conversation and messaging that thread again mints a NEW record under the
        SAME key. The file then exists and carries no title, so a check that asks
        only whether the session is there lets the turn write a name derived from
        the conversation that was deleted.

        Mutation: drop the identity term from the guard (pass
        ``_record_is_untitled``) -- red, because the replacement is untitled.
        """
        log = _Log(
            {"created_at": "2026-09-20T05:00:00.100000+00:00"},
            becomes={"created_at": "2026-09-20T05:00:31.900000+00:00"},
        )
        renamed: list[str] = []
        title = await _call(
            _Sessions(_title_provider()),
            log,
            _KEY,
            "user",
            "assistant",
            source="telegram",
            set_channel_title=lambda t: _append(renamed, t),
        )
        assert title == ""
        assert "title" not in log.meta  # the replacement keeps its own identity
        assert renamed == []

    @pytest.mark.asyncio
    async def test_a_vanished_record_releases_the_claim(self, audits):
        """The claim must not outlive the conversation it was taken for.

        It lives in a process-wide LRU, so a claim held through a deletion
        silences auto-titling for whatever takes the key next until the gateway
        restarts.

        Mutation: remove the ``release_claim`` call in the refusal branch -- red.
        """
        assert auto_title.try_claim(_KEY) is True
        log = _Log(exists=False)
        title = await _call(_Sessions(_title_provider()), log, _KEY, "u", "a", source="telegram")
        assert title == ""
        assert auto_title.is_titled(_KEY) is False  # a later conversation may be named

    @pytest.mark.asyncio
    async def test_a_record_without_a_creation_stamp_is_still_pinned(self, audits):
        """A stamp-less record is not the same state as no record.

        Every path that mints a metadata line stamps it from a clock, so a
        replacement always acquires one. Requiring a stamp-less record to still
        have none therefore pins it as firmly as a stamp pins the ordinary case.

        Mutation: fold the two states together (accept anything when the stamp is
        empty) -- red, because the replacement is untitled and the original had
        no stamp to compare.
        """
        log = _Log(
            {"agent": "kirocrew"},  # written in an older shape: no created_at
            becomes={"agent": "kirocrew", "created_at": "2026-09-20T06:00:12.500000+00:00"},
        )
        renamed: list[str] = []
        title = await _call(
            _Sessions(_title_provider()),
            log,
            _KEY,
            "user",
            "assistant",
            source="telegram",
            set_channel_title=lambda t: _append(renamed, t),
        )
        assert title == ""
        assert "title" not in log.meta
        assert renamed == []

    @pytest.mark.asyncio
    async def test_a_deleted_record_without_a_stamp_still_releases_the_claim(self, audits):
        """The gone case has to be asked separately from the replaced one.

        An absent record carries no stamp either, so a stamp-less record reads as
        an identity MATCH once it is deleted, and a check that only compares
        stamps would keep the claim on a conversation that is gone.

        Mutation: drop the ``not meta`` term from the re-read -- red.
        """
        assert auto_title.try_claim(_KEY) is True
        log = _Log({"agent": "kirocrew"}, becomes={})  # no stamp, then deleted
        title = await _call(_Sessions(_title_provider()), log, _KEY, "u", "a", source="telegram")
        assert title == ""
        assert auto_title.is_titled(_KEY) is False

    @pytest.mark.asyncio
    async def test_an_unreadable_record_keeps_the_claim(self, audits):
        """A read that did not answer is not evidence the record is gone.

        The store answers a damaged first line with an empty dict and a false
        readable flag, without raising, so a check that reads only the dict sees
        the same value a deletion produces. Releasing the claim there bills a
        fresh naming turn on every following exchange for as long as the record
        stays damaged.

        Mutation: read the dict alone (``get_metadata``) instead of the status --
        red, because the empty dict reads as a deletion.
        """
        assert auto_title.try_claim(_KEY) is True
        log = _Log({"created_at": "2026-09-20T06:30:00.250000+00:00"}, becomes_unreadable=True)
        title = await _call(_Sessions(_title_provider()), log, _KEY, "u", "a", source="telegram")
        assert title == ""
        assert auto_title.is_titled(_KEY) is True  # unreadable is not a verdict

    @pytest.mark.asyncio
    async def test_a_record_appearing_during_the_turn_is_refused(self, audits):
        """A record that was not there to pin is refused, not titled.

        The key outlives the record, so a record appearing mid-turn need not be
        the conversation the title was generated from: a deletion landing between
        the claim and the pre-read leaves nothing to pin, and the replacement that
        follows reads as an ordinary untitled record.

        Mutation: accept when the pre-read found no record (return True for a
        non-present state) -- red, because the replacement is untitled.
        """
        log = _Log(exists=False, becomes={"created_at": "2026-09-20T07:05:00.750000+00:00"})
        renamed: list[str] = []
        title = await _call(
            _Sessions(_title_provider()),
            log,
            _KEY,
            "user",
            "assistant",
            source="telegram",
            set_channel_title=lambda t: _append(renamed, t),
        )
        assert title == ""
        assert "title" not in log.meta
        assert renamed == []

    @pytest.mark.asyncio
    async def test_an_unpinnable_record_spends_no_model_turn(self, audits):
        """The refusal is deterministic, so it must come BEFORE the turn.

        ``state != RECORD_PRESENT`` is known at entry and no title the model could
        produce would change the verdict, so streaming one first would spend a
        whole background turn on an answer that is discarded either way. All three
        dispatchers persist the turn before they pin -- ``slack/handler.py`` and
        ``slack/transport_dispatch.py`` always did, and Telegram was reordered to
        match -- so a record that cannot be pinned is one that is genuinely gone,
        not a conversation whose first turn has not been written yet.

        Mutation: move the check back below ``_stream_title`` -- red, the provider
        is prompted.
        """
        provider = _title_provider()
        log = _Log(exists=False, becomes={"created_at": "2026-09-20T07:05:00.750000+00:00"})
        title = await _call(_Sessions(provider), log, _KEY, "user", "assistant", source="telegram")
        assert title == ""
        assert provider.prompts == []  # no turn was spent on a decided refusal
        assert "title" not in log.meta

    @pytest.mark.asyncio
    async def test_a_record_appearing_during_the_turn_releases_the_claim(self, audits):
        """Refusing an unpinnable record is affordable only because of this.

        The conversation is named by the NEXT exchange, once there is a record to
        pin, rather than never -- which is what keeping the claim here would mean,
        since the claim lives in a process-wide LRU.

        Mutation: keep the claim when the pre-read found no record -- red.
        """
        assert auto_title.try_claim(_KEY) is True
        log = _Log(exists=False, becomes={"created_at": "2026-09-20T07:05:00.750000+00:00"})
        title = await _call(_Sessions(_title_provider()), log, _KEY, "u", "a", source="telegram")
        assert title == ""
        assert auto_title.is_titled(_KEY) is False  # the next exchange may name it

    @pytest.mark.asyncio
    async def test_an_unreadable_recheck_does_not_retain_an_unowned_claim(self, audits):
        """A turn that pinned nothing releases the claim even if the recheck is damaged.

        UNKNOWN keeps the claim because the record may still be OURS, and that
        only means something when the pre-read pinned one. Consulting UNKNOWN
        first let a damaged recheck retain a claim nobody owned, which is
        process-wide, so auto-titling went silent for whatever took the key next
        until the gateway restarted -- undoing the release that makes refusing an
        absent record affordable at all.

        Mutation: decide UNKNOWN before the unpinnable case -- red, the claim is
        retained.
        """
        assert auto_title.try_claim(_KEY) is True
        log = _Log(exists=False, becomes_unreadable=True)
        title = await _call(_Sessions(_title_provider()), log, _KEY, "u", "a", source="telegram")
        assert title == ""
        assert auto_title.is_titled(_KEY) is False  # nothing was pinned, so nothing to hold

    def test_every_scheduling_site_pins_before_it_claims(self):
        """The claim must not be held across the pin's own suspension point.

        ``pin_record`` awaits a metadata read on a worker thread, and
        ``release_claim`` is only reachable from inside the scheduled task. Claim
        first and a cancellation delivered during that read (``!stop``) leaves the
        claim held with no task to release it -- and the claim is process-wide, so
        that key cannot be auto-titled again until the gateway restarts.

        Asserted on the source because the ordering lives at three call sites
        inside large streaming tails, and reversing it leaves every behavioural
        test in this repository green: the window needs a cancellation delivered
        inside a thread hand-off, which no test double reproduces. An ordering
        nothing checks is an ordering that comes back.
        """
        sites = {
            "slack/handler.py": "conversation_log, session_key",
            "slack/transport_dispatch.py": "conversation_log, session_key",
            "telegram/transport_dispatch.py": "self.conv_log, session_key",
        }
        package_root = os.path.dirname(os.path.dirname(auto_title.__file__))
        for relative, pin_args in sites.items():
            with open(os.path.join(package_root, relative), encoding="utf-8") as handle:
                body = handle.read()
            code = "\n".join(
                line for line in body.splitlines() if not line.lstrip().startswith("#")
            )
            pin_at = code.find(f"auto_title.pin_record({pin_args})")
            claim_at = code.find("auto_title.try_claim(session_key)")
            assert pin_at != -1, f"{relative}: no pin_record call found"
            assert claim_at != -1, f"{relative}: no try_claim call found"
            assert pin_at < claim_at, f"{relative}: claims before it pins"

    def test_every_permitless_scheduling_site_persists_before_it_pins(self):
        """A pin taken before the turn's own write can only ever read ABSENT.

        The record the pin reads is the one this turn's persist mints, so pinning
        first makes a new conversation's first pin ABSENT, which the guard refuses
        -- costing the first exchange its generated name on every new thread. Both
        dispatch tails persist first, so an unpinnable record there means a record
        that is genuinely gone.

        The Slack native tail is excluded because it holds a per-session permit
        that it releases BEFORE this persist, so a pin placed after the write is
        not exclusive: the sibling test below states the rule that site obeys
        instead, and it keeps the first exchange nameable by re-reading ABSENT.

        Asserted on the source because the ordering lives in large streaming tails
        and no double reproduces it. Each token below is the tail's OWN persist --
        the last occurrence in the file -- so a title block moved back above it
        reads as a pin that precedes the write.
        """
        sites = {
            "slack/transport_dispatch.py": (
                "await save_conversation_turn_off_loop(",
                "conversation_log, session_key",
            ),
            "telegram/transport_dispatch.py": (
                "self._persist_turn,",
                "self.conv_log, session_key",
            ),
        }
        package_root = os.path.dirname(os.path.dirname(auto_title.__file__))
        for relative, (persist_token, pin_args) in sites.items():
            with open(os.path.join(package_root, relative), encoding="utf-8") as handle:
                body = handle.read()
            code = "\n".join(
                line for line in body.splitlines() if not line.lstrip().startswith("#")
            )
            persist_at = code.rfind(persist_token)
            pin_at = code.find(f"auto_title.pin_record({pin_args})")
            assert persist_at != -1, f"{relative}: no persist call found"
            assert pin_at != -1, f"{relative}: no pin_record call found"
            assert persist_at < pin_at, f"{relative}: pins before it persists"

    def test_the_slack_tail_pins_while_it_still_holds_the_permit(self):
        """The Slack tail's pin must precede the permit release, not just the claim.

        That tail releases the per-session permit and then spends several Slack
        round-trips finishing the turn before it reaches the title block. A queued
        turn takes the released permit inside that span, so a delete plus a
        re-message can retire this key's record and mint a replacement under the
        same thread-derived key. A pin read after the release captures the
        REPLACEMENT, the guard matches it, and the title generated from this turn
        names a conversation it never ran in. Only a read taken while the permit is
        held is exclusive.

        Asserted on the source: the release and the title block sit hundreds of
        lines apart in one streaming tail, the window needs a real second turn
        interleaving with released-permit I/O, and no double in this repository
        reproduces that. The ordering is invisible to every behavioural test, which
        is exactly why it needs a structural one.

        The anchor is the verdict step rather than any ``_release_permit()`` text.
        Earlier releases in the file belong to paths that return before the title
        block, and the last one trails it, so neither bounds the window. The
        verdict step is where the permit is released on the path that reaches the
        title block -- and on the deferred-OPTIONS path the release is later still,
        so a pin above the verdict step is held on both.
        """
        package_root = os.path.dirname(os.path.dirname(auto_title.__file__))
        with open(os.path.join(package_root, "slack/handler.py"), encoding="utf-8") as handle:
            body = handle.read()
        code = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))
        pin_at = code.find("auto_title.pin_record(conversation_log, session_key)")
        verdict_at = code.find("_options_verdict_deferred = bool(")
        persist_at = code.rfind("_turn_row_ts = await save_conversation_turn_off_loop(")
        assert pin_at != -1, "no pin_record call found"
        assert verdict_at != -1, "no verdict step found"
        assert persist_at != -1, "no persist call found"
        assert pin_at < verdict_at, "the tail releases the permit before it pins"
        assert pin_at < persist_at, "the permit-held pin must precede the turn's own write"

    def test_the_slack_tail_rereads_only_an_absent_pin(self):
        """Re-reading the pin is confined to the one state that cannot be confused.

        Pinning under the permit means a key whose record has not been minted yet
        reads ABSENT, because that tail persists this turn's row after the release.
        Re-reading ABSENT once the row has landed is what keeps a brand-new
        conversation nameable from its first exchange, and it is safe because a
        record that does not exist has no replacement it could be mistaken for.

        PRESENT must never be re-read: that identity is precisely what a
        replacement would overwrite, so widening this condition reopens the window
        the permit-held pin closes.
        """
        package_root = os.path.dirname(os.path.dirname(auto_title.__file__))
        with open(os.path.join(package_root, "slack/handler.py"), encoding="utf-8") as handle:
            body = handle.read()
        code = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))
        reread_at = code.rfind("auto_title.pin_record(conversation_log, session_key)")
        first_pin_at = code.find("auto_title.pin_record(conversation_log, session_key)")
        assert reread_at > first_pin_at, "the tail has no second, post-write pin read"
        guard = code[max(0, reread_at - 400) : reread_at]
        assert "RECORD_ABSENT" in guard, "the re-read is not confined to an ABSENT pin"

    def test_every_scheduling_site_peeks_the_claim_before_the_pin(self):
        """The pin is a thread hop, so an already-named conversation must skip it.

        ``try_claim`` and ``is_titled`` test the SAME membership, so once a key is
        claimed or titled the pin's value is read and then discarded. Pinning
        before claiming put that read ahead of the cheap test, which would charge
        a metadata read to every later message of every already-named
        conversation, in all three dispatchers. The peek restores the short
        circuit without weakening the pin-before-claim order.
        """
        sites = {
            "slack/handler.py": "conversation_log, session_key",
            "slack/transport_dispatch.py": "conversation_log, session_key",
            "telegram/transport_dispatch.py": "self.conv_log, session_key",
        }
        package_root = os.path.dirname(os.path.dirname(auto_title.__file__))
        for relative, pin_args in sites.items():
            with open(os.path.join(package_root, relative), encoding="utf-8") as handle:
                body = handle.read()
            code = "\n".join(
                line for line in body.splitlines() if not line.lstrip().startswith("#")
            )
            peek_at = code.find("auto_title.is_titled(session_key)")
            pin_at = code.find(f"auto_title.pin_record({pin_args})")
            assert peek_at != -1, f"{relative}: no is_titled peek found"
            assert pin_at != -1, f"{relative}: no pin_record call found"
            assert peek_at < pin_at, f"{relative}: pins before it peeks the claim"

    @pytest.mark.asyncio
    async def test_the_pin_cannot_be_omitted(self, audits):
        """No caller may skip the pin, because skipping it reopens the window.

        The pin is what makes the guard compare against the record the turn was
        authorised for. A default would let a call site added later read the
        record inside the task instead -- one event-loop tick after the claim,
        which is long enough for a delete plus a re-message on the same thread to
        substitute a replacement. Requiring it means that mistake is a TypeError
        at the call site rather than a wrong title in production.

        Mutation: give ``pin`` a default and read the record inside -- red, the
        call succeeds.
        """
        with pytest.raises(TypeError):
            await auto_title.maybe_auto_title(
                _Sessions(_title_provider()), _Log(), _KEY, "u", "a", source="telegram"
            )

    @pytest.mark.asyncio
    async def test_pinning_late_is_what_the_early_capture_prevents(self, audits):
        """The capture's TIMING is the fix, shown as a differential.

        The replacement test above pins BEFORE the replacement lands, the way the
        shipped callers do, and the write is refused. This one pins AFTER it --
        which is what reading the record inside the detached task amounted to, one
        event-loop tick later -- and the very same code writes the deleted
        conversation's title onto the replacement.

        The only difference between the two tests is when the pin was taken, so
        this pair is the mutation: it lives in the test rather than in the source,
        which also means it keeps working if someone reformats the module.
        """
        log = _Log(becomes={"created_at": "2026-09-20T07:05:00.750000+00:00"})
        # Two reads: the first is the stand-in's substitution point, so the SECOND
        # sees the replacement -- which is what a pin taken after the scheduling
        # tick sees.
        await auto_title.pin_record(log, _KEY)
        late = await auto_title.pin_record(log, _KEY)
        renamed: list[str] = []
        title = await auto_title.maybe_auto_title(
            _Sessions(_title_provider()),
            log,
            _KEY,
            "u",
            "a",
            pin=late,
            source="telegram",
            set_channel_title=lambda t: _append(renamed, t),
        )
        # The wrong outcome, on purpose: this is the defect, reproduced.
        assert title == "Deploy the gateway"
        assert log.meta["title"] == "Deploy the gateway"
        assert renamed == ["Deploy the gateway"]

    @pytest.mark.asyncio
    async def test_a_record_that_is_already_named_keeps_the_claim(self, audits):
        """The complement, so the release above is conditional and not blanket.

        A refusal because somebody already named the conversation must KEEP the
        claim: releasing it spends another naming turn on a conversation that
        does not need one.

        Mutation: release the claim unconditionally on refusal -- red.
        """
        assert auto_title.try_claim(_KEY) is True
        log = _Log({"title": "Chosen by hand", "created_at": "2026-09-20T05:00:00.100000+00:00"})
        title = await _call(_Sessions(_title_provider()), log, _KEY, "u", "a", source="telegram")
        assert title == ""
        assert log.meta["title"] == "Chosen by hand"  # untouched
        assert auto_title.is_titled(_KEY) is True

    def _persist_a_new_telegram_turn(self, pending: bool) -> list[str]:
        """Run Telegram's own persist and return the titles it wrote.

        Called unbound with a stub ``self``: the method reaches only ``conv_log``,
        so a whole dispatcher would add a Telegram client and a session store to
        the fixture without changing what is measured.
        """
        from kiro_crew.telegram.transport_dispatch import TelegramDispatcher

        titles: list[str] = []

        class _Atomic:
            def __enter__(self) -> None:
                return None

            def __exit__(self, *exc: object) -> bool:
                return False

        class _ConvLog:
            def atomic_appends(self, key: str) -> _Atomic:
                return _Atomic()

            def append(self, *args: object, **kwargs: object) -> None:
                return None

            def set_title(self, key: str, title: str) -> None:
                titles.append(title)

        TelegramDispatcher._persist_turn(
            SimpleNamespace(conv_log=_ConvLog()),
            _KEY,
            "Deploy the gateway",
            "on it",
            True,
            auto_title_pending=pending,
        )
        return titles

    def test_a_pending_auto_title_suppresses_the_deterministic_fallback(self):
        """A fallback written here would become the conversation's name for good.

        The guard refuses a record that already carries a name, and a refusal on a
        record that is present and unchanged KEEPS the claim -- so the truncation
        would stick and no later exchange would retry. The claim cannot suppress
        this write by itself, because it is taken AFTER it: the pin reads a record
        this turn has already persisted, so the caller passes the suppression
        explicitly.

        Mutation: ignore ``auto_title_pending`` in ``_persist_turn`` -- red, the
        truncation is written.
        """
        assert self._persist_a_new_telegram_turn(pending=True) == []

    def test_a_turn_no_one_will_title_still_gets_the_fallback(self):
        """The complement, so the suppression cannot be read as removing it.

        A turn with no text, a restricted session, a resumed session or a key
        already claimed elsewhere gets no generated name, and a new conversation
        with no name at all is worse than a truncated one.

        Mutation: suppress unconditionally -- red, no title is written.
        """
        assert self._persist_a_new_telegram_turn(pending=False) == ["Deploy the gateway"]


async def _append(sink: list[str], title: str) -> None:
    sink.append(title)


# ──────────────────────────────────────────────────────────────────────
# The turn itself
# ──────────────────────────────────────────────────────────────────────
class TestTurn:
    @pytest.mark.asyncio
    async def test_every_tool_request_is_rejected_and_audited(self, audits):
        """A naming turn must never run a tool.

        The prompt is built from text the model itself produced, so a tool call
        here is prompt-injection reach. Mutation: drop the
        ``EVENT_PERMISSION_REQUEST`` branch — red on both assertions (nothing
        rejected, nothing audited), and the request is left unanswered so the
        agent process wedges.
        """
        provider = _Provider(
            [
                _ev(EVENT_PERMISSION_REQUEST, request_id="rq1"),
                _ev(EVENT_TEXT_CHUNK, text="Deploy the gateway"),
                _ev(EVENT_COMPLETE),
            ]
        )
        title = await _call(_Sessions(provider), None, _KEY, "u", "a", source="telegram")
        assert provider.rejected == ["rq1"]
        assert title == "Deploy the gateway"
        rejections = [e for e in audits if e["operation"] == "auto_title.tool_rejected"]
        assert rejections and rejections[0]["outcome"] == "denied"
        assert rejections[0]["source"] == "telegram"
        assert rejections[0]["resources"] == "rq1"

    @pytest.mark.asyncio
    async def test_the_background_session_is_released(self, audits):
        sessions = _Sessions(_title_provider())
        await _call(sessions, None, _KEY, "u", "a", source="telegram")
        assert sessions.released  # BACKGROUND_KEY released in background_turn's finally

    @pytest.mark.asyncio
    async def test_the_turn_label_names_the_channel(self, audits, monkeypatch):
        """Background spend is attributed per channel, not pooled.

        Mutation: hardcode ``task="slack_auto_title"`` — red.
        """
        seen: dict = {}
        real = auto_title.background_turn

        def _spy(sessions, *, task, agent=None):
            seen["task"] = task
            return real(sessions, task=task, agent=agent)

        monkeypatch.setattr(auto_title, "background_turn", _spy)
        await _call(_Sessions(_title_provider()), None, _KEY, "u", "a", source="telegram")
        assert seen["task"] == "telegram_auto_title"

    @pytest.mark.asyncio
    async def test_the_prompt_is_bounded_on_both_sides(self, audits):
        """Mutation: drop the ``[:TITLE_INPUT_CHARS]`` slices — red.

        An unbounded prompt is an unbounded bill on a turn whose whole output is
        six words.
        """
        provider = _title_provider()
        await _call(_Sessions(provider), None, _KEY, "u" * 5000, "a" * 5000, source="telegram")
        prompt = provider.prompts[0]
        assert "u" * auto_title.TITLE_INPUT_CHARS in prompt
        assert "u" * (auto_title.TITLE_INPUT_CHARS + 1) not in prompt
        assert "a" * (auto_title.TITLE_INPUT_CHARS + 1) not in prompt

    @pytest.mark.asyncio
    async def test_a_skip_verdict_releases_the_claim(self, audits):
        """Mutation: drop the ``release_claim`` on the SKIP branch — red.

        A conversation that was not nameable YET must be nameable at its next
        exchange; keeping the claim leaves it on the fallback name forever.
        """
        auto_title.try_claim(_KEY)
        renamed: list[str] = []
        title = await _call(
            _Sessions(_Provider([_ev(EVENT_TEXT_CHUNK, text="SKIP"), _ev(EVENT_COMPLETE)])),
            _Log(),
            _KEY,
            "hi",
            "hello",
            source="telegram",
            set_channel_title=lambda t: _append(renamed, t),
        )
        assert title == ""
        assert renamed == []
        assert auto_title.is_titled(_KEY) is False

    @pytest.mark.asyncio
    async def test_a_stream_failure_releases_the_claim(self, audits):
        """Mutation: drop the ``release_claim`` in the outer ``except`` — red."""
        auto_title.try_claim(_KEY)
        title = await _call(
            _Sessions(_Provider(raises=RuntimeError("provider died"))),
            _Log(),
            _KEY,
            "u",
            "a",
            source="telegram",
        )
        assert title == ""
        assert auto_title.is_titled(_KEY) is False

    @pytest.mark.asyncio
    async def test_a_slow_turn_is_abandoned_and_the_claim_released(self, audits, monkeypatch):
        """Mutation: replace the ``wait_for`` timeout with ``None`` — red.

        The provider WOULD produce a usable title, just far too late, so without
        the budget the title lands and the log is written. The budget is lowered
        rather than waited out, so the passing case stays fast.
        """
        monkeypatch.setattr(auto_title, "TITLE_TURN_TIMEOUT_SECS", 0.01)
        auto_title.try_claim(_KEY)
        log = _Log()
        title = await _call(
            _Sessions(_Provider.slow("Deploy the gateway", 0.5)),
            log,
            _KEY,
            "u",
            "a",
            source="telegram",
        )
        assert title == ""
        assert "title" not in log.meta  # the abandoned turn wrote nothing
        assert auto_title.is_titled(_KEY) is False

    @pytest.mark.asyncio
    async def test_a_transcript_write_failure_still_renames_the_channel(self, audits):
        """The channel is renamed even when the transcript write fails.

        A name was generated and the turn was spent; losing the transcript write
        must not also lose the visible rename, and must not look like a retryable
        failure. The guard beside it governs the DURABLE record only, so a write
        that raises still reaches the channel: this test is what fails if that
        guard is ever widened to cover the rename as well.
        """
        auto_title.try_claim(_KEY)
        renamed: list[str] = []
        title = await _call(
            _Sessions(_title_provider()),
            _Log(raises=OSError("log locked")),
            _KEY,
            "u",
            "a",
            source="telegram",
            set_channel_title=lambda t: _append(renamed, t),
        )
        assert title == "Deploy the gateway"
        assert renamed == ["Deploy the gateway"]
        assert auto_title.is_titled(_KEY) is True

    @pytest.mark.asyncio
    async def test_no_store_at_all_still_renames_the_channel(self, audits):
        """``conv_log`` of ``None`` is documented: a channel with no transcript, or
        a restricted session that persists nothing. There is no record to pin and
        no write to authorize, so the rename is the whole operation and must still
        happen -- gating it on the store's verdict would silence naming entirely
        for those channels.
        """
        auto_title.try_claim(_KEY)
        renamed: list[str] = []
        title = await _call(
            _Sessions(_title_provider()),
            None,
            _KEY,
            "u",
            "a",
            source="telegram",
            set_channel_title=lambda t: _append(renamed, t),
        )
        assert title == "Deploy the gateway"
        assert renamed == ["Deploy the gateway"]

    @pytest.mark.asyncio
    async def test_no_channel_setter_still_titles_the_transcript(self, audits):
        """A channel with no renameable conversation omits the callback."""
        log = _Log()
        title = await _call(_Sessions(_title_provider()), log, _KEY, "u", "a", source="telegram")
        assert title == "Deploy the gateway"
        assert log.meta["title"] == "Deploy the gateway"

    @pytest.mark.asyncio
    async def test_the_success_audit_names_the_channel(self, audits):
        await _call(
            _Sessions(_title_provider()),
            None,
            _KEY,
            "u",
            "a",
            source="telegram",
            resources="chat42:" + _KEY,
        )
        applied = [e for e in audits if e["operation"] == "telegram.thread_auto_title"]
        assert applied and applied[0]["source"] == "telegram"
        assert applied[0]["resources"] == "chat42:" + _KEY


# ──────────────────────────────────────────────────────────────────────
# Prompt and title cleaning
# ──────────────────────────────────────────────────────────────────────
class TestCleaning:
    def test_a_curly_brace_in_the_conversation_does_not_raise(self):
        """The conversation text reaches the prompt verbatim, braces included.

        Mutation: apply ``.format(...)`` to the assembled prompt (the shape that
        made this an f-string) — red with ``KeyError: '"key"'``, swallowed by the
        outer ``except`` as a silently missing title for every JSON exchange.
        """
        prompt = auto_title.build_title_prompt('parse this: {"key": "value"}', "sure {}")
        assert '{"key": "value"}' in prompt
        assert "sure {}" in prompt

    def test_only_the_first_line_is_kept_and_quoting_is_trimmed(self):
        assert auto_title.clean_title('"Deploy the gateway".\nand more') == "Deploy the gateway"

    def test_angle_brackets_are_dropped(self):
        """They open a link in Slack mrkdwn and a tag in Telegram HTML, and a
        title is rendered as-is on both. Mutation: drop the ``replace`` calls —
        red."""
        cleaned = auto_title.clean_title("<https://evil.test|click me>")
        assert "<" not in cleaned and ">" not in cleaned

    def test_the_skip_verdict_and_an_empty_reply_mean_no_title(self):
        assert auto_title.clean_title("SKIP") == ""
        assert auto_title.clean_title("skip") == ""
        assert auto_title.clean_title("") == ""
        assert auto_title.clean_title("   \n  ") == ""

    def test_a_credential_in_the_title_is_redacted(self):
        """The model can echo a secret back in the name it proposes, and a title
        is displayed everywhere the conversation is listed. Mutation: drop the two
        redactor calls — red."""
        cleaned = auto_title.clean_title("AKIAIOSFODNN7EXAMPLE key rotation")
        assert "AKIAIOSFODNN7EXAMPLE" not in cleaned

    def test_the_title_is_capped(self):
        """Mutation: drop the ``[:TITLE_MAX_CHARS]`` slice — red."""
        assert len(auto_title.clean_title("z" * 500)) == auto_title.TITLE_MAX_CHARS


# ──────────────────────────────────────────────────────────────────────
# The per-loop lock
# ──────────────────────────────────────────────────────────────────────
class TestLock:
    @pytest.mark.asyncio
    async def test_reset_releases_a_held_permit(self):
        """``reset()`` does both halves, and this is the half easy to leave out.

        A caller resetting this state is recovering from something that did not
        finish: a test crashing mid-title leaves the claim marked AND the lock held.
        Clearing only the claim leaves the next caller blocking on a permit nobody
        will release, and `LoopBoundLock` rebinding per loop covers a NEW loop but
        not a leaked permit on the same one.
        """
        held = auto_title.get_lock()
        await held.acquire()  # deliberately never released, as a crash would leave it
        assert held.locked()

        auto_title.reset()

        fresh = auto_title.get_lock()
        assert fresh is not held, "reset must install a lock, not reuse the held one"
        assert not fresh.locked()
        # And it is actually usable, not merely reporting itself free.
        await asyncio.wait_for(fresh.acquire(), timeout=1)
        fresh.release()

    def test_the_lock_still_works_when_the_event_loop_changes(self):
        """A bare module-global ``asyncio.Lock`` acquired from a second loop raises
        ``RuntimeError``, which the outer ``except Exception`` then swallows as a
        silently skipped title. The shared ``LoopBoundLock`` keeps one inner lock
        per loop, so the guarantee is asserted through what a caller can observe:
        the second loop acquires it and its title still lands.

        The lock OBJECT is deliberately stable across loops -- it is the module
        global callers hold -- so identity is not the thing to assert here; a
        rebound pointer is the design ``LoopBoundLock`` exists to replace, because
        a release on one loop would unlock another's critical section.
        """

        def _run_once(key: str):
            provider = _title_provider()
            log = _Log()

            async def _hold(lock, seen: list[int]):
                async with lock:
                    seen.append(1)
                    await asyncio.sleep(0)

            async def _go():
                lock = auto_title.get_lock()
                # CONTEND it. An uncontended `asyncio.Lock.acquire()` fast-paths
                # without ever calling `_get_loop()`, so it never binds a loop and
                # a bare lock would pass this test with the defect intact. Two
                # concurrent holders make the second one wait, which is the call
                # that binds -- and, for a bare lock on the second loop, raises.
                seen: list[int] = []
                await asyncio.gather(*(_hold(lock, seen) for _ in range(3)))
                assert len(seen) == 3
                await _call(_Sessions(provider), log, key, "u", "a", source="telegram")
                return lock, lock._bound()  # this loop's underlying asyncio.Lock

            lock, inner = asyncio.run(_go())
            return lock, inner, log

        lock1, inner1, log1 = _run_once("k1")
        lock2, inner2, log2 = _run_once("k2")
        # One shared chokepoint object, one inner lock per loop.
        assert lock2 is lock1, "the module global is the object callers hold"
        assert inner2 is not inner1
        assert log1.meta["title"] == "Deploy the gateway"
        assert log2.meta["title"] == "Deploy the gateway"

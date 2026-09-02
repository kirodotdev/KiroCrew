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
    against the record as it stands at write time, and the return value says
    whether the merge was applied.
    """

    def __init__(self, meta: dict | None = None, raises: BaseException | None = None):
        self.meta = dict(meta or {})
        self.raises = raises
        self.guarded_calls: list[tuple[str, dict]] = []

    def update_metadata_if(self, key, fields, guard):
        if self.raises is not None:
            raise self.raises
        self.guarded_calls.append((key, dict(fields)))
        if not guard(self.meta):
            return False
        self.meta.update(fields)
        return True


def _title_provider(title: str = "Deploy the gateway") -> _Provider:
    return _Provider([_ev(EVENT_TEXT_CHUNK, text=title), _ev(EVENT_COMPLETE)])


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
            title = await auto_title.maybe_auto_title(
                sessions, log, _KEY, "user", "assistant", source="telegram"
            )
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
        title = await auto_title.maybe_auto_title(
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
        title = await auto_title.maybe_auto_title(
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
        title = await auto_title.maybe_auto_title(
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
        title = await auto_title.maybe_auto_title(
            _Sessions(_title_provider()), log, _KEY, "u", "a", source="telegram"
        )
        assert title == "Deploy the gateway"


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
        title = await auto_title.maybe_auto_title(
            _Sessions(provider), None, _KEY, "u", "a", source="telegram"
        )
        assert provider.rejected == ["rq1"]
        assert title == "Deploy the gateway"
        rejections = [e for e in audits if e["operation"] == "auto_title.tool_rejected"]
        assert rejections and rejections[0]["outcome"] == "denied"
        assert rejections[0]["source"] == "telegram"
        assert rejections[0]["resources"] == "rq1"

    @pytest.mark.asyncio
    async def test_the_background_session_is_released(self, audits):
        sessions = _Sessions(_title_provider())
        await auto_title.maybe_auto_title(sessions, None, _KEY, "u", "a", source="telegram")
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
        await auto_title.maybe_auto_title(
            _Sessions(_title_provider()), None, _KEY, "u", "a", source="telegram"
        )
        assert seen["task"] == "telegram_auto_title"

    @pytest.mark.asyncio
    async def test_the_prompt_is_bounded_on_both_sides(self, audits):
        """Mutation: drop the ``[:TITLE_INPUT_CHARS]`` slices — red.

        An unbounded prompt is an unbounded bill on a turn whose whole output is
        six words.
        """
        provider = _title_provider()
        await auto_title.maybe_auto_title(
            _Sessions(provider), None, _KEY, "u" * 5000, "a" * 5000, source="telegram"
        )
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
        title = await auto_title.maybe_auto_title(
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
        title = await auto_title.maybe_auto_title(
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
        title = await auto_title.maybe_auto_title(
            _Sessions(_Provider.slow("Deploy the gateway", 0.5)),
            log,
            _KEY,
            "u",
            "a",
            source="telegram",
        )
        assert title == ""
        assert log.meta == {}
        assert auto_title.is_titled(_KEY) is False

    @pytest.mark.asyncio
    async def test_a_transcript_write_failure_still_renames_the_channel(self, audits):
        """A name was generated and the turn was spent; losing the transcript
        write must not also lose the visible rename, and must not look like a
        retryable failure."""
        auto_title.try_claim(_KEY)
        renamed: list[str] = []
        title = await auto_title.maybe_auto_title(
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
    async def test_no_channel_setter_still_titles_the_transcript(self, audits):
        """A channel with no renameable conversation omits the callback."""
        log = _Log()
        title = await auto_title.maybe_auto_title(
            _Sessions(_title_provider()), log, _KEY, "u", "a", source="telegram"
        )
        assert title == "Deploy the gateway"
        assert log.meta["title"] == "Deploy the gateway"

    @pytest.mark.asyncio
    async def test_the_success_audit_names_the_channel(self, audits):
        await auto_title.maybe_auto_title(
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
                await auto_title.maybe_auto_title(
                    _Sessions(provider), log, key, "u", "a", source="telegram"
                )
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

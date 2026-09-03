"""Tests for kiro_crew.feishu.streaming_card (StreamingCardSession, Layer 2c).

What is worth pinning here is the failure policy, not the happy path. A live card
is the only surface the user sees, so every branch decides between three
user-visible outcomes: the answer keeps streaming, the answer arrives as an
ordinary text reply instead, or the answer is lost. The tests are organised by
that decision rather than by method.

Two things about the transport shape drive the design of ``FakeClient`` below:
Feishu answers a *business* error with HTTP 200 and a non-zero ``code`` in the
JSON body, and ``LarkClient`` turns that body code into ``CardApiError.code``.
A fake that only raised bare exceptions would report every failure as
"unrecognised -> fall back to text" and would never exercise the code-driven
policy that is the whole point of the module.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, NamedTuple

import pytest

from kiro_crew.feishu import streaming_card as sc
from kiro_crew.feishu.client import CardApiError
from kiro_crew.feishu.streaming_card import StreamingCardSession

_LOGGER = "kiro_crew.feishu.streaming_card"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class Call(NamedTuple):
    """One recorded CardKit call, tagged with the lifecycle step it belongs to."""

    kind: str
    method: str
    path: str
    body: dict[str, Any]


def _kind(method: str, path: str) -> str:
    """Name the lifecycle step a (method, path) pair represents.

    Deliberately derived from the request itself rather than from call order, so
    an out-of-order lifecycle shows up as a wrong *sequence of names* instead of
    quietly passing.
    """
    if method == "POST":
        return "create"  # step 1
    if method == "PATCH":
        return "settings"  # step 4
    if path.endswith("/content"):
        return "push"  # step 3
    return "final"  # step 5


class FakeClient:
    """Records CardKit calls without needing lark_oapi.

    Mirrors the two parts of the real ``LarkClient`` contract this module leans
    on: ``card_api`` resolves to the envelope's ``data`` object (which is where
    ``start`` finds ``card_id``), and a non-zero body code becomes a
    ``CardApiError`` carrying that code. Queue codes per step in ``codes`` to
    drive the error taxonomy, or put an exception in ``raises`` to drive the
    "unrecognised failure" path.
    """

    def __init__(self, card_id: str = "card-1") -> None:
        self.card_id = card_id
        self.calls: list[Call] = []
        self.replies: list[tuple[str, str]] = []
        # step name -> body codes answered one per call, in order (0 == success)
        self.codes: dict[str, list[int]] = {}
        # step name -> raise this instead (a transport error, no body code)
        self.raises: dict[str, BaseException] = {}
        self.reply_raises: BaseException | None = None

    def _answer(self, kind: str, path: str) -> None:
        exc = self.raises.get(kind)
        if exc is not None:
            raise exc
        queue = self.codes.get(kind) or []
        code = queue.pop(0) if queue else 0
        if code:
            # Exactly what LarkClient._sync_http does with a 200 + body code.
            raise CardApiError(code, f"simulated code {code}", path)

    async def card_api(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        kind = _kind(method, path)
        self.calls.append(Call(kind, method, path, dict(body or {})))
        self._answer(kind, path)
        return {"card_id": self.card_id} if kind == "create" else {}

    async def send_card_reply(self, message_id: str, card_id: str) -> None:
        self.replies.append((message_id, card_id))
        if self.reply_raises is not None:
            raise self.reply_raises

    # -- readers -------------------------------------------------------------

    @property
    def kinds(self) -> list[str]:
        return [c.kind for c in self.calls]

    def of(self, kind: str) -> list[Call]:
        return [c for c in self.calls if c.kind == kind]

    def contents(self) -> list[str]:
        """The text carried by each content push, in order."""
        return [str(c.body.get("content", "")) for c in self.of("push")]

    def sequences(self) -> list[int]:
        return [int(c.body["sequence"]) for c in self.calls if "sequence" in c.body]


class FakeClock:
    """Stands in for the module's ``time`` so the throttle is deterministic.

    The module only ever asks for ``monotonic()``. Driving it from the test is
    what lets the 100 ms throttle and the 2 s long-gap deferral be asserted
    without sleeping, which on a loaded xdist worker is the difference between a
    real assertion and a coin flip.
    """

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(sc, "time", fake)
    return fake


async def _started(client: FakeClient, message_id: str = "msg-1") -> StreamingCardSession:
    session = StreamingCardSession(client, message_id)
    assert await session.start() is True
    return session


def _table(tag: str) -> str:
    return f"| {tag} | b |\n| --- | --- |\n| 1 | 2 |\n"


# ---------------------------------------------------------------------------
# Card payloads
# ---------------------------------------------------------------------------


class TestCardPayloads:
    def test_streaming_card_declares_streaming_and_the_target_element(self) -> None:
        card = sc.build_streaming_card()
        assert card["config"]["streaming_mode"] is True
        element = card["body"]["elements"][0]
        # Every content push addresses this element by id; a mismatch would make
        # the card render but never update.
        assert element["element_id"] == sc.STREAMING_ELEMENT_ID
        assert element["content"] == ""
        pacing = card["config"]["streaming_config"]
        assert pacing["print_frequency_ms"]["default"] == sc.STREAM_PRINT_FREQUENCY_MS
        assert pacing["print_step"]["default"] == sc.STREAM_PRINT_STEP

    def test_summary_is_clamped_to_fifty_chars(self) -> None:
        card = sc.build_streaming_card("x" * 200)
        assert len(card["config"]["summary"]["content"]) == 50

    def test_final_card_carries_the_text_and_drops_streaming(self) -> None:
        card = sc.build_final_card("done")
        assert "streaming_mode" not in card["config"]
        assert card["body"]["elements"][0]["content"] == "done"


# ---------------------------------------------------------------------------
# Markdown normalisation
# ---------------------------------------------------------------------------


class TestNormalizeCardLinks:
    def test_a_bare_url_becomes_an_explicit_link(self) -> None:
        out = sc.normalize_card_links("see https://example.com/a")
        assert out == "see [https://example.com/a](https://example.com/a)"

    def test_underscores_are_percent_encoded_in_the_target_only(self) -> None:
        """The visible label keeps the real URL; only the target is encoded.

        Feishu re-tokenizes a bare URL and splits it around ``_``, so the target
        must be encoded -- but encoding the label too would show the user a URL
        they cannot read back or retype.
        """
        out = sc.normalize_card_links("https://example.com/a_b_c")
        assert out == "[https://example.com/a_b_c](https://example.com/a%5Fb%5Fc)"

    def test_trailing_sentence_punctuation_stays_outside_the_link(self) -> None:
        out = sc.normalize_card_links("go to https://example.com/a.")
        assert out == "go to [https://example.com/a](https://example.com/a)."

    def test_an_already_linked_url_is_left_alone(self) -> None:
        text = "[label](https://example.com/a_b)"
        assert sc.normalize_card_links(text) == text

    def test_a_url_inside_inline_code_is_protected(self) -> None:
        text = "run `curl https://example.com/a_b` now"
        assert sc.normalize_card_links(text) == text

    def test_a_url_inside_a_fence_is_protected(self) -> None:
        text = "```\nhttps://example.com/a_b\n```"
        assert sc.normalize_card_links(text) == text

    def test_an_indented_fence_is_dedented(self) -> None:
        """An indented ``` is not recognised by the card renderer at all, so the
        block would render as literal text with its backticks showing."""
        out = sc.normalize_card_links("    ```\n    code\n    ```")
        assert out.startswith("```\n")
        assert out.endswith("```")

    def test_an_internal_failure_returns_the_argument_unchanged(self, monkeypatch: Any) -> None:
        """Fault-injected on a pure helper -- the only way to reach the guard.

        The wrapper exists so a *rendering nicety* can never cost the user the
        reply: the text goes out unlinkified rather than not at all. It returns
        the ARGUMENT, byte for byte -- the fence dedent that runs first is bound
        to its own name precisely so a later failure cannot leak a
        half-transformed value out through the guard.
        """

        def boom(_text: str) -> Any:
            raise RuntimeError("protect exploded")

        src = "    ```\nsee https://example.com/a_b\n```"
        monkeypatch.setattr(sc, "_protect", boom)
        out = sc.normalize_card_links(src)

        assert out == src


class TestOptimizeCardMarkdown:
    def test_headings_are_clamped(self) -> None:
        """``#``/``##`` render enormous in a card; h1-h3 collapse to h4/h5."""
        out = sc.optimize_card_markdown("# Title\n## Sub\n### Deep\n")
        assert out == "#### Title\n##### Sub\n##### Deep\n"

    def test_a_heading_already_small_enough_is_untouched(self) -> None:
        text = "#### Title\n###### Deep\n"
        assert sc.optimize_card_markdown(text) == text

    def test_a_heading_inside_a_fence_is_not_clamped(self) -> None:
        out = sc.optimize_card_markdown("```\n# not a heading\n```")
        assert "# not a heading" in out
        assert "#### not a heading" not in out

    def test_an_image_feishu_cannot_resolve_is_stripped(self) -> None:
        """An http(s) or local image target fails the WHOLE card (200570), so
        losing the image is strictly better than losing the answer."""
        out = sc.optimize_card_markdown("before ![alt](https://example.com/a.png) after")
        assert out == "before  after"

    def test_a_feishu_image_key_is_kept(self) -> None:
        text = "![alt](img_v2_abc)"
        assert sc.optimize_card_markdown(text) == text

    def test_runs_of_blank_lines_are_collapsed(self) -> None:
        assert sc.optimize_card_markdown("a\n\n\n\n\nb") == "a\n\nb"

    def test_a_fence_gets_vertical_space_before_it(self) -> None:
        """Blank lines produce no vertical space in a card, so a ``<br>`` is
        injected around block constructs.

        Only the space *before the opening fence* is asserted. The same pass also
        injects a ``<br>`` immediately after an opening fence and before a
        closing one -- i.e. inside the code block -- which is reported separately
        as a defect rather than pinned here.
        """
        out = sc.optimize_card_markdown("text\n```\ncode\n```")
        assert "text\n<br>\n```" in out

    def test_an_internal_failure_returns_the_original_text(self, monkeypatch: Any) -> None:
        def boom(_text: str) -> Any:
            raise RuntimeError("protect exploded")

        monkeypatch.setattr(sc, "_protect", boom)
        text = "# Title\n![alt](https://example.com/a.png)"
        assert sc.optimize_card_markdown(text) == text


class TestFenceExcessTables:
    def test_tables_within_the_limit_are_left_native(self) -> None:
        text = "\n".join(_table(f"t{i}") for i in range(sc.FEISHU_CARD_TABLE_LIMIT))
        assert sc.fence_excess_tables(text) == text

    def test_excess_tables_are_demoted_to_fenced_code(self) -> None:
        """Feishu rejects a card carrying too many native tables outright, which
        loses the whole answer. A fenced table is ugly but arrives."""
        text = _table("keep") + "\n" + _table("demote")
        out = sc.fence_excess_tables(text, limit=1)
        assert out.startswith("| keep |")
        assert "```\n| demote | b |\n| --- | --- |\n| 1 | 2 |\n```" in out

    def test_the_whole_table_is_wrapped_not_just_the_matched_two_lines(self) -> None:
        text = _table("keep") + "\n| demote | b |\n| --- | --- |\n| r1 | x |\n| r2 | y |\n"
        out = sc.fence_excess_tables(text, limit=1)
        assert "| r1 | x |\n| r2 | y |\n```" in out

    def test_a_table_ending_at_eof_without_a_newline_is_closed(self) -> None:
        text = _table("keep") + "\n| demote | b |\n| --- | --- |\n| 1 | 2 |"
        out = sc.fence_excess_tables(text, limit=1)
        assert out.endswith("| 1 | 2 |\n```\n")

    def test_prose_between_tables_is_preserved(self) -> None:
        text = _table("keep") + "\nprose line\n" + _table("demote")
        out = sc.fence_excess_tables(text, limit=1)
        assert "prose line" in out

    def test_an_internal_failure_returns_the_original_text(self, monkeypatch: Any) -> None:
        class Boom:
            def finditer(self, _text: str) -> Any:
                raise RuntimeError("scan exploded")

        monkeypatch.setattr(sc, "_TABLE_RE", Boom())
        text = _table("a") + _table("b")
        assert sc.fence_excess_tables(text, limit=1) == text


class TestPrepareCardText:
    def test_the_pipeline_runs_links_then_markdown(self) -> None:
        out = sc.prepare_card_text("# T\nsee https://example.com/a_b")
        assert out.startswith("#### T")
        assert "(https://example.com/a%5Fb)" in out

    def test_table_demotion_is_opt_in(self) -> None:
        text = "\n".join(_table(f"t{i}") for i in range(sc.FEISHU_CARD_TABLE_LIMIT + 1))
        assert "```" not in sc.prepare_card_text(text)
        assert "```" in sc.prepare_card_text(text, demote_tables=True)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestStart:
    @pytest.mark.asyncio
    async def test_start_creates_a_card_then_anchors_it_to_the_message(self) -> None:
        client = FakeClient()
        session = StreamingCardSession(client, "msg-1")

        assert await session.start() is True

        assert client.kinds == ["create"]
        create = client.of("create")[0]
        assert create.path == "/open-apis/cardkit/v1/cards"
        assert create.body["type"] == "card_json"
        assert json.loads(create.body["data"]) == sc.build_streaming_card()
        # The reply carries no text of its own -- only the card_id reference.
        assert client.replies == [("msg-1", "card-1")]
        assert session.live is True
        assert session.delivered is False
        assert session.anchor_gone is False

    @pytest.mark.asyncio
    async def test_creation_consumes_no_sequence_number(self) -> None:
        client = FakeClient()
        await _started(client)
        assert "sequence" not in client.of("create")[0].body

    @pytest.mark.asyncio
    async def test_a_create_with_no_card_id_is_reported_and_not_anchored(self, caplog: Any) -> None:
        """Nothing has been shown yet, so the caller's text reply is the answer."""
        client = FakeClient(card_id="")
        session = StreamingCardSession(client, "msg-1")

        with caplog.at_level("WARNING", logger=_LOGGER):
            assert await session.start() is False

        assert client.replies == []
        assert session.live is False
        assert session.anchor_gone is False
        assert any("no card_id" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_a_recalled_anchor_during_start_forbids_the_text_fallback(self) -> None:
        client = FakeClient()
        client.codes["create"] = [sc.ERR_MSG_RECALLED]
        session = StreamingCardSession(client, "msg-1")

        assert await session.start() is False
        # A text reply to a recalled message fails too -- the caller must not try.
        assert session.anchor_gone is True

    @pytest.mark.asyncio
    async def test_an_ordinary_card_error_during_start_still_allows_fallback(
        self, caplog: Any
    ) -> None:
        client = FakeClient()
        client.codes["create"] = [99999]
        session = StreamingCardSession(client, "msg-1")

        with caplog.at_level("WARNING", logger=_LOGGER):
            assert await session.start() is False

        assert session.anchor_gone is False
        assert session.live is False
        assert caplog.records

    @pytest.mark.asyncio
    async def test_a_failure_anchoring_the_card_is_survivable(self, caplog: Any) -> None:
        """A non-CardApiError (a transport fault) must not escape start()."""
        client = FakeClient()
        client.reply_raises = RuntimeError("socket died")
        session = StreamingCardSession(client, "msg-1")

        with caplog.at_level("WARNING", logger=_LOGGER):
            assert await session.start() is False

        assert session.live is False
        assert caplog.records


class TestLifecycleOrder:
    @pytest.mark.asyncio
    async def test_the_five_steps_happen_in_order(self, clock: FakeClock) -> None:
        """Step 4 (close streaming mode) MUST precede step 5 (full replace).

        Step 5 is what repairs a dropped intermediate frame, so a card left in
        streaming mode when it lands would animate the repaired text a second
        time.
        """
        client = FakeClient()
        session = await _started(client)
        await session.push("partial", force=True)

        assert await session.finish("partial answer") is True

        assert client.kinds == ["create", "push", "push", "settings", "final"]
        assert client.kinds.index("settings") < client.kinds.index("final")

    @pytest.mark.asyncio
    async def test_step_four_turns_streaming_mode_off(self, clock: FakeClock) -> None:
        client = FakeClient()
        session = await _started(client)
        await session.finish("answer")

        settings = client.of("settings")[0]
        assert settings.path.endswith(f"/cards/{client.card_id}/settings")
        assert json.loads(settings.body["settings"]) == {"streaming_mode": False}

    @pytest.mark.asyncio
    async def test_step_five_replaces_the_card_with_the_whole_answer(
        self, clock: FakeClock
    ) -> None:
        client = FakeClient()
        session = await _started(client)
        await session.finish("the whole answer")

        final = client.of("final")[0]
        assert final.path == f"/open-apis/cardkit/v1/cards/{client.card_id}"
        card = json.loads(final.body["card"]["data"])
        assert card["body"]["elements"][0]["content"] == "the whole answer"

    @pytest.mark.asyncio
    async def test_content_pushes_address_the_streaming_element(self, clock: FakeClock) -> None:
        client = FakeClient()
        session = await _started(client)
        await session.push("hi", force=True)

        push = client.of("push")[0]
        assert push.path == (
            f"/open-apis/cardkit/v1/cards/{client.card_id}"
            f"/elements/{sc.STREAMING_ELEMENT_ID}/content"
        )

    @pytest.mark.asyncio
    async def test_finish_on_a_session_that_never_started_touches_nothing(self) -> None:
        client = FakeClient()
        session = StreamingCardSession(client, "msg-1")
        assert await session.finish("answer") is False
        assert client.calls == []


class TestSequenceNumbers:
    @pytest.mark.asyncio
    async def test_every_mutating_step_takes_the_next_number(self, clock: FakeClock) -> None:
        client = FakeClient()
        session = await _started(client)
        await session.push("one", force=True)
        await session.finish("one two")

        seqs = client.sequences()
        assert len(seqs) == 4  # two pushes, settings, final replace
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs)

    @pytest.mark.asyncio
    async def test_a_failed_push_does_not_give_its_number_back(self, clock: FakeClock) -> None:
        """Rolling back would risk reusing a number the server already consumed.
        A gap is tolerated by Feishu; only monotonicity matters."""
        client = FakeClient()
        client.codes["push"] = [sc.ERR_RATE_LIMITED]
        session = await _started(client)

        await session.push("first frame", force=True)
        await session.push("first frame extended", force=True)

        seqs = client.sequences()
        assert len(seqs) == 2
        assert seqs[1] > seqs[0]


class TestCumulativeText:
    @pytest.mark.asyncio
    async def test_each_push_carries_the_whole_answer_so_far(self, clock: FakeClock) -> None:
        """The server diffs successive pushes to animate them, and it is what
        makes a dropped frame self-healing. A delta would corrupt both."""
        client = FakeClient()
        session = await _started(client)

        await session.push("Hello ", force=True)
        await session.push("Hello world", force=True)

        assert client.contents() == ["Hello ", "Hello world"]


# ---------------------------------------------------------------------------
# Error taxonomy
# ---------------------------------------------------------------------------


class TestRateLimited:
    @pytest.mark.asyncio
    async def test_a_rate_limited_frame_is_dropped_without_retiring(
        self, clock: FakeClock, caplog: Any
    ) -> None:
        client = FakeClient()
        client.codes["push"] = [sc.ERR_RATE_LIMITED]
        session = await _started(client)

        with caplog.at_level("DEBUG", logger=_LOGGER):
            await session.push("dropped frame", force=True)

        assert session.live is True
        assert session.anchor_gone is False
        assert session.delivered is False

    @pytest.mark.asyncio
    async def test_the_next_push_supersedes_the_dropped_one(self, clock: FakeClock) -> None:
        """No retry and no backoff is safe *because* the next push is cumulative:
        the newer frame carries everything the lost one did."""
        client = FakeClient()
        client.codes["push"] = [sc.ERR_RATE_LIMITED]
        session = await _started(client)

        await session.push("half", force=True)
        await session.push("half and half", force=True)

        assert client.contents() == ["half", "half and half"]
        assert session.live is True


class TestCardConstraint:
    @pytest.mark.asyncio
    async def test_the_first_constraint_hit_demotes_tables_and_keeps_streaming(
        self, clock: FakeClock
    ) -> None:
        client = FakeClient()
        client.codes["push"] = [sc.ERR_CARD_CONSTRAINT]
        session = await _started(client)
        text = "\n".join(_table(f"t{i}") for i in range(sc.FEISHU_CARD_TABLE_LIMIT + 1))

        await session.push(text, force=True)
        assert session.live is True
        assert "```" not in client.contents()[0]

        await session.push(text, force=True)
        # Same input, now demoted -- which is the observable proof the flag stuck.
        assert "```" in client.contents()[1]

    @pytest.mark.asyncio
    async def test_a_second_constraint_hit_retires_the_session(self, clock: FakeClock) -> None:
        """Demotion was the one repair available; a second rejection means the
        card cannot be made to render, so the caller should send text."""
        client = FakeClient()
        client.codes["push"] = [sc.ERR_CARD_CONSTRAINT, sc.ERR_CARD_CONSTRAINT]
        session = await _started(client)

        await session.push("first", force=True)
        await session.push("first second", force=True)

        assert session.live is False
        assert session.anchor_gone is False
        assert session.delivered is False


class TestAnchorGone:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("code", [sc.ERR_MSG_RECALLED, sc.ERR_MSG_DELETED])
    async def test_a_gone_anchor_retires_and_forbids_a_text_fallback(
        self, clock: FakeClock, code: int
    ) -> None:
        """Nothing can reach a recalled or deleted message, a plain-text reply
        included -- so the caller must be told not to try, or it raises on a
        second undeliverable send."""
        client = FakeClient()
        client.codes["push"] = [code]
        session = await _started(client)

        await session.push("text", force=True)

        assert session.live is False
        assert session.anchor_gone is True

    @pytest.mark.asyncio
    async def test_a_retired_session_stops_pushing(self, clock: FakeClock) -> None:
        client = FakeClient()
        client.codes["push"] = [sc.ERR_MSG_DELETED]
        session = await _started(client)
        await session.push("text", force=True)

        await session.push("more text", force=True)
        assert len(client.of("push")) == 1

    @pytest.mark.asyncio
    async def test_finish_on_a_retired_session_makes_no_further_calls(
        self, clock: FakeClock
    ) -> None:
        client = FakeClient()
        client.codes["push"] = [sc.ERR_MSG_DELETED]
        session = await _started(client)
        await session.push("text", force=True)

        assert await session.finish("text and more") is False
        assert client.kinds == ["create", "push"]


class TestConcurrentFlushes:
    @pytest.mark.asyncio
    async def test_a_frame_queued_on_the_lock_is_dropped_if_the_card_retires(
        self, clock: FakeClock
    ) -> None:
        """The liveness check is repeated *after* acquiring the lock, and that
        second check is the one that matters.

        A renderer can call push() again while an earlier push is still in
        flight. The second caller passed the first check while the card was
        still live, so without the re-check it would push to a card that the
        first call has since retired -- against a recalled anchor, an error per
        frame for the rest of the turn.
        """
        client = FakeClient()
        client.codes["push"] = [sc.ERR_MSG_DELETED]
        entered = asyncio.Event()
        release = asyncio.Event()
        real_card_api = client.card_api

        async def gated(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
            if _kind(method, path) == "push" and not entered.is_set():
                entered.set()
                await release.wait()
            return await real_card_api(method, path, body)

        client.card_api = gated  # type: ignore[method-assign]
        session = await _started(client)

        first = asyncio.create_task(session.push("first", force=True))
        await entered.wait()  # first call holds the lock
        second = asyncio.create_task(session.push("second", force=True))

        # Wait for the second call to be genuinely queued ON the lock. Yielding a
        # fixed number of times would silently pass by never starting it at all,
        # which exercises the pre-lock check instead of the one under test.
        for _ in range(100):
            if getattr(session._lock, "_waiters", None):
                break
            await asyncio.sleep(0)
        else:  # pragma: no cover - only on a broken event loop
            pytest.fail("the second flush never queued on the lock")

        release.set()
        await asyncio.gather(first, second)

        assert session.live is False
        assert session.anchor_gone is True
        assert len(client.of("push")) == 1


class TestUnrecognisedFailure:
    @pytest.mark.asyncio
    async def test_an_unknown_body_code_retires_so_the_caller_falls_back(
        self, clock: FakeClock, caplog: Any
    ) -> None:
        client = FakeClient()
        client.codes["push"] = [99999]
        session = await _started(client)

        with caplog.at_level("WARNING", logger=_LOGGER):
            await session.push("text", force=True)

        assert session.live is False
        assert session.anchor_gone is False
        assert any("falling back to text" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_an_exception_with_no_code_retires_too(self, clock: FakeClock) -> None:
        """A transport fault carries no business code; unrecognised means retire."""
        client = FakeClient()
        client.raises["push"] = RuntimeError("connection reset")
        session = await _started(client)

        await session.push("text", force=True)

        assert session.live is False
        assert session.anchor_gone is False


# ---------------------------------------------------------------------------
# delivered -- the property the caller acts on
# ---------------------------------------------------------------------------


class TestDelivered:
    @pytest.mark.asyncio
    async def test_delivered_is_false_until_something_is_on_screen(self, clock: FakeClock) -> None:
        client = FakeClient()
        session = await _started(client)
        assert session.delivered is False
        await session.push("partial", force=True)
        # A pushed frame is not the finished answer; only finish() claims that.
        assert session.delivered is False

    @pytest.mark.asyncio
    async def test_delivered_is_true_once_the_answer_is_in_the_card(self, clock: FakeClock) -> None:
        """True is an instruction to the caller: do NOT also send text, or the
        user reads the same answer twice."""
        client = FakeClient()
        session = await _started(client)

        assert await session.finish("the answer") is True
        assert session.delivered is True

    @pytest.mark.asyncio
    async def test_delivered_stays_false_when_the_card_showed_nothing(
        self, clock: FakeClock
    ) -> None:
        client = FakeClient()
        client.codes["push"] = [99999]
        session = await _started(client)

        assert await session.finish("the answer") is False
        assert session.delivered is False
        # Nothing reached the user, so the caller's text reply is the only copy.
        assert client.of("final") == []

    @pytest.mark.asyncio
    async def test_finish_is_safe_to_call_again(self, clock: FakeClock) -> None:
        client = FakeClient()
        session = await _started(client)
        assert await session.finish("the answer") is True

        assert await session.finish("the answer") is True


class TestFinishRepairsDroppedFrames:
    @pytest.mark.asyncio
    async def test_a_rate_limited_final_frame_is_repaired_by_the_full_replace(
        self, clock: FakeClock
    ) -> None:
        """This is why a dropped frame needs no retry of its own: step 5 sends
        the complete text regardless of which frames were lost."""
        client = FakeClient()
        session = await _started(client)
        await session.push("half", force=True)
        client.codes["push"] = [sc.ERR_RATE_LIMITED]

        assert await session.finish("half and the rest") is True

        card = json.loads(client.of("final")[0].body["card"]["data"])
        assert card["body"]["elements"][0]["content"] == "half and the rest"

    @pytest.mark.asyncio
    async def test_a_failed_settings_patch_does_not_stop_the_full_replace(
        self, clock: FakeClock
    ) -> None:
        """Leaving streaming mode on is cosmetic -- the text is already on
        screen, so the failure is logged and step 5 still runs."""
        client = FakeClient()
        client.codes["settings"] = [99999]
        session = await _started(client)

        assert await session.finish("the answer") is True
        assert len(client.of("final")) == 1
        assert session.live is True

    @pytest.mark.asyncio
    async def test_a_failed_full_replace_still_reports_delivered(self, clock: FakeClock) -> None:
        """The content push already put the whole answer on screen, so a text
        fallback would duplicate it."""
        client = FakeClient()
        client.raises["final"] = RuntimeError("gateway timeout")
        session = await _started(client)

        assert await session.finish("the answer") is True
        assert client.contents() == ["the answer"]


# ---------------------------------------------------------------------------
# Throttle
# ---------------------------------------------------------------------------


class TestThrottle:
    @pytest.mark.asyncio
    async def test_a_push_inside_the_throttle_window_is_skipped(self, clock: FakeClock) -> None:
        client = FakeClient()
        session = await _started(client)

        clock.advance(sc.CARDKIT_THROTTLE_S / 2)
        await session.push("too soon")

        assert client.of("push") == []

    @pytest.mark.asyncio
    async def test_a_push_past_the_throttle_window_lands(self, clock: FakeClock) -> None:
        client = FakeClient()
        session = await _started(client)

        clock.advance(sc.CARDKIT_THROTTLE_S * 1.5)
        await session.push("now due")

        assert client.contents() == ["now due"]

    @pytest.mark.asyncio
    async def test_force_bypasses_the_throttle(self, clock: FakeClock) -> None:
        """Used before a long silence (a tool call), where waiting for the next
        chunk would leave the user staring at stale text."""
        client = FakeClient()
        session = await _started(client)

        await session.push("forced", force=True)

        assert client.contents() == ["forced"]

    @pytest.mark.asyncio
    async def test_a_tiny_fragment_after_a_long_gap_is_deferred_then_shown(
        self, clock: FakeClock
    ) -> None:
        """After a quiet period the first frame would carry one or two
        characters, which reads as a stutter."""
        client = FakeClient()
        session = await _started(client)

        clock.advance(sc.LONG_GAP_THRESHOLD_S + 0.5)
        await session.push("tiny")
        assert client.of("push") == []

        # Still inside the batching window.
        clock.advance(sc.BATCH_AFTER_GAP_S / 2)
        await session.push("tiny bit")
        assert client.of("push") == []

        # Past it: the deferral is a delay, never a drop.
        clock.advance(sc.BATCH_AFTER_GAP_S)
        await session.push("tiny bit more")
        assert client.contents() == ["tiny bit more"]

    @pytest.mark.asyncio
    async def test_a_substantial_fragment_after_a_long_gap_is_never_deferred(
        self, clock: FakeClock
    ) -> None:
        """Nothing schedules a retry, so deferring a real paragraph would hide it
        until the next event -- and if the model then goes quiet, forever."""
        client = FakeClient()
        session = await _started(client)

        clock.advance(sc.LONG_GAP_THRESHOLD_S + 0.5)
        await session.push("x" * (sc._ANTI_STUTTER_MAX_CHARS + 1))

        assert len(client.of("push")) == 1

    @pytest.mark.asyncio
    async def test_text_identical_to_what_is_shown_is_not_resent(self, clock: FakeClock) -> None:
        client = FakeClient()
        session = await _started(client)

        await session.push("same", force=True)
        await session.push("same", force=True)

        assert len(client.of("push")) == 1

    @pytest.mark.asyncio
    async def test_an_empty_buffer_is_not_pushed(self, clock: FakeClock) -> None:
        client = FakeClient()
        session = await _started(client)

        await session.push("", force=True)

        assert client.of("push") == []

"""``section_marker`` — the stateless directive that draws a labelled chapter
break in the calling session's transcript.

The row is HELD to a turn boundary rather than appended inline, and the reason is
positional rather than about teardown. A turn builds its prompt with
``exclude_last_n=1`` so the current turn's user message is not fed back as
history, and that exclusion is a raw positional slice applied BEFORE role
filtering — so a row appended after the current-turn user row becomes the
physical tail, absorbs the exclusion, and the user's message survives the slice
and is replayed. Keeping the role out of ``RECALL_ROLES`` does not rescue that:
membership decides whether the row itself is replayed, never which row the slice
removes. So the marker rides ``/note``'s existing hold instead of inventing a
second notion of "held".

These tests pin the tool's payload and descriptor, the applier's queuing and its
refusals, the flushed row's shape (role, label under ``meta``, no context half),
the shared per-turn cap, and the recall exclusion.

The flush seams themselves — that a held row is written at the end of a turn
rather than mid-turn — are ``chat_runner._start_next_queued_turn`` and
``_finish_queue_cycle``, covered by the ``/note`` deferral tests in
``test_gateway_appkit_endpoints.py``; this file pins that a marker lands in the
same hold those seams drain.
"""

import logging
from pathlib import Path

import pytest

from kiro_crew import mcp_core, session_directive
from kiro_crew.dashboard.session_directive_apply import (
    SECTION_MARKER_ROLE,
    _max_section_label,
    apply_session_directive,
)
from kiro_crew.session_surface import has_dashboard_surface

#: Anchored to the repo root rather than the CWD, per AUTOSDE ``test-file-paths``:
#: a relative path makes the source read depend on where pytest was invoked.
_REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _no_live_gateway():
    """Stub the out-of-band POST for every test in this module.

    Emitting a directive publishes through ``mcp_core._post``, so a bare
    ``_call_tool_inner`` reaches a running gateway and writes denied-auth audit and log
    records outside test isolation. Stubbing it here keeps the marker in the returned
    text (the kiro-cli path) exercised while the socket write is inert. The delivery
    tests re-patch the same attribute to capture calls, which nests over this one.
    """
    from unittest.mock import patch

    from kiro_crew.mcp_tools import control

    with patch.object(control.mcp_core, "_post", return_value=None):
        yield


# ───────────────────────────── the tool ──────────────────────────────────────


class TestSectionMarkerTool:
    """Stateless: the tool validates its arguments and returns a directive. It
    resolves no session identity and appends nothing itself."""

    def test_returns_the_validated_payload(self):
        result = mcp_core._call_tool_inner("section_marker", {"label": "item-42"})
        assert session_directive.decode(result, "section_marker") == {
            "label": "item-42",
        }

    def test_the_payload_carries_no_collapse_field(self):
        """Phase 1 is "the event and the rule (no collapse)". A ``collapse_earlier``
        flag was originally specified and recorded on every row, but nothing read
        it — the renderer takes only ``label`` — so it was Phase 2's surface
        shipping early. Phase 2 adds it treating an absent key as true, which is
        byte-compatible with every row written here.

        Guarded rather than merely deleted, so a later edit cannot reintroduce an
        unread flag without this failing.
        """
        for args in ({}, {"label": "x"}):
            payload = session_directive.decode(
                mcp_core._call_tool_inner("section_marker", args), "section_marker"
            )
            assert set(payload) == {"label"}, payload

    def test_an_unknown_field_is_rejected(self):
        """The negative control for the guard above: the schema is closed, so a
        caller cannot smuggle the removed flag back in as an extra key.
        """
        from kiro_crew.validation import ValidationError

        with pytest.raises(ValidationError):
            mcp_core._call_tool_inner("section_marker", {"collapse_earlier": True})

    def test_a_line_break_or_tab_in_the_label_is_rejected(self):
        """The label is drawn as a ONE-LINE rule caption by both renderers and
        persisted to the jsonl, so a newline in it splits the caption.

        Not covered by the shared hidden-category sweep: measured, that sweep
        STRIPS a zero-width space but passes ``\\n``/``\\r``/``\\t`` through
        verbatim, because they are ordinary whitespace almost everywhere else.

        U+2028/U+2029 are included because a browser honours both as forced line
        breaks in the rendered caption, and their categories are ``Zl``/``Zp`` —
        outside the sweep's ``Cc``/``Cf``/``Cs``, so only this pattern stops them.
        """
        from kiro_crew.validation import ValidationError

        for bad in ("a\nb", "a\rb", "a\tb", "a\u2028b", "a\u2029b"):
            with pytest.raises(ValidationError):
                mcp_core._call_tool_inner("section_marker", {"label": bad})

    def test_the_sweep_does_not_strip_the_unicode_separators(self):
        """Proof the pattern above is load-bearing rather than redundant.

        If the shared sweep removed U+2028/U+2029 the pattern would be belt-and-
        braces; it does not, so the pattern is the only guard standing between a
        separator in untrusted tool output and a split caption.
        """
        from kiro_crew.validation import strip_hidden_unicode

        for sep in ("\u2028", "\u2029"):
            assert sep in strip_hidden_unicode(f"a{sep}b")
        # Control: the sweep does remove the class it owns, so it is really running.
        assert strip_hidden_unicode("a\u200bb") == "ab"

    def test_the_hidden_category_sweep_still_strips_its_own_class(self):
        """Positive control for the test above, and proof the new pattern did not
        replace the sweep: a zero-width space is accepted and removed, so the two
        guards cover different characters rather than one shadowing the other.
        """
        result = mcp_core._call_tool_inner("section_marker", {"label": "a\u200bb"})
        assert session_directive.decode(result, "section_marker")["label"] == "ab"

    def test_ordinary_spaces_in_a_label_survive(self):
        """Negative control: the pattern must reject line breaks and tabs only, not
        whitespace in general — a chapter heading is normally several words.
        """
        result = mcp_core._call_tool_inner("section_marker", {"label": "review item 42"})
        assert session_directive.decode(result, "section_marker")["label"] == "review item 42"

    def test_an_overlong_label_is_rejected(self):
        """The label is a chapter heading. Uncapped, a structural row becomes a
        route for smuggling a paragraph of prose into the transcript."""
        from kiro_crew.validation import ValidationError

        with pytest.raises(ValidationError):
            mcp_core._call_tool_inner("section_marker", {"label": "x" * 121})

    def test_a_label_at_the_cap_is_accepted(self):
        """The negative control for the test above: 121 must fail *because* it is
        over the cap, not because any long label fails."""
        result = mcp_core._call_tool_inner("section_marker", {"label": "x" * 120})
        assert session_directive.decode(result, "section_marker")["label"] == "x" * 120

    def test_the_confirmation_hedges_rather_than_promising_the_row(self):
        """The applier can refuse this directive on three paths that never reach
        the model — no dashboard surface, a headless producer, and multi-stage plan
        execution — so an unconditional "it will appear" would have the assistant
        report a section break the user cannot see. Every sibling directive hedges
        for the same reason.

        The context clause is asserted too: it is the part doing real work, telling
        the model the marker is not a reset, so a reword must not drop it.
        """
        result = mcp_core._call_tool_inner("section_marker", {"label": "item-42"})
        assert "do not assume it" in result
        assert "no model context is dropped" in result
        # The bare promise must be gone: "it appears" is fine only under the
        # user-facing / not-in-a-plan condition stated alongside it.
        assert "it will appear at the end of" not in result

    def test_listed_in_tools_with_its_one_optional_parameter(self):
        descriptor = next(t for t in mcp_core._list_tools() if t["name"] == "section_marker")
        schema = descriptor["inputSchema"]
        assert schema["type"] == "object"
        assert set(schema["properties"]) == {"label"}
        assert not schema.get("required")

    def test_description_says_a_marker_separates_turns(self):
        """The one honest limitation of riding a turn-boundary hold: several
        markers emitted in one turn clump at that turn's end. A caller that does
        not know this will draw them mid-turn and get a pile."""
        descriptor = next(t for t in mcp_core._list_tools() if t["name"] == "section_marker")
        assert "separates turns" in descriptor["description"].lower()

    def test_confirmation_does_not_claim_context_was_dropped(self):
        """A marker changes only what is rendered. A model reading this as a
        context reset would stop carrying forward what it still has."""
        human = session_directive.strip_marker(
            mcp_core._call_tool_inner("section_marker", {"label": "a"})
        )
        assert "rendered" in human.lower()

    def test_is_a_recognized_directive_tool(self):
        """The forgery gate honours a marker only under a canonical directive tool
        name; omitting the tool here would make every marker it emits inert."""
        assert "section_marker" in session_directive.DIRECTIVE_TOOLS


# ───────────────────────────── the applier ───────────────────────────────────


class _FakeSlot:
    """Minimal slot: the applier touches ``key``, ``running``,
    ``_in_stage_execution``, ``_deferred_notes`` and ``append``.

    ``running`` defaults TRUE because that is the real case — the applier is
    consumed at a tool-result event, i.e. from inside a turn.
    """

    def __init__(self, key: str = "dashboard:test-slot", running: bool = True):
        self.key = key
        self.linked_session_key = ""
        self.running = running
        self._in_stage_execution = False
        self._deferred_notes: list[dict] = []
        self.messages: list[dict] = []

    def append(self, role, content, cls="", ts="", *, broadcast=True, meta=None, **kw):
        row = {"role": role, "content": content, "cls": cls, "meta": dict(meta or {})}
        self.messages.append(row)
        return row

    def append_pending_context(self, entry):  # pragma: no cover - must never fire
        raise AssertionError("a section marker must never queue model context")

    def flush(self) -> int:
        """Stand-in for ``flush_deferred_notes``' write half, reproducing the
        role/meta contract the real one applies (``slot_buffers``)."""
        held, self._deferred_notes = self._deferred_notes, []
        for note in held:
            assert note["context"] is None
            role = note.get("role") or "inject"
            self.append(
                role=role,
                content=note["content"],
                cls=note["cls"],
                meta=dict(note.get("meta") or {}),
            )
        return len(held)


async def _apply(slot, args=None, *, session_key=None, user_origin=True):
    """Apply one ``section_marker`` directive.

    ``user_origin`` defaults TRUE because that is the case the applier tests
    model: an authenticated human typed into the session's own composer, which is
    what ``chat_runner`` passes as ``producer_is_user_facing``. Pass False to
    model a HEADLESS producer (a cron turn, a sub-agent sharing its parent's
    slot, a taskrunner turn) riding the same slot.
    """
    return await apply_session_directive(
        None,
        slot,
        session_key or (slot.key if slot else "dashboard:test-slot"),
        "section_marker",
        args if args is not None else {"label": "item-42"},
        producer_is_user_facing=user_origin,
    )


class TestSectionMarkerApplier:
    @pytest.mark.asyncio
    async def test_a_marker_requested_mid_turn_is_held_not_appended(self):
        """Exit criterion 3. Appending now would make the marker the physical
        tail and hand it the ``exclude_last_n=1`` slice the user row needs, so the
        user's message would be replayed a second time."""
        slot = _FakeSlot(running=True)
        result = await _apply(slot)
        assert slot.messages == []
        assert len(slot._deferred_notes) == 1
        assert "queued" in result

        # …and it lands once the turn's seam drains the hold.
        assert slot.flush() == 1
        assert [m["role"] for m in slot.messages] == [SECTION_MARKER_ROLE]

    @pytest.mark.asyncio
    async def test_the_flushed_row_carries_its_label_under_meta(self):
        """Exit criterion 2's shape half. ``meta`` is the machine surface: the
        dashboard serializer persists ``cls`` only for ``role == "system"``, so a
        cls-carried label would vanish on one of the two write paths."""
        slot = _FakeSlot()
        await _apply(slot, {"label": "item-42"})
        slot.flush()
        row = slot.messages[0]
        assert row["meta"]["label"] == "item-42"
        # ``label`` is the WHOLE meta: no unread Phase-2 flag rides along.
        assert set(row["meta"]) == {"label"}
        assert "noteSession" not in row["meta"]

    @pytest.mark.asyncio
    async def test_content_is_a_human_readable_fallback(self):
        """``content`` is the COMPATIBILITY surface. An older client that does not
        know the role draws this string; empty content or raw JSON there would
        show a blank row or a brace salad instead of a legible line."""
        slot = _FakeSlot()
        await _apply(slot, {"label": "second-item"})
        slot.flush()
        assert slot.messages[0]["content"] == "— End of: second-item —"

    @pytest.mark.asyncio
    async def test_an_unlabelled_marker_still_draws_a_break(self):
        slot = _FakeSlot()
        await _apply(slot, {})
        slot.flush()
        assert slot.messages[0]["content"] == "— End of section —"
        assert slot.messages[0]["meta"]["label"] == ""

    @pytest.mark.asyncio
    async def test_a_credential_in_the_label_never_reaches_meta(self):
        """``meta["label"]`` is the field both renderers DRAW (``meta?.label ??
        content``), and the row is appended with ``broadcast=True`` onto a live-SSE
        path that merges direct meta unredacted — its safety argument being that
        live tool meta is redacted at its source, which is this applier. Scrubbing
        only ``content`` left the drawn field raw, so an injected key reached the
        dashboard unscanned and was scrubbed only on a later HTTP refetch."""
        slot = _FakeSlot(running=True)
        await _apply(slot, {"label": "finished AKIAIOSFODNN7EXAMPLE"})
        # Scrubbed in the HELD entry, not merely on the way out of the flush: the
        # entry is what a later flush copies into the row's meta.
        assert "AKIAIOSFODNN7EXAMPLE" not in slot._deferred_notes[0]["meta"]["label"]
        slot.flush()
        row = slot.messages[0]
        assert "AKIAIOSFODNN7EXAMPLE" not in row["meta"]["label"]
        assert "AKIAIOSFODNN7EXAMPLE" not in row["content"]
        assert "[REDACTED" in row["meta"]["label"]

    @pytest.mark.asyncio
    async def test_an_exfil_url_in_the_label_never_reaches_meta(self):
        """The label goes through BOTH redactors, not just the credential one:
        ``content`` did, and the two surfaces must carry one guarantee."""
        slot = _FakeSlot(running=True)
        label = "done https://evil.example.test/collect?d=" + ("dGhlIHF1aWNrIGJyb3du" * 4)
        await _apply(slot, {"label": label})
        slot.flush()
        row = slot.messages[0]
        assert "evil.example.test/collect" not in row["meta"]["label"]
        assert "evil.example.test/collect" not in row["content"]
        assert "[REDACTED" in row["meta"]["label"]

    @pytest.mark.asyncio
    async def test_the_immediate_path_scrubs_the_label_too(self):
        """An idle slot skips the hold and appends directly, which is a SECOND
        ``slot.append(..., meta=meta)`` call site. A fix applied only at the
        deferred one would leave this route broadcasting the raw label."""
        slot = _FakeSlot(running=False)
        await _apply(slot, {"label": "shipped AKIAIOSFODNN7EXAMPLE"})
        row = slot.messages[0]
        assert "AKIAIOSFODNN7EXAMPLE" not in row["meta"]["label"]
        assert "AKIAIOSFODNN7EXAMPLE" not in row["content"]

    @pytest.mark.asyncio
    async def test_a_credential_split_by_the_length_cap_is_still_scrubbed(self):
        """Redaction is ordered BEFORE the cap. Truncating first can cut a key
        mid-pattern so the regex no longer matches it, leaving a partial secret in
        both surfaces — a check that runs after the cut shares the cut's blind
        spot. The key here straddles the 120-char boundary."""
        slot = _FakeSlot(running=True)
        cap = _max_section_label()
        pad = "x" * (cap - 8)
        await _apply(slot, {"label": f"{pad} AKIAIOSFODNN7EXAMPLE"})
        slot.flush()
        row = slot.messages[0]
        assert "AKIA" not in row["meta"]["label"]
        assert len(row["meta"]["label"]) <= cap

    @pytest.mark.asyncio
    async def test_an_ordinary_label_is_left_alone(self):
        """The negative control for the four above: a clean label must survive
        byte-for-byte, or the redaction could be passing by mangling everything."""
        slot = _FakeSlot(running=True)
        await _apply(slot, {"label": "review item-42 (part 2/3)"})
        slot.flush()
        row = slot.messages[0]
        assert row["meta"]["label"] == "review item-42 (part 2/3)"
        assert row["content"] == "— End of: review item-42 (part 2/3) —"

    def test_the_cap_is_read_off_the_schema_not_re_spelled(self, monkeypatch):
        """Two spellings of one limit diverge silently — neither number is invalid
        on its own, so nothing fails at the moment they stop matching. Patching the
        schema must MOVE the applier's cap; a re-spelled literal would ignore this
        and the assertion below would keep reading the old value."""
        import dataclasses

        from kiro_crew import validation

        declared = next(
            f.max_len for f in validation.SECTION_MARKER_SCHEMA.fields if f.name == "label"
        )
        assert _max_section_label() == declared

        patched = dataclasses.replace(
            validation.SECTION_MARKER_SCHEMA,
            fields=[
                dataclasses.replace(f, max_len=17) if f.name == "label" else f
                for f in validation.SECTION_MARKER_SCHEMA.fields
            ],
        )
        monkeypatch.setattr(validation, "SECTION_MARKER_SCHEMA", patched)
        assert _max_section_label() == 17

    @pytest.mark.asyncio
    async def test_the_applier_truncates_at_the_schema_cap(self, monkeypatch):
        """The behavioural half of the above: reading the cap correctly is only
        worth anything if the truncation actually uses it."""
        import dataclasses

        from kiro_crew import validation

        patched = dataclasses.replace(
            validation.SECTION_MARKER_SCHEMA,
            fields=[
                dataclasses.replace(f, max_len=17) if f.name == "label" else f
                for f in validation.SECTION_MARKER_SCHEMA.fields
            ],
        )
        monkeypatch.setattr(validation, "SECTION_MARKER_SCHEMA", patched)

        slot = _FakeSlot(running=True)
        await _apply(slot, {"label": "y" * 500})
        slot.flush()
        assert len(slot.messages[0]["meta"]["label"]) == 17

    @pytest.mark.asyncio
    async def test_no_model_context_is_ever_queued(self):
        """The reason ``/note`` itself is not reused: it ALWAYS writes a
        ``_pending_context`` entry drained into the next user message, and a
        chapter break must never enter a model's prompt. ``_FakeSlot`` raises if
        anything tries."""
        slot = _FakeSlot()
        await _apply(slot)
        assert slot._deferred_notes[0]["context"] is None
        slot.flush()  # would raise via append_pending_context

    @pytest.mark.asyncio
    async def test_an_idle_slot_gets_the_row_immediately(self):
        """No turn in flight means no positional tail to steal, so holding it
        would delay the row for no reason."""
        slot = _FakeSlot(running=False)
        result = await _apply(slot)
        assert slot._deferred_notes == []
        assert [m["role"] for m in slot.messages] == [SECTION_MARKER_ROLE]
        assert "drawn" in result

    @pytest.mark.asyncio
    async def test_a_stage_execution_is_refused_not_held(self):
        """REPLACES an earlier test that asserted the row was HELD here. That
        pinned the defect rather than the contract: ``running`` is false between a
        plan's stages while the tail is still owned, so ``/note``'s both-flags gate
        made the marker defer — but the per-cycle flush is skipped for the whole
        duration of ``_in_stage_execution``, so the row would not appear at this
        stage's boundary at all. Refusing is what makes the tool's promise true.
        """
        slot = _FakeSlot(running=False)
        slot._in_stage_execution = True
        result = await _apply(slot)
        assert "in_stage_execution" in result
        assert slot.messages == []
        assert slot._deferred_notes == []

    @pytest.mark.asyncio
    async def test_the_eleventh_marker_in_one_turn_is_refused(self):
        """Exit criterion 5. Shares ``/note``'s ``_MAX_DEFERRED_NOTES`` hold, so a
        caller cannot park unbounded rows on one turn. The directive path has no
        HTTP response to carry a 429, so the refusal names the same
        ``deferred_notes_full`` condition the endpoint's 429 body does."""
        from kiro_crew.dashboard.chat_handlers import _MAX_DEFERRED_NOTES

        slot = _FakeSlot()
        for i in range(_MAX_DEFERRED_NOTES):
            assert "queued" in await _apply(slot, {"label": f"n{i}"})

        result = await _apply(slot, {"label": "one too many"})
        assert result.startswith("Error:")
        assert "deferred_notes_full" in result
        assert len(slot._deferred_notes) == _MAX_DEFERRED_NOTES

    @pytest.mark.asyncio
    async def test_slotless_caller_refused(self):
        """A channel transport's TurnDriver holds no slot, and a marker IS a
        rendering — a messaging channel has no transcript surface to draw on."""
        result = await apply_session_directive(
            None, None, "slack:C123:456", "section_marker", {"label": "x"}
        )
        assert result.startswith("Error:")

    @pytest.mark.asyncio
    async def test_a_label_over_the_cap_is_truncated_at_the_applier(self):
        """Belt to the schema's braces. The applier is reachable from the channel
        consumer as well as the MCP tool, so it must not trust that a validator
        ran upstream."""
        slot = _FakeSlot()
        await _apply(slot, {"label": "y" * 500})
        slot.flush()
        assert len(slot.messages[0]["meta"]["label"]) == 120


# ──────────────── provenance: a tab is not an entitlement ────────────────────


class TestHeadlessProducersAreRefused:
    """``section_marker`` sits in ``_DASHBOARD_ONLY_DIRECTIVES``, whose gate asks
    only whether an open tab exists. That is not the same question as whether a
    HUMAN asked, and the two come apart for exactly the producers the tool
    promises to refuse: a cron turn can run on a user's slot and a sub-agent can
    share its parent's slot, so both inherit a tab they did not open.
    """

    def test_the_surface_gate_cannot_tell_a_headless_turn_apart(self):
        """The premise, pinned so the tests below cannot be misread as belt on
        braces. ``has_dashboard_surface`` is true for ANY ``dashboard:``-prefixed
        key, so the surface gate admits a cron turn riding such a slot — which is
        why provenance has to be checked separately.
        """
        assert has_dashboard_surface("dashboard:ridden-by-cron") is True

    @pytest.mark.asyncio
    async def test_a_headless_producer_is_refused_with_a_tab_open(self):
        """The case that matters, and the one a tabless test cannot reach: the
        session HAS a dashboard surface (asserted above), so the dashboard-only
        gate admits it, and the refusal can only come from provenance. A test
        using a tabless headless caller would pass with the defect still live,
        because the surface gate refuses that one on its own.
        """
        slot = _FakeSlot(key="dashboard:ridden-by-cron")
        result = await _apply(slot, user_origin=False)
        assert "headless" in result
        assert "Nothing was changed" in result
        # Neither written now nor parked for a later flush.
        assert slot.messages == []
        assert slot._deferred_notes == []

    @pytest.mark.asyncio
    async def test_a_human_turn_on_the_same_slot_is_admitted(self):
        """Negative control for the test above: the refusal must be caused by the
        producer's provenance, not by anything else about this slot or key.
        """
        slot = _FakeSlot(key="dashboard:ridden-by-cron")
        result = await _apply(slot, user_origin=True)
        assert "queued" in result
        assert len(slot._deferred_notes) == 1

    @pytest.mark.asyncio
    async def test_the_sibling_directives_refusal_text_is_unchanged(self):
        """``section_marker`` joining this gate must not reword an unrelated tool's
        refusal. ``set_project`` and ``reset_conversation`` share the gate but not
        this change's scope, so they keep the wording they had before.
        """
        for kind in ("set_project", "reset_conversation"):
            slot = _FakeSlot(key="dashboard:ridden-by-cron")
            result = await apply_session_directive(
                None,
                slot,
                slot.key,
                kind,
                {"path": "/tmp"},
                producer_is_user_facing=False,
            )
            assert "(dashboard or a messaging channel)" in result
            # The section_marker-specific clause must NOT leak onto these two.
            assert "taskrunner turns" not in result
            assert "open dashboard tab" not in result

    @pytest.mark.asyncio
    async def test_section_marker_keeps_its_own_wording(self):
        """The other side of the split: the marker's refusal is the counter-intuitive
        one (the tab IS open), so it says so.
        """
        slot = _FakeSlot(key="dashboard:ridden-by-cron")
        result = await _apply(slot, user_origin=False)
        assert "open dashboard tab" in result
        assert "taskrunner turns" in result

    @pytest.mark.asyncio
    async def test_a_provenance_refusal_reaches_the_operator_log(self, caplog):
        """The refusal must leave a gateway-log trace, not only a SEL audit event.

        This gate turns a working cron or sub-agent follow-up card into a no-op.
        The refusal text goes to the MODEL and the audit goes to the security
        stream, so an operator asking why their cards stopped appearing has
        nothing in the log they actually read. The sibling marker gate in the
        messaging driver writes such a line for exactly this reason.
        """
        slot = _FakeSlot(key="dashboard:ridden-by-cron")
        with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.session_directive_apply"):
            result = await apply_session_directive(
                None,
                slot,
                slot.key,
                "suggest_followup",
                {"items": []},
                producer_is_user_facing=False,
            )
        assert "Error:" in result
        refused = [r for r in caplog.records if "REFUSED" in r.getMessage()]
        assert refused, "the provenance refusal left no WARNING in the operator log"
        assert "suggest_followup" in refused[0].getMessage()

    @pytest.mark.asyncio
    async def test_an_admitted_call_logs_no_refusal(self, caplog):
        """Negative control: the warning must fire on the refusal path only."""
        slot = _FakeSlot()
        with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.session_directive_apply"):
            await _apply(slot, user_origin=True)
        assert [r for r in caplog.records if "REFUSED" in r.getMessage()] == []

    @pytest.mark.asyncio
    async def test_every_gated_directive_leaves_a_refusal_record(self, caplog):
        """All four gated directives get the same operator signal, not just the new two.

        A scoping conditional here bought one extra branch and left the two older
        directives with the SEL audit as their only record — the same
        operator-cannot-see-why harm the log exists to remove.
        """
        slot = _FakeSlot(key="dashboard:ridden-by-cron")
        for kind in ("set_project", "reset_conversation"):
            caplog.clear()
            with caplog.at_level(
                logging.WARNING, logger="kiro_crew.dashboard.session_directive_apply"
            ):
                result = await apply_session_directive(
                    None,
                    slot,
                    slot.key,
                    kind,
                    {"path": "/tmp"} if kind == "set_project" else {},
                    producer_is_user_facing=False,
                )
            assert "Error:" in result, f"{kind} must still refuse"
            assert [
                r for r in caplog.records if "REFUSED" in r.getMessage()
            ], f"{kind} must leave the operator the same refusal record as its siblings"


# ─────────────── multi-stage plans: refused, not deferred ────────────────────


class TestMarkersAreRefusedDuringStageExecution:
    """The hold a marker rides is drained by a per-cycle flush that is skipped
    while ``_in_stage_execution`` is set, and that flag spans the WHOLE plan
    rather than one stage. So deferring inside a plan turns a turn-boundary hold
    into an end-of-plan hold, which is the opposite of what the tool describes.
    """

    @pytest.mark.asyncio
    async def test_a_marker_inside_a_plan_is_refused(self):
        slot = _FakeSlot()
        slot._in_stage_execution = True
        result = await _apply(slot)
        assert "in_stage_execution" in result
        assert "Nothing was changed" in result
        # Crucially NOT parked: parking is the defect, since the hold would not
        # drain until the plan exited.
        assert slot._deferred_notes == []
        assert slot.messages == []

    @pytest.mark.asyncio
    async def test_the_same_slot_outside_a_plan_still_queues(self):
        """Negative control: the refusal is caused by the stage flag alone."""
        slot = _FakeSlot()
        slot._in_stage_execution = False
        result = await _apply(slot)
        assert "queued" in result
        assert len(slot._deferred_notes) == 1


# ─────────────────────── import placement (AUTOSDE) ──────────────────────────


class TestSchemaImportPlacement:
    def test_the_validation_import_is_at_module_scope(self):
        """AUTOSDE ``top-level-imports``: the statement belongs at the top of the
        file, and none of the rule's three exceptions apply here (no
        ``TYPE_CHECKING`` — the symbol is read at runtime; no cycle — importing
        ``validation`` pulls in no ``kiro_crew.dashboard`` module; no optional
        dependency).

        It must be the MODULE, not the symbol: the cap has to resolve at CALL
        time so a patched schema is observed, which the two tests above pin. A
        ``from kiro_crew.validation import SECTION_MARKER_SCHEMA`` would satisfy
        the rule and silently freeze that binding, so this asserts the absence of
        that form too.
        """
        src = (_REPO_ROOT / "src/kiro_crew/dashboard/session_directive_apply.py").read_text(
            encoding="utf-8"
        )
        assert "\nfrom kiro_crew import validation\n" in src
        # Scoped to CODE lines. A plain substring test also matches the module
        # docstring's own warning against the from-import form, so it would fail
        # on the very text telling a later editor not to reintroduce it.
        offending = [
            line
            for line in src.splitlines()
            if line.lstrip().startswith("from kiro_crew.validation import")
        ]
        assert offending == []

    def test_the_security_redactors_are_at_module_scope(self):
        """AUTOSDE ``top-level-imports``. Measured: ``kiro_crew.security`` imports
        cleanly whether it or this module is the entry point, so none of the rule's
        three exceptions applies and the import belongs at the top.
        """
        src = (_REPO_ROOT / "src/kiro_crew/dashboard/session_directive_apply.py").read_text(
            encoding="utf-8"
        )
        assert "\nfrom kiro_crew.security import redact_credentials" in src
        # Named exactly, not prefix-matched: this module also carries UNRELATED
        # function-local security imports whose placement is not this test's claim.
        indented = [
            line
            for line in src.splitlines()
            if line.startswith("    from kiro_crew.security import")
            and ("redact_credentials" in line or "redact_exfiltration_urls" in line)
        ]
        assert indented == []

    def test_the_chat_handlers_import_carries_a_circular_annotation(self):
        """The rule exempts a genuine circular import only when a comment explains
        it. ``chat_handlers`` qualifies and the comment is required: measured, that
        module raises ImportError from a partially initialised ``kiro_crew.artifacts``
        when imported as the entry point, so it cannot be hoisted.

        Asserted structurally — the annotation must sit in the lines immediately
        above the import, so a later edit that hoists or moves the import without
        the rationale fails here rather than silently losing the exemption.
        """
        lines = (
            (_REPO_ROOT / "src/kiro_crew/dashboard/session_directive_apply.py")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        idx = [
            i
            for i, line in enumerate(lines)
            if "from kiro_crew.dashboard.chat_handlers import _MAX_DEFERRED_NOTES" in line
        ]
        assert len(idx) == 1, idx
        preceding = "\n".join(lines[max(0, idx[0] - 12) : idx[0]])
        assert "circular import" in preceding


# ─────────────────────── role-set memberships ────────────────────────────────


class TestSectionMarkerRoleSets:
    """Each of these is a note against a well-meaning later edit: all three sets
    exclude the role by default today, and none should gain it."""

    def test_never_replayed_to_a_model(self):
        """Exit criterion 4's constant half — a chapter break must not enter a
        prompt. The behavioural half is
        ``test_recall_and_replay_skip_section_markers`` below."""
        from kiro_crew.context import RECALL_ROLES

        assert SECTION_MARKER_ROLE not in RECALL_ROLES

    def test_does_not_retire_a_pending_question_card(self):
        from kiro_crew.dashboard.state import _QUESTION_RETIRING_ROLES

        assert SECTION_MARKER_ROLE not in _QUESTION_RETIRING_ROLES

    def test_does_not_rank_as_an_inbound_prompt(self):
        """Otherwise a session would look freshly asked-of in the sidebar every
        time a break was drawn."""
        from kiro_crew.dashboard.state import _PROMPT_ROLES

        assert SECTION_MARKER_ROLE not in _PROMPT_ROLES

    def test_is_persisted_rather_than_transient(self):
        """Exit criterion 2's durability half: the role must not be in the set the
        save path drops, or the row would never reach disk."""
        from kiro_crew.dashboard.state import _TRANSIENT_ROLES

        assert SECTION_MARKER_ROLE not in _TRANSIENT_ROLES


# ──────────────────── the REAL flush, not a stand-in ─────────────────────────


class TestRealFlushWritesTheMarkerRow:
    """``_FakeSlot.flush`` above reproduces the flush contract, which means it
    shares a blind spot with the code it stands in for: if the real
    ``flush_deferred_notes`` dropped ``role`` or ``meta``, every applier test
    would still pass. These exercise the real ``SlotBuffers.flush_deferred_notes``
    on a real ``_ChatSlot``, so the row shape is measured rather than assumed.
    """

    def _slot(self):
        from kiro_crew.dashboard.state import _ChatSlot

        return _ChatSlot("section-marker-flush")

    @staticmethod
    def _held(slot, **over):
        """A held entry authorized against the slot's LIVE session.

        The flush drops a held row whose recorded session no longer matches, so an
        entry stamped with anything else is dropped before the write and every
        assertion below would pass vacuously on an empty transcript.
        """
        from kiro_crew.dashboard.chat_utils import effective_session_key

        entry = {
            "content": "— End of: item-42 —",
            "cls": "",
            "context": None,
            "role": SECTION_MARKER_ROLE,
            "meta": {"label": "item-42"},
            "session": effective_session_key(slot),
        }
        entry.update(over)
        return entry

    def test_a_held_marker_is_written_with_its_role_and_label(self):
        slot = self._slot()
        slot._deferred_notes.append(self._held(slot))
        assert slot.flush_deferred_notes(markers_only=False) == 1
        row = slot.messages[-1]
        assert row["role"] == SECTION_MARKER_ROLE
        assert row["content"] == "— End of: item-42 —"
        assert row["meta"]["label"] == "item-42"
        # Drained, so a second seam cannot re-write it.
        assert slot._deferred_notes == []
        assert slot.flush_deferred_notes(markers_only=False) == 0

    def test_a_marker_row_is_not_stamped_as_a_note(self):
        """``meta.noteSession`` is the surviving half of the /note wire contract:
        the frontend's ``isNoteRow`` returns true for ANY row carrying it, because
        ``cls`` does not survive the write path for a non-system role. Stamping it
        on a marker would make that predicate call the marker a note."""
        slot = self._slot()
        slot._deferred_notes.append(self._held(slot, content="— End of section —"))
        assert slot.flush_deferred_notes(markers_only=False) == 1
        assert "noteSession" not in slot.messages[-1]["meta"]

    def test_a_note_entry_keeps_its_prior_shape(self):
        """The negative control for the two above: the role/meta passthrough must
        not have changed what a /note entry — which supplies neither key — writes.
        A note is still an ``inject`` carrying ``reconcile-note`` and its
        ``noteSession`` stamp."""
        slot = self._slot()
        entry = self._held(slot, content="a note", cls="reconcile-note")
        del entry["role"]
        del entry["meta"]
        slot._deferred_notes.append(entry)
        assert slot.flush_deferred_notes(markers_only=False) == 1
        row = slot.messages[-1]
        assert row["role"] == "inject"
        assert row["cls"] == "reconcile-note"
        assert "noteSession" in row["meta"]


class TestTheFlushModeIsRequiredNotDefaulted:
    """A seam that says nothing must fail to CALL, not inherit flush-everything.

    The hold carries two element classes with opposite release policies and neither
    wrong choice fails visibly, so the guard has to be the signature: documentation and
    per-seam tests cannot reach a seam nobody has written yet.
    """

    def test_omitting_the_mode_is_a_type_error_on_both_layers(self):
        import inspect

        from kiro_crew.dashboard.slot_buffers import SlotBufferCoordinator
        from kiro_crew.dashboard.state import _ChatSlot

        for owner, func in (
            ("_ChatSlot", _ChatSlot.flush_deferred_notes),
            ("SlotBufferCoordinator", SlotBufferCoordinator.flush_deferred_notes),
        ):
            param = inspect.signature(func).parameters["markers_only"]
            assert param.kind is inspect.Parameter.KEYWORD_ONLY, f"{owner}: not keyword-only"
            assert param.default is inspect.Parameter.empty, (
                f"{owner}: markers_only has default {param.default!r}; a seam that says "
                f"nothing would silently inherit it"
            )

    def test_every_production_seam_states_its_class(self):
        """Read off the tree, so a NEW seam added without a mode fails here too."""
        import pathlib

        from kiro_crew import dashboard

        root = pathlib.Path(dashboard.__file__).parent
        bare: list[str] = []
        for path in sorted(root.glob("*.py")):
            for n, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
                code = line.split("#", 1)[0]
                if ".flush_deferred_notes()" in code:
                    bare.append(f"{path.name}:{n}")
        assert not bare, f"seam(s) calling the flush with no mode: {bare}"


class TestMarkerIsAttributedToTheTurnNotTheSlot:
    """A slot's ``linked_session_key`` is MUTABLE: a cron or workflow result can
    bind an unbound slot part-way through a turn. ``flush_deferred_notes``
    defends against that by comparing the held entry's recorded session against
    the slot's live one — so the recorded value must be the CALLER's key for this
    turn. Re-deriving it from the slot puts the post-rebind key on both sides of
    that comparison, which can then never disagree, and the old turn's marker is
    written into the transcript the slot now points at.

    These drive the real applier into the real ``flush_deferred_notes``, because
    the defect lives in the relationship between the two rather than in either.
    """

    TURN = "dashboard:the-turns-own-session"

    class _UnfinishedTask:
        """Minimal stand-in for a turn in flight. ``_ChatSlot.running`` is a
        read-only property (``task is not None and not task.done()``), so this is
        what makes the slot read as running.
        """

        def done(self) -> bool:
            return False

    def _slot(self):
        from kiro_crew.dashboard.state import _ChatSlot

        slot = _ChatSlot("section-marker-rebind")
        # Deferral via an IN-FLIGHT TURN, which is the ordinary route: the applier
        # holds the row while ``running``. Deliberately NOT via
        # ``_in_stage_execution`` — a marker inside multi-stage plan execution is
        # refused outright (the plan's hold is not flushed per stage), so using
        # that flag here would refuse the directive and leave these attribution
        # assertions testing nothing.
        slot.task = self._UnfinishedTask()
        assert slot.running is True
        return slot

    @pytest.mark.asyncio
    async def test_a_marker_is_not_written_into_a_slot_rebound_mid_turn(self):
        slot = self._slot()
        # The cron result lands BEFORE the directive is applied, so a
        # slot-derived key would already read the cron's session.
        slot.linked_session_key = "cron-nightly-digest"
        await _apply(slot, {"label": "item-42"}, session_key=self.TURN)

        assert slot._deferred_notes[0]["session"] == self.TURN
        # Dropped rather than misfiled: this turn's chapter break does not belong
        # in the cron's transcript.
        assert slot.flush_deferred_notes(markers_only=False) == 0
        assert [m for m in slot.messages if m.get("role") == SECTION_MARKER_ROLE] == []

    @pytest.mark.asyncio
    async def test_an_unrebound_slot_still_gets_its_marker(self):
        """The positive control for the drop above. Without this, the assertion
        that nothing was written would pass just as well if the applier had
        queued nothing at all, or if the flush wrote no marker under any
        circumstances."""
        slot = self._slot()
        slot.linked_session_key = self.TURN
        await _apply(slot, {"label": "item-42"}, session_key=self.TURN)

        assert slot.flush_deferred_notes(markers_only=False) == 1
        row = slot.messages[-1]
        assert row["role"] == SECTION_MARKER_ROLE
        assert row["meta"]["label"] == "item-42"


def test_recall_and_replay_skip_section_markers(tmp_path):
    """Exit criterion 4, behaviourally: neither ``build_session_replay`` nor
    ``_recall_rows`` returns a marker's content for a session containing them.

    The surrounding user/assistant rows are the positive control — a replay that
    returned nothing at all would pass a bare absence assertion.
    """
    from kiro_crew.context import _recall_rows, build_session_replay
    from kiro_crew.history import ConversationLog

    log = ConversationLog(base_dir=tmp_path)
    log.append("k", "user", "review item one")
    log.append("k", "assistant", "item one done")
    log.append("k", SECTION_MARKER_ROLE, "— End of: item-one —")
    log.append("k", "user", "now item two")

    replay = build_session_replay(log, "k")
    assert replay is not None
    assert "review item one" in replay  # positive control
    assert "item one done" in replay  # positive control
    assert "item-one" not in replay
    assert SECTION_MARKER_ROLE not in replay

    rows = _recall_rows(log, "k", conv_max=50)
    assert any("review item one" in str(r) for r in rows)  # positive control
    assert not any(SECTION_MARKER_ROLE in str(r) for r in rows)


class TestARefusalReachesTheModel:
    """The tool's hedged confirmation is replaced by the applier's real outcome.

    Both consumer paths must fold the applier's return into the tool result, or a
    refusal is invisible and the assistant reports a break the user cannot see. The
    tool's own comment asserts this, so it is pinned rather than left as prose.
    """

    def test_both_consumer_paths_substitute_the_applier_outcome(self):
        runner = (_REPO_ROOT / "src/kiro_crew/dashboard/chat_runner.py").read_text(encoding="utf-8")
        # Marker path and out-of-band path each assign the applier's result into the
        # per-tool-call output map that becomes the model-visible tool result.
        assert (
            runner.count("_dir_consumed_out[event.tool_call_id] = _out") >= 2
        ), "a consumer path stopped surfacing the applier outcome"
        assert (
            'session_directive.strip_marker(_out) + "\\n\\n" + _applied_one' in runner
        ), "the out-of-band path no longer appends the applier outcome"

    def test_the_tool_comment_does_not_claim_refusals_are_invisible(self):
        control_src = (_REPO_ROOT / "src/kiro_crew/mcp_tools/control.py").read_text(
            encoding="utf-8"
        )
        assert "none of that refusal" not in control_src, (
            "the comment asserts refusals never reach the model, which both consumer "
            "paths contradict"
        )


class TestTheGatewayBoundaryStaysStubbed:
    """The autouse fixture is the isolation, so removing it must fail a test."""

    def test_no_test_reaches_the_real_post(self):
        from kiro_crew import mcp_core as live
        from kiro_crew.mcp_tools import control

        # A Mock here proves the fixture is active; the real function means a
        # `_call_tool_inner` in this module would write to a running gateway.
        assert control.mcp_core._post is not live.__dict__.get("_post") or hasattr(
            control.mcp_core._post, "assert_called"
        ), "the autouse gateway stub is not installed; directive tests would hit a live gateway"


class TestDeliveryIsProviderNeutral:
    """A marker must reach the control plane on a backend that emits no
    ``_meta.kiro`` identity, not only on kiro-cli.

    The marker in the tool result is the kiro-cli path: the consumer honours it
    only after verifying that identity, so a backend which never sends one drops
    the directive silently. ``control._emit_directive`` is the provider-neutral
    path added alongside it — it returns the same marker AND parks the payload
    out of band, keyed on the session the gateway verifies. A directive that
    calls ``session_directive.encode`` directly therefore works on exactly one
    backend, and the failure is invisible: the model is told the break was
    requested either way.
    """

    def test_a_marker_is_published_out_of_band(self):
        from unittest.mock import patch

        from kiro_crew.mcp_tools import control

        posted: list[tuple] = []
        with patch.object(
            control.mcp_core, "_post", side_effect=lambda p, b: posted.append((p, b))
        ):
            out = control.section_marker("section_marker", {"label": "Phase two"})

        assert posted == [
            (
                "/api/session-directive",
                {"kind": "section_marker", "args": {"label": "Phase two"}},
            )
        ]
        assert session_directive.has_marker(out)  # the kiro-cli path still works

    def test_every_directive_tool_publishes_out_of_band(self):
        """The durable guard: the next directive added here cannot regress to the
        kiro-cli-only path without failing this test."""
        import inspect

        from kiro_crew.mcp_tools import control

        checked = set()
        for kind in session_directive.DIRECTIVE_TOOLS:
            fn = control.HANDLERS.get(kind)
            if fn is None:
                continue
            checked.add(kind)
            src = inspect.getsource(fn)
            assert "_emit_directive(" in src, f"{kind} does not publish out of band"
            assert (
                "session_directive.encode(" not in src
            ), f"{kind} still encodes directly, so it reaches only kiro-cli"

        # Positive control: the loop must actually have inspected every directive,
        # or the assertions above are vacuous.
        assert checked == set(session_directive.DIRECTIVE_TOOLS)
        assert len(checked) >= 8

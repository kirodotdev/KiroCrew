"""Tests for the auto-nudge egress scrub and its log hygiene.

The model receives ``message`` whole on every cycle and it is never rewritten.
Everything a READER sees is an egress surface instead, so the scrub belongs at
the SINKS. This suite pins three properties:

1. Every text field is scrubbed unless it is named -- a denylist, not an
   allowlist, so a field added later is covered rather than silently missed.
   The addressing fields are the deliberate exemption, because a rewritten
   ``id`` or ``slot_key`` would leave a row the client cannot act on.
2. A projection that cannot scrub refuses BEFORE it mutates, so a host that
   cannot compose a credential policy costs a 503 instead of a 500 on an
   already-committed change.
3. No log sink here carries a store value raw, nor re-exposes one via a traceback.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew import autonudge as _an
from kiro_crew import autonudge_authz as authz
from kiro_crew.autonudge import AutoNudgeService, NudgeLoop
from kiro_crew.dashboard.handlers import autonudge as autonudge_handlers
from kiro_crew.monitoring.models import MonitorState
from kiro_crew.security import redact

SECRET = "AKIAIOSFODNN7EXAMPLE"


def _install_broken() -> None:
    import dataclasses

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.platform import build_default_context, set_context

    class _Broken:
        def redact(self, text: str) -> str:
            from kiro_crew.platform import PlatformCompositionError

            raise PlatformCompositionError("companion credential policy unreadable")

    base = build_default_context(KiroCrewConfig())
    set_context(dataclasses.replace(base, credentials=_Broken()))


def _write_store(tmp_path, rows) -> None:
    (tmp_path / "autonudge.json").write_text(
        json.dumps({"version": 1, "loops": rows}), encoding="utf-8"
    )


def _loop(**kw) -> NudgeLoop:
    base = dict(
        id="loop-abc",
        slot_key="chat-1-1785",
        message="the full multi-paragraph babysit instruction",
        idle_secs=300,
        max_cycles=24,
        cycle_count=3,
    )
    base.update(kw)
    return NudgeLoop(**base)  # type: ignore[arg-type]


class TestTheListSerializerScrubsLoopText:
    """``GET /api/autonudge`` was the third sink, and it served ``message`` raw.

    ``_load`` repairs the store and the transcript row scrubs at the sink, but the
    REST serializer was a bare ``asdict``. Several producers reach ``svc.add``
    without the authorizer -- among them one whose message is composed from
    external issue text -- so this is reachable, not theoretical.
    """

    def test_a_credential_in_message_does_not_reach_the_client(self) -> None:
        secret = "AKIAIOSFODNN7EXAMPLE"
        out = autonudge_handlers._serialize(_loop(message=f"do the thing {secret}"))
        assert secret not in out["message"], "the REST surface serves an unredacted credential"
        assert "REDACTED" in out["message"]

    def test_a_clean_loop_round_trips_unchanged(self) -> None:
        """The scrub must not rewrite ordinary text.

        Its own arm because a scrub that replaced every value with a placeholder
        would satisfy the arm above while destroying the surface.
        """
        loop = _loop(message="run the next cycle")
        out = autonudge_handlers._serialize(loop)
        assert out["message"] == "run the next cycle"

    def test_the_addressing_fields_are_never_rewritten(self) -> None:
        """``id`` and ``slot_key`` must survive verbatim or the client cannot act.

        A rewritten ``id`` would break ``PATCH``/``DELETE`` targeting, turning a
        redaction into a functional regression.

        The values are CREDENTIAL-SHAPED on purpose. Benign ones leave the test
        green when the ``ADDRESSING_FIELDS`` exemption is deleted, because the scrub
        would not have rewritten them either -- so it asserted nothing about the
        exemption it exists to pin. These two are values the scrub DOES rewrite, so
        removing the exemption fails here instead of silently shipping ids no client
        can address.
        """
        secret = "AKIAIOSFODNN7EXAMPLE"
        loop = _loop(id=f"loop-{secret}", slot_key=f"chat-{secret}-1785")
        out = autonudge_handlers._serialize(loop)
        assert out["id"] == f"loop-{secret}"
        assert out["slot_key"] == f"chat-{secret}-1785"

    def test_a_credential_shaped_value_really_is_rewritten_elsewhere(self) -> None:
        """The control for the arm above: the scrub DOES rewrite that value.

        Without this, an exemption test using credential-shaped ids passes just as
        well when the scrub has stopped working altogether.
        """
        secret = "AKIAIOSFODNN7EXAMPLE"
        out = autonudge_handlers._serialize(_loop(message=f"deploy {secret}"))
        assert secret not in out["message"]

    def test_a_field_the_scrub_does_not_name_is_still_covered(self) -> None:
        """The denylist shape, pinned.

        ``stopped_reason`` is agent-supplied free text (``autonudge_stop(reason=)``)
        and is named nowhere in the scrub. An allowlist would have missed it, which
        is exactly how a new free-text field comes to need a scrub of its own.
        """
        secret = "AKIAIOSFODNN7EXAMPLE"
        out = autonudge_handlers._serialize(_loop(stopped_reason=f"gave up {secret}"))
        assert secret not in out["stopped_reason"]


class TestScrubbedLogTextCannotForgeARecord:
    """The ``_load`` warnings returned control characters intact.

    The two redactors remove credential- and URL-shaped SUBSTRINGS, not control
    characters: a newline survives both, so a store-supplied id or key carrying
    one splits one ``%s`` warning into several records, and the forged tail is
    indistinguishable from a real line.

    The escape is supplied by ``repr`` -- ``redact(repr(value))``, the spelling the
    scheduler's identical malformed-entry warning and the session store already
    use. It replaced a hand-rolled ``str.isprintable`` comprehension; ``repr``
    escapes the same set, because CPython keys its own ``str`` escaping on
    ``str.isprintable``.

    Not a denylist on ``\\n``. Every non-printable character is escaped, so
    ``\\r``, ``\\x1b`` and U+2028/U+2029 cannot be substituted for it tomorrow.
    """

    # A tail that would read as a whole extra record if a newline survived.
    FORGED = "AutoNudge: all clear, nothing to see here"

    @staticmethod
    def _scrub(value: object) -> str:
        """The spelling the ``_load`` warnings use, applied here verbatim."""
        return redact(repr(value))

    def test_a_newline_is_escaped_not_returned_raw(self) -> None:
        out = self._scrub(f"loop-abc\n{self.FORGED}")
        assert "\n" not in out, f"a raw newline survived the sink: {out!r}"
        assert out.count("\\n") == 1, f"the newline was dropped rather than escaped: {out!r}"
        assert "loop-abc" in out, "the value was destroyed rather than escaped"
        assert self.FORGED in out, "the tail was dropped -- escape, do not truncate"

    @pytest.mark.parametrize(
        "raw,name",
        [
            ("\r", "carriage return"),
            ("\t", "tab"),
            ("\x1b", "ANSI escape"),
            ("\x0b", "vertical tab"),
            ("\x85", "NEL"),
            ("\u2028", "line separator"),
            ("\u2029", "paragraph separator"),
        ],
    )
    def test_every_control_character_is_escaped_not_just_newline(self, raw, name) -> None:
        """The denylist-on-newline fix would pass the arm above and fail here.

        Output encoding is transformed against what is ALLOWED (printable)
        rather than by enumerating what is forbidden. A denylist is always
        incomplete: escape only ``\\n`` and the next separator becomes the next
        bug.
        """
        out = self._scrub(f"loop-abc{raw}tail")
        assert raw not in out, f"a raw {name} survived the sink: {out!r}"
        assert out.isprintable(), f"the return is not a single printable line: {out!r}"

    def test_printable_text_survives_readably(self) -> None:
        """Negative control: the escape must not MANGLE ordinary values.

        ``repr`` quotes the value, which is the accepted cost of using the shared
        spelling rather than a second one -- so the assertion is that the text
        arrives intact and unescaped INSIDE the quotes, not that the return is
        byte-identical to the input. A fix that escaped indiscriminately would
        pass every arm above and fail here.
        """
        for value in ("loop-abc", "id with spaces", "banner, message", "café — ok"):
            out = self._scrub(value)
            assert value in out, f"a printable value was altered: {value!r} -> {out!r}"
            assert "\\" not in out, f"an ordinary value picked up an escape: {out!r}"

    def test_a_credential_is_still_redacted_after_the_escape(self) -> None:
        """Negative control: the escape must not displace the redaction."""
        out = self._scrub("loop-AKIAIOSFODNN7EXAMPLE\nx")
        assert "AKIAIOSFODNN7EXAMPLE" not in out, "the credential survived"
        assert "[REDACTED: credential]" in out, "the credential arm did not run"
        assert "\n" not in out, "a raw newline survived alongside the redaction"

    @pytest.mark.asyncio
    async def test_a_newline_bearing_id_cannot_forge_a_log_record(self, tmp_path, caplog) -> None:
        """End to end through the real ``_load`` warning, not just the helper.

        The unit arms pin the sink; this one pins that the sink is what the
        warning actually uses. ``slot_key`` is omitted so construction fails and
        the malformed-entry arm runs -- the arm that names the id.
        """
        from kiro_crew.autonudge import AutoNudgeService as _Svc

        (tmp_path / "autonudge.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "loops": [{"id": f"abc123\n{self.FORGED}", "idle_secs": 300}],
                }
            ),
            encoding="utf-8",
        )
        svc = _Svc(base_dir=tmp_path)
        try:
            with caplog.at_level("WARNING"):
                await svc.start()
            warnings = [r for r in caplog.records if "malformed loop entry" in r.getMessage()]
            assert warnings, "the malformed-entry arm did not run -- fixture is wrong"
            for rec in warnings:
                msg = rec.getMessage()
                assert "\n" not in msg, f"the record was split into several lines: {msg!r}"
                assert not msg.startswith(self.FORGED), "the forged tail became its own record"
        finally:
            svc.stop()


class TestNoMutationCommitsWhenTheResponseCannotBeSerialized:
    """The write landed and the caller was told it failed.

    The ordering was the defect. On a host whose credential policy cannot compose,
    a MESSAGELESS request scrubs nothing during authorization -- the message
    compare is gated on ``message is not None`` -- so nothing raises before the
    mutation. ``svc.add``/``svc.update`` COMMITS after the critical audit, and only
    then does the handler serialize the response and raise. Result: HTTP 500 with
    the mutation already persisted and audited as ``success``, so a retry would
    apply it twice.

    The fix probes the policy in BOTH authorizers before auditing or mutating, so
    an unusable policy is a clean audited 503 with nothing written. Pinned in both
    directions: refused-and-unwritten when the policy is broken, and completely
    unaffected when it works.
    """

    class _BrokenPolicy:
        """A host that declares a companion policy it cannot compose."""

        def redact(self, text: str) -> str:
            from kiro_crew.platform import PlatformCompositionError

            raise PlatformCompositionError("companion credential policy unreadable")

    @staticmethod
    def _install(policy) -> None:
        import dataclasses

        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.platform import build_default_context, set_context

        base = build_default_context(KiroCrewConfig())
        set_context(dataclasses.replace(base, credentials=policy))

    @pytest.fixture()
    def audits(self, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
        events: list[dict] = []
        monkeypatch.setattr(
            authz,
            "sel",
            lambda: SimpleNamespace(log_tool_invocation=lambda **kw: events.append(kw)),
        )
        return events

    def test_the_serializer_really_does_raise_under_this_policy(self) -> None:
        """CONTROL FIRST: without this the arms below could pass vacuously.

        If ``_serialize`` did not raise under the broken policy there would be no
        500-after-commit to prevent, and a 503 from the authorizers would prove
        nothing about the ordering.
        """
        from kiro_crew.platform import PlatformCompositionError

        self._install(self._BrokenPolicy())
        loop = NudgeLoop(id="l1", slot_key="chat-1-123", message="keep going")
        with pytest.raises(PlatformCompositionError):
            autonudge_handlers._serialize(loop)

    @pytest.mark.asyncio
    async def test_the_probe_runs_before_the_critical_invoked_audit(self, audits) -> None:
        """The refusal must precede the ``invoked`` audit, not follow it.

        An ``invoked`` event records an ATTEMPTED mutation. Emitting one and then
        refusing would leave the audit trail claiming a write that never happened --
        the mirror of the bug being fixed, where the write happened and the caller
        was told it had not.
        """
        svc = MagicMock()
        svc.list_all = lambda: []
        svc.get_by_id = lambda _id: None
        svc.get_by_id = lambda _id: SimpleNamespace(message="x")
        svc.update = AsyncMock(return_value=None)
        self._install(self._BrokenPolicy())

        await authz.authorize_and_update_nudge(
            svc=svc, loop_id="loop-1", idle_secs=600, source="dashboard"
        )
        assert not [
            a for a in audits if a.get("outcome") == "invoked"
        ], f"an invoked audit was written for a mutation that was refused: {audits!r}"

    @pytest.mark.asyncio
    async def test_a_working_policy_is_completely_unaffected(self, tmp_path, audits) -> None:
        """PRESERVED: the probe must not turn ordinary requests into refusals."""
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            armed = await svc.add(slot_key="chat-5-555", message="keep going")
            loop, error, status = await authz.authorize_and_update_nudge(
                svc=svc, loop_id=armed.id, idle_secs=600, source="dashboard"
            )
            assert status == 200, f"a healthy update was refused: {error}"
            assert loop.idle_secs == 600, "the update did not apply"
            # And the response really can be serialized, which is the property the
            # probe exists to guarantee.
            assert isinstance(autonudge_handlers._serialize(loop)["message"], str)
        finally:
            svc.stop()


class TestANumericStoredMessageIsCoercedNotServedRaw:
    """``message: 42`` in the store crashed the goal popover.

    The store is hand-editable JSON and ``NudgeLoop`` is a plain dataclass, so
    ``{"message": 42}`` becomes ``loop.message = 42`` -- ``_load`` repairs the
    numeric timer fields, but nothing coerces ``message``. The REST projection
    then served it untouched, because ``scrub_loop_text`` returned every
    ``int``/``float``/``bool`` early.

    The popover reads the field as ``message || DEFAULT_MSG``, and ``42`` is
    truthy, so the number reached ``.trim()`` and threw. (``0`` was survivable
    only by accident: it is falsy, so the default template took over.)

    The numeric pass-through is NOT simply wrong, which is why the fix is
    field-aware rather than a blanket ``str()``: the declared numeric fields are
    ones clients do arithmetic on, so coercing ``300`` would break the contract.
    """

    @staticmethod
    def _serialized(**overrides):
        loop = NudgeLoop(
            id="loop-num",
            slot_key="chat-1-123",
            message=overrides.pop("message", "watch the build"),
            **overrides,
        )
        return autonudge_handlers._serialize(loop)

    def test_a_numeric_message_is_served_as_a_string(self) -> None:
        """The bug: a number reached the wire, where the client calls ``.trim()``."""
        out = self._serialized(message=42)
        assert isinstance(out["message"], str), (
            f"a numeric message was served as {type(out['message']).__name__}, which "
            "crashes message.trim() in the popover"
        )
        assert out["message"] == "42", f"the value was not preserved: {out['message']!r}"

    def test_a_numeric_message_survives_the_loader_uncoerced(self, tmp_path) -> None:
        """Establishes the premise rather than assuming it: ``_load`` does not coerce."""
        (tmp_path / "autonudge.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "loops": [
                        {"id": "l1", "slot_key": "chat-1-123", "message": 42, "idle_secs": 300}
                    ],
                }
            ),
            encoding="utf-8",
        )
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            armed = svc.get_by_id("l1")
            assert armed is not None, "the row was refused, so the premise does not hold"
            assert armed.message == 42, (
                f"_load coerced the message to {armed.message!r}; if this ever becomes "
                "the fix, this test is the place to record it"
            )
            out = autonudge_handlers._serialize(armed)
            assert isinstance(out["message"], str), "the projection served a raw number"
        finally:
            svc.stop()

    @pytest.mark.parametrize(
        "field,value",
        [
            ("idle_secs", 300),
            ("max_cycles", 7),
            ("cycle_count", 3),
            ("max_runtime_secs", 900),
            ("active", True),
            ("approval_stalled", False),
            ("last_fire_ts", 1.5),
            ("created_ts", 2.5),
            ("next_due_ts", 3.5),
        ],
    )
    def test_every_declared_numeric_field_stays_numeric(self, field, value) -> None:
        """PRESERVED: the whole reason the early return existed.

        All nine declared numeric fields, named individually -- a fix that coerced
        any one of them to a string would break arithmetic and comparison on the
        client, which is what the docstring's ``300`` -> ``"300"`` warning is about.
        """
        out = self._serialized(**{field: value})
        assert out[field] == value and isinstance(out[field], type(value)), (
            f"{field} was coerced from {type(value).__name__} to "
            f"{type(out[field]).__name__}: {out[field]!r}"
        )

    def test_the_numeric_exemption_is_derived_and_finds_the_declared_fields(self) -> None:
        """The derivation must actually resolve, because empty FAILS OPEN into coercion.

        Replaces a set/dataclass drift test: there is now one definition, so drift is
        impossible. What IS possible is a derivation that silently resolves to nothing
        -- annotations are strings under ``from __future__ import annotations``, so a
        probe that stopped matching would exempt no field and coerce every number a
        client does arithmetic on.
        """
        derived = _an._numeric_loop_fields()
        assert derived, "the derivation resolved to nothing -- every number would coerce"
        assert {
            "idle_secs",
            "max_cycles",
            "cycle_count",
            "active",
        } <= derived, f"the derivation missed a known numeric field: {sorted(derived)}"
        assert (
            "message" not in derived and "id" not in derived
        ), f"a text field slipped into the exemption: {sorted(derived)}"

    def test_a_non_numeric_value_in_a_numeric_field_is_still_coerced(self) -> None:
        """A numeric FIELD does not license a non-numeric value onto the wire."""
        out = self._serialized(idle_secs="AKIAIOSFODNN7EXAMPLE")
        assert isinstance(out["idle_secs"], str)
        assert "AKIAIOSFODNN7EXAMPLE" not in out["idle_secs"], "the scrub was skipped"

    def test_none_is_not_stringified(self) -> None:
        """PRESERVED: ``None`` must not become the literal string ``"None"``.

        A blanket ``str()`` coercion would do exactly that -- corrupting an absent
        value into a four-character message -- so ``None`` keeps passing through.
        The popover's ``|| DEFAULT_MSG`` already handles it, since ``None`` is falsy.
        """
        out = self._serialized(message=None)
        assert out["message"] is None, f"None was stringified to {out['message']!r}"

    def test_a_string_message_still_scrubs(self) -> None:
        """PRESERVED: the ordinary path is untouched."""
        out = self._serialized(message="deploy with AKIAIOSFODNN7EXAMPLE now")
        assert isinstance(out["message"], str)
        assert "AKIAIOSFODNN7EXAMPLE" not in out["message"], "the scrub was lost"

    def test_an_empty_string_message_is_returned_as_is(self) -> None:
        """PRESERVED: the empty-string short-circuit the compare path depends on."""
        assert _an.scrub_loop_text("", field="message") == ""

    def test_the_broadcast_path_coerces_too(self) -> None:
        """The websocket sink shares the rule, so it must share the coercion.

        This arm covers the RULE only -- that passing ``field`` coerces a number to
        text. ``TestTheWebsocketBroadcastSharesTheProjection`` is what proves the
        broadcast actually calls it; asserting the rule here and claiming the sink
        would be a claim this test cannot fail on.
        """
        assert isinstance(_an.scrub_loop_text(42, field="message"), str)


class TestTheWebsocketBroadcastSharesTheProjection:
    """``autonudge_state`` is a peer egress of the REST reads, not a documented one.

    The posture registry names this broadcast as a redaction sink. That sentence was
    written while the observer still built its payload from raw attributes, so the
    registry asserted a scrub that did not exist -- and the surface it describes is
    the widest one here: every connected dashboard client, reached by a producer
    (issue-radar, composing from an external issue body) that never passes the
    arm-time authorizer.

    Pinned at the SOURCE because the payload is built inside a closure in the Slack
    gateway's service wiring, which a unit test cannot reach without standing up the
    gateway. A source assertion still fails for the intended reason: reinstating
    ``"message": loop.message`` puts the raw attribute back in the region.
    """

    def _observer_region(self) -> str:
        from pathlib import Path

        import kiro_crew.slack.gateway as gw

        src = Path(gw.__file__).read_text(encoding="utf-8")
        start = src.index("loop_payload: dict[str, Any]")
        end = src.index("autonudge_state", start)
        return src[start:end]

    def test_the_payload_is_built_through_the_shared_rule(self) -> None:
        assert (
            "_scrub_serialized_field" in self._observer_region()
        ), "the broadcast stopped routing through the shared projection"

    def test_no_raw_text_attribute_reaches_the_payload(self) -> None:
        region = self._observer_region()
        for raw in ('"message": loop.message', '"stopped_reason": loop.stopped_reason'):
            assert raw not in region, f"{raw} is back on the broadcast payload"

    def test_the_region_really_is_the_broadcast_payload(self) -> None:
        """Control: the located region is the payload, not an empty slice."""
        region = self._observer_region()
        assert "loop.message" in region, "located region does not build the payload"
        assert "slot_key" in region


class TestTheRedactedProjectionCannotOverwriteTheStoredMessage:
    """The popover's own Save destroyed the operator's prompt.

    The mechanism needs both halves of an asymmetry to line up:

    * ``svc.add`` stores a message WITHOUT the PATCH path's redaction pair -- the
      MCP arming tools and any direct service caller go in that way -- so the
      stored text keeps whatever it was armed with.
    * ``_serialize`` projects that field through ``scrub_loop_text``, a DIFFERENT
      and wider rule than the pair on the update authorizer's message arm.

    So the popover loads a projection that differs from the stored value and its
    Save PATCHes it straight back, silently replacing the operator's instruction.
    The remedy: a submitted message equal to the current scrubbed projection is
    treated as UNCHANGED, compared with the very same ``scrub_loop_text`` so the
    two cannot drift apart.
    """

    @pytest.fixture()
    def audits(self, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
        """Capture SEL events rather than writing them (mirrors the authz suite)."""
        events: list[dict] = []
        monkeypatch.setattr(
            authz,
            "sel",
            lambda: SimpleNamespace(log_tool_invocation=lambda **kw: events.append(kw)),
        )
        return events

    @pytest.fixture()
    def svc(self, tmp_path):
        service = AutoNudgeService(base_dir=tmp_path)
        yield service
        service.stop()

    async def _armed(self, svc):
        """Arm through ``svc.add``, the path that does NOT redact on the way in."""
        original = f"deploy using key {SECRET} and report back"
        loop = await svc.add(slot_key="chat-1-123", message=original, idle_secs=300)
        assert loop.message == original, "svc.add unexpectedly redacted on the way in"
        return original, loop.id

    @pytest.mark.asyncio
    async def test_a_genuinely_different_message_still_replaces_and_is_redacted(
        self, svc, audits
    ) -> None:
        """Preserved: a real edit still lands, and inbound redaction still applies."""
        _, loop_id = await self._armed(svc)

        loop, error, status = await authz.authorize_and_update_nudge(
            svc=svc,
            loop_id=loop_id,
            message=f"completely new instruction {SECRET}",
            source="test",
        )
        assert status == 200, f"a genuine edit was refused: {error}"
        assert loop.message.startswith("completely new instruction")
        assert SECRET not in loop.message, "inbound redaction was lost"

    @pytest.mark.asyncio
    async def test_a_submitted_empty_string_still_clears_as_it_does_today(
        self, svc, audits
    ) -> None:
        """Preserved: '' is not the projection of a non-empty message, so it applies."""
        _, loop_id = await self._armed(svc)

        loop, error, status = await authz.authorize_and_update_nudge(
            svc=svc, loop_id=loop_id, message="", source="test"
        )
        assert status == 200, f"the empty-string update was refused: {error}"
        assert loop.message == "", "'' stopped being applied"

    @pytest.mark.asyncio
    async def test_a_message_with_nothing_to_scrub_is_still_updatable(self, svc, audits) -> None:
        """Preserved: when projection == stored, re-saving it is a genuine no-op.

        A benign message projects to itself, so the new predicate treats a re-save as
        unchanged -- which is correct, because applying it would store the identical
        value. Pinned so the predicate cannot be read as breaking benign saves.
        """
        benign = await svc.add(slot_key="chat-2-456", message="just do it")
        loop, error, status = await authz.authorize_and_update_nudge(
            svc=svc, loop_id=benign.id, message="just do it", idle_secs=900, source="test"
        )
        assert status == 200, f"a benign re-save was refused: {error}"
        assert loop.message == "just do it"
        assert loop.idle_secs == 900

    @pytest.mark.asyncio
    async def test_an_unknown_loop_still_produces_the_existing_404(self, svc, audits) -> None:
        """Preserved: the pre-read must not invent a second 404 path."""
        loop, error, status = await authz.authorize_and_update_nudge(
            svc=svc, loop_id="no-such-loop", message="anything", source="test"
        )
        assert status == 404, f"the existing not-found path changed: {status} {error}"
        assert loop is None
        assert error == "loop not found"


class TestNonStringFieldsCannotBypassTheScrub:
    """``not isinstance(value, str)`` was an EARLY-OUT.

    ``_serialize`` skipped any non-string value, so an agent-written
    ``message: ["AKIA..."]`` rode straight through to ``GET /api/autonudge`` and
    the ``autonudge_state`` WS broadcast. Measured before the fix: the row loaded
    and the serialized payload carried the list verbatim.

    Two halves, because the right answer differs per field. ADDRESSING fields
    (``id``/``slot_key``) are exempt from scrubbing BY DESIGN, so a non-string
    there rides both the exemption and the early-out -- and a list ``id`` is
    unhashable, so indexing the store by it raises uncaught and NOTHING arms; those
    are REFUSED at load. Other ``str``-declared fields are REDACT-COERCED at the
    sink, which keeps the value inspectable where blanking would not. The declared
    int/float/bool fields still pass through UNTOUCHED -- coercing ``idle_secs``
    would break every client that compares it.
    """

    def _row(self, **over) -> dict:
        row = {"id": "n1", "slot_key": "chat-1-2", "message": "ok", "idle_secs": 300}
        row.update(over)
        return row

    @pytest.mark.asyncio
    async def test_a_non_string_message_is_not_served_raw(self, tmp_path) -> None:
        """Fails on the unmodified tree: the payload carries the list verbatim."""
        _write_store(tmp_path, [self._row(message=[SECRET])])
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            assert "n1" in svc._loops, "fixture wrong -- the row did not load"
            payload = json.dumps(autonudge_handlers._serialize(svc._loops["n1"]))
            assert SECRET not in payload, "a non-string message leaked the credential"
            assert "[REDACTED: credential]" in payload, "the value was not redact-coerced"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", ["banner", "stopped_reason", "stop_sentinel_path"])
    async def test_every_other_str_field_is_redact_coerced(self, tmp_path, field) -> None:
        """One arm per field: one break cannot validate the whole denylist."""
        _write_store(tmp_path, [self._row(**{field: [SECRET]})])
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            assert "n1" in svc._loops, f"the {field} row did not load, so nothing was scrubbed"
            payload = json.dumps(autonudge_handlers._serialize(svc._loops["n1"]))
            assert SECRET not in payload, f"a non-string {field} leaked the credential"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_the_numeric_and_bool_fields_keep_their_types(self, tmp_path) -> None:
        """Negative control, and the one that matters most.

        A fix that coerced every non-string would pass every arm above while
        turning ``idle_secs`` into ``"300"`` and ``active`` into ``"True"``,
        breaking every client that compares them. Pins all nine.
        """
        _write_store(tmp_path, [self._row()])
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            out = autonudge_handlers._serialize(svc._loops["n1"])
            for name, want in (
                ("idle_secs", int),
                ("max_cycles", int),
                ("cycle_count", int),
                ("max_runtime_secs", int),
                ("active", bool),
                ("approval_stalled", bool),
                ("last_fire_ts", float),
                ("created_ts", float),
                ("next_due_ts", float),
            ):
                assert isinstance(out[name], want), f"{name} became {type(out[name]).__name__}"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_clean_loop_round_trips_unchanged(self, tmp_path) -> None:
        """Negative control: the coercion must not rewrite ordinary values."""
        _write_store(tmp_path, [self._row(message="just keep going")])
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            out = autonudge_handlers._serialize(svc._loops["n1"])
            assert out["message"] == "just keep going"
            assert out["id"] == "n1" and out["slot_key"] == "chat-1-2"
        finally:
            svc.stop()


class TestOneScrubRuleServesEveryLoopReader:
    """Every projection of a loop must scrub the same fields as every other.

    ``_serialize`` runs each non-addressing field through ``scrub_loop_text``. A second
    assembly that skips it serves raw what the first scrubs, and ``stopped_reason`` is
    the field that exposes: agent-supplied free text (``autonudge_stop(reason=)``),
    neither withheld nor mapped out of the reduced row, on a list route with no owner
    gate.
    """

    def _structured(self, reason: str) -> NudgeLoop:
        loop = _loop(stopped_reason=reason)
        loop.monitor = MonitorState(
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            created_ts=1_000.0,
        )
        return loop

    def test_the_reduced_row_scrubs_every_field_the_plain_row_scrubs(self) -> None:
        reason = f"gave up after {SECRET}"
        loop = self._structured(reason)
        assert autonudge_handlers.is_structured_monitor_loop(
            loop
        ), "precondition: this must take the REDUCING arm, not _serialize"
        reduced = autonudge_handlers._serialize_for_legacy_reader(loop)
        assert "stopped_reason" in reduced, (
            "precondition: the field must be SERVED here, or its absence would pass "
            "this test vacuously"
        )
        plain = autonudge_handlers._serialize(_loop(stopped_reason=reason))
        assert reduced["stopped_reason"] == plain["stopped_reason"], (
            "the two arms disagree on what a loop's text projection is, so one of them "
            "serves what the other scrubs"
        )
        assert SECRET not in json.dumps(
            reduced
        ), "an agent-written stop reason reached the un-gated legacy list verbatim"

    def test_the_in_session_reading_scrubs_its_stop_reason_too(self) -> None:
        reading = autonudge_handlers._autonudge_loop_reading(
            _loop(stopped_reason=f"gave up after {SECRET}")
        )
        assert "stopped_reason" in reading, "precondition: the field must be served here"
        assert SECRET not in json.dumps(
            reading
        ), "the in-session reading assembles its own payload and skipped the scrub"


class TestAScrubFailureCannotDeleteAPersistedLoop:
    """The at-rest scrub runs inside the per-row arm whose job is to SKIP bad rows.

    That arm cannot tell "this row is malformed" from "the host's credential policy
    will not compose for ANY row" -- and the second condition is the one this change
    introduces, because ``_load`` now calls the raising context shim. Skipping a
    WELL-FORMED row is not a read-side inconvenience: a repair on any other row sets
    ``_store_dirty``, so ``start`` rewrites the store from the survivors and the
    skipped loop is gone with no recovery path.

    So the policy failure must propagate, leaving the store untouched, rather than be
    absorbed as a per-row verdict.
    """

    @pytest.mark.asyncio
    async def test_the_policy_failure_propagates_instead_of_skipping_the_row(
        self, tmp_path
    ) -> None:
        """A policy that cannot compose raises out of ``start`` instead of dropping rows."""
        from kiro_crew.platform import PlatformCompositionError

        _write_store(
            tmp_path,
            [
                {"id": "keep-me", "slot_key": "chat-1-1", "message": "watch it", "idle_secs": 300},
                {
                    "id": "keep-me-2",
                    "slot_key": "chat-1-2",
                    "message": "and this",
                    "idle_secs": 300,
                },
            ],
        )
        _install_broken()
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            with pytest.raises(PlatformCompositionError):
                await svc.start()
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_the_store_file_still_holds_both_rows(self, tmp_path) -> None:
        """The consequence the raise exists to prevent, asserted on disk."""
        from kiro_crew.platform import PlatformCompositionError

        rows = [
            {"id": "keep-me", "slot_key": "chat-1-1", "message": "watch it", "idle_secs": 300},
            {"id": "keep-me-2", "slot_key": "chat-1-2", "message": "and this", "idle_secs": 300},
        ]
        _write_store(tmp_path, rows)
        _install_broken()
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            with pytest.raises(PlatformCompositionError):
                await svc.start()
        finally:
            svc.stop()
        on_disk = json.loads((tmp_path / "autonudge.json").read_text(encoding="utf-8"))
        assert [r["id"] for r in on_disk["loops"]] == [
            "keep-me",
            "keep-me-2",
        ], "the store was rewritten from the surviving rows, deleting a loop"

    @pytest.mark.asyncio
    async def test_a_working_policy_still_loads_both_rows(self, tmp_path) -> None:
        """Control: the raise must not turn ordinary startup into a failure."""
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.platform import build_default_context, set_context

        set_context(build_default_context(KiroCrewConfig()))
        _write_store(
            tmp_path,
            [
                {"id": "keep-me", "slot_key": "chat-1-1", "message": "watch it", "idle_secs": 300},
                {
                    "id": "keep-me-2",
                    "slot_key": "chat-1-2",
                    "message": "and this",
                    "idle_secs": 300,
                },
            ],
        )
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            assert sorted(svc._loops) == ["keep-me", "keep-me-2"]
        finally:
            svc.stop()


class TestTheAddressingExemptionCannotCarryACredential:
    """The exemption serves ``id``/``slot_key`` unscrubbed, so the store must not hold one.

    Rewriting either breaks addressing, which is why they are exempt -- but that makes the
    pair the one way a credential reaches a reader verbatim, and ``slot_key`` is
    caller-supplied on the arming request. Closing it at the load boundary keeps the
    exemption safe without making a row unaddressable.
    """

    @classmethod
    def _refused_row(cls) -> dict:
        return {"id": f"n1-{SECRET}", "slot_key": "chat-1-2", "message": "ok", "idle_secs": 300}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", ["id", "slot_key"])
    async def test_a_credential_shaped_addressing_field_is_refused(self, tmp_path, field) -> None:
        row = {"id": "n1", "slot_key": "chat-1-2", "message": "ok", "idle_secs": 300}
        row[field] = f"{row[field]}-{SECRET}"
        _write_store(tmp_path, [row])
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            assert svc._loops == {}, f"a credential in {field} loaded and is served verbatim"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_refused_row_survives_a_survivors_rewrite(self, tmp_path) -> None:
        """A false positive must not DELETE the row, which the skip arm would.

        The second row's message scrub dirties the store, so ``start`` rewrites it
        from the live set -- the exact path that drops a row held out of that set.
        """
        refused = self._refused_row()
        dirty = {"id": "n2", "slot_key": "chat-1-3", "message": SECRET, "idle_secs": 300}
        _write_store(tmp_path, [refused, dirty])
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            assert set(svc._loops) == {"n2"}
            written = json.loads((tmp_path / "autonudge.json").read_text(encoding="utf-8"))
        finally:
            svc.stop()
        kept = {r.get("id"): r for r in written["loops"]}
        assert (
            kept.get("n2", {}).get("message") != SECRET
        ), "the store was never rewritten, so retention was not exercised"
        assert refused in written["loops"], "the rewrite deleted the refused row"

    @pytest.mark.asyncio
    async def test_the_refusal_names_the_field_and_not_the_value(self, tmp_path, caplog) -> None:
        _write_store(tmp_path, [{**self._refused_row(), "gate": 0}])
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            with caplog.at_level("WARNING"):
                await svc.start()
        finally:
            svc.stop()
        assert "whose id is unusable" in caplog.text, "the holding arm never fired"
        assert SECRET not in caplog.text, "a repair logged the raw id before the refusal"

    @pytest.mark.asyncio
    async def test_an_ordinary_row_still_loads(self, tmp_path) -> None:
        """Control: the refusal must not reject addressable ids."""
        _write_store(
            tmp_path,
            [{"id": "n1", "slot_key": "chat-1-2", "message": "ok", "idle_secs": 300}],
        )
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            assert "n1" in svc._loops
        finally:
            svc.stop()

    @staticmethod
    def _monitor(**changes) -> MonitorState:
        base = {"kind": "github_pull_request", "target": "o/r#1", "objective": "review_ready"}
        return MonitorState(**{**base, "created_ts": 1_000.0, **changes})

    @pytest.mark.parametrize(
        "generated",
        [
            "chat-1-1785",
            "slack:1700000000.100100",
            "discord:kiro:direct:428211",
            "webex:someone@example.com",
            "webex:space:Y2lzY29zcGFyazovL3VzL1JPT00v"
            "YmJjZWIxYWQtNDNmMS0zYjU4LTkxNDctZjE0YmIwYzRkMTU0",
            "a1b2c3d4",
        ],
    )
    @pytest.mark.parametrize("field", sorted(_an.ADDRESSING_FIELDS))
    def test_no_generated_addressing_shape_is_refused(self, generated, field) -> None:
        """Mirrors ``_load``'s predicate. A Webex space key is a 76-char unbroken base64
        run, so a widened matcher would silently un-arm a stored loop; that fails here."""
        assert _an.scrub_loop_text(generated, field=field) == generated

    @pytest.mark.asyncio
    async def test_a_held_row_still_owns_its_id_and_slot(self, tmp_path) -> None:
        _write_store(tmp_path, [self._refused_row()])
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            for slot, lid in (("chat-1-2", None), ("chat-9-8", self._refused_row()["id"])):
                with pytest.raises(_an.MonitorUpdateConflict) as err:
                    await svc.add(slot, "a replacement", loop_id=lid)
                assert SECRET not in str(err.value), "the refusal echoed the credential"
            assert await svc.add("chat-9-9", "a free slot still arms")
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_refused_row_survives_a_custom_monitor_snapshot(self, tmp_path) -> None:
        """A staged monitor replacement builds its own payload from the LIVE set only.

        Retention was honoured by ``_serialize_state`` alone, so this routine write --
        and the monitor-add and rollback builders sharing its shape -- rewrote the
        store without the held row, with no recovery path once it landed.
        """
        refused = self._refused_row()
        _write_store(tmp_path, [refused])
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            assert svc._loops == {}, "the row was not quarantined, so retention is untested"
            live = NudgeLoop(
                id="m1", slot_key="chat-1-9", message="watch it", monitor=self._monitor()
            )
            svc._loops[live.id] = live
            staged = NudgeLoop(
                id="m1",
                slot_key="chat-1-9",
                message="watch it",
                monitor=self._monitor(wake_in_flight=True),
            )
            await svc._persist_staged_monitor_locked(live, staged)
            written = json.loads((tmp_path / "autonudge.json").read_text(encoding="utf-8"))
        finally:
            svc.stop()
        assert "m1" in [
            r.get("id") for r in written["loops"]
        ], "the snapshot never landed, so retention was not exercised"
        assert refused in written["loops"], "a custom monitor snapshot deleted the refused row"


class TestNoRouteAnswers500WhenTheProjectionCannotScrub:
    """The projection runs after the mutation commits, so an unguarded route 500s.

    Every route that serializes a loop or monitor goes through ``_ok_serialized``, which
    is what keeps a NEW route from reintroducing the 500: a hand-built
    ``json_response`` carrying a serializer is the shape that regressed.
    """

    def test_a_failing_projection_answers_503(self) -> None:
        _install_broken()
        resp = autonudge_handlers._ok_serialized(
            "loop", lambda: autonudge_handlers._serialize(_loop(message="watch it"))
        )
        assert resp.status == 503, "a committed mutation answered 500, not 503"

    def test_a_working_projection_still_answers_200(self) -> None:
        """Control: the guard must not turn ordinary replies into refusals."""
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.platform import build_default_context, set_context

        set_context(build_default_context(KiroCrewConfig()))
        resp = autonudge_handlers._ok_serialized(
            "loop", lambda: autonudge_handlers._serialize(_loop(message="watch it"))
        )
        assert resp.status == 200

    def test_no_route_hand_builds_a_serialized_reply(self) -> None:
        from pathlib import Path

        src = Path(autonudge_handlers.__file__).read_text(encoding="utf-8")
        shapes = (
            '"loop": _serialize(',
            '"monitor": _serialize_monitor(',
            '"monitor": _redact_monitor_value(',
        )
        for shape in shapes:
            # Compared as a bool so a failure names the shape instead of printing
            # the whole module, which pytest would otherwise dump.
            bypassed = shape in src
            assert not bypassed, f"a route bypasses _ok_serialized: {shape}"

    @pytest.mark.asyncio
    async def test_a_broken_policy_stops_no_monitor(self) -> None:
        """The preflight half: the mutation must not run at all."""
        _install_broken()
        calls: list[str] = []

        class _Svc:
            async def stop_monitor(self, loop_id, user_reason=""):
                calls.append(loop_id)
                return _loop()

        loop, error, status = await authz.authorize_and_stop_monitor(
            svc=_Svc(), loop_id="n1", session_key="chat-1-2", source="dashboard"
        )
        assert status == 503 and loop is None, f"expected an audited 503, got {status}"
        assert calls == [], "the monitor was stopped before the policy was checked"

    @pytest.fixture()
    def audits(self, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
        events: list[dict] = []
        monkeypatch.setattr(
            authz,
            "sel",
            lambda: SimpleNamespace(log_tool_invocation=lambda **kw: events.append(kw)),
        )
        return events

    @pytest.mark.asyncio
    async def test_the_stop_refusal_is_audited(self, audits) -> None:
        """A refusal SEL never sees is a refusal nobody can review afterwards."""
        _install_broken()
        _loop_, error, status = await authz.authorize_and_stop_monitor(
            svc=SimpleNamespace(), loop_id="n1", session_key="chat-1-2", source="dashboard"
        )
        assert status == 503
        assert [e["outcome"] for e in audits] == ["denied"], f"unaudited refusal: {audits}"

    @pytest.mark.asyncio
    async def test_the_update_refusal_is_audited(self, audits) -> None:
        _install_broken()
        _loop_, error, status = await authz.authorize_and_update_monitor(
            svc=SimpleNamespace(),
            state=SimpleNamespace(_slots={}, sessions=None, channel_transports=None),
            loop_id="n1",
            session_key="chat-1-2",
            patch={"interval_secs": 60},
            source="dashboard",
        )
        assert status == 503
        assert [e["outcome"] for e in audits] == ["denied"], f"unaudited refusal: {audits}"

    @pytest.mark.asyncio
    async def test_the_structured_monitor_read_answers_503(self, monkeypatch) -> None:
        """Its sibling branch got the guard; this one still raised into a 500."""
        loop = _loop(slot_key="chat-1-111")
        loop.monitor = MonitorState(
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            created_ts=1_000.0,
        )
        monkeypatch.setattr(
            autonudge_handlers,
            "_autonudge_get",
            lambda: SimpleNamespace(get_by_slot=lambda _key: loop),
        )
        request = make_mocked_request(
            "GET",
            "/api/autonudge/session-monitor",
            headers={"X-Session-Key": "dashboard:chat-1-111"},
        )
        request["internal_auth"] = True
        _install_broken()

        resp = await autonudge_handlers.api_session_monitor_get(request)

        assert resp.status == 503, f"the structured branch answered {resp.status}, not 503"

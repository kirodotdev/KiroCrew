"""Tests for the optional per-loop ``banner`` on auto-nudge.

A nudge loop's ``message`` serves two consumers with opposite needs. The model
needs the whole instruction re-delivered on every cycle — that is the guarantee
the nudge exists to provide. A person reading the transcript needs only "a nudge
happened", and today gets the same multi-KB payload appended per cycle: measured
on one long-running loop, 44 nudge rows of ~7.9KB were 51.8% of the entire
671,900-char session file.

``banner`` lets a loop opt into a short visible row while the prompt stays whole.

Two properties carry the change, and the FIRST is the acceptance bar:

1. **Default byte-identity.** A loop with no banner must append exactly the row
   it appended before this feature existed. Every armed loop in the fleet
   depends on that, and the feature is worthless if buying it costs a behaviour
   change for loops that never asked for it.
2. **Divergence proved at the PROMPT.** The whole point is that the row and the
   prompt differ, so a test asserting only on the row would pass just as well if
   the prompt had ALSO been shortened — which is precisely the defect this must
   not introduce. Every banner test therefore asserts on the argument handed to
   ``_run_chat``, not merely on ``slot.append``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import kiro_crew.mcp_core as mcp_core
from kiro_crew import autonudge as _an
from kiro_crew import autonudge_authz as authz
from kiro_crew import session_directive
from kiro_crew.autonudge import AutoNudgeService, NudgeLoop
from kiro_crew.autonudge_authz import MAX_BANNER_CHARS
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers import autonudge as autonudge_handlers
from kiro_crew.mcp_core import _call_tool_inner
from kiro_crew.security import redact
from kiro_crew.slack import gateway as gw
from kiro_crew.validation import ValidationError


def _moved_aside_sidecar(base_dir: Path) -> Path:
    """The single ``.corrupt-<ts>`` copy an unreadable sidecar is renamed to.

    Design review asked for the move-aside so recovery is a restart rather than a hand
    repair; the bytes must still be there, which is what these tests assert on.
    """
    matches = sorted(base_dir.glob("autonudge.quarantine.json.corrupt-*"))
    assert len(matches) == 1, f"expected exactly one moved-aside copy, got {matches!r}"
    return matches[0]


def _held_aside_rows(base_dir) -> list:
    """Read held-aside rows from the quarantine sidecar, the single durable location."""
    path = Path(base_dir) / "autonudge.quarantine.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("quarantined", [])


# ── Fire-path harness (mirrors test_autonudge_dashboard_fire.py) ──


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


def _slot(key: str = "chat-1-1785") -> MagicMock:
    slot = MagicMock()
    slot.key = key
    slot.running = False
    slot._in_stage_execution = False
    return slot


def _orchestrator() -> gw.GatewayOrchestrator:
    cfg = KiroCrewConfig()
    with patch.object(cfg, "load_credentials", return_value={"KIROCREW_OWNER_ID": "U_OWNER"}):
        orch = gw.GatewayOrchestrator(cfg, no_dashboard=True, no_crons=True, no_open=True)
    orch.dashboard_state = SimpleNamespace(
        get_slot=MagicMock(return_value=None),
        push_slots_update=MagicMock(),
        _background_tasks=set(),
        run_background_turn=MagicMock(side_effect=lambda _slot, coro: coro),
    )
    orch.autonudge_svc = MagicMock()
    orch.autonudge_svc.remove = AsyncMock()
    orch._session_tasks = {}
    return orch


def _fake_spawn():
    def _spawn(state, slot, coro, **kwargs):
        coro.close()
        return MagicMock(name="turn-task")

    return _spawn


async def _fire(loop: NudgeLoop, *, ledger: str = "") -> tuple[str, str]:
    """Fire once; return ``(appended_row, prompt_passed_to_run_chat)``.

    Both halves come from the REAL ``_fire_dashboard_nudge``, so the two can be
    compared against each other rather than against a re-derivation of what the
    code is assumed to do.
    """
    appended, prompt, _meta = await _fire_full(loop, ledger=ledger)
    return appended, prompt


async def _fire_full(loop: NudgeLoop, *, ledger: str = "") -> tuple[str, str, dict]:
    """``_fire`` plus the appended row's ``meta`` block.

    Split out rather than changing ``_fire``'s shape so the row-vs-prompt tests
    that only care about the two strings keep reading exactly as before.
    """
    orch = _orchestrator()
    slot = _slot()
    orch.dashboard_state.get_slot = MagicMock(return_value=slot)
    run_chat = AsyncMock()

    async def _compose(message, sentinel, slot_key):
        # Stand in for the ledger-snapshot composer: exercised with and without
        # a snapshot, because the banner branch must bypass the snapshot while
        # the prompt keeps it.
        body = message.replace("{{STOP_FILE}}", sentinel or "")
        return f"{ledger}\n\n{body}" if ledger else body

    with (
        patch.object(gw, "spawn_guarded_turn", _fake_spawn()),
        patch.object(gw, "compose_nudge_body", new=AsyncMock(side_effect=_compose)),
        patch("kiro_crew.dashboard.chat._run_chat", new=run_chat),
    ):
        assert await orch._fire_dashboard_nudge(loop) is True

    appended = slot.append.call_args.args[1]
    prompt = run_chat.call_args.args[2]
    meta = slot.append.call_args.kwargs["meta"]["nudge"]
    return appended, prompt, meta


class TestDefaultRowIsUnchanged:
    """THE ACCEPTANCE BAR — every loop that never asked for this is untouched."""

    @pytest.mark.asyncio
    async def test_no_banner_appends_the_pre_change_string(self) -> None:
        """The row is exactly ``[auto-nudge cycle N]\\n<composed body>``.

        Written as a literal rather than as ``row == prompt`` so it pins the
        historical FORMAT too: an identity assertion would still pass if both
        sides changed together.
        """
        loop = _loop()
        row, prompt = await _fire(loop)
        expected = f"[auto-nudge cycle {loop.cycle_count + 1}]\n{loop.message}"
        assert row == expected
        assert prompt == expected

    @pytest.mark.asyncio
    async def test_no_banner_keeps_the_ledger_snapshot_in_the_row(self) -> None:
        """A snapshot-prefixed body is unchanged too.

        The banner branch bypasses ``compose_nudge_body``, so the default branch
        must be shown to still carry its output — otherwise a refactor that
        routed BOTH branches around the composer would look correct here.
        """
        loop = _loop()
        row, prompt = await _fire(loop, ledger="LEDGER: 2 open items")
        assert row == prompt
        assert "LEDGER: 2 open items" in row

    @pytest.mark.asyncio
    async def test_whitespace_only_banner_is_treated_as_absent(self) -> None:
        loop = _loop(banner="   \n\t ")
        row, prompt = await _fire(loop)
        assert row == f"[auto-nudge cycle {loop.cycle_count + 1}]\n{loop.message}"
        assert row == prompt

    def test_the_field_defaults_to_empty(self) -> None:
        """A loop constructed without the kwarg must not opt in by accident."""
        assert NudgeLoop(id="i", slot_key="chat-1-1", message="m").banner == ""


class TestBannerDivergesTheRowFromThePrompt:
    @pytest.mark.asyncio
    async def test_row_shows_the_banner_and_the_prompt_keeps_the_message(self) -> None:
        """TEST 2 — the one that actually proves the feature.

        Four assertions, because three of them can pass while the change is
        still wrong: the row could carry the banner AND the message (no saving),
        or the prompt could have been shortened alongside the row (instruction
        silently deleted). The prompt assertions are the ones that catch the
        defect this change must not introduce.
        """
        loop = _loop(banner="babysit cycle — see the loop file")
        row, prompt = await _fire(loop)

        assert "babysit cycle — see the loop file" in row
        assert loop.message not in row, "the row still carries the full message"
        assert loop.message in prompt, "the PROMPT was shortened — the model lost instruction"
        assert "babysit cycle" not in prompt, "the banner leaked into the model's copy"

    @pytest.mark.asyncio
    async def test_the_prompt_keeps_the_ledger_snapshot_the_row_drops(self) -> None:
        """The composer's snapshot belongs to the model, not to the display.

        Sharpens the previous test: a banner implementation that shortened only
        the message but still prefixed the snapshot would pass every assertion
        above while leaving a multi-KB row.
        """
        loop = _loop(banner="cycle ran")
        row, prompt = await _fire(loop, ledger="LEDGER: 2 open items")
        assert "LEDGER" in prompt
        assert "LEDGER" not in row

    @pytest.mark.asyncio
    async def test_the_cycle_prefix_survives_on_the_banner_branch(self) -> None:
        """The counter is the row's remaining information — it must not be lost."""
        loop = _loop(banner="cycle ran", cycle_count=41)
        row, _prompt = await _fire(loop)
        assert row.startswith("[auto-nudge cycle 42]\n")

    @pytest.mark.asyncio
    async def test_stop_file_is_rendered_in_a_banner(self) -> None:
        loop = _loop(banner="stop me: {{STOP_FILE}}", stop_sentinel_path="/tmp/.stop-chat-1")
        row, prompt = await _fire(loop)
        assert "stop me: /tmp/.stop-chat-1" in row
        assert "{{STOP_FILE}}" not in row
        assert loop.message in prompt

    @pytest.mark.asyncio
    async def test_the_banner_is_the_only_thing_shortened(self) -> None:
        """A negative control on the saving the field exists to deliver.

        Fails if the row is not dramatically smaller than the prompt, which is
        the whole measured motivation. Sized off the real ratio (7.9KB row vs a
        one-line banner), not a token difference.
        """
        loop = _loop(message="x" * 6000, banner="cycle ran")
        row, prompt = await _fire(loop)
        assert len(row) < 100
        assert len(prompt) > 6000


# ── Persistence: tolerant in BOTH directions, so no store-version bump ──


class TestNonStringBannerCannotWedgeTheLoop:
    """A truthy NON-STRING ``banner`` used to crash every dashboard fire.

    ``banner: str`` is a plain dataclass annotation, unenforced at runtime, and
    ``_load`` constructs a loop straight from parsed JSON with no coercion. So a
    store carrying ``"banner": 5`` yields ``loop.banner == 5``; the fire path then
    called ``(5 or "").strip()`` -> ``5.strip()`` -> ``AttributeError``. The fire
    raises, nothing is delivered, and ``_run_fire_cycle`` re-arms an undelivered
    cycle with backoff — so the loop rearms forever and never delivers again.

    THREE arms, because no one of them alone has coverage:

    1. bad input fires AND lands on the ``tagged`` fallback — asserting only that
       it does not raise would pass under a fix that treats EVERY banner as
       absent, silently deleting the feature this PR exists to add;
    2. a valid string banner still renders through ``render_nudge_message`` — the
       arm that catches exactly that over-broad fix;
    3. whitespace-only still falls through to ``tagged`` — the behaviour the
       comment block at the call site defends, and the likeliest casualty of a
       type-guard rewrite.
    """

    @pytest.mark.parametrize("bad", [5, 1.5, ["x"], {"a": 1}, object()])
    @pytest.mark.asyncio
    async def test_bad_input_arm_fires_and_falls_back_to_tagged(self, bad) -> None:
        """ARM 1 — no crash, AND the delivered row is the full-message fallback."""
        loop = _loop(banner=bad)
        row, prompt = await _fire(loop)
        expected = f"[auto-nudge cycle {loop.cycle_count + 1}]\n{loop.message}"
        assert row == expected, "a non-string banner did not fall back to `tagged`"
        assert prompt == expected, "the prompt diverged on a fallback row"

    @pytest.mark.asyncio
    async def test_good_input_arm_still_renders_the_banner(self) -> None:
        """ARM 2 — the guard must not throw the baby out.

        Asserts on the RENDERED banner body including ``{{STOP_FILE}}``
        substitution, so a fix that treats every banner as absent fails here
        rather than passing quietly.
        """
        loop = _loop(banner="watching CI — halt: {{STOP_FILE}}", stop_sentinel_path="/tmp/.stop-x")
        row, prompt = await _fire(loop)
        assert row == f"[auto-nudge cycle {loop.cycle_count + 1}]\nwatching CI — halt: /tmp/.stop-x"
        assert "{{STOP_FILE}}" not in row, "the banner bypassed render_nudge_message"
        assert loop.message not in row, "the row still carries the full message"
        assert loop.message in prompt, "the PROMPT was shortened"

    @pytest.mark.asyncio
    async def test_whitespace_arm_still_falls_through_to_tagged(self) -> None:
        """ARM 3 — a BLANK row is worse than the verbose one it replaced."""
        loop = _loop(banner="  \t\n ")
        row, prompt = await _fire(loop)
        expected = f"[auto-nudge cycle {loop.cycle_count + 1}]\n{loop.message}"
        assert row == expected
        assert row == prompt

    @pytest.mark.asyncio
    async def test_falsy_non_strings_behave_as_before(self) -> None:
        """0 / None / "" were already safe under the old expression; keep them so.

        The crash needed a TRUTHY non-string — ``(0 or "")`` yielded ``""`` and
        survived. Pinned so the guard is a strict widening, never a change of
        behaviour for an input that already worked.
        """
        for benign in (0, None, "", False):
            loop = _loop(banner=benign)
            row, prompt = await _fire(loop)
            expected = f"[auto-nudge cycle {loop.cycle_count + 1}]\n{loop.message}"
            assert row == expected, f"banner={benign!r} changed behaviour"
            assert row == prompt


class TestLoadNormalizesANonStringBanner:
    """The boundary repair, matching ``repair_sentinel_path``'s existing shape.

    The call-site guard alone stops the crash, but leaves the bad value in the
    store: every boot reloads ``"banner": 5`` and silently suppresses the banner
    with no signal. ``_load`` already repairs the fields whose corrupt value has a
    demonstrated runtime consequence — ``stop_sentinel_path`` via an isinstance
    check, ``idle_secs`` / ``next_due_ts`` via ``_repair_number`` — so this follows
    that selective convention rather than making ``_load`` validate every field.
    """

    def _write_store(self, tmp_path, banner) -> None:
        (tmp_path / "autonudge.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "loops": [
                        {
                            "id": "abc123",
                            "slot_key": "chat-9-1",
                            "message": "go",
                            "idle_secs": 300,
                            "banner": banner,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    @pytest.mark.asyncio
    async def test_non_string_banner_is_repaired_and_persisted(self, tmp_path, caplog) -> None:
        self._write_store(tmp_path, 5)
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            with caplog.at_level("WARNING"):
                await svc.start()
            assert svc._loops["abc123"].banner == ""
            # Persisted, not merely tolerated in memory — otherwise the next boot
            # re-derives the same suppression.
            on_disk = json.loads((tmp_path / "autonudge.json").read_text(encoding="utf-8"))
            assert on_disk["loops"][0]["banner"] == ""
            assert "non-string banner" in caplog.text
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_valid_banner_is_untouched_and_the_store_is_not_rewritten(
        self, tmp_path
    ) -> None:
        """Negative control on the repair: it must not fire on good input.

        Without this, a repair that blanked EVERY banner would pass the test
        above. The byte comparison also proves ``_store_dirty`` was not set, so a
        clean store is not rewritten on every boot.
        """
        self._write_store(tmp_path, "cycle ran")
        before = (tmp_path / "autonudge.json").read_bytes()
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            assert svc._loops["abc123"].banner == "cycle ran"
            assert svc._store_dirty is False
            assert (tmp_path / "autonudge.json").read_bytes() == before
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_non_string_banner_is_never_logged_by_value(self, tmp_path, caplog) -> None:
        """The warning must name the TYPE, never interpolate the value.

        "Non-string" does not mean "value-free": a hand-edited store can hold a
        one-element list whose member is a credential, and this warning reaches
        the log ring and the ``/api/logs`` SSE stream. The log-record redaction
        filter cannot save it either — it is seeded with *literal known* secret
        values, so an arbitrary token in a store file is invisible to it.

        Asserts on ``record.args`` as well as the formatted text because ``%r``
        formats LAZILY: the raw object sits in ``args`` until a handler renders
        it, so a handler that serialises ``args`` structurally sees it without
        ever producing the interpolated string. Measured caveat, so the next
        reader does not overrate it: this assertion is NOT independently
        falsifiable through caplog. Both an args-only mutation (a stale trailing
        arg the format string does not consume) and a restored ``%r`` fail the
        TEXT assertion first, because logging's own formatting-error path dumps
        the entire record — args included — into the emitted text. It is kept as
        a cheap guard for the non-caplog handler shape, not as proven coverage.
        """
        secret = "AKIAIOSFODNN7EXAMPLE"
        self._write_store(tmp_path, [secret])
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            with caplog.at_level("WARNING"):
                await svc.start()
        finally:
            svc.stop()

        assert secret not in caplog.text, "the credential reached the formatted log line"
        for record in caplog.records:
            assert secret not in str(record.args), "the credential reached the record args"
        # Positive assertion, so a fix that simply deletes the warning does not
        # pass: the type is what makes this diagnosable at all.
        assert "type list" in caplog.text
        assert svc._loops["abc123"].banner == ""

    @pytest.mark.asyncio
    async def test_a_benign_non_string_still_reports_its_type(self, tmp_path, caplog) -> None:
        """Control on the type-only form: the diagnostic survives for a plain int.

        Guards the reverse mistake — a fix that stops naming the type, or names
        it only for containers, leaves an operator with a warning that cannot be
        acted on.
        """
        self._write_store(tmp_path, 5)
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            with caplog.at_level("WARNING"):
                await svc.start()
            assert "type int" in caplog.text
            assert svc._loops["abc123"].banner == ""
        finally:
            svc.stop()


class TestLoadEnforcesTheBannerCap:
    """The 500-char cap must hold on the LOAD path, not only on the write path.

    Both authorized write paths reject an over-cap banner with a 400
    (``autonudge_authz``), so nothing that arrived through the API can exceed it.
    The store is not one of those paths — ``autonudge.json`` is a file an agent
    can write directly — and ``_load`` is the only other way a banner reaches
    memory, so an unbounded value gets in with no bound applied anywhere.

    That matters because the banner is then scanned synchronously: ``_load``
    itself runs both redactors over it, and the fire path runs
    ``render_nudge_message`` plus the same two passes on every cycle. Those scans
    are linear in the banner's length, and nothing else bounds it.

    Treated as ABSENT rather than truncated, matching this loader's existing
    convention for an invalid persisted value: the sibling non-string arm sets
    ``""`` and marks the store dirty, and ``repair_sentinel_path`` does the same.
    Truncating would invent a banner the operator never wrote.
    """

    def _write_store(self, tmp_path, banner) -> None:
        (tmp_path / "autonudge.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "loops": [
                        {
                            "id": "abc123",
                            "slot_key": "chat-9-1",
                            "message": "the full multi-paragraph babysit instruction",
                            "idle_secs": 300,
                            "banner": banner,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    @pytest.mark.asyncio
    async def test_an_oversized_persisted_banner_is_treated_as_absent(
        self, tmp_path, caplog
    ) -> None:
        """Over the cap by one character is enough — the bound is the assertion.

        Fails on the unmodified tree, where the oversized banner survives the
        load intact because no length bound exists on this path at all.
        """
        oversized = "b" * (MAX_BANNER_CHARS + 1)
        self._write_store(tmp_path, oversized)
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            with caplog.at_level("WARNING"):
                await svc.start()
            assert svc._loops["abc123"].banner == "", "the oversized banner survived the load"
            # Persisted, matching the sibling arms: otherwise every boot re-reads
            # the same oversized value and re-does the work of rejecting it.
            on_disk = json.loads((tmp_path / "autonudge.json").read_text(encoding="utf-8"))
            assert on_disk["loops"][0]["banner"] == ""
            # The reason must be diagnosable, and the value must not be logged —
            # it is attacker-controlled text of unbounded length.
            assert f"max {MAX_BANNER_CHARS}" in caplog.text
            assert oversized not in caplog.text, "the warning echoed the banner"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_banner_at_the_cap_loads_intact(self, tmp_path) -> None:
        """Negative control: the bound must be inclusive at exactly the cap.

        Without this, a fix that blanked every banner — or used ``>=`` — would
        pass the arm above while destroying the feature. The byte comparison also
        proves ``_store_dirty`` stayed False, so a valid store is not rewritten
        on every boot.
        """
        at_cap = "b" * MAX_BANNER_CHARS
        self._write_store(tmp_path, at_cap)
        before = (tmp_path / "autonudge.json").read_bytes()
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            assert svc._loops["abc123"].banner == at_cap
            assert len(svc._loops["abc123"].banner) == MAX_BANNER_CHARS
            assert svc._store_dirty is False
            assert (tmp_path / "autonudge.json").read_bytes() == before
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_banner_at_the_cap_carrying_a_credential_is_kept_raw_on_disk(
        self, tmp_path
    ) -> None:
        """The cap is enforced on the RAW stored value only, by design.

        The load-time redaction that used to run here was subtracted, so there is
        no longer a post-redaction bound: nothing scrubs the banner before the
        cap, and nothing persists a scrubbed value that could have grown past it.
        That growth concern belonged entirely to the persisting arm -- redaction
        can grow a string (``[REDACTED: credential]`` is 22 characters replacing a
        20-character key id), so persisting the scrubbed value could breach the
        bound on disk. With nothing persisted, there is nothing to re-bound.

        An at-cap banner is therefore kept exactly as written, and the sinks scrub
        it on the way out (see TestAPersistedBannerIsScrubbedAtEverySinkNotAtLoad).
        """
        secret = "AKIA" + "IOSFODNN7EXAMPLE"
        at_cap_with_secret = "b" * (MAX_BANNER_CHARS - len(secret)) + secret
        assert len(at_cap_with_secret) == MAX_BANNER_CHARS, "fixture is not at the cap"
        self._write_store(tmp_path, at_cap_with_secret)
        before = (tmp_path / "autonudge.json").read_bytes()
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            assert (
                svc._loops["abc123"].banner == at_cap_with_secret
            ), "an at-cap banner was altered at load"
            assert (tmp_path / "autonudge.json").read_bytes() == before, "the store was rewritten"
        finally:
            svc.stop()


class TestMalformedEntryWarningWithholdsTheRow:
    """The construction-failure sink must not dump the row it failed to build.

    F1, first-principles review. Every arm in ``_load`` withholds the banner's
    VALUE and scrubs the id, on the stated ground that the rule belongs to the
    SINK rather than to one branch. The ``except`` arm wrapping those arms did
    not: it logged ``%r`` of the whole persisted dict, so a row that fails to
    construct put every field -- the ``banner`` this PR adds, ``message``, and
    any credential inside either -- into the same log ring and ``/api/logs``
    stream the arms exist to keep it out of.

    The row is the ONE object in the function guaranteed to be attacker-shaped:
    construction failed precisely because it was not the shape we expected.
    """

    SECRET = "AKIAIOSFODNN7EXAMPLE"

    def _write_malformed_store(self, tmp_path, extra) -> None:
        """A row that CANNOT construct: ``slot_key`` is REQUIRED and absent.

        The omission has to be a required dataclass field. A wrong-TYPE value
        does not work -- dataclasses do no type checking, so ``idle_secs={}``
        constructs happily and no exception is raised at all (measured: the first
        draft of this fixture used exactly that and the row loaded).
        """
        row = {
            "id": "abc123",
            "message": f"deploy with {self.SECRET}",
            "idle_secs": 300,
        }
        row.update(extra)
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": [row]}), encoding="utf-8"
        )

    @pytest.mark.asyncio
    async def test_a_malformed_row_is_not_echoed_into_the_log(self, tmp_path, caplog) -> None:
        """Fails on the unmodified tree, where ``%r`` of the row is logged."""
        self._write_malformed_store(tmp_path, {"banner": f"watching {self.SECRET}"})
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            with caplog.at_level("WARNING"):
                await svc.start()
            assert "abc123" not in svc._loops, "a malformed row was loaded anyway"
            assert self.SECRET not in caplog.text, "the warning echoed the credential"
            assert "watching" not in caplog.text, "the warning echoed the banner value"
            assert "deploy with" not in caplog.text, "the warning echoed the message value"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_the_warning_still_says_which_row_and_which_fields(
        self, tmp_path, caplog
    ) -> None:
        """Negative control: withholding the VALUES must not blank the warning.

        A fix that simply dropped the interpolation would pass the arm above
        while making a malformed row undiagnosable. The operator still needs the
        row's identity and the field NAMES present, which is what makes a
        hand-edited store fixable.
        """
        self._write_malformed_store(tmp_path, {"banner": "watching CI"})
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            with caplog.at_level("WARNING"):
                await svc.start()
            assert "abc123" in caplog.text, "the warning names no row -- undiagnosable"
            assert "message" in caplog.text, "the warning names no field -- undiagnosable"
            assert "banner" in caplog.text, "the field NAMES are what make it fixable"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_credential_shaped_id_in_a_malformed_row_is_redacted(
        self, tmp_path, caplog
    ) -> None:
        """The id is named, so it gets the same scrub the repair arms give it.

        Naming the row cannot become a new leak: the id comes out of the same
        hand-editable store as every other field.
        """
        self._write_malformed_store(tmp_path, {"id": f"loop-{self.SECRET}"})
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            with caplog.at_level("WARNING"):
                await svc.start()
            assert self.SECRET not in caplog.text, "a credential-shaped id was echoed raw"
            assert "[REDACTED: credential]" in caplog.text, "the id was not scrubbed"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("row", [None, 42, "oops", [1, 2], True])
    async def test_a_non_dict_row_is_skipped_not_fatal(self, tmp_path, caplog, row) -> None:
        """F1, Opus + GPT (BLOCKING): a non-object row must SKIP, not kill startup.

        ``loops`` is hand-editable JSON, so an element need not be an object at
        all. Construction raises, this arm runs, and ``raw.get`` does not exist on
        a non-dict -- so the arm meant to SKIP the row instead raises
        ``AttributeError`` out of ``_load``, out of the unguarded
        ``run_in_executor(None, self._load)`` in ``start()``, and NO loop arms.
        The previous ``%r`` handler tolerated this; the id-scrubbing rewrite
        introduced the regression.

        Fails on the unmodified tree with AttributeError, not an assertion.
        """
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": [row]}), encoding="utf-8"
        )
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            with caplog.at_level("WARNING"):
                await svc.start()  # must not raise
            assert svc._loops == {}, "a non-dict row produced a loop"
            assert "malformed" in caplog.text, "the row was dropped with no warning at all"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_dict_row_still_arms_normally(self, tmp_path) -> None:
        """Negative control for the guard: a WELL-FORMED row must still load.

        A guard written as "treat everything as non-dict" would pass the arm above
        while making the loader load nothing at all.
        """
        (tmp_path / "autonudge.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "loops": [
                        {
                            "id": "good1",
                            "slot_key": "chat-9-1",
                            "message": "go",
                            "idle_secs": 300,
                            "banner": "watching CI",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            assert "good1" in svc._loops, "a well-formed row failed to arm"
            assert svc._loops["good1"].banner == "watching CI"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_credential_shaped_field_name_is_redacted(self, tmp_path, caplog) -> None:
        """F2, GPT (BLOCKING): the joined field NAMES are attacker-controlled too.

        ``raw`` is hand-editable JSON, so its KEYS are as untrusted as its values
        -- a key can itself be a credential. ``bad_id`` above is run through both
        redactors; the joined names were not, which is the asymmetry. Same sink,
        same log ring, same ``/api/logs`` stream.

        Fails on the unmodified tree, where the key is joined in verbatim.
        """
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": [{"id": "abc123", f"tok_{self.SECRET}": 1}]}),
            encoding="utf-8",
        )
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            with caplog.at_level("WARNING"):
                await svc.start()
            assert self.SECRET not in caplog.text, "a credential-shaped field NAME was echoed"
        finally:
            svc.stop()


class TestAPersistedBannerIsScrubbedAtEverySinkNotAtLoad:
    """First Principles: the load-time redact-and-persist arm was subtracted.

    ``_load`` no longer rewrites the store to scrub a banner. It is sink-only
    treatment now, exactly what this PR gives ``message``: the value is left on
    disk as the operator wrote it, and every egress scrubs it on the way out.

    The banner reaches exactly TWO egresses, and both are covered here:

    * ``GET /api/autonudge`` -- ``_serialize`` runs a DENYLIST, so ``banner`` is
      scrubbed because it is not one of ``ADDRESSING_FIELDS``.
    * the fire path's transcript row -- ``redact(shown)`` in ``slack/gateway.py``.

    It reaches no third one: the ``autonudge_state`` websocket payload does not
    carry ``banner`` at all.

    ``_load``'s non-string and oversize repairs deliberately REMAIN -- those have
    named runtime consequences (a non-string is ``.strip()``ed on the fire path
    and wedges the loop; the cap bounds per-cycle work), which a scrub does not.
    """

    SECRET = "AKIA" + "IOSFODNN7EXAMPLE"

    def _write_store(self, tmp_path, banner) -> None:
        (tmp_path / "autonudge.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "loops": [
                        {
                            "id": "abc123",
                            "slot_key": "chat-9-1",
                            "message": "the full multi-paragraph babysit instruction",
                            "idle_secs": 300,
                            "banner": banner,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def _app(self, monkeypatch, svc):
        """The real ``GET /api/autonudge`` route over a real service."""
        from aiohttp import web

        from kiro_crew.dashboard.handlers import autonudge as _handler

        monkeypatch.setattr(_handler, "_autonudge_get", lambda: svc)
        app = web.Application()
        app.router.add_get("/api/autonudge", _handler.api_autonudge_list)
        return app

    async def _served_banner(self, tmp_path, monkeypatch, banner) -> str:
        from aiohttp.test_utils import TestClient, TestServer

        self._write_store(tmp_path, banner)
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            async with TestClient(TestServer(self._app(monkeypatch, svc))) as client:
                resp = await client.get("/api/autonudge")
                assert resp.status == 200
                body = await resp.json()
        finally:
            svc.stop()
        return body["loops"][0]["banner"]

    @pytest.mark.asyncio
    async def test_the_rest_surface_never_serves_a_persisted_credential(
        self, tmp_path, monkeypatch
    ) -> None:
        """The reported path, now closed by the serializer rather than by ``_load``.

        Asserts on the endpoint's own JSON rather than on the in-memory field, so
        it cannot pass by a change that masks the value somewhere else while the
        serializer still reads the raw one.
        """
        served = await self._served_banner(tmp_path, monkeypatch, f"watching CI {self.SECRET}")
        assert self.SECRET not in served, "the REST surface served the raw credential"
        assert "[REDACTED: credential]" in served
        assert "watching CI" in served, "redaction ate the whole banner"

    @pytest.mark.asyncio
    async def test_an_exfiltrating_url_in_a_persisted_banner_is_redacted(
        self, tmp_path, monkeypatch
    ) -> None:
        """Its own arm because one break-arm cannot validate two passes.

        Removing only ``redact_exfiltration_urls`` from the shared helper leaves
        the credential arms green. Payload shape taken from that function's own
        behaviour: it keys on data embedded in a URL query, not a host denylist.
        """
        served = await self._served_banner(
            tmp_path,
            monkeypatch,
            "report to https://example.com/upload?data=aGVsbG8gd29ybGQgdGhpcyBpcyBiYXNlNjQ=",
        )
        assert "aGVsbG8gd29ybGQ" not in served
        assert "[REDACTED: suspicious URL to example.com]" in served

    @pytest.mark.asyncio
    async def test_a_newline_bearing_banner_still_reaches_the_sink_scrubbed(
        self, tmp_path, monkeypatch
    ) -> None:
        """A banner is free text, so it can carry a newline AND a credential.

        The subtraction removed the only pass that touched it before the sink, so
        this pins that the sink still scrubs a multi-line value rather than only a
        single-line one.
        """
        served = await self._served_banner(
            tmp_path, monkeypatch, f"watching CI\nkey={self.SECRET}\ntail"
        )
        assert self.SECRET not in served, "a multi-line banner reached the client raw"
        assert "[REDACTED: credential]" in served

    @pytest.mark.asyncio
    async def test_the_store_is_NOT_rewritten_for_a_credential_banner(self, tmp_path) -> None:
        """THE SUBTRACTION ITSELF: load no longer corrects the file.

        Byte-compared, so it also proves ``_store_dirty`` stayed clear. Before the
        subtraction this file came back with ``[REDACTED: credential]`` written
        into it; the credential now stays where the operator put it and is scrubbed
        on the way out instead.
        """
        self._write_store(tmp_path, f"watching CI {self.SECRET}")
        before = (tmp_path / "autonudge.json").read_bytes()
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            assert (
                svc._loops["abc123"].banner == f"watching CI {self.SECRET}"
            ), "the load-time arm is still rewriting the in-memory value"
            assert svc._store_dirty is False, "the load-time arm still marks the store dirty"
            assert (tmp_path / "autonudge.json").read_bytes() == before
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_the_sink_assertion_fails_when_redact_is_a_passthrough(
        self, tmp_path, monkeypatch
    ) -> None:
        """CONTROL: proves the arms above can FAIL now that load does not scrub.

        With the load-time pass gone, the serializer is the only thing standing
        between the store and the client for a REST read. Stubbing the shared
        redactor to identity must therefore let the credential through -- if it
        does not, the arms above are passing for some other reason and prove
        nothing about the sink.

        The stubbed seam is ``redact_via_context``, which is what the sink now
        calls: the scrub was routed through the platform credential policy so a
        composed host's own patterns reach this projection. The ASSERTION is
        unchanged -- only the name of the seam moved. ``monkeypatch.setattr``
        raises on an absent attribute by default, so if the sink is ever rerouted
        again this control fails loudly with ``AttributeError`` rather than
        quietly stubbing something nothing consults.

        It is stubbed on ``kiro_crew.autonudge`` because that is where
        ``scrub_loop_text`` is now DEFINED -- it moved out of the handler so
        ``autonudge_authz`` could share the one rule (the handler imports authz, so
        authz importing the handler back would be a cycle). Patching the handler's
        own ``redact_via_context`` is exactly the silent no-op this docstring warns
        about: that name still EXISTS there for ``_redact_monitor_value``, so the
        stub would land without error while the projection consulted the real
        redactor -- which is how this control was caught degrading.
        """
        monkeypatch.setattr(_an, "redact_via_context", lambda text: text)
        served = await self._served_banner(tmp_path, monkeypatch, f"watching CI {self.SECRET}")
        assert self.SECRET in served, (
            "the credential was scrubbed even with redact stubbed out -- the sink "
            "arms above are not measuring the sink"
        )

    @pytest.mark.asyncio
    async def test_a_clean_banner_is_untouched_and_the_store_is_not_rewritten(
        self, tmp_path
    ) -> None:
        """Negative control: the surviving repairs must not fire on benign text."""
        self._write_store(tmp_path, "watching CI — halt: {{STOP_FILE}}")
        before = (tmp_path / "autonudge.json").read_bytes()
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            assert svc._loops["abc123"].banner == "watching CI — halt: {{STOP_FILE}}"
            assert svc._store_dirty is False
            assert (tmp_path / "autonudge.json").read_bytes() == before
        finally:
            svc.stop()


class TestLoadRedactsTheLoopIdInBannerRepairWarnings:
    """The repair warnings must not echo a crafted loop ID either.

    The three banner-repair arms already withhold the banner VALUE, because the
    store is a file an agent can write directly. ``loop.id`` arrives from that
    same file, through the same ``NudgeLoop(**raw)`` construction, into the same
    ``logger.warning`` — and warnings reach the dashboard log ring and the
    ``/api/logs`` stream. Withholding one field from that sink while
    interpolating another from the identical source is not a policy, it is an
    oversight: the anti-pattern is stated on the SINK, not on a chosen field.

    Redacting a COPY is load-bearing. ``loop.id`` is the store key
    (``self._loops[loop.id]``) and the identity ``remove``/``update`` resolve, so
    rewriting the field itself would strand the loop as unaddressable — which the
    last test here pins.
    """

    SECRET = "AKIAIOSFODNN7EXAMPLE"

    def _write_store(self, tmp_path, loop_id, banner) -> None:
        (tmp_path / "autonudge.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "loops": [
                        {
                            "id": loop_id,
                            "slot_key": "chat-9-1",
                            "message": "the full multi-paragraph babysit instruction",
                            "idle_secs": 300,
                            "banner": banner,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    async def _load_with_logs(self, tmp_path, caplog):
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            with caplog.at_level("WARNING"):
                await svc.start()
        finally:
            svc.stop()
        return svc

    @pytest.mark.asyncio
    async def test_the_non_string_arm_does_not_echo_a_credential_bearing_id(
        self, tmp_path, caplog
    ) -> None:
        """A list banner reaches the type-only arm; its ID must still be scrubbed."""
        self._write_store(tmp_path, f"loop-{self.SECRET}", ["nudge"])
        await self._load_with_logs(tmp_path, caplog)
        assert self.SECRET not in caplog.text, "the non-string arm echoed the credential ID"
        assert "[REDACTED: credential]" in caplog.text

    @pytest.mark.asyncio
    async def test_the_oversized_arm_does_not_echo_a_credential_bearing_id(
        self, tmp_path, caplog
    ) -> None:
        """Its own arm: one break cannot validate three separate call sites."""
        self._write_store(tmp_path, f"loop-{self.SECRET}", "x" * (MAX_BANNER_CHARS + 1))
        await self._load_with_logs(tmp_path, caplog)
        assert self.SECRET not in caplog.text, "the oversized arm echoed the credential ID"
        assert "[REDACTED: credential]" in caplog.text

    @pytest.mark.asyncio
    async def test_the_correction_arm_does_not_echo_a_credential_bearing_id(
        self, tmp_path, caplog
    ) -> None:
        """The third site, reached only when the banner itself needed scrubbing."""
        self._write_store(tmp_path, f"loop-{self.SECRET}", f"watching CI {self.SECRET}")
        await self._load_with_logs(tmp_path, caplog)
        assert self.SECRET not in caplog.text, "the correction arm echoed the credential ID"
        assert "[REDACTED: credential]" in caplog.text

    @pytest.mark.asyncio
    async def test_an_exfiltrating_url_in_the_id_is_redacted_too(self, tmp_path, caplog) -> None:
        """BOTH redactors, not just the credential one.

        A fix applying only ``redact_credentials`` leaves this red: the payload
        keys on data embedded in a URL query, which is the other redactor's
        trigger. Shape taken from that function's own behaviour, mirroring
        ``TestLoadRedactsAPersistedBanner``'s exfil arm.
        """
        url = "https://example.com/upload?data=aGVsbG8gd29ybGQgdGhpcyBpcyBiYXNlNjQ="
        self._write_store(tmp_path, f"loop-{url}", ["nudge"])
        await self._load_with_logs(tmp_path, caplog)
        assert "aGVsbG8gd29ybGQ" not in caplog.text, "the ID's exfil payload reached the log"
        assert "[REDACTED: suspicious URL to example.com]" in caplog.text

    @pytest.mark.asyncio
    async def test_a_benign_id_is_still_named_in_the_warning(self, tmp_path, caplog) -> None:
        """NEGATIVE CONTROL: the warning must stay actionable.

        Without this, blanking or constant-ising every ID would pass every arm
        above while destroying the only thing that makes the warning useful —
        which loop to go and fix. Green on both sides of the change.
        """
        self._write_store(tmp_path, "abc123", ["nudge"])
        await self._load_with_logs(tmp_path, caplog)
        assert "abc123" in caplog.text, "the warning no longer names the offending loop"

    @pytest.mark.asyncio
    async def test_the_id_itself_is_not_rewritten(self, tmp_path, caplog) -> None:
        """The redaction is on a display COPY, never on the identity.

        ``loop.id`` keys ``self._loops`` and is what ``remove``/``update`` resolve,
        so a fix that scrubbed the field in place would leave the loop addressable
        only by a name no caller holds.

        SUPERSEDED IN PART by the addressing-field trust boundary (GPT 5.6,
        blocking): a credential-shaped ``id`` is now QUARANTINED at load rather
        than armed, because the REST serializer serves addressing fields
        unscrubbed. The guarantee this arm exists for is unchanged and still
        asserted -- the identity is never REWRITTEN -- and the row is re-emitted
        verbatim under ``quarantined``, which the body checks. The
        scrubbing-in-place failure mode it was written against would show up here
        as a loop keyed by a redacted id, and that is asserted directly.
        """
        raw_id = f"loop-{self.SECRET}"
        self._write_store(tmp_path, raw_id, ["nudge"])
        svc = await self._load_with_logs(tmp_path, caplog)
        assert raw_id not in svc._loops, "a credential-shaped id was armed"
        assert svc._loops == {}, "the refused loop was armed under some other key"
        assert not any("REDACTED" in k for k in svc._loops), "the identity was scrubbed in place"
        held = _held_aside_rows(tmp_path)
        assert len(held) == 1, "the quarantined row was not preserved"
        assert held[0]["id"] == raw_id, "the persisted identity was rewritten"


class TestPersistedBannerIsRedactedAtTheBroadcastSink:
    """A persisted banner must not reach dashboard clients unredacted.

    Both authorizer paths scrub a banner on the way IN, so a banner armed through
    REST or the workflow bridge is already clean in the store. The gap is the
    other producers — the same hand-edited ``autonudge.json`` and internal
    ``svc.add`` the type/whitespace guard exists for. Those never pass the
    authorizer, and ``_fire_dashboard_nudge`` puts the value in a row that is
    persisted and broadcast to every connected client. An input-side control does
    not discharge an output sink.

    ``_fire_slack_nudge`` already redacts before persisting its own replay row, so
    the dashboard path was the inconsistent sibling rather than this being a new
    rule.

    This is now the ONLY layer on the transcript path: the load-time scrub was
    subtracted, so a banner is left on disk as written and every egress scrubs it
    on the way out. The REST egress is covered by the ``_serialize`` denylist (see
    TestAPersistedBannerIsScrubbedAtEverySinkNotAtLoad).
    """

    def _write_store(self, tmp_path, banner: str) -> None:
        (tmp_path / "autonudge.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "loops": [
                        {
                            "id": "abc123",
                            "slot_key": "chat-1-1785",
                            "message": "the full multi-paragraph babysit instruction",
                            "idle_secs": 300,
                            "cycle_count": 3,
                            "banner": banner,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    @pytest.mark.asyncio
    async def test_restart_replay_of_a_credential_banner_is_redacted(self, tmp_path) -> None:
        """End-to-end: store -> _load -> _fire_dashboard_nudge -> broadcast row.

        This arm now ISOLATES the sink rather than merely confirming an invariant:
        the load-time scrub was subtracted, so the raw credential really does
        travel from disk into the fire path and the sink is the only thing that
        can stop it.
        """
        secret = "AKIAIOSFODNN7EXAMPLE"
        self._write_store(tmp_path, f"watching CI {secret}")
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            loaded = svc._loops["abc123"]
            assert secret in loaded.banner, (
                "precondition: the raw banner must survive _load, otherwise this arm "
                "is not measuring the sink"
            )
            row, prompt = await _fire(loaded)
        finally:
            svc.stop()

        assert secret not in row, "the raw credential reached the broadcast row"
        assert "[REDACTED: credential]" in row
        assert "watching CI" in row, "redaction ate the whole banner"
        assert row.startswith("[auto-nudge cycle 4]\n"), "the cycle prefix was lost"
        # The prompt is a different consumer and is not a broadcast sink; the
        # model must still receive its instruction intact.
        assert loaded.message in prompt

    @pytest.mark.asyncio
    async def test_a_clean_banner_is_untouched_by_the_redaction(self) -> None:
        """Negative control on the redaction: it must not fire on benign text.

        Without this, replacing the banner with a constant would pass the test
        above. Also pins that ``{{STOP_FILE}}`` substitution still happens and
        survives the scan — the redactors run AFTER it.
        """
        loop = _loop(banner="watching CI — halt: {{STOP_FILE}}", stop_sentinel_path="/tmp/.stop-x")
        row, prompt = await _fire(loop)
        assert row == f"[auto-nudge cycle {loop.cycle_count + 1}]\nwatching CI — halt: /tmp/.stop-x"
        assert "REDACTED" not in row
        assert loop.message in prompt

    @pytest.mark.asyncio
    async def test_the_no_banner_row_is_redacted_like_the_slack_sibling(self) -> None:
        """The row built from ``message`` is scrubbed at the sink, the prompt is not.

        Raised by the Design and First Principles lanes as the counted unfixed
        sibling. Fixed rather than tracked: ``_fire_slack_nudge`` already scrubs
        the whole ``tagged`` before persisting its replay row, so the dashboard
        path was the odd one out.

        Only the store-bypassing producers can reach this: a REST/MCP write is
        already scrubbed by ``authorize_autonudge_write``, so this secret has to
        arrive via a hand-edited store, exactly as ``_loop`` simulates.

        The prompt assertion is the load-bearing half. Scrubbing ``tagged`` would
        corrupt the instruction the model receives, which is the one thing the
        nudge exists to deliver — so the two must differ here.
        """
        secret = "AKIAIOSFODNN7EXAMPLE"
        loop = _loop(message=f"instruction {secret}", banner="")
        row, prompt = await _fire(loop)
        assert secret not in row, "the broadcast row carries an unredacted credential"
        assert "REDACTED" in row
        assert prompt == f"[auto-nudge cycle {loop.cycle_count + 1}]\ninstruction {secret}"
        assert row != prompt, "the prompt must keep the full instruction"

    @pytest.mark.asyncio
    async def test_a_clean_no_banner_row_stays_byte_identical(self) -> None:
        """The round-1 acceptance bar, still met after the scrub above.

        Its own arm because the scrub could have been implemented in a way that
        rewrites every row (a placeholder, a truncation); this pins that a
        message with nothing credential-shaped in it is passed through unchanged,
        so no existing loop's transcript changes.
        """
        loop = _loop(message="run the next cycle", banner="")
        row, prompt = await _fire(loop)
        assert row == f"[auto-nudge cycle {loop.cycle_count + 1}]\nrun the next cycle"
        assert row == prompt

    @pytest.mark.asyncio
    async def test_an_exfiltrating_url_in_a_banner_is_redacted(self) -> None:
        """The second redactor is wired too, not just the credential one.

        Its own arm because one break-arm cannot validate two calls: removing
        only ``redact_exfiltration_urls`` leaves the credential test green.

        The payload shape is taken from the function's OWN behaviour rather than
        assumed: ``redact_exfiltration_urls`` does not key on a host denylist —
        a bare ``https://webhook.site/abc123`` is a no-op — it keys on DATA
        EMBEDDED IN THE QUERY. A long base64 blob trips it, and verified
        separately that ``redact_credentials`` alone leaves this string
        untouched, so this arm isolates the exfiltration call.
        """
        loop = _loop(
            banner="report to https://example.com/upload?data=aGVsbG8gd29ybGQgdGhpcyBpcyBiYXNlNjQ="
        )
        row, _prompt = await _fire(loop)
        assert "aGVsbG8gd29ybGQ" not in row, "the exfiltrating URL reached the broadcast row"
        assert "[REDACTED: suspicious URL to example.com]" in row


class TestPersistenceRoundTrip:
    """``_load`` filters unknown keys, which is what makes this additive.

    Both directions are pinned because only one of them is obvious. Old store /
    new code is the upgrade everyone will hit. New store / OLD code is the
    DOWNGRADE — a user reverting the release — and it is the direction that
    would justify bumping ``_STORE_VERSION`` if it raised. It does not, so the
    version stays at 1 rather than signalling a break that is not happening.
    """

    def _write_store(self, tmp_path, loops: list[dict]) -> None:
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": loops}), encoding="utf-8"
        )

    @pytest.mark.asyncio
    async def test_old_store_without_banner_loads(self, tmp_path) -> None:
        self._write_store(
            tmp_path,
            [{"id": "abc123", "slot_key": "chat-9-1", "message": "go", "idle_secs": 300}],
        )
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            assert svc._loops["abc123"].banner == ""
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_new_store_with_banner_loads_and_survives_a_rewrite(self, tmp_path) -> None:
        self._write_store(
            tmp_path,
            [
                {
                    "id": "abc123",
                    "slot_key": "chat-9-1",
                    "message": "go",
                    "idle_secs": 300,
                    "banner": "cycle ran",
                }
            ],
        )
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            assert svc._loops["abc123"].banner == "cycle ran"
            # The value must round-trip through the store, not merely load: a
            # field read but dropped from ``asdict`` would be silently lost on
            # the first persist.
            await svc._persist_locked()
            on_disk = json.loads((tmp_path / "autonudge.json").read_text(encoding="utf-8"))
            assert on_disk["loops"][0]["banner"] == "cycle ran"
            assert on_disk["version"] == 1, "the store version must not have been bumped"
        finally:
            svc.stop()

    def test_a_banner_bearing_row_loads_against_a_pre_field_definition(self) -> None:
        """DOWNGRADE simulation: old code reading a new store.

        Reproduces ``_load``'s construction against a ``NudgeLoop`` definition
        that predates ``banner``, which is what a reverted build would have. The
        key is filtered rather than passed, so the row degrades to the verbose
        display instead of raising ``TypeError`` and taking every loop with it.
        """

        @dataclass
        class PreBannerNudgeLoop:
            id: str
            slot_key: str
            message: str
            idle_secs: int = 60

        raw = {
            "id": "abc123",
            "slot_key": "chat-9-1",
            "message": "go",
            "idle_secs": 300,
            "banner": "cycle ran",
        }
        old_loop = PreBannerNudgeLoop(
            **{k: raw[k] for k in raw if k in PreBannerNudgeLoop.__dataclass_fields__}
        )
        assert old_loop.message == "go"
        assert not hasattr(old_loop, "banner")
        # Negative control: the filter is doing the work, not the dataclass.
        with pytest.raises(TypeError):
            PreBannerNudgeLoop(**raw)  # type: ignore[arg-type]


# ── REST surface: cap, type, and pass-through ──


class TestRestSurface:
    def _app(self, monkeypatch, fake_svc):
        from aiohttp import web

        from kiro_crew.dashboard.handlers import autonudge as _handler

        monkeypatch.setattr(_handler, "_autonudge_get", lambda: fake_svc)
        state = MagicMock()
        state._slots = {"chat-1-123": MagicMock(workspace="default")}
        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/autonudge", _handler.api_autonudge_start)
        app.router.add_patch("/api/autonudge/{loop_id}", _handler.api_autonudge_update)
        return app

    def _svc(self, **loop_kw):
        svc = MagicMock()
        loop = NudgeLoop(id="loop-1", slot_key="chat-1-123", message="go", **loop_kw)
        svc.add = AsyncMock(return_value=loop)
        svc.update = AsyncMock(return_value=loop)
        # Harness parity: the real ``AutoNudgeService`` exposes ``list_all``, and
        # the update authorizer uses it to resolve an opaque ``loop_id`` to its
        # slot key before deciding whether a banner is supported there. A bare
        # ``MagicMock`` would return a non-iterable mock and turn that lookup into
        # a 500 that says nothing about the code under test.
        svc.list_all = lambda: [loop]
        svc.get_by_id = lambda _id, _rows=[loop]: next(
            (r for r in _rows if getattr(r, "id", None) == _id), None
        )
        return svc

    @pytest.mark.asyncio
    async def test_over_cap_banner_is_400_and_arms_nothing(self, monkeypatch) -> None:
        from aiohttp.test_utils import TestClient, TestServer

        svc = self._svc()
        async with TestClient(TestServer(self._app(monkeypatch, svc))) as client:
            resp = await client.post(
                "/api/autonudge",
                json={
                    "slot_key": "chat-1-123",
                    "message": "go",
                    "banner": "b" * (MAX_BANNER_CHARS + 1),
                },
            )
            assert resp.status == 400
            assert "banner" in (await resp.json())["error"]
        svc.add.assert_not_awaited(), "an over-cap banner still armed the loop"

    @pytest.mark.asyncio
    async def test_at_cap_banner_is_accepted(self, monkeypatch) -> None:
        """Negative control on the boundary: the cap must be off-by-one correct.

        Without this, ``>=`` would pass the over-cap test above while rejecting
        every legitimate banner of exactly the documented length.
        """
        from aiohttp.test_utils import TestClient, TestServer

        svc = self._svc()
        async with TestClient(TestServer(self._app(monkeypatch, svc))) as client:
            resp = await client.post(
                "/api/autonudge",
                json={"slot_key": "chat-1-123", "message": "go", "banner": "b" * MAX_BANNER_CHARS},
            )
            assert resp.status == 200
        assert len(svc.add.await_args.kwargs["banner"]) == MAX_BANNER_CHARS

    @pytest.mark.asyncio
    async def test_non_string_banner_is_400_not_500(self, monkeypatch) -> None:
        from aiohttp.test_utils import TestClient, TestServer

        svc = self._svc()
        async with TestClient(TestServer(self._app(monkeypatch, svc))) as client:
            for bad in (5, ["x"], {"a": 1}):
                resp = await client.post(
                    "/api/autonudge",
                    json={"slot_key": "chat-1-123", "message": "go", "banner": bad},
                )
                assert resp.status == 400, f"banner={bad!r} gave {resp.status}"
        svc.add.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_absent_banner_reaches_the_service_as_empty(self, monkeypatch) -> None:
        """The handler passes ``None`` when the key is absent; "" must be stored.

        Pinned because ``body.get("banner")`` yields ``None``, and a ``None``
        landing on a ``str`` field would serialize as JSON ``null`` and make the
        truthiness check in the fire path depend on the caller's JSON shape.
        """
        from aiohttp.test_utils import TestClient, TestServer

        svc = self._svc()
        async with TestClient(TestServer(self._app(monkeypatch, svc))) as client:
            resp = await client.post(
                "/api/autonudge", json={"slot_key": "chat-1-123", "message": "go"}
            )
            assert resp.status == 200
        assert svc.add.await_args.kwargs["banner"] == ""

    @pytest.mark.asyncio
    async def test_patch_can_quiet_a_running_loop(self, monkeypatch) -> None:
        from aiohttp.test_utils import TestClient, TestServer

        svc = self._svc(banner="cycle ran")
        async with TestClient(TestServer(self._app(monkeypatch, svc))) as client:
            resp = await client.patch("/api/autonudge/loop-1", json={"banner": "cycle ran"})
            assert resp.status == 200
            assert (await resp.json())["loop"]["banner"] == "cycle ran"
        assert svc.update.await_args.kwargs["banner"] == "cycle ran"
        # A banner-only patch must not silently rewrite the instruction.
        assert svc.update.await_args.kwargs["message"] is None

    @pytest.mark.asyncio
    async def test_patch_over_cap_banner_is_400_and_updates_nothing(self, monkeypatch) -> None:
        """The update path is a bypass unless it enforces the same cap."""
        from aiohttp.test_utils import TestClient, TestServer

        svc = self._svc()
        async with TestClient(TestServer(self._app(monkeypatch, svc))) as client:
            resp = await client.patch(
                "/api/autonudge/loop-1", json={"banner": "b" * (MAX_BANNER_CHARS + 1)}
            )
            assert resp.status == 400
        svc.update.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_patch_omitting_banner_leaves_it_alone(self, monkeypatch) -> None:
        """``None`` means "not supplied" on the update path, not "clear it".

        Distinct from the arm path, where ``None`` normalizes to "". Every other
        PATCH-issuing caller (the goal popover sends idle_secs/active on each
        edit) would otherwise erase a banner it never mentioned.
        """
        from aiohttp.test_utils import TestClient, TestServer

        svc = self._svc()
        async with TestClient(TestServer(self._app(monkeypatch, svc))) as client:
            resp = await client.patch("/api/autonudge/loop-1", json={"idle_secs": 120})
            assert resp.status == 200
        assert svc.update.await_args.kwargs["banner"] is None

    @pytest.mark.asyncio
    async def test_patch_empty_string_banner_clears_it(self, monkeypatch) -> None:
        from aiohttp.test_utils import TestClient, TestServer

        svc = self._svc()
        async with TestClient(TestServer(self._app(monkeypatch, svc))) as client:
            resp = await client.patch("/api/autonudge/loop-1", json={"banner": "   "})
            assert resp.status == 200
        assert svc.update.await_args.kwargs["banner"] == ""

    @pytest.mark.asyncio
    async def test_a_banner_redaction_grows_past_the_cap_is_rejected_on_add(
        self, monkeypatch
    ) -> None:
        """The cap must bind the value STORED, not the one received.

        ``[REDACTED: credential]`` is 22 characters and replaces a 20-character
        AWS access key ID, so an at-cap banner carrying one measures 502 once
        scrubbed. Checking the cap only before redaction accepts it with a 200 and
        persists a value that breaches the bound — which the loader then blanks on
        a later boot, losing the operator's banner with no error ever surfaced.
        A 400 at the door is the visible answer.
        """
        from aiohttp.test_utils import TestClient, TestServer

        secret = "AKIAIOSFODNN7EXAMPLE"
        at_cap_with_secret = "b" * (MAX_BANNER_CHARS - len(secret)) + secret
        assert len(at_cap_with_secret) == MAX_BANNER_CHARS, "fixture is not at the cap"
        svc = self._svc()
        async with TestClient(TestServer(self._app(monkeypatch, svc))) as client:
            resp = await client.post(
                "/api/autonudge",
                json={
                    "slot_key": "chat-1-123",
                    "message": "go",
                    "banner": at_cap_with_secret,
                },
            )
            assert resp.status == 400, "a banner that redaction grows past the cap was accepted"
            assert "banner" in (await resp.json())["error"]
        svc.add.assert_not_awaited(), "the over-cap-after-redaction banner still armed the loop"

    @pytest.mark.asyncio
    async def test_a_banner_redaction_grows_past_the_cap_is_rejected_on_update(
        self, monkeypatch
    ) -> None:
        """Its own arm: add and update carry SEPARATE cap checks.

        Fixing only the add path leaves this red, which is the point — one break
        cannot validate two independent call sites.
        """
        from aiohttp.test_utils import TestClient, TestServer

        secret = "AKIAIOSFODNN7EXAMPLE"
        at_cap_with_secret = "b" * (MAX_BANNER_CHARS - len(secret)) + secret
        svc = self._svc()
        async with TestClient(TestServer(self._app(monkeypatch, svc))) as client:
            resp = await client.patch("/api/autonudge/loop-1", json={"banner": at_cap_with_secret})
            assert resp.status == 400, "the update path accepted a banner that grows past the cap"
            assert "banner" in (await resp.json())["error"]
        svc.update.assert_not_awaited(), "the over-cap-after-redaction banner still updated"

    @pytest.mark.asyncio
    async def test_an_at_cap_banner_is_accepted_on_update(self, monkeypatch) -> None:
        """Negative control on the UPDATE boundary specifically.

        ``test_at_cap_banner_is_accepted`` pins this for POST, which enters
        ``authorize_and_add_nudge``; PATCH enters ``authorize_and_update_nudge``,
        a SEPARATE validator with its own cap checks. Without a PATCH-side
        boundary control, a ``>=`` in the update path's post-redaction check would
        reject every legitimate at-cap banner on update while every POST test
        stayed green -- a gap a break-arm found rather than a review.
        """
        from aiohttp.test_utils import TestClient, TestServer

        at_cap = "b" * MAX_BANNER_CHARS
        svc = self._svc()
        async with TestClient(TestServer(self._app(monkeypatch, svc))) as client:
            resp = await client.patch("/api/autonudge/loop-1", json={"banner": at_cap})
            assert resp.status == 200, "an at-cap banner was rejected on update"
        assert len(svc.update.await_args.kwargs["banner"]) == MAX_BANNER_CHARS

    @pytest.mark.asyncio
    async def test_an_at_cap_banner_redaction_shrinks_is_still_accepted(self, monkeypatch) -> None:
        """Negative control: redaction that does NOT grow the value stays accepted.

        The exfiltration placeholder replaces the whole matched URL and here
        measures 27 characters SHORTER, so this at-cap banner ends well inside the
        bound. Without this, a fix that rejected any banner whose redaction
        changed it — or that simply lowered the cap — would pass the two arms
        above while refusing legitimate input. Complements
        ``test_at_cap_banner_is_accepted``, which pins the clean at-cap case.
        """
        from aiohttp.test_utils import TestClient, TestServer

        url = "https://a.co/upload?data=aGVsbG8gd29ybGQgdGhpcyBpcyBiYXNlNjQ="
        at_cap_with_url = "y" * (MAX_BANNER_CHARS - len(url)) + url
        assert len(at_cap_with_url) == MAX_BANNER_CHARS, "fixture is not at the cap"
        svc = self._svc()
        async with TestClient(TestServer(self._app(monkeypatch, svc))) as client:
            resp = await client.post(
                "/api/autonudge",
                json={"slot_key": "chat-1-123", "message": "go", "banner": at_cap_with_url},
            )
            assert resp.status == 200, "a banner that redaction shrinks was wrongly rejected"
        stored = svc.add.await_args.kwargs["banner"]
        assert len(stored) <= MAX_BANNER_CHARS
        assert "aGVsbG8gd29ybGQ" not in stored, "the exfil payload was stored unredacted"


class TestBannerIsRedactedLikeTheMessage:
    """A banner is persisted and broadcast, so it is a leak surface too.

    Not in the original design sketch: it was scoped as display-only, and
    display-only is exactly what makes it easy to forget. The message redaction
    exists because the value is persisted and re-injected; a banner is persisted
    and rendered into every connected browser's transcript, which is the same
    exposure with a shorter string.
    """

    @pytest.mark.asyncio
    async def test_arm_path_redacts_a_credential_in_a_banner(self, monkeypatch) -> None:
        from aiohttp.test_utils import TestClient, TestServer

        svc = MagicMock()
        svc.add = AsyncMock(
            return_value=NudgeLoop(id="loop-1", slot_key="chat-1-123", message="go")
        )
        app = TestRestSurface()._app(monkeypatch, svc)
        secret = "AKIAIOSFODNN7EXAMPLE"
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/autonudge",
                json={"slot_key": "chat-1-123", "message": "go", "banner": f"key {secret}"},
            )
            assert resp.status == 200
        assert secret not in svc.add.await_args.kwargs["banner"]

    @pytest.mark.asyncio
    async def test_update_path_redacts_a_credential_in_a_banner(self, monkeypatch) -> None:
        """The arm-path guard is a trivial bypass unless PATCH enforces it too."""
        from aiohttp.test_utils import TestClient, TestServer

        svc = MagicMock()
        svc.update = AsyncMock(
            return_value=NudgeLoop(id="loop-1", slot_key="chat-1-123", message="go")
        )
        app = TestRestSurface()._app(monkeypatch, svc)
        secret = "AKIAIOSFODNN7EXAMPLE"
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/autonudge/loop-1", json={"banner": f"key {secret}"})
            assert resp.status == 200
        assert secret not in svc.update.await_args.kwargs["banner"]


class TestMonitorToolsCanSetTheBanner:
    """F2: the arming surface that produced the measured harm must be able to set it.

    design-review and first-principles-review converge: counted setters = 0, so
    the field ships dead and the 51.8%-of-session bloat continues for every loop
    armed the normal way. The lane states the remedy as a disjunction -- wire ONE
    existing arming surface -- and names two: the MCP ``monitor_start`` schema and
    the dashboard popover. The MCP tools are the smaller change AND are what armed
    the babysit loop the PR's own measurement came from, so wiring them is what
    makes the shipped remedy reachable by the measured offender.

    Routed through the SAME authorised seam the REST endpoints use
    (``authorize_and_add_nudge`` / ``authorize_and_update_nudge``), so the cap and
    the two redaction passes are the existing ones -- no second validation path.
    """

    SECRET = "AKIAIOSFODNN7EXAMPLE"

    @pytest.fixture()
    def default_install(self, monkeypatch):
        """No accepted identity source, so the stateless tools RETURN a directive
        rather than short-circuiting. Mirrors the fixture in
        ``test_autonudge_stop_auth.py``, which is where these tools' contract
        tests live."""
        monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "")
        return monkeypatch

    def test_monitor_start_carries_a_banner_into_the_directive(self, default_install) -> None:
        """Fails on the unmodified tree: ``banner`` is not a declared field, so
        ``validate_tool_args`` rejects the call outright."""
        result = _call_tool_inner(
            "monitor_start", {"message": "watch CI until green", "banner": "watching CI"}
        )
        args = session_directive.decode(result, "monitor_start")
        assert args.get("banner") == "watching CI"

    def test_monitor_update_carries_a_banner_into_the_patch(self, default_install) -> None:
        """Its own arm: add and update are separate validators and separate handlers."""
        result = _call_tool_inner("monitor_update", {"banner": "still watching"})
        args = session_directive.decode(result, "monitor_update")
        assert args["patch"].get("banner") == "still watching"

    def test_monitor_update_can_clear_the_banner(self, default_install) -> None:
        """An empty string is a REQUEST TO CLEAR, not an omission.

        ``message`` rejects blank because a loop with no instruction cannot fire,
        but a loop with no banner is the default state, so blank must round-trip
        rather than be dropped as "unchanged" -- otherwise a banner set once can
        never be removed without tearing the loop down.
        """
        result = _call_tool_inner("monitor_update", {"banner": ""})
        args = session_directive.decode(result, "monitor_update")
        assert "banner" in args["patch"], "an empty banner was silently dropped"
        assert args["patch"]["banner"] == ""

    def test_a_banner_over_the_cap_is_rejected_at_the_tool(self, default_install) -> None:
        """The schema bound is the entry filter; the authz seam still owns the
        post-redaction cap.

        The message is asserted, not just the exception type: before the field is
        declared this raises ``ValidationError`` too -- for "unknown field" -- so a
        bare ``pytest.raises`` would pass vacuously and prove nothing.
        """
        with pytest.raises(ValidationError) as excinfo:
            _call_tool_inner(
                "monitor_start",
                {"message": "watch CI", "banner": "b" * (MAX_BANNER_CHARS + 1)},
            )
        assert "unknown field" not in str(
            excinfo.value
        ), "rejected because the field is undeclared, not because it is over the cap"

    def test_an_at_cap_banner_is_accepted_at_the_tool(self, default_install) -> None:
        """Negative control on the boundary: a ``>=`` in the schema bound would
        reject every legitimate at-cap banner and this is what catches it."""
        result = _call_tool_inner(
            "monitor_start", {"message": "watch CI", "banner": "b" * MAX_BANNER_CHARS}
        )
        args = session_directive.decode(result, "monitor_start")
        assert len(args["banner"]) == MAX_BANNER_CHARS

    def test_omitting_the_banner_leaves_the_payload_shape_untouched(self, default_install) -> None:
        """Negative control, and the reason the field is added CONDITIONALLY.

        ``test_autonudge_stop_auth.py`` pins the monitor_start payload with EXACT
        dict equality, so emitting ``banner`` unconditionally would break a
        contract test belonging to another file. A caller that sets no banner must
        see the payload it saw before.
        """
        result = _call_tool_inner("monitor_start", {"message": "watch CI", "max_cycles": 5})
        args = session_directive.decode(result, "monitor_start")
        assert args == {
            "message": "watch CI",
            "idle_secs": 300,
            "max_cycles": 5,
            "max_runtime_secs": 0,
        }, "the no-banner payload shape changed"

    @pytest.mark.asyncio
    async def test_the_applier_hands_the_banner_to_the_authorised_seam(self) -> None:
        """The end of the wire: the consumer must PASS it, not just accept it.

        Both applier call sites name every kwarg explicitly -- there is no
        ``**patch`` splat -- so omitting it here would advertise a field that is
        silently dropped, which is the defect class already fixed once on this PR
        in the slot-close restore path.
        """
        from kiro_crew.dashboard import session_directive_apply as sda

        svc = MagicMock()
        authz = AsyncMock(return_value=(SimpleNamespace(id="loop-1"), None, 200))
        with (
            patch.object(sda, "_binding", return_value="chat-9-1"),
            patch("kiro_crew.autonudge.get_instance", return_value=svc),
            patch("kiro_crew.autonudge_authz.authorize_and_add_nudge", authz),
        ):
            await sda._monitor_start(
                MagicMock(), "chat-9-1", {"message": "go", "banner": "watching CI"}
            )
        assert authz.await_args.kwargs["banner"] == "watching CI"


class TestChannelBoundLoopsRefuseABanner:
    """A banner is dead config on a channel-bound loop, so the authorizer says so.

    ``_fire`` routes a channel key to ``_fire_slack_nudge`` / ``_fire_discord_nudge``
    / ``_fire_webex_nudge``; none of them reads ``loop.banner``, and both read
    sites live inside ``_fire_dashboard_nudge``. Accepting and PERSISTING a field
    those paths can never honour is a silent no-op the caller cannot detect --
    worse than a refusal, because the loop arms and looks configured.

    Both chokepoints are covered on purpose. Refusing only on ``add`` would leave
    ``PATCH /api/autonudge/{id}`` able to set the same dead field on the same
    loop, so the hole would move rather than close.
    """

    @pytest.fixture
    def audits(self, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
        """Capture SEL events rather than writing them (mirrors the authz suite)."""
        events: list[dict] = []
        monkeypatch.setattr(
            authz,
            "sel",
            lambda: SimpleNamespace(log_tool_invocation=lambda **kw: events.append(kw)),
        )
        return events

    @staticmethod
    def _state() -> SimpleNamespace:
        return SimpleNamespace(_slots={}, sessions=None, channel_transports={})

    @pytest.mark.asyncio
    async def test_add_refuses_a_banner_on_a_ROUTABLE_channel_session(
        self, audits: list[dict]
    ) -> None:
        """The arm that demonstrates the DEFECT, not merely the fix.

        A routable session is required for that: without one the add is refused
        with a 404 long before the banner is looked at, so the request would
        change colour when the guard lands while never having proved that a
        banner was accepted and PERSISTED. Here the pre-fix path reaches
        ``svc.add`` and carries ``banner`` into the store.
        """
        channel = object()  # identity-stable: the admission check compares `is`
        state = SimpleNamespace(
            _slots={},
            sessions=SimpleNamespace(get_channel=lambda _k: channel),
            channel_transports={},
        )
        svc = MagicMock()
        svc.add = AsyncMock(
            return_value=NudgeLoop(id="loop-1", slot_key="slack:1785", message="go")
        )
        loop, error, status = await authz.authorize_and_add_nudge(
            svc=svc,
            state=state,
            slot_key="slack:1785",
            message="watch the build",
            banner="watching CI",
            source="dashboard",
        )
        assert status == 400, (
            f"a banner was accepted for a routable channel session (status {status}); "
            f"svc.add kwargs = {svc.add.await_args}"
        )
        assert "channel" in (error or ""), error
        svc.add.assert_not_awaited(), "the loop armed with a banner its fire path cannot read"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("slot_key", ["slack:1785", "discord:agent:direct:u1", "webex:room1"])
    async def test_add_refuses_a_banner_on_every_channel_namespace(
        self, slot_key: str, audits: list[dict]
    ) -> None:
        """Coverage that the guard fires for all three namespaces, not just slack.

        Honest about its own limits: with no routable session these requests are
        refused anyway (404) before the guard lands, so this arm proves the guard
        fires and returns the CHANNEL reason for each namespace -- the proof that
        a banner was otherwise accepted lives in the routable-session arm above.
        """
        svc = MagicMock()
        svc.add = AsyncMock()
        loop, error, status = await authz.authorize_and_add_nudge(
            svc=svc,
            state=self._state(),
            slot_key=slot_key,
            message="watch the build",
            banner="watching CI",
            source="dashboard",
        )
        assert status == 400, f"the guard did not fire for {slot_key} (status {status})"
        assert loop is None
        assert "channel" in (error or ""), error
        svc.add.assert_not_awaited(), "the loop armed despite the refusal"
        assert audits and audits[-1]["outcome"] == "denied", "the refusal skipped the SEL audit"

    @pytest.mark.asyncio
    async def test_add_still_accepts_a_banner_on_a_dashboard_slot(self, audits: list[dict]) -> None:
        """Negative control: the guard must not fire on the surface that reads it."""
        slot = MagicMock(workspace="default")
        state = SimpleNamespace(_slots={"chat-1-123": slot}, sessions=None, channel_transports={})
        svc = MagicMock()
        svc.add = AsyncMock(
            return_value=NudgeLoop(id="loop-1", slot_key="chat-1-123", message="go")
        )
        loop, error, status = await authz.authorize_and_add_nudge(
            svc=svc,
            state=state,
            slot_key="chat-1-123",
            message="go",
            banner="watching CI",
            source="dashboard",
        )
        assert status == 200, f"a dashboard banner was refused: {error}"
        assert error is None
        svc.add.assert_awaited()

    @pytest.mark.asyncio
    async def test_add_accepts_a_blank_banner_on_a_channel_loop(self, audits: list[dict]) -> None:
        """Blank means "no banner", so it must not be read as setting one.

        Without this arm the guard could be written as ``banner is not None`` and
        still pass the refusal test above, while breaking every channel-bound
        caller that passes the default ``banner=""``.
        """
        svc = MagicMock()
        svc.add = AsyncMock(
            return_value=NudgeLoop(id="loop-1", slot_key="slack:1785", message="go")
        )
        loop, error, status = await authz.authorize_and_add_nudge(
            svc=svc,
            state=self._state(),
            slot_key="slack:1785",
            message="go",
            banner="   ",
            source="dashboard",
        )
        assert status != 400, f"a blank banner was treated as setting one: {error}"

    @pytest.mark.asyncio
    async def test_update_refuses_a_banner_on_a_channel_bound_loop(
        self, audits: list[dict]
    ) -> None:
        """The update path holds only an opaque ``loop_id``, so it resolves first."""
        stored = NudgeLoop(id="loop-9", slot_key="slack:1785", message="go")
        svc = MagicMock()
        svc.list_all = lambda: [stored]
        svc.get_by_id = lambda _id, _rows=[stored]: next(
            (r for r in _rows if getattr(r, "id", None) == _id), None
        )
        svc.update = AsyncMock()
        loop, error, status = await authz.authorize_and_update_nudge(
            svc=svc,
            loop_id="loop-9",
            banner="watching CI",
            source="dashboard",
        )
        assert status == 400, "a banner was accepted for a channel-bound loop via PATCH"
        assert loop is None
        assert "channel" in (error or ""), error
        svc.update.assert_not_awaited(), "the patch applied despite the refusal"

    @pytest.mark.asyncio
    async def test_update_still_accepts_a_banner_on_a_dashboard_loop(
        self, audits: list[dict]
    ) -> None:
        """Negative control for the resolved-lookup arm."""
        stored = NudgeLoop(id="loop-9", slot_key="chat-1-123", message="go")
        svc = MagicMock()
        svc.list_all = lambda: [stored]
        svc.get_by_id = lambda _id, _rows=[stored]: next(
            (r for r in _rows if getattr(r, "id", None) == _id), None
        )
        svc.update = AsyncMock(return_value=stored)
        loop, error, status = await authz.authorize_and_update_nudge(
            svc=svc,
            loop_id="loop-9",
            banner="watching CI",
            source="dashboard",
        )
        assert status == 200, f"a dashboard banner was refused on update: {error}"
        svc.update.assert_awaited()

    @pytest.mark.asyncio
    async def test_update_resolves_the_loop_BY_ID_not_by_position(self, audits: list[dict]) -> None:
        """Two loops, and the channel-bound one is FIRST in the list.

        Added because a break-arm exposed that the single-loop cases cannot see
        the difference: with one loop in the store, "match the id" and "take the
        first" select the same object, so neither arm proves the lookup is keyed
        on anything. Here a positional lookup refuses a perfectly legal dashboard
        patch by reading the wrong loop's slot key.
        """
        channel_loop = NudgeLoop(id="loop-1", slot_key="slack:1785", message="go")
        target = NudgeLoop(id="loop-9", slot_key="chat-1-123", message="go")
        svc = MagicMock()
        svc.list_all = lambda: [channel_loop, target]
        svc.get_by_id = lambda _id, _rows=[channel_loop, target]: next(
            (r for r in _rows if getattr(r, "id", None) == _id), None
        )
        svc.update = AsyncMock(return_value=target)
        loop, error, status = await authz.authorize_and_update_nudge(
            svc=svc,
            loop_id="loop-9",
            banner="watching CI",
            source="dashboard",
        )
        assert (
            status == 200
        ), f"the lookup read the wrong loop's slot key and refused a dashboard patch: {error}"
        svc.update.assert_awaited()

    @pytest.mark.asyncio
    async def test_update_refuses_when_the_TARGET_is_the_channel_loop(
        self, audits: list[dict]
    ) -> None:
        """The mirror of the arm above, so the pair covers both directions.

        Same two-loop store, but the id names the channel-bound one. Without this
        the id-keyed lookup could be satisfied by a rule that always picks the
        dashboard loop.
        """
        channel_loop = NudgeLoop(id="loop-1", slot_key="slack:1785", message="go")
        dashboard_loop = NudgeLoop(id="loop-9", slot_key="chat-1-123", message="go")
        svc = MagicMock()
        svc.list_all = lambda: [channel_loop, dashboard_loop]
        svc.get_by_id = lambda _id, _rows=[channel_loop, dashboard_loop]: next(
            (r for r in _rows if getattr(r, "id", None) == _id), None
        )
        svc.update = AsyncMock()
        loop, error, status = await authz.authorize_and_update_nudge(
            svc=svc,
            loop_id="loop-1",
            banner="watching CI",
            source="dashboard",
        )
        assert status == 400, "a banner was accepted for the channel-bound target"
        assert "channel" in (error or ""), error
        svc.update.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_update_of_an_unknown_id_is_not_refused_as_channel_bound(
        self, audits: list[dict]
    ) -> None:
        """An unresolvable id must not be *guessed* as channel-bound.

        The lookup can legitimately find nothing (the loop was removed between
        request and authorization). Inventing a refusal there would turn a 404
        into a misleading 400 about channels.
        """
        svc = MagicMock()
        svc.list_all = lambda: []
        svc.get_by_id = lambda _id: None
        svc.update = AsyncMock(return_value=None)
        loop, error, status = await authz.authorize_and_update_nudge(
            svc=svc,
            loop_id="loop-missing",
            banner="watching CI",
            source="dashboard",
        )
        assert status != 400 or "channel" not in (
            error or ""
        ), f"an unknown id was reported as channel-bound: {error}"

    # ── A mis-composed credential policy must DENY with an audit, not raise ──

    @staticmethod
    def _mis_compose(monkeypatch: pytest.MonkeyPatch) -> None:
        """Make ``redact_via_context`` fail-close exactly as a mis-composed host does."""
        from kiro_crew.platform import PlatformCompositionError

        def _raise(_text: str) -> str:
            raise PlatformCompositionError("companion credential policy unreadable")

        monkeypatch.setattr(authz, "redact_via_context", _raise)

    @pytest.mark.asyncio
    async def test_arm_denies_with_503_and_audits_when_the_policy_cannot_compose(
        self, monkeypatch: pytest.MonkeyPatch, audits: list[dict]
    ) -> None:
        """GPT 5.6 (BLOCKING): the raise escaped normalisation before ``_deny``.

        ``redact_via_context`` is fail-closed and re-raises
        ``PlatformCompositionError``. ``normalize_banner`` does not catch it and
        neither did its callers, so on a host that declares a credential policy it
        could not compose a non-blank banner blew past this path's ``_deny`` -- the
        request failed with NO SEL event, which is the one thing every refusal here
        is supposed to guarantee.
        """
        self._mis_compose(monkeypatch)
        # A REAL slot: with empty ``_slots`` the arm path refuses with 404 "unknown
        # slot" long before the banner is normalised, so the test would pass on the
        # unfixed tree for the wrong reason and measure nothing.
        slot = MagicMock(workspace="default")
        state = SimpleNamespace(_slots={"chat-1-123": slot}, sessions=None, channel_transports={})
        svc = MagicMock()
        svc.add = AsyncMock()
        loop, error, status = await authz.authorize_and_add_nudge(
            svc=svc,
            state=state,
            slot_key="chat-1-123",
            message="watch the build",
            banner="watching CI",
            source="dashboard",
        )
        assert status == 503, f"a mis-composed policy did not yield a 503 (status {status})"
        assert loop is None
        assert audits and audits[-1]["outcome"] == "denied", "the refusal skipped the SEL audit"
        svc.add.assert_not_awaited(), "the loop armed despite an unusable credential policy"

    @staticmethod
    def _mis_compose_scrub(monkeypatch: pytest.MonkeyPatch) -> None:
        """Make the MESSAGE-comparison scrub fail-close, which is a different seam.

        ``scrub_loop_text`` is DEFINED in ``kiro_crew.autonudge`` and therefore calls
        THAT module's ``redact_via_context``. ``_mis_compose`` above patches the name
        bound in ``autonudge_authz``, which is what ``normalize_banner`` consults --
        so it cannot reach the message comparison at all, and using it here would
        stub something this path never calls and measure nothing.

        Patching only this seam also keeps the two paths independent: the banner
        normalisation still sees a WORKING policy, so a 503 from this test can only
        have come from the message comparison.
        """
        from kiro_crew.platform import PlatformCompositionError

        def _raise(_text: str) -> str:
            raise PlatformCompositionError("companion credential policy unreadable")

        monkeypatch.setattr(_an, "redact_via_context", _raise)

    @pytest.mark.asyncio
    async def test_update_with_a_message_denies_with_503_and_audits(
        self, monkeypatch: pytest.MonkeyPatch, audits: list[dict]
    ) -> None:
        """Opus 4.8 + GPT 5.6 (BLOCKING): the projection compare had no guard.

        The comparison added to stop the popover overwriting a stored instruction
        calls ``scrub_loop_text``, which is fail-closed. On a host whose credential
        policy cannot compose, that raise escaped BEFORE ``_deny`` and before the
        critical ``invoked`` audit, so a PATCH carrying a message died as an
        unaudited 500 -- the same "no SEL event at all" hole the banner block below
        already guards, reintroduced 27 lines above it.

        Reachable even though ``_load`` refuses persisted rows: ``svc.add`` never
        scrubs the message and the arm path never calls the scrub when the banner is
        empty, so a bannerless loop still lands in ``_loops`` on such a host.
        """
        self._mis_compose_scrub(monkeypatch)
        svc = MagicMock()
        svc.list_all = lambda: []
        svc.get_by_id = lambda _id: None
        # A REAL stored loop, so the comparison is actually reached: with
        # ``get_by_id`` returning None the guard short-circuits on ``current is not
        # None`` and never scrubs, which would pass on the unfixed tree for the
        # wrong reason.
        svc.get_by_id = lambda _id: SimpleNamespace(message="the original instruction")
        svc.update = AsyncMock(return_value=None)

        loop, error, status = await authz.authorize_and_update_nudge(
            svc=svc,
            loop_id="loop-1",
            message="anything at all",
            source="dashboard",
        )
        assert status == 503, f"a mis-composed policy did not yield a 503 (status {status})"
        assert loop is None
        assert audits and audits[-1]["outcome"] == "denied", "the refusal skipped the SEL audit"
        svc.update.assert_not_awaited(), "the update landed despite an unusable policy"

    @pytest.mark.asyncio
    async def test_the_refused_message_is_never_applied(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch, audits: list[dict]
    ) -> None:
        """The refusal must leave the stored message alone, asserted not assumed.

        ``_deny`` returns, so nothing downstream runs -- but that is control flow
        reasoning, and the point of a control is to observe it. Measured through the
        real service: the store still holds the original text afterwards.
        """
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            armed = await svc.add(slot_key="chat-9-999", message="the original instruction")
            self._mis_compose_scrub(monkeypatch)

            _, _, status = await authz.authorize_and_update_nudge(
                svc=svc,
                loop_id=armed.id,
                message="a replacement the operator typed",
                idle_secs=600,
                source="dashboard",
            )
            assert status == 503, f"expected an audited refusal, got {status}"
            still = svc.get_by_id(armed.id)
            assert (
                still.message == "the original instruction"
            ), f"the refused write was applied anyway: {still.message!r}"
            assert still.idle_secs != 600, "an accompanying field was applied despite the refusal"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_working_policy_still_nulls_the_resubmitted_projection(
        self, tmp_path, audits: list[dict]
    ) -> None:
        """PRESERVED: the 15:23Z projection-overwrite fix must survive this guard."""
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            secret = "AKIAIOSFODNN7EXAMPLE"
            original = f"deploy using key {secret} and report back"
            armed = await svc.add(slot_key="chat-8-888", message=original)
            projection = autonudge_handlers._serialize(armed)
            assert projection["message"] != original

            loop, error, status = await authz.authorize_and_update_nudge(
                svc=svc,
                loop_id=armed.id,
                message=projection["message"],
                idle_secs=600,
                source="dashboard",
            )
            assert status == 200, f"the ordinary save broke: {error}"
            assert loop.message == original, "the projection overwrite regressed"
            assert loop.idle_secs == 600
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_working_policy_still_replaces_a_genuinely_different_message(
        self, tmp_path, audits: list[dict]
    ) -> None:
        """PRESERVED: a real edit still lands and is still scrubbed on the way in."""
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            secret = "AKIAIOSFODNN7EXAMPLE"
            armed = await svc.add(slot_key="chat-7-777", message="the original instruction")
            loop, error, status = await authz.authorize_and_update_nudge(
                svc=svc,
                loop_id=armed.id,
                message=f"a new instruction {secret}",
                source="dashboard",
            )
            assert status == 200, f"a genuine edit was refused: {error}"
            assert loop.message.startswith("a new instruction")
            assert secret not in loop.message, "inbound redaction was lost"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_bannerless_messageless_update_is_unaffected(
        self, monkeypatch: pytest.MonkeyPatch, audits: list[dict]
    ) -> None:
        """PRESERVED: with no message there is nothing to compare, so no 503.

        The guard is gated on ``message is not None``, exactly as the banner guard is
        gated on a non-blank banner -- a mis-composed policy must not turn every
        field-only save into a refusal.
        """
        self._mis_compose_scrub(monkeypatch)
        svc = MagicMock()
        svc.list_all = lambda: []
        svc.get_by_id = lambda _id: None
        svc.get_by_id = lambda _id: SimpleNamespace(message="the original instruction")
        svc.update = AsyncMock(return_value=SimpleNamespace(slot_key="chat-1-123", idle_secs=600))

        _, error, status = await authz.authorize_and_update_nudge(
            svc=svc, loop_id="loop-1", idle_secs=600, source="dashboard"
        )
        assert status == 200, f"a message-free save was refused: {error}"
        svc.update.assert_awaited()

    @pytest.mark.asyncio
    async def test_update_denies_with_503_and_audits_when_the_policy_cannot_compose(
        self, monkeypatch: pytest.MonkeyPatch, audits: list[dict]
    ) -> None:
        """The same hole on the OTHER call site -- fixing one would move it, not close it."""
        self._mis_compose(monkeypatch)
        svc = MagicMock()
        svc.list_all = lambda: []
        svc.get_by_id = lambda _id: None
        svc.update = AsyncMock(return_value=None)
        loop, error, status = await authz.authorize_and_update_nudge(
            svc=svc,
            loop_id="loop-1",
            banner="watching CI",
            source="dashboard",
        )
        assert status == 503, f"the update path did not yield a 503 (status {status})"
        assert audits and audits[-1]["outcome"] == "denied", "the refusal skipped the SEL audit"
        svc.update.assert_not_awaited(), "the update landed despite an unusable policy"

    @pytest.mark.asyncio
    async def test_a_blank_banner_is_unaffected_by_a_mis_composed_policy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PRESERVED: nothing is scrubbed when there is nothing to scrub.

        The redaction is gated on a non-blank banner, so a mis-composed policy must
        not turn every no-banner request into a 503.
        """
        self._mis_compose(monkeypatch)
        value, err = authz.normalize_banner(None, absent_ok=True)
        assert (value, err) == ("", None), f"an absent banner was refused: {err}"
        value, err = authz.normalize_banner("   ", absent_ok=True)
        assert (value, err) == ("", None), f"a blank banner was refused: {err}"

    def test_a_working_policy_still_redacts_and_still_caps(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PRESERVED, both halves: the scrub still runs and the cap re-check still fires."""
        from kiro_crew.autonudge import MAX_BANNER_CHARS

        monkeypatch.setattr(authz, "redact_via_context", lambda text: text.replace("SEK", "[R]"))
        value, err = authz.normalize_banner("token SEK here", absent_ok=True)
        assert err is None and "SEK" not in value, f"redaction stopped running: {value!r}"

        # a policy that GROWS the value past the cap must still hit the 400
        monkeypatch.setattr(authz, "redact_via_context", lambda text: text + "x" * 64)
        value, err = authz.normalize_banner("a" * MAX_BANNER_CHARS, absent_ok=True)
        assert (
            err is not None and "once credentials are masked" in err
        ), f"the cap re-check is gone: {err}"


class TestTheListSerializerScrubsLoopText:
    """``GET /api/autonudge`` was the third sink, and it served ``message`` raw.

    ``_load`` repairs the store and the transcript row scrubs at the sink, but the
    REST serializer was a bare ``asdict``. Three producers reach ``svc.add``
    without the authorizer -- the goal loop (``dashboard/chat_runner.py``),
    auto-research, and issue-radar, whose message is composed from external issue
    text -- so this is reachable, not theoretical.
    """

    def test_a_credential_in_message_does_not_reach_the_client(self) -> None:
        """The item First Principles counted as the unfixed sibling."""
        secret = "AKIAIOSFODNN7EXAMPLE"
        out = autonudge_handlers._serialize(_loop(message=f"do the thing {secret}"))
        assert secret not in out["message"], "the REST surface serves an unredacted credential"
        assert "REDACTED" in out["message"]

    def test_a_clean_loop_round_trips_unchanged(self) -> None:
        """The scrub must not rewrite ordinary text.

        Its own arm because a scrub that replaced every value with a placeholder
        would satisfy the arm above while destroying the surface.
        """
        loop = _loop(message="run the next cycle", banner="watching CI")
        out = autonudge_handlers._serialize(loop)
        assert out["message"] == "run the next cycle"
        assert out["banner"] == "watching CI"

    def test_the_addressing_fields_are_never_rewritten(self) -> None:
        """``id`` and ``slot_key`` must survive verbatim or the client cannot act.

        A rewritten ``id`` would break ``PATCH``/``DELETE`` targeting, turning a
        redaction into a functional regression.
        """
        loop = _loop(id="loop-abc", slot_key="chat-1-1785")
        out = autonudge_handlers._serialize(loop)
        assert out["id"] == "loop-abc"
        assert out["slot_key"] == "chat-1-1785"

    def test_a_field_the_scrub_does_not_name_is_still_covered(self) -> None:
        """The denylist shape, pinned.

        ``stopped_reason`` is agent-supplied free text (``autonudge_stop(reason=)``)
        and is named nowhere in the scrub. An allowlist would have missed it, which
        is exactly how ``banner`` came to need a scrub of its own.
        """
        secret = "AKIAIOSFODNN7EXAMPLE"
        out = autonudge_handlers._serialize(_loop(stopped_reason=f"gave up {secret}"))
        assert secret not in out["stopped_reason"]


class TestTheNudgeMetaCarriesNoBannerMarker:
    """First Principles: the ``banner: True`` marker had zero consumers, so it went.

    The sole ``meta.nudge`` reader is ``website/src/pages/chat/NudgeCard.tsx``, and it
    reads ``cycle``/``loop_id``/``body`` only. A field nothing reads is a contract
    nobody is holding: it has to be kept correct at every write site while buying
    the operator nothing, and the right time to add it is when a reader exists.

    NOTE, a live tension worth naming rather than arbitrating: Design Review asked
    for this marker on an earlier sha (history should distinguish a stand-in from
    the injected text) and First Principles asks for its removal here. Both were
    current on their own shas. It is removed per First Principles, and the
    disagreement is escalated.

    These arms are the SUBTRACTION's guard: the meta block must be exactly what it
    was before the feature existed, so a banner loop and a plain loop are
    indistinguishable in meta.
    """

    @pytest.mark.asyncio
    async def test_a_banner_row_carries_no_banner_key(self) -> None:
        _row, _prompt, meta = await _fire_full(_loop(banner="watching PR #123 for CI"))
        assert "banner" not in meta, "the zero-consumer marker is still written"
        assert set(meta) == {"cycle", "loop_id"}

    @pytest.mark.asyncio
    async def test_a_normal_row_carries_the_same_meta_as_a_banner_row(self) -> None:
        """The point of the subtraction: the two are now identical in meta."""
        _row, _prompt, plain = await _fire_full(_loop(banner=""))
        _row2, _prompt2, bannered = await _fire_full(_loop(banner="quiet"))
        assert set(plain) == set(bannered) == {"cycle", "loop_id"}

    @pytest.mark.asyncio
    async def test_the_banner_text_is_not_in_meta_either(self) -> None:
        """The row's ``content`` holds the banner; copying it into meta would be the
        double-broadcast the meta block exists to avoid."""
        banner = "watching PR #123 for CI"
        row, _prompt, meta = await _fire_full(_loop(banner=banner))
        assert banner in row
        assert banner not in str(meta)

    @pytest.mark.asyncio
    async def test_the_cycle_and_loop_id_still_reach_the_reader(self) -> None:
        """Negative control: removing the marker must not disturb what NudgeCard reads."""
        loop = _loop(banner="quiet", cycle_count=7)
        _row, _prompt, meta = await _fire_full(loop)
        assert meta["cycle"] == 8
        assert meta["loop_id"] == loop.id


class TestTheCountedLogHygieneSiblings:
    """First Principles: the log-hygiene rule this PR argues from left 2 siblings.

    The lane named both: ``cron.py`` logging a malformed entry's id, and
    the autonudge loader logging a malformed row. Both
    read from a store an agent can write directly, and both land in the log ring
    and the ``/api/logs`` stream.
    """


class TestScrubbedLogTextCannotForgeARecord:
    """GPT 5.6 (BLOCKING): the ``_load`` warnings returned control characters intact.

    The two redactors remove credential- and URL-shaped SUBSTRINGS. Neither is a
    ``str``-shape control: a newline survives both, so a store-supplied id or key
    carrying one arrives whole at the ``%s`` warning and splits one record into
    several in the log ring and the ``/api/logs`` stream. The forged tail is
    attacker-authored and indistinguishable from a real line, which makes the log
    unreliable exactly where it is used to diagnose a hand-edited store.

    The escape is supplied by ``repr`` -- ``redact(repr(value))``, the spelling
    ``cron.py`` uses for the identical malformed-entry warning and the base's own
    at ``session_storage.py``. That replaced a hand-rolled ``str.isprintable``
    comprehension: First Principles asked for one definition rather than two, and
    ``repr`` escapes the same set, because CPython's ``repr`` for ``str`` keys its
    own escaping on ``str.isprintable``. Measured across newline, CR, ESC, NUL,
    U+2028 and a lone surrogate: identical escaping, plus a literal backslash is
    now escaped too, which REMOVES an ambiguity the hand-rolled version accepted.

    Not a denylist on ``\\n``. Every non-printable character is escaped, so
    ``\\r`` (a bare CR rewrites a line in a terminal), ``\\x1b`` (an ANSI escape
    can erase the line above it), and U+2028/U+2029 (line separators several log
    viewers honour) cannot be substituted for it tomorrow.
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

    def test_the_load_warnings_use_the_shared_spelling(self) -> None:
        """First Principles: one definition for both loaders, not two.

        Pins that ``autonudge.py`` reaches for ``redact(repr(...))`` and no longer
        carries a hand-rolled escape of its own.

        This used to ban the token ``isprintable`` outright. That over-reached: the
        subtraction it protects is the removal of a hand-rolled log ESCAPE, and the
        addressing guard now uses the same builtin for a different job -- deciding
        whether to REFUSE a persisted row at the trust boundary. Banning the token
        conflated the two, so the ban is replaced by the narrower property that was
        always the point: the only use is the refusal predicate, and nothing here
        escapes text for a log line by hand.
        """
        src = Path(_an.__file__).read_text(encoding="utf-8")
        assert "_scrub_for_log" not in src, "the second spelling is still defined"
        # ONE spelling, defined once, and now homed beside ``redact_via_context`` in
        # ``platform.context`` rather than in this service module: it is generic log
        # hygiene, and leaving it here made ``cron.py`` import the whole autonudge
        # service for a five-line helper. Same reasoning the PR applied to
        # ``MAX_BANNER_CHARS``. It still routes through the ACTIVE credential policy
        # instead of the bare ``security.redact``, which let a composed host's own
        # patterns be skipped.
        # ONE spelling, defined once, and homed with its ONLY consumer. The relocation
        # to ``platform.context`` was justified by ``cron.py`` importing this module for
        # a five-line helper; with the cron and ops_mission_control loader redactions
        # deferred to their own PR (Design + First Principles both asked for that split),
        # that justification is gone and the helper belongs beside the code that uses it.
        assert "def redact_store_value(" in src, "the one shared log-scrub spelling is not defined"
        assert (
            src.count("redact_store_value(") >= 5
        ), "the store-sourced log sinks do not share one spelling"
        assert (
            "redact_log_via_context(repr(value))" in src
        ), "the shared spelling no longer routes through the active credential policy"
        assert src.count("redact(repr(") == 0, "a bare-redactor log scrub survives"
        assert "redact_credentials(" not in src, "the redaction pair is still hand-rolled"
        # ``isprintable`` now has TWO uses, and the property being pinned is that
        # EVERY use is an addressing-field VALIDITY predicate -- never a hand-rolled
        # log escape, which is the subtraction this test protects. The second use is
        # the refused-row eviction deliberately re-applying the load-time guard's own
        # test, so it only ever drops a row whose key it could actually vet.
        assert src.count("isprintable") == 1, (
            "isprintable count moved -- the load-time validity check is the only "
            "user now that the refused-row eviction is gone"
        )
        assert (
            "not got.isprintable()" in src
        ), "the load-time addressing refusal predicate no longer uses it"

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
    """GPT 5.6 (BLOCKING): the write landed and the caller was told it failed.

    The ordering was the defect. On a host whose credential policy cannot compose:

    * a BANNERLESS, MESSAGELESS request scrubs nothing during authorization --
      ``normalize_banner`` returns early on a blank banner and the message compare is
      gated on ``message is not None`` -- so nothing raises before the mutation;
    * ``svc.add`` / ``svc.update`` COMMITS (authz :408 / :692, after the critical
      audit);
    * only then does the handler serialize the response, and ``_serialize`` ->
      ``scrub_loop_text`` -> ``redact_via_context`` raises.

    Result: HTTP 500 with the mutation already persisted and audited as ``success``.
    The store and the caller's belief about the store disagree, permanently, and a
    retry would apply it twice.

    The fix probes the policy in BOTH authorizers before auditing or mutating, so an
    unusable policy is a clean audited 503 with nothing written. Pinned in both
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
    async def test_a_bannerless_update_does_not_commit_then_fail(self, audits) -> None:
        """The update path: a field-only PATCH must not persist behind a 500."""
        svc = MagicMock()
        svc.list_all = lambda: []
        svc.get_by_id = lambda _id: None
        svc.get_by_id = lambda _id: SimpleNamespace(message="the original instruction")
        svc.update = AsyncMock(return_value=None)
        self._install(self._BrokenPolicy())

        loop, error, status = await authz.authorize_and_update_nudge(
            svc=svc, loop_id="loop-1", idle_secs=600, source="dashboard"
        )
        assert status == 503, f"expected an audited refusal before the write, got {status}"
        assert loop is None
        svc.update.assert_not_awaited(), (
            "the mutation COMMITTED even though the response could not be serialized -- "
            "the caller gets a 500 and the store keeps the write"
        )
        assert audits and audits[-1]["outcome"] == "denied", "the refusal skipped the SEL audit"

    @pytest.mark.asyncio
    async def test_a_bannerless_arm_does_not_commit_then_fail(self, audits) -> None:
        """The arm path: same ordering, same remedy."""
        slot = MagicMock(workspace="default")
        state = SimpleNamespace(_slots={"chat-1-123": slot}, sessions=None, channel_transports={})
        svc = MagicMock()
        svc.list_all = lambda: []
        svc.get_by_id = lambda _id: None
        svc.add = AsyncMock()
        self._install(self._BrokenPolicy())

        loop, error, status = await authz.authorize_and_add_nudge(
            svc=svc,
            state=state,
            slot_key="chat-1-123",
            message="watch the build",
            source="dashboard",
        )
        assert status == 503, f"expected an audited refusal before the write, got {status}"
        assert loop is None
        svc.add.assert_not_awaited(), "the loop armed despite an unserializable response"
        assert audits and audits[-1]["outcome"] == "denied", "the refusal skipped the SEL audit"

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
    """GPT 5.6 (BLOCKING): ``message: 42`` in the store crashed the goal popover.

    The store is hand-editable JSON and ``NudgeLoop`` is a plain dataclass, so
    ``{"message": 42}`` becomes ``loop.message = 42`` -- ``_load`` repairs the
    numeric timer fields and the banner, but nothing coerces ``message``. The REST
    projection then served it untouched, because ``scrub_loop_text`` returned every
    ``int``/``float``/``bool`` early.

    ``AutoNudgePopover.tsx`` reads ``loop?.message || DEFAULT_MSG``, and ``42`` is
    truthy, so the number reached ``message.trim()`` and threw -- the popover died
    rather than showing the row. (``0`` was survivable only by accident: it is
    falsy, so the default template took over.)

    The numeric pass-through is NOT simply wrong, which is why this is a
    field-aware fix rather than a blanket ``str()``: nine of the sixteen fields are
    declared numeric and clients do arithmetic on them, so coercing ``300`` to
    ``"300"`` would break the contract this projection exists to serve. The
    exemption therefore keys on the FIELD, not on the value's type.
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

    def test_the_numeric_exemption_matches_the_dataclass(self) -> None:
        """The exemption set must be exactly the declared-numeric fields, not a guess.

        Pinned against ``NudgeLoop`` itself so a field added or retyped later cannot
        silently fall out of the exemption (which would coerce a number) or into it
        (which would re-open this crash for a text field).
        """
        declared = {
            name
            for name, spec in NudgeLoop.__dataclass_fields__.items()
            if str(spec.type).replace("'", "") in {"int", "float", "bool"}
        }
        assert declared, "the annotation probe found nothing -- it cannot fail correctly"
        assert _an._NUMERIC_LOOP_FIELDS == declared, (
            f"exemption set drifted from the dataclass: "
            f"only-in-set={_an._NUMERIC_LOOP_FIELDS - declared} "
            f"only-in-class={declared - _an._NUMERIC_LOOP_FIELDS}"
        )

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

        ``slack/gateway.py`` scrubs ``loop.message`` through the same function; if it
        passed no field the number would reach the browser by the other route and the
        fix would have moved the crash rather than closed it.
        """
        assert isinstance(_an.scrub_loop_text(42, field="message"), str)


class TestTheRedactedProjectionCannotOverwriteTheStoredMessage:
    """GPT 5.6 (BLOCKING): the popover's own Save destroyed the operator's prompt.

    The mechanism needs both halves of an asymmetry to line up:

    * ``svc.add`` stores a message WITHOUT the PATCH path's redaction pair -- the
      MCP arming tools and any direct service caller go in that way -- so the stored
      text keeps whatever it was armed with.
    * ``_serialize`` projects that field through ``scrub_loop_text`` ->
      ``redact_via_context``, which is a DIFFERENT and wider rule than the pair at
      ``authorize_and_update_nudge``'s message arm (a composed host contributes its
      own patterns).

    So the popover loads a projection that differs from the stored value, and its
    Save PATCHes that projection straight back. Nothing errored, nothing warned,
    and the operator's instruction was replaced by ``[REDACTED: ...]`` permanently.

    The remedy is the lane's own: a submitted message equal to the current scrubbed
    projection is treated as UNCHANGED. It is compared with the very same
    ``scrub_loop_text`` the projection uses -- not a second hand-rolled redaction --
    so the two cannot drift apart.
    """

    SECRET = "AKIAIOSFODNN7EXAMPLE"

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
        original = f"deploy using key {self.SECRET} and report back"
        loop = await svc.add(slot_key="chat-1-123", message=original, idle_secs=300)
        assert loop.message == original, "svc.add unexpectedly redacted on the way in"
        return original, loop.id

    @pytest.mark.asyncio
    async def test_saving_the_loaded_projection_leaves_the_original_intact(
        self, svc, audits
    ) -> None:
        """The bug: PATCH the exact projection back, as the popover's Save does."""
        original, loop_id = await self._armed(svc)

        projection = autonudge_handlers._serialize(svc.list_all()[0])
        assert projection["message"] != original, (
            "the projection did not differ from the stored value, so this test would "
            "not exercise the overwrite at all"
        )
        assert self.SECRET not in projection["message"]

        # The popover sends the whole form back: the untouched message field it was
        # served, alongside the field the operator actually changed.
        loop, error, status = await authz.authorize_and_update_nudge(
            svc=svc,
            loop_id=loop_id,
            message=projection["message"],
            idle_secs=600,
            source="test",
        )
        assert status == 200, f"the save itself failed: {error}"
        assert loop.idle_secs == 600, "the operator's real edit was lost"
        assert loop.message == original, (
            "the redacted projection overwrote the stored instruction: "
            f"{loop.message!r} replaced {original!r}"
        )

    @pytest.mark.asyncio
    async def test_the_audit_does_not_claim_a_message_change_that_was_dropped(
        self, svc, audits
    ) -> None:
        """The dropped field must be absent from the critical ``invoked`` audit.

        The ``fields`` list is what an auditor reads to learn which fields a caller
        mutated. Recording ``message`` on a save that deliberately applied no message
        would make that record disagree with the store.
        """
        _, loop_id = await self._armed(svc)
        projection = autonudge_handlers._serialize(svc.list_all()[0])

        _, _, status = await authz.authorize_and_update_nudge(
            svc=svc,
            loop_id=loop_id,
            message=projection["message"],
            idle_secs=600,
            source="test",
        )
        assert status == 200

        invoked = [c for c in audits if c.get("outcome") == "invoked"]
        assert invoked, "the critical invoked audit stopped being written"
        recorded = invoked[-1]["metadata"]["fields"]
        assert "idle_secs" in recorded, "the field that WAS applied is missing"
        assert (
            "message" not in recorded
        ), f"the audit claims a message change that was dropped: {recorded!r}"

    @pytest.mark.asyncio
    async def test_a_genuinely_different_message_still_replaces_and_is_redacted(
        self, svc, audits
    ) -> None:
        """Preserved: a real edit still lands, and inbound redaction still applies."""
        _, loop_id = await self._armed(svc)

        loop, error, status = await authz.authorize_and_update_nudge(
            svc=svc,
            loop_id=loop_id,
            message=f"completely new instruction {self.SECRET}",
            source="test",
        )
        assert status == 200, f"a genuine edit was refused: {error}"
        assert loop.message.startswith("completely new instruction")
        assert self.SECRET not in loop.message, "inbound redaction was lost"

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


class TestSentinelRepairWarningsAttachNoTraceback:
    """GPT 5.6 (BLOCKING): ``exc_info=True`` re-exposed what ``_scrub`` withheld.

    Both sentinel-repair arms interpolate ``_scrub(...)`` precisely because the
    value comes out of a hand-editable store and the log ring is served by
    ``/api/logs``. ``exc_info=True`` then attached the traceback, and a traceback
    ends with the exception's own ``str()`` -- which for the failures these arms
    exist to catch embeds the offending path verbatim (``OSError: [Errno 36] File
    name too long: '<path>'``). So the scrubbed argument was served next to an
    unscrubbed copy of the same value, on the same record.

    Both directions are pinned: no traceback text, AND the warning still fires with
    the scrubbed value while startup proceeds -- the ``# noqa: BLE001`` on each arm
    says a repair failure must never block startup, so turning either warning into
    a raise, a return or a silence would be a regression, not a fix.
    """

    SECRET = "AKIAIOSFODNN7EXAMPLE"

    def _path(self) -> str:
        return f"/tmp/{'d' * 200}{self.SECRET}/STOP"

    @staticmethod
    def _assert_no_traceback(records, secret: str, where: str) -> None:
        """No record on this path may carry traceback text, formatted or not."""
        import logging

        fmt = logging.Formatter("%(message)s")
        for rec in records:
            assert rec.exc_info is None, (
                f"{where}: the record still attaches exc_info, so the traceback "
                f"(and the raw value in its exception text) reaches /api/logs"
            )
            assert rec.exc_text is None, f"{where}: the record carries cached traceback text"
            assert secret not in fmt.format(
                rec
            ), f"{where}: the credential is still in the emitted record: {fmt.format(rec)!r}"

    def test_the_rehome_arm_emits_scrubbed_and_without_a_traceback(
        self, caplog, monkeypatch
    ) -> None:
        """Site 1: the ``could not re-home sentinel`` arm."""
        bad = self._path()

        def _raise(_s):
            raise OSError(f"[Errno 36] File name too long: '{bad}'")

        # normpath runs INSIDE the arm's own try, which is what the real
        # filesystem failure this arm catches also does.
        monkeypatch.setattr(_an.os.path, "normpath", _raise)
        with caplog.at_level("WARNING"):
            # The property that matters here is that it RETURNED rather than raised:
            # this arm's ``# noqa: BLE001`` says a repair failure must never block
            # startup. The exact return value is not asserted, because patching
            # ``normpath`` also perturbs the sensitivity check further down, so
            # pinning it would measure the patch rather than the arm.
            out = _an.repair_sentinel_path(bad)
        assert isinstance(out, str), "the repair arm no longer returns a string"

        mine = [r for r in caplog.records if "could not re-home sentinel" in r.getMessage()]
        assert mine, "the repair warning stopped being emitted at all"
        self._assert_no_traceback(mine, self.SECRET, "re-home arm")

    def test_the_sensitivity_arm_emits_scrubbed_and_without_a_traceback(
        self, caplog, monkeypatch
    ) -> None:
        """Site 2: the ``sensitivity re-check failed`` arm, which also drops the sentinel."""
        bad = self._path()

        def _raise(_p):
            raise OSError(f"[Errno 36] File name too long: '{bad}'")

        monkeypatch.setattr(_an, "is_sensitive_path", _raise)
        with caplog.at_level("WARNING"):
            out = _an.repair_sentinel_path(bad)

        mine = [r for r in caplog.records if "sensitivity re-check failed" in r.getMessage()]
        assert mine, "the sensitivity warning stopped being emitted at all"
        assert out == "", "the fail-closed drop was lost -- an unvalidated sentinel survived"
        self._assert_no_traceback(mine, self.SECRET, "sensitivity arm")

    def test_a_benign_path_is_untouched(self, caplog) -> None:
        """Negative control: neither arm may fire on a path that repairs cleanly."""
        with caplog.at_level("WARNING"):
            out = _an.repair_sentinel_path("/tmp/kc-benign/STOP")
        assert out == "/tmp/kc-benign/STOP"
        assert not [
            r
            for r in caplog.records
            if "could not re-home" in r.getMessage() or "sensitivity re-check" in r.getMessage()
        ], "a repair arm fired on a benign path"


class TestTheMalformedEntryArmTracebackIsMeasured:
    """Measures, rather than assumes, whether the ``:950`` sibling leaks too.

    The malformed-entry arm scrubs ``bad_id`` and ``fields`` for the same reason
    and also passes ``exc_info=True``. Whether that is the identical bypass depends
    on one thing only: can an exception raised inside the per-row ``try`` carry
    store-sourced TEXT in its own message? A traceback lists source lines and frame
    locations, not local values, so the exception's ``str()`` is the whole exposure.
    """

    SECRET = "AKIAIOSFODNN7EXAMPLE"

    @staticmethod
    def _write(tmp_path, rows) -> None:
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": rows}), encoding="utf-8"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "row",
        [
            pytest.param(42, id="row-is-an-int"),
            pytest.param("AKIAIOSFODNN7EXAMPLE", id="row-is-a-credential-string"),
            pytest.param(["AKIAIOSFODNN7EXAMPLE"], id="row-is-a-list"),
            pytest.param({"AKIAIOSFODNN7EXAMPLE": 1}, id="credential-shaped-KEY"),
            pytest.param(
                {"id": "x", "slot_key": "s", "idle_secs": "AKIAIOSFODNN7EXAMPLE"},
                id="credential-in-a-numeric-field",
            ),
        ],
    )
    async def test_whether_a_malformed_row_puts_its_text_in_the_traceback(
        self, tmp_path, caplog, row
    ) -> None:
        """Records what the arm actually emits, and fails only on a real leak.

        If any of these shapes lands the credential in the emitted record, the
        ``:950`` site is the same defect and must lose its ``exc_info`` too.
        """
        import logging

        self._write(tmp_path, [row])
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            with caplog.at_level("WARNING"):
                await svc.start()  # must never raise -- the arm's whole job is to skip
            fmt = logging.Formatter("%(message)s")
            leaked = [
                r
                for r in caplog.records
                if self.SECRET in (fmt.format(r) + (r.exc_text or "") + str(r.exc_info or ""))
            ]
            assert not leaked, (
                "the malformed-entry arm leaked the credential: "
                f"{[fmt.format(r) for r in leaked]!r}"
            )
        finally:
            svc.stop()


class TestTheAddressingGuardUsesTheActiveCredentialPolicy:
    """GPT 5.6 (BLOCKING): ``_load``'s addressing guard used the bare redactor.

    ``if redact(got) != got`` is the credential-shape DETECTOR for the two fields
    ``_serialize`` deliberately serves unscrubbed. Asking the bare
    ``security.redact`` means a host that loads a companion credential policy --
    whose whole purpose is extra, host-specific patterns -- had those patterns
    skipped by the detector. A companion-only credential parked in an
    agent-writable loop ``id`` was therefore judged clean, the loop armed, and the
    value reached every dashboard client verbatim through ``GET /api/autonudge``
    and the transcript row's ``meta.nudge.loop_id``.

    Both directions are pinned, because a guard that refuses EVERYTHING is not a
    fix: the companion shape must be refused AND an ordinary id must still arm.
    """

    COMPANION_TOKEN = "COMPANION-SSO-COOKIE-9f3a2b4c7d1e"

    class _CompanionPolicy:
        """Core redaction plus ONE host-specific pattern, as a companion supplies."""

        token = "COMPANION-SSO-COOKIE-9f3a2b4c7d1e"

        def redact(self, text: str) -> str:
            from kiro_crew import security

            return security.redact(text).replace(self.token, "[REDACTED: companion]")

    @staticmethod
    def _install(policy) -> None:
        import dataclasses

        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.platform import build_default_context, set_context

        base = build_default_context(KiroCrewConfig())
        set_context(dataclasses.replace(base, credentials=policy))

    @staticmethod
    def _write(tmp_path, **over) -> None:
        row = {
            "id": "abc123",
            "slot_key": "chat-1-2",
            "message": "keep going",
            "idle_secs": 300,
        }
        row.update(over)
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": [row]}), encoding="utf-8"
        )

    def test_core_redaction_leaves_the_companion_token_alone(self) -> None:
        """Control FIRST: without this the arms below could pass vacuously.

        If core redaction already stripped this shape, the bare detector and the
        policy-routed one would agree and nothing below could discriminate.
        """
        from kiro_crew.security import redact

        assert redact(self.COMPANION_TOKEN) == self.COMPANION_TOKEN, (
            "core redaction now strips this shape, so it can no longer distinguish "
            "the bare detector from the platform-routed one -- pick another token"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", ["id", "slot_key"])
    async def test_a_companion_shaped_addressing_field_is_refused(
        self, tmp_path, caplog, field
    ) -> None:
        """The lane's path: agent-writable store row -> _load -> dashboard API."""
        self._install(self._CompanionPolicy())
        self._write(tmp_path, **{field: f"loop-{self.COMPANION_TOKEN}"})
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            with caplog.at_level("WARNING"):
                await svc.start()

            assert svc._loops == {}, (
                f"a companion-shaped {field} armed anyway -- the guard asked the bare "
                "redactor, so the active policy's patterns never ran"
            )
            assert "refusing loop" in caplog.text, "the refusal was silent"

            # and it must not be reachable through the REST projection either
            for loop in svc.list_all():
                assert self.COMPANION_TOKEN not in str(autonudge_handlers._serialize(loop))
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_an_ordinary_id_still_arms(self, tmp_path) -> None:
        """The other direction: a guard that refuses everything is not a fix."""
        self._install(self._CompanionPolicy())
        self._write(tmp_path, id="chat-1281-1785676802")
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            assert "chat-1281-1785676802" in svc._loops, "an ordinary id was refused"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_mis_composed_host_refuses_every_row_without_losing_one(
        self, tmp_path, caplog
    ) -> None:
        """``redact_via_context`` is FAIL-CLOSED, so establish what that does HERE.

        It re-raises ``PlatformCompositionError`` -- a host declaring a companion
        policy it could not compose. That exception is ``RuntimeError``-derived, so
        the per-row ``except Exception`` would swallow it once per row and the
        malformed-entry arm would log N confusing "skipping malformed loop entry"
        warnings; and the same call inside THAT arm would escape ``_load``
        altogether, escaping the unguarded ``run_in_executor`` in ``start()``.

        So the loader resolves the policy ONCE, up front. This pins the resulting
        contract: no loop arms (the security decision fails closed) and ``start()``
        does NOT raise.

        Non-destructiveness is pinned at the WRITE boundary now rather than by
        carrying rows through memory: ``_load_refused`` makes ``_write_state``
        refuse, so the file survives even though the payload is empty. That is
        cron's answer to the same state, and ``TestARefusalDoesNotDestroyTheStore``
        proves the file is byte-identical afterwards.
        """
        from kiro_crew.platform import PlatformCompositionError

        class _MisComposed:
            def redact(self, text: str) -> str:
                raise PlatformCompositionError("companion policy unreadable")

        self._install(_MisComposed())
        self._write(tmp_path, id="chat-1281-1785676802")
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            with caplog.at_level("ERROR"):
                await svc.start()  # must NOT raise

            assert svc._loops == {}, "a loop armed without a usable credential policy"
            assert svc._load_refused is True, "the write-refusal flag was not set"
            # The payload IS empty now -- and that is safe only because the write is
            # refused. Asserting both together is the point: either alone would pass
            # while the store was being destroyed.
            assert svc._serialize_state()["loops"] == []
            original = (tmp_path / "autonudge.json").read_text(encoding="utf-8")
            # RAISES rather than returning quietly, so a mutation caller's existing
            # rollback handler fires instead of it confirming an undurable loop.
            with pytest.raises(_an.AutoNudgeStoreUnvetted):
                svc._write_state(svc._serialize_state())
            assert (tmp_path / "autonudge.json").read_text(
                encoding="utf-8"
            ) == original, "the store was overwritten while unvettable"
        finally:
            svc.stop()


class TestCredentialShapedAddressingFieldsAreRefused:
    """GPT 5.6 (BLOCKING): the REST serializer exempts the addressing fields.

    ``id`` and ``slot_key`` pass through ``_serialize`` unscrubbed because the
    client addresses the row by them -- rewriting either leaves a row that renders
    but cannot be acted on. That exemption is only safe if an addressing field can
    never CARRY a credential, and the store is a file an agent writes directly, so
    nothing upstream guaranteed it: a credential placed in ``id`` reached every
    dashboard client verbatim through ``GET /api/autonudge`` and through the
    transcript row's ``meta.nudge.loop_id``.

    ``_load`` now REFUSES such a loop rather than scrubbing it, and refusing is
    also what matches the arm-time contract -- ``authorize_and_add_nudge`` never
    mints an id of this shape, so a store row carrying one did not come from the
    API.
    """

    SECRET = "AKIAIOSFODNN7EXAMPLE"

    def _write(self, tmp_path, **over) -> None:
        row = {
            "id": "abc123",
            "slot_key": "chat-1-2",
            "message": "keep going",
            "idle_secs": 300,
        }
        row.update(over)
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": [row]}), encoding="utf-8"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", ["id", "slot_key"])
    async def test_a_credential_shaped_addressing_field_is_refused(
        self, tmp_path, caplog, field
    ) -> None:
        """Fails on the unmodified tree, where the loop arms and is then served."""
        self._write(tmp_path, **{field: f"loop-{self.SECRET}"})
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            with caplog.at_level("WARNING"):
                await svc.start()
            assert svc._loops == {}, "a credential-shaped addressing field was armed anyway"
            assert "refusing loop" in caplog.text, "the refusal was silent"
            assert field in caplog.text, "the warning does not name the offending field"
            assert self.SECRET not in caplog.text, "the refusal echoed the credential"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_refused_row_is_not_dropped_by_the_next_write(self, tmp_path, caplog) -> None:
        """GPT 5.6 (BLOCKING): the refused row was deleted by the next write.

        Declining the row kept it out of ``_loops``, so the next wholesale write --
        any ``add``/``update``/stop -- serialized the store WITHOUT it and the
        operator's row was permanently gone. The warning named a field to fix in a
        file that no longer contained it.

        The fix arms ``_load_refused``, so every persist raises and the file on disk
        is left untouched until the entry is repaired and the process restarted. That
        is the same mechanism the whole-store arm already used, and it holds no row in
        memory, so the rollback gap that killed the earlier hold stays unreachable.

        Discriminates the two refusals by the WARNING, not by ``_loops``: both empty it
        now, so "the clean row still armed" no longer separates them. Only the row-level
        arm names the offending loop and field, so asserting that text proves the policy
        composed and one row was declined -- without it, a leaked broken policy would
        satisfy the raise for the wrong reason.
        """
        rows = [
            {"id": "abc123", "slot_key": "chat-1-2", "message": "keep going", "idle_secs": 300},
            {"id": self.SECRET, "slot_key": "chat-3-3", "message": "x", "idle_secs": 300},
        ]
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": rows}), encoding="utf-8"
        )

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            with caplog.at_level("WARNING"):
                svc._load()
            assert "refusing loop" in caplog.text, (
                "no row-level refusal was logged, so this is the WHOLE-STORE arm and the "
                "raise below would prove nothing about a single declined row"
            )
            svc._write_state(svc._serialize_state())
            after = (tmp_path / "autonudge.quarantine.json").read_text(encoding="utf-8")
            assert _held_aside_rows(tmp_path), "the declined row was dropped"
            assert self.SECRET in after, (
                "the refused row is gone from disk -- the operator was told to fix a "
                "field in an entry the next write had already deleted"
            )
        finally:
            svc.stop()

    @pytest.mark.parametrize(
        "payload",
        [
            "{not json at all",
            '["a", "list", "not", "an", "object"]',
            '{"quarantined": ["not-an-object"]}',
        ],
        ids=["unparseable", "wrong-shape", "non-object-member"],
    )
    @pytest.mark.asyncio
    async def test_an_unreadable_sidecar_is_not_unlinked_by_the_next_write(
        self, tmp_path, caplog, payload
    ) -> None:
        """GPT 5.6 (BLOCKING): a corrupt sidecar was deleted by the next write.

        ``_read_quarantine_sidecar`` answered unparseable or wrongly-shaped content with
        ``[]``, which is indistinguishable from "nothing is held aside". The loader
        therefore armed normally, and the next successful write called
        ``_drop_quarantine_sidecar`` and UNLINKED the only surviving copy of rows the
        loader itself had refused -- so the operator lost exactly the data the warning
        told them to repair. A downgrade that writes an older sidecar format reaches this.

        The fix sets ``_load_refused`` on either failure, so every persist raises
        ``AutoNudgeStoreUnvetted`` and the file survives until it is repaired and the
        process restarted.

        Asserts the BYTES are still on disk rather than only that the call raised: a
        raise that happened after the unlink would satisfy a raises-only assertion while
        the data was already gone. Both arms are parametrized because they are separate
        ``return []`` sites, and covering one would leave the other free to regress.
        """
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": []}), encoding="utf-8"
        )
        sidecar = tmp_path / "autonudge.quarantine.json"
        sidecar.write_text(payload, encoding="utf-8")

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            with caplog.at_level("WARNING"):
                svc._load()
            assert (
                svc._load_refused is True
            ), "an unreadable sidecar left writes ENABLED, so the next one unlinks it"
            with pytest.raises(_an.AutoNudgeStoreUnvetted):
                svc._write_state(svc._serialize_state())
            assert not sidecar.exists(), (
                "the unreadable sidecar is still in place, so a restart hits the same "
                "refusal and the outage needs a hand repair"
            )
            assert (
                _moved_aside_sidecar(tmp_path).read_text(encoding="utf-8") == payload
            ), "the sidecar bytes were lost, so held-aside rows are unrecoverable"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_stale_sidecar_row_does_not_roll_back_the_repaired_loop(self, tmp_path) -> None:
        """GPT 5.6 (BLOCKING): a stale sidecar copy overwrote the authoritative row.

        The row loop is LAST-WINS on ``id`` (``self._loops[loop.id] = loop``), and the
        sidecar rows were concatenated AFTER the main store. So if an unlink ever fails
        and the operator then repairs the loop through the API, the next restart replays
        the held-aside copy last and silently rolls the configuration back -- then
        persists the rollback on the following write.

        The fix puts the sidecar rows FIRST, so a same-``id`` main-store row lands on top.

        Both rows carry a SAFE addressing field here: the stale copy has to be one that
        would otherwise arm, or the ordering it is meant to prove is never exercised.
        """
        loop_id = "abc123"
        (tmp_path / "autonudge.quarantine.json").write_text(
            json.dumps(
                {
                    "quarantined": [
                        {
                            "id": loop_id,
                            "slot_key": "chat-1-2",
                            "message": "STALE instruction",
                            "idle_secs": 999,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        (tmp_path / "autonudge.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "loops": [
                        {
                            "id": loop_id,
                            "slot_key": "chat-1-2",
                            "message": "repaired instruction",
                            "idle_secs": 300,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            armed = svc._loops.get(loop_id)
            assert armed is not None, "the repaired loop did not arm at all"
            assert armed.idle_secs == 300, (
                f"the stale sidecar row won: idle_secs={armed.idle_secs}, so a restart "
                "rolled the operator's repair back"
            )
            assert (
                armed.message == "repaired instruction"
            ), "the stale sidecar message overwrote the repaired one"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_held_row_does_not_arm_a_second_timer_on_a_claimed_slot(self, tmp_path) -> None:
        """GPT 5.6 (BLOCKING): a repaired held row armed a DUPLICATE loop on one slot.

        The earlier fix made the store win on duplicate ``id``. But an operator who
        replaces a refused loop through the API gets a NEW id on the same ``slot_key``, so
        the id-keyed last-wins collapse never fires: the held copy and the replacement both
        armed, and that slot then took two unattended turns per cycle.

        Both rows carry SAFE addressing fields here -- the held copy has to be one that
        would otherwise arm, or the de-duplication this pins is never exercised.

        Also asserts the held copy is still HELD. Dropping it to avoid the duplicate would
        trade this bug for the sibling one: the next write would unlink the only copy.
        """
        slot = "chat-7-7"
        (tmp_path / "autonudge.quarantine.json").write_text(
            json.dumps(
                {
                    "quarantined": [
                        {
                            "id": "held-copy",
                            "slot_key": slot,
                            "message": "the held instruction",
                            "idle_secs": 300,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        (tmp_path / "autonudge.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "loops": [
                        {
                            "id": "replacement",
                            "slot_key": slot,
                            "message": "the replacement instruction",
                            "idle_secs": 300,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            on_slot = [loop for loop in svc._loops.values() if loop.slot_key == slot]
            assert len(on_slot) == 1, (
                f"{len(on_slot)} timers armed on one slot, so it takes duplicate "
                f"unattended turns: {sorted(loop.id for loop in on_slot)!r}"
            )
            assert on_slot[0].id == "replacement", (
                "the held copy won the slot instead of the authoritative store row: "
                f"armed={on_slot[0].id!r}"
            )
            assert [row.get("id") for row in svc._quarantined] == ["held-copy"], (
                "the held copy was dropped rather than held aside, so the next write "
                "unlinks the only surviving copy"
            )
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_two_held_rows_sharing_an_id_both_survive_the_additive_write(
        self, tmp_path, monkeypatch
    ) -> None:
        """GPT 5.6 (BLOCKING): keying on ``id`` alone COLLAPSED distinct held rows.

        Two sidecar rows can carry the same id while differing in content -- one repaired,
        one still held. An id-keyed de-duplication treated them as the same row and kept
        only the in-memory copy, so a failed main-store replacement lost the other for
        good. The key has to cover the whole serialized row.
        """
        held = {
            "id": "same-id",
            "slot_key": "chat-1-1",
            "message": "AKIAIOSFODNN7EXAMPLE",
            "idle_secs": 300,
        }
        repaired = {
            "id": "same-id",
            "slot_key": "chat-2-2",
            "message": "a different instruction entirely",
            "idle_secs": 600,
        }
        sidecar = tmp_path / "autonudge.quarantine.json"
        sidecar.write_text(json.dumps({"quarantined": [repaired]}), encoding="utf-8")
        store = tmp_path / "autonudge.json"
        store.write_text(json.dumps({"version": 1, "loops": []}), encoding="utf-8")

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._quarantined = [dict(held)]
            real_replace = _an.replace_with_retry

            def only_the_store_fails(src, dst):
                if Path(dst) == svc._path:
                    raise OSError("store volume is full")
                return real_replace(src, dst)

            monkeypatch.setattr(_an, "replace_with_retry", only_the_store_fails)
            with pytest.raises(OSError):
                svc._write_state({"version": 1, "loops": []})

            on_disk = json.loads(sidecar.read_text(encoding="utf-8"))["quarantined"]
            assert len(on_disk) == 2, (
                "the two same-id rows collapsed to one, so the copy not held in memory is "
                f"lost now that the store replacement failed: on disk={on_disk!r}"
            )
            assert sorted(row["slot_key"] for row in on_disk) == ["chat-1-1", "chat-2-2"]
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_an_unreadable_sidecar_refuses_the_write_and_is_moved_aside(
        self, tmp_path
    ) -> None:
        """GPT 5.6 (BLOCKING) reconciled with Design's availability concern.

        GPT: returning here let the store land and the sidecar compact around rows this
        process never enumerated, so the write must RAISE instead.

        Design: making an unreadable sidecar refuse until a human edits JSON reintroduces
        the "one bad artifact disarms everything" cliff this PR removed for the main store.

        Both: refuse THIS write, and move the file aside so a restart recovers. The bytes
        survive either way, which is what an operator needs to re-inject the rows.
        """
        sidecar = tmp_path / "autonudge.quarantine.json"
        payload = "{ this is not json at all"
        sidecar.write_text(payload, encoding="utf-8")
        store = tmp_path / "autonudge.json"
        store.write_text(json.dumps({"version": 1, "loops": []}), encoding="utf-8")
        before = store.read_text(encoding="utf-8")

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._quarantined = [
                {
                    "id": "held",
                    "slot_key": "chat-3-3",
                    "message": "AKIAIOSFODNN7EXAMPLE",
                    "idle_secs": 300,
                }
            ]
            with pytest.raises(_an.AutoNudgeStoreUnvetted):
                svc._write_quarantine_sidecar()

            assert store.read_text(encoding="utf-8") == before, (
                "the store was replaced around rows nothing enumerated, which is the "
                "overwrite this refusal exists to prevent"
            )
            assert not sidecar.exists(), (
                "the unreadable file is still in place, so every later write hits the "
                "same refusal and the outage needs a hand repair"
            )
            assert (
                _moved_aside_sidecar(tmp_path).read_text(encoding="utf-8") == payload
            ), "the moved-aside copy does not hold the original bytes"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_repaired_row_survives_a_store_replacement_that_fails(
        self, tmp_path, monkeypatch
    ) -> None:
        """GPT 5.6 (BLOCKING): the pre-replacement sidecar write could DELETE a row.

        The crash window: one row is repaired this pass (so it leaves ``_quarantined``)
        while a sibling stays held. Writing the in-memory set alone lands a REDUCED
        sidecar, and when the main-store replacement then fails, the repaired row is in
        neither file -- the store still holds old content that never had it.

        So the pre-replacement write must be ADDITIVE, with compaction waiting for the
        store to land. Asserts on the FILE, because that is all a restart can read.
        """
        repaired = {
            "id": "repaired",
            "slot_key": "chat-9-9",
            "message": "operator fixed this one",
            "idle_secs": 300,
        }
        still_held = {
            "id": "still-held",
            "slot_key": "chat-8-8",
            "message": "AKIAIOSFODNN7EXAMPLE",
            "idle_secs": 300,
        }
        sidecar = tmp_path / "autonudge.quarantine.json"
        sidecar.write_text(json.dumps({"quarantined": [repaired, still_held]}), encoding="utf-8")
        store = tmp_path / "autonudge.json"
        store.write_text(json.dumps({"version": 1, "loops": []}), encoding="utf-8")
        before = store.read_text(encoding="utf-8")

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            # The repaired row armed and left the held set; the sibling stayed.
            svc._quarantined = [dict(still_held)]

            real_replace = _an.replace_with_retry

            def only_the_store_fails(src, dst):
                if Path(dst) == svc._path:
                    raise OSError("store volume is full")
                return real_replace(src, dst)

            monkeypatch.setattr(_an, "replace_with_retry", only_the_store_fails)
            with pytest.raises(OSError):
                svc._write_state({"version": 1, "loops": []})

            assert store.read_text(encoding="utf-8") == before, (
                "the store changed even though its replacement raised, so this test is "
                "not measuring the crash window it claims to"
            )
            on_disk = json.loads(sidecar.read_text(encoding="utf-8"))["quarantined"]
            ids = sorted(str(row.get("id")) for row in on_disk)
            assert ids == ["repaired", "still-held"], (
                "the repaired row is gone from the sidecar while the store still holds "
                f"its old content, so a restart loses it permanently: on disk={ids!r}"
            )
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_the_sidecar_is_compacted_once_the_store_lands(self, tmp_path) -> None:
        """The additive write must not let the sidecar grow without bound.

        Additive is only correct BEFORE the replacement; once the store is durable the
        rows it superseded have to go, or every repaired row accumulates forever.
        """
        stale = {"id": "gone", "slot_key": "chat-1-1", "message": "x", "idle_secs": 300}
        sidecar = tmp_path / "autonudge.quarantine.json"
        sidecar.write_text(json.dumps({"quarantined": [stale]}), encoding="utf-8")
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": []}), encoding="utf-8"
        )

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._quarantined = [
                {
                    "id": "held",
                    "slot_key": "chat-2-2",
                    "message": "AKIAIOSFODNN7EXAMPLE",
                    "idle_secs": 300,
                }
            ]
            svc._write_state({"version": 1, "loops": []})

            on_disk = json.loads(sidecar.read_text(encoding="utf-8"))["quarantined"]
            assert [row.get("id") for row in on_disk] == ["held"], (
                "the superseded row was still on disk after the store landed, so the "
                f"sidecar grows without bound: {[r.get('id') for r in on_disk]!r}"
            )
        finally:
            svc.stop()

    @pytest.mark.parametrize(
        "held",
        ['{"id": "x"}', "null", '"a string"', "7"],
        ids=["object", "null", "string", "number"],
    )
    @pytest.mark.asyncio
    async def test_a_non_list_quarantined_refuses_rather_than_reading_as_empty(
        self, tmp_path, held
    ) -> None:
        """GPT 5.6 (BLOCKING): a non-list ``quarantined`` was silently deleted.

        ``_rows_or_empty`` answers anything that is not a list with ``[]``, which is
        indistinguishable from "nothing is held aside". The loader armed normally and the
        next persist called ``_drop_quarantine_sidecar``, unlinking the only copy of rows
        an operator still had to repair.

        ``null`` is parametrized alongside the object shape because it is equally
        "present but not a list" and equally reads as empty through that helper.
        """
        sidecar = tmp_path / "autonudge.quarantine.json"
        payload = '{"quarantined": ' + held + "}"
        sidecar.write_text(payload, encoding="utf-8")
        (tmp_path / "autonudge.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "loops": [
                        {
                            "id": "keep",
                            "slot_key": "chat-1-1",
                            "message": "fine",
                            "idle_secs": 300,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert (
                svc._load_refused is True
            ), "a non-list `quarantined` read as empty, so the next write unlinks it"
            assert not svc._loops, (
                "loops armed under a refused store; a delivered cycle cannot record "
                f"itself, so a restart repeats it. armed={sorted(svc._loops)!r}"
            )
            with pytest.raises(_an.AutoNudgeStoreUnvetted):
                svc._write_state(svc._serialize_state())
            assert (
                _moved_aside_sidecar(tmp_path).read_text(encoding="utf-8") == payload
            ), "the sidecar bytes were lost, so the operator has nothing to repair"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_refused_row_does_not_strand_the_loops_loaded_before_it(self, tmp_path) -> None:
        """GPT 5.6 (BLOCKING): a declined row stranded every loop already loaded.

        ``_loops[loop.id] = loop`` runs per row inside the load loop, and ``_load`` does
        not reset ``_loops`` on entry, so rows BEFORE the unusable one were already
        armed when the refusal latched. Each then fired once, its post-fire persist
        raised ``AutoNudgeStoreUnvetted``, and it never re-armed -- so a single bad
        addressing field silently froze healthy channel loops mid-cycle.

        Quarantining the offending row is what makes that unreachable: the row is held
        aside under ``quarantined`` and its SIBLINGS still arm, so ``_load_refused``
        stays False. The whole-store refusal is a different arm, for a host whose
        credential policy will not compose at all, and the file on disk is untouched
        either way.

        Order matters -- the clean row is FIRST, so it is in ``_loops`` by the time the
        second row is declined. A fixture with the bad row first would pass even
        unfixed.
        """
        rows = [
            {"id": "clean-1", "slot_key": "chat-1-1", "message": "one", "idle_secs": 300},
            {"id": self.SECRET, "slot_key": "chat-2-2", "message": "two", "idle_secs": 300},
            {"id": "clean-2", "slot_key": "chat-3-3", "message": "three", "idle_secs": 300},
        ]
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": rows}), encoding="utf-8"
        )

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert svc._load_refused is False, "one bad row disarmed the whole store"
            assert sorted(svc._loops) == [
                "clean-1",
                "clean-2",
            ], f"healthy loops were disarmed by one bad row: {sorted(svc._loops)!r}. Quarantine holds the offending row and leaves its siblings armed"
            svc._write_state(svc._serialize_state())
            assert _held_aside_rows(tmp_path), "the declined row was dropped"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_clean_loop_still_arms(self, tmp_path) -> None:
        """Negative control: the guard must not refuse ordinary rows.

        A predicate that rejected everything -- or that compared the wrong pair of
        values -- would pass every arm above while disarming the whole fleet.
        """
        self._write(tmp_path)
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            assert "abc123" in svc._loops, "a clean loop was refused"
            assert svc._loops["abc123"].slot_key == "chat-1-2"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_the_refusal_does_not_rewrite_the_addressing_value(self, tmp_path) -> None:
        """Refused, NOT scrubbed. Scrubbing would rewrite the identity the client
        resolves the row by, leaving a row that is displayed but unactionable --
        so the loop must be absent entirely rather than present under a mangled
        id."""
        self._write(tmp_path, id=f"loop-{self.SECRET}")
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            assert svc._loops == {}
            assert not any("REDACTED" in k for k in svc._loops), "a scrubbed id was armed"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_the_refusal_leaves_the_store_row_for_the_operator(self, tmp_path) -> None:
        """The warning says "fix the store entry", so the entry must still be there.

        Found by a failing test, not by reasoning: ``_load``'s banner repair sets
        ``_store_dirty``, and ``start()`` then persists ``self._loops`` -- which no
        longer holds the refused row. Without suppression the refusal DELETED the
        very entry the operator was told to fix, and the on-disk ``loops`` list came
        back empty. The banner here is a list so the repair arm fires and arms the
        dirty flag, which is the condition that made it destructive.
        """
        row = {
            "id": f"loop-{self.SECRET}",
            "slot_key": "chat-1-2",
            "message": "keep going",
            "idle_secs": 300,
            "banner": ["not-a-string"],
        }
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": [row]}), encoding="utf-8"
        )
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            assert svc._loops == {}, "the loop was armed"
            held = _held_aside_rows(tmp_path)
            assert len(held) == 1, "the refusal deleted the operator's row"
            assert held[0]["id"] == row["id"], "the persisted identity changed"
        finally:
            svc.stop()

    def test_the_serializer_and_the_loader_share_one_field_set(self) -> None:
        """Two copies could drift so the serializer exempts a field the loader
        does not guard -- which is exactly the hole the exemption would open."""
        from kiro_crew.autonudge import ADDRESSING_FIELDS

        assert autonudge_handlers._UNSCRUBBED_FIELDS is ADDRESSING_FIELDS
        assert sorted(ADDRESSING_FIELDS) == ["id", "slot_key"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", ["id", "slot_key"])
    async def test_a_non_printable_addressing_field_is_refused(
        self, tmp_path, caplog, field
    ) -> None:
        """GPT 5.6 (BLOCKING): ``redact`` is not a control-character guard.

        ``redact`` rewrites credential- and URL-shaped text, so a newline rides
        straight through ``redact(got) != got`` -- the row constructs cleanly, arms,
        and the id then reaches ~15 bare ``%s`` log calls, where one newline splits
        a record in two and the operator reads an attacker-authored second line as
        the gateway's own.

        This is a DIFFERENT path from
        ``TestScrubbedLogTextCannotForgeARecord`` above: that fixture omits
        ``slot_key`` so construction fails and the malformed-entry arm runs. Here
        every field is present and well-typed, so the row reaches the addressing
        guard -- which is the arm that used to let it through.
        """
        forged = "2026-01-01 00:00:00 WARNING FORGED: gateway compromised"
        self._write(tmp_path, **{field: f"abc123\n{forged}"})
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            with caplog.at_level("WARNING"):
                await svc.start()
            assert svc._loops == {}, "a newline-bearing addressing field was armed anyway"
            assert "refusing loop" in caplog.text, "the refusal was silent"
            assert field in caplog.text, "the warning does not name the offending field"
            refusals = [r for r in caplog.records if "refusing loop" in r.getMessage()]
            assert refusals, "no refusal record was emitted"
            for rec in refusals:
                msg = rec.getMessage()
                # The property is that the record cannot be SPLIT, not that the
                # text is absent. The warning renders the id through
                # ``redact(repr(...))``, and ``repr`` turns the newline into a
                # literal backslash-n, so the injected tail stays inert on one
                # line. Asserting absence instead would fail here for the right
                # reason and the wrong claim.
                assert "\n" not in msg, f"the refusal itself was split in two: {msg!r}"
                if forged in msg:
                    assert "\\n" in msg, "the newline reached the record unescaped"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad",
        [
            pytest.param("abc\rdef", id="carriage-return"),
            pytest.param("abc\tdef", id="tab"),
            pytest.param("abc\x1b[31mdef", id="ansi-escape"),
            pytest.param("abc\x00def", id="nul"),
        ],
    )
    async def test_other_non_printables_are_refused_too(self, tmp_path, bad) -> None:
        """The predicate is a class of characters, not one special-cased newline.

        Measured: none of these is caught by ``redact``, so before this guard every
        one of them armed and reached the log sinks.
        """
        self._write(tmp_path, id=bad)
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            assert svc._loops == {}, f"{bad!r} was armed"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_printable_addressing_value_is_still_accepted(self, tmp_path) -> None:
        """NEGATIVE CONTROL for the new arm, and it must be able to fail.

        ``str.isprintable()`` is False for an EMPTY string's opposite reasons and
        True for the ASCII space, so a guard written as ``got.isascii()`` or as a
        whitespace ban would refuse ordinary keys. A real slot key carries hyphens
        and digits; refuse those and the whole fleet disarms while every arm above
        still passes.
        """
        self._write(tmp_path, id="chat-1281-1785676802", slot_key="chat-1281-1785676802")
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            assert "chat-1281-1785676802" in svc._loops, "an ordinary printable id was refused"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_the_non_printable_refusal_keeps_the_row_on_disk(self, tmp_path) -> None:
        """The refusal is unchanged; the row now SURVIVES the next write.

        This arm previously pinned the DROP, which was the data-loss defect itself
        (GPT 5.6, BLOCKING): the operator was warned by name about a field in an entry
        that the next wholesale write had already deleted. The loop not arming is the
        security property and it is untouched -- only the row's fate on disk changed.
        """
        bad = "abc123\nFORGED"
        self._write(tmp_path, id=bad)
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            assert svc._loops == {}, "the loop was armed"
            assert (
                svc._load_refused is False
            ), "one unusable row refused the whole store instead of being quarantined"
            await svc._persist_locked()
            assert _held_aside_rows(tmp_path), "the declined row was dropped"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_id, why",
        [
            pytest.param("loop-AKIAIOSFODNN7EXAMPLE", "credential", id="credential-shaped"),
            pytest.param("loop\nFORGED-LINE", "non-printable", id="non-printable"),
            pytest.param(["loop-1"], "non-string", id="non-string"),
        ],
    )
    async def test_validation_runs_BEFORE_the_monitor_warning_sink(
        self, tmp_path, caplog, bad_id, why
    ) -> None:
        """GPT 5.6 (BLOCKING): ordering IS the control here.

        ``_load`` used to validate the addressing fields only AFTER the
        quarantined-malformed-monitor warning, which interpolates a bare
        ``loop.id``. A row that is unsafe in ``id`` AND carries a monitor that will
        not parse therefore hit that ``%s`` first, putting the raw value into the
        log ring and ``/api/logs`` before anything refused it.

        This fixture is that exact row: an unsafe ``id`` plus ``monitor`` set to a
        value ``monitor_state_from_dict`` rejects. The assertions are about ORDER --
        the row must be refused and NO record may carry the raw value. A guard that
        still validates but does so too late passes an ordinary refusal test and
        fails this one.
        """
        self._write(tmp_path, id=bad_id, monitor={"not": "a valid monitor"})
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            with caplog.at_level("DEBUG"):
                await svc.start()

            # (a) refused
            assert svc._loops == {}, f"an id that is {why} was armed anyway"
            assert "refusing loop" in caplog.text, "the refusal was silent"

            # (b) the REFUSAL's own record may name the id -- that is its job, and it
            # renders it through ``redact(repr(...))`` so a credential is stripped and
            # a control character is escaped. What must never happen is any OTHER sink
            # carrying the value, and no record may be split by a real newline.
            #
            # Scoped to THIS module's logger on purpose. ``caplog`` at DEBUG also
            # captures unrelated records (config-deprecation notices, the sandbox
            # userns probe), some of which are legitimately multi-line -- asserting
            # over every captured record made the arm depend on what else happened to
            # log during the test, which failed 1 run in 4 for a reason that had
            # nothing to do with the code under test.
            needle = bad_id if isinstance(bad_id, str) else repr(bad_id)
            mine = [r for r in caplog.records if r.name == "kiro_crew.autonudge"]
            assert mine, "no autonudge record was captured -- the fixture never reached _load"
            for rec in mine:
                msg = rec.getMessage()
                assert "\n" not in msg, f"a record was split into several lines: {msg!r}"
                if "refusing loop" in msg:
                    # the intended disclosure: scrubbed and escaped
                    assert self.SECRET not in msg, "the refusal echoed the credential unredacted"
                    continue
                assert needle not in msg, f"a non-refusal record carried the id: {msg!r}"

            # (c) the sink that used to leak must not have run for this row at all --
            # reaching it is what the relocation prevents.
            assert not [
                r for r in mine if "quarantined malformed monitor" in r.getMessage()
            ], "the monitor sink ran on a row that should have been refused first"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_malformed_monitor_on_a_SAFE_row_still_quarantines(self, tmp_path) -> None:
        """Positive control for the arm above, so its silence is not vacuous.

        The ordering assertion checks that the monitor sink did NOT run. That is only
        meaningful if the sink runs at all on a row whose addressing fields are fine
        -- otherwise the fixture could be wrong about what triggers it and the
        assertion would pass for the wrong reason.
        """
        self._write(tmp_path, monitor={"not": "a valid monitor"})
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            loop = svc._loops.get("abc123")
            assert loop is not None, "a safe row with a bad monitor was refused"
            assert loop.monitor is not None, "the monitor was not quarantined"
        finally:
            svc.stop()


class TestTheStopSentinelSurvivesARefusedArm:
    """GPT 5.6 (BLOCKING): the policy refusal ran AFTER deleting the stop sentinel.

    The auto-default path resolves a per-session sentinel and unlinks it, so a fresh loop
    does not inherit a stale stop signal. That unlink is unconditional
    (``unlink(missing_ok=True)``) and it sat BEFORE the 503 policy probe. So on a host
    whose credential policy cannot compose: the operator's live stop file for an
    ALREADY-RUNNING loop was deleted, and only then was the arm refused -- leaving the old
    unattended loop running with its stop signal gone. Deleting a control-plane file and
    then declining to do the work is the data loss.

    Both 503 sites test the same condition, but they are NOT interchangeable, which is why
    the second one had to move rather than be dropped: the earlier one only fires when
    ``normalize_banner`` actually scrubs, and it returns early on a BLANK banner. A
    bannerless arm -- the common case, and the only shape a channel key permits -- reaches
    the sentinel block with the policy still unprobed. So the probe is lifted above the
    unlink instead.

    ``test_the_unlink_still_happens_on_a_healthy_host`` is the negative control: it fails
    if the unlink is simply removed, which would break the documented reason it exists
    ("per-session sentinel so multiple loops don't clash").
    """

    SLOT = "chat-1281-1785676802"
    WORKSPACE = "default"

    class _BrokenPolicy:
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

    def _state(self):
        """A REAL dashboard slot: the auto-default branch needs one to resolve a path."""
        return SimpleNamespace(
            _slots={self.SLOT: MagicMock(workspace=self.WORKSPACE)},
            sessions=None,
            channel_transports={},
            push_slots_update=lambda: None,
        )

    def _sentinel(self) -> Path:
        """The exact path the auto-default branch resolves for this slot."""
        return Path(authz.resolve_stop_sentinel(self.SLOT, self.WORKSPACE))

    @pytest.mark.asyncio
    async def test_a_refused_arm_leaves_a_live_stop_file_alone(self, tmp_path) -> None:
        """THE finding: refuse first, so an existing stop signal is never destroyed."""
        sentinel = self._sentinel()
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text("STOP", encoding="utf-8")
        assert sentinel.exists(), "fixture failed to place the sentinel"

        svc = MagicMock()
        svc.list_all = lambda: []
        svc.get_by_id = lambda _id: None
        svc.add = AsyncMock()
        self._install(self._BrokenPolicy())
        try:
            loop, error, status = await authz.authorize_and_add_nudge(
                svc=svc,
                state=self._state(),
                slot_key=self.SLOT,
                message="keep going",
                source="dashboard",
            )
            assert status == 503, f"expected the policy refusal, got {status} {error!r}"
            assert loop is None
            svc.add.assert_not_awaited()
            assert sentinel.exists(), (
                "the arm deleted the operator's live stop file and THEN refused -- the "
                "already-running loop is now unattended with no way to stop it"
            )
            assert sentinel.read_text(encoding="utf-8") == "STOP", "the file was rewritten"
        finally:
            sentinel.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_the_unlink_still_happens_on_a_healthy_host(self, tmp_path) -> None:
        """NEGATIVE CONTROL: the fix must reorder the probe, not delete the unlink.

        A stale sentinel left in place would stop the NEW loop on its first cycle, which
        is exactly what the auto-default unlink exists to prevent. This arm fails if the
        unlink is removed rather than moved.
        """
        sentinel = self._sentinel()
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text("STOP", encoding="utf-8")

        armed = SimpleNamespace(id="loop-1", slot_key=self.SLOT, message="m", banner="")
        svc = MagicMock()
        svc.list_all = lambda: []
        svc.get_by_id = lambda _id: None
        svc.add = AsyncMock(return_value=armed)
        # A healthy policy: the probe passes, so the unlink must still run.
        self._install(SimpleNamespace(redact=lambda text: text))
        try:
            _loop, error, status = await authz.authorize_and_add_nudge(
                svc=svc,
                state=self._state(),
                slot_key=self.SLOT,
                message="keep going",
                source="dashboard",
            )
            assert status == 200, f"the healthy arm was refused: {status} {error!r}"
            assert not sentinel.exists(), (
                "the stale stop file survived a successful arm -- the new loop will stop "
                "on its first cycle; the probe must be REORDERED, not the unlink removed"
            )
        finally:
            sentinel.unlink(missing_ok=True)


class TestAClientCanDetectTheDestructiveEcho:
    """Design: the PATCH echo guard is a success-that-isn't.

    ``authorize_and_update_nudge`` compares an incoming ``message`` against
    ``scrub_loop_text(current.message)`` and, on a match, sets it to ``None`` and answers
    200. So a client that read the loop, changed an unrelated field and PATCHed the whole
    object back was told the write succeeded while its ``message`` was discarded. The
    popover's dirty check stops OUR client doing it and a log line records it, but neither
    reaches a third-party client -- the API contract itself said nothing.

    The contract now says it on BOTH surfaces, because a GET-only flag is not enough:

    * **GET** carries ``message_redacted`` -- true when the served projection differs from
      the stored value, i.e. echoing it back WOULD be destructive. A client that reads
      before writing can now see that in advance.
    * **PATCH** carries ``message_ignored`` -- true when the guard kept the stored goal.
      A client that never GETs first learns nothing from the GET flag, so the response to
      the lossy write has to say so itself. Without this arm the finding is only half
      answered.

    ``test_a_clean_message_is_not_flagged`` is the negative control: a message with nothing
    credential-shaped in it round-trips unchanged, so the flag must be false and
    ``message_ignored`` absent. A flag hardcoded true, or one keyed on "did we scrub at all"
    rather than "did the value change", passes the arms above and fails this one.
    """

    SECRET = "AKIAIOSFODNN7EXAMPLE"

    @staticmethod
    def _loop(message: str):
        return NudgeLoop(id="loop-1", slot_key="chat-1-123", message=message)

    def test_the_GET_projection_flags_a_redacted_message(self) -> None:
        """A client reading first must be able to see that an echo would destroy data."""
        served = autonudge_handlers._serialize(self._loop(f"deploy with {self.SECRET}"))
        assert self.SECRET not in served["message"], "the projection did not scrub"
        assert served.get("message_redacted") is True, (
            "the projection gives a client no way to know its `message` differs from the "
            "stored value, so echoing it back silently destroys the original"
        )

    def test_a_clean_message_is_not_flagged(self) -> None:
        """NEGATIVE CONTROL: the flag must track CHANGE, not merely 'we ran a scrubber'."""
        served = autonudge_handlers._serialize(self._loop("just keep going"))
        assert served["message"] == "just keep going", "a clean message was altered"
        assert served.get("message_redacted") is False, (
            "a message that round-trips unchanged was flagged as redacted, so the flag "
            "cannot tell a client whether an echo is actually destructive"
        )

    @pytest.mark.asyncio
    async def test_the_PATCH_response_names_the_ignored_field(self, tmp_path) -> None:
        """A client that never GETs first must still learn the field was discarded."""
        stored = self._loop(f"deploy with {self.SECRET}")
        echoed = _an.scrub_loop_text(stored.message, field="message")
        assert echoed != stored.message, "fixture failed: the message was not redacted"

        svc = MagicMock()
        svc.list_all = lambda: [stored]
        svc.get_by_id = lambda _id, _s=stored: _s if _id == _s.id else None
        svc.update = AsyncMock(return_value=stored)
        loop, error, status = await authz.authorize_and_update_nudge(
            svc=svc,
            loop_id="loop-1",
            message=echoed,
            source="dashboard",
        )
        assert status == 200, f"the echo was refused rather than ignored: {status} {error!r}"
        assert "message" not in (svc.update.await_args.kwargs or {}) or (
            svc.update.await_args.kwargs.get("message") is None
        ), "the echoed message was written through"
        ignored = svc.update.await_args.kwargs.get("message", "sentinel") is None
        assert ignored is True, (
            f"the update path does not report the kept goal: {ignored!r}. A client that "
            "did not GET first is told 200 with no indication its message was discarded"
        )


class TestTheTwoSkipArmsHoldNoRow:
    """GPT 5.6 (BLOCKING) + First Principles (Subtraction): the hold had to go.

    ``_load`` has TWO arms that decline a row, and they disagreed:

    * the malformed-entry ``except`` arm ``continue``s with no hold, so the row is
      dropped by the next wholesale write -- the contract ``cron.py`` documents
      ("dropped from the store on the next write");
    * the addressing-guard arm HELD the row and wrote it back, which then needed a
      retirement on slot close, a rollback for that retirement, and a rollback in
      ``_add_locked`` -- three hand-maintained paths for one invariant.

    The third path could not be completed. An aborted slot close rolls back through
    ``chat_handlers._restore_slot_nudge_loop(exc.loop, ...)`` where
    ``exc.loop = svc.get_by_slot(name)``, and ``get_by_slot`` searches ``_loops`` only --
    so for a refused row it is ``None`` and the caller has no token to restore. The
    retirement had already reached disk, so the row was permanently gone. That gap sits
    in a caller autonudge does not own, which is why no fourth rollback could close it.

    So the hold is gone, and NEITHER arm holds a row. This test pins that: it FAILS while
    one arm holds and the other drops, and passes once neither does.

    The arms are no longer identical at the WRITE, and deliberately so. A row declined by
    the addressing guard is QUARANTINED -- re-emitted under ``quarantined`` so the entry
    stays on disk to be repaired, because dropping it silently was a data-loss defect
    (GPT 5.6, BLOCKING). The malformed-entry arm still drops its row. What both share, and
    what this class covers, is that no row is HELD IN ``_loops``: with nothing in the live
    map there is no retirement, no retirement rollback, and nothing for an aborted close to
    restore. A quarantined row is invisible to ``get_by_slot``, so it cannot enter that path.
    """

    SECRET = "AKIAIOSFODNN7EXAMPLE"

    @staticmethod
    def _write(tmp_path, rows) -> None:
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": rows}), encoding="utf-8"
        )

    @pytest.mark.asyncio
    async def test_the_two_skip_arms_agree_and_differ_only_in_preservation(self, tmp_path) -> None:
        """Both arms skip the row and keep loading; only preservation differs.

        A MALFORMED row is dropped and loading continues -- the contract the sibling cron
        loader documents. An unusable ADDRESSING field is QUARANTINED: also skipped, also
        non-fatal to its siblings, but re-emitted under ``quarantined`` so the entry an
        operator was told to repair survives the next wholesale write.

        Refusing the WHOLE store was the earlier answer to that data-loss defect, and it
        cost every healthy loop in the file (design-review: availability cliff). Neither
        arm holds a row in ``_loops``, which ``test_no_held_row_state_exists_to_be_lost``
        pins, so the rollback gap that killed the original hold stays unreachable.
        """
        good = {"id": "keep", "slot_key": "chat-1-1", "message": "fine", "idle_secs": 300}
        malformed = {"id": ["not", "a", "string"], "slot_key": "chat-2-2"}
        bad_addr = {"id": self.SECRET, "slot_key": "chat-3-3", "message": "x"}

        # Malformed alone: dropped, and the sibling arms.
        self._write(tmp_path, [good, malformed])
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert [lp.id for lp in svc.list_all()] == ["keep"], "the good row did not arm"
            assert svc._load_refused is False, "a malformed row refused the whole store"
            payload_ids = [r.get("id") for r in svc._serialize_state()["loops"]]
            assert payload_ids == ["keep"], f"the malformed row was carried: {payload_ids!r}"
        finally:
            svc.stop()

        # Unusable addressing field: quarantined, and the sibling still arms.
        self._write(tmp_path, [good, bad_addr])
        svc2 = AutoNudgeService(base_dir=tmp_path)
        try:
            svc2._load()
            assert svc2._load_refused is False, "an unusable addressing field refused the store"
            assert [lp.id for lp in svc2.list_all()] == [
                "keep"
            ], f"a bad row disarmed its sibling: {sorted(svc2._loops)!r}"
            assert self.SECRET not in svc2._loops, "the credential-shaped row was armed"
            payload = svc2._serialize_state()
            assert [r.get("id") for r in payload["loops"]] == ["keep"]
            assert [r.get("id") for r in svc2._quarantined] == [
                self.SECRET
            ], "the declined row was not preserved for repair"
        finally:
            svc2.stop()

    def test_no_held_row_state_exists_to_be_lost(self) -> None:
        """Structural: the data-loss path is unreachable because nothing is held.

        A rollback gap can only lose state that exists. With no hold there is no
        retirement, no retirement rollback, and nothing for an aborted close to fail to
        restore -- which is what makes the hazard unreachable rather than merely guarded.
        """
        import inspect

        src = inspect.getsource(_an)
        for gone in (
            "_refused_raw_rows",
            "_retire_refused_rows_for_slot",
            "held_before_retire",
        ):
            assert gone not in src, f"{gone} still exists, so the loss path is still live"
        # The whole-store refusal is a DIFFERENT mechanism and must survive.
        assert "_load_refused" in src, "the whole-store refusal was removed too"

    @pytest.mark.asyncio
    async def test_an_aborted_close_cannot_lose_a_refused_row(self, tmp_path) -> None:
        """The finding's own scenario, made harmless.

        The row is not in memory and not in the payload from the moment it is refused, so
        a close that retires nothing and then aborts has nothing to delete.
        """
        self._write(tmp_path, [{"id": self.SECRET, "slot_key": "chat-1-1", "message": "x"}])
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            before_ids = [r.get("id") for r in svc._serialize_state()["loops"]]
            await svc.remove_by_slot("chat-1-1")
            after_ids = [r.get("id") for r in svc._serialize_state()["loops"]]
            assert before_ids == after_ids == [], (
                f"a close changed refused-row state: {before_ids!r} -> {after_ids!r}; "
                "with no hold there is nothing for an aborted close to lose"
            )
        finally:
            svc.stop()


class TestANullStoreKeyDoesNotAbortStartup:
    """A store key present but null must not crash ``_load``.

    ``data.get(key, [])`` yields the default only when the key is ABSENT, so a
    hand-edited store carrying ``"quarantined": null`` returned ``None`` and the
    comprehension raised ``TypeError`` uncaught at gateway startup. The sibling
    ``loops`` key is unpacked at the same site and had the identical hazard.
    """

    @staticmethod
    def _write(tmp_path, payload) -> None:
        (tmp_path / "autonudge.json").write_text(json.dumps(payload), encoding="utf-8")

    GOOD = {"id": "keep", "slot_key": "chat-1-1", "message": "fine", "idle_secs": 300}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", [None, "not-a-list", 7, {"id": "x"}])
    async def test_a_non_list_quarantined_value_still_arms_the_clean_loops(
        self, tmp_path, bad
    ) -> None:
        """``_load`` completes and the well-formed loop arms."""
        self._write(tmp_path, {"version": 1, "loops": [self.GOOD], "quarantined": bad})
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert [lp.id for lp in svc.list_all()] == [
                "keep"
            ], f"a {type(bad).__name__} quarantined value disarmed the clean loop"
            assert svc._load_refused is False, "a malformed quarantined value refused the store"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", [None, "not-a-list", 7])
    async def test_a_non_list_loops_value_does_not_crash_the_load(self, tmp_path, bad) -> None:
        """The sibling key is unpacked at the same site, so it needs the same guard."""
        self._write(tmp_path, {"version": 1, "loops": bad, "quarantined": []})
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert svc.list_all() == [], f"a {type(bad).__name__} loops value armed something"
        finally:
            svc.stop()


class TestARepairedQuarantinedRowRearms:
    """A quarantined row is revalidated on every load, not carried forward blindly.

    Quarantine holds a row an operator was told to repair. If ``_load`` only ever
    validated ``loops``, a repaired row would stay disarmed forever and the operator
    would have to hand-edit it back into ``loops`` to recover it.
    """

    SECRET = "AKIAIOSFODNN7EXAMPLE"

    @staticmethod
    def _write(tmp_path, loops, quarantined) -> None:
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": loops}),
            encoding="utf-8",
        )
        (tmp_path / "autonudge.quarantine.json").write_text(
            json.dumps({"version": 1, "quarantined": quarantined}),
            encoding="utf-8",
        )

    @pytest.mark.asyncio
    async def test_a_repaired_quarantined_row_rearms(self, tmp_path) -> None:
        """A quarantined row whose addressing fields now pass is armed again."""
        good = {"id": "keep", "slot_key": "chat-1-1", "message": "fine", "idle_secs": 300}
        repaired = {"id": "fixed", "slot_key": "chat-2-2", "message": "back", "idle_secs": 300}
        self._write(tmp_path, [good], [repaired])
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert sorted(lp.id for lp in svc.list_all()) == [
                "fixed",
                "keep",
            ], f"the repaired row did not re-arm: {sorted(svc._loops)!r}"
            payload = svc._serialize_state()
            assert (
                not svc._quarantined
            ), f"the repaired row stayed quarantined: {svc._quarantined!r}"
            assert sorted(r.get("id") for r in payload["loops"]) == ["fixed", "keep"]
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_still_unsafe_quarantined_row_stays_quarantined(self, tmp_path) -> None:
        """Revalidation rebuilds quarantine from the rows that STILL fail."""
        good = {"id": "keep", "slot_key": "chat-1-1", "message": "fine", "idle_secs": 300}
        still_bad = {"id": self.SECRET, "slot_key": "chat-3-3", "message": "x"}
        self._write(tmp_path, [good], [still_bad])
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert [lp.id for lp in svc.list_all()] == [
                "keep"
            ], f"an unsafe row was reactivated: {sorted(svc._loops)!r}"
            assert self.SECRET not in svc._loops, "the credential-shaped row was armed"
            assert [r.get("id") for r in svc._quarantined] == [
                self.SECRET
            ], "the still-unsafe row was not preserved for repair"
        finally:
            svc.stop()


class TestARefusalDoesNotDestroyTheStore:
    """A whole-store refusal must not overwrite the operator's file with nothing.

    Scoped to the WHOLE-STORE arm only, which is the one this class still covers: a host
    that cannot compose its credential policy, so no row's addressing fields can be
    vetted. ``_loops`` is then empty because nothing could be checked rather than because
    the store is empty, so persisting it would delete every row. Every write raises
    ``AutoNudgeStoreUnvetted`` instead.

    A SINGLE unusable row does NOT arm that refusal: it is QUARANTINED instead, because
    dropping it deleted the entry the operator was told to repair, and refusing the whole
    store would have disarmed its healthy siblings. That arm is covered by
    ``TestCredentialShapedAddressingFieldsAreRefused``, which also discriminates the two
    outcomes -- a clean row still arms there, which a whole-store refusal would prevent.

    The refusal itself keeps its own tests
    (``TestCredentialShapedAddressingFieldsAreRefused`` and
    ``TestTheAddressingGuardUsesTheActiveCredentialPolicy``).
    """

    SECRET = "AKIAIOSFODNN7EXAMPLE"

    class _BrokenPolicy:
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

    def _write(self, tmp_path, rows) -> None:
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": rows}), encoding="utf-8"
        )

    @pytest.mark.asyncio
    async def test_an_unvettable_store_refuses_the_write_instead(self, tmp_path) -> None:
        """Arm two, and the one that matters: the file must survive untouched.

        This is the arm the old machinery existed for. Without the write refusal the
        subtraction WOULD have deleted the operator's store, because every row is
        refused here and the payload is therefore empty.
        """
        self._write(tmp_path, [{"id": "a", "slot_key": "chat-1-1", "message": "one"}])
        original = (tmp_path / "autonudge.json").read_text(encoding="utf-8")
        self._install(self._BrokenPolicy())

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert svc.list_all() == [], "a loop armed on an unvettable store"
            with pytest.raises(_an.AutoNudgeStoreUnvetted):
                svc._write_state(svc._serialize_state())
            after = (tmp_path / "autonudge.json").read_text(encoding="utf-8")
            assert after == original, (
                "the store was overwritten while unvettable -- the write refusal is the "
                "only thing standing between this state and total data loss"
            )
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_healthy_store_still_persists(self, tmp_path) -> None:
        """PRESERVED: the refusal must not block ordinary writes."""
        self._write(tmp_path, [])
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            armed = await svc.add(slot_key="chat-9-9", message="keep going")
            svc._write_state(svc._serialize_state())
            on_disk = json.loads((tmp_path / "autonudge.json").read_text(encoding="utf-8"))
            assert [r["id"] for r in on_disk["loops"]] == [
                armed.id
            ], f"a healthy write did not land: {on_disk!r}"
        finally:
            svc.stop()


class TestTheObserverBroadcastIsScrubbed:
    """Opus 4.8 (FINDING): the ``autonudge_state`` WS broadcast leaked ``message``.

    ``_serialize`` closed the REST path, but the observer shipped ``loop.message``
    verbatim to every connected dashboard client, where ``AutoNudgePopover`` renders
    it raw. Three producers reach ``svc.add`` without the authorizer, so a
    credential could arrive and go straight out over the socket.
    """

    SECRET = "AKIAIOSFODNN7EXAMPLE"

    def test_the_observer_scrubs_the_message_before_broadcasting(self) -> None:
        """Source-level: the payload must not interpolate ``loop.message`` raw.

        A cheap structural guard that nobody re-inlines the raw value. The
        BEHAVIOUR is proved in ``TestNonStringMessageDoesNotDropTheObserverBroadcast``
        by driving the real observer; this arm only pins the call site, so it
        asserts the scrub CALL rather than the redactor pair -- the pair moved into
        ``scrub_loop_text`` when the socket and REST surfaces were made to share one
        definition.

        The expected spelling carries ``field="message"`` because the numeric
        exemption is keyed on the FIELD now, not on the value's type: a call that
        omitted the field would take the coercion path's default and still be safe,
        but it would no longer be the same rule the REST projection applies, and the
        two surfaces sharing ONE rule is the property this pair of tests exists to
        protect. Pinning the full spelling is what makes a silent divergence fail.
        """
        import inspect

        src = inspect.getsource(gw)
        marker = '"autonudge_state"'
        assert marker in src, "the broadcast site moved -- this test is measuring nothing"
        head = src.split(marker, 1)[0][-1400:]
        body = src.split(marker, 1)[1][:600]
        assert (
            'scrub_loop_text(loop.message, field="message")' in head
        ), "the message is not scrubbed before the broadcast"
        assert '"message": safe_message' in body, "the payload still ships the raw message"
        assert '"message": loop.message' not in body, "the raw message is still interpolated"

    def test_the_broadcast_carries_the_redaction_flag(self) -> None:
        """GPT 5.6 (BLOCKING): scrubbing without the flag re-opens the overwrite.

        The socket frame REPLACES the REST-fetched loop wholesale --
        ``ChatPage`` does ``setAutoNudgeLoop(detail.loop ?? null)`` and hands that
        object to ``AutoNudgePopover`` as ``loop``. So a payload that scrubs
        ``message`` but omits ``message_redacted`` leaves the flag ``undefined``
        after any frame: the masked-credential notice stops rendering and
        ``editsRedactedGoal`` goes false, disarming the confirm gate. An edit then
        stores the mask over the original instruction with no warning -- the exact
        loss the REST flag exists to prevent.

        The truthiness must match the REST projection, which compares the SERVED
        value against the STORED one rather than asking whether a scrubber ran.
        """
        import inspect

        src = inspect.getsource(gw)
        marker = '"autonudge_state"'
        assert marker in src, "the broadcast site moved -- this test is measuring nothing"
        body = src.split(marker, 1)[1][:800]
        assert (
            '"message_redacted"' in body
        ), "the socket payload omits message_redacted, so a frame disarms the overwrite guard"
        assert (
            '"message_redacted": safe_message != loop.message' in body
        ), "the flag is present but not keyed on served-differs-from-stored like the REST projection"

    @pytest.mark.asyncio
    async def test_a_settings_only_save_does_not_claim_the_goal_was_ignored(self) -> None:
        """Opus 4.8 + First Principles + a maintainer: a routine save warned falsely.

        Driven through the HANDLER, not the helper. The old arm read
        ``svc.update.await_args.kwargs``, so the handler's own construction of the
        response flag was never exercised -- which is why this shipped.

        The popover omits ``message`` on an interval-only save
        (``if (loop && message !== (loop.message ?? '')) patch.message = message``) and
        every stop sends ``{"active": false}`` alone. Deriving the flag from the request
        body then read key-absent as ``None`` and reported a drop, so the client ran
        ``setMessageIgnored(true); return`` and left the popover open on a save that
        fully succeeded.
        """
        stored = NudgeLoop(id="loop-1", slot_key="chat-1-123", message="keep going")
        svc = MagicMock()
        svc.list_all = lambda: [stored]
        svc.get_by_id = lambda _id, _s=stored: _s if _id == _s.id else None
        svc.update = AsyncMock(return_value=stored)

        captured: dict = {}

        class _Resp:
            def __init__(self, body, status=200):
                captured["body"] = body
                captured["status"] = status

        request = SimpleNamespace(
            match_info={"loop_id": "loop-1"},
            remote="127.0.0.1",
            json=AsyncMock(return_value={"idle_secs": 120, "max_cycles": 0, "active": True}),
        )

        with (
            patch.object(autonudge_handlers, "_autonudge_get", lambda: svc),
            patch.object(autonudge_handlers.web, "json_response", _Resp),
        ):
            await autonudge_handlers.api_autonudge_update(request)

        assert captured["status"] == 200, f"the interval-only save was refused: {captured}"
        assert (
            "message_ignored" not in captured["body"]
        ), f"an interval-only save reported the goal as ignored: {captured['body']}"

    @pytest.mark.asyncio
    async def test_a_stop_does_not_claim_the_goal_was_ignored(self) -> None:
        """A maintainer: every stop submits ``{"active": false}`` and no ``message``.

        The sibling arm above covers the interval-only save. This covers the stop, which
        is the other body carrying no ``message`` key -- named separately because it is
        the one path a client takes on EVERY stop, so a false "goal ignored" there would
        surface constantly.

        Driven through the handler, so the response the client actually parses is what is
        asserted. Reading the flag off ``svc.update.await_args`` would pass even if the
        handler built the payload wrongly, which is exactly how the original shipped.
        """
        stored = NudgeLoop(id="loop-1", slot_key="chat-1-123", message="keep going")
        svc = MagicMock()
        svc.list_all = lambda: [stored]
        svc.get_by_id = lambda _id, _s=stored: _s if _id == _s.id else None
        svc.update = AsyncMock(return_value=stored)

        captured: dict = {}

        class _Resp:
            def __init__(self, body, status=200):
                captured["body"] = body
                captured["status"] = status

        request = SimpleNamespace(
            match_info={"loop_id": "loop-1"},
            remote="127.0.0.1",
            json=AsyncMock(return_value={"active": False}),
        )

        with (
            patch.object(autonudge_handlers, "_autonudge_get", lambda: svc),
            patch.object(autonudge_handlers.web, "json_response", _Resp),
        ):
            await autonudge_handlers.api_autonudge_update(request)

        assert captured["status"] == 200, f"the stop was refused: {captured}"
        assert "message_ignored" not in captured["body"], (
            f"a stop reported the goal as ignored: {captured['body']}. The client runs "
            "setMessageIgnored(true) and leaves the popover open on a stop that worked"
        )

    def test_one_denylist_scrubs_banner_and_message_together(self) -> None:
        """First Principles: the `message` scrub is NOT separable from the banner.

        `_serialize` exempts only `ADDRESSING_FIELDS`, so a single loop scrubs every text
        field. Lifting `message` out means either dropping the denylist -- which stops
        scrubbing `banner` -- or re-exempting `message` and restoring the verbatim leak.

        Pinned as a fact rather than left in prose, because a docstring claiming the two
        are one rule cannot fail when someone splits them.
        """
        secret = "AKIA" + "IOSFODNN7EXAMPLE"
        loop = NudgeLoop(
            id="loop-1",
            slot_key="chat-1-1",
            message=f"goal {secret}",
            banner=f"banner {secret}",
        )

        served = autonudge_handlers._serialize(loop)

        assert secret not in str(
            served.get("message")
        ), f"message served verbatim: {served.get('message')!r}"
        assert secret not in str(served.get("banner")), (
            f"banner served verbatim: {served.get('banner')!r} -- the banner relies on the "
            "same denylist as message, so removing one removes both"
        )
        assert autonudge_handlers._UNSCRUBBED_FIELDS == {"id", "slot_key"}, (
            "the exempt set changed: if `message` or `banner` is exempted here, the "
            "shared-denylist claim in _serialize's docstring is no longer true"
        )

    def test_the_rest_projection_still_keys_the_flag_the_same_way(self) -> None:
        """NEGATIVE CONTROL: the two surfaces must not drift apart again.

        Fails if the REST projection stops deriving the flag by comparing the served
        value to the stored one -- which is what would make the socket spelling above
        a different rule wearing the same name.
        """
        import inspect

        rest = inspect.getsource(autonudge_handlers)
        assert (
            'out["message_redacted"] = out.get("message") != getattr(loop, "message", None)' in rest
        ), "the REST projection no longer derives the flag from served-differs-from-stored"


class TestNonStringMessageDoesNotDropTheObserverBroadcast:
    """The observer scrub must survive a non-string ``message``.

    Both redactors raise ``TypeError: expected string or bytes-like object`` on a
    list, dict, int or ``None`` -- measured directly, all four. So any scrub on
    this path that took ``message`` unguarded would raise for a STORED non-string
    on ANY update, even one that only changes ``idle_secs``.

    That failure mode would be DATA LOSS rather than an error the operator sees:
    ``AutoNudgeService._emit`` wraps every observer in ``except Exception`` and
    only logs, so nothing 500s -- the ``autonudge_state`` broadcast would simply
    be dropped and the dashboard would stop seeing that loop's updates. That is
    why the primary arm asserts the broadcast HAPPENED; a test that only exercised
    the coercion helper would pass while the socket stayed mute.

    A non-string ``message`` is reachable: ``_load`` refuses a non-string
    ADDRESSING field but coerces the text ones at the sink, and the dataclass
    annotation is not enforced on ``NudgeLoop(**raw)``, so a hand-edited store
    arms such a loop. The authorizer redacts a message supplied BY a PATCH, which
    is a different value -- it never touches one already on the loop.
    """

    SECRET = "AKIA" + "IOSFODNN7EXAMPLE"

    async def _real_observer(self):
        """The gateway's OWN ``_observer`` closure, not a re-implementation."""
        cfg = KiroCrewConfig()
        with patch.object(cfg, "load_credentials", return_value={"KIROCREW_OWNER_ID": "U_OWNER"}):
            orch = gw.GatewayOrchestrator(cfg, no_dashboard=True, no_crons=True, no_open=True)
        ds = MagicMock()
        ds.broadcast_ws = MagicMock()
        orch.dashboard_state = ds
        with patch("kiro_crew.slack.gateway.autonudge_enabled", return_value=True):
            with patch("kiro_crew.slack.gateway.AutoNudgeService") as mock_svc:
                inst = MagicMock()
                inst.start = AsyncMock()
                inst.subscribe = MagicMock()
                inst.remove = AsyncMock()
                mock_svc.return_value = inst
                await orch._init_autonudge()
        return inst.subscribe.call_args.args[0], ds

    def _store_with(self, tmp_path, message) -> AutoNudgeService:
        """Arm a loop straight from the store, the way a hand edit does."""
        (tmp_path / "autonudge.json").write_text(
            json.dumps(
                {
                    "loops": [
                        {
                            "id": "loop-nonstr",
                            "slot_key": "chat-1-1785",
                            "message": message,
                            "idle_secs": 300,
                            # Inactive on purpose: the emit under test is
                            # unconditional, and an armed loop would schedule real
                            # timers this test has no use for.
                            "active": False,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        svc = AutoNudgeService(base_dir=tmp_path)
        svc._load()
        return svc

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "stored",
        [
            pytest.param(["AKIA" + "IOSFODNN7EXAMPLE"], id="list"),
            pytest.param({"k": "AKIA" + "IOSFODNN7EXAMPLE"}, id="dict"),
            pytest.param(None, id="none"),
            pytest.param(42, id="int"),
        ],
    )
    async def test_update_still_broadcasts_when_the_stored_message_is_not_a_string(
        self, tmp_path, stored
    ) -> None:
        """THE REPORTED PATH: stored non-string -> update -> broadcast survives."""
        observer, ds = await self._real_observer()
        svc = self._store_with(tmp_path, stored)
        assert svc._loops, "the loop did not arm -- this test would measure nothing"
        svc.subscribe(observer)

        # The real update path: _update_locked -> _update_unserialized ->
        # _emit("updated", loop) -> the observer above, through _emit's own
        # try/except. Only idle_secs changes; the non-string message is the value
        # ALREADY on the loop, which is what the finding describes.
        await svc.update("loop-nonstr", idle_secs=600)

        assert ds.broadcast_ws.called, (
            "the autonudge_state broadcast was DROPPED -- _emit swallowed an "
            "exception from the observer scrub"
        )
        topic, payload = ds.broadcast_ws.call_args.args
        assert topic == "autonudge_state"
        assert payload["event"] == "updated"
        assert self.SECRET not in str(payload), "the credential reached the socket payload"

    @pytest.mark.asyncio
    async def test_the_socket_message_matches_what_the_rest_surface_would_send(
        self, tmp_path
    ) -> None:
        """PARITY, the property the fix is built on: one function, two callers.

        Asserted against ``_serialize``'s output rather than a literal, so a future
        change to the rule cannot make the two surfaces disagree without failing
        here.
        """
        observer, ds = await self._real_observer()
        svc = self._store_with(tmp_path, [self.SECRET])
        svc.subscribe(observer)
        await svc.update("loop-nonstr", idle_secs=600)

        _topic, payload = ds.broadcast_ws.call_args.args
        rest = autonudge_handlers._serialize(svc._loops["loop-nonstr"])
        assert payload["loop"]["message"] == rest["message"]
        assert isinstance(payload["loop"]["message"], str)

    @pytest.mark.asyncio
    async def test_declared_scalars_are_not_coerced_on_the_socket_either(self, tmp_path) -> None:
        """NEGATIVE CONTROL: a fix that stringified everything would pass the above.

        Clients compare and do arithmetic on these, so ``600`` must not arrive as
        ``"600"``.
        """
        observer, ds = await self._real_observer()
        svc = self._store_with(tmp_path, [self.SECRET])
        svc.subscribe(observer)
        await svc.update("loop-nonstr", idle_secs=600)

        _topic, payload = ds.broadcast_ws.call_args.args
        assert payload["loop"]["idle_secs"] == 600
        assert isinstance(payload["loop"]["idle_secs"], int)
        assert isinstance(payload["loop"]["active"], bool)

    def test_emit_swallows_an_observer_exception(self) -> None:
        """Pins the mechanism the primary arm relies on.

        If ``_emit`` ever let an observer exception propagate, a raise would become
        a visible 500 rather than a dropped broadcast -- a different defect, and the
        primary arm's failure message would then be wrong about what went wrong.
        """
        svc = AutoNudgeService()
        boom = MagicMock(side_effect=TypeError("expected string or bytes-like object"))
        svc.subscribe(boom)
        svc._emit("updated", _loop())  # must not raise
        boom.assert_called_once()

    def test_both_redactors_reject_a_non_string(self) -> None:
        """Why the coercion branch is load-bearing rather than defensive."""
        from kiro_crew.security import redact_credentials, redact_exfiltration_urls

        for probe in ([self.SECRET], {"k": self.SECRET}, 42, None):
            for fn in (redact_exfiltration_urls, redact_credentials):
                with pytest.raises(TypeError):
                    fn(probe)


class TestNonStringFieldsCannotBypassTheScrub:
    """GPT 5.6 (BLOCKING): ``not isinstance(value, str)`` was an EARLY-OUT.

    ``_serialize``'s loop skipped any non-string value, so an agent-written
    ``message: ["AKIA..."]`` rode straight through to ``GET /api/autonudge`` and
    the ``autonudge_state`` WS broadcast. Measured before the fix: the loop LOADED
    and the serialized payload carried ``message = ['AKIAIOSFODNN7EXAMPLE']``.

    Two halves, because the right answer differs per field:

    * ADDRESSING fields (``id``/``slot_key``) are exempt from scrubbing BY DESIGN,
      so a non-string there rides both the exemption and the early-out -- and a
      list ``id`` is unhashable, so ``self._loops[loop.id] = loop`` raises
      uncaught, escapes ``_load`` and ``start()``, and NO loop arms at all. Those
      are REFUSED at load, like a credential-shaped one.
    * Other ``str``-declared fields are REDACT-COERCED at the sink. Blanking would
      destroy the operator's ability to see what is wrong; coercing scrubs the
      credential and keeps the value inspectable, and the field is declared ``str``
      so a string is what the contract already promises.

    The nine int/float/bool fields must pass through UNTOUCHED -- coercing
    ``idle_secs`` to ``"300"`` would break every client that compares it.
    """

    SECRET = "AKIAIOSFODNN7EXAMPLE"

    def _row(self, **over) -> dict:
        row = {"id": "n1", "slot_key": "chat-1-2", "message": "ok", "idle_secs": 300}
        row.update(over)
        return row

    def _write(self, tmp_path, row) -> None:
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": [row]}), encoding="utf-8"
        )

    @pytest.mark.asyncio
    async def test_a_non_string_message_is_not_served_raw(self, tmp_path) -> None:
        """Fails on the unmodified tree: the payload carries the list verbatim."""
        self._write(tmp_path, self._row(message=[self.SECRET]))
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            assert "n1" in svc._loops, "fixture wrong -- the row did not load"
            payload = json.dumps(autonudge_handlers._serialize(svc._loops["n1"]))
            assert self.SECRET not in payload, "a non-string message leaked the credential"
            assert "[REDACTED: credential]" in payload, "the value was not redact-coerced"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", ["banner", "stopped_reason", "stop_sentinel_path"])
    async def test_every_other_str_field_is_redact_coerced(self, tmp_path, field) -> None:
        """One arm per field: one break cannot validate the whole denylist."""
        self._write(tmp_path, self._row(**{field: [self.SECRET]}))
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            if "n1" not in svc._loops:
                pytest.skip(f"{field} is refused at load, not coerced -- covered elsewhere")
            payload = json.dumps(autonudge_handlers._serialize(svc._loops["n1"]))
            assert self.SECRET not in payload, f"a non-string {field} leaked the credential"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", ["id", "slot_key"])
    async def test_a_non_string_addressing_field_is_refused(self, tmp_path, field) -> None:
        """Half 1. For ``id`` this ALSO fixes an uncaught TypeError that armed no
        loops at all; ``start()`` must complete rather than raise."""
        self._write(tmp_path, self._row(**{field: [self.SECRET]}))
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            assert svc._loops == {}, f"a non-string {field} was armed"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_the_numeric_and_bool_fields_keep_their_types(self, tmp_path) -> None:
        """Negative control, and the one that matters most.

        A fix that coerced every non-string would pass every arm above while
        turning ``idle_secs`` into ``"300"`` and ``active`` into ``"True"``,
        breaking every client that compares them. Pins all nine.
        """
        self._write(tmp_path, self._row())
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
        self._write(tmp_path, self._row(message="just keep going"))
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            out = autonudge_handlers._serialize(svc._loops["n1"])
            assert out["message"] == "just keep going"
            assert out["id"] == "n1" and out["slot_key"] == "chat-1-2"
        finally:
            svc.stop()


class TestBannerIsScrubbedByTheActiveCredentialPolicy:
    """GPT 5.6 (BLOCKING): the banner must route through the platform policy.

    ``normalize_banner`` scrubbed with the bare ``security.redact``. A host that
    loads a companion credential policy -- whose entire purpose is extra,
    host-specific credential patterns -- therefore had those patterns SKIPPED for
    the one field this PR adds. The banner is PERSISTED to the loop store and
    broadcast to every connected browser as a transcript row, so a
    companion-shaped token parked there survived verbatim on both surfaces.

    ``platform.redact_via_context`` is the canonical shim for exactly this: it
    routes through ``current_context().credentials.redact``, and the Default
    policy delegates to ``security.redact``, so a standalone process keeps
    byte-for-byte today's behaviour while a composed host gains its own patterns.
    """

    # Shaped like a host cookie/token, deliberately NOT one of core redaction's
    # known credential shapes -- the control below proves core leaves it alone,
    # which is what makes this test able to fail.
    COMPANION_TOKEN = "COMPANION-SSO-COOKIE-9f3a2b4c7d1e"

    class _CompanionPolicy:
        """Delegates to core redaction and adds ONE host-specific pattern.

        This is the shape a real companion supplies: core coverage is preserved
        (so app-specific redaction is RETAINED, per the lane's own instruction)
        and the host's extra pattern rides on top.
        """

        token = "COMPANION-SSO-COOKIE-9f3a2b4c7d1e"

        def redact(self, text: str) -> str:
            from kiro_crew import security

            return security.redact(text).replace(self.token, "[REDACTED: companion]")

    @staticmethod
    def _install(policy) -> None:
        import dataclasses

        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.platform import build_default_context, set_context

        base = build_default_context(KiroCrewConfig())
        set_context(dataclasses.replace(base, credentials=policy))

    def test_core_redaction_leaves_the_companion_token_alone(self) -> None:
        """Control FIRST: without it the assertions below could pass vacuously.

        If core redaction already stripped this token, routing through the
        companion policy would be unobservable and every assertion here would
        hold for the wrong reason.
        """
        from kiro_crew.security import redact

        assert redact(self.COMPANION_TOKEN) == self.COMPANION_TOKEN, (
            "core redaction now strips this shape, so it can no longer distinguish "
            "the bare call from the platform shim -- pick a different token"
        )

    def test_the_companion_token_is_scrubbed_out_of_a_banner(self) -> None:
        """The lane's path, at its first hop: MCP banner -> normalize_banner."""
        from kiro_crew.autonudge_authz import normalize_banner

        self._install(self._CompanionPolicy())
        value, err = normalize_banner(f"deploying with {self.COMPANION_TOKEN}", absent_ok=True)
        assert err is None, f"a normal banner was refused: {err}"
        assert self.COMPANION_TOKEN not in value, (
            "the companion token survived normalize_banner -- the banner is scrubbed "
            "with the bare redactor, so the active policy's patterns never ran"
        )
        assert "[REDACTED: companion]" in value, "the companion policy did not run at all"

    @pytest.mark.asyncio
    async def test_the_companion_token_reaches_neither_the_store_nor_the_row(
        self, tmp_path
    ) -> None:
        """End to end, both surfaces the lane names: persisted row AND broadcast.

        The banner is normalized at the door, so what lands in the store is what
        every later surface serves. Asserting on BOTH is the point: a token that
        is absent from the REST projection but present on disk is still exposed,
        because the next boot serves it.
        """
        from kiro_crew.autonudge import AutoNudgeService
        from kiro_crew.autonudge_authz import normalize_banner

        self._install(self._CompanionPolicy())
        banner, err = normalize_banner(f"tail {self.COMPANION_TOKEN}", absent_ok=True)
        assert err is None

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            await svc.add(
                slot_key="chat-1-2",
                message="keep going",
                idle_secs=300,
                banner=banner,
            )

            on_disk = (tmp_path / "autonudge.json").read_text(encoding="utf-8")
            assert self.COMPANION_TOKEN not in on_disk, "the token was PERSISTED to the store"

            for loop in svc.list_all():
                out = autonudge_handlers._serialize(loop)
                assert self.COMPANION_TOKEN not in str(out), "the token reached the REST row"
        finally:
            svc.stop()

    def test_a_policy_that_lengthens_past_the_cap_still_gets_the_cap_refusal(self) -> None:
        """The post-redaction cap re-check must SURVIVE the reroute.

        Redaction can GROW a banner, which is why the cap is checked twice. A
        reroute that dropped the second check would look correct on every
        credential test and silently let an at-cap banner through into the store,
        where the loader's own cap arm would later blank it.
        """
        from kiro_crew.autonudge import MAX_BANNER_CHARS
        from kiro_crew.autonudge_authz import normalize_banner

        class _GrowingPolicy:
            def redact(self, text: str) -> str:
                return text + "x" * 64

        self._install(_GrowingPolicy())
        value, err = normalize_banner("a" * MAX_BANNER_CHARS, absent_ok=True)
        assert err is not None, "an over-cap post-redaction banner was accepted"
        assert "once credentials are masked" in err, f"the wrong arm refused it: {err}"
        assert value == ""

    def test_core_credential_shapes_are_still_redacted(self) -> None:
        """App-specific redaction is RETAINED, which the lane asked for explicitly.

        The reroute must not trade core coverage for companion coverage: a
        companion policy delegating to core keeps both.
        """
        from kiro_crew.autonudge_authz import normalize_banner

        self._install(self._CompanionPolicy())
        value, err = normalize_banner("key AKIAIOSFODNN7EXAMPLE here", absent_ok=True)
        assert err is None
        assert "AKIAIOSFODNN7EXAMPLE" not in value, "core credential redaction was lost"


class TestTheBannerStaysOptIn:
    """No shipped producer sets the banner: a caller must ask for it.

    An earlier revision wired ``/goal`` to set the objective as its banner. That was
    withdrawn because it changed an existing command's behaviour without anyone opting
    in, and because two sibling verbose producers would have been left unwired -- half a
    fix reads as a whole one. The field is REST/MCP opt-in only, so "no existing loop
    changes behaviour" is true of every shipped caller.
    """

    def test_no_shipped_producer_sets_a_banner(self) -> None:
        """Pins the withdrawal: re-wiring one producer alone must fail this."""
        import inspect

        from kiro_crew.dashboard import chat_runner

        src = inspect.getsource(chat_runner)
        assert "banner=" not in src, (
            "a producer now sets a banner without opting in; either withdraw it or wire "
            "the sibling verbose producers too so the change is whole"
        )

    def test_the_banner_is_bounded_at_the_call_site(self) -> None:
        """``add`` does not validate, so an unbounded objective would be cleared on load."""
        import inspect

        from kiro_crew.autonudge import AutoNudgeService

        assert "banner" in inspect.signature(AutoNudgeService.add).parameters
        src = inspect.getsource(AutoNudgeService.add)
        assert (
            "normalize_banner" not in src
        ), "add now validates the banner, so the call-site bound may be redundant"


class TestAStagedMonitorWriteKeepsQuarantine:
    """A staged monitor snapshot is a WHOLESALE write, so it must carry quarantine.

    Two builders reach ``_write_state``. ``_serialize_state`` re-emits ``quarantined``;
    the monitor snapshot builder did not, so a staged transition on a HEALTHY loop
    deleted the row an operator was told to repair.
    """

    SECRET = "AKIAIOSFODNN7EXAMPLE"

    @pytest.mark.asyncio
    async def test_a_staged_monitor_snapshot_still_carries_quarantined(self, tmp_path) -> None:
        """A monitor payload keeps quarantined rows the transition never touched."""
        good = {"id": "keep", "slot_key": "chat-1-1", "message": "fine", "idle_secs": 300}
        unusable = {"id": self.SECRET, "slot_key": "chat-9-9", "message": "x", "idle_secs": 300}
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": [good, unusable]}),
            encoding="utf-8",
        )
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert svc._quarantined, "fixture did not quarantine the credential-shaped row"
            loop = svc._loops["keep"]
            svc._write_state(svc._monitor_snapshot_with_replacement(loop, loop))
            assert [r.get("id") for r in _held_aside_rows(tmp_path)] == [
                self.SECRET
            ], "a staged monitor write dropped the held-aside row from the sidecar"
        finally:
            svc.stop()


class TestADowngradeCannotDeleteQuarantinedRows:
    """A build predating ``quarantined`` must not be able to destroy a held-aside row.

    Such a build writes ``autonudge.json`` WHOLESALE and knows nothing of the key, so
    an embedded-only copy is deleted by its next write. The sidecar is what survives.
    """

    SECRET = "AKIAIOSFODNN7EXAMPLE"

    @pytest.mark.asyncio
    async def test_a_quarantined_row_survives_an_older_builds_wholesale_write(
        self, tmp_path
    ) -> None:
        """A row held aside is recoverable after a downgrade drops the embedded copy."""
        good = {"id": "keep", "slot_key": "chat-1-1", "message": "fine", "idle_secs": 300}
        unusable = {"id": self.SECRET, "slot_key": "chat-9-9", "message": "x", "idle_secs": 300}
        store = tmp_path / "autonudge.json"
        store.write_text(json.dumps({"version": 1, "loops": [good, unusable]}), encoding="utf-8")

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert svc._quarantined, "fixture did not quarantine the credential-shaped row"
            # Persist once so the sidecar exists, exactly as a live write would.
            svc._write_state(svc._serialize_state())
            sidecar = tmp_path / "autonudge.quarantine.json"
            assert sidecar.exists(), "the write sink did not persist the quarantine sidecar"

            # THE DOWNGRADE: an older build re-emits the store with no `quarantined`
            # key and never touches the sidecar.
            store.write_text(json.dumps({"version": 1, "loops": [good]}), encoding="utf-8")
            assert "quarantined" not in json.loads(store.read_text(encoding="utf-8"))
        finally:
            svc.stop()

        after = AutoNudgeService(base_dir=tmp_path)
        try:
            after._load()
            assert [r.get("id") for r in after._quarantined] == [self.SECRET], (
                "a downgrade permanently deleted the quarantined row; "
                f"recovered={[r.get('id') for r in after._quarantined]!r}"
            )
        finally:
            after.stop()


class TestTheQuarantineSidecarIsWrittenSafely:
    """The sidecar must not be able to corrupt or crash the store it protects.

    Ordering: it is written BEFORE the main store, so a sidecar failure leaves the old
    consistent file. Shape: a non-object sidecar is ignored rather than fatal.
    """

    SECRET = "AKIAIOSFODNN7EXAMPLE"

    def _store_with_one_unusable_row(self, tmp_path):
        good = {"id": "keep", "slot_key": "chat-1-1", "message": "fine", "idle_secs": 300}
        unusable = {"id": self.SECRET, "slot_key": "chat-9-9", "message": "x", "idle_secs": 300}
        store = tmp_path / "autonudge.json"
        store.write_text(json.dumps({"version": 1, "loops": [good, unusable]}), encoding="utf-8")
        return store

    @pytest.mark.asyncio
    async def test_a_row_in_both_files_is_quarantined_once_not_twice(self, tmp_path) -> None:
        """GPT 5.6 (BLOCKING): a failed replacement made the next load DUPLICATE a row.

        Once the sidecar write has landed and the main-store replacement then fails, the
        unsafe row is in BOTH files. The load loop reaches it twice and appended it twice,
        so each failed replacement accumulated another copy of the same quarantine record.
        """
        unusable = {
            "id": self.SECRET,
            "slot_key": "chat-9-9",
            "message": "x",
            "idle_secs": 300,
        }
        (tmp_path / "autonudge.quarantine.json").write_text(
            json.dumps({"quarantined": [unusable]}), encoding="utf-8"
        )
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": [unusable]}), encoding="utf-8"
        )

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            held = [row.get("id") for row in svc._quarantined]
            assert len(held) == 1, (
                "the row present in BOTH files was quarantined twice, so every failed "
                f"replacement accumulates another duplicate record: {len(held)} copies"
            )
            assert not svc._loops, "the unsafe row armed instead of being held aside"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_failed_sidecar_write_leaves_the_store_file_untouched(self, tmp_path) -> None:
        """ORDERING: the main store is not replaced when the sidecar cannot be written."""
        store = self._store_with_one_unusable_row(tmp_path)
        before = store.read_text(encoding="utf-8")

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert svc._quarantined, "fixture did not quarantine the credential-shaped row"

            def _boom() -> None:
                raise OSError("sidecar volume is full")

            svc._write_quarantine_sidecar = _boom  # type: ignore[method-assign]
            with pytest.raises(OSError):
                svc._write_state(svc._serialize_state())
            assert store.read_text(encoding="utf-8") == before, (
                "the store was replaced before the sidecar was durable, so a sidecar "
                "failure left committed disk state inconsistent"
            )
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_non_object_sidecar_refuses_the_store_rather_than_arming(
        self, tmp_path
    ) -> None:
        """GPT 5.6 (BLOCKING): tolerating a bad sidecar armed loops that could not persist.

        This test previously asserted the OPPOSITE -- that a list-shaped sidecar was
        "ignored rather than fatal" and the store's own quarantined row survived in
        memory. That tolerance was the defect: writes were already refused, so a loop
        armed under it delivers a cycle it cannot record, and the next restart re-fires
        that cycle past its own cap.

        So the contract is now REFUSE, and the assertions below cover both halves of it:
        nothing arms, and the file survives for the operator to repair. `_load` must
        still not RAISE -- a startup crash would be a third failure mode.
        """
        self._store_with_one_unusable_row(tmp_path)
        sidecar = tmp_path / "autonudge.quarantine.json"
        # A list, not an object -- `raw.get` on this would raise AttributeError.
        sidecar.write_text("[]", encoding="utf-8")

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()  # must not raise
            assert svc._load_refused is True, "a non-object sidecar left writes enabled"
            assert not svc._loops, (
                "loops armed under a refused store; a delivered cycle cannot record "
                f"itself, so a restart repeats it. armed={sorted(svc._loops)!r}"
            )
            assert not svc._quarantined, (
                "rows were held in memory under a refused store, which cannot be "
                "persisted and so is lost silently on restart"
            )
            with pytest.raises(_an.AutoNudgeStoreUnvetted):
                svc._write_state(svc._serialize_state())
            assert (
                _moved_aside_sidecar(tmp_path).read_text(encoding="utf-8") == "[]"
            ), "the sidecar bytes were lost, so the operator has nothing to repair"
        finally:
            svc.stop()

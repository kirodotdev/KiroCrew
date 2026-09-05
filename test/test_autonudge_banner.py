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

import asyncio
import json
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import kiro_crew.mcp_core as mcp_core
from kiro_crew import autonudge_authz as authz
from kiro_crew import session_directive
from kiro_crew.autonudge import AutoNudgeService, NudgeLoop
from kiro_crew.autonudge_authz import MAX_BANNER_CHARS
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.mcp_core import _call_tool_inner
from kiro_crew.slack import gateway as gw
from kiro_crew.validation import ValidationError

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

    spawned: list[asyncio.Task] = []

    def _spawn(_state, _slot, coro, **_kwargs):
        # #5184 dispatches ``_run_chat`` inside a coroutine handed to
        # ``spawn_guarded_turn``; RUN it (mirroring the dashboard-fire tests)
        # so the prompt reaches the patched ``_run_chat`` rather than closing
        # the coroutine unrun.
        task = asyncio.create_task(coro)
        spawned.append(task)
        return task

    with (
        patch.object(gw, "spawn_guarded_turn", _spawn),
        patch.object(gw, "compose_nudge_body", new=AsyncMock(side_effect=_compose)),
        patch("kiro_crew.dashboard.chat._run_chat", new=run_chat),
    ):
        assert await orch._fire_dashboard_nudge(loop) is True
        await asyncio.gather(*spawned)

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
    async def test_a_credential_shaped_sentinel_is_scrubbed_in_the_row_not_the_prompt(self) -> None:
        """``{{STOP_FILE}}`` substitutes the sentinel AFTER the banner's write-path scrub.

        Without a sink pass over the composed row, that path reaches the persisted and
        broadcast transcript raw. The prompt must keep it: the model has to be able to
        write the real file.
        """
        secret = "AKIAIOSFODNN7EXAMPLE"
        loop = _loop(
            message="run until done: {{STOP_FILE}}",
            banner="halt: {{STOP_FILE}}",
            stop_sentinel_path=f"/tmp/.stop-{secret}",
        )
        row, prompt = await _fire(loop)
        assert secret not in row, f"credential-shaped sentinel reached the persisted row: {row!r}"
        assert secret in prompt, "the model lost the real sentinel path it must write"

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
    async def test_an_oversized_persisted_banner_is_blanked_at_load(self, tmp_path) -> None:
        """A persisted banner over the cap is treated as ABSENT at load — a value
        the authorized write path would have rejected is not invented back by
        truncation, and the row falls back to the full message.

        Fails on the unmodified tree, where the oversized banner survives the
        load intact because no length bound exists on this path at all.
        """
        oversized = "b" * (MAX_BANNER_CHARS + 50)
        self._write_store(tmp_path, oversized)
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            assert svc._loops["abc123"].banner == "", "the oversized banner was not blanked"
            # Persisted, so a boot does not re-read and re-reject the same value.
            on_disk = json.loads((tmp_path / "autonudge.json").read_text(encoding="utf-8"))
            assert on_disk["loops"][0]["banner"] == ""
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_valid_persisted_credential_banner_is_scrubbed_at_load(self, tmp_path) -> None:
        """A banner reaching the store past the authorized write paths (hand-edited
        file, direct agent ``svc.add``, or persisted before this scrub existed) is
        credential-scrubbed at load and re-persisted, so ``GET /api/autonudge``
        (``asdict``), the WS broadcast, and the fire path can never serve it raw."""
        self._write_store(tmp_path, "deploy with AKIAIOSFODNN7EXAMPLE now")
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            stored = svc._loops["abc123"].banner
            assert "AKIAIOSFODNN7EXAMPLE" not in stored, "the credential survived the load scrub"
            assert "[REDACTED" in stored, "the load-path scrub did not run"
            on_disk = json.loads((tmp_path / "autonudge.json").read_text(encoding="utf-8"))
            assert "AKIAIOSFODNN7EXAMPLE" not in on_disk["loops"][0]["banner"]
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_persisted_credential_banner_over_the_cap_is_blanked(self, tmp_path) -> None:
        """A persisted credential-shaped banner LONGER than the cap is blanked at
        load. Redaction still runs on the FULL value first (so an UNDER-cap
        credential is masked and kept — the test above), but an over-cap value is
        treated as absent, so no raw prefix of a straddling secret can survive a
        slice — there is no slice."""
        secret = "AKIAIOSFODNN7EXAMPLE"
        straddling = "x" * (MAX_BANNER_CHARS - 10) + secret + " trailing context " * 5
        assert len(straddling) > MAX_BANNER_CHARS, "fixture must exceed the cap"
        self._write_store(tmp_path, straddling)
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            assert svc._loops["abc123"].banner == "", "the over-cap banner was not blanked"
            on_disk = json.loads((tmp_path / "autonudge.json").read_text(encoding="utf-8"))
            assert "AKIA" not in on_disk["loops"][0]["banner"]
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_an_over_cap_banner_that_redaction_shrinks_is_still_blanked(
        self, tmp_path
    ) -> None:
        """GPT finding: blanking must key on the ORIGINAL length too. An exfil URL
        that redaction replaces with a shorter placeholder can drop a >cap banner
        below the cap; keying only on the scrubbed length would KEEP a value the
        write path would have rejected. The over-cap original is blanked
        regardless of how far masking shrinks it."""
        oversized_url = "https://evil.example.com/steal?d=" + "Z" * (MAX_BANNER_CHARS + 100)
        assert len(oversized_url) > MAX_BANNER_CHARS, "fixture must exceed the cap"
        self._write_store(tmp_path, oversized_url)
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            assert (
                svc._loops["abc123"].banner == ""
            ), "an over-cap banner survived because masking shrank it below the cap"
            on_disk = json.loads((tmp_path / "autonudge.json").read_text(encoding="utf-8"))
            assert on_disk["loops"][0]["banner"] == ""
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


class TestLoadScrubsThePersistedMessage:
    """``_load`` credential-scrubs the sibling ``message`` field, not just ``banner``.

    The load-scrub's rationale — the store is writable out-of-band (hand-edited
    ``autonudge.json``, direct ``svc.add``) and served RAW by ``GET /api/autonudge``
    — applies identically to ``message``, which the authorized write paths already
    scrub. Unlike the banner this is redaction ONLY: ``message`` is the payload the
    model receives and has no fallback row, so it is never blanked on length.
    """

    def _write_store(self, tmp_path, message) -> None:
        (tmp_path / "autonudge.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "loops": [
                        {
                            "id": "abc123",
                            "slot_key": "chat-9-1",
                            "message": message,
                            "idle_secs": 300,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    @pytest.mark.asyncio
    async def test_a_persisted_credential_message_is_scrubbed_at_load(self, tmp_path) -> None:
        """A message reaching the store past the authorized write paths is
        credential-scrubbed at load and re-persisted, so ``GET /api/autonudge``
        (``asdict``) and the WS broadcast can never serve it raw."""
        self._write_store(tmp_path, "run the deploy with AKIAIOSFODNN7EXAMPLE now")
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            stored = svc._loops["abc123"].message
            assert "AKIAIOSFODNN7EXAMPLE" not in stored, "the credential survived the load scrub"
            assert "[REDACTED" in stored, "the message load-path scrub did not run"
            on_disk = json.loads((tmp_path / "autonudge.json").read_text(encoding="utf-8"))
            assert "AKIAIOSFODNN7EXAMPLE" not in on_disk["loops"][0]["message"]
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_clean_message_is_left_untouched(self, tmp_path) -> None:
        """Negative control: a message with nothing to redact is unchanged and the
        store is not marked dirty by the scrub."""
        self._write_store(tmp_path, "the full multi-paragraph babysit instruction")
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            assert svc._loops["abc123"].message == "the full multi-paragraph babysit instruction"
        finally:
            svc.stop()


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


class TestRestSurface:

    def _app(self, monkeypatch, fake_svc):
        from aiohttp import web

        from kiro_crew.dashboard.handlers import autonudge as _handler

        monkeypatch.setattr(_handler, "_autonudge_get", lambda: fake_svc)
        state = MagicMock()
        state._slots = {
            "chat-1-123": MagicMock(workspace="default", memory_mode="persistent", mode="chat")
        }
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
        """A bound dashboard session, so the monitor tools emit a directive
        rather than refusing. Mirrors the fixture in
        ``test_autonudge_stop_auth.py``, which is where these tools' contract
        tests live: ``monitor_update`` now requires a strict session binding, so
        an empty key is refused before the banner patch is built."""
        monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "dashboard:chat-1-1")
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
            "gate": True,
        }, "the no-banner payload shape changed (banner must stay absent; gate is the tool default)"

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
        slot = MagicMock(workspace="default", memory_mode="persistent", mode="chat")
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


class TestTheNudgeMetaCarriesNoAbridgedMarker:
    """The nudge meta is byte-identical on a bannered and an unbannered row.

    An earlier revision tagged bannered rows with an ``abridged`` boolean intended
    for a dashboard badge, but no consumer was ever built (``NudgeCard`` reads only
    ``cycle``/``loop_id``/``body``), so the flag was broadcast-and-persisted schema
    surface with zero readers and has been removed. The banner content in the row's
    ``content`` already distinguishes it; the meta carries exactly ``cycle`` and
    ``loop_id`` whether or not a banner shows.
    """

    @pytest.mark.asyncio
    async def test_a_banner_row_meta_is_exactly_cycle_and_loop_id(self) -> None:
        _row, _prompt, meta = await _fire_full(_loop(banner="watching PR #123 for CI"))
        assert "abridged" not in meta, "the removed abridged marker is back"
        assert set(meta) == {"cycle", "loop_id"}

    @pytest.mark.asyncio
    async def test_a_bannered_and_plain_row_have_identical_meta_keys(self) -> None:
        """Item-2 contract: a bannered row's meta stays byte-identical to a plain
        row's — the feature adds no meta key at all."""
        _r1, _p1, bannered = await _fire_full(_loop(banner="watching PR #123 for CI"))
        _r2, _p2, plain = await _fire_full(_loop(banner=""))
        assert "abridged" not in bannered and "abridged" not in plain
        assert set(bannered) == set(plain) == {"cycle", "loop_id"}

    @pytest.mark.asyncio
    async def test_the_banner_text_is_never_in_meta(self) -> None:
        """Load-bearing: the row's ``content`` holds the banner; copying the TEXT
        into meta would be the double-broadcast this feature must not become."""
        banner = "watching PR #123 for CI"
        row, _prompt, meta = await _fire_full(_loop(banner=banner))
        assert banner in row
        assert banner not in str(meta)

    @pytest.mark.asyncio
    async def test_the_cycle_and_loop_id_still_reach_the_reader(self) -> None:
        """Negative control: the meta block still carries what NudgeCard reads."""
        loop = _loop(banner="quiet", cycle_count=7)
        _row, _prompt, meta = await _fire_full(loop)
        assert meta["cycle"] == 8
        assert meta["loop_id"] == loop.id


class TestARealProducerSetsTheBanner:
    """A field no shipped producer sets cannot reduce the bloat it exists to reduce.

    ``/goal`` arms a loop whose ``message`` is a multi-paragraph instruction while the
    human-authored objective sits right there at the call site. Wiring it is what makes
    the collapsed row show the objective instead of the whole prompt.
    """

    def test_the_goal_command_passes_a_banner_to_add(self) -> None:
        import inspect

        from kiro_crew.dashboard import chat_runner

        src = inspect.getsource(chat_runner)
        assert (
            "normalize_banner(_objective, absent_ok=True, truncate=True)" in src
        ), "/goal no longer routes the objective through normalize_banner(truncate=True)"

    def test_the_banner_is_bounded_at_the_call_site(self) -> None:
        """``add`` does not validate, so an unbounded objective would be cleared on load."""
        import inspect

        from kiro_crew.autonudge import AutoNudgeService

        assert "banner" in inspect.signature(AutoNudgeService.add).parameters
        src = inspect.getsource(AutoNudgeService.add)
        assert (
            "normalize_banner" not in src
        ), "add now validates the banner, so the call-site bound may be redundant"


class TestTheServiceExposesGetByIdForTheUpdatePath:
    """The update-path channel refusal resolves the loop through ``svc.get_by_id``.

    A test fake that DEFINED ``get_by_id`` hid that the real ``AutoNudgeService``
    lacked it, so the refusal shipped dead: ``hasattr(svc, "get_by_id")`` was
    False on the instance ``get_instance()`` returns, the guard was skipped, and a
    banner on a channel-bound loop was accepted on PATCH. This pins the accessor on
    the REAL service surface, so the guard can never regress to unreachable again.
    """

    @pytest.mark.asyncio
    async def test_the_real_service_resolves_a_loop_by_id(self, tmp_path) -> None:
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            loop = await svc.add("chat-7-1", "go", idle_secs=300)
            assert svc.get_by_id(loop.id) is loop, "the real service cannot resolve a loop by id"
            assert svc.get_by_id("no-such-id") is None
        finally:
            svc.stop()


class TestBanneredRowShowsTheBannerEveryCycle:
    """With a banner set, the transcript row is the short stand-in on EVERY cycle.

    Credential redaction is NOT done at this fire sink — it lives at the write
    paths (``normalize_banner`` on every authorized producer: the REST add/update
    authorizers, the MCP tools, and ``/goal``) and at ``_load`` for a banner that
    reached the store another way, so ``loop.banner`` is already scrubbed by the
    time it renders here.
    """

    @pytest.mark.asyncio
    async def test_first_cycle_shows_the_banner_like_every_cycle(self) -> None:
        """Spec: with a banner set, the row carries the short stand-in on EVERY
        cycle including the first (``cycle_count == 0``) — no first-cycle
        exception, matching ``injected-messages.md``."""
        loop = _loop(banner="watching CI", cycle_count=0)
        row, prompt = await _fire(loop)
        assert "watching CI" in row, "the first cycle did not show the banner"
        assert loop.message not in row, "the first cycle carried the full message"
        assert loop.message in prompt

"""Tests for Response Verbosity (``default`` / ``concise`` / ``ultra`` / ``answer_only``).

Lives under ``test/`` (the collected root per setup.cfg ``testpaths``) so these
run in CI. Covers three layers: the ``{{VERBOSITY_BLOCK}}`` prompt-template
resolution, the dashboard-config PUT/GET validation, and a guard that the
shipped main prompt actually carries the placeholder (so concise mode can never
be silently disabled by a dropped token).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

import kiro_crew
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.context import ContextBuilder


def _resolve(prompt: str, session_key: str, *, verbosity: str = "default") -> str:
    fake_cfg = SimpleNamespace(
        dashboard=SimpleNamespace(widget_density="more", verbosity=verbosity)
    )
    with patch("kiro_crew.context.KiroCrewConfig.load", return_value=fake_cfg):
        return ContextBuilder._resolve_prompt_templates(prompt, session_key)


class TestVerbosityBlockPlaceholder:
    """``{{VERBOSITY_BLOCK}}`` expands on ALL transports when concise; empty on default."""

    def test_default_strips_placeholder_everywhere(self):
        prompt = "prefix {{VERBOSITY_BLOCK}} suffix"
        for key in ("dashboard:abc", "slack:C1:1.2", "cli:local", ""):
            result = _resolve(prompt, key, verbosity="default")
            assert "{{VERBOSITY_BLOCK}}" not in result
            assert "Concise mode is on" not in result

    def test_concise_emits_block_on_every_transport(self):
        for key in ("dashboard:abc", "slack:C1:1.2", "cli:local", ""):
            result = _resolve("{{VERBOSITY_BLOCK}}", key, verbosity="concise")
            assert "## Response Verbosity: Concise" in result
            assert "Lead with the answer" in result

    def test_concise_keeps_safety_carveout(self):
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="concise")
        assert "security warnings" in result
        assert "irreversible" in result
        assert "multi-step" in result

    def test_concise_bounds_the_stakes_carveout_to_omission_not_length(self):
        """The old carve-out ("Ignore concise mode and keep full detail for:
        ...") switched the mode OFF at high stakes — an unbounded length
        licence in the one place the reader most needs the call surfaced, not
        buried. Recast on the same single axis answer_only uses: the warning
        always appears but is one line (call, risk, undoability); an
        order-sensitive multi-step procedure keeps its full length because a
        dropped step IS an omission, and payload was already exempt as
        correctness, not stakes.
        """
        result = " ".join(
            _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="concise").split()
        )
        # The unbounded length licence is gone.
        assert "Ignore concise mode" not in result
        assert "keep full detail" not in result
        # The bounded, omission-focused form is in: the warning must APPEAR,
        # and it is one line.
        assert "Stakes change what concise mode must not omit" in result
        assert "always appear, each as one line naming the call, the risk" in result
        assert "whether it can be undone" in result
        assert "the mechanism and the failure modes are not required" in result

    def test_missing_verbosity_attr_defaults_to_empty(self):
        fake_cfg = SimpleNamespace(dashboard=SimpleNamespace(widget_density="more"))
        with patch("kiro_crew.context.KiroCrewConfig.load", return_value=fake_cfg):
            result = ContextBuilder._resolve_prompt_templates(
                "a {{VERBOSITY_BLOCK}} b", "dashboard:x"
            )
        assert result == "a  b"


class TestUltraConciseBlock:
    """``ultra`` is a distinct, stricter level — not an alias of ``concise``."""

    def test_ultra_emits_its_own_block_on_every_transport(self):
        for key in ("dashboard:abc", "slack:C1:1.2", "cli:local", ""):
            result = _resolve("{{VERBOSITY_BLOCK}}", key, verbosity="ultra")
            assert "## Response Verbosity: Ultra-Brief (ADHD reader)" in result
            assert "simulate the reader" in result
            # The concise block must NOT leak in — the branches are exclusive.
            assert "Concise mode is on" not in result

    def test_ultra_constrains_the_whole_response_not_just_the_opening(self):
        """Regression: the ORIGINAL ultra prompt capped only the opening, then
        said "supporting detail is welcome" and "length after it is fine" —
        which the model read as a licence to expand. Measured output averaged
        1,407 chars, LONGER than default and 76% longer than concise, defeating
        the whole point of the mode. The rewrite removes that licence: the
        suppression must apply to the entire reply, not a lede budget.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="ultra")
        assert "Open with THE answer in 1–2 sentences" in result
        # The expansion licences that caused the bug must be GONE.
        assert "supporting detail is welcome" not in result
        assert "governs the OPENING, not the whole response" not in result
        assert "Length after it is fine" not in result

    def test_ultra_overrides_the_completionist_bias(self):
        """The mechanism that actually shortens output: naming and opposing the
        model's own drive toward completeness, so it stops volunteering detail.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="ultra")
        assert "strong bias toward completeness. Override it" in result
        assert "80% complete in 2 lines beats 100% complete in 20 lines" in result

    def test_ultra_models_the_reader_who_stops_reading(self):
        """Ultra is written for a reader who will not scroll — the prompt must
        say so explicitly, because that framing is what drives prioritization.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="ultra")
        assert "first 2 sentences" in result
        assert "close the tab" in result
        assert "wasted tokens" in result

    def test_ultra_bans_the_structures_that_inflate_output(self):
        """Regression: the original prompt ENCOURAGED tables and structure as
        "signposts", which added tokens instead of removing them. Structure is
        now a banned expansion vector, not an endorsed navigation aid.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="ultra")
        assert "Do NOT add: tables, headers" in result
        assert "would the reader be stuck without this line?" in result
        # The old "structure is not padding" endorsement must be gone.
        assert "it is not padding" not in result

    def test_ultra_caps_supporting_bullets(self):
        """Detail is permitted only when its absence blocks the reader, and is
        bounded — an unbounded bullet list is how the old prompt leaked length.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="ultra")
        assert "only if the reader would be STUCK without them" in result
        assert "Max 3" in result

    def test_ultra_takes_a_position(self):
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="ultra")
        assert "Take a position. Name your pick" in result
        assert 'Resolve "it depends" immediately' in result

    def test_ultra_marks_the_critical_point_for_scanners(self):
        """The reader scans for emphasis before reading — exactly one anchor."""
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="ultra")
        assert "Bold the single most critical point" in result

    def test_ultra_never_cuts_a_required_output_format(self):
        """Regression guard: the brevity rules must not eat a surface-required
        element (an options line, a diff block, a PR URL), which renders the
        response broken rather than terse.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="ultra")
        assert "Required output formats are sacred and never cut" in result
        assert "[OPTIONS:] lines" in result
        assert "diff blocks for file changes" in result
        assert "full PR/MR URLs" in result

    def test_ultra_exempts_explicitly_requested_long_output(self):
        """Brevity constrains UNSOLICITED verbosity — never requested depth."""
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="ultra")
        assert "When the user ASKS for something long" in result
        assert "deliver what was asked" in result

    def test_ultra_is_stricter_than_concise(self):
        ultra = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="ultra")
        concise = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="concise")
        assert ultra != concise
        # concise explicitly ALLOWS a brief progress note; ultra does not.
        assert "Keep progress signal brief, not absent" in concise
        assert "Keep progress signal brief, not absent" not in ultra
        # ultra carries the anti-completionist override; concise does not.
        assert "Override it" in ultra
        assert "Override it" not in concise

    def test_ultra_keeps_safety_carveout(self):
        """The brevity floor: a terse reply must never OMIT a security
        warning, a destructive-action confirmation, or a step in an ordered
        procedure — those failures cause mistakes, not just terseness.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="ultra")
        assert "security warnings" in result
        assert "irreversible" in result
        assert "multi-step" in result
        # Correctness carve-out: code/errors are never compressed.
        assert "verbatim" in result

    def test_ultra_bounds_the_stakes_carveout_to_omission_not_length(self):
        """The old carve-out ("Never compress for brevity: security warnings,
        ...") was an unbounded length licence: it authorised the model to stay
        verbose exactly at high stakes, the one place ultra's whole framing
        (the reader closes the tab) makes a wall of text most costly. Recast on
        the same single axis answer_only uses — stakes govern what may not be
        OMITTED, never how long the reply is — the warning is mandatory but
        one line; an ordered procedure keeps its full length because a dropped
        step IS an omission, and payload (code, commands, errors) was already
        exempt as correctness, not stakes.
        """
        result = " ".join(_resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="ultra").split())
        # The unbounded length licence is gone — including its echo in the
        # required-formats bullet, which listed security warnings as a
        # never-cut format ("regardless of brevity").
        assert "Never compress for brevity" not in result
        assert "URLs, security warnings" not in result
        # The bounded, omission-focused form is in: the warning must APPEAR,
        # and it is one line.
        assert "Stakes change what you must not omit, never the length" in result
        assert "always appear, each as one line naming the call, the risk" in result
        assert "whether it can be undone" in result
        assert "the mechanism and the failure modes are not required" in result

    def test_unknown_level_falls_back_to_empty(self):
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="bogus")
        assert result == ""


class TestAnswerOnlyBlock:
    """``answer_only`` is the strictest level: the answer, and no prose around it.

    The block is a short checklist, not an essay. It ran as 1,300 words of
    rules and the model copied the register of its instructions -- long,
    dense, text-only -- rather than the rule they stated. Three checks with a
    hard test each replaced it: draw the shape, cap the words, cut the rest.
    The measurements behind it live in the PR that made the change.
    """

    def _block(self) -> str:
        return _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")

    def test_answer_only_emits_its_own_block_on_every_transport(self):
        for key in ("dashboard:abc", "slack:C1:1.2", "cli:local", ""):
            result = _resolve("{{VERBOSITY_BLOCK}}", key, verbosity="answer_only")
            assert "## Response Verbosity: Answer Only" in result
            # The other levels must NOT leak in -- the branches are exclusive.
            assert "Concise mode is on" not in result
            assert "Ultra-Brief" not in result

    def test_the_block_is_a_short_checklist_not_an_essay(self):
        """The model mirrors the register of its instructions. A brevity rule
        delivered as 1,300 words of prose produced 1,300-word-register replies;
        the fix is structural, so the length of the block itself is pinned.
        """
        words = len(self._block().split())
        assert words < 300, f"answer_only block grew to {words} words"

    def test_three_checks_in_a_fixed_order(self):
        """Order is load-bearing: the shape check must run before any prose is
        drafted, or the model writes the paragraph and then asks whether a
        picture would have been shorter.
        """
        block = self._block()
        assert "three checks, in this order, before you write" in block
        assert block.index("1. Shape check") < block.index("2. Word check")
        assert block.index("2. Word check") < block.index("3. Cut check")

    def test_shape_check_draws_the_shape_in_the_form_the_surface_renders(self):
        """The old rule lived in the fourteenth paragraph as "prefer" and was
        never reached. Now it is the first check and imperative. The form is
        gated on the surface: a widget or mermaid only where the Inline Widgets
        section is present (the dashboard renders both), else a plain table --
        an unconditional "emit a widget" would land raw ``<mcwidget>`` markup in
        a Slack or CLI reply, and a mermaid fence reaches Discord as raw source.
        """
        block = self._block()
        assert "Does the answer have a shape" in block
        assert "steps, before/after, cases and verdicts, sizes" in block
        assert (
            "a widget or a mermaid fence when your instructions carry an Inline "
            "Widgets section, else a plain table" in block
        )
        # The Inline Widgets section already says to load the `widgets` skill;
        # repeating it here would be a second spelling of the same instruction.
        assert "load `widgets`" not in block

    def test_a_picture_holds_labels_not_sentences(self):
        """A widget that is a table of prose is text in a box -- the reported
        failure once pictures did appear. The check caps what goes inside.
        """
        block = self._block()
        assert "labels of one to three words and numbers, never a sentence" in block
        assert "it goes under the picture, once" in block

    def test_word_check_caps_sentence_length_and_vocabulary(self):
        """Age 5 as a named register was read as style advice and ignored;
        "a technical term stays when it IS the fact" was read as a licence for
        every term the model thought precise. Two mechanical tests replace it.
        """
        block = self._block()
        assert "Each sentence: at most 12 words" in block
        assert "one the user has used, or one a child knows" in block
        assert "replaced, or defined in three words" in block

    def test_cut_check_names_what_goes_and_what_stays(self):
        """Enumerated bans, not a vague "be brief" -- each named category is a
        distinct way explanation creeps back in. The keep-list is the payload
        floor: this mode cuts prose, never code, commands or required formats.
        """
        block = self._block()
        assert (
            "Delete: preamble, what you did, where you found it, why, options you "
            "rejected, caveats, offers to help" in block
        )
        # "verbatim" is scoped to what the user asked for or must run; an
        # unscoped verbatim licence let a log-check reply paste every line read.
        assert "code, commands and paths the user asked for or must run, verbatim" in block
        # "any" keeps the list open: the parenthetical is examples, not the set.
        assert "any required format ([OPTIONS:], diffs, PR links)" in block

    def test_an_ordered_procedure_stays_complete(self):
        """A dropped step causes the mistake, so steps are payload, not prose.
        The shape check may draw steps as a picture; this keeps every step in
        it, in order, so the picture cannot shorten a procedure by omission.
        The Settings help text promises this for every level.
        """
        assert "every step of an ordered procedure, in order" in self._block()

    def test_a_destructive_command_carries_its_undo_line(self):
        """A bare destructive one-liner is a trap, not a terse answer. The undo
        note is bounded to one line so it cannot reopen explanation.
        """
        assert "one undo line for anything destructive" in self._block()

    def test_high_stakes_gets_one_risk_line(self):
        """Stakes change what must not be omitted, never the length: one line
        naming the risk, on the domains where a wrong call is hard to undo.
        """
        assert "one risk line for anything touching security, data or spend" in self._block()

    def test_asking_why_keeps_the_checks_and_adds_one_line_per_point(self):
        """A request for the reason is not a request for a document: the reason
        turns on, the three checks stay on.
        """
        block = self._block()
        assert "Asked why? Same three checks, plus the reason as one line per point" in block

    def test_the_reason_stays_discoverable_by_a_three_word_offer(self):
        """The delete-list drops offers to help, not the one offer that tells
        the user the reasoning exists. Bounded to three words so it cannot
        grow back into the explanation it points at.
        """
        assert 'Not asked? Offer it in three words: "say why".' in self._block()

    def test_answer_only_turns_itself_off_when_depth_is_requested(self):
        block = self._block()
        assert 'Asked for depth (a doc, a walkthrough, "in detail")' in block
        assert "This mode is off for that reply" in block

    def test_answer_only_preserves_the_users_language(self):
        assert "Reply in the user's language." in self._block()

    def test_the_three_checks_are_unique_to_answer_only(self):
        for level in ("concise", "ultra"):
            other = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity=level)
            assert "Shape check" not in other
            assert "at most 12 words" not in other

    def test_answer_only_is_stricter_than_ultra(self):
        answer_only = self._block()
        ultra = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="ultra")
        assert answer_only != ultra
        # ultra budgets an explanation (bullets); answer_only grants none.
        assert "Max 3" in ultra
        assert "Max 3" not in answer_only
        assert "Say only the answer" not in ultra


class TestShippedPromptCarriesToken:
    """Regression guard: the main prompt MUST ship the placeholder, else concise mode is a silent no-op."""

    def test_main_prompt_has_verbosity_placeholder(self):
        prompt_md = Path(kiro_crew.__file__).parent / "config" / "prompt.md"
        assert "{{VERBOSITY_BLOCK}}" in prompt_md.read_text(encoding="utf-8")


class TestVerbosityRoundTrip:
    """dashboard.verbosity persistence (config layer)."""

    @pytest.fixture()
    def cfg_file(self, tmp_path):
        p = tmp_path / "config.json"
        p.write_text("{}", encoding="utf-8")
        with patch("kiro_crew.config.loader.config_path", return_value=p):
            yield p

    def test_defaults_to_default(self):
        assert KiroCrewConfig().dashboard.verbosity == "default"

    def test_answer_only_is_an_advertised_enum_value(self):
        """The Settings UI and the config-patch validator both read this enum;
        a level missing here is a level the user cannot select.
        """
        field = KiroCrewConfig().dashboard.__dataclass_fields__["verbosity"]
        assert field.metadata["enum"] == ["default", "concise", "ultra", "answer_only"]

    def test_answer_only_round_trips(self, cfg_file):
        cfg = KiroCrewConfig()
        cfg.dashboard.verbosity = "answer_only"
        cfg.save()
        assert KiroCrewConfig.load().dashboard.verbosity == "answer_only"

    def test_save_load(self, cfg_file):
        cfg = KiroCrewConfig()
        cfg.dashboard.verbosity = "concise"
        cfg.save()
        assert json.loads(cfg_file.read_text())["dashboard"]["verbosity"] == "concise"
        assert KiroCrewConfig.load().dashboard.verbosity == "concise"

    def test_load_from_existing(self, cfg_file):
        cfg_file.write_text(json.dumps({"dashboard": {"verbosity": "concise"}}), encoding="utf-8")
        assert KiroCrewConfig.load().dashboard.verbosity == "concise"


@pytest.fixture()
def cfg_file(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{}", encoding="utf-8")
    with patch("kiro_crew.config.loader.config_path", return_value=p):
        yield p


@pytest.fixture()
def mock_sel():
    try:
        import kiro_crew.dashboard.handlers  # noqa: F401
    except ImportError:
        pytest.skip("dashboard handler deps not available locally")
    m = MagicMock()
    m.log_tool_invocation = MagicMock()
    with patch("kiro_crew.dashboard.handlers.sel", return_value=m):
        yield m


@pytest.fixture()
def handler_app(cfg_file, mock_sel):
    from kiro_crew.dashboard.handlers.files import api_dashboard_config

    app = web.Application()
    app.router.add_put("/api/dashboard/config", api_dashboard_config)
    app.router.add_get("/api/dashboard/config", api_dashboard_config)
    return as_owner(app)


@pytest.mark.asyncio
async def test_handler_put_verbosity_concise(handler_app, cfg_file):
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.put("/api/dashboard/config", json={"verbosity": "concise"})
        assert resp.status == 200
    assert KiroCrewConfig.load().dashboard.verbosity == "concise"


@pytest.mark.asyncio
async def test_handler_put_verbosity_ultra(handler_app, cfg_file):
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.put("/api/dashboard/config", json={"verbosity": "ultra"})
        assert resp.status == 200
    assert KiroCrewConfig.load().dashboard.verbosity == "ultra"


@pytest.mark.asyncio
async def test_handler_put_verbosity_answer_only(handler_app, cfg_file):
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.put("/api/dashboard/config", json={"verbosity": "answer_only"})
        assert resp.status == 200
    assert KiroCrewConfig.load().dashboard.verbosity == "answer_only"


@pytest.mark.asyncio
async def test_handler_rejection_names_every_accepted_level(handler_app, cfg_file):
    """A 400 that omits a level reads as "that level does not exist"."""
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.put("/api/dashboard/config", json={"verbosity": "aggressive"})
        assert resp.status == 400
        message = (await resp.json())["error"]
    for level in ("default", "concise", "ultra", "answer_only"):
        assert level in message, level


@pytest.mark.asyncio
async def test_handler_put_verbosity_rejects_invalid(handler_app, cfg_file):
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.put("/api/dashboard/config", json={"verbosity": "aggressive"})
        assert resp.status == 400
    # bad value must not be persisted
    assert KiroCrewConfig.load().dashboard.verbosity == "default"


@pytest.mark.asyncio
async def test_handler_get_returns_verbosity(handler_app, cfg_file):
    cfg_file.write_text(json.dumps({"dashboard": {"verbosity": "concise"}}), encoding="utf-8")
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.get("/api/dashboard/config")
        assert resp.status == 200
        assert (await resp.json())["verbosity"] == "concise"

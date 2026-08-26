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

    def test_missing_verbosity_attr_defaults_to_empty(self):
        fake_cfg = SimpleNamespace(dashboard=SimpleNamespace(widget_density="more"))
        with patch("kiro_crew.context.KiroCrewConfig.load", return_value=fake_cfg):
            result = ContextBuilder._resolve_prompt_templates("a {{VERBOSITY_BLOCK}} b", "dashboard:x")
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
        """The brevity floor: a terse reply must never truncate a security
        warning, a destructive-action confirmation, or a step in an ordered
        procedure — those failures cause mistakes, not just terseness.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="ultra")
        assert "security warnings" in result
        assert "irreversible" in result
        assert "multi-step" in result
        # Correctness carve-out: code/errors are never compressed.
        assert "verbatim" in result

    def test_unknown_level_falls_back_to_empty(self):
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="bogus")
        assert result == ""


class TestAnswerOnlyBlock:
    """``answer_only`` is the strictest level: the answer, and no prose around it.

    ``ultra`` still budgets a 1-2 sentence answer plus up to three supporting
    bullets, so it shortens explanation without removing it. ``answer_only``
    removes it: explanation becomes opt-in, capped at one sentence when the
    answer genuinely cannot stand alone.
    """

    def test_answer_only_emits_its_own_block_on_every_transport(self):
        for key in ("dashboard:abc", "slack:C1:1.2", "cli:local", ""):
            result = _resolve("{{VERBOSITY_BLOCK}}", key, verbosity="answer_only")
            assert "## Response Verbosity: Answer Only" in result
            # The other levels must NOT leak in -- the branches are exclusive.
            assert "Concise mode is on" not in result
            assert "Ultra-Brief" not in result

    def test_answer_only_makes_explanation_opt_in(self):
        """The whole point of the level: the user asks, or it does not exist."""
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        assert "Explanation is opt-in" in result
        assert "No explanation by default" in result

    def test_answer_only_caps_unavoidable_context_at_one_sentence(self):
        """A hard numeric cap, because "brief" is what ultra already says and
        the model reads it as a licence to expand.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        assert "you get ONE sentence. Not two." in result

    def test_answer_only_names_the_categories_it_removes(self):
        """Enumerated bans, not a vague "be brief" -- each named category is a
        distinct way explanation creeps back in.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        for banned in (
            "preamble",
            "restating the question",
            "what you just did",
            "rationale",
            "alternatives",
            "caveats",
            "trade-offs",
            "offers to help",
        ):
            assert banned in result, banned
        assert "do not narrate it" in result

    def test_answer_only_cuts_prose_never_payload(self):
        """Regression floor: a mode that removes explanation must not start
        truncating the thing being asked for.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        assert "verbatim and complete" in result
        assert "cuts prose, never payload" in result

    def test_answer_only_turns_itself_off_when_detail_is_requested(self):
        """Detailed explanations are still reachable -- by asking. Without this
        the level is a dead end rather than a default.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        assert "asks you to explain" in result
        assert "this mode is off" in result
        assert "full detail they asked for" in result

    def test_answer_only_explains_high_stakes_decisions_unasked(self):
        """The second escape hatch, and the one the user cannot trigger: when a
        recommendation carries real consequences, the reasoning is part of the
        answer. Waiting to be asked assumes the user already knows enough to
        know they should ask — which is exactly what is missing when the stakes
        are highest.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        assert "Explain in full, unasked" in result
        assert "cannot decide correctly without the reasoning" in result

    def test_answer_only_names_the_high_stakes_domains(self):
        """Named domains, so the model does not have to infer what "important"
        means from an abstraction it can rationalise away.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        for domain in (
            "security posture",
            "credential or data exposure",
            "permissions and trust boundaries",
            "deleting or overwriting data",
            "spend",
            "hard to undo",
        ):
            assert domain in result, domain

    def test_high_stakes_hatch_is_a_judgement_not_a_checklist(self):
        """The named domains are examples, not an allowlist — an unlisted but
        equally consequential decision must still get the explanation.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        assert "a judgement you make, not a category you match" in result

    def test_high_stakes_explanation_lands_before_the_user_chooses(self):
        """Reasoning delivered after the decision is not a decision aid. The
        prompt has to say WHEN, not just THAT.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        assert "how reversible it is" in result
        assert "BEFORE they choose" in result

    def test_high_stakes_underexplaining_is_named_as_the_worse_failure(self):
        """Without an explicit ordering the model resolves the conflict toward
        the mode it was just told to obey, and stays terse where it matters.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        assert "is a defect here, not brevity" in result
        assert "Terseness is worth less than a correct decision" in result

    def test_the_stakes_hatch_is_unique_to_answer_only(self):
        """concise and ultra shorten explanation rather than removing it, so
        they need no such override; asserting that keeps the levels distinct.
        """
        answer_only = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        assert "Explain in full, unasked" in answer_only
        for level in ("concise", "ultra"):
            assert "Explain in full, unasked" not in _resolve(
                "{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity=level
            )

    def test_a_destructive_command_carries_its_undo_path(self):
        """Measured gap this closes: asked how to delete every local branch
        merged into main, answer_only returned the bare command and conveyed
        reversibility in 0/3 samples where unconstrained default managed 2/3
        (two independent graders agreeing). The high-stakes paragraph covers
        RECOMMENDING an action in a consequential class; it did not cover the
        answer simply BEING the destructive command, where "show it and stop"
        applies and stops too early.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        assert "destroys, overwrites or rewrites something" in result
        assert "the undo path rides along with it in the same reply" in " ".join(result.split())
        assert "or plainly that you cannot" in result

    def test_the_undo_note_is_bounded_so_it_cannot_reopen_explanation(self):
        """The rule has to buy exactly one clause. Without a bound it becomes a
        licence to explain, which is the failure mode this whole level exists
        to prevent.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        assert "One clause is enough" in result

    def test_the_undo_rule_is_scoped_to_the_show_it_and_stop_rule(self):
        """It is an exception to stopping, not a new general obligation -- a
        non-destructive command still gets handed over bare.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        assert "One exception to stopping" in result
        assert "Show it and stop" in result

    def test_the_undo_rule_names_the_cost_of_omitting_it(self):
        """Naming the consequence is what makes the model treat a missing undo
        path as a defect rather than as successful brevity.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        assert "is not a terse answer, it is a trap" in " ".join(result.split())

    def test_the_undo_rule_is_unique_to_answer_only(self):
        """concise and ultra still permit explanation around a command, so they
        need no such rule; asserting it keeps the levels from converging.
        """
        for level in ("concise", "ultra"):
            assert "One exception to stopping" not in _resolve(
                "{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity=level
            )

    def test_answer_only_keeps_safety_carveout(self):
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        assert "security warnings" in result
        assert "irreversible" in result
        assert "multi-step" in result

    def test_answer_only_never_cuts_a_required_output_format(self):
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        assert "Required output formats are sacred" in result
        assert "[OPTIONS:] lines" in result
        assert "diff blocks for file changes" in result
        assert "full PR/MR URLs" in result

    def test_answer_only_preserves_the_users_language(self):
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        assert "Preserve the user's language" in result

    def test_answer_only_is_stricter_than_ultra(self):
        answer_only = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        ultra = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="ultra")
        assert answer_only != ultra
        # ultra budgets an explanation (bullets); answer_only grants none.
        assert "Max 3" in ultra
        assert "Max 3" not in answer_only
        assert "No explanation by default" not in ultra


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
    return app


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

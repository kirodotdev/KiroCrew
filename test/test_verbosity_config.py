"""Tests for the Response Verbosity control (``default`` / ``concise`` / ``ultra``).

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
            assert "## Response Verbosity: Punchline First (ADHD reader)" in result
            assert "reader with ADHD" in result
            # The concise block must NOT leak in — the branches are exclusive.
            assert "Concise mode is on" not in result

    def test_ultra_caps_the_opening_not_the_whole_reply(self):
        """Regression: the cap is a lede budget, NOT a hard whole-response limit.

        An earlier draft said "Hard cap: ~3 sentences of prose per response",
        which suppressed detail the user actually wanted. The cap must be
        scoped to the opening and must explicitly license detail after it.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="ultra")
        assert "at most 3 sentences" in result
        assert "governs the OPENING, not the whole response" in result
        assert "supporting detail is welcome" in result
        assert "Hard cap" not in result

    def test_ultra_requires_scannable_structure(self):
        """Detail after the lede must be scannable, and structure is not padding."""
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="ultra")
        assert "scannable" in result
        assert "it is not padding" in result

    def test_ultra_bans_narration_and_reasoning_dumps(self):
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="ultra")
        assert "narration" in result
        assert "not the reasoning chain" in result

    def test_ultra_still_allows_the_chain_for_hard_problems(self):
        """Regression guard: "conclusion not chain" must not suppress a chain the
        user needs. An over-strict reading left hard diagnoses unexplainable.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="ultra")
        assert "Do include the chain when the user needs it" in result
        assert "genuinely" in result and "hard problem" in result

    def test_ultra_still_allows_options_when_no_clear_winner(self):
        """Regression guard: "take a position" must not suppress genuine options.

        Hedging is the target, not the existence of alternatives — when the
        recommendation is not a clear winner the contenders ARE the answer.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="ultra")
        assert "not a clear winner" in result
        assert "Hedging is the thing to avoid, not the existence of options" in result

    def test_ultra_prefers_punchy_text_over_bold_labels(self):
        """Emphasis is not the mechanism — a punchy first clause is."""
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="ultra")
        assert "a punchy first clause beats a bold label" in result
        assert "bold lead-in label" not in result

    def test_ultra_never_cuts_a_required_output_format(self):
        """Regression guard: the "no closing summary" rule must not eat a
        surface-required trailing element (an options line, a diff block, a
        PR URL), which renders the response broken rather than terse.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="ultra")
        assert "Required output formats are not filler and are never cut" in result
        assert "options/choice line" in result
        assert "diff block" in result

    def test_ultra_is_stricter_than_concise(self):
        ultra = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="ultra")
        concise = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="concise")
        assert ultra != concise
        # concise explicitly ALLOWS a brief progress note; ultra budgets one line.
        assert "Keep progress signal brief, not absent" in concise
        assert "Keep progress signal brief, not absent" not in ultra

    def test_ultra_keeps_safety_carveout(self):
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="ultra")
        assert "security warnings" in result
        assert "irreversible" in result
        assert "multi-step" in result
        # Correctness carve-out: code/errors are never compressed.
        assert "verbatim" in result

    def test_unknown_level_falls_back_to_empty(self):
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="bogus")
        assert result == ""


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

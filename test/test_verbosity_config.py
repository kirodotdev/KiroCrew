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

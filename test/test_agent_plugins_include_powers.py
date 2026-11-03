"""Every spec Crew writes keeps KAS's own plugin loader off (``includePowers: false``).

Agent Plugins reach a session only through Crew's loader (the plugin MCP source,
after the owner turned a plugin on), so KAS must never load the same plugin a
second time. Every code site that pins ``includeMcpJson`` off pins
``includePowers`` off beside it; the source scan below is what catches a writer
added later. Shipped JSON templates do not carry the key: their writers set it
(``agent._refresh_dynamic_fields`` for the default agent, ``_register_agents``
for app agents), and the tests below prove both.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from app_bridges_helpers import _stub_venv_probe  # noqa: F401
from app_bridges_helpers import app_env_fixture  # noqa: F401
from app_bridges_helpers import _make_app_source

from kiro_crew import agent as agent_mod
from kiro_crew.acp.client_mcp_array import spec_is_toolless
from kiro_crew.acp.kas_agents import to_client_custom_agent
from kiro_crew.agent_materialization import auto_approve
from kiro_crew.apps.manager import APP_MANIFEST_FILENAME
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.connections.mint import _mint_spec_body
from kiro_crew.dashboard.side_readonly_spec import derive_readonly_spec

SRC = Path(__file__).resolve().parent.parent / "src" / "kiro_crew"
_PIN = re.compile(r"""["']includeMcpJson["']\s*(?:\]\s*=|:)\s*(?:False|false)\b""")
_POWERS = re.compile(r"""["']includePowers["']\s*(?:\]\s*=|:)\s*(?:False|false)\b""")


def _writer_sites() -> list[tuple[Path, int]]:
    sites = []
    for path in sorted(SRC.rglob("*.py")):
        if "docs" in path.parts:
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if _PIN.search(line):
                sites.append((path, i))
    return sites


def test_the_scan_finds_the_known_writers():
    names = {p.name for p, _ in _writer_sites()}
    for expected in (
        "agent.py",
        "auto_approve.py",
        "service_agents.py",
        "spec_rebuild.py",
        "mint.py",
        "side_readonly_spec.py",
    ):
        assert expected in names


@pytest.mark.parametrize("site", _writer_sites(), ids=lambda s: f"{s[0].name}:{s[1] + 1}")
def test_every_include_mcp_json_pin_has_an_include_powers_pin(site):
    path, line = site
    lines = path.read_text(encoding="utf-8").splitlines()
    window = "\n".join(lines[max(0, line - 3) : line + 4])
    assert _POWERS.search(window), f"{path}:{line + 1} pins includeMcpJson but not includePowers"


def test_the_governed_tail_pins_it():
    config: dict = {"includePowers": True, "tools": []}
    auto_approve.govern_spec(config, agent_filename="x.json", source="test", kas_policy="none")
    assert config["includePowers"] is False


def test_the_default_agent_refresh_pins_it():
    config: dict = {"name": "kirocrew", "includePowers": True, "tools": []}
    agent_mod._refresh_dynamic_fields(config, gated_off=frozenset())
    assert config["includeMcpJson"] is False and config["includePowers"] is False


def test_the_side_readonly_spec_pins_it():
    spec = derive_readonly_spec({"name": "base", "includePowers": True}, base_name="base")
    assert spec["includePowers"] is False


def test_the_connection_mint_body_pins_it():
    assert _mint_spec_body("kirocrew-mint-x", {}, "")["includePowers"] is False


def test_an_app_agent_spec_pins_it(app_env, tmp_path):
    """App agents are written from their shipped template outside the governed
    tail, so ``_register_agents`` sets the key itself; the template need not."""
    from kiro_crew.apps import bridges

    src = _make_app_source(tmp_path, name="powers-app")
    manifest = AppManifest.from_dict(json.loads((src / APP_MANIFEST_FILENAME).read_text()))
    bridges._register_agents("powers-app", manifest, src)
    written = json.loads((app_env["kiro_agents"] / "powers-app--my-agent.json").read_text())
    assert written["includePowers"] is False


def test_kas_still_receives_the_flag_and_a_toolless_spec_stays_toolless():
    spec = {"name": "a", "tools": [], "includeMcpJson": False, "includePowers": False}
    assert spec_is_toolless(spec)
    out = to_client_custom_agent("a", spec, "", session_key="k")
    assert out["includePowers"] is False and out["includeMcpJson"] is False

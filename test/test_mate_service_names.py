# SPDX-License-Identifier: Apache-2.0
"""The crewmate writer keeps the mate conductor and worker specs apart.

Both are generated into ``~/.kiro/mates/agents/`` beside the crewmates' own
files (``test_mate_service_specs.py``), so no crewmate file may take either name,
and a crewmate created on the conductor copies it from that folder.
"""

from __future__ import annotations

import json

import pytest

import kiro_crew.config.paths as paths_mod
from kiro_crew.agent_files import MATE_SERVICE_AGENT_NAMES

CONDUCTOR = "kirocrew-mate-conductor"


@pytest.fixture
def mates(tmp_path, monkeypatch):
    kiro = tmp_path / "kiro"
    (kiro / "agents").mkdir(parents=True)
    monkeypatch.setattr(paths_mod, "kiro_home", lambda: kiro)
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: kiro / "agents")
    folder = kiro / "mates" / "agents"
    folder.mkdir(parents=True)
    return folder


@pytest.mark.parametrize("name", sorted(MATE_SERVICE_AGENT_NAMES))
def test_a_crewmate_file_by_a_service_name_is_refused(mates, name):
    from kiro_crew.mate_config import MateConfigError, has_mate_config, write_mate_spec

    generated = {"name": name, "prompt": "generated"}
    (mates / f"{name}.json").write_text(json.dumps(generated), "utf-8")
    with pytest.raises(MateConfigError, match="no crewmate may own"):
        write_mate_spec(name, {"name": name, "prompt": "crewmate"})
    assert json.loads((mates / f"{name}.json").read_text("utf-8")) == generated
    assert has_mate_config(name) is False


def test_a_crewmate_created_on_the_conductor_copies_it_from_the_crewmate_folder(mates):
    """The first crewmate is created on the conductor; its copy reads the moved spec."""
    from kiro_crew.mate_config import _resolved_source_spec

    (mates / f"{CONDUCTOR}.json").write_text(
        json.dumps({"name": CONDUCTOR, "prompt": "c"}), "utf-8"
    )
    assert _resolved_source_spec(CONDUCTOR)["prompt"] == "c"

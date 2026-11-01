"""A crew named as a crew answers to its ``member_id`` without being re-keyed.

``config.agents`` holds two key shapes side by side and neither is rewritten: a
record from before crews were keyed by id is keyed by its display name and
carries its id as ``member_id``; a newer one is keyed by its id. A caller that
has stated the name is a crew (an explicit ``crew_agent``, a ``member``
selection, a member-namespace lookup) and holds the id of a name-keyed record
must reach the key the record is stored under. A bare ``agent`` that may be a
template is never taken as a member id, and a ``display_name`` label never
resolves a crew.
"""

from __future__ import annotations

import pytest

from kiro_crew.config.loader import (
    KiroCrewAgentConfig,
    KiroCrewConfig,
    resolve_crew_identity,
)
from kiro_crew.config.loader_agents import _resolve_agent_selection
from kiro_crew.members import canonical_member_key, member_slug

LEGACY_KEY = "Crew Program Manager"
LEGACY_ID = "crew-program-manager"
NEW_KEY = "release-captain"
NEW_LABEL = "Release Captain"
TEMPLATE = "kirocrew"


@pytest.fixture
def cfg(monkeypatch):
    config = KiroCrewConfig()
    config.agents = {
        "default": KiroCrewAgentConfig(kiro_agent=TEMPLATE),
        LEGACY_KEY: KiroCrewAgentConfig(kiro_agent=TEMPLATE, member_id=LEGACY_ID),
        NEW_KEY: KiroCrewAgentConfig(
            kiro_agent=TEMPLATE, member_id=NEW_KEY, display_name=NEW_LABEL
        ),
    }
    config.default_agent = "default"

    def materialized(name, project_dir=None):
        return name if name == TEMPLATE else ""

    for target in ("kiro_crew.config.loader", "kiro_crew.config.loader_agents"):
        monkeypatch.setattr(f"{target}._materialized_kiro_agent", materialized)
    return config


def test_stored_keys_are_not_rewritten(cfg):
    assert list(cfg.agents) == ["default", LEGACY_KEY, NEW_KEY]


@pytest.mark.parametrize(
    ("handle", "key"),
    [(LEGACY_KEY, LEGACY_KEY), (LEGACY_ID, LEGACY_KEY), (NEW_KEY, NEW_KEY)],
)
def test_a_crew_named_as_a_crew_resolves_by_key_or_member_id(cfg, handle, key):
    assert canonical_member_key(handle, cfg) == key
    assert resolve_crew_identity(cfg, None, handle) == key
    record, alias, template, resolved = _resolve_agent_selection(
        cfg, handle, selection_kind="member"
    )
    assert (record, alias, template, resolved) == (cfg.agents[key], key, "", True)


@pytest.mark.parametrize("handle", [LEGACY_KEY, LEGACY_ID])
def test_both_handles_of_a_legacy_record_share_one_slug(cfg, handle):
    assert member_slug(handle, cfg) == LEGACY_ID


def test_a_bare_agent_is_never_taken_as_a_member_id(cfg):
    assert resolve_crew_identity(cfg, LEGACY_ID, None) == ""
    record, alias, _, _ = _resolve_agent_selection(cfg, LEGACY_ID)
    assert (record, alias) == (None, "")


def test_template_selection_never_resolves_a_member_id(cfg):
    record, alias, _, _ = _resolve_agent_selection(cfg, LEGACY_ID, selection_kind="template")
    assert (record, alias) == (None, "")


def test_a_template_name_stays_a_template_when_a_crew_carries_it_as_its_id(cfg):
    cfg.agents[LEGACY_KEY].member_id = TEMPLATE
    assert resolve_crew_identity(cfg, TEMPLATE, None) == ""
    record, alias, template, _ = _resolve_agent_selection(cfg, TEMPLATE)
    assert (record, alias, template) == (None, "", TEMPLATE)


def test_a_label_never_resolves_a_crew(cfg):
    assert canonical_member_key(NEW_LABEL, cfg) == NEW_LABEL
    assert resolve_crew_identity(cfg, None, NEW_LABEL) == NEW_LABEL
    record, alias, _, resolved = _resolve_agent_selection(cfg, NEW_LABEL, selection_kind="member")
    assert (record, alias, resolved) == (None, "", False)


def test_a_member_id_two_records_carry_resolves_to_nothing(cfg):
    cfg.agents["other"] = KiroCrewAgentConfig(kiro_agent=TEMPLATE, member_id=LEGACY_ID)
    assert canonical_member_key(LEGACY_ID, cfg) == LEGACY_ID
    assert resolve_crew_identity(cfg, None, LEGACY_ID) == LEGACY_ID
    record, alias, _, resolved = _resolve_agent_selection(cfg, LEGACY_ID, selection_kind="member")
    assert (record, alias, resolved) == (None, "", False)


def test_no_crew_stays_no_crew(cfg):
    assert resolve_crew_identity(cfg, LEGACY_KEY, "") == ""


def test_unknown_name_is_unchanged(cfg):
    assert canonical_member_key("nobody", cfg) == "nobody"
    assert resolve_crew_identity(cfg, None, "nobody") == "nobody"


def test_an_empty_roster_leaves_a_handle_unchanged():
    config = KiroCrewConfig()
    config.agents = {}
    assert canonical_member_key(LEGACY_ID, config) == LEGACY_ID
    assert resolve_crew_identity(config, None, LEGACY_ID) == LEGACY_ID

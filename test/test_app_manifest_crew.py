"""The manifest's ``crew`` section: the templates an app offers for hire.

A template is a Custom Agent the app already ships under ``agents`` plus a job
card (role, triggers, initial briefing). Typed like ``contributes`` so it is
checked on every parse, round-trips, and is signed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.apps.manifest import (
    _MAX_CREW_DUTY,
    _MAX_CREW_ROLE,
    _MAX_CREW_STARTERS,
    _MAX_CREW_TAGS,
    _MAX_CREW_TEMPLATES_PER_APP,
    CREW_CATEGORIES,
    AppManifest,
    CrewConfig,
    CrewTemplate,
)

GHOST = {
    "kind": "ghost",
    "traits": {
        "eyes": "visor",
        "brows": "none",
        "mouth": "none",
        "accessory": "phones",
        "prop": "bolt",
        "blush": False,
        "flip": False,
        "tile": "#de2121",
    },
}


def _gallery_card(**overrides) -> dict:
    """A card carrying everything the hire gallery renders."""
    base = _template(
        duty="Triages every page, correlates it with deploys.",
        category="ops",
        tags=["Incident triage", "Deploy correlation", "Rollback plans"],
        starter_prompts=[
            "What paged overnight?",
            {"text": "Draft a rollback plan.", "attachment": "incident-4471.md"},
        ],
        avatar=GHOST,
    )
    base.update(overrides)
    return base


def _manifest(**overrides) -> dict:
    base = {
        "name": "oncall-pack",
        "version": "1.2.0",
        "displayName": "Oncall pack",
        "description": "Oncall roles",
        "author": "tester",
        "agents": ["agents/triage.json", "agents/scribe.json"],
    }
    base.update(overrides)
    return base


def _template(**overrides) -> dict:
    base = {
        "agent": "agents/triage.json",
        "role": "Oncall Triage Engineer",
        "description": "Triages pages.",
        "triggers": "incident, prod outage",
        "initial_briefing": "briefings/triage.md",
    }
    base.update(overrides)
    return base


class TestParseAndRoundTrip:
    def test_parses_into_typed_templates(self):
        m = AppManifest.from_dict(_manifest(crew={"templates": [_template()]}))
        assert isinstance(m.crew, CrewConfig)
        assert len(m.crew.templates) == 1
        t = m.crew.templates[0]
        assert isinstance(t, CrewTemplate)
        assert (t.agent, t.role, t.triggers) == (
            "agents/triage.json",
            "Oncall Triage Engineer",
            "incident, prod outage",
        )
        assert t.initial_briefing == "briefings/triage.md"
        # A known field, not `extra`.
        assert "crew" not in m.extra

    def test_round_trips_and_omits_an_empty_section(self):
        d = _manifest(crew={"templates": [_template()]})
        assert AppManifest.from_dict(d).to_dict()["crew"] == d["crew"]
        assert "crew" not in AppManifest.from_dict(_manifest()).to_dict()

    def test_role_whitespace_is_collapsed_and_non_strings_read_as_empty(self):
        m = AppManifest.from_dict(
            _manifest(crew={"templates": [_template(role="  Oncall   Triage ", triggers=7)]})
        )
        t = m.crew.templates[0]
        assert t.role == "Oncall Triage"
        assert t.triggers == ""

    def test_the_gallery_fields_parse_normalize_and_round_trip(self):
        m = AppManifest.from_dict(_manifest(crew={"templates": [_gallery_card()]}))
        t = m.crew.templates[0]
        assert t.duty == "Triages every page, correlates it with deploys."
        assert t.category == "ops" and t.scenario == "ops"
        assert t.tags == ["Incident triage", "Deploy correlation", "Rollback plans"]
        # A bare string is a prompt with no attachment; an object keeps its attachment.
        assert t.starter_prompts == [
            {"text": "What paged overnight?"},
            {"text": "Draft a rollback plan.", "attachment": "incident-4471.md"},
        ]
        assert t.avatar == GHOST
        # The member's face is the card's ghost, normalized by the crew record's validator.
        assert t.member_avatar["kind"] == "ghost"
        assert t.member_avatar["traits"]["eyes"] == "visor"
        d = _manifest(crew={"templates": [_gallery_card()]})
        out = AppManifest.from_dict(d).to_dict()["crew"]["templates"][0]
        assert out["starter_prompts"] == t.starter_prompts  # normalized form round-trips
        assert {k: out[k] for k in ("duty", "category", "tags", "avatar")} == {
            k: d["crew"]["templates"][0][k] for k in ("duty", "category", "tags", "avatar")
        }
        assert "starter_prompts" in out

    def test_a_card_without_gallery_fields_files_under_other_and_serializes_as_before(self):
        d = _manifest(crew={"templates": [_template()]})
        t = AppManifest.from_dict(d).crew.templates[0]
        assert t.scenario == "other" and t.member_avatar == {}
        assert AppManifest.from_dict(d).to_dict()["crew"] == d["crew"]

    def test_category_is_case_folded_and_an_unknown_one_reads_as_other_after_validation(self):
        t = AppManifest.from_dict(
            _manifest(crew={"templates": [_gallery_card(category=" Research ")]})
        ).crew.templates[0]
        assert t.category == "research" and t.scenario == "research"
        assert set(CREW_CATEGORIES) >= {
            "engineering",
            "ops",
            "research",
            "release",
            "product",
            "writing",
            "other",
        }


class TestValidation:
    def test_a_well_formed_section_validates(self, tmp_path: Path):
        (tmp_path / "agents").mkdir()
        (tmp_path / "agents" / "triage.json").write_text("{}")
        m = AppManifest.from_dict(_manifest(crew={"templates": [_template()]}))
        assert m.validate(app_root=tmp_path) == []

    def test_two_agent_files_registering_under_one_name_fail_install(self, tmp_path: Path):
        """The bridge materializes every agent as ``<app>--<effective name>``
        (declared ``name``, else the stem); two files sharing one overwrite each
        other, and a template hire would copy whichever registered last. Judged
        only when the tree is known."""
        (tmp_path / "agents").mkdir()
        (tmp_path / "agents" / "triage.json").write_text(json.dumps({"name": "triage"}))
        (tmp_path / "agents" / "scribe.json").write_text(json.dumps({"name": "triage"}))
        m = AppManifest.from_dict(_manifest(crew={"templates": [_template()]}))
        assert m.validate() == []  # no root: the declared names cannot be read
        errors = m.validate(app_root=tmp_path)
        assert any("both register as agent 'triage'" in e for e in errors), errors
        # Same stem under two directories, no declared name: the same collision.
        (tmp_path / "agents" / "scribe.json").write_text("{}")
        (tmp_path / "more").mkdir()
        (tmp_path / "more" / "scribe.json").write_text("{}")
        m = AppManifest.from_dict(
            _manifest(agents=["agents/triage.json", "agents/scribe.json", "more/scribe.json"])
        )
        errors = m.validate(app_root=tmp_path)
        assert any("both register as agent 'scribe'" in e for e in errors), errors
        # Distinct names: clean.
        (tmp_path / "more" / "scribe.json").write_text(json.dumps({"name": "notes"}))
        assert m.validate(app_root=tmp_path) == []
        # Names differing only by case collide too: the materialized files are
        # one file on a case-insensitive filesystem.
        (tmp_path / "more" / "scribe.json").write_text(json.dumps({"name": "Triage"}))
        errors = m.validate(app_root=tmp_path)
        assert any("both register as agent 'triage'" in e for e in errors), errors

    def test_a_listed_agent_must_be_a_file_the_app_ships(self, tmp_path: Path):
        """Listed is not shipped: a card whose agent names no regular file would
        install, be skipped by registration, and fail the first hire instead of
        the install. Judged only when the root is known."""
        m = AppManifest.from_dict(_manifest(crew={"templates": [_template()]}))
        assert m.validate() == []  # no root: nothing to check against
        errors = m.validate(app_root=tmp_path)
        assert any("is not a file shipped by the app" in e for e in errors)
        (tmp_path / "agents").mkdir()
        (tmp_path / "agents" / "triage.json").mkdir()  # a directory is not a file either
        assert any("is not a file shipped by the app" in e for e in m.validate(app_root=tmp_path))

    def test_agent_must_be_one_of_the_shipped_agents(self):
        m = AppManifest.from_dict(
            _manifest(crew={"templates": [_template(agent="agents/ghost.json")]})
        )
        errors = m.validate()
        assert any("not one of the manifest's agents paths" in e for e in errors)
        m = AppManifest.from_dict(_manifest(crew={"templates": [_template(agent="")]}))
        assert any("agent is required" in e for e in m.validate())

    def test_role_is_required_and_bounded(self):
        m = AppManifest.from_dict(_manifest(crew={"templates": [_template(role="")]}))
        assert any("role is required" in e for e in m.validate())
        m = AppManifest.from_dict(
            _manifest(crew={"templates": [_template(role="x" * (_MAX_CREW_ROLE + 1))]})
        )
        assert any(f"role exceeds {_MAX_CREW_ROLE}" in e for e in m.validate())

    @pytest.mark.parametrize("briefing", ["../secrets.md", "/etc/passwd.md", "notes.txt"])
    def test_initial_briefing_is_a_markdown_file_inside_the_app(self, briefing, tmp_path):
        (tmp_path / "agents").mkdir()
        (tmp_path / "agents" / "triage.json").write_text("{}")
        m = AppManifest.from_dict(
            _manifest(crew={"templates": [_template(initial_briefing=briefing)]})
        )
        errors = m.validate(app_root=tmp_path)
        assert errors, briefing
        assert all("initial_briefing" in e for e in errors)

    def test_duplicate_agents_and_the_cap_are_refused(self):
        m = AppManifest.from_dict(_manifest(crew={"templates": [_template(), _template()]}))
        assert any("duplicate agent" in e for e in m.validate())
        many = [_template() for _ in range(_MAX_CREW_TEMPLATES_PER_APP + 1)]
        m = AppManifest.from_dict(_manifest(crew={"templates": many}))
        assert any(f"at most {_MAX_CREW_TEMPLATES_PER_APP}" in e for e in m.validate())

    def test_a_gallery_card_validates(self, tmp_path: Path):
        (tmp_path / "agents").mkdir()
        (tmp_path / "agents" / "triage.json").write_text("{}")
        m = AppManifest.from_dict(_manifest(crew={"templates": [_gallery_card()]}))
        assert m.validate(app_root=tmp_path) == []

    @pytest.mark.parametrize(
        "card, needle",
        [
            (_gallery_card(duty="x" * (_MAX_CREW_DUTY + 1)), f"duty exceeds {_MAX_CREW_DUTY}"),
            (_gallery_card(category="weird"), "category 'weird' is not one of"),
            (_gallery_card(tags=["t"] * (_MAX_CREW_TAGS + 1)), f"at most {_MAX_CREW_TAGS} tags"),
            (_gallery_card(tags=["ok", ""]), "tags must not be empty"),
            (_gallery_card(tags=["x" * 33]), "exceeds 32 characters"),
            (_gallery_card(tags="Incident triage"), "tags has the wrong shape"),
            (
                _gallery_card(starter_prompts=["a"] * (_MAX_CREW_STARTERS + 1)),
                f"at most {_MAX_CREW_STARTERS} starter_prompts",
            ),
            (_gallery_card(starter_prompts=[{"text": ""}]), "starter_prompts entries need a text"),
            (_gallery_card(starter_prompts=[7]), "starter_prompts has the wrong shape"),
            (
                _gallery_card(starter_prompts=[{"text": "a", "attachment": "../x.md"}]),
                "attachment must be a short file NAME",
            ),
            (_gallery_card(avatar={"kind": "image"}), "avatar must be a ghost face"),
            (_gallery_card(avatar={"kind": "pack", "id": "foxes"}), "avatar must be a ghost face"),
            (
                _gallery_card(avatar={"kind": "ghost", "traits": {"eyes": "visor"}, "motions": {}}),
                "avatar must be a ghost face",
            ),
            (_gallery_card(avatar="ghost"), "avatar has the wrong shape"),
        ],
    )
    def test_gallery_fields_are_bounded_and_shaped(self, card, needle):
        """Every gallery field becomes rendered text or a face on the member, so a
        card that cannot be rendered as declared fails install, not the gallery."""
        m = AppManifest.from_dict(_manifest(crew={"templates": [card]}))
        errors = m.validate()
        assert any(needle in e for e in errors), (needle, errors)

    @pytest.mark.parametrize(
        "crew, needle",
        [
            ("nope", "crew must be an object"),
            ({"templates": "nope"}, "crew.templates must be an array"),
            ({"templates": [_template(), "nope", 3]}, "2 entries are not an object"),
        ],
    )
    def test_malformed_shapes_fail_validation_instead_of_vanishing(self, crew, needle):
        """The fail-open shape `contributes` closes: a value coerced to "nothing"
        would install clean and never list a template."""
        m = AppManifest.from_dict(_manifest(crew=crew))
        errors = m.validate()
        assert any(needle in e for e in errors), errors


class TestSigning:
    def test_the_job_card_is_part_of_the_signed_payload(self):
        plain = AppManifest.from_dict(_manifest(signer="k1"))
        with_crew = AppManifest.from_dict(_manifest(signer="k1", crew={"templates": [_template()]}))
        assert plain.signing_payload() != with_crew.signing_payload()
        body = json.loads(with_crew.signing_payload())
        assert body["crew"]["templates"][0]["role"] == "Oncall Triage Engineer"
        # Changing the role alone changes the bytes.
        tampered = AppManifest.from_dict(
            _manifest(signer="k1", crew={"templates": [_template(role="Boss")]})
        )
        assert tampered.signing_payload() != with_crew.signing_payload()

    def test_a_manifest_without_templates_keeps_its_pre_crew_payload(self):
        """Manifests signed before templates existed must verify unchanged."""
        m = AppManifest.from_dict(_manifest(signer="k1"))
        assert "crew" not in json.loads(m.signing_payload())

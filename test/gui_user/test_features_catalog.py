"""The feature inventory: features.json is well-formed and FEATURES.md is rendered from it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from gui_user import features_catalog as fc
from gui_user import scenarios

HERE = Path(__file__).parent


class TestShippedInventory:
    def test_features_json_validates(self) -> None:
        doc = fc.load()
        records = fc.validate(doc)
        assert len(records) == doc["counts"]["total"] >= 200
        assert {r["feature"] for r in records} <= set(fc.FEATURE_TITLES)

    def test_registry_matches_the_scenario_loader(self) -> None:
        # One taxonomy: a slug the inventory groups by is a slug a scenario may claim.
        # The loader's registry ships with the feature/user_story scenario schema; on a
        # branch without it the check is skipped, not faked -- once both are on main the
        # two must agree.
        registry = getattr(scenarios, "FEATURES", None)
        if registry is None:
            pytest.skip("scenarios.FEATURES is not on this branch yet")
        assert fc.FEATURE_TITLES == registry

    def test_features_md_is_rendered_from_features_json(self) -> None:
        assert (HERE / "FEATURES.md").read_text(encoding="utf-8") == fc.render(fc.load())

    def test_every_p0_is_runnable_on_the_target(self) -> None:
        # P0 is "first scenario batch"; a P0 the lane cannot drive is a contradiction.
        for r in fc.validate(fc.load()):
            if r["priority"] == "P0":
                assert r["runnable"] in ("smoke", "nightly"), r["id"]
            if r["runnable"] in ("native-only", "needs-secret", "excluded"):
                assert r["priority"] == "P3", r["id"]

    def test_seeds_are_shipped_fixtures(self) -> None:
        fixtures = {
            p.name
            for p in (HERE.parents[1] / "src" / "kiro_crew" / "tests_fixtures").iterdir()
            if p.is_dir()
        }
        for r in fc.validate(fc.load()):
            seed = r["preconditions"].get("seed")
            assert seed in fixtures, f"{r['id']}: seed {seed!r} is not a fixture"


def _doc(**overrides):
    rec = {
        "id": "chat-demo",
        "feature": "chat",
        "title": "Demo",
        "user_story": "As a user, I want a thing, so that it is done.",
        "start_url": "/chat",
        "entry_path": "rail",
        "preconditions": {"seed": "rich", "members": [], "feature_flags": [], "notes": ""},
        "runnable": "smoke",
        "estimated_steps": 3,
        "rationale": "r",
        "source": ["docs/feature-map/README.md"],
        "priority": "P1",
        "merged_from": ["chat-demo"],
        "component": "website/src/pages/ChatDemo.tsx",
    }
    rec.update(overrides)
    return {"generated_from_sha": "abc", "counts": fc.compute_counts([rec]), "features": [rec]}


def _two(**second):
    doc = _doc()
    other = dict(doc["features"][0], id="settings-demo", feature="settings")
    other.update(second)
    doc["features"].append(other)
    doc["features"].sort(
        key=lambda r: (list(fc.FEATURE_TITLES).index(r["feature"]), r["priority"], r["id"])
    )
    doc["counts"] = fc.compute_counts(doc["features"])
    return doc


class TestCrossSlugDuplicates:
    def test_same_route_seed_component_under_two_slugs_is_rejected(self) -> None:
        with pytest.raises(fc.CatalogError, match="cross-slug duplicate at /chat"):
            fc.validate(_two())

    def test_message_names_both_records(self) -> None:
        problems = fc.cross_slug_duplicates(_two()["features"])
        assert len(problems) == 1
        assert "chat: chat-demo" in problems[0] and "settings: settings-demo" in problems[0]

    def test_distinct_route_or_seed_or_component_is_not_a_duplicate(self) -> None:
        assert fc.cross_slug_duplicates(_two(start_url="/settings")["features"]) == []
        assert (
            fc.cross_slug_duplicates(
                _two(
                    preconditions={
                        "seed": "minimal",
                        "members": [],
                        "feature_flags": [],
                        "notes": "",
                    }
                )["features"]
            )
            == []
        )
        assert (
            fc.cross_slug_duplicates(_two(component="website/src/pages/Other.tsx")["features"])
            == []
        )

    def test_same_slug_may_share_a_component(self) -> None:
        # Several flows on one panel under ONE feature are sub-flows, not duplicates.
        doc = _two(feature="chat")
        assert fc.cross_slug_duplicates(doc["features"]) == []

    def test_records_without_a_component_are_exempt(self) -> None:
        assert fc.cross_slug_duplicates(_two(component="")["features"]) == []

    def test_shipped_inventory_has_no_cross_slug_duplicates(self) -> None:
        assert fc.cross_slug_duplicates(fc.load()["features"]) == []


class TestValidation:
    def test_minimal_document_renders(self) -> None:
        md = fc.render(_doc())
        assert "## Chat sessions (`chat`)" in md
        assert "| P1 | `chat-demo` |" in md
        assert "| **All features** | 1 | 1 | 0 | 0 | 0 | 0 |" in md

    def test_proposed_slugs_section_is_a_table_or_says_none(self) -> None:
        # With nothing proposed the section says so instead of printing an empty table;
        # with proposals it lists them so the registry owner can adopt or refuse each.
        assert "None outstanding: every area the readers proposed" in fc.render(_doc())
        doc = _doc()
        doc["proposed_features"] = [{"slug": "teleporter", "title": "Teleporter", "reason": "why"}]
        md = fc.render(doc)
        assert "| `teleporter` | Teleporter | why |" in md
        assert "None outstanding" not in md

    def test_every_registry_slug_is_used_by_the_shipped_inventory(self) -> None:
        # The registry was widened to the areas the inventory readers proposed; a slug no
        # record claims is either a typo in the registry or a record filed elsewhere.
        used = {r["feature"] for r in fc.validate(fc.load())}
        assert used == set(fc.FEATURE_TITLES)

    @pytest.mark.parametrize(
        "overrides,match",
        [
            ({"feature": "teleporter"}, "not a registry slug"),
            ({"feature": []}, "not a registry slug"),
            ({"feature": None}, "not a registry slug"),
            ({"start_url": "chat"}, "absolute path"),
            ({"start_url": "/capabilities?tab=knowledge"}, "absolute path"),
            ({"runnable": "weekly"}, "runnable"),
            ({"priority": "P9"}, "priority"),
            ({"estimated_steps": "3"}, "estimated_steps"),
            ({"estimated_steps": True}, "estimated_steps"),
            ({"source": "x"}, "must be lists"),
            ({"component": 7}, "component must be a string"),
        ],
    )
    def test_rejects_malformed_records(self, overrides, match) -> None:
        with pytest.raises(fc.CatalogError, match=match):
            fc.validate(_doc(**overrides))

    def test_rejects_missing_key_duplicate_id_and_stale_counts(self) -> None:
        doc = _doc()
        del doc["features"][0]["rationale"]
        with pytest.raises(fc.CatalogError, match="missing rationale"):
            fc.validate(doc)
        doc = _doc()
        doc["features"].append(dict(doc["features"][0]))
        doc["counts"] = fc.compute_counts(doc["features"])
        with pytest.raises(fc.CatalogError, match="duplicate id"):
            fc.validate(doc)
        doc = _doc()
        doc["counts"]["smoke"] = 7
        with pytest.raises(fc.CatalogError, match="counts"):
            fc.validate(doc)

    @pytest.mark.parametrize("proposed", ["x", ["x"], [7], [None], [{"slug": "a"}, "b"]])
    def test_rejects_malformed_proposed_features(self, proposed) -> None:
        doc = _doc()
        doc["proposed_features"] = proposed
        with pytest.raises(fc.CatalogError, match="proposed_features must be a list of mappings"):
            fc.render(doc)

    def test_rejects_non_mapping_records_without_crashing(self) -> None:
        for bad in ("chat-demo", 7, None, ["chat-demo"]):
            doc = _doc()
            doc["features"].append(bad)
            doc["counts"] = fc.compute_counts(doc["features"])
            with pytest.raises(fc.CatalogError, match="must be a mapping"):
                fc.validate(doc)

    def test_rejects_out_of_order_records(self) -> None:
        doc = _doc()
        second = dict(doc["features"][0], id="chat-aaa")
        doc["features"].append(second)  # 'chat-aaa' sorts before 'chat-demo'
        doc["counts"] = fc.compute_counts(doc["features"])
        with pytest.raises(fc.CatalogError, match="ordered"):
            fc.validate(doc)

    def test_cell_neutralizes_pipes_and_newlines(self) -> None:
        md = fc.render(_doc(user_story="a | b\nc"))
        assert "a / b c" in md and "a | b" not in md


class TestCli:
    def test_check_and_write(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        j = tmp_path / "features.json"
        m = tmp_path / "FEATURES.md"
        j.write_text(json.dumps(_doc()), encoding="utf-8")
        monkeypatch.setattr(fc, "FEATURES_JSON", j)
        monkeypatch.setattr(fc, "FEATURES_MD", m)
        assert fc.main(["--check"]) == 1  # no markdown yet
        assert "stale" in capsys.readouterr().err
        assert fc.main(["--write"]) == 0
        assert fc.main(["--check"]) == 0
        j.write_text("not json", encoding="utf-8")
        assert fc.main(["--check"]) == 2

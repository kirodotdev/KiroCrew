"""The optional ``widgets`` list of a dashboard package (phone home-screen widgets).

Pins the spec limits and binding rules :mod:`kiro_crew.artifact_store.dashboard_widgets`
enforces, and the three package-level promises: a package without widgets is stored
exactly as before, a widgets-only edit is a new version, and a revert brings the
target version's widgets back.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.artifact_store import dashboard_package as dp
from kiro_crew.artifact_store import dashboard_widgets as dw
from kiro_crew.artifacts import ArtifactStore, ArtifactValidationError
from kiro_crew.testing.synthetic_credentials import github_classic


def _types() -> dict[str, Any]:
    return {
        "pipeline_state": {
            "type": "enum",
            "choices": ["green", "amber", "red"],
            "source": {"agentic": True},
            "stale_after_s": 3600,
        },
        "open_prs": {"type": "number", "source": {"agentic": True}, "stale_after_s": 3600},
        "milestones_done": {"type": "number", "source": {"agentic": True}},
        "milestones_total": {"type": "number", "source": {"agentic": True}},
        "headline": {"type": "text", "source": {"agentic": True}},
        "waiting_items": {"type": "array", "source": {"fold": "waiting", "path": "items"}},
        "accepted_series": {"type": "array", "source": {"fold": "workstreams", "path": "series"}},
        "boards": {"type": "array", "source": {"fold": "worktree", "path": "boards"}},
    }


def _blocks() -> list[dict[str, Any]]:
    return [
        {
            "type": "status",
            "label": "Pipeline",
            "field": "pipeline_state",
            "tones": {"green": "ok", "amber": "warn", "red": "bad"},
        },
        {
            "type": "metric",
            "label": "Open PRs",
            "field": "open_prs",
            "format": "integer",
            "warn_at": 10,
            "bad_at": 20,
            "higher_is_worse": True,
        },
        {
            "type": "progress",
            "label": "Milestones",
            "field": "milestones_done",
            "total_field": "milestones_total",
        },
        {
            "type": "list",
            "label": "Waiting on you",
            "field": "waiting_items",
            "item_text": "title",
            "item_detail": "age",
            "max_items": 3,
        },
        {
            "type": "sparkline",
            "label": "Accepted per hour",
            "field": "accepted_series",
            "point": "accepted",
            "max_points": 24,
        },
    ]


def _widget(**over: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": "build-health",
        "title": "Build health",
        "sizes": ["small", "medium", "large"],
        "blocks": _blocks(),
        "tap": {"open": "bot"},
    }
    out.update(over)
    return out


def _package(widgets: Any = None, *, types: dict[str, Any] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "kind": "dashboard",
        "bound_to": "crewmate:pebble",
        "model": {"types": types if types is not None else _types()},
        "view": {"html": '<p data-dashboard-field="open_prs"></p>'},
        "theme": {"tokens": {}},
    }
    if widgets is not None:
        out["widgets"] = widgets
    return out


def _refused(widgets: Any, match: str, *, types: dict[str, Any] | None = None) -> str:
    with pytest.raises(ArtifactValidationError, match=match) as info:
        dp.validate_package(_package(widgets, types=types))
    return str(info.value)


def _one_block(block: dict[str, Any]) -> list[dict[str, Any]]:
    return [_widget(blocks=[block])]


class TestAPackageWithoutWidgetsIsUnchanged:
    def test_no_widgets_key_stores_the_same_bytes_and_fingerprint(self) -> None:
        canonical = dp.validate_package(_package())
        assert "widgets" not in canonical
        assert list(canonical) == ["kind", "bound_to", "model", "view", "theme"]
        # The fingerprint formula before widgets existed: model + view + theme only.
        legacy = json.dumps(
            {k: canonical[k] for k in ("model", "view", "theme")},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        assert dp._layout_fingerprint(canonical) == hashlib.sha256(legacy.encode()).hexdigest()

    def test_an_empty_widgets_list_is_the_same_as_none(self) -> None:
        bare = dp.dump_package(dp.validate_package(_package()))
        empty = dp.dump_package(dp.validate_package(_package([])))
        assert empty == bare
        assert not dp.layout_changed(bare, json.dumps(_package([])))


class TestAValidWidget:
    def test_the_design_doc_example_is_accepted_and_canonical(self) -> None:
        widgets = dp.validate_package(_package([_widget()]))["widgets"]
        assert len(widgets) == 1
        w = widgets[0]
        assert w["id"] == "build-health"
        assert w["tap"] == {"open": "bot"}
        assert [b["type"] for b in w["blocks"]] == [
            "status",
            "metric",
            "progress",
            "list",
            "sparkline",
        ]
        assert w["blocks"][1]["unit"] == ""

    def test_defaults_are_filled_and_sizes_ordered(self) -> None:
        blocks = [
            {"type": "metric", "label": "Open", "field": "open_prs"},
            {"type": "list", "label": "Waiting", "field": "waiting_items", "item_text": "text"},
            {"type": "sparkline", "label": "Trend", "field": "accepted_series"},
            {"type": "title", "text": "Pebble"},
            {"type": "title", "field": "headline"},
        ]
        raw = _widget(sizes=["large", "small"], blocks=blocks)
        del raw["tap"]
        w = dp.validate_package(_package([raw]))["widgets"][0]
        assert w["sizes"] == ["small", "large"]
        assert w["tap"] == {"open": "bot"}
        assert w["blocks"][0]["format"] == "integer"
        assert w["blocks"][0]["higher_is_worse"] is True
        assert w["blocks"][1]["max_items"] == dw.LIST_MAX_ITEMS_DEFAULT
        assert w["blocks"][2]["max_points"] == dw.SPARKLINE_MAX_POINTS_DEFAULT

    def test_a_widget_at_every_limit_is_accepted(self) -> None:
        label = "L" * dw.MAX_WIDGET_LABEL_LEN
        block = {
            "type": "metric",
            "label": label,
            "field": "open_prs",
            "unit": "u" * dw.MAX_UNIT_LEN,
        }
        widgets = [
            _widget(id=f"w{i}-" + "x" * 29, title=label, blocks=[dict(block)] * dw.MAX_BLOCKS)
            for i in range(dw.MAX_WIDGETS)
        ]
        stored = dp.validate_package(_package(widgets))["widgets"]
        assert len(stored) == dw.MAX_WIDGETS
        assert all(len(w["blocks"]) == dw.MAX_BLOCKS for w in stored)
        size = len(json.dumps(stored, indent=2, sort_keys=True).encode())
        assert size <= dw.MAX_WIDGETS_BYTES

    def test_unicode_titles_and_labels_count_characters_not_bytes(self) -> None:
        title = "构建健康状况" * 6 + "进度"  # 38 characters, 114 bytes of UTF-8
        label = "é" * dw.MAX_WIDGET_LABEL_LEN
        block = {"type": "metric", "label": label, "field": "open_prs"}
        w = dp.validate_package(_package([_widget(title=title, blocks=[block])]))["widgets"][0]
        assert w["title"] == title and w["blocks"][0]["label"] == label
        _refused(
            [_widget(title="🚀" * (dw.MAX_WIDGET_LABEL_LEN + 1), blocks=[block])],
            "at most 40 characters",
        )


class TestSpecLimits:
    def test_more_than_four_widgets_is_refused(self) -> None:
        widgets = [_widget(id=f"w{i}") for i in range(dw.MAX_WIDGETS + 1)]
        _refused(widgets, "holds 5 widgets; at most 4")

    def test_widgets_must_be_a_list(self) -> None:
        _refused({"id": "x"}, "widgets: must be a list")

    @pytest.mark.parametrize("count", [0, dw.MAX_BLOCKS + 1])
    def test_blocks_outside_one_to_six_are_refused(self, count: int) -> None:
        blocks = [{"type": "metric", "label": "Open", "field": "open_prs"}] * count
        _refused([_widget(blocks=blocks)], "must be a list of 1 to 6 blocks")

    @pytest.mark.parametrize("bad", ["Build", "1build", "build_health", "b" * 33, "", 7])
    def test_a_malformed_id_is_refused(self, bad: Any) -> None:
        _refused([_widget(id=bad)], r"widgets\[0\]\.id: .* is not a widget id")

    def test_a_32_character_id_is_accepted(self) -> None:
        dp.validate_package(_package([_widget(id="b" * 32)]))

    def test_duplicate_ids_are_refused(self) -> None:
        _refused([_widget(), _widget()], r"widgets\[1\]\.id: 'build-health' is used by two")

    def test_the_reserved_status_id_is_refused(self) -> None:
        _refused([_widget(id="status")], "'status' is reserved")

    @pytest.mark.parametrize("title", ["", "   ", "T" * 41, "two\nlines", 5])
    def test_a_bad_title_is_refused(self, title: Any) -> None:
        _refused([_widget(title=title)], r"widgets\[0\]\.title")

    def test_a_label_over_forty_characters_is_refused(self) -> None:
        block = {"type": "metric", "label": "L" * 41, "field": "open_prs"}
        _refused(_one_block(block), r"blocks\[0\]\.label: must be at most 40")

    def test_a_unit_over_eight_characters_is_refused(self) -> None:
        block = {"type": "metric", "label": "Open", "field": "open_prs", "unit": "u" * 9}
        _refused(_one_block(block), r"blocks\[0\]\.unit: must be at most 8")

    @pytest.mark.parametrize(
        ("sizes", "match"),
        [([], "non-empty"), (["tiny"], "'tiny' is not a size"), (["small", "small"], "twice")],
    )
    def test_bad_sizes_are_refused(self, sizes: list[str], match: str) -> None:
        _refused([_widget(sizes=sizes)], match)

    @pytest.mark.parametrize("target", ["artifact", "dashboard", "url", "", None])
    def test_a_tap_other_than_bot_is_refused(self, target: Any) -> None:
        _refused([_widget(tap={"open": target})], r"tap\.open: .* opens only its own Bot")

    def test_a_tap_with_extra_keys_is_refused(self) -> None:
        _refused([_widget(tap={"open": "bot", "url": "https://x"})], "unknown key")

    def test_widgets_over_sixteen_kib_are_refused(self) -> None:
        choices = [f"{i}" + "c" * 119 for i in range(dw.MAX_TONES)]
        types = _types()
        types["lane"] = {"type": "enum", "choices": choices, "source": {"agentic": True}}
        block = {
            "type": "status",
            "label": "Lane",
            "field": "lane",
            "tones": {c: "ok" for c in choices},
        }
        widgets = [
            _widget(id=f"w{i}", blocks=[dict(block)] * dw.MAX_BLOCKS) for i in range(dw.MAX_WIDGETS)
        ]
        _refused(widgets, "over the 16384-byte limit for widgets", types=types)

    @pytest.mark.parametrize("n", [0, 6])
    def test_list_max_items_outside_one_to_five_is_refused(self, n: int) -> None:
        block = {
            "type": "list",
            "label": "W",
            "field": "waiting_items",
            "item_text": "t",
            "max_items": n,
        }
        _refused(_one_block(block), "max_items: must be a whole number from 1 to 5")

    @pytest.mark.parametrize("n", [1, 49])
    def test_sparkline_max_points_outside_two_to_48_is_refused(self, n: int) -> None:
        block = {"type": "sparkline", "label": "S", "field": "accepted_series", "max_points": n}
        _refused(_one_block(block), "max_points: must be a whole number from 2 to 48")

    def test_more_than_eight_tones_is_refused(self) -> None:
        choices = [f"c{i}" for i in range(9)]
        types = _types()
        types["lane"] = {"type": "enum", "choices": choices, "source": {"agentic": True}}
        block = {
            "type": "status",
            "label": "L",
            "field": "lane",
            "tones": dict.fromkeys(choices, "ok"),
        }
        _refused(_one_block(block), "holds 9 entries; at most 8", types=types)

    def test_an_unknown_tone_is_refused(self) -> None:
        block = {"type": "status", "label": "P", "field": "pipeline_state", "tones": {"red": "x"}}
        _refused(_one_block(block), "a tone is one of")

    def test_a_tone_for_a_value_the_enum_does_not_have_is_refused(self) -> None:
        block = {"type": "status", "label": "P", "field": "pipeline_state", "tones": {"blue": "ok"}}
        _refused(_one_block(block), "is not one of the field's choices")


class TestBindings:
    def test_an_undeclared_field_is_refused_by_name(self) -> None:
        block = {"type": "metric", "label": "Open", "field": "closed_prs"}
        message = _refused(_one_block(block), "model.types does not declare")
        assert "'closed_prs'" in message

    def test_an_undeclared_total_field_is_refused_by_name(self) -> None:
        block = {
            "type": "progress",
            "label": "M",
            "field": "milestones_done",
            "total_field": "nope",
        }
        assert "'nope'" in _refused(_one_block(block), "does not declare")

    def test_a_field_of_the_wrong_type_is_refused(self) -> None:
        block = {"type": "sparkline", "label": "S", "field": "open_prs"}
        _refused(_one_block(block), "binds 'open_prs', a 'number' field; this block takes")

    def test_a_worktree_sourced_field_is_refused_with_widget_tree_fold(self) -> None:
        block = {"type": "list", "label": "Boards", "field": "boards", "item_text": "name"}
        message = _refused(_one_block(block), "widget_tree_fold")
        assert "'boards'" in message and "'worktree'" in message

    def test_every_tree_fold_is_refused_not_only_worktree(self, monkeypatch) -> None:
        monkeypatch.setattr(dw, "_tree_fold_names", lambda: frozenset({"waiting"}))
        block = {"type": "list", "label": "W", "field": "waiting_items", "item_text": "t"}
        _refused(_one_block(block), "widget_tree_fold")

    def test_progress_needs_exactly_one_total(self) -> None:
        block = {"type": "progress", "label": "M", "field": "milestones_done"}
        _refused(_one_block(block), "exactly one of 'total' or 'total_field'")
        block.update(total=5, total_field="milestones_total")
        _refused(_one_block(block), "exactly one of 'total' or 'total_field'")

    def test_a_title_needs_exactly_one_of_text_or_field(self) -> None:
        _refused(_one_block({"type": "title"}), "exactly one of 'text' or 'field'")

    def test_an_unknown_block_type_or_key_is_refused(self) -> None:
        _refused(_one_block({"type": "chart", "label": "x", "field": "open_prs"}), "block type")
        block = {"type": "metric", "label": "x", "field": "open_prs", "colour": "red"}
        _refused(_one_block(block), "unknown key")

    def test_a_value_shaped_key_in_a_block_is_refused_as_data(self) -> None:
        block = {"type": "metric", "label": "x", "field": "open_prs", "value": 7}
        _refused(_one_block(block), "a package holds layout only")

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), True, "10"])
    def test_a_threshold_must_be_a_finite_number(self, bad: Any) -> None:
        block = {"type": "metric", "label": "x", "field": "open_prs", "warn_at": bad}
        _refused(_one_block(block), "warn_at: must be a finite number")


class TestRefusalsAreRedacted:
    @pytest.mark.parametrize(
        "make",
        [
            lambda tok: [_widget(id=tok)],
            lambda tok: [_widget(**{tok: 1})],
            lambda tok: _one_block({"type": "metric", "label": "x", "field": tok}),
            lambda tok: _one_block(
                {"type": "status", "label": "P", "field": "pipeline_state", "tones": {tok: "ok"}}
            ),
        ],
        ids=["id", "unknown-key", "field", "tone-key"],
    )
    def test_a_credential_shaped_value_is_not_echoed(self, make: Any) -> None:
        token = github_classic()
        with pytest.raises(ArtifactValidationError) as info:
            dp.validate_package(_package(make(token)))
        assert token not in str(info.value)


class TestNumbers:
    def test_a_huge_json_integer_is_refused_not_crashed(self) -> None:
        text = json.dumps(
            _package(_one_block({"type": "metric", "label": "x", "field": "open_prs"}))
        ).replace('"field": "open_prs"', '"field": "open_prs", "warn_at": 1' + "0" * 400)
        with pytest.raises(ArtifactValidationError, match="warn_at: must be a finite number"):
            dp.parse_package(text)

    def test_a_large_but_safe_integer_and_a_float_are_accepted(self) -> None:
        block = {
            "type": "progress",
            "label": "M",
            "field": "milestones_done",
            "total": dw.MAX_SAFE_INTEGER,
        }
        dp.validate_package(_package(_one_block(block)))
        block["total"] = 1.5e300
        dp.validate_package(_package(_one_block(block)))


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(root=tmp_path / "artifacts")


def _owner_write(store: ArtifactStore, slug: str, **kwargs: Any) -> Any:
    token = store.get(slug, assign_token=True).content_token
    return store.update(slug, snapshot_token=token, **kwargs)


class TestVersionsAndRevert:
    def test_a_widgets_only_edit_creates_a_new_version(self, store: ArtifactStore) -> None:
        art = store.create(
            name="Pebble", kind="dashboard", content=json.dumps(_package([_widget()]))
        )
        before = dp.parse_package(store.get(art.slug).content or "")
        edited = _package([_widget(title="Build health v2")])
        after_art = _owner_write(store, art.slug, content=json.dumps(edited))
        after = dp.parse_package(store.get(art.slug).content or "")
        assert after_art.version == 2
        assert {k: after[k] for k in ("model", "view", "theme")} == {
            k: before[k] for k in ("model", "view", "theme")
        }
        assert dp._layout_fingerprint(after) != dp._layout_fingerprint(before)

    def test_adding_widgets_to_a_package_without_them_creates_a_version(
        self, store: ArtifactStore
    ) -> None:
        art = store.create(name="Pebble", kind="dashboard", content=json.dumps(_package()))
        assert _owner_write(store, art.slug, content=json.dumps(_package([_widget()]))).version == 2

    def test_revert_brings_back_the_target_versions_widgets(self, store: ArtifactStore) -> None:
        v1_widgets = [_widget()]
        art = store.create(
            name="Pebble", kind="dashboard", content=json.dumps(_package(v1_widgets))
        )
        _owner_write(store, art.slug, content=json.dumps(_package([_widget(id="other")])))
        target = store.get(art.slug, version=1)
        reverted = _owner_write(
            store,
            art.slug,
            content=target.content,
            event_type="reverted",
            from_version=1,
            snapshot=True,
        )
        restored = dp.parse_package(store.get(art.slug).content or "")
        assert reverted.version == 3
        assert (
            restored["widgets"]
            == dp.validate_package(_package(copy.deepcopy(v1_widgets)))["widgets"]
        )

    def test_revert_package_carries_widgets_and_keeps_the_live_binding(self) -> None:
        live = _package()
        live["bound_to"] = "crewmate:other"
        out = json.loads(dp.revert_package(json.dumps(live), json.dumps(_package([_widget()]))))
        assert out["bound_to"] == "crewmate:other"
        assert [w["id"] for w in out["widgets"]] == ["build-health"]

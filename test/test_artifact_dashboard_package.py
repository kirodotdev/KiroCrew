"""A ``kind="dashboard"`` artifact holds layout, versions on layout change, and reverts to layout.

Four claims, pinned from the store rather than from the validator alone, because
the store is where every write path lands: the dashboard tool, the browser PATCH
handler and a plain ``artifact_update`` all reach
:meth:`kiro_crew.artifacts.ArtifactStore.update`.

1. A package round-trips: what comes back parses, and the data line's one
   entry point finds it by its binding.
2. A version appears exactly when ``model`` / ``view`` / ``theme`` changed --
   whatever the caller asked for, in either direction.
3. A revert restores layout and keeps the LIVE binding.
4. An invalid package is refused on every write path, with a reason that names
   the rule.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.artifact_store import dashboard_package as dp
from kiro_crew.artifact_store.rules import ALLOWED_KINDS, USER_SELECTABLE_KINDS
from kiro_crew.artifacts import ArtifactStore, ArtifactValidationError

BOUND = "crewmate:mate"


def package(
    *,
    bound_to: str = BOUND,
    types: dict[str, Any] | None = None,
    blocks: list[dict[str, Any]] | None = None,
    tokens: dict[str, str] | None = None,
    css: str | None = None,
) -> dict[str, Any]:
    """A valid package, with each part overridable so a test changes one thing."""
    out: dict[str, Any] = {
        "kind": "dashboard",
        "bound_to": bound_to,
        "model": {
            "types": (
                types
                if types is not None
                else {
                    "open_prs": {"type": "number", "label": "Open PRs"},
                    "last_run": {"type": "timestamp", "label": "Last run"},
                    "lane": {"type": "enum", "label": "Lane", "choices": ["green", "red"]},
                }
            )
        },
        "view": {
            "blocks": (
                blocks
                if blocks is not None
                else [
                    {"id": "prs", "type": "stat", "fields": ["open_prs"], "title": "Open PRs"},
                    {"id": "runs", "type": "table", "fields": ["last_run", "lane"]},
                ]
            )
        },
        "theme": {"tokens": tokens if tokens is not None else {"--panel-bg": "oklch(21% 0 0)"}},
    }
    if css is not None:
        out["theme"]["css"] = css
    return out


def content(**kwargs: Any) -> str:
    return json.dumps(package(**kwargs))


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(root=tmp_path / "artifacts")


@pytest.fixture
def saved(store: ArtifactStore):
    return store.create(name="Mate dashboard", kind="dashboard", content=content())


class TestSaveAndRead:
    def test_the_kind_exists_and_no_human_can_hand_pick_it(self) -> None:
        # The store is the gate, so the one bypass that would matter is the
        # browser's type control writing the kind onto a prose document.
        assert "dashboard" in ALLOWED_KINDS
        assert "dashboard" not in USER_SELECTABLE_KINDS

    def test_a_saved_package_reads_back_as_a_package(self, store, saved) -> None:
        assert saved.kind == "dashboard"
        assert saved.version == 1
        read = store.get(saved.slug)
        parsed = dp.parse_package(read.content or "")
        assert parsed["bound_to"] == BOUND
        assert sorted(parsed["model"]["types"]) == ["lane", "last_run", "open_prs"]
        assert [b["id"] for b in parsed["view"]["blocks"]] == ["prs", "runs"]

    def test_the_stored_bytes_are_canonical_not_the_authors(self, store) -> None:
        # Same package, keys shuffled and whitespace different: the stored form
        # has to be one thing or the layout comparison reads a change that the
        # author's formatter made.
        shuffled = json.dumps(package(), sort_keys=True, indent=4)
        art = store.create(name="Shuffled", kind="dashboard", content=shuffled)
        a = store.get(art.slug).content
        b = dp.canonical_package_content(content())
        assert a == b

    def test_read_dashboard_model_finds_the_package_by_its_binding(self, store, saved) -> None:
        model = dp.read_dashboard_model(BOUND, store=store)
        assert model is not None
        assert model.slug == saved.slug
        assert model.version == 1
        assert model.bound_to == BOUND
        assert model.declares("open_prs")
        assert not model.declares("unclaimed_value")
        assert model.fields["lane"] == {
            "type": "enum",
            "label": "Lane",
            "choices": ["green", "red"],
        }
        assert model.subscriptions == {"prs": ("open_prs",), "runs": ("last_run", "lane")}
        assert model.subscribers("lane") == ("runs",)
        assert model.subscribers("open_prs") == ("prs",)

    def test_nothing_bound_is_the_empty_state_not_an_error(self, store, saved) -> None:
        assert dp.read_dashboard_model("crewmate:someone-else", store=store) is None

    def test_an_unparseable_sibling_does_not_hide_a_good_package(self, store, saved) -> None:
        # One corrupt package must not make every other page unreadable.
        broken = store.create(name="Broken", kind="json", content="{not a package")
        broken.kind = "dashboard"
        store._write_meta(broken)
        model = dp.read_dashboard_model(BOUND, store=store)
        assert model is not None and model.slug == saved.slug

    def test_a_binding_that_is_not_a_binding_raises_rather_than_matching_nothing(
        self, store
    ) -> None:
        with pytest.raises(ArtifactValidationError, match="is not a binding"):
            dp.read_dashboard_model("mate", store=store)

    def test_a_dashboard_cannot_be_a_live_file_pointer(self, store, tmp_path) -> None:
        linked = tmp_path / "layout.json"
        linked.write_text(content(), encoding="utf-8")
        with pytest.raises(ArtifactValidationError, match="store-owned"):
            store.create(
                name="Linked",
                kind="dashboard",
                content=content(),
                source_path=str(linked),
                source_root=str(tmp_path),
            )


class TestVersionOnlyOnLayoutChange:
    def test_rewriting_the_same_layout_creates_no_version(self, store, saved) -> None:
        art = store.update(saved.slug, content=content(), snapshot=True)
        assert art.version == 1
        assert store.list_versions(saved.slug) == [1]
        assert [e["type"] for e in art.events] == ["created"]

    def test_a_theme_change_creates_a_version_even_unasked(self, store, saved) -> None:
        # The browser PATCH path defaults snapshot to False; a real layout
        # change must still leave something to revert to.
        art = store.update(
            saved.slug,
            content=content(tokens={"--panel-bg": "oklch(98% 0 0)"}),
            snapshot=False,
        )
        assert art.version == 2
        assert store.list_versions(saved.slug) == [1, 2]

    def test_a_model_change_creates_a_version(self, store, saved) -> None:
        types = {
            "open_prs": {"type": "number", "label": "Open PRs", "unit": "PRs"},
            "last_run": {"type": "timestamp", "label": "Last run"},
            "lane": {"type": "enum", "label": "Lane", "choices": ["green", "red"]},
        }
        art = store.update(saved.slug, content=content(types=types))
        assert art.version == 2

    def test_reordering_blocks_is_a_layout_change(self, store, saved) -> None:
        flipped = [
            {"id": "runs", "type": "table", "fields": ["last_run", "lane"]},
            {"id": "prs", "type": "stat", "fields": ["open_prs"], "title": "Open PRs"},
        ]
        art = store.update(saved.slug, content=content(blocks=flipped))
        assert art.version == 2

    def test_a_rebind_alone_creates_no_version_but_is_stored(self, store, saved) -> None:
        # bound_to says WHERE the page hangs, not what it looks like, and a
        # version exists so someone can go back to a LAYOUT.
        art = store.update(saved.slug, content=content(bound_to="session:chat-7-1791007589"))
        assert art.version == 1
        assert dp.read_dashboard_model(BOUND, store=store) is None
        moved = dp.read_dashboard_model("session:chat-7-1791007589", store=store)
        assert moved is not None and moved.slug == saved.slug

    def test_a_metadata_only_update_creates_no_version(self, store, saved) -> None:
        art = store.update(saved.slug, description="the manager page", snapshot=True)
        assert art.version == 1
        assert store.list_versions(saved.slug) == [1]

    def test_switching_to_the_kind_without_a_package_is_refused(self, store) -> None:
        plain = store.create(name="Prose", kind="markdown", content="# notes")
        with pytest.raises(ArtifactValidationError, match="needs the package content"):
            store.update(plain.slug, kind="dashboard")


class TestRevertRestoresLayoutOnly:
    def test_revert_restores_the_layout_and_keeps_the_live_binding(self, store, saved) -> None:
        v1_tokens = {"--panel-bg": "oklch(21% 0 0)"}
        v2 = store.update(
            saved.slug,
            content=content(bound_to="session:chat-9-1791", tokens={"--panel-bg": "red"}),
        )
        assert v2.version == 2
        live_binding = dp.parse_package(store.get(saved.slug).content or "")["bound_to"]
        assert live_binding == "session:chat-9-1791"

        # The revert flow as the handler drives it: read the target version,
        # PATCH its content back with event_type='reverted'.
        target = store.get(saved.slug, version=1)
        art = store.update(
            saved.slug,
            content=target.content,
            event_type="reverted",
            from_version=1,
            snapshot=True,
        )
        restored = dp.parse_package(store.get(saved.slug).content or "")
        assert restored["theme"]["tokens"] == v1_tokens  # layout came back
        assert restored["bound_to"] == "session:chat-9-1791"  # binding did not
        assert art.version == 3
        assert art.events[-1]["type"] == "reverted"
        assert art.events[-1]["from_version"] == 1

    def test_reverting_to_an_identical_layout_creates_no_version(self, store, saved) -> None:
        store.update(saved.slug, content=content(tokens={"--panel-bg": "red"}))
        store.update(saved.slug, content=content())  # back to v1's layout by hand
        before = store.get(saved.slug).version
        target = store.get(saved.slug, version=1)
        art = store.update(
            saved.slug, content=target.content, event_type="reverted", from_version=1, snapshot=True
        )
        assert art.version == before


class TestRejectInvalid:
    @pytest.mark.parametrize(
        "mutate,expected",
        [
            pytest.param(
                lambda p: p["model"]["types"].update({"x": {"type": "sparkline"}}),
                "unknown data type 'sparkline'",
                id="unknown-data-type",
            ),
            pytest.param(
                lambda p: p["model"]["types"]["open_prs"].update({"precisionn": 2}),
                "unknown key(s) ['precisionn']",
                id="unknown-field-key",
            ),
            pytest.param(
                lambda p: p["model"]["types"].update({"lane2": {"type": "enum"}}),
                "is required by this type",
                id="missing-required-field-key",
            ),
            pytest.param(
                lambda p: p["view"]["blocks"][0].update({"type": "heatmap"}),
                "unknown block type 'heatmap'",
                id="unknown-block-type",
            ),
            pytest.param(
                lambda p: p["view"]["blocks"][0].update({"fields": ["not_declared"]}),
                "which model.types does not declare",
                id="block-names-an-undeclared-field",
            ),
            pytest.param(
                lambda p: p["view"]["blocks"][0].update({"fields": ["open_prs", "lane"]}),
                "reads between 1 and 1 fields",
                id="block-reads-too-many-fields",
            ),
            pytest.param(
                lambda p: p["view"]["blocks"].append(
                    {"id": "prs", "type": "stat", "fields": ["open_prs"]}
                ),
                "is used by an earlier block",
                id="duplicate-block-id",
            ),
            pytest.param(
                lambda p: p.update({"data": {"open_prs": 4}}),
                "values stay in the crew log",
                id="data-at-the-top-level",
            ),
            pytest.param(
                lambda p: p["model"]["types"]["open_prs"].update({"value": 4}),
                "values stay in the crew log",
                id="a-value-on-a-field",
            ),
            pytest.param(
                lambda p: p["view"]["blocks"][0].update({"rows": [1, 2]}),
                "values stay in the crew log",
                id="rows-on-a-block",
            ),
            pytest.param(
                lambda p: p.update({"bound_to": "mate"}),
                "is not a binding",
                id="binding-without-a-scope",
            ),
            pytest.param(
                lambda p: p.update({"bound_to": "team:everyone"}),
                "is not a binding",
                id="unknown-binding-scope",
            ),
            pytest.param(
                lambda p: p.update({"kind": "widget"}),
                "must be 'dashboard'",
                id="wrong-kind-inside-the-package",
            ),
            pytest.param(
                lambda p: p.pop("theme"),
                "theme: is required",
                id="no-theme",
            ),
            pytest.param(
                lambda p: p["theme"]["tokens"].update({"panel-bg": "red"}),
                "is not a theme token",
                id="token-is-not-a-custom-property",
            ),
            pytest.param(
                lambda p: p["theme"]["tokens"].update({"--panel-bg": "red; z-index:9"}),
                "no ';'",
                id="token-closes-its-own-declaration",
            ),
            pytest.param(
                lambda p: p["theme"].update({"css": "@import url(http://x/y.css);"}),
                "the page iframe has no network",
                id="css-fetches",
            ),
            pytest.param(
                lambda p: p["model"].update({"types": {}}),
                "must declare at least one field",
                id="empty-model",
            ),
            pytest.param(
                lambda p: p["view"].update({"blocks": []}),
                "must place at least one block",
                id="empty-view",
            ),
            pytest.param(
                lambda p: p["model"]["types"].update({"Open PRs": {"type": "number"}}),
                "is not a field name",
                id="field-name-is-prose",
            ),
        ],
    )
    def test_create_refuses_it(self, store, mutate, expected) -> None:
        p = package()
        mutate(p)
        with pytest.raises(ArtifactValidationError) as exc:
            store.create(name="Bad", kind="dashboard", content=json.dumps(p))
        assert expected in str(exc.value)

    @pytest.mark.parametrize(
        "mutate,expected",
        [
            pytest.param(
                lambda p: p["model"]["types"].update({"x": {"type": "sparkline"}}),
                "unknown data type 'sparkline'",
                id="unknown-data-type",
            ),
            pytest.param(
                lambda p: p.update({"data": {"open_prs": 4}}),
                "values stay in the crew log",
                id="data-at-the-top-level",
            ),
            pytest.param(
                lambda p: p.update({"bound_to": "mate"}),
                "is not a binding",
                id="binding-without-a-scope",
            ),
        ],
    )
    def test_plain_artifact_update_refuses_it_too(self, store, saved, mutate, expected) -> None:
        # The MCP tool and the browser PATCH handler both call store.update, so
        # this is the gate neither of them can walk around.
        p = package()
        mutate(p)
        with pytest.raises(ArtifactValidationError) as exc:
            store.update(saved.slug, content=json.dumps(p))
        assert expected in str(exc.value)
        # Refused means nothing was written.
        assert dp.parse_package(store.get(saved.slug).content or "") == dp.validate_package(
            package()
        )
        assert store.get(saved.slug).version == 1

    def test_content_that_is_not_json_is_refused(self, store) -> None:
        with pytest.raises(ArtifactValidationError, match="is not valid JSON"):
            store.create(name="Bad", kind="dashboard", content="# a prose document")

    def test_a_package_the_size_of_data_is_refused(self, store) -> None:
        p = package()
        p["theme"]["css"] = "/*" + "x" * (dp.MAX_THEME_CSS_BYTES + 1) + "*/"
        with pytest.raises(ArtifactValidationError, match="bytes of UTF-8"):
            store.create(name="Huge", kind="dashboard", content=json.dumps(p))

    def test_a_json_list_is_not_a_package(self, store) -> None:
        with pytest.raises(ArtifactValidationError, match="must be an object"):
            store.create(name="Bad", kind="dashboard", content="[]")


class TestSchemaMatchesTheValidator:
    def test_the_schema_enums_are_the_catalogs(self) -> None:
        # The schema is a document for consumers; the catalogs are what the
        # validator reads. A type added to one has to appear in the other, so
        # the schema is built from them rather than written out beside them.
        schema = dp.package_json_schema()
        field_schema = schema["properties"]["model"]["properties"]["types"]["additionalProperties"]
        assert field_schema["properties"]["type"]["enum"] == sorted(dp.data_type_catalog())
        block_schema = schema["properties"]["view"]["properties"]["blocks"]["items"]
        assert block_schema["properties"]["type"]["enum"] == sorted(dp.view_block_catalog())

    def test_the_schema_requires_what_the_validator_requires(self) -> None:
        schema = dp.package_json_schema()
        assert schema["required"] == ["kind", "bound_to", "model", "view", "theme"]
        assert schema["additionalProperties"] is False
        canonical = dp.validate_package(package())
        assert sorted(canonical) == sorted(schema["required"])

    def test_a_valid_package_satisfies_the_published_schema(self) -> None:
        jsonschema = pytest.importorskip("jsonschema")
        jsonschema.validate(dp.validate_package(package()), dp.package_json_schema())

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
from kiro_crew.artifact_store.records import ALLOWED_EVENT_TYPES
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
                    "open_prs": {
                        "type": "number",
                        "label": "Open PRs",
                        "source": {"fold": "work", "path": "summary.open_prs"},
                    },
                    "last_run": {
                        "type": "timestamp",
                        "label": "Last run",
                        "source": {"fold": "status", "path": "last_run"},
                    },
                    "lane": {
                        "type": "enum",
                        "label": "Lane",
                        "choices": ["green", "red"],
                        "source": {"agentic": True},
                    },
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

    def test_the_binding_reads_back_off_the_stored_package(self, store, saved) -> None:
        # The binding is part of the package, so it survives the write without
        # any lookup helper: a reader keyed by binding scans these values.
        parsed = dp.parse_package(store.get(saved.slug).content or "")
        assert parsed["bound_to"] == BOUND


class TestWhereAValueComesFrom:
    """A field says who fills it, so a translation never has to guess."""

    def test_a_fold_field_and_an_agentic_one_keep_their_declared_source(self, store, saved) -> None:
        # ``source`` is what decides whether an agent may write a field, so the
        # claim this package has to carry is that both shapes survive the write
        # intact and distinguishable. Which fields a page then exposes to an
        # agent is the reader's question, in the PR that adds the reader.
        types = dp.parse_package(store.get(saved.slug).content or "")["model"]["types"]
        assert types["lane"]["source"] == {"agentic": True}
        assert types["open_prs"]["source"] == {"fold": "work", "path": "summary.open_prs"}
        assert types["last_run"]["source"] == {"fold": "status", "path": "last_run"}

    def test_the_fold_names_come_from_the_manifest_not_a_copy(self) -> None:
        from kiro_crew.dashboard_templates.manifest import FOLD_NAMES

        assert dp.fold_names() == frozenset(FOLD_NAMES)

    @pytest.mark.parametrize(
        "name",
        ["open_prs", "a", "x9", "Open_PRs", "9lives", "open-prs", "", "a" * 65, "open prs"],
    )
    def test_a_name_this_package_accepts_a_manifest_field_accepts(self, name) -> None:
        # The translation keeps the package's own field names, so the two
        # grammars have to agree on every name, not just on the good ones.
        from kiro_crew.dashboard_templates.manifest import _FIELD

        mine = dp._FIELD_NAME_RE.fullmatch(name) is not None
        theirs = _FIELD.match(name) is not None
        assert mine == theirs, name

    def test_a_binding_that_is_not_a_binding_is_refused(self) -> None:
        with pytest.raises(ArtifactValidationError, match="is not a binding"):
            dp.validate_bound_to("mate")

    def test_a_suppressed_snapshot_still_hands_back_the_live_package(self, store, saved) -> None:
        # A dashboard never versions on a content-free write, so the explicit
        # Snapshot path is disarmed -- and THAT path is also where a
        # snapshot-only update loads the live bytes off disk. A caller that gets
        # a meta-only record back reads `content` as empty, and an auto-sync
        # publisher uploads that emptiness over a live dashboard and calls the
        # push a success. So the bytes come back even though the version does
        # not move.
        live = store.get(saved.slug).content
        assert live, "fixture precondition: the saved package has content"

        art = store.update(saved.slug, snapshot=True)

        assert art.version == 1, "a content-free dashboard write must not version"
        assert art.content == live, "a suppressed snapshot must still carry the live package"
        assert dp.parse_package(art.content or "")["bound_to"] == BOUND
        # And nothing was written over the stored copy either.
        assert store.get(saved.slug).content == live

    def test_a_dashboard_cannot_be_relocated_onto_a_file(self, store, saved, tmp_path) -> None:
        # The third door into "a dashboard with a live file pointer". create
        # refuses it and a kind switch refuses it, so relocate has to as well:
        # a pointer makes every later get re-read the package from outside the
        # store, past the write gate, and a revert then writes the binding it
        # recovered into that outside file -- handing the page to whoever the
        # stale copy named.
        elsewhere = tmp_path / "stolen.json"
        elsewhere.write_text(content(bound_to="crewmate:someone-else"), encoding="utf-8")
        with pytest.raises(ArtifactValidationError, match="store-owned"):
            store.relocate(saved.slug, str(elsewhere))
        # Refused means the record still owns its bytes and its binding.
        after = store.get(saved.slug)
        assert not after.source_path
        assert dp.parse_package(after.content or "")["bound_to"] == BOUND

    def test_switching_into_the_kind_cannot_keep_a_live_pointer(self, store, tmp_path) -> None:
        linked = tmp_path / "doc.json"
        linked.write_text("{}", encoding="utf-8")
        art = store.create(
            name="Linked doc",
            kind="json",
            content="{}",
            source_path=str(linked),
            source_root=str(tmp_path),
        )
        assert art.source_path, "fixture precondition: it really is a live pointer"
        with pytest.raises(ArtifactValidationError, match="store-owned"):
            store.update(art.slug, kind="dashboard", content=content())

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
        types = package()["model"]["types"]
        types["open_prs"]["unit"] = "PRs"
        art = store.update(saved.slug, content=content(types=types))
        assert art.version == 2

    def test_moving_a_field_from_a_fold_to_the_agent_creates_a_version(self, store, saved) -> None:
        # Who fills a field is part of the model, so changing it is a layout
        # change -- and it is the change that decides whether an agentic write
        # may land, so it needs history.
        types = package()["model"]["types"]
        types["open_prs"]["source"] = {"agentic": True}
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
        # Stored without a version, so the new binding is what a reader finds.
        parsed = dp.parse_package(store.get(saved.slug).content or "")
        assert parsed["bound_to"] == "session:chat-7-1791007589"

    def test_a_metadata_only_update_creates_no_version(self, store, saved) -> None:
        art = store.update(saved.slug, description="the manager page", snapshot=True)
        assert art.version == 1
        assert store.list_versions(saved.slug) == [1]

    def test_switching_to_the_kind_without_a_package_is_refused(self, store) -> None:
        plain = store.create(name="Prose", kind="markdown", content="# notes")
        with pytest.raises(ArtifactValidationError, match="needs the package content"):
            store.update(plain.slug, kind="dashboard")


class TestARefusedWriteChangesNothing:
    """A write this store refuses must leave the artifact exactly as it was.

    The dashboard branch is where that is easiest to break: a layout change FORCES the
    snapshot on, and the snapshot path rotates the content token, replaces
    ``current.html`` and rescans the comment anchors. A refusal raised after any of
    those three answers 400 for a write that has already landed -- and landed with no
    version entry naming it, so nothing in the history says where those bytes came
    from. These cases pin that every refusal happens ahead of all three.
    """

    def test_a_bogus_event_type_leaves_the_stored_package_untouched(self, store, saved) -> None:
        before = store.get(saved.slug)
        changed = content(tokens={"--panel-bg": "oklch(98% 0 0)"})
        assert changed != before.content, "the fixture did not change the layout"

        with pytest.raises(ArtifactValidationError, match="invalid event type"):
            store.update(saved.slug, content=changed, event_type="bogus")

        after = store.get(saved.slug)
        assert after.content == before.content, "the refused content was persisted anyway"
        assert after.version == before.version
        assert after.content_token == before.content_token, "the token rotated for a refused write"
        assert store.list_versions(saved.slug) == [1]
        assert [e["type"] for e in after.events] == ["created"]

    def test_the_refusal_does_not_depend_on_whether_the_write_would_version(
        self, store, saved
    ) -> None:
        """Same layout, so no version -- and the bad event type is still refused.

        A check living INSIDE the version block would be skipped entirely by a write
        that happens not to version, which silently accepts an event type that is not
        real. The refusal must not depend on that.
        """
        with pytest.raises(ArtifactValidationError, match="invalid event type"):
            store.update(saved.slug, content=content(), event_type="bogus")
        assert store.get(saved.slug).version == 1

    def test_a_metadata_only_write_with_a_bogus_event_type_is_refused(self, store, saved) -> None:
        """No content at all, so nothing would be written -- still refused.

        Named rather than inferred: the caller is told its event type is not real, which
        is the point of the tightening. Silently ignoring it left an API caller believing
        the store had recorded an event it has no name for.
        """
        with pytest.raises(ArtifactValidationError, match="invalid event type"):
            store.update(saved.slug, name="Renamed", event_type="bogus")
        assert store.get(saved.slug).name == saved.name

    @pytest.mark.parametrize("allowed", ["edited", "iterated", "reverted"])
    def test_the_event_types_the_app_sends_still_pass(self, store, saved, allowed: str) -> None:
        """The positive control: the refusal is about the VALUE, not about the check.

        These three are what the app's own writers send -- the website client types the
        field to exactly them, and the MCP tool sends ``reverted`` -- so they are the
        set that must keep working. They are all in ``ALLOWED_EVENT_TYPES``.
        """
        assert allowed in ALLOWED_EVENT_TYPES
        art = store.update(saved.slug, name=f"n-{allowed}", event_type=allowed)
        assert art.name == f"n-{allowed}"


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
                lambda p: p["model"]["types"].update(
                    {"lane2": {"type": "enum", "source": {"agentic": True}}}
                ),
                "is required by this type",
                id="missing-required-field-key",
            ),
            pytest.param(
                lambda p: p["model"]["types"]["open_prs"].pop("source"),
                "a field must say where its value comes from",
                id="no-source",
            ),
            pytest.param(
                lambda p: p["model"]["types"]["open_prs"].update(
                    {"source": {"fold": "invented", "path": "a"}}
                ),
                "is not a crew-log fold",
                id="unknown-fold",
            ),
            pytest.param(
                lambda p: p["model"]["types"]["open_prs"].update(
                    {"source": {"fold": "work", "path": "a..b"}}
                ),
                "must be dotted keys",
                id="fold-path-is-not-a-path",
            ),
            pytest.param(
                lambda p: p["model"]["types"]["open_prs"].update({"source": {"fold": "work"}}),
                "must be dotted keys",
                id="fold-source-without-a-path",
            ),
            pytest.param(
                lambda p: p["model"]["types"]["lane"].update(
                    {"source": {"agentic": True, "fold": "work"}}
                ),
                "unknown key(s) ['fold']",
                id="agentic-source-also-names-a-fold",
            ),
            pytest.param(
                lambda p: p["model"]["types"]["lane"].update({"source": {"agentic": False}}),
                "an agentic source is written exactly",
                id="agentic-false-is-not-a-source",
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

    def test_the_byte_cap_applies_to_the_stored_form_not_the_submitted_text(self, store) -> None:
        # Indentation is added by the store, so a layout can arrive under the
        # cap and be written over it. The caps that matter are the ones measured
        # on the bytes a write actually puts on disk, because an oversized
        # package is one this module's own reader refuses: the page would come
        # back blank with nothing refused at write time.
        types = {
            f"f{i:05d}": {
                "type": "enum",
                "label": "L" * dp.MAX_LABEL_LEN,
                "description": "D" * dp.MAX_LABEL_LEN,
                "choices": [f"c{j:03d}" + "z" * (dp.MAX_LABEL_LEN - 4) for j in range(30)],
                "source": {"agentic": True},
            }
            for i in range(dp.MAX_MODEL_FIELDS)
        }
        blocks = [
            {"id": f"b{i:05d}", "type": "table", "fields": [f"f{i:05d}"]}
            for i in range(dp.MAX_VIEW_BLOCKS)
        ]
        submitted = json.dumps(package(types=types, blocks=blocks), separators=(",", ":"))
        # Every per-part cap is satisfied and the submitted text is under the
        # package cap, so nothing upstream of the serializer can refuse this.
        assert len(submitted.encode("utf-8")) <= dp.MAX_PACKAGE_BYTES
        with pytest.raises(ArtifactValidationError, match="bytes once stored"):
            store.create(name="Indented over", kind="dashboard", content=submitted)
        # The revert path ends in the same serializer, so it refuses it too.
        with pytest.raises(ArtifactValidationError, match="bytes once stored"):
            dp.revert_package(submitted, submitted)

    @pytest.mark.parametrize(
        "mutate,where",
        [
            pytest.param(
                lambda p: p["model"]["types"]["open_prs"].__setitem__("label", "bad \ud800 x"),
                "package.model.types.open_prs.label",
                id="a-label",
            ),
            pytest.param(
                lambda p: p["theme"].__setitem__("css", ".x{color:red} /* \ud800 */"),
                "package.theme.css",
                id="theme-css",
            ),
            pytest.param(
                lambda p: p["model"]["types"]["lane"]["choices"].append("gre\ud800en"),
                "package.model.types.lane.choices[2]",
                id="a-nested-value",
            ),
            pytest.param(
                lambda p: p["model"]["types"].__setitem__(
                    "ba\ud800d", p["model"]["types"]["open_prs"]
                ),
                "key",
                id="a-key",
            ),
        ],
    )
    def test_an_unpaired_surrogate_is_refused_wherever_it_sits(self, store, mutate, where) -> None:
        # "\ud800" is a well-formed JSON escape that survives decoding as an
        # unpaired code point, and UTF-8 cannot encode it. Every refusal has to
        # be an ArtifactValidationError, which callers answer with a 400: a
        # UnicodeEncodeError reaching a byte-cap check or the file write is a
        # 500 instead. Parameterised across a label, theme.css, a nested list
        # entry and a KEY, because the invariant is "no string anywhere", and a
        # per-site guard only ever covers the site it was written for.
        p = package()
        mutate(p)
        submitted = json.dumps(p)
        assert "\\ud800" in submitted
        with pytest.raises(ArtifactValidationError) as exc:
            store.create(name="Surrogate", kind="dashboard", content=submitted)
        assert "unpaired surrogate" in str(exc.value)
        # The path names where it sits, so a fix does not need a bisect.
        assert where in str(exc.value)
        # The revert path parses both sides, so it refuses it too.
        with pytest.raises(ArtifactValidationError, match="unpaired surrogate"):
            dp.revert_package(submitted, submitted)

    def test_a_surrogate_written_literally_is_refused_before_the_size_check(self) -> None:
        # Measuring the submitted text encodes it, so the scan has to come
        # first: a caller inside the process can hand over a str that already
        # holds the code point, with no escape for the decoder to expand.
        with pytest.raises(ArtifactValidationError, match="unpaired surrogate"):
            dp.parse_package('{"kind": "dashboard", "bound_to": "crewmate:m\ud800"}')

    def test_a_package_too_deep_to_decode_is_refused_not_a_crash(self, store) -> None:
        # The byte cap bounds size, not depth. This payload is well under the
        # cap and deep enough to exhaust the JSON decoder's stack, and the
        # refusal has to be an ArtifactValidationError: every caller answers
        # that with a 400, while a RecursionError reaching them is a 500.
        deep = "[" * (dp.MAX_PACKAGE_BYTES - 1)
        assert len(deep.encode("utf-8")) < dp.MAX_PACKAGE_BYTES
        with pytest.raises(ArtifactValidationError, match="nests deeper"):
            store.create(name="Deep", kind="dashboard", content=deep)

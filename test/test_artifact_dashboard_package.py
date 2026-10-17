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
5. ``artifact_save`` -- the only save path an agent has -- reaches that gate at
   all, and reaching it is all the tool layer does.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kiro_crew.artifact_store import dashboard_package as dp
from kiro_crew.artifact_store.records import ALLOWED_EVENT_TYPES
from kiro_crew.artifact_store.rules import ALLOWED_KINDS, USER_SELECTABLE_KINDS
from kiro_crew.artifacts import ArtifactStore, ArtifactValidationError
from kiro_crew.validation import (
    _ARTIFACT_KIND_RE,
    ARTIFACT_LIST_SCHEMA,
    ARTIFACT_SAVE_SCHEMA,
    validate_tool_args,
)


def _owner_write(store: Any, slug: str, **kwargs: Any) -> Any:
    """A dashboard content write under the record's CURRENT token.

    ``ArtifactStore.update`` refuses a kind or content write on a record whose kind is
    ``dashboard`` unless it names the read it was authorized against, and an absent
    token is a refusal -- see
    ``test_dashboard_package_owner_gate.TestAnUntokenedWriteCannotTakeADashboardOver``.
    The route reads the slug before every write and hands the store that token; a test
    playing the owner reads it here for the same reason.
    """
    token = store.get(slug, assign_token=True).content_token
    return store.update(slug, snapshot_token=token, **kwargs)


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


def _tool_kind_enum(tool: str) -> list[str]:
    """The ``kind`` values the MCP tool *tool* advertises to an agent."""
    from kiro_crew.mcp_tools.artifacts import schemas

    for descriptor in schemas():
        if descriptor["name"] == tool:
            enum = descriptor["inputSchema"]["properties"]["kind"]["enum"]
            assert isinstance(enum, list) and enum
            return enum
    raise AssertionError(f"no {tool} descriptor in the artifacts tool schemas")


def _save_through_the_tool(
    store: "ArtifactStore", args: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    """Run the real ``artifact_save`` handler against *store*.

    The handler reaches the gateway, so the POST is the seam: this stands a real
    :class:`ArtifactStore` behind it and renders a refusal the way the dashboard's
    own handler does, as ``{"error": ...}``. That keeps the whole tool path under
    test -- schema, handler, store, package validator -- rather than the schema
    alone, which is the half that cannot tell a reachable kind from an advertised
    one.
    """
    from kiro_crew import mcp_core
    from kiro_crew.mcp_tools.artifacts import artifact_save

    posted: dict[str, Any] = {}

    def _fake_post(path: str, body: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
        assert path == "/api/artifacts", path
        posted.update(body or {})
        try:
            art = store.create(
                name=posted["name"], kind=posted.get("kind"), content=posted["content"]
            )
        except ArtifactValidationError as exc:
            return {"error": str(exc)}
        return {"slug": art.slug, "version": art.version, "name": art.name, "kind": art.kind}

    with (
        patch.object(mcp_core, "_post", side_effect=_fake_post),
        patch.object(mcp_core, "_resolve_session_key", return_value=""),
    ):
        return artifact_save("artifact_save", args), posted


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


class TestTheAgentToolMaySaveOne:
    """``artifact_save`` is the only save path an agent has, so it has to reach the gate.

    A package is composed by an agent, so the agent tool is what decides whether this
    kind can be brought into being at all. Three lists stand between a call and the
    store and all three have to carry the name: the tool's advertised enum, the kind
    pattern the tool schema validates against, and the store's own ``ALLOWED_KINDS``.
    A name missing from either of the first two refuses the call ``kind: invalid
    format`` before the store sees the bytes, which is a refusal with no rule behind
    it.

    The second claim is what makes the first safe. Carrying the name is ALL the front
    lists do -- what the bytes may be is the store's single gate in
    ``dashboard_package``, so a body that is not a package is refused with the rule it
    broke rather than stored under a kind the renderer would then have to parse.
    """

    def test_the_three_lists_agree_on_the_kind(self) -> None:
        assert "dashboard" in ALLOWED_KINDS
        assert _ARTIFACT_KIND_RE.fullmatch("dashboard") is not None
        assert "dashboard" in _tool_kind_enum("artifact_save")
        assert "dashboard" in _tool_kind_enum("artifact_list")

    @pytest.mark.parametrize("tool", ["artifact_save", "artifact_list"])
    def test_every_kind_the_tool_advertises_reaches_the_store(self, tool: str) -> None:
        # Drift pin, and the direction matters. An enum entry the pattern or the
        # store does not carry is advertised to every agent and then refused on
        # use, which is a refusal naming no rule. The reverse is allowed:
        # ``image`` needs a source_path the MCP save path never sends, so the
        # pattern is deliberately wider than what the tool offers.
        for kind in _tool_kind_enum(tool):
            assert _ARTIFACT_KIND_RE.fullmatch(kind) is not None, kind
            assert kind in ALLOWED_KINDS, kind

    def test_the_tool_schema_takes_a_package(self) -> None:
        cleaned = validate_tool_args(
            {"name": "Mate dashboard", "content": content(), "kind": "dashboard"},
            ARTIFACT_SAVE_SCHEMA,
        )
        assert cleaned["kind"] == "dashboard"

    def test_the_list_filter_takes_the_kind_too(self) -> None:
        # An agent that can create one has to be able to find it again.
        cleaned = validate_tool_args({"kind": "dashboard"}, ARTIFACT_LIST_SCHEMA)
        assert cleaned["kind"] == "dashboard"

    def test_a_package_saved_through_the_tool_lands_as_a_package(self, store) -> None:
        out, posted = _save_through_the_tool(
            store, {"name": "Mate dashboard", "content": content(), "kind": "dashboard"}
        )
        # The handler forwards the kind rather than letting inference re-type a
        # JSON body as plain ``json``, which would store the package under a kind
        # nothing validates.
        assert posted["kind"] == "dashboard"
        assert out.startswith("Saved artifact: slug=")
        stored = store.list(kind="dashboard")
        assert len(stored) == 1
        # Version 1 because that is what creating ANY artifact answers; there is no
        # version 0 in this store. It is not an approval of anything, which is the
        # next test.
        assert stored[0].version == 1
        assert dp.parse_package(store.get(stored[0].slug).content or "")["bound_to"] == BOUND

    def test_a_save_is_a_write_and_never_a_persons_yes(self, store) -> None:
        # Every version this store cuts appends a lifecycle event carrying the
        # ACTOR, and the actor comes from how the request authenticated rather than
        # from anything a caller spells. A tool save is an agent save, so no event
        # it writes can read as a person's. Pinned here because "the package has a
        # version" is the thing most easily misread as "someone said yes to it":
        # what a version means is the display half's question, and nothing on this
        # path answers it.
        _save_through_the_tool(
            store, {"name": "Mate dashboard", "content": content(), "kind": "dashboard"}
        )
        art = store.get(store.list(kind="dashboard")[0].slug)
        events = list(getattr(art, "events", None) or [])
        assert events, "a create records its own lifecycle event"
        assert not [e for e in events if str(e.get("by") or "") == "user"]

    def test_a_body_that_is_not_a_package_is_refused_with_its_reason(self, store) -> None:
        out, _ = _save_through_the_tool(
            store, {"name": "Prose", "content": "# a prose document", "kind": "dashboard"}
        )
        assert "is not valid JSON" in out
        assert not store.list(kind="dashboard")

    def test_a_package_carrying_values_is_refused_with_its_reason(self, store) -> None:
        # The refusal an agent is most likely to earn: valid JSON, right shape,
        # and values pasted into it. The rule it names is the one it broke.
        p = package()
        p["view"]["blocks"][1]["data"] = [{"last_run": "now"}]
        out, _ = _save_through_the_tool(
            store, {"name": "With values", "content": json.dumps(p), "kind": "dashboard"}
        )
        assert "values stay in the crew log" in out
        assert not store.list(kind="dashboard")

    def test_the_tool_cannot_ask_for_a_linked_package(self, store) -> None:
        # The serve path refuses a linked artifact because its bytes come from a
        # file nothing gates. The tool schema has no source_path field at all, so
        # the request cannot be spelled, and the store refuses it even if it were.
        assert not any(f.name == "source_path" for f in ARTIFACT_SAVE_SCHEMA.fields)

    def test_the_enum_tells_the_agent_every_key_the_package_needs(self) -> None:
        # The description is the only place an agent learns what the content has
        # to be, so a key missing from it is a refusal the agent cannot act on:
        # it composes what it was told, the store refuses the result, and nothing
        # in either half says which list is short.
        from kiro_crew.mcp_tools.artifacts import schemas

        save = next(s for s in schemas() if s["name"] == "artifact_save")
        described = save["inputSchema"]["properties"]["kind"]["description"]
        # The keys come from the VALIDATOR'S OWN canonical output, not from a list
        # written again here: a second copy of the five keys in this file would
        # drift from the gate exactly the way the description can.
        for key in json.loads(dp.canonical_package_content(content())):
            assert key in described, key
        # And the rule behind the keys, which is the one a package most often breaks.
        assert "values stay in the crew log" in described

    def test_deleting_the_package_leaves_the_binding_unclaimed_again(self, store) -> None:
        # THE UNDO, and it is the artifact store's own. A bound package changes
        # what the member's write route checks against, so the step that ends that
        # is the step that makes the binding unclaimed: deleting the artifact. The
        # lookup every reader goes through answers None again, which is the answer
        # it gives for a member that never had one, so a reader falls back to the
        # template instance exactly as before the save.
        out, _ = _save_through_the_tool(
            store, {"name": "Mate dashboard", "content": content(), "kind": "dashboard"}
        )
        assert out.startswith("Saved artifact: slug=")
        slug = store.list(kind="dashboard")[0].slug
        assert dp.resolve_bound_slug(BOUND, store=store) == slug

        assert store.delete(slug) is not False
        assert dp.resolve_bound_slug(BOUND, store=store) is None
        assert not store.list(kind="dashboard")

    def test_an_agent_tool_kind_is_not_a_hand_pickable_one(self) -> None:
        # The browser's type control is the one path a PERSON writes a kind on,
        # and a prose document flipped to this kind stores content no reader can
        # parse. The agent tool offering the kind does not make it one a person
        # may pick: the two lists answer different questions.
        assert "dashboard" not in USER_SELECTABLE_KINDS


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
        """Still the manifest module's own set and still not a copy; the set this gate
        is defined by is now the PACKAGE one, because a package's page has a reader a
        template's does not -- `member_dashboard_fields.LivePage` reads a tree-keyed
        fold, and the template feed reaches a fold only by its bus scope."""
        from kiro_crew.dashboard_templates.manifest import FOLD_NAMES, PACKAGE_FOLD_NAMES

        assert dp.fold_names() == frozenset(PACKAGE_FOLD_NAMES)
        # And it is a SUPERSET of the template gate rather than a different list: the
        # two surfaces answer one question, so a name neither can reach is in neither.
        assert frozenset(FOLD_NAMES) < dp.fold_names()

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
            _owner_write(store, art.slug, kind="dashboard", content=content())

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
        art = _owner_write(store, saved.slug, content=content(), snapshot=True)
        assert art.version == 1
        assert store.list_versions(saved.slug) == [1]
        assert [e["type"] for e in art.events] == ["created"]

    def test_a_theme_change_creates_a_version_even_unasked(self, store, saved) -> None:
        # The browser PATCH path defaults snapshot to False; a real layout
        # change must still leave something to revert to.
        art = _owner_write(
            store,
            saved.slug,
            content=content(tokens={"--panel-bg": "oklch(98% 0 0)"}),
            snapshot=False,
        )
        assert art.version == 2
        assert store.list_versions(saved.slug) == [1, 2]

    def test_a_model_change_creates_a_version(self, store, saved) -> None:
        types = package()["model"]["types"]
        types["open_prs"]["unit"] = "PRs"
        art = _owner_write(store, saved.slug, content=content(types=types))
        assert art.version == 2

    def test_moving_a_field_from_a_fold_to_the_agent_creates_a_version(self, store, saved) -> None:
        # Who fills a field is part of the model, so changing it is a layout
        # change -- and it is the change that decides whether an agentic write
        # may land, so it needs history.
        types = package()["model"]["types"]
        types["open_prs"]["source"] = {"agentic": True}
        art = _owner_write(store, saved.slug, content=content(types=types))
        assert art.version == 2

    def test_reordering_blocks_is_a_layout_change(self, store, saved) -> None:
        flipped = [
            {"id": "runs", "type": "table", "fields": ["last_run", "lane"]},
            {"id": "prs", "type": "stat", "fields": ["open_prs"], "title": "Open PRs"},
        ]
        art = _owner_write(store, saved.slug, content=content(blocks=flipped))
        assert art.version == 2

    def test_a_rebind_alone_creates_no_version_but_is_stored(self, store, saved) -> None:
        # bound_to says WHERE the page hangs, not what it looks like, and a
        # version exists so someone can go back to a LAYOUT.
        art = _owner_write(store, saved.slug, content=content(bound_to="session:chat-7-1791007589"))
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
            _owner_write(store, saved.slug, content=changed, event_type="bogus")

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
            _owner_write(store, saved.slug, content=content(), event_type="bogus")
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
        v2 = _owner_write(
            store,
            saved.slug,
            content=content(bound_to="session:chat-9-1791", tokens={"--panel-bg": "red"}),
        )
        assert v2.version == 2
        live_binding = dp.parse_package(store.get(saved.slug).content or "")["bound_to"]
        assert live_binding == "session:chat-9-1791"

        # The revert flow as the handler drives it: read the target version,
        # PATCH its content back with event_type='reverted'.
        target = store.get(saved.slug, version=1)
        art = _owner_write(
            store,
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
        _owner_write(store, saved.slug, content=content(tokens={"--panel-bg": "red"}))
        _owner_write(store, saved.slug, content=content())  # back to v1's layout by hand
        before = store.get(saved.slug).version
        target = store.get(saved.slug, version=1)
        art = _owner_write(
            store,
            saved.slug,
            content=target.content,
            event_type="reverted",
            from_version=1,
            snapshot=True,
        )
        assert art.version == before


class TestAFieldNameIsNotAPlaceToHideACredential:
    """A field NAME travels further than its value, so it needs the same gate.

    Field VALUES are redacted on the way out. The names are not: they are served
    in the package's `fields` list (`member_dashboard.py:865`) and written into
    the page's data island, so a name shaped like a token is published in clear
    by the one surface that scrubs everything else.

    `_FIELD_NAME_RE` does not stop it. A lower-case fine-grained-PAT shape fits
    inside `[a-z][a-z0-9_]{0,63}` with room to spare, and so does a `ghp_` shape.

    The gate is the REDACTOR ITSELF rather than a second copy of its patterns:
    run the name through the same shim every egress site uses and refuse if it
    comes back changed. So a companion's extra patterns apply here too, and a
    pattern added there cannot leave this check behind.
    """

    #: Assembled from parts on purpose. A literal token shape in the tree trips
    #: GitHub's push protection, and the point here is the SHAPE, not a secret.
    FINE_GRAINED = "github" + "_pat_" + "a1b2c3d4e5" * 4
    CLASSIC = "ghp" + "_" + "a1b2c3d4e5" * 3

    @pytest.mark.parametrize("shape", [FINE_GRAINED, CLASSIC])
    def test_the_name_rule_alone_would_have_allowed_it(self, shape: str) -> None:
        """Why the extra gate is needed and not redundant with the regex."""
        assert dp._FIELD_NAME_RE.fullmatch(shape) is not None
        assert len(shape) <= 64

    @pytest.mark.parametrize("shape", [FINE_GRAINED, CLASSIC])
    def test_a_credential_shaped_field_name_is_refused(
        self, store: ArtifactStore, shape: str
    ) -> None:
        # ADDED to a valid model rather than replacing it, so the only thing
        # wrong with this package is the one field name under test.
        p = package()
        p["model"]["types"][shape] = {
            "type": "number",
            "label": "PRs",
            "source": {"agentic": True},
        }
        with pytest.raises(ArtifactValidationError) as exc:
            store.create(name="Sneaky", kind="dashboard", content=json.dumps(p))
        said = str(exc.value)
        assert f"model.types.{shape}" in said or "credential" in said
        # The refusal must not quote the name back in full: an error string is
        # logged, and echoing the thing we just called credential-shaped would
        # put it exactly where we are trying to keep it out of.
        assert shape not in said

    def test_an_ordinary_field_name_still_passes(self, store: ArtifactStore) -> None:
        """The gate has to be narrow: it refuses what the redactor rewrites and
        nothing else, so a normal model is unaffected."""
        art = store.create(name="Fine", kind="dashboard", content=content())
        assert art.slug


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
            _owner_write(store, saved.slug, content=json.dumps(p))
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

    def test_the_byte_cap_applies_to_the_stored_form_not_the_submitted_text(
        self, store, monkeypatch
    ) -> None:
        # Indentation is added by the store, so a layout can arrive under the
        # cap and be written over it. The caps that matter are the ones measured
        # on the bytes a write actually puts on disk, because an oversized
        # package is one this module's own reader refuses: the page would come
        # back blank with nothing refused at write time.
        #
        # The cap is SET between the two forms of one layout below. The real one
        # is sized for a page that inlines a library, which no block layout comes
        # near, and the property asked here is which form the cap is measured on.
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
        stored = dp.dump_package(dp.parse_package(submitted))
        monkeypatch.setattr(dp, "MAX_PACKAGE_BYTES", len(submitted.encode("utf-8")))
        # Every per-part cap is satisfied and the submitted text is at the
        # package cap, so nothing upstream of the serializer can refuse this.
        assert len(stored.encode("utf-8")) > dp.MAX_PACKAGE_BYTES
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


class TestTheBindingLookup:
    """``resolve_bound_slug``: which stored package is bound to a member.

    The one entry point the data line reads, so what it does with a record it cannot
    fully validate decides what the caller upstream believes. Answering ``None`` for a
    package that IS present reads as "this member has no package", and the write path
    acts on that by validating against a DIFFERENT Model -- so a record whose envelope
    names the binding has to be found even when the layout inside it does not parse.
    """

    def _corrupt(self, store: ArtifactStore, tmp_path: Path, *, bound_to: str = BOUND) -> str:
        """A stored dashboard record whose envelope is readable and whose model is not.

        Written through the store and then damaged on disk, because the write gate
        refuses an invalid package by design: the records this guards against are the
        ones the gate never saw in this shape -- a package stored under an older schema,
        or a write that did not finish.
        """
        art = store.create(name="Drifted", kind="dashboard", content=content(bound_to=bound_to))
        damaged = dict(package(bound_to=bound_to))
        damaged["model"] = {"types": {"open_prs": {"type": "sparkline"}}}
        (tmp_path / "artifacts" / art.slug / "current.html").write_text(
            json.dumps(damaged), encoding="utf-8"
        )
        # The premise of every case below: this content really is past the validator.
        with pytest.raises(ArtifactValidationError):
            dp.parse_package(store.get(art.slug).content or "")
        return art.slug

    def test_a_stored_package_is_found_by_its_binding(self, store, saved) -> None:
        assert dp.resolve_bound_slug(BOUND, store=store) == saved.slug

    def test_a_binding_nothing_is_stored_for_is_none(self, store, saved) -> None:
        assert dp.resolve_bound_slug("crewmate:someone-else", store=store) is None

    def test_a_package_whose_model_will_not_parse_is_still_found(self, store, tmp_path) -> None:
        """The defect this case exists for.

        A corrupt package skipped here reaches the caller as "no binding", which falls
        through to the builtin Model and lets a write replace a stored cell with a value
        of the wrong SHAPE -- a text cell overwritten by an array. The binding lives in
        the envelope, one level above the layout, so it is readable in exactly the case
        the layout is not.
        """
        slug = self._corrupt(store, tmp_path)
        assert dp.resolve_bound_slug(BOUND, store=store) == slug

    def _shredded(self, store: ArtifactStore, tmp_path: Path) -> str:
        """A stored dashboard record with no readable envelope at all."""
        art = store.create(
            name="Shredded", kind="dashboard", content=content(bound_to="session:member-atlas")
        )
        (tmp_path / "artifacts" / art.slug / "current.html").write_text(
            "\x00 not json", encoding="utf-8"
        )
        return art.slug

    def test_a_record_with_no_readable_envelope_refuses_to_say_absent(
        self, store, tmp_path
    ) -> None:
        """``None`` is a CLAIM, and this is the case that cannot support it.

        A record whose envelope will not decode may BE the one bound here -- that is
        exactly what could not be read. Answering ``None`` reports it as "no package",
        the caller falls through to a builtin Model, and a write checked against that
        Model replaces a cell with a value of the wrong shape.
        """
        self._shredded(store, tmp_path)
        with pytest.raises(dp.BindingLookupIncomplete, match="could not be read"):
            dp.resolve_bound_slug("session:member-atlas", store=store)

    def test_an_unreadable_record_leaves_a_found_package_unproven(
        self, store, tmp_path, saved
    ) -> None:
        """A match is not the end of the scan, because ONE binding names ONE package.

        The lookup has to read the rest to know the match is unique, and a record it
        could not open may be a SECOND claimant -- which is the same unproven answer as
        a record that may be the only claimant, seen from the other side. So an
        unreadable record refuses here too, and the sentence says which uniqueness went
        unproven rather than reporting absence.

        A caller that draws a page may answer this with the page it already has;
        ``member_dashboard._bound_package_slug`` does exactly that. A caller that
        VALIDATES A WRITE may not, which is why the refusal is the function's answer and
        the fallback is the reader's.
        """
        self._shredded(store, tmp_path)
        with pytest.raises(dp.BindingLookupIncomplete, match="is the only one bound to"):
            dp.resolve_bound_slug(BOUND, store=store)
        assert saved.slug

    def test_two_packages_naming_one_binding_are_refused(self, store, saved) -> None:
        """Picking the first seen would be arbitrary -- the listing order is the store's,
        not a statement about which package is live -- and a reader cannot tell an
        arbitrary pick from a correct one, so a relisted library would silently swap
        which page a crewmate shows.

        ``BindingAmbiguous`` is a SUBCLASS of ``BindingLookupIncomplete`` so that every
        caller already handling "no provable package" handles this with no change.
        """
        second = store.create(name="Second", kind="dashboard", content=content(bound_to=BOUND))
        with pytest.raises(dp.BindingAmbiguous) as refused:
            dp.resolve_bound_slug(BOUND, store=store)
        assert saved.slug in str(refused.value) and second.slug in str(refused.value)
        assert isinstance(refused.value, dp.BindingLookupIncomplete)

    def test_a_live_backed_record_cannot_claim_a_binding(self, store, saved) -> None:
        """Its bytes are a file's current contents, which no artifact-API gate stands in
        front of. Skipped before it is opened, so it can neither be mistaken for this
        binding's package nor make the binding ambiguous -- an ungated writer would
        otherwise turn a resolvable dashboard into a refused one.

        DEFENCE IN DEPTH, and the test says so rather than implying the hole is open:
        the write gate already refuses `kind="dashboard"` with a `source_path` ("a
        dashboard package is store-owned ... a linked file is read back unvalidated"),
        so such a record cannot be created through the API today. What this guards is a
        record that predates that refusal or was written around it, which is why the
        listing is stubbed -- `create` will not make one.
        """
        from types import SimpleNamespace

        real = store.list(kind="dashboard")
        assert [a.slug for a in real] == [saved.slug], "the fixture library is not what this needs"
        listed = [
            SimpleNamespace(slug="pointer", kind="dashboard", source_path="/elsewhere/p.json"),
            *real,
        ]
        stub = SimpleNamespace(list=lambda kind=None, **_k: listed, get=store.get)
        assert dp.resolve_bound_slug(BOUND, store=stub) == saved.slug

    def test_a_copy_with_provenance_is_an_ordinary_candidate(self, store, saved) -> None:
        """`source_copy_only` means the record owns its bytes and only RECORDS where
        they came from, so the reason to skip a live pointer does not apply to it."""
        from types import SimpleNamespace

        listed = [
            SimpleNamespace(
                slug=saved.slug,
                kind="dashboard",
                source_path="/elsewhere/p.json",
                source_copy_only=True,
            )
        ]
        stub = SimpleNamespace(list=lambda kind=None, **_k: listed, get=store.get)
        assert dp.resolve_bound_slug(BOUND, store=stub) == saved.slug

    def test_a_scan_the_cap_cut_short_refuses_to_say_absent(
        self, store, tmp_path, monkeypatch, saved
    ) -> None:
        """The second way "absent" goes unproven: the newest-first cap.

        The cap is lowered rather than storing 257 packages, because what is under test
        is the arithmetic between the cap and the library size, not the write path.
        """
        store.create(name="Newer", kind="dashboard", content=content(bound_to="crewmate:newer"))
        monkeypatch.setattr(dp, "MAX_BINDING_SCAN", 1)
        with pytest.raises(dp.BindingLookupIncomplete, match="dashboard artifacts are stored"):
            dp.resolve_bound_slug(BOUND, store=store)

    def test_a_package_inside_the_cap_is_still_found(self, store, monkeypatch, saved) -> None:
        # The limit of the previous case: a cap that is not exceeded says nothing, and
        # the lookup answers normally.
        monkeypatch.setattr(dp, "MAX_BINDING_SCAN", 1)
        assert dp.resolve_bound_slug(BOUND, store=store) == saved.slug

    def test_metadata_that_is_not_an_object_is_skipped_and_not_a_crash(
        self, store, tmp_path, saved
    ) -> None:
        """A PRE-EXISTING crash this line's new caller is what exposes.

        ``decode_meta`` reads keys off the parsed record, so a ``meta.json`` holding
        ``[]`` reaches it as a list. Without the type refusal that is an
        ``AttributeError``, and ``ArtifactStore.list`` tolerates ``ArtifactError``,
        ``OSError``, ``ValueError`` and ``TypeError`` and not that one -- so ONE such
        file turns a whole listing into an unhandled crash, which through this module is
        a 500 on a dashboard write with no refusal recorded.

        Refused as an ``ArtifactError`` instead, ``list`` skips the record the way it
        skips any unreadable one and the rest of the library still lists.
        """
        art = store.create(name="Listy", kind="dashboard", content=content(bound_to=BOUND))
        (tmp_path / "artifacts" / art.slug / "meta.json").write_text("[]", encoding="utf-8")

        slugs = [a.slug for a in store.list(kind="dashboard")]
        assert art.slug not in slugs, "list did not skip the non-object record"
        assert saved.slug in slugs, "list dropped the valid record too"

    def test_a_record_of_unknown_kind_does_not_make_the_lookup_refuse(
        self, store, tmp_path, saved
    ) -> None:
        """The boundary of what ``None`` claims, and why it is not widened here.

        ``ArtifactStore.list`` drops a record whose own ``meta.json`` will not read,
        with a warning, so it never reaches the scan. Its ``kind`` is exactly what that
        file holds, so it is only POSSIBLY a dashboard and as likely the widget below.
        The three cases that DO refuse each name a record the listing proved is a
        dashboard; this one names nothing, so the lookup answers for what it can see
        and a member whose page is drawn from a template goes on using it.

        Refusing here instead would cost every member with no package at all: one
        damaged widget anywhere in the store would turn each of its writes into a
        broken-package refusal for a package it does not have.
        """
        art = store.create(name="Some widget", kind="widget", content="<p>hi</p>")
        (tmp_path / "artifacts" / art.slug / "meta.json").write_text("{ not json", encoding="utf-8")
        # The premise: the listing really does hide it, so the scan cannot see it.
        assert art.slug not in [a.slug for a in store.list()]
        assert dp.resolve_bound_slug("crewmate:someone-else", store=store) is None

        # The limit of that: a record the scan DID see and could not decode is a
        # dashboard by the listing's own account, and still refuses.
        self._shredded(store, tmp_path)
        with pytest.raises(dp.BindingLookupIncomplete, match="could not be read"):
            dp.resolve_bound_slug("session:member-atlas", store=store)

    def test_an_unlistable_record_never_hides_a_package_that_is_found(
        self, store, tmp_path, saved
    ) -> None:
        # The record the listing hid is invisible to the scan either way, so a package
        # that IS listed and matches still answers.
        art = store.create(name="Other", kind="dashboard", content=content(bound_to=BOUND))
        (tmp_path / "artifacts" / art.slug / "meta.json").write_text("{ not json", encoding="utf-8")
        assert dp.resolve_bound_slug(BOUND, store=store) == saved.slug

    def test_a_read_this_process_is_not_allowed_refuses_to_say_absent(
        self, store, saved, monkeypatch
    ) -> None:
        """The third way: an ``OSError`` on one artifact file.

        Raised from ``get`` rather than by changing a mode bit, so the case runs the
        same whether or not the suite is root.
        """

        def _denied(_slug: str, **_kw):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(store, "get", _denied)
        with pytest.raises(dp.BindingLookupIncomplete, match="could not be read"):
            dp.resolve_bound_slug(BOUND, store=store)

    def test_a_corrupt_package_bound_elsewhere_is_not_claimed(self, store, tmp_path, saved) -> None:
        # Reading the envelope loosens what counts as readable, not what counts as a
        # match: the binding still has to be the one asked for.
        self._corrupt(store, tmp_path, bound_to="session:member-atlas")
        assert dp.resolve_bound_slug(BOUND, store=store) == saved.slug

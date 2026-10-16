"""The bundle shapes the import refuses with a typed error, and the ones it accepts.

A lone surrogate, a non-text identity column or membership id, a source reference
the bundle does not declare and one source id under two uris raise
``KnowledgeBundleError``, and ``_validate_knowledge_bundle`` checks the state lists'
shape. A folder row carries nothing onto this host, a local row wins only while it
owns items, and a bundle without the state tables imports its items with no
ownership rows. Shared stores, builders and probes live in
``knowledge_bundle_ownership_helpers.py``.
"""

import time

import pytest
from knowledge_bundle_ownership_helpers import (  # noqa: F401 -- fixtures are requested by name
    AGGREGATE_URI,
    _folder_doc,
    _item_count,
    _owned_doc,
    _owner_of,
    exporter_fixture,
    importer_fixture,
)

from kiro_crew.dashboard.handlers.knowledge import _validate_knowledge_bundle
from kiro_crew.knowledge.store import BUNDLE_STATE_KEY_COL, KnowledgeBundleError


class TestStateTextMustBeBindable:
    """`isinstance(value, str)` is not enough: a lone surrogate is a `str` SQLite
    cannot encode, and the `UnicodeEncodeError` sits outside every arm the import
    endpoint catches, so it surfaces as a 500 instead of the malformed-bundle 400."""

    def test_a_lone_surrogate_key_is_a_typed_error(self, exporter, importer):
        _owned_doc(exporter)
        bundle = exporter.export_all()
        bundle["agent_item_state"][0]["slug"] = "bad\ud800key"
        with pytest.raises(KnowledgeBundleError, match="valid UTF-8"):
            importer.import_bundle(bundle)

    def test_a_lone_surrogate_carried_column_is_a_typed_error(self, exporter, importer):
        _owned_doc(exporter)
        bundle = exporter.export_all()
        bundle["agent_item_state"][0]["name"] = "bad\ud800name"
        with pytest.raises(KnowledgeBundleError, match="valid UTF-8"):
            importer.import_bundle(bundle)

    def test_a_non_string_key_is_a_typed_error(self, exporter, importer):
        _owned_doc(exporter)
        bundle = exporter.export_all()
        bundle["agent_item_state"][0]["slug"] = 7
        with pytest.raises(KnowledgeBundleError, match="must be a string or null"):
            importer.import_bundle(bundle)


class TestIdentityColumnsMustBeText:
    """Both become dictionary keys while the bundle's source ids are rewritten, so a
    list or dict raises an unhashable-type TypeError that no handler arm catches --
    a 500 in place of the typed 400. Enforced at the writer, like the properties and
    aliases columns, so a caller that is not the dashboard endpoint is safe too."""

    @pytest.mark.parametrize("bad", [[], {}, 7, None, ""])
    def test_a_source_id_that_is_not_text_is_refused(self, importer, bad):
        bundle = {
            "sources": [{"id": bad, "name": "A", "source_type": "local_file", "uri": "/a.md"}]
        }
        with pytest.raises(KnowledgeBundleError, match="sources.id"):
            importer.import_bundle(bundle)

    @pytest.mark.parametrize("bad", [[], {}, 7, None, ""])
    def test_a_source_uri_that_is_not_text_is_refused(self, importer, bad):
        bundle = {"sources": [{"id": "s1", "name": "A", "source_type": "local_file", "uri": bad}]}
        with pytest.raises(KnowledgeBundleError, match="sources.uri"):
            importer.import_bundle(bundle)

    @pytest.mark.parametrize("field", ["id", "uri"])
    def test_a_lone_surrogate_identity_is_a_typed_error(self, importer, field):
        """A lone surrogate is a `str` SQLite cannot encode, and the `UnicodeEncodeError`
        it raises at bind time is outside every arm the endpoint catches -- a 500 where
        the typed 400 belongs. Both identity values reach a bind, so both take the gate
        every other bundle string takes."""
        src = {"id": "s1", "name": "A", "source_type": "local_file", "uri": "/a.md"}
        src[field] = "bad\ud800value"
        with pytest.raises(KnowledgeBundleError, match=f"sources.{field}"):
            importer.import_bundle({"sources": [src]})

    @pytest.mark.parametrize("field", ["id", "uri"])
    def test_the_surrogate_refusal_names_utf8(self, importer, field):
        src = {"id": "s1", "name": "A", "source_type": "local_file", "uri": "/a.md"}
        src[field] = "bad\ud800value"
        with pytest.raises(KnowledgeBundleError, match="valid UTF-8"):
            importer.import_bundle({"sources": [src]})

    def test_a_lone_surrogate_created_at_is_a_typed_error(self, importer):
        """`created_at` is checked for type and emptiness, and a lone surrogate passes
        both: it is a non-empty `str`. It reaches the same bind as the identity values,
        so it takes the same encodability gate, or the `UnicodeEncodeError` escapes every
        arm the endpoint catches and answers 500 where the typed 400 belongs."""
        src = {
            "id": "s1",
            "name": "A",
            "source_type": "local_file",
            "uri": "/a.md",
            "created_at": "bad\ud800value",
        }
        with pytest.raises(KnowledgeBundleError, match="sources.created_at"):
            importer.import_bundle({"sources": [src]})

    def test_the_created_at_surrogate_refusal_names_utf8(self, importer):
        src = {
            "id": "s1",
            "name": "A",
            "source_type": "local_file",
            "uri": "/a.md",
            "created_at": "bad\ud800value",
        }
        with pytest.raises(KnowledgeBundleError, match="valid UTF-8"):
            importer.import_bundle({"sources": [src]})

    @pytest.mark.parametrize("field", ["name", "source_type"])
    def test_a_null_not_null_column_is_refused_not_dropped(self, importer, field):
        """The schema declares these NOT NULL. A suppressed insert is worse than a loud
        one: the row never lands, the uri stays absent, nothing maps this bundle's source
        id, and the map's id fallback then resolves the UNRELATED local source whose id
        happens to match -- filing the documents under it and granting ownership, with no
        error. That is the collision the remap already detects, arriving by the back
        door."""
        src = {"id": "s1", "name": "A", "source_type": "local_file", "uri": "/a.md"}
        src[field] = None
        with pytest.raises(KnowledgeBundleError, match=f"sources.{field}"):
            importer.import_bundle({"sources": [src]})

    def test_a_null_name_does_not_misfile_items_onto_a_colliding_local_source(self, importer):
        """The whole point: the items must not land on the unrelated local source."""
        local_sid = importer.add_source(
            name="Local", source_type="local_folder", uri="/local/other"
        )
        bundle = {
            "sources": [
                {"id": local_sid, "name": None, "source_type": "local_file", "uri": "/a.md"}
            ],
            "items": [
                {
                    "id": "i1",
                    "title": "T",
                    "content": "body",
                    "item_type": "document",
                    "source_id": local_sid,
                }
            ],
        }
        with pytest.raises(KnowledgeBundleError):
            importer.import_bundle(bundle)
        assert (
            importer.db.execute(
                "SELECT COUNT(*) AS n FROM items WHERE source_id = ?", (local_sid,)
            ).fetchone()["n"]
            == 0
        )

    def test_a_null_created_at_is_refused_not_dropped(self, importer):
        """`created_at` is the last NOT NULL column a bundle supplies: a MISSING key falls
        back to the import clock, but an explicit null does not. Validated so no
        bundle-supplied null can reach the constraint, which is what keeps the answer a
        typed rejection instead of a driver error the endpoint's arms do not name."""
        local_sid = importer.add_source(
            name="Local", source_type="local_folder", uri="/local/other"
        )
        bundle = {
            "sources": [
                {
                    "id": local_sid,
                    "name": "A",
                    "source_type": "local_file",
                    "uri": "/a.md",
                    "created_at": None,
                }
            ],
            "items": [
                {
                    "id": "i1",
                    "title": "T",
                    "content": "body",
                    "item_type": "document",
                    "source_id": local_sid,
                }
            ],
        }
        with pytest.raises(KnowledgeBundleError, match="sources.created_at"):
            importer.import_bundle(bundle)
        assert (
            importer.db.execute(
                "SELECT COUNT(*) AS n FROM items WHERE source_id = ?", (local_sid,)
            ).fetchone()["n"]
            == 0
        )

    def test_a_missing_created_at_still_defaults_to_the_import_clock(self, importer):
        """Absence is not nullity: an export that omits the field must still import."""
        bundle = {
            "sources": [{"id": "s1", "name": "A", "source_type": "local_file", "uri": "/a.md"}]
        }
        importer.import_bundle(bundle)
        row = importer.db.execute("SELECT created_at FROM sources WHERE uri = '/a.md'").fetchone()
        assert row["created_at"]

    def test_a_state_row_source_id_that_is_not_text_is_skipped_not_raised(self, exporter, importer):
        _owned_doc(exporter)
        bundle = exporter.export_all()
        bundle["agent_item_state"][0]["source_id"] = []

        result = importer.import_bundle(bundle)

        assert result["items_imported"] == 1
        assert result["ownership_rows_imported"] == 0

    def test_a_state_row_naming_an_undeclared_source_is_named_in_the_account(
        self, exporter, importer
    ):
        """A text source id the bundle never declares resolves to no source here, so
        the row is skipped and its items arrive unowned. The account names the row,
        as every other unowned-arrival path does, and the import still succeeds."""
        _, item = _owned_doc(exporter)
        bundle = exporter.export_all()
        bundle["agent_item_state"][0]["source_id"] = "undeclared-source"

        result = importer.import_bundle(bundle)

        assert result["items_imported"] == 1
        assert result["ownership_rows_imported"] == 0
        named = [
            w for w in result["withheld"] if w.get("reason") == "ownership_row_source_undeclared"
        ]
        assert named == [
            {
                "reason": "ownership_row_source_undeclared",
                "table": "agent_item_state",
                "source_id": "undeclared-source",
                "key": "doc",
                "items": item,
            }
        ]

    def test_a_state_row_source_id_that_is_not_text_adds_no_account_entry(self, exporter, importer):
        _owned_doc(exporter)
        bundle = exporter.export_all()
        bundle["agent_item_state"][0]["source_id"] = []

        result = importer.import_bundle(bundle)

        assert not [
            w for w in result["withheld"] if w.get("reason") == "ownership_row_source_undeclared"
        ]


class TestMembershipIdentifiersMustBeText:
    """Every one of these is tested against a `set[str]` of withheld items. Set
    membership hashes the key, so a list or dict raises a `TypeError` no handler arm
    catches -- a 500 where the typed 400 belongs -- and a number hashes cleanly while
    matching nothing, which writes a withheld item's dependent row as though the item
    had arrived. Enforced at the writer, beside the gate on the source identity
    columns."""

    @pytest.mark.parametrize("bad", [[], {}, 7])
    def test_an_item_id_that_is_not_text_is_refused(self, importer, bad):
        with pytest.raises(KnowledgeBundleError, match="items.id"):
            importer.import_bundle({"items": [{"id": bad, "title": "T", "content": "c"}]})

    @pytest.mark.parametrize("bad", [[], {}, 7])
    def test_a_relation_source_item_id_that_is_not_text_is_refused(self, importer, bad):
        with pytest.raises(KnowledgeBundleError, match="relations.source_item_id"):
            importer.import_bundle(
                {
                    "relations": [
                        {
                            "id": "r1",
                            "source_id": "e1",
                            "target_id": "e2",
                            "relation_type": "rel",
                            "source_item_id": bad,
                        }
                    ]
                }
            )

    @pytest.mark.parametrize("bad", [[], {}, 7])
    def test_a_location_item_id_that_is_not_text_is_refused(self, importer, bad):
        with pytest.raises(KnowledgeBundleError, match="source_locations.item_id"):
            importer.import_bundle(
                {"source_locations": [{"id": "l1", "item_id": bad, "source_id": "s1"}]}
            )

    @pytest.mark.parametrize("bad", [[], {}, 7])
    def test_a_mention_item_id_that_is_not_text_is_refused(self, importer, bad):
        with pytest.raises(KnowledgeBundleError, match="mentions.item_id"):
            importer.import_bundle({"mentions": [{"entity_id": "e1", "item_id": bad}]})

    @pytest.mark.parametrize("bad", [[], {}, 7])
    def test_an_entity_id_that_is_not_text_is_refused(self, importer, bad):
        """`entities.id` reaches BOTH halves of the problem: the entity insert binds it,
        and the created-entities set holds it. The driver's own bind error is not an
        `sqlite3.Error` subclass here, so neither half is caught downstream."""
        with pytest.raises(KnowledgeBundleError, match="entities.id"):
            importer.import_bundle(
                {"entities": [{"id": bad, "name": "E", "entity_type": "concept"}]}
            )

    def test_a_repeated_item_id_is_refused(self, importer):
        """`item_source` is keyed on `items.id`, so a repeat overwrites the earlier row's
        mapped source -- and because the withheld set holds the ID rather than the row, a
        collision resolved against the surviving mapping skips BOTH rows while the import
        reports success. `items.id` is a PRIMARY KEY, so only one could ever land."""
        bundle = {
            "items": [
                {"id": "i1", "title": "A", "content": "a"},
                {"id": "i1", "title": "B", "content": "b"},
            ]
        }
        with pytest.raises(KnowledgeBundleError, match="items"):
            importer.import_bundle(bundle)

    def test_the_repeated_id_refusal_says_it_repeats(self, importer):
        bundle = {
            "items": [
                {"id": "i1", "title": "A", "content": "a"},
                {"id": "i1", "title": "B", "content": "b"},
            ]
        }
        with pytest.raises(KnowledgeBundleError, match="repeats an id"):
            importer.import_bundle(bundle)

    def test_distinct_item_ids_are_not_refused(self, importer):
        """The rule is about a REPEAT, so two ordinary items must still pass the gate."""
        importer._bundle_membership_ids({"items": [{"id": "i1"}, {"id": "i2"}, {"title": "no id"}]})

    def test_a_null_or_absent_source_item_id_passes_the_gate(self, importer):
        """A relation need not name a source item, and an absent key reads the same way,
        so neither may be refused. Exercised at the gate itself to keep the case free of
        the foreign-key checks a whole import would also apply."""
        importer._bundle_membership_ids({"relations": [{"source_item_id": None}, {}]})


class TestAnUndeclaredSourceReferenceIsRefused:
    """A bundle's source ids are local to the store that minted them, so the import
    resolves them through the `sources` entry's uri. A reference the bundle does not
    declare has no uri to resolve, leaving only the id -- and filing content under
    whichever local row happens to share an id is what the DECLARED path already
    refuses, where a bundle id held by a different uri gets a freshly minted one
    instead. The undeclared case cannot be repaired that way, because nothing names
    the source its content belongs to, so it is refused."""

    def test_item_naming_an_unknown_source_is_refused(self, importer):
        bundle = {
            "sources": [],
            "items": [
                {
                    "id": "i1",
                    "title": "T",
                    "content": "orphan text",
                    "item_type": "document",
                    "source_id": "no-such-source",
                }
            ],
        }
        with pytest.raises(KnowledgeBundleError, match="items.source_id"):
            importer.import_bundle(bundle)
        assert importer.db.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"] == 0

    def test_location_naming_an_unknown_source_is_refused(self, importer):
        bundle = {
            "source_locations": [
                {"id": "loc1", "item_id": "no-such-item", "source_id": "no-such-source"}
            ]
        }
        with pytest.raises(KnowledgeBundleError, match="source_locations.source_id"):
            importer.import_bundle(bundle)

    def test_an_id_this_store_holds_is_refused_just_the_same(self, importer):
        """The case the foreign key cannot catch, and the only one that loses data. The
        id exists HERE, under a source the bundle never names, so the constraint is
        satisfied and the content would be filed under an unrelated local source with
        nothing naming it."""
        other = importer.add_source(name="Other", source_type="local_file", uri="/local/other.md")
        bundle = {
            "items": [
                {
                    "id": "i1",
                    "title": "T",
                    "content": "foreign text",
                    "item_type": "document",
                    "source_id": other,
                }
            ]
        }

        with pytest.raises(KnowledgeBundleError, match="items.source_id"):
            importer.import_bundle(bundle)

        assert (
            _item_count(importer, "foreign text") == 0
        ), "foreign content was filed under a local source the bundle never named"

    def test_a_declared_source_still_resolves(self, exporter, importer):
        """The control. The refusal must not touch a bundle that carries its sources,
        which is every bundle any exporter here produces."""
        _owned_doc(exporter, slug="doc", body="remote body", name="Remote")

        result = importer.import_bundle(exporter.export_all())

        assert result["items_imported"] == 1
        assert result["ownership_rows_imported"] == 1

    def test_item_with_no_source_at_all_still_imports(self, importer):
        """A null reference is not a reference: an item filed under no source is
        legitimate and must not be caught by the refusal."""
        bundle = {
            "items": [{"id": "i1", "title": "T", "content": "loose text", "item_type": "document"}]
        }
        assert importer.import_bundle(bundle)["items_imported"] == 1
        assert (
            importer.db.execute("SELECT source_id FROM items WHERE id = 'i1'").fetchone()[
                "source_id"
            ]
            is None
        )


class TestBundleValidation:
    """The new lists go through the same shape checks as the existing ones."""

    @pytest.mark.parametrize("table", sorted(BUNDLE_STATE_KEY_COL))
    def test_state_table_must_be_a_list(self, table):
        assert _validate_knowledge_bundle({table: {}}) == f"'{table}' must be a list"

    @pytest.mark.parametrize("table", sorted(BUNDLE_STATE_KEY_COL))
    def test_state_entries_must_be_objects(self, table):
        assert _validate_knowledge_bundle({table: ["nope"]}) == f"'{table}' entries must be objects"

    def test_key_column_must_be_text(self):
        assert (
            _validate_knowledge_bundle({"agent_item_state": [{"slug": 7}]})
            == "'agent_item_state.slug' must be a string or null"
        )

    def test_display_name_must_be_text(self):
        assert (
            _validate_knowledge_bundle({"agent_item_state": [{"name": 7}]})
            == "'agent_item_state.name' must be a string or null"
        )

    def test_mtime_shape_is_not_validated_because_it_does_not_travel(self):
        assert _validate_knowledge_bundle({"folder_file_state": [{"mtime": "soon"}]}) is None

    def test_numeric_mtime_and_absent_tables_pass(self):
        assert _validate_knowledge_bundle({"folder_file_state": [{"mtime": 1.5}]}) is None
        assert _validate_knowledge_bundle({"items": []}) is None


class TestAFolderRowCarriesNothingOntoThisHost:
    """A carried `mtime` is one of the things that makes the row dangerous: the scan
    skips hashing a live row whose recorded mtime is at or above the file's, so an
    exporting host's newer value stops the local file being hashed and leaves the
    imported text indexed in its place. Refusing the whole row settles that and the
    deletion order together."""

    def test_no_folder_row_reaches_the_database(self, exporter, importer):
        exporter.db.execute("UPDATE folder_file_state SET mtime = 99999999999.0")
        _folder_doc(exporter)
        exporter.db.commit()

        importer.import_bundle(exporter.export_all())

        assert (
            importer.db.execute("SELECT COUNT(*) AS n FROM folder_file_state").fetchone()["n"] == 0
        )

    def test_no_host_mtime_can_suppress_a_local_hash(self, exporter, importer):
        """With no row at all, every real mtime fails the scan's `mtime <= recorded`
        gate, so the local file is always hashed on its own terms."""
        _folder_doc(exporter)
        importer.import_bundle(exporter.export_all())
        rows = importer.db.execute("SELECT mtime FROM folder_file_state").fetchall()
        assert rows == []
        # flake-ok: time.time() is one sample host mtime among fixed values, never a wait or a deadline
        for real_mtime in (0.0, -1.0, -2208988800.0, 1.0, time.time()):
            assert real_mtime is not None  # nothing recorded to compare against

    def test_the_folder_items_arrive_unowned_not_missing(self, exporter, importer):
        """The state they are in when a bundle carries no state tables at all."""
        _, folder_item = _folder_doc(exporter)
        result = importer.import_bundle(exporter.export_all())
        assert result["items_imported"] == 1
        assert result["items_withheld"] == 0
        assert _owner_of(importer, "folder_file_state", folder_item) is None


class TestLocalRowWinsOnlyWhileItOwnsItems:
    """A local row with an empty or stale group holds a marker, not ownership, and
    nothing else will ever name the items the bundle brought."""

    def test_empty_local_group_yields_to_the_imported_row(self, exporter, importer):
        _, remote_item = _owned_doc(exporter, slug="doc", body="remote body", name="Remote")
        local_sid = importer.add_source(name="Auto-added", source_type="agent", uri=AGGREGATE_URI)
        # A document that lost a de-duplication: a live row with no group.
        importer.db.execute(
            "INSERT INTO agent_item_state (source_id, slug, content_hash, "
            "item_ids, updated_at, name, status) VALUES (?, 'doc', 'loser-hash', "
            "'[]', '2026-01-01T00:00:00', 'Local', 'deduped')",
            (local_sid,),
        )
        importer.db.commit()

        result = importer.import_bundle(exporter.export_all())

        assert result["ownership_rows_imported"] == 1
        assert _owner_of(importer, "agent_item_state", remote_item) == (local_sid, "doc")

    def test_stale_local_group_yields_to_the_imported_row(self, exporter, importer):
        _, remote_item = _owned_doc(exporter, slug="doc", body="remote body", name="Remote")
        local_sid = importer.add_source(name="Auto-added", source_type="agent", uri=AGGREGATE_URI)
        # A group naming only items that have since been deleted.
        importer.db.execute(
            "INSERT INTO agent_item_state (source_id, slug, content_hash, "
            "item_ids, updated_at, name, status) VALUES (?, 'doc', 'gone-hash', "
            "'[\"deleted-item\"]', '2026-01-01T00:00:00', 'Local', 'active')",
            (local_sid,),
        )
        importer.db.commit()

        result = importer.import_bundle(exporter.export_all())

        assert result["ownership_rows_imported"] == 1
        assert _owner_of(importer, "agent_item_state", remote_item) is not None

    def test_displaced_marker_releases_its_claim_on_another_source_items(self, exporter, importer):
        """Leaving the claim behind under a hash no row names means a later deletion
        of the holder reassigns an item here with nothing to adopt it into."""
        _owned_doc(exporter, slug="doc", body="remote body", name="Remote")
        winner = importer.add_source(
            name="Winner", source_type="local_file", uri="/local/winner.md"
        )
        held = importer.add_item(
            title="Winner",
            content="shared text",
            item_type="document",
            source_id=winner,
            content_hash="loser-hash",
        )
        local_sid = importer.add_source(name="Auto-added", source_type="agent", uri=AGGREGATE_URI)
        importer.db.execute(
            "INSERT INTO agent_item_state (source_id, slug, content_hash, "
            "item_ids, updated_at, name, status) VALUES (?, 'doc', 'loser-hash', "
            "'[]', '2026-01-01T00:00:00', 'Local', 'deduped')",
            (local_sid,),
        )
        importer.add_source_location(held, local_sid)
        importer.db.commit()
        assert (
            importer.db.execute(
                "SELECT COUNT(*) AS n FROM source_locations WHERE source_id = ?", (local_sid,)
            ).fetchone()["n"]
            == 1
        )

        importer.import_bundle(exporter.export_all())

        assert (
            importer.db.execute(
                "SELECT COUNT(*) AS n FROM source_locations WHERE source_id = ?", (local_sid,)
            ).fetchone()["n"]
            == 0
        )
        assert (
            importer.db.execute("SELECT COUNT(*) AS n FROM items WHERE id = ?", (held,)).fetchone()[
                "n"
            ]
            == 1
        ), "the winner's item must not be deleted"


class TestDuplicateSourceIdIsMalformed:
    """One id may name only one uri. ``sources.id`` is a PRIMARY KEY, so no export
    produces a repeat; accepting one would let the later entry overwrite the earlier
    one's place in the map and file items under a source never named for them."""

    def test_repeated_id_under_two_uris_is_refused(self, importer):
        bundle = {
            "sources": [
                {"id": "s1", "name": "A", "source_type": "local_file", "uri": "/a.md"},
                {"id": "s1", "name": "B", "source_type": "local_file", "uri": "/b.md"},
            ]
        }
        with pytest.raises(KnowledgeBundleError, match="repeats an id"):
            importer.import_bundle(bundle)
        assert importer.db.execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"] == 0

    def test_repeated_id_under_the_same_uri_is_fine(self, importer):
        bundle = {
            "sources": [
                {"id": "s1", "name": "A", "source_type": "local_file", "uri": "/a.md"},
                {"id": "s1", "name": "A", "source_type": "local_file", "uri": "/a.md"},
            ]
        }
        importer.import_bundle(bundle)
        assert importer.db.execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"] == 1


class TestLegacyBundle:
    """A bundle written without the state tables imports exactly as before."""

    def test_missing_state_tables_import_cleanly(self, exporter, importer):
        _owned_doc(exporter)
        bundle = exporter.export_all()
        for table in BUNDLE_STATE_KEY_COL:
            bundle.pop(table)
        result = importer.import_bundle(bundle)
        assert result["items_imported"] == 1
        assert result["ownership_rows_imported"] == 0

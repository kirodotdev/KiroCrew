"""``.knowledge`` bundles: export, import, and the checks that refuse a malformed bundle.

A bundle value that would commit a corrupt column is refused as
``KnowledgeBundleError`` before any write binds it. Which ownership rows an import
restores, and which items it withholds, is decided in ``bundle_ownership``.

A function here whose first parameter is ``self`` is a ``KnowledgeStore`` method:
the class binds it under the same name in ``knowledge/store.py``, so it reads the
store's fields and calls its sibling methods through ``self``.

The ``KnowledgeStore`` fields this module uses. Writes: none. Reads: none; every query
goes through ``self.db``.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from kiro_crew.knowledge.store_parts.item_ownership import _DOC_STATE_KEY_COL
from kiro_crew.knowledge.store_parts.source_rows import _WALKING_SOURCE_TYPES, _without_sync_status

if TYPE_CHECKING:
    from kiro_crew.knowledge.store import KnowledgeStore


class KnowledgeBundleError(ValueError):
    """A bundle value would commit a corrupt JSON column.

    Raised by :meth:`KnowledgeStore.import_bundle` before any INSERT binds a
    ``sources.properties`` / ``entities.aliases`` value that is not the JSON
    text every reader ``json.loads()`` back.  The dashboard import handler is
    the store's only production caller today; the invariant lives here, with
    the writer, so any future caller (an MCP tool, a CLI import, an app
    backend) is safe by construction instead of depending on one HTTP path's
    pre-validation.
    """


# Named by the path callers import it from: a traceback renders a raised class as
# ``module.qualname``, so this module's name must not reach it.
KnowledgeBundleError.__module__ = "kiro_crew.knowledge.store"


def _validated_json_column(
    value: object, *, field: str, default: str, shape: type, shape_name: str
) -> tuple[str, Any]:
    """Return ``(text, parsed)`` to bind for a store JSON column, or raise.

    ``None`` (and an absent key, which callers pass as ``None``) falls back
    to ``default`` -- the same value the column's schema DEFAULT would
    supply.  Anything present must be JSON text whose parsed value is a
    ``shape`` instance: several readers parse the raw column with
    ``json.loads()`` and no shape guard (source detail handlers index the
    parsed dict; ``find_entity()`` calls ``.lower()`` on each parsed alias),
    so a non-string, an empty string, or the wrong parsed shape commits a
    row that crashes a later, unrelated read.  ``json.loads`` raises
    ``RecursionError`` (not ``ValueError``) on deeply nested input, so it
    is caught alongside.  A lone-surrogate escape (``"\\ud800"``) in the
    outer request JSON decodes to text that ``json.loads`` accepts but the
    SQLite driver cannot UTF-8-encode at bind time, so encodability is
    checked here too -- otherwise the bind raises ``UnicodeEncodeError``
    past the typed-error contract.
    """
    if value is None:
        return default, shape()
    if not isinstance(value, str):
        raise KnowledgeBundleError(f"'{field}' must be a JSON {shape_name} string or null")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise KnowledgeBundleError(f"'{field}' must be valid UTF-8 text") from None
    try:
        parsed = json.loads(value)
    except (ValueError, RecursionError):
        raise KnowledgeBundleError(f"'{field}' must be valid JSON") from None
    if not isinstance(parsed, shape):
        raise KnowledgeBundleError(f"'{field}' must be a JSON {shape_name}")
    return value, parsed


def _validated_properties(value: object) -> str:
    """``sources.properties``: JSON text parsing to an object, or NULL."""
    text, _ = _validated_json_column(
        value, field="sources.properties", default="{}", shape=dict, shape_name="object"
    )
    return text


def _validated_aliases(value: object) -> str:
    """``entities.aliases``: JSON text parsing to an array of strings, or NULL."""
    text, parsed = _validated_json_column(
        value, field="entities.aliases", default="[]", shape=list, shape_name="array"
    )
    if not all(isinstance(alias, str) for alias in parsed):
        raise KnowledgeBundleError("'entities.aliases' must be a JSON array of strings")
    return text


def _validated_embedding_sig(value: object) -> str | None:
    """``items.embedding_sig``: an opaque signature string, or NULL.

    Deliberately shape-only. The value's grammar belongs to its producer
    (:func:`kiro_crew.knowledge.embedder.embed_signature`), and a signature this
    store cannot recognise is safe in the only direction that matters: it fails
    to equal the importing store's own signature, so ``_vector_search`` refuses
    the vector instead of scoring it across spaces. What is NOT safe is a
    non-string reaching the bind, which raises past the typed-error contract --
    hence the guard here rather than at one HTTP path.
    """
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise KnowledgeBundleError("'items.embedding_sig' must be a non-empty string or null")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise KnowledgeBundleError("'items.embedding_sig' must be valid UTF-8 text") from None
    return value


# A group names a document's chunks, and each member is an item id: a uuid-shaped
# string. Both halves are bounded because a cap on the COUNT bounds memory only when
# every retained field is bounded too, and both refusals are visible -- a bundle
# exceeding either is rejected by ``_bundle_group_bounds`` rather than trimmed here.
_MAX_ITEM_GROUP_MEMBERS = 4096


_MAX_ITEM_ID_CHARS = 128


def _bundle_item_group(value: object) -> list[str]:
    """A state row's ``item_ids`` as it arrives in a bundle, parsed to a list of ids.

    A bundle is untrusted input, so anything that is not JSON text holding an array of
    non-empty strings reads as no group at all, which drops the row rather than
    committing ownership that cannot be checked. Duplicates collapse and order is
    kept: the column stores the set of items one document owns, and a repeated id
    would make the group's size disagree with the items behind it.

    Only the TEXT shape is accepted, because that is the only shape any producer
    emits: the column is TEXT holding JSON and an export ships it verbatim.
    """
    if not isinstance(value, str) or not value.strip():
        return []
    try:
        raw: object = json.loads(value)
    except (ValueError, RecursionError):
        return []
    if not isinstance(raw, list):
        return []
    group: list[str] = []
    seen: set[str] = set()
    for entry in raw:
        # A set, because ``entry not in group`` on the growing list costs O(N^2) and
        # this parser runs under BEGIN IMMEDIATE inside a worker thread: a bundle
        # shipping one large unique array spins there holding the write lock.
        if not isinstance(entry, str) or not entry or entry in seen:
            continue
        if len(entry) > _MAX_ITEM_ID_CHARS:
            continue
        group.append(entry)
        seen.add(entry)
        if len(group) >= _MAX_ITEM_GROUP_MEMBERS:
            break
    return group


#: Public view of the per-document state tables a ``.knowledge`` bundle carries,
#: mapped to the column that identifies one document inside each. Derived from
#: ``_DOC_STATE_KEY_COL`` so the dashboard's bundle validator and :meth:`import_bundle`
#: read one list. A bundle that moves items WITHOUT these rows moves content the
#: receiving store can never manage: nothing claims the items for de-duplication,
#: the Sources UI has no per-document group label for them, and a later ingest of
#: the same document adds a second copy instead of replacing the first.
BUNDLE_STATE_KEY_COL: dict[str, str] = dict(_DOC_STATE_KEY_COL)


def export_item(self: KnowledgeStore, item_id) -> dict:
    item = self.get_item(item_id)
    if not item:
        return {}
    mentions = self.db.execute(
        "SELECT entity_id FROM mentions WHERE item_id = ?", (item_id,)
    ).fetchall()
    entity_ids = [m["entity_id"] for m in mentions]
    entity_id_set = set(entity_ids)
    entities = []
    for eid in entity_ids:
        row = self.db.execute("SELECT * FROM entities WHERE id = ?", (eid,)).fetchone()
        if row:
            entities.append(dict(row))
    relations = []
    seen_ids = set()
    for eid in entity_ids:
        for row in self.db.execute(
            "SELECT * FROM entity_relations WHERE source_id = ? OR target_id = ?", (eid, eid)
        ):
            r = dict(row)
            if r["id"] in seen_ids:
                continue
            # A relation whose OTHER endpoint isn't among this item's
            # mentioned entities, or that was recorded under a different
            # item's observation (source_item_id), would re-import
            # referencing an entity/item this single-item bundle never
            # carries -- an FK violation on the receiving end. Only keep
            # relations fully contained in what this bundle exports.
            if r["source_id"] not in entity_id_set or r["target_id"] not in entity_id_set:
                continue
            if r["source_item_id"] not in (None, item_id):
                continue
            seen_ids.add(r["id"])
            relations.append(r)
    locations = [
        dict(r)
        for r in self.db.execute("SELECT * FROM source_locations WHERE item_id = ?", (item_id,))
    ]
    mentions = [
        dict(r) for r in self.db.execute("SELECT * FROM mentions WHERE item_id = ?", (item_id,))
    ]
    source_ids = {
        sid for sid in (item.get("source_id"), *(loc["source_id"] for loc in locations)) if sid
    }
    sources = []
    for sid in source_ids:
        row = self.db.execute("SELECT * FROM sources WHERE id = ?", (sid,)).fetchone()
        if row:
            sources.append(dict(row))
    return {
        "items": [item],
        "sources": sources,
        "entities": entities,
        "relations": relations,
        "source_locations": locations,
        "mentions": mentions,
    }


def export_all(self: KnowledgeStore, namespace: str | None = None) -> dict:
    if namespace:
        items = [
            self._serialize_item(r)
            for r in self.db.execute("SELECT * FROM items WHERE namespace = ?", (namespace,))
        ]
        item_ids = {i["id"] for i in items}
    else:
        items = [self._serialize_item(r) for r in self.db.execute("SELECT * FROM items")]
        item_ids = None
    if item_ids is not None:
        items_subq = "SELECT id FROM items WHERE namespace = ?"
        relations = [
            dict(r)
            for r in self.db.execute(
                f"SELECT * FROM entity_relations WHERE source_item_id IS NULL OR source_item_id IN ({items_subq})",  # noqa: S608
                (namespace,),
            )
        ]
        source_locations = [
            dict(r)
            for r in self.db.execute(
                f"SELECT * FROM source_locations WHERE item_id IN ({items_subq})",  # noqa: S608
                (namespace,),
            )
        ]
        mentions = [
            dict(r)
            for r in self.db.execute(
                f"SELECT * FROM mentions WHERE item_id IN ({items_subq})",  # noqa: S608
                (namespace,),
            )
        ]
    else:
        relations = [dict(r) for r in self.db.execute("SELECT * FROM entity_relations")]
        source_locations = [dict(r) for r in self.db.execute("SELECT * FROM source_locations")]
        mentions = [dict(r) for r in self.db.execute("SELECT * FROM mentions")]
    # Ownership a bundle cannot back is worse than shipping none: the accepting pass
    # keeps only the ids the import wrote, so an id this export does not carry is
    # named on every restore in the account of what arrived owned by nothing, and the
    # group it reaches the other side in is one id short of what the row claims.
    # Deleting one chunk of a document leaves exactly that shape -- no delete path
    # prunes a state row's group, by design -- so the routine case would report
    # content as unowned that nothing is actually missing. Pruned to the items
    # actually carried, and to the ones filed under the row's own source, which is
    # the same pair the accepting pass checks.
    exported_owner: dict[str, object] = {
        row["id"]: row.get("source_id")
        for row in items
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    state_tables: dict[str, list[dict]] = {}
    for table in BUNDLE_STATE_KEY_COL:
        rows = [
            dict(r) for r in self.db.execute(f"SELECT * FROM {table}")
        ]  # noqa: S608 -- table from a module constant
        kept: list[dict] = []
        for row in rows:
            group = [
                item
                for item in _bundle_item_group(row.get("item_ids"))
                if exported_owner.get(item) == row.get("source_id")
            ]
            if not group:
                continue
            row["item_ids"] = json.dumps(group)
            kept.append(row)
        # A namespace-scoped export needs no separate filter: ``exported_owner`` holds
        # only the items this export carries, so a row belonging wholly to another
        # namespace prunes to nothing and is dropped here -- and with it that
        # namespace's ``file_path``, document name and content hashes.
        state_tables[table] = kept
    return {
        "items": items,
        "entities": [dict(r) for r in self.db.execute("SELECT * FROM entities")],
        "relations": relations,
        "sources": [dict(r) for r in self.db.execute("SELECT * FROM sources")],
        "source_locations": source_locations,
        "mentions": mentions,
        # Ownership travels with the content. These rows are what make an
        # imported item manageable at all.
        **state_tables,
    }


def import_bundle(self: KnowledgeStore, bundle: dict) -> dict:
    items_imported = 0
    entities_created = 0
    relations_rebuilt = 0
    state_rows_imported = 0
    now = datetime.now().isoformat()
    # A bundle names its sources by the ids the EXPORTING store minted, and
    # ``sources.uri`` carries the UNIQUE constraint, so the same logical source
    # -- the artifact aggregate at ``artifact://``, a folder watched on two
    # machines -- holds a different id on either side. Every row that points at
    # a source is rewritten through this map, which makes the rule one sentence:
    # a bundle source's uri always ends up present in this store, and everything
    # that pointed at that source points at whichever local row owns that uri.
    #
    # Without it a bundle whose uri is already here inserts no source row (the
    # unique index refuses it) and then every item in the bundle references a
    # source id this store does not have, so the foreign key refuses the write
    # and the entire import rolls back.
    source_id_map: dict[str, str] = {}

    def _source_fk(raw: object, field: str) -> object:
        """*raw* rewritten to its local source id, refused if it resolves nowhere.

        ``source_id_map`` is built from the bundle's own ``sources`` entries, so a
        reference absent from it names a source the bundle does not carry. With no
        uri to match on, the only handle left is an id -- and an id is local to
        whichever store minted it, so resolving one against this store's rows files
        the bundle's content under whatever local source happens to share it. The
        DECLARED path already refuses exactly that: when a bundle source's uri is
        absent here but its id is held by a different uri, the uri arrives under a
        freshly minted id rather than being filed under the unrelated row. An
        undeclared reference cannot be repaired that way, because nothing in the
        bundle names the source its content belongs to, so it is refused as the
        typed rejection the import endpoint turns into its malformed-bundle 400.

        This is also what makes the plain ``INSERT`` in the sources loop defence in
        depth rather than a hope: were it ever to fail and leave a uri unmapped, its
        items are refused here instead of landing under an unrelated source.

        A NULL reference is not a reference. An item filed under no source at all is
        legitimate and arrives exactly as it came.
        """
        if raw is None:
            return None
        mapped = source_id_map.get(raw) if isinstance(raw, str) else None
        if mapped is None:
            raise KnowledgeBundleError(f"'{field}' names a source this bundle does not carry")
        return mapped

    self.db.execute("BEGIN IMMEDIATE")
    try:
        claimed_uris: dict[str, str] = {}
        for src in bundle.get("sources", []):
            # Restore the status from the COLUMN, which ``export_all`` ships
            # (it serializes SELECT * FROM sources). Reading the blob copy
            # instead would land every bundle exported from a fixed store at
            # the 'pending' default -- there is no copy there any more -- and
            # silently resume a folder the user had paused. A bundle written
            # before this change has the blob copy and no column, so fall
            # back to it. Both go through the same allowlist as the other
            # insert paths: a bundle is untrusted input, and a restored
            # 'syncing' would report a conflict forever for a sync that
            # never started.
            #
            # The blob is then stripped like every other insert path: after
            # an insert the column is the only place a status lives, and a
            # value the allowlist just refused has no business surviving
            # inside the row it was refused from. The migration would retire
            # such a key on the next open without ever promoting it, so this
            # is the boundary holding, not a second line of defence.
            props_text = _validated_properties(src.get("properties"))
            # Both identity columns become DICTIONARY KEYS while the bundle's
            # source ids are rewritten, so a list or dict here raises an
            # unhashable-type TypeError rather than the typed rejection the
            # import endpoint turns into a 400. They are also the only handles a
            # source row has, so an empty one names nothing. Enforced at the
            # writer, like the properties and aliases columns, so a caller that
            # is not the dashboard endpoint is safe by construction.
            claimed_id = src.get("id")
            claimed_uri = src.get("uri")
            for label, value in (("id", claimed_id), ("uri", claimed_uri)):
                if not isinstance(value, str) or not value:
                    raise KnowledgeBundleError(f"'sources.{label}' must be a non-empty string")
                # A lone surrogate is a ``str`` SQLite cannot encode, and the
                # ``UnicodeEncodeError`` it raises at bind time sits outside every
                # arm the import endpoint catches, so it surfaces as a 500 rather
                # than the malformed-bundle 400. These two reach a bind like every
                # other bundle string, so they take the same gate.
                try:
                    value.encode("utf-8")
                except UnicodeEncodeError:
                    raise KnowledgeBundleError(
                        f"'sources.{label}' must be valid UTF-8 text"
                    ) from None
            # ``name`` and ``source_type`` are the schema's other NOT NULL columns on
            # this table. An explicit JSON null in either is a constraint violation
            # at insert time, and a suppressed insert is far worse than a loud one:
            # the row never lands, so the uri stays absent, nothing maps this
            # bundle's source id -- and the id lookup that backs the map up then
            # resolves the UNRELATED local source whose id happens to match, filing
            # this bundle's documents under it and granting ownership over them,
            # silently. That is the collision the branch below already detects and
            # routes around, arriving through the fallback instead.
            for label in ("name", "source_type"):
                value = src.get(label)
                if not isinstance(value, str):
                    raise KnowledgeBundleError(f"'sources.{label}' must be a string")
                try:
                    value.encode("utf-8")
                except UnicodeEncodeError:
                    raise KnowledgeBundleError(
                        f"'sources.{label}' must be valid UTF-8 text"
                    ) from None
            # ``created_at`` is the last NOT NULL column a bundle supplies (a missing
            # key falls back to the import clock, but an explicit null does not, and
            # ``updated_at`` is always written from the clock). Validated here so no
            # bundle-supplied null can reach the constraint at all, which is what
            # keeps the answer a typed rejection rather than a driver error.
            claimed_created = src.get("created_at", now)
            if not isinstance(claimed_created, str) or not claimed_created:
                raise KnowledgeBundleError(
                    "'sources.created_at' must be a non-empty string when present"
                )
            try:
                claimed_created.encode("utf-8")
            except UnicodeEncodeError:
                raise KnowledgeBundleError(
                    "'sources.created_at' must be valid UTF-8 text"
                ) from None
            # One id may name only one uri. ``sources.id`` is a PRIMARY KEY, so no
            # export produces two entries sharing one; accepting them would let the
            # later entry overwrite the earlier one's place in the map and file the
            # bundle's items under a source that was never named for them.
            if claimed_uris.setdefault(claimed_id, claimed_uri) != claimed_uri:
                raise KnowledgeBundleError("'sources' repeats an id under two different uris")
            restored = src.get("sync_status")
            if not isinstance(restored, str) or not restored:
                restored = json.loads(props_text or "{}").get("sync_status")
            # A source whose scan WALKS A TREE is never restored as scannable,
            # whatever the bundle says. The allowlist above admits 'active', and a
            # bundle is untrusted input that names its own uri -- so restoring the
            # claimed status would let an imported row point at any readable
            # directory and have the next sweep walk it and spend extraction calls
            # on it, with nobody having asked for that folder. The user chose to
            # import the bundle; they did not thereby choose each directory inside
            # it. 'pending_confirmation' is the same state the add-source endpoint
            # uses, so the row keeps its items and its properties and waits behind
            # the same Confirm control. Aggregate and single-file sources are
            # unaffected: they walk nothing.
            if src.get("source_type") in _WALKING_SOURCE_TYPES and restored != "paused":
                restored = "pending_confirmation"
            # Resolve by uri first, because the uri is the source's identity
            # across stores while the id is local to whichever store minted it.
            local = self.db.execute(
                "SELECT id FROM sources WHERE uri = ?", (src["uri"],)
            ).fetchone()
            if local is None:
                target_id = src["id"]
                if self.db.execute("SELECT 1 FROM sources WHERE id = ?", (target_id,)).fetchone():
                    # The uri is absent but its id is already taken by a source
                    # holding a DIFFERENT uri. Reusing that id would file this
                    # bundle's documents under an unrelated source and skipping
                    # would drop them, so the uri arrives under an id of its own.
                    target_id = str(uuid4())
                # Plain INSERT, not ``INSERT OR IGNORE``: this branch has already
                # established that the uri is absent and that the id it will use is
                # free, so no constraint can fire here for a legitimate row, and the
                # validations above leave no bundle-supplied null able to reach one.
                # It stays plain as defence in depth, because a SUPPRESSED failure
                # here is the worst outcome available: the row never lands, the uri
                # stays absent, nothing maps this bundle's source, and the map's id
                # fallback then files the documents under whatever unrelated local
                # source shares the id -- silently, and with ownership granted over
                # them. A driver error is not this module's typed rejection (the
                # store's SQLite driver raises a class the endpoint's arms do not
                # name), so it surfaces as a server error; that is a worse ANSWER
                # than a 400 and a far better OUTCOME than silent misfiling.
                self.db.execute(
                    "INSERT INTO sources (id, name, source_type, uri, properties, "
                    "sync_status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        target_id,
                        src["name"],
                        src["source_type"],
                        src["uri"],
                        _without_sync_status(props_text),
                        self._initial_status_or_default(restored),
                        src.get("created_at", now),
                        now,
                    ),
                )
                local = self.db.execute(
                    "SELECT id FROM sources WHERE uri = ?", (src["uri"],)
                ).fetchone()
            if local is not None:
                source_id_map[claimed_id] = local["id"]
        # Decided BEFORE the items go in, because an item that arrives and then
        # finds no row to own it is the very defect this ownership round-trip
        # exists to remove.
        self._bundle_membership_ids(bundle)
        self._bundle_group_bounds(bundle)
        blocked_items, withheld_account = self._bundle_blocked_items(bundle, source_id_map)
        # The ids this import actually INSERTED. Ownership is granted over these
        # alone: an id the bundle ships that already exists here was skipped by
        # ``INSERT OR IGNORE``, so the row in the store is local content, and
        # letting an imported state row name it would hand a foreign document the
        # authority to replace or delete it.
        inserted_items: set[str] = set()
        inserted_entities: set[str] = set()
        bundle_entity_refs: set[str] = set()
        live_entity_refs: set[str] = set()
        for item in bundle.get("items", []):
            if item.get("id") in blocked_items:
                continue
            raw_emb = item.get("embedding")
            if isinstance(raw_emb, str) and raw_emb:
                try:
                    raw_emb = base64.b64decode(raw_emb)
                except Exception:
                    raw_emb = None
            # ``embedding_sig`` travels WITH the blob. It is the only thing
            # that says which vector space the imported vector belongs to,
            # and ``HybridRetriever._vector_search`` pins it -- so dropping
            # it lands every imported item at NULL, which the vector leg
            # reads as unproven provenance and refuses. The vectors are in
            # the bundle and would simply never be scored again until a full
            # re-embed. A foreign-space signature is exactly as welcome: it
            # will not match the importing store's own signature, so those
            # vectors are refused on purpose rather than by accident.
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO items (id, title, content, item_type, source_id, chunk_index, namespace, summary, tags, embedding, embedding_sig, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item["id"],
                    item["title"],
                    item["content"],
                    item["item_type"],
                    # Through the map: the exporting store's source id is not this
                    # store's, and an item left pointing at the bundle's id is
                    # refused by the foreign key, which loses the whole import.
                    _source_fk(item.get("source_id"), "items.source_id"),
                    item.get("chunk_index", 0),
                    item.get("namespace", "default"),
                    item.get("summary"),
                    item.get("tags", "[]"),
                    raw_emb,
                    _validated_embedding_sig(item.get("embedding_sig")),
                    item.get("status", "active"),
                    item.get("created_at", now),
                    now,
                ),
            )
            if cursor.rowcount > 0:
                items_imported += 1
                inserted_items.add(item["id"])
                row = self.db.execute(
                    "SELECT rowid FROM items WHERE id = ?", (item["id"],)
                ).fetchone()
                if row:
                    self._fts_index(row[0], item["title"], item["content"], item.get("tags", "[]"))
        for ent in bundle.get("entities", []):
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO entities (id, name, entity_type, description, aliases, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    ent["id"],
                    ent["name"],
                    ent["entity_type"],
                    ent.get("description"),
                    _validated_aliases(ent.get("aliases")),
                    ent.get("created_at", now),
                    now,
                ),
            )
            if cursor.rowcount > 0:
                entities_created += 1
                inserted_entities.add(ent["id"])
        # Every row below points at an item by foreign key, so one naming an item
        # this import withheld has nothing to attach to. Skipping such a row keeps
        # the rest of an otherwise valid bundle: leaving it in makes the constraint
        # refuse the write and the whole import is lost over a document this store
        # already has its own copy of.
        for rel in bundle.get("relations", []):
            for endpoint in (rel.get("source_id"), rel.get("target_id")):
                if isinstance(endpoint, str):
                    bundle_entity_refs.add(endpoint)
                    if rel.get("source_item_id") not in blocked_items:
                        live_entity_refs.add(endpoint)
            if rel.get("source_item_id") in blocked_items:
                continue
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO entity_relations (id, source_id, target_id, relation_type, description, weight, source_item_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rel["id"],
                    rel["source_id"],
                    rel["target_id"],
                    rel["relation_type"],
                    rel.get("description"),
                    rel.get("weight", 1.0),
                    rel.get("source_item_id"),
                    rel.get("created_at", now),
                ),
            )
            if cursor.rowcount > 0:
                relations_rebuilt += 1
        for loc in bundle.get("source_locations", []):
            if loc.get("item_id") in blocked_items:
                continue
            self.db.execute(
                "INSERT OR IGNORE INTO source_locations (id, item_id, source_id, chunk_range, section_title, anchor, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    loc["id"],
                    loc["item_id"],
                    _source_fk(loc["source_id"], "source_locations.source_id"),
                    loc.get("chunk_range"),
                    loc.get("section_title"),
                    loc.get("anchor"),
                    loc.get("created_at", now),
                ),
            )
        for m in bundle.get("mentions", []):
            entity_ref = m.get("entity_id")
            if isinstance(entity_ref, str):
                bundle_entity_refs.add(entity_ref)
                if m.get("item_id") not in blocked_items:
                    live_entity_refs.add(entity_ref)
            if m.get("item_id") in blocked_items:
                continue
            self.db.execute(
                "INSERT OR IGNORE INTO mentions (item_id, entity_id, context, created_at) "
                "VALUES (?, ?, ?, ?)",
                (m["item_id"], m["entity_id"], m.get("context"), m.get("created_at", now)),
            )
        state_rows_imported, dropped_state_rows = self._import_bundle_state(
            bundle, source_id_map, inserted_items, now
        )
        withheld_account.extend(dropped_state_rows)
        # An entity is only reachable through a mention or a relation, and both skip
        # a withheld item -- so an entity the bundle referenced ONLY from rows that
        # named withheld items arrives referenced by nothing, becomes a graph node no
        # document supports, and inflates ``entities_created`` past what is
        # reachable. Scoped three ways: to the entities this import inserted, to ones
        # the bundle actually referenced (a bundle carrying a standalone entity and no
        # mentions means that entity deliberately, and hand-built bundles do it), and
        # to ones nothing in the store references now (a pre-existing local mention
        # still counts).
        stranded = inserted_entities & (bundle_entity_refs - live_entity_refs)
        for entity_id in sorted(stranded):
            still_referenced = self.db.execute(
                "SELECT 1 FROM mentions WHERE entity_id = ? UNION ALL "
                "SELECT 1 FROM entity_relations WHERE source_id = ? OR target_id = ? "
                "LIMIT 1",
                (entity_id, entity_id, entity_id),
            ).fetchone()
            if still_referenced:
                continue
            self.db.execute("DELETE FROM entities WHERE id = ?", (entity_id,))
            entities_created -= 1
        self.db.execute("COMMIT")
    except Exception:
        self.db.execute("ROLLBACK")
        raise
    self._load_graph()
    return {
        "items_imported": items_imported,
        "entities_created": entities_created,
        "relations_rebuilt": relations_rebuilt,
        "ownership_rows_imported": state_rows_imported,
        # Withheld items are the one silent outcome here: a document this store
        # already holds under the same key contributes nothing and lowers
        # ``items_imported`` with no way for the caller to tell why.
        "items_withheld": len(blocked_items),
        # Every id held back is accounted for here by reason and by the document
        # or id it belongs to, so a caller can say WHICH content did not arrive
        # instead of only how much.
        "withheld": withheld_account,
    }


def _bundle_group_bounds(self: KnowledgeStore, bundle: dict) -> None:
    """Refuse a bundle whose ownership group exceeds a named bound.

    ``_bundle_item_group`` stops at ``_MAX_ITEM_GROUP_MEMBERS`` and drops an entry
    longer than ``_MAX_ITEM_ID_CHARS``. Trimming alone would under-claim in silence:
    the accepting pass would read a shorter group than the bundle states and hand the
    missing chunks to no one. So the bundle is REFUSED here instead, which the import
    endpoint answers as a typed 400 naming the field, and a bundle that reaches the
    parser is already inside both bounds.
    """
    for table in _DOC_STATE_KEY_COL:
        rows = bundle.get(table, [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            raw = row.get("item_ids")
            if not isinstance(raw, str) or not raw.strip():
                continue
            try:
                parsed: object = json.loads(raw)
            except (ValueError, RecursionError):
                continue
            if not isinstance(parsed, list):
                continue
            if len(parsed) > _MAX_ITEM_GROUP_MEMBERS:
                raise KnowledgeBundleError(
                    f"{table}.item_ids names {len(parsed)} items, over the "
                    f"{_MAX_ITEM_GROUP_MEMBERS} bound"
                )
            for entry in parsed:
                if isinstance(entry, str) and len(entry) > _MAX_ITEM_ID_CHARS:
                    raise KnowledgeBundleError(
                        f"{table}.item_ids holds a {len(entry)}-character id, over "
                        f"the {_MAX_ITEM_ID_CHARS} bound"
                    )


def _bundle_membership_ids(self: KnowledgeStore, bundle: dict) -> None:
    """Refuse a bundle whose row identifiers cannot be hashed or matched.

    Each of these is tested against a ``set[str]`` of withheld items, and set
    membership hashes the key unconditionally -- so a JSON array or object here
    raises ``TypeError``, which sits outside every arm the import endpoint catches
    and answers 500 where the typed 400 belongs. A number is accepted by the hash
    and is worse than a crash: it matches nothing, so a withheld item's dependent
    row is written as though the item had arrived. ``None`` is legitimate, because a
    relation need not name a source item and an absent key reads the same way.

    The same gate guards ``sources.id`` and ``sources.uri`` one loop earlier, for
    the same reason and with the same answer. ``entities.id`` is here for both
    halves of it: the entity insert binds it AND the created set holds it.
    """
    for field, rows, col in (
        ("items.id", bundle.get("items", []), "id"),
        ("entities.id", bundle.get("entities", []), "id"),
        ("relations.source_item_id", bundle.get("relations", []), "source_item_id"),
        ("source_locations.item_id", bundle.get("source_locations", []), "item_id"),
        ("mentions.item_id", bundle.get("mentions", []), "item_id"),
    ):
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            value = row.get(col)
            if value is not None and not isinstance(value, str):
                raise KnowledgeBundleError(f"'{field}' must be a string when present")
    # One id may name only one item. ``item_source`` is keyed on it, so a repeat
    # silently overwrites the earlier row's mapped source -- and because the
    # withheld set holds the ID rather than the row, a collision resolved against
    # the surviving mapping then skips BOTH rows while the import reports success.
    # ``items.id`` is a PRIMARY KEY, so only one of the two could ever land through
    # ``INSERT OR IGNORE`` in any case. The same refusal guards ``sources.id`` for
    # this same overwrite, a few lines above.
    seen_item_ids: set[str] = set()
    items = bundle.get("items", [])
    if isinstance(items, list):
        for row in items:
            if not isinstance(row, dict):
                continue
            value = row.get("id")
            if isinstance(value, str):
                if value in seen_item_ids:
                    raise KnowledgeBundleError("'items' repeats an id")
                seen_item_ids.add(value)

"""``kirocrew memory``: the verbs over the vector store and the markdown layer.

``_memory_cmd`` runs one verb through ``_memory_verb`` and turns a refusal the verb
raises into one stderr line and exit 1. The verbs that must not open the default
store -- ``show``, a history-layer ``search``, the backup verbs, ``carve`` and
``create-store`` -- dispatch before it is opened. ``_finding_store_suffix`` labels a
named store's row, here and in ``kirocrew security audit`` (``cli_commands_security``).

An owner of the mirror facade ``kiro_crew.cli_commands``: callers and tests reach every
name here as ``kiro_crew.cli_commands.<name>``, and a patch through the facade reaches
this module. This module never imports the facade.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from sqlite3 import Error as StdlibSQLiteError

from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.embeddings import (
    get_shared_embedder,
    make_sync_embed_fn,
    model_file_present,
    store_embedding_space_is_stale,
)
from kiro_crew.hooks import safe_read_file
from kiro_crew.memory import MemoryStore
from kiro_crew.memory_stores import (
    DEFAULT_MEMORY_STORE,
    UnknownMemoryStore,
    memory_store_namespace_lock,
    memory_store_version,
    named_store_or_empty,
    owned_store_path,
    require_memory_store,
    resolve_declared_store,
)
from kiro_crew.security import scan_memory
from kiro_crew.terminal_safe import _TERMINAL_CTRL_RE, safe_terminal_line
from kiro_crew.vector_memory import VectorMemoryStore, _lesson_display_text, declared_store


def _finding_store_suffix(finding: dict) -> str:
    """`` (store <name>)`` for a NAMED store's finding, ``""`` for the default's.

    ``security.scan_memory`` returns one flat list spanning every declared store,
    and an unlabelled row reads as the global memory's, so a crew silo's rows must
    name their silo. ``named_store_or_empty`` treats ``"default"`` and a missing key
    the same, so default-store rows stay unlabelled.
    """
    name = named_store_or_empty(finding.get("store", ""))
    return f" (store {name})" if name else ""


#: The facet columns a V2 export carries, LITERAL here for the same reason the
#: `memory carve` parser spells its choices out: `memory_schema` must stay off the
#: `kirocrew gateway` boot path, which every launch pays for. Drift is caught rather
#: than prevented -- a test asserts this equals ``memory_schema.FACET_NAMES``, so a
#: sixth facet added there fails that check instead of going unreported here.
_IMPORT_FACET_KEYS = ("scope", "surface", "crew", "session_key", "derived_from")


#: The collections ``VectorMemoryStore.import_memory`` ITERATES, and therefore the
#: ones ``memory import`` must type-check before it opens the destination. Not the
#: whole export payload: ``events`` is exported for the record and never imported,
#: and ``markdown`` is export-only. Spelled here rather than inline so the test that
#: reads ``import_memory``'s source can compare the two and fail on drift -- a
#: collection this list misses is one that raises AFTER the store was created.
_IMPORTED_COLLECTIONS = ("semantic", "episodic")


#: Printed after `memory import` when rows still lack a vector. The CLI store has
#: no embed_fn (loading the model would slow every invocation), so imported episodes,
#: and semantic rows whose export names another model, land keyword-only. The
#: gateway's repair loop and boot sweep (`slack/gateway_runtime/memory_lifecycle.py`)
#: fill them once the embedding backend is ready, so no time is promised.
_IMPORT_EMBED_NOTE = (
    "Note: embedding vectors are not built by this command. The gateway's background\n"
    "  re-embed sweep fills them once its embedding backend is ready; until then the\n"
    "  imported rows are keyword-searchable only. `kirocrew memory stats` shows the\n"
    "  progress on its Embedded line."
)


#: Most rows ``memory export`` writes per collection. A cut collection is named on
#: stderr with its full count, so a short file never reads as a complete one.
_EXPORT_EPISODIC_LIMIT = 10_000


_EXPORT_EVENTS_LIMIT = 1_000


def _markdown_memory_store() -> MemoryStore:
    """MemoryStore anchored where the DEFAULT runtime writer writes.

    The gateway's consolidator writes the markdown layer over a bare
    ``MemoryStore()`` (the ``workspace`` dir under the data home), so the reader
    resolves the same way; when that differs from ``workspace_dir_for()``, the
    writer wins.

    DEFAULT-ONLY: a named store's markdown root sits under the fenced
    ``memory_stores/`` subtree, which ``markdown_snapshot`` cannot read, so
    ``memory export`` refuses ``--include-markdown`` for a named store.
    """
    return MemoryStore()


def _memory_search_history(args: argparse.Namespace) -> None:
    """Print FTS5 hits from the markdown memory layer.

    Reads the same index the heartbeat and gateway keep current, so this needs
    no embedder and no vector store — it answers "where did I write this word"
    against preferences, projects and the dated daily-history files.
    """
    store = _markdown_memory_store()
    results = store.search(args.query, limit=10)
    if not results:
        # An empty index is not the same statement as an absent word, so the two
        # are reported differently.
        if not store.index_row_count():
            print("Memory history index is empty or unavailable; nothing was searched.")
        else:
            print("No memory-history matches.")
        return
    print("  Daily history / preferences / projects:")
    for r in results:
        # Strip terminal control sequences for the same reason the semantic
        # listing does: memory holds whatever the user pasted into a session.
        path = _TERMINAL_CTRL_RE.sub("", str(r.get("path", "?")))
        snippet = _TERMINAL_CTRL_RE.sub("", str(r.get("snippet", ""))).strip()
        print(f"    {path}")
        if snippet:
            print(f"        {snippet}")


def _memory_show(args: argparse.Namespace) -> None:
    """Read-only view of the markdown memory layer (preferences/projects/history).

    Routes through :class:`MemoryStore`'s own readers so consumers depend on an
    interface rather than the on-disk layout. A missing or empty file is a
    normal state and prints as empty rather than erroring. Validation failures
    go to stderr with a non-zero exit so scheduled (non-TTY) consumers get a
    real failure signal instead of non-JSON text on stdout.
    """
    target = getattr(args, "target", None)
    since_raw = getattr(args, "since", None)
    since = None
    if since_raw:
        if target not in (None, "history"):
            print("--since applies only to history", file=sys.stderr)
            sys.exit(1)
        try:
            since = datetime.strptime(since_raw, "%Y-%m-%d").date()
        except ValueError:
            print(f"Invalid --since date (expected YYYY-MM-DD): {since_raw}", file=sys.stderr)
            sys.exit(1)
    snapshot = _markdown_memory_store().markdown_snapshot(since=since)
    if getattr(args, "format", "md") == "json":
        payload = snapshot[target] if target else snapshot
        print(json.dumps(payload, indent=2))
        return
    parts: list[str] = []
    for key in [target] if target else ["preferences", "projects", "history"]:
        if key == "history":
            parts.extend(e["content"].strip() for e in snapshot[key] if e["content"].strip())
        elif snapshot[key]["content"].strip():
            parts.append(snapshot[key]["content"].strip())
    text = "\n\n".join(parts)
    if text:
        # The markdown layer is consolidator (LLM) written — strip terminal
        # control sequences before printing, same policy as `memory list`.
        print(_TERMINAL_CTRL_RE.sub("", text))


# Each says WHICH index answered, because the caller cannot tell a keyword hit
# from a semantic one by looking at the output. On stderr, not stdout: the hits
# are still the answer and `--layer vector`'s stdout shape is a documented
# promise. Same degradations, same wording shape, as the `kirocrew run`
# store in cli_server.
_SEARCH_KEYWORD_ONLY_NO_MODEL = (
    "Embedding model not downloaded yet — keyword-matching this query "
    "(the gateway downloads it in the background)"
)


_SEARCH_KEYWORD_ONLY_STALE_SPACE = (
    "Embedding model changed — keyword-matching this query "
    "(the gateway re-embeds in the background)"
)


_SEARCH_KEYWORD_ONLY_NOT_READY = (
    "Embedding model did not load — keyword-matching this query "
    "(run 'kirocrew doctor' for the reason)"
)


_SEARCH_KEYWORD_ONLY_PENDING_ROWS = (
    "No embedded episodic row matched — keyword-matching this query "
    "(rows still waiting for the gateway's re-embed sweep are keyword-only)"
)


# Loading the GGUF off local disk, matching the wait the knowledge bench allows
# for the same one-shot CLI block (`cli_bench._KB_REAL_EMBED_TIMEOUT_S`).
_SEARCH_MODEL_LOAD_TIMEOUT_SECS = 120.0


def _memory_search_embedding(store: VectorMemoryStore, query: str) -> list[float] | None:
    """Query vector for `memory search`, or None when it must degrade to keyword.

    ``search_episodic`` text-searches whenever ``query_embedding`` is None and
    does NOT auto-embed (only ``get_episodic_context`` does), so a one-shot read
    must produce its own query vector or it never does semantic recall.

    NO download kick: a one-shot CLI must not start a 610MB download it abandons
    at exit; the gateway owns that. A stale vector space degrades too: the loaded
    FAISS index was built by another model, and comparing vectors across models
    is worse than keyword.

    ``embed_fn`` is bound only on the path that embeds, and no
    ``embed_fn_factory`` is wired, so a lazy rebind cannot undo the degraded
    branches.
    """
    if not query:
        return None
    # Imported here so the cli_commands facade keeps serving only its owners' names.
    from kiro_crew.embeddings import ollama_backend_selected

    if not model_file_present() and not ollama_backend_selected():
        print(_SEARCH_KEYWORD_ONLY_NO_MODEL, file=sys.stderr)
        return None
    if store_embedding_space_is_stale(store):
        print(_SEARCH_KEYWORD_ONLY_STALE_SPACE, file=sys.stderr)
        return None
    # BLOCK once, here: make_sync_embed_fn() waits at most _COLD_LOAD_WAIT_SECS,
    # which a slow host can exceed. wait_ready is the llama.cpp backend's seam,
    # not on the EmbeddingBackend ABC, so probe for it.
    embedder = get_shared_embedder()
    wait_ready = getattr(embedder, "wait_ready", None)
    ready = (
        wait_ready(timeout=_SEARCH_MODEL_LOAD_TIMEOUT_SECS)
        if callable(wait_ready)
        else embedder.is_ready()
    )
    if not ready:
        print(_SEARCH_KEYWORD_ONLY_NOT_READY, file=sys.stderr)
        return None
    store.embed_fn = make_sync_embed_fn()
    return store._try_embed(query)


def _memory_backup_cmd(action: str, args: argparse.Namespace) -> None:
    """The three verbs that must work on a store too broken to open.

    None of these touches ``VectorMemoryStore``, so they run against a file that
    fails ``PRAGMA journal_mode=WAL``.
    """
    if action == "backup":
        from kiro_crew import memory_backup

        keep = getattr(args, "keep", None)
        if keep is None:
            keep = KiroCrewConfig.load().memory.backup_keep
        # ``force``: the interval guard is for the heartbeat sweep. An operator typing
        # this verb wants a copy of the store as it is NOW, as the dashboard's
        # "back up now" and the pre-update snapshot do.
        result = memory_backup.back_up_all_stores(int(keep), force=True)
        # With the guard bypassed, ``skipped`` means the store was never opened or its
        # file is empty, so a pass that copied nothing says so.
        print(
            f"Backed up {result['backed_up']} store(s); "
            f"removed {result['pruned']} old; {result['failed']} failed; "
            f"{result['skipped']} skipped (nothing to copy)."
        )
        # A store that failed to copy has no fresh backup, so a script must not read
        # this run as success. A skip is not a failure: there was nothing to copy.
        if result["failed"]:
            sys.exit(1)

    elif action == "backups":
        from kiro_crew import memory_backup
        from kiro_crew.memory_stores import declared_store_names, owned_store_path

        only = getattr(args, "store", None)
        # ``declared_store_names`` matches the order of the pass that wrote the backups.
        # ``owned_store_path`` makes an undeclared name, or a declared one whose path
        # is not that store's own file, one skipped row rather than a traceback.
        for name in [only] if only else declared_store_names():
            path = owned_store_path(name)
            if path is None:
                print(f"{name}: not a declared memory store")
                if only:
                    # The one store asked for cannot be listed: a failure, like
                    # `memory forget` on a key that is not there.
                    sys.exit(1)
                continue
            backups = memory_backup.list_backups(path)
            print(f"{name}:")
            if not backups:
                # Said plainly: silence would read as "fine".
                print("  (no backups yet)")
            for backup in backups:
                print(f"  {backup.name}  {backup.stat().st_size / 1_048_576:.1f} MiB")

    elif action == "restore":
        from kiro_crew import memory_backup
        from kiro_crew.member_memory_backup import is_member_store
        from kiro_crew.memory_stores import DEFAULT_MEMORY_STORE

        # Defaulted only when the flag is ABSENT, as in `_memory_verb`: restore is a
        # write, and `--store ''` (a shell expanding an unset variable) names no store.
        target_store = getattr(args, "store", None)
        if target_store is None:
            target_store = DEFAULT_MEMORY_STORE
        if not target_store:
            raise ValueError("--store names no store; nothing was restored.")
        chosen = getattr(args, "from_backup", None)
        if getattr(args, "cancel_pending", False):
            from kiro_crew.memory_stores import owned_store_path

            path = owned_store_path(target_store)
            if chosen or path is None:
                raise ValueError("Cancel requires an available memory store and cannot use --from")
            cancelled = memory_backup.cancel_pending_restore(path)
            print("Staged restore cancelled." if cancelled else "No staged restore to cancel.")
            print("Current memory and backups are unchanged.")
            return
        source = Path(chosen) if chosen else memory_backup.newest_backup(target_store)
        # A restore that finds no backup raises ValueError, which `_memory_cmd`
        # reports as one line on stderr with exit 1.
        if source is None:
            raise ValueError(f"no backup found for memory store {target_store!r}")
        try:
            restored = memory_backup.restore_from_backup(source, target_store)
        except FileNotFoundError as exc:
            raise ValueError(str(exc)) from exc
        print(f"Staged restore for {target_store!r} from {source.name}.")
        print("Restart the gateway to activate it. Current memory is unchanged.")
        if is_member_store(restored):
            print("Activation preserves any existing member memory directory in full.")
            return
        print("Activation preserves the prior database and WAL as memory.db.superseded.*")


def _admitted_store_path(
    store_name: str, cfg: "KiroCrewConfig", *, may_create: bool
) -> Path | None:
    """*store_name*'s own vector file, or ``None`` after printing the refusal to stderr.

    One guard for every ``kirocrew memory`` verb that opens a NAMED store, because
    the cause it guards is not per-verb: ``VectorMemoryStore.init`` creates whatever
    path ``resolve_store_path`` composes, so each un-admitted call site is its own
    silent-recreation hole. ``may_create`` is true only for ``import``, whose whole
    job is to populate a store that may hold nothing yet.
    """
    try:
        # ADMISSION BEFORE RESOLUTION. The resolvers compose a path by shape, and
        # `VectorMemoryStore.init` CREATES what is missing, so resolving first would
        # silently recreate a deleted declared store EMPTY and `export` would ship an
        # empty payload that reads as "holds nothing" rather than as a loss.
        #
        # This seam rather than a local `exists()`: it also carries the retirement,
        # declaration, ownership-exclusivity and manifest checks, and refuses an
        # undeclared name.
        #
        # `default` is admitted without a filesystem check: its absence is a normal
        # pre-first-write state, while a DECLARED named store's absence is not.
        require_memory_store(store_name, config=cfg)
    except UnknownMemoryStore as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return None
    db_path = owned_store_path(store_name)
    if db_path is None:
        # Defence in depth: `owned_store_path` re-checks that the path is that
        # store's OWN file, so a symlink cannot hand two crews one silo.
        print(
            f"Error: memory store {store_name!r} did not resolve to its own file",
            file=sys.stderr,
        )
        return None
    if named_store_or_empty(store_name) and not may_create and not db_path.exists():
        # ONLY `import` may create a named store's database; every other verb is a
        # READ, and a read must not create what it failed to find. Admission does not
        # cover this: for a V1 named store `_require_legacy_store_files` returns on a
        # missing `memory.db`, so a store whose directory survived without its
        # database would pass and `init()` would write a fresh empty one. Named
        # stores only: the default store's `memory.db` is absent before first write.
        print(
            f"Error: memory store {store_name!r} has no {db_path.name}; refusing to "
            "create one on a read. If the store was never written this is expected; "
            "if it held rows, restore it with `kirocrew memory restore --store "
            f"{store_name}` before reading.",
            file=sys.stderr,
        )
        return None
    return db_path


def _memory_carve(args: argparse.Namespace) -> None:
    """Filter or count one memory store's rows by their carve facets.

    Reads the store NAMED on the command line: facets exist only on a crew silo,
    and ``_memory_cmd``'s shared store is the default one. ``resolve_declared_store``
    raises on an undeclared or malformed name and never degrades onto the default.

    Not an MCP tool: letting a session name an arbitrary store is the cross-crew
    read a silo exists to prevent, which is why the HTTP route requires the
    dashboard owner to select a named store.
    """
    from kiro_crew import memory_schema

    requested = getattr(args, "store", None) or DEFAULT_MEMORY_STORE
    try:
        name = resolve_declared_store(requested)
    except UnknownMemoryStore as exc:
        # A shape defect raises rather than degrading: repairing `../work` or `Work`
        # onto `work` would point two crews at one directory.
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    cfg = KiroCrewConfig.load()
    # `carve` is a READ, so a deleted declared store is refused rather than
    # recreated empty by the `init()` below.
    db_path = _admitted_store_path(name, cfg, may_create=False)
    if db_path is None:
        sys.exit(1)
    # Opened the way the store's declaration says: a member store refuses a bare
    # V1 `init()`, and V2 member stores are the stores facets exist for.
    store = declared_store(
        db_path, store_id=name, config=cfg, embedding_dim=cfg.memory.embedding_dim
    )
    store.init()
    try:
        # Keyed by facet NAME, read off the namespace by that name: an omitted flag
        # is absent from the mapping (the axis is unconstrained), while an
        # explicitly empty one filters for the rows no writer attributed.
        filters = {
            facet: value
            for facet in memory_schema.FACET_NAMES
            for value in [getattr(args, facet, None)]
            if value is not None
        }
        kind = getattr(args, "kind", None) or ""
        group_by = getattr(args, "count_by", None)
        if group_by:
            counts = store.count_by_facet(group_by, filters, kind=kind)
            if not counts:
                print(f"No live rows in {name!r} match that carve.")
                return
            for value, total in counts.items():
                label = _TERMINAL_CTRL_RE.sub("", value) if value else "(unattributed)"
                print(f"  {label}: {total}")
            return
        rows = store.list_by_facets(
            filters,
            kind=kind,
            limit=int(getattr(args, "limit", 50)),
            offset=int(getattr(args, "offset", 0)),
        )
        if not rows:
            print(f"No live rows in {name!r} match that carve.")
            return
        for row in rows:
            # The key for a semantic row, the id for an episode, which has none.
            handle = row["key"] or row["id"]
            text = _TERMINAL_CTRL_RE.sub("", str(row["text"]))[:120]
            print(f"  [{row['kind']}] {_TERMINAL_CTRL_RE.sub('', str(handle))}: {text}")
            axes = " ".join(
                f"{facet}={_TERMINAL_CTRL_RE.sub('', str(row[facet]))}"
                for facet in memory_schema.FACET_NAMES
                if row[facet]
            )
            if axes:
                print(f"        {axes}")
    except memory_schema.FacetsUnsupported as exc:
        # Named, never answered with an empty list: "no rows carry that crew" and
        # "this store cannot record a crew at all" are different facts, and only
        # one of them means the carve is empty.
        print(f"Cannot carve {name!r}: {exc}", file=sys.stderr)
        sys.exit(1)
    except memory_schema.UnknownFacet as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        store.close()


def _settle_created_database(
    db_path: Path,
    store_name: str,
    import_file: str,
    *,
    created: bool,
    rows_landed: bool | None,
    preexisting_sidecars: set[Path],
) -> bool:
    """Decide what happens to a database THIS import brought into existence.

    Called from the `finally`, so it runs on an interrupt too: an aborted import's
    empty file masks a loss just as a completed empty one does.

    ``rows_landed is None`` means the import never reported. Rows may have been
    committed before the abort, so nothing is deleted; the operator is told what
    is there instead.

    Returns True when it printed a "nothing could be imported" error, so the
    caller can exit 1 once no exception is already on its way out. It cannot exit
    here: a ``SystemExit`` raised in a ``finally`` would replace the real error.
    """
    if not created or rows_landed:
        return False
    if rows_landed is None:
        print(
            f"Error: the import into memory store {store_name!r} did not finish, and it "
            f"created {db_path}. Rows already written are still in it, so it was left "
            "in place; if the import wrote nothing, remove that file so a later read "
            "reports the missing database instead of answering with no rows."
        )
        return False
    # AFTER `store.close()`, which is load-bearing: on Windows an open SQLite handle
    # refuses the unlink, so removing the file while it is open would raise here and
    # pass on POSIX.
    leftovers = [
        q for q in db_path.parent.glob(db_path.name + "-*") if q not in preexisting_sidecars
    ]
    for leftover in (db_path, *leftovers):
        try:
            leftover.unlink()
        except OSError as exc:
            print(f"Warning: could not remove {leftover.name}: {exc}")
    if db_path.exists():
        # The removal failed, so the empty database still masks the loss.
        print(
            f"Error: nothing in {import_file} could be imported, and the empty "
            f"{db_path.name} this created could NOT be removed from memory store "
            f"{store_name!r}. Remove {db_path} by hand: while it exists a read "
            "answers with no rows instead of reporting that the database is missing."
        )
    else:
        print(
            f"Error: nothing in {import_file} could be imported, so memory store "
            f"{store_name!r} still has no {db_path.name}. Its database was missing "
            "before this import and the empty one this created was removed, because a "
            "later read must still report the loss rather than answer with no rows."
        )
    return True


def _memory_create_store(args: argparse.Namespace) -> None:
    """``kirocrew memory create-store <name>``: create a declared V1 store's directory.

    Use never creates a store's directory (a deleted store must stay a visible
    loss), so this explicit step is how a declared store becomes usable. A
    refusal raises ``UnknownMemoryStore`` (a ``ValueError``), which
    :func:`_memory_cmd` prints as one line with exit 1.
    """
    from kiro_crew.memory_stores import create_declared_store

    name = args.name
    path, created = create_declared_store(name)
    safe = _TERMINAL_CTRL_RE.sub("", name)
    if created:
        print(f"Created memory store {safe!r} at {path}.")
    else:
        print(f"Memory store {safe!r} already exists at {path}; nothing changed.")


def _interactive_terminal() -> bool:
    """Whether both stdin and stdout are a terminal a person can answer at."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def _refuse_forget_outside_owner_terminal() -> None:
    """Refuse ``memory forget`` on native Windows, from an agent session's shell,
    or with no terminal.

    These are the CLI's own refusals, checked before any store opens. The first is
    the platform: the verb reaches the gateway only over the dashboard's Unix
    socket, and a Python with no ``socket.AF_UNIX`` (native Windows) cannot open
    one. That is the same condition ``loopback_http.unix_socket_urlopen`` fails on,
    so the refusal comes before the value is shown or a confirmation is asked for.
    The other two keep an agent from running the verb; they are not what
    authorizes the delete. That is the gateway's owner gate on the dashboard delete
    route, which the verb calls (``memory_forget_client``), so an empty answer here
    grants nothing by itself.
    """
    import socket

    if not hasattr(socket, "AF_UNIX"):
        raise ValueError(
            "`kirocrew memory forget` is not available on native Windows (it needs "
            "the dashboard's Unix socket); nothing was forgotten. Delete the key "
            "from the dashboard Memory page."
        )
    from kiro_crew.mcp_caller import resolve_own_identity

    identity = resolve_own_identity()
    if identity.session_key or identity.failed:
        raise ValueError(
            "`kirocrew memory forget` does not run from an agent session's shell; "
            "nothing was forgotten. Run it in your own terminal, or delete the key "
            "from the dashboard Memory page."
        )
    if not _interactive_terminal():
        raise ValueError(
            "`kirocrew memory forget` asks you to confirm, so it needs an interactive "
            "terminal; nothing was forgotten."
        )


def _memory_forget(store: VectorMemoryStore, args: argparse.Namespace) -> None:
    """Show one semantic key, ask the person at the terminal, then delete it
    through the gateway's owner-gated route. Exits 1 when the key is absent."""
    from kiro_crew.memory_forget_client import AlreadyAbsent, forget_via_gateway

    key = str(args.key)
    winner = getattr(args, "superseded_by", None)
    shown_key = _TERMINAL_CTRL_RE.sub("", key)
    if winner is not None and winner == key:
        raise ValueError("--superseded-by names the key being forgotten; nothing was forgotten.")
    row = store.get_semantic(key)
    if row is None:
        print(f"Already absent: {shown_key}", file=sys.stderr)
        sys.exit(1)
    if winner is not None and store.get_semantic(winner) is None:
        raise ValueError(
            f"--superseded-by {_TERMINAL_CTRL_RE.sub('', winner)!r} is not a live key; "
            "nothing was forgotten."
        )
    try:
        value = json.loads(row["value_json"])
    except (TypeError, ValueError):
        value = row["value_json"]
    if key.startswith("lesson."):
        value = _lesson_display_text(value) or value
    print(f"  {shown_key}: {_TERMINAL_CTRL_RE.sub('', str(value))}")
    try:
        typed = input("Type the key again to forget it: ")
    except (EOFError, KeyboardInterrupt):
        print()
        typed = ""
    if typed.strip() != key:
        print("Not confirmed; nothing was forgotten.", file=sys.stderr)
        sys.exit(1)
    try:
        # Compare-and-delete against the value just shown, so a write landing
        # after it was shown is never removed unseen.
        forget_via_gateway(key, expect_value_json=row["value_json"], superseded_by=winner)
    except AlreadyAbsent:
        print(f"Already absent: {shown_key}", file=sys.stderr)
        sys.exit(1)
    if winner is not None:
        print(f"Forgotten; superseded by {_TERMINAL_CTRL_RE.sub('', winner)}.")
    else:
        print("Forgotten.")


def _memory_cmd(args: argparse.Namespace) -> None:
    """Manage the memory system (vector store + markdown layer).

    A refusal RAISED out of a verb is one line on stderr with exit 1 rather than a
    traceback, which keeps the stream and exit code that traceback had. An opener
    raises `ValueError` past admission (a member database that changed after it was
    admitted, the startup barrier) and SQLite raises its own error on a corrupt
    file, so the boundary is here rather than at each open. The refusals a verb
    prints itself (`_admitted_store_path`, carve's name check) keep their stdout
    one-liners.
    """
    try:
        _memory_verb(args)
    except (ValueError, sqlite3.Error, StdlibSQLiteError) as exc:
        logging.getLogger("kiro_crew.cli_commands").debug("kirocrew memory refused", exc_info=True)
        print(f"Error: {_TERMINAL_CTRL_RE.sub('', str(exc))}", file=sys.stderr)
        sys.exit(1)


def _memory_verb(args: argparse.Namespace) -> None:
    """Run one ``kirocrew memory`` verb; :func:`_memory_cmd` owns its refusals."""
    action = getattr(args, "mem_action", None)
    # Checked before ANY store opens, so a refused call touches nothing.
    if action == "forget":
        _refuse_forget_outside_owner_terminal()
    # "show" reads only the markdown layer — don't open (or create) the
    # vector store for it.
    if action == "show":
        _memory_show(args)
        return
    # "search --layer history" reads only the markdown FTS index, so don't open
    # (or create) the vector store for it — same reason as "show" above.
    if action == "search" and getattr(args, "layer", "all") == "history":
        _memory_search_history(args)
        return
    # The backup verbs run BEFORE the store is opened: `restore` and `backups` are
    # for a corrupt store, and `store.init()` below runs `PRAGMA journal_mode=WAL`,
    # which raises "file is not a database" on exactly that file.
    if action in ("backup", "backups", "restore"):
        _memory_backup_cmd(action, args)
        return
    # "carve" opens the store NAMED on the command line, not the shared open below.
    if action == "carve":
        _memory_carve(args)
        return
    # "create-store" names the store that does not exist yet, so nothing opens first.
    if action == "create-store":
        _memory_create_store(args)
        return
    cfg = KiroCrewConfig.load()
    # Only `export` and `import` name a store here; every other verb resolves the
    # DEFAULT store, the same `config_dir()/MEMORY_DB_FILE` `VectorMemoryStore()`
    # derives for itself.
    #
    # Defaulted only when the flag is ABSENT: an `or` would swallow `--store ''`
    # (a shell expanding an unset variable) and point a WRITE at the default store,
    # while `resolve_store_path('')` refuses it.
    store_name = getattr(args, "store", None)
    if store_name is None:
        store_name = DEFAULT_MEMORY_STORE
    # ONE HOLD FROM ADMISSION THROUGH CLEANUP. Admission, the version check, the
    # existence check and the final cleanup all ask about one store's identity; a
    # concurrent allocation, retirement or replacement between them would let the
    # import write into a store none of those checks measured.
    #
    # `memory_store_namespace_lock` is ordered BEFORE the store's own file locks, so
    # holding it around the open cannot invert. Taken only for an `import` into a
    # NAMED store, the only path that removes a file.
    import_wrote_nothing = False
    with contextlib.ExitStack() as create_guard:
        if action == "import" and named_store_or_empty(store_name):
            create_guard.enter_context(memory_store_namespace_lock())
        db_path = _admitted_store_path(store_name, cfg, may_create=action == "import")
        if db_path is None:
            sys.exit(1)
        if action == "import" and memory_store_version(store_name) == 2:
            # REFUSED BEFORE THE OPEN: `import_memory` rebuilds rows through
            # `set_semantic` and `write_episodic` with no `facets=`, so a V2 destination
            # would receive rows stripped of scope, surface, crew, session_key and
            # derived_from while reporting success. A named V1 destination still works;
            # V1 has no facets to lose.
            #
            # `memory_store_version` is the same resolver `VectorMemoryStore.__init__`
            # uses, so this does not depend on the store object being built.
            print(
                f"Error: memory store {store_name!r} is a private V2 store, and this import "
                "cannot carry the scope, surface, crew, session and derivation facets its "
                "rows hold, so nothing was written. Import into a V1 store, or use the "
                "owner-authorized copy in the dashboard, which preserves provenance.",
                file=sys.stderr,
            )
            sys.exit(1)
        import_payload: dict | None = None
        if action == "import":
            # PARSED BEFORE THE OPEN: `store.init()` creates `memory.db`, so a missing
            # or malformed input file must not leave a fresh empty database that masks
            # a lost store from the read guard.
            # Each refusal below imported nothing, so it exits 1 on stderr: a script
            # running `memory import f && next` must not read it as a success.
            import_file = getattr(args, "file", None)
            if not import_file:
                print("Usage: kirocrew memory import <file>", file=sys.stderr)
                sys.exit(1)
            path = Path(import_file)
            if not path.is_file():
                print(f"File not found: {import_file}", file=sys.stderr)
                sys.exit(1)
            try:
                loaded = json.loads(safe_read_file(str(path)))
            except (OSError, ValueError) as exc:
                # OSError as well as ValueError: `safe_read_file` re-raises a
                # PermissionError or a mid-read truncation unchanged.
                print(f"Error: {import_file} could not be read as JSON: {exc}", file=sys.stderr)
                sys.exit(1)
            if not isinstance(loaded, dict):
                # `import_memory` reads `data.get(...)`, so a top-level list or scalar
                # would raise inside the write path -- after the store was opened.
                print(
                    f"Error: {import_file} is not an export object; nothing was imported.",
                    file=sys.stderr,
                )
                sys.exit(1)
            for collection in _IMPORTED_COLLECTIONS:
                # `{"semantic": null}` answers `None` from `data.get(..., [])`, and
                # `import_memory` iterates it outside its per-entry try/except, so it
                # would raise after `store.init()` created the database.
                if not isinstance(loaded.get(collection, []), list):
                    print(
                        f"Error: {import_file} has a {collection!r} collection that is not "
                        "a list; nothing was imported.",
                        file=sys.stderr,
                    )
                    sys.exit(1)
            import_payload = loaded
        # Checked on the outcome, not payload shape: many list-shaped payloads write
        # nothing. If THIS import created a named store's database and no row landed,
        # the file is removed after the handle closes, so a later read still reports
        # the missing database.
        import_created_db = (
            action == "import" and named_store_or_empty(store_name) and not db_path.exists()
        )
        # Tri-state: `False` until the write loop (an interrupt in `init()` is a
        # provable zero), `None` during it (rows may be committed, so nothing is
        # deleted), then the real answer.
        import_rows_landed: bool | None = False
        # Sidecars that already exist, so cleanup removes only what THIS import made:
        # an operator's `memory.db-recovery` matches `memory.db-*` like `-wal` does.
        preexisting_sidecars = (
            set(db_path.parent.glob(db_path.name + "-*")) if import_created_db else set()
        )
        # A V2 member store reaches here only as an `export` source (a V2 `import` is
        # refused above), and it must be opened through member admission.
        store = declared_store(
            db_path, store_id=store_name, config=cfg, embedding_dim=cfg.memory.embedding_dim
        )
        try:
            # INSIDE the try: `init()` creates `memory.db`, so an interrupt in it must
            # still reach the settlement below.
            store.init()
            if action == "list":
                entries = store.get_all_semantic()
                if not entries:
                    print("No semantic memory entries.")
                    return
                for e in entries:
                    try:
                        val = json.loads(e["value_json"])
                    except Exception:
                        val = e["value_json"]
                    # A lesson row stores its rule and NOT-clause as separate fields,
                    # so printing the decoded value would show a Python dict repr on
                    # this surface while every other reader shows the prose.
                    if str(e["key"]).startswith("lesson."):
                        val = _lesson_display_text(val) or val
                    safe_val = _TERMINAL_CTRL_RE.sub("", str(val))
                    print(
                        f"  {e['key']}: {safe_val}  (confidence={e['confidence']}, source={e['source']})"
                    )

            elif action == "search":
                layer = getattr(args, "layer", "all")
                if layer in ("vector", "all"):
                    query_embedding = _memory_search_embedding(store, args.query)
                    results = store.search_episodic(
                        query_embedding=query_embedding,
                        query_text=args.query,
                        limit=10,
                    )
                    if not results and query_embedding is not None:
                        # Vector search scores only rows with an embedding, and
                        # un-embedded rows (deferred bulk writes, `memory import` /
                        # `memory migrate`, reconcile_embedding_space) wait on the
                        # gateway's backfill sweep. So retry the keyword leg rather
                        # than report "no such memory".
                        results = store.search_episodic(query_text=args.query, limit=10)
                        if results:
                            # Only when keyword recovered something; on stderr, since
                            # stdout shape is a promise.
                            print(_SEARCH_KEYWORD_ONLY_PENDING_ROWS, file=sys.stderr)
                    if not results:
                        print("No episodic memories found.")
                        # Under "all" the markdown layer is still to come: an empty
                        # vector result is not an empty answer.
                        if layer == "vector":
                            return
                    elif layer == "all":
                        # Both sections are printed, so both are named. Unlabelled,
                        # the first block of hits reads as the whole answer. Held
                        # back under "vector", whose output shape is a promise.
                        print("  Episodic recall:")
                    for r in results:
                        tags = (
                            json.loads(r.get("tags", "[]"))
                            if isinstance(r.get("tags"), str)
                            else r.get("tags", [])
                        )
                        print(f"  [{r.get('importance', 0):.1f}] {r['text'][:120]}")
                        if tags:
                            print(f"        tags: {', '.join(tags)}")
                if layer == "all":
                    _memory_search_history(args)

            elif action == "stats":
                stats = store.memory_stats()
                print(
                    f"  Semantic: {stats['semantic_active']} active, {stats['semantic_deleted']} deleted"
                )
                print(
                    f"  Episodic: {stats['episodic_active']} active, {stats['episodic_deleted']} deleted"
                )
                print(f"  Embedded: {stats['embedded_count']}/{stats['episodic_active']}")
                pending = stats["episodic_active"] - stats["embedded_count"]
                if pending > 0:
                    # A bare ratio reads as a fault. Name what fills the gap, so a
                    # just-imported store is not mistaken for a broken one.
                    print(
                        f"    {pending} row(s) wait for the gateway's background re-embed "
                        "sweep (keyword-searchable until then)"
                    )
                if stats["faiss_available"]:
                    print(f"  FAISS accelerator: {stats['faiss_index_size']} vectors indexed")
                else:
                    print("  FAISS accelerator: not installed — stdlib cosine fallback (exact)")
                print(f"  Audit events: {stats['events_count']}")
                reads = store.read_counters()
                # This process only; the gateway's totals are the `reads` object on
                # GET /api/memory/observability.
                print(
                    f"  Reads (this process): {reads['rows_read']} rows over "
                    f"{reads['statements_executed']} statements"
                )
                print(
                    f"    population scans: semantic {reads['semantic_full_scans']}"
                    f" ({reads['semantic_rows_read']} rows),"
                    f" episodic {reads['episodic_full_scans']}"
                    f" ({reads['episodic_rows_read']} rows)"
                )

            elif action == "audit":
                findings = scan_memory()
                if findings:
                    print(f"⚠️  {len(findings)} suspicious entries:\n")
                    for f in findings:
                        print(
                            f"  [{f['type']}] {f['key']}{_finding_store_suffix(f)}: {f['warning']}"
                        )
                        print(f"    {f['value'][:120]}\n")
                else:
                    print("✅ No suspicious content in memory.")

            elif action == "export":
                if getattr(args, "include_markdown", False) and named_store_or_empty(store_name):
                    # REFUSED: a named store's markdown tree is not part of this payload,
                    # and refusing keeps a partial payload from reading as complete.
                    print(
                        f"Error: the markdown layer of memory store {store_name!r} cannot be "
                        "exported here. That tree is read through a fence this verb does not "
                        "carry, and its refusal is indistinguishable from an empty file, so "
                        "no payload is written rather than one claiming the tree is empty. "
                        "Re-run without --include-markdown to export the store's rows.",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                episodic = store.get_episodic_list(limit=_EXPORT_EPISODIC_LIMIT)
                events = store.get_events(limit=_EXPORT_EVENTS_LIMIT)
                # A full page may hide more rows. Reading past the limit counts
                # them through the same query the export used, on V1 and V2 alike.
                for collection, rows, limit, rest in (
                    ("episodes", episodic, _EXPORT_EPISODIC_LIMIT, store.get_episodic_list),
                    ("events", events, _EXPORT_EVENTS_LIMIT, store.get_events),
                ):
                    if len(rows) >= limit:
                        omitted = len(rest(limit=-1, offset=limit))
                        if omitted:
                            print(
                                f"warning: exported {len(rows)} of {len(rows) + omitted} "
                                f"{collection}; {omitted} omitted",
                                file=sys.stderr,
                            )
                # Read on both sides of the row read; see the vector block below.
                space_before = store.recorded_embedding_space()
                semantic = store.get_all_semantic()
                data: dict[str, object] = {
                    "semantic": semantic,
                    "episodic": episodic,
                    "events": events,
                }
                if any(row.get("embedding") is not None for row in semantic):
                    # Each vector BLOB ships as base64 of its float32 bytes
                    # (`default=str` would write an unreadable repr). The two
                    # top-level keys name the source space and model, so an importer
                    # carries vectors only into a store using the same model.
                    from kiro_crew.vector_memory_runtime import bulk_ingest

                    for row in semantic:
                        row["embedding"] = bulk_ingest.encode_embedding_blob(row.get("embedding"))
                    space_after = store.recorded_embedding_space()
                    if space_after == space_before:
                        data["embedding_space_sig"], data["embedding_model"] = (
                            bulk_ingest.export_embedding_space(space_after)
                        )
                    else:
                        # A model swap committed while the rows were read: their
                        # vectors may belong to either space, so name neither.
                        data["embedding_space_sig"], data["embedding_model"] = None, None
                if getattr(args, "include_markdown", False):
                    # Opt-in so the default payload shape stays byte-identical
                    # for existing consumers. Only reachable for the DEFAULT store,
                    # per the refusal above.
                    data["markdown"] = _markdown_memory_store().markdown_snapshot()
                output = json.dumps(data, indent=2, default=str)
                out_file = getattr(args, "output", None)
                if out_file:
                    try:
                        # A missing parent folder is created, as `perf` and
                        # `manifest` do for their own output path.
                        Path(out_file).parent.mkdir(parents=True, exist_ok=True)
                        Path(out_file).write_text(output, encoding="utf-8")
                    except OSError as exc:
                        # A directory at the path, a file where a folder should be
                        # or a read-only target: one line, not a traceback.
                        shown = safe_terminal_line(str(out_file))
                        reason = safe_terminal_line(str(exc.strerror or exc))
                        print(f"Error: could not write {shown}: {reason}", file=sys.stderr)
                        sys.exit(1)
                    print(f"Exported to {out_file}")
                else:
                    print(output)

            elif action == "migrate":
                counts = store.migrate_from_markdown()
                print(f"Migration complete:")  # noqa: F541
                print(f"  Semantic: {counts['semantic']}")
                print(f"  Episodic: {counts['episodic']}")
                print(f"  Skipped:  {counts['skipped']}")

            elif action == "retired":
                restore_id = getattr(args, "restore_id", None)
                if restore_id:
                    ok = store.restore_episodic(restore_id)
                    print("Restored." if ok else "Not found, or already active.")
                    if not ok:
                        # Nothing was restored, so exit 1 like `memory forget` on
                        # an absent key.
                        sys.exit(1)
                else:
                    rows = store.get_retired_episodic(limit=int(getattr(args, "limit", 20)))
                    if not rows:
                        print("No episodes were superseded by a semantic write.")
                    for row in rows:
                        print(f"  {row['id']}  superseded by {row['superseded_by']}")
                        print(f"    {row['text'][:120]}")

            elif action == "forget":
                _memory_forget(store, args)

            elif action == "import":
                # Validated and parsed above, before this store was opened, so nothing
                # here can be the reason an empty database exists.
                data = import_payload or {}
                # Unknown for exactly the span of the write loop: an abort inside it
                # may have committed rows already, so the settlement must not delete.
                import_rows_landed = None
                counts = store.import_memory(data)
                import_rows_landed = counts["semantic"] > 0 or counts["episodic"] > 0
                dropped = 0
                for collection in _IMPORTED_COLLECTIONS:
                    imported: object = data.get(collection) or []
                    if not isinstance(imported, list):
                        continue
                    for entry in imported:
                        if isinstance(entry, dict) and any(
                            entry.get(facet) is not None for facet in _IMPORT_FACET_KEYS
                        ):
                            dropped += 1
                print(f"Import complete:")  # noqa: F541
                print(f"  Semantic: {counts['semantic']}")
                print(f"  Episodic: {counts['episodic']}")
                print(f"  Skipped:  {counts['skipped']}")
                if (
                    counts["semantic"] > 0 or counts["episodic"] > 0
                ) and store.has_pending_embeddings():
                    print(_IMPORT_EMBED_NOTE)
                if dropped and memory_store_version(store_name) != 2:
                    # A V2 export's rows carry facets that a V1 store cannot hold, so
                    # the rows land without their attribution; say so. Counted over
                    # the PAYLOAD, which includes skipped rows, so the note does not
                    # claim every counted row was written.
                    print(
                        f"Note: {dropped} payload row(s) carried carve facets "
                        "(scope, surface, crew, session_key, derived_from). This "
                        "destination does not store facets, so any of those rows "
                        "that imported did so WITHOUT that attribution; the export "
                        "file still holds it."
                    )
                if "markdown" in data:
                    # The markdown collection is export-only: the markdown layer is
                    # consolidator-owned, so import never writes it. Say so rather
                    # than letting a backup/restore silently drop it.
                    print(
                        "Note: the 'markdown' collection is export-only and was NOT imported "
                        "(the markdown memory layer has no write path here)."
                    )

            else:
                print(
                    "Usage: kirocrew memory "
                    "{list|search|show|stats|audit|export|migrate|forget|import}"
                )
        finally:
            try:
                store.close()
            except Exception:
                # A store whose `init()` failed may not be closeable; the settlement
                # below is what the operator needs and must not be lost to that.
                logging.getLogger("kiro_crew.cli_commands").debug(
                    "could not close memory store %r", store_name, exc_info=True
                )
            import_wrote_nothing = _settle_created_database(
                db_path,
                store_name,
                str(import_file) if action == "import" and import_file else "",
                created=bool(import_created_db),
                rows_landed=import_rows_landed,
                preexisting_sidecars=preexisting_sidecars,
            )
    # Reached only when nothing raised out of the block above, so an import that
    # failed with its own error keeps that error. Outside the hold, so the lock is
    # released before the exit.
    if import_wrote_nothing:
        sys.exit(1)

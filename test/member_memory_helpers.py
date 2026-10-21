"""Synthetic member SQLite stores and ordinarily authenticated dashboard requests.

Fixtures are imported and rebound explicitly by each consumer; this module never
allocates state at import time or accesses the real crew home.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import shutil
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock
from urllib.parse import urlencode

import pytest
from aiohttp import streams, web
from aiohttp.test_utils import make_mocked_request
from tmpdir_helpers import SHORT_TMP_PREFIX, short_tmp_base

from kiro_crew import memory_stores
from kiro_crew.atomic_write import on_event_loop
from kiro_crew.config import loader
from kiro_crew.context import ContextBuilder
from kiro_crew.dashboard.handlers import cron
from kiro_crew.dashboard.handlers._shared import markdown_memory_for_store
from kiro_crew.memory import MemoryStore
from kiro_crew.vector_memory import VectorMemoryStore, create_member_database, open_member_database

#: A credential-shaped token for "sensitive document" fixtures.
DOCUMENT_CREDENTIAL = "AKIAIOSFODNN7EXAMPLE"

MEMBERS = ("alice", "bob")


# ── on-disk store shape ──────────────────────────────────────────────────────────


def member_manifest(owner: str, *, memory_version: int = 2) -> dict[str, Any]:
    """The configured stable owner and database format for a synthetic store."""
    return {"memory_version": memory_version, "owner_member": owner, "owner_member_id": owner}


# ── member-database template cache ──────────────────────────────────────────────
#
# create_member_database does a FULL-sync WAL commit (member-store durability,
# vector_memory_lifecycle.py keeps synchronous=FULL on purpose). The shared env
# fixture provisions three member DBs per test, and on a loaded Windows backend
# shard that per-test fsync can exceed the job's pytest timeout during setup. The
# written bytes are identical whenever a given (owner, store_id) is created, so
# each one is built ONCE per process and file-copied per test.
#
# Per PROCESS is per XDIST WORKER: every worker is its own interpreter, so this
# module global is private to the worker and never shared across them. It is built
# lazily on first use, never at import, so the module keeps its "no state at
# import time" contract. A created member DB is self-contained after close: its
# WAL is checkpointed and the -wal/-shm sidecars are gone, so copying memory.db
# alone reproduces the whole store (test_member_memory_helpers.py pins this). The
# copy is a fresh file per test, so no two tests ever share a handle or a path.

_TEMPLATE_ROOT: Path | None = None
_TEMPLATE_LOCK = threading.Lock()
_TEMPLATES: dict[tuple[str, str], Path] = {}


def _template_db(owner: str, store_id: str) -> Path:
    """A per-process cached member-DB template for *(owner, store_id)*; build-once.

    The identity ``create_member_database`` writes is exactly ``(owner, store_id)``
    (``read_member_database_identity`` reads it back), so that pair is the cache
    key: two provisionings with the same identity are byte-equivalent and share
    one template, while a different name or owner gets its own.
    """
    global _TEMPLATE_ROOT
    key = (owner, store_id)
    with _TEMPLATE_LOCK:
        if _TEMPLATE_ROOT is None:
            _TEMPLATE_ROOT = Path(
                tempfile.mkdtemp(prefix=SHORT_TMP_PREFIX + "member-db-", dir=short_tmp_base())
            )
            atexit.register(shutil.rmtree, _TEMPLATE_ROOT, ignore_errors=True)
        template = _TEMPLATES.get(key)
        if template is None:
            template = _TEMPLATE_ROOT / f"{len(_TEMPLATES)}" / memory_stores.MEMORY_DB_FILE
            template.parent.mkdir(parents=True)
            create_member_database(template, member_id=owner, store_id=store_id)
            _TEMPLATES[key] = template
        return template


def write_member_manifest(
    directory: Path, owner: str, *, memory_version: int = 2
) -> dict[str, Any]:
    """Explicitly provision a synthetic member database and return its config.

    A V2 store's ``memory.db`` is file-copied from a per-worker template rather
    than created in place, so each test skips the FULL-sync create commit while
    still getting its own isolated database file (see the template cache above).
    """
    directory.mkdir(parents=True, exist_ok=True)
    record = member_manifest(owner, memory_version=memory_version)
    database = directory / memory_stores.MEMORY_DB_FILE
    if memory_version == 2 and not database.exists():
        shutil.copy2(_template_db(owner, directory.name), database)
    return record


def declare_v2_store(
    home: Path, name: str, owner: str | None = None, *, memory_version: int = 2
) -> Path:
    """Provision ``<home>/memory_stores/<name>/memory.db``; return its directory."""
    owner = name.removeprefix("member-") if owner is None else owner
    directory = home / "memory_stores" / name
    write_member_manifest(directory, owner, memory_version=memory_version)
    return directory


def write_member_home(home: Path, *members: str) -> dict[str, Any]:
    """Declare one V2 store per member under *home* and write the ``config.json`` naming them.

    The config carries the ``default`` store, one ``memory_stores`` entry per
    member (the stable identity record) and one agent per member bound to its store.
    Returns the config payload written.
    """
    config: dict[str, Any] = {"memory_stores": {"default": {}}, "agents": {}}
    for member in members:
        name = f"member-{member}"
        config["memory_stores"][name] = write_member_manifest(home / "memory_stores" / name, member)
        config["agents"][member] = {"memory_store": name, "member_id": member}
    (home / "config.json").write_text(json.dumps(config), encoding="utf-8")
    return config


def forget_declared_stores(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the loaded-config and declared-store memos so the next lookup re-reads the home."""
    loader._invalidate_config_cache()
    monkeypatch.setattr(memory_stores, "_DECLARED_MEMO", None)


# ── environment fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    write_member_home(tmp_path, *MEMBERS)
    forget_declared_stores(monkeypatch)
    tiers = {}
    for name in ("", "member-alice", "member-bob"):
        path = (
            tmp_path / "memory.db" if not name else tmp_path / "memory_stores" / name / "memory.db"
        )
        if name:
            tier = open_member_database(path, member_id=name.removeprefix("member-"), store_id=name)
        else:
            tier = VectorMemoryStore(db_path=path)
            tier.init()
        tiers[name] = tier
    metadata = {}
    from kiro_crew.history import ConversationLog
    from kiro_crew.member_memory_auth import bind_private_session_store

    history = ConversationLog()

    def bind_session(key, store):
        """Record *key*'s vouched execution and its ``memory_store`` metadata on disk.

        Both writes end in a tmp-file-plus-rename whose Windows sharing-violation
        retry (``atomic_write.replace_with_retry``) is a single attempt on the
        event-loop thread, so a bind issued from an ``async def`` test loses to any
        handle a scanner holds for a few milliseconds. This synchronous form is for
        the fixture body and synchronous tests; a coroutine awaits
        ``bind_session_async`` so the writes run on a worker thread, where the
        retry applies.
        """
        if on_event_loop():
            raise RuntimeError(
                "bind_session called on the event loop: "
                "an async test must `await env.bind_session_async(key, store)`"
            )
        row = {"memory_store": store}
        bind_private_session_store(key, store)
        history.update_metadata(key, row)
        metadata[key] = row

    async def bind_session_async(key, store):
        await asyncio.to_thread(bind_session, key, store)

    bind_session("dashboard:alice", "member-alice")
    conversation_log = SimpleNamespace(
        get_metadata=lambda key: metadata.get(key, {}),
        get_metadata_status=lambda key: (metadata.get(key, {}), True),
    )
    global_memory = MemoryStore(workspace=tmp_path / "global-workspace")
    global_memory.init()
    global_memory.vector_store = tiers[""]
    state = SimpleNamespace(
        owner_id="owner",
        context_builder=SimpleNamespace(
            memory=global_memory,
            conversation_log=conversation_log,
        ),
        conversation_log=conversation_log,
        sessions=None,
        consolidator=None,
        _restricted_keys=set(),
        _slots={"alice": SimpleNamespace(key="alice", is_restricted=False, blocks_reads=False)},
    )

    async def ensure(name):
        memory_stores.require_memory_store(name)
        return tiers.get(name)

    monkeypatch.setattr(ContextBuilder, "ensure_store", staticmethod(ensure))
    monkeypatch.setattr(cron, "_sel", lambda: SimpleNamespace(log_api_access=lambda **kwargs: None))
    try:
        yield SimpleNamespace(
            tiers=tiers,
            state=state,
            metadata=metadata,
            history=history,
            bind_session=bind_session,
            bind_session_async=bind_session_async,
            home=tmp_path,
        )
    finally:
        for tier in tiers.values():
            tier.close()
        loader._invalidate_config_cache()


# ── aiohttp request stand-ins ─────────────────────────────────────────────────────


def json_payload(raw: bytes) -> streams.StreamReader:
    """A readable request body; ``make_mocked_request``'s default reads as empty."""
    reader = streams.StreamReader(
        mock.Mock(_reading_paused=False), limit=len(raw) + 1, loop=asyncio.get_running_loop()
    )
    reader.feed_data(raw)
    reader.feed_eof()
    return reader


def make_request(
    state: Any,
    path: str,
    *,
    method: str | None = None,
    query: dict[str, Any] | None = None,
    body: Any = None,
    owner: bool = False,
    owner_subject: str = "owner",
    internal: bool = False,
    session: str = "dashboard:alice",
    match_info: dict[str, str] | None = None,
    attested: bool = True,
    session_token: str = "",
) -> web.Request:
    """A mocked dashboard request against *state* in one of the authenticated shapes.

    *method* defaults to POST when a *body* is given and GET otherwise. The body
    is JSON-encoded onto a readable stream with the matching content headers.
    ``owner`` publishes the cookie-path claims (the *owner_subject* identity plus
    an EMPTY app claim); ``internal`` marks the ``X-Internal-Secret`` branch;
    *session* is sent as ``X-Session-Key``.

    ``attested`` stands in for the kernel peer attestation the unix-socket
    middleware sets, which is what an internal caller declaring a session key
    arrives with in production; ``attested=False`` is the bare-header shape a
    process that merely holds the internal secret can produce. *session_token*
    sends ``X-Session-Token``, the other attestation an internal caller can
    carry.
    """
    method = method or ("POST" if body is not None else "GET")
    target = f"{path}?{urlencode(query)}" if query else path
    app = web.Application()
    app["state"] = state
    headers = {"X-Session-Key": session}
    if session_token:
        headers["X-Session-Token"] = session_token
    kwargs: dict[str, Any] = {}
    if body is not None:
        raw = json.dumps(body).encode()
        headers.update({"Content-Type": "application/json", "Content-Length": str(len(raw))})
        kwargs["payload"] = json_payload(raw)
    if match_info is not None:
        kwargs["match_info"] = match_info
    result = make_mocked_request(method, target, app=app, headers=headers, **kwargs)
    if owner:
        result["user"] = owner_subject
        result["app"] = ""
    if internal:
        result["internal_auth"] = True
        if attested:
            result["peer_verified"] = True
    return result


def request(
    env,
    *,
    body=None,
    query=None,
    owner=False,
    internal=False,
    session="dashboard:alice",
    attested=True,
    session_token="",
):
    """``make_request`` against ``env.state``: POST ``/api/memory/seed`` with a body, else GET recall."""
    path = "/api/memory/seed" if body is not None else "/api/memory/recall"
    return make_request(
        env.state,
        path,
        query=query,
        body=body,
        owner=owner,
        internal=internal,
        session=session,
        attested=attested,
        session_token=session_token,
    )


# ── route conveniences ────────────────────────────────────────────────────────────


def seed_body(*items, source="default", target="member-alice"):
    return {"source_store": source, "store": target, "items": list(items)}


async def document_store(env, store: str) -> MemoryStore:
    """The markdown ``MemoryStore`` for *store*; the global one is wired onto ``env.state``."""
    if not store:
        memory_store = MemoryStore()
        memory_store.init()
        memory_store.vector_store = env.tiers[""]
        env.state.context_builder.memory = memory_store
        return memory_store
    return await markdown_memory_for_store(env.state, store)

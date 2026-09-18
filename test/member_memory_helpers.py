"""Shared fixtures and builders for the private member-memory (v2) test surface.

Fixture visibility -- this is a PLAIN module, not a ``conftest.py``, and it is
NOT registered through ``pytest_plugins`` (``test/conftest.py`` is not the rootdir
conftest, so pytest refuses ``pytest_plugins`` there; a rootdir registration
would also publish these fixture names to the whole suite). pytest only sees a
fixture defined here from a test module that imports it, so a consumer pulls the
fixtures in by name with the alias-and-rebind idiom::

    from member_memory_helpers import env as _member_env
    from member_memory_helpers import member_proof as _member_proof
    from member_memory_helpers import request

    env = _member_env
    member_proof = _member_proof

The rebind is what pytest discovers (a module attribute whose value carries the
fixture marker registers under the attribute name), and the alias is what keeps
flake8 quiet: a test parameter named ``env`` shadowing a directly imported,
otherwise-unused ``env`` is F811, whereas shadowing a module-level assignment is
not. Plain functions (``request``, ``make_request``, ``declare_v2_store``, ...)
are imported directly.

What lives here:

* ``env`` / ``member_proof`` -- the two-member (alice, bob) V2 environment under
  ``KIROCREW_HOME=tmp_path`` with the alice dashboard session bound, and a minted
  session proof for it.
* ``write_member_home`` / ``declare_v2_store`` / ``write_member_manifest`` -- the
  on-disk shape of a V2 store (``<root>/memory_stores/<name>/member-memory.json``),
  with or without the matching ``config.json``. They bypass
  ``memory_stores.provision_member_memory`` on purpose: the tests that use them
  exercise the store contents, not the admission gate.
* ``forget_declared_stores`` -- drops the config and declared-store memos after a
  helper above has changed the home on disk.
* ``patch_private_memory_supported`` -- the sandbox-capability gate patch.
* ``make_request`` / ``request`` / ``json_payload`` -- the aiohttp
  ``make_mocked_request`` builder with a readable JSON body and the owner /
  internal / member-proof claim shapes.
* ``seed_body`` / ``document_store`` / ``DOCUMENT_CREDENTIAL`` -- seed-route and
  markdown-document conveniences used by the memory route tests.

Nothing here allocates at import time; every path derives from ``tmp_path`` and
nothing changes the working directory.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock
from urllib.parse import urlencode

import pytest
from aiohttp import streams, web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import memory_stores
from kiro_crew.config import loader
from kiro_crew.context import ContextBuilder
from kiro_crew.dashboard.handlers import cron
from kiro_crew.dashboard.handlers._shared import markdown_memory_for_store
from kiro_crew.memory import MemoryStore
from kiro_crew.vector_memory import VectorMemoryStore

#: Dotted path of the sandbox-capability gate that ``patch_private_memory_supported`` patches.
PRIVATE_EXECUTION_GATE = "kiro_crew.member_memory_auth.private_memory_execution_supported"

#: A credential-shaped token for "sensitive document" fixtures.
DOCUMENT_CREDENTIAL = "AKIAIOSFODNN7EXAMPLE"

MEMBERS = ("alice", "bob")


# ── on-disk store shape ──────────────────────────────────────────────────────────


def member_manifest(owner: str, *, memory_version: int = 2) -> dict[str, Any]:
    """The ``member-memory.json`` record for *owner*; also the ``memory_stores`` config entry."""
    return {"memory_version": memory_version, "owner_member": owner}


def write_member_manifest(
    directory: Path, owner: str, *, memory_version: int = 2
) -> dict[str, Any]:
    """Create *directory* and write its ``member-memory.json``; returns the record written."""
    directory.mkdir(parents=True, exist_ok=True)
    record = member_manifest(owner, memory_version=memory_version)
    (directory / memory_stores.MEMBER_MEMORY_MANIFEST).write_text(
        json.dumps(record), encoding="utf-8"
    )
    return record


def declare_v2_store(
    home: Path, name: str, owner: str | None = None, *, memory_version: int = 2
) -> Path:
    """Lay down ``<home>/memory_stores/<name>/member-memory.json``; returns the store directory.

    *owner* defaults to *name* without its ``member-`` prefix. Open the tier
    yourself (``VectorMemoryStore(db_path=directory / memory_stores.MEMORY_DB_FILE)``)
    so the test owns its close.
    """
    owner = name.removeprefix("member-") if owner is None else owner
    directory = home / "memory_stores" / name
    write_member_manifest(directory, owner, memory_version=memory_version)
    return directory


def write_member_home(home: Path, *members: str) -> dict[str, Any]:
    """Declare one V2 store per member under *home* and write the ``config.json`` naming them.

    The config carries the ``default`` store, one ``memory_stores`` entry per
    member (the manifest record) and one agent per member bound to its store.
    Returns the config payload written.
    """
    config: dict[str, Any] = {"memory_stores": {"default": {}}, "agents": {}}
    for member in members:
        name = f"member-{member}"
        config["memory_stores"][name] = write_member_manifest(home / "memory_stores" / name, member)
        config["agents"][member] = {"memory_store": name}
    (home / "config.json").write_text(json.dumps(config), encoding="utf-8")
    return config


def forget_declared_stores(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the loaded-config and declared-store memos so the next lookup re-reads the home."""
    loader._invalidate_config_cache()
    monkeypatch.setattr(memory_stores, "_DECLARED_MEMO", None)


def patch_private_memory_supported(monkeypatch: pytest.MonkeyPatch, value: bool = True) -> None:
    """Pin the sandbox-capability gate so private execution reads as (un)supported."""
    monkeypatch.setattr(PRIVATE_EXECUTION_GATE, lambda **kwargs: value)


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
        tier = VectorMemoryStore(db_path=path)
        tier.init()
        tiers[name] = tier
    metadata = {}
    from kiro_crew.history import ConversationLog
    from kiro_crew.member_memory_auth import bind_private_session_store

    history = ConversationLog()

    def bind_session(key, store):
        row = {"memory_store": store}
        bind_private_session_store(key, store)
        history.update_metadata(key, row)
        metadata[key] = row

    bind_session("dashboard:alice", "member-alice")
    conversation_log = SimpleNamespace(
        get_metadata=lambda key: metadata.get(key, {}),
        get_metadata_status=lambda key: (metadata.get(key, {}), True),
    )
    state = SimpleNamespace(
        owner_id="owner",
        context_builder=SimpleNamespace(
            memory=SimpleNamespace(vector_store=tiers[""]),
            conversation_log=conversation_log,
        ),
        conversation_log=conversation_log,
        sessions=None,
        consolidator=None,
        _restricted_keys=set(),
        _slots={"alice": SimpleNamespace(is_restricted=False, blocks_reads=False)},
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
            home=tmp_path,
        )
    finally:
        for tier in tiers.values():
            tier.close()
        loader._invalidate_config_cache()


@pytest.fixture
def member_proof(env, monkeypatch):
    from kiro_crew import member_memory_auth, platform_compat

    monkeypatch.setattr(platform_compat, "get_process_start_id", lambda pid: f"test-start-{pid}")
    member_memory_auth.publish_member_session_pid(
        os.getpid(), "dashboard:alice", memory_store="member-alice"
    )
    proof = member_memory_auth.issue_member_session_proof("dashboard:alice", os.getpid())
    assert proof
    return proof


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
    proof: str = "",
    match_info: dict[str, str] | None = None,
) -> web.Request:
    """A mocked dashboard request against *state* in one of the authenticated shapes.

    *method* defaults to POST when a *body* is given and GET otherwise. The body
    is JSON-encoded onto a readable stream with the matching content headers.
    ``owner`` publishes the cookie-path claims (the *owner_subject* identity plus
    an EMPTY app claim); ``internal`` marks the ``X-Internal-Secret`` branch;
    *proof* rides in ``X-Member-Session-Proof``; *session* is always sent as
    ``X-Session-Key``.
    """
    method = method or ("POST" if body is not None else "GET")
    target = f"{path}?{urlencode(query)}" if query else path
    app = web.Application()
    app["state"] = state
    headers = {"X-Session-Key": session}
    if proof:
        headers["X-Member-Session-Proof"] = proof
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
    return result


def request(
    env, *, body=None, query=None, owner=False, internal=False, session="dashboard:alice", proof=""
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
        proof=proof,
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

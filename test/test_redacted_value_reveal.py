"""The owner's reveal of a removed credential (way A).

The rule under test (RFC *Owner reveal*): redacted values may be revealed only
to the owner through the browser; no agent-reachable path may ever return them;
originals never touch disk.

Credential-shaped material is built at runtime so no literal token shape sits
in the source.
"""

from __future__ import annotations

import asyncio
import builtins
import copy
import json
import logging
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

import kiro_crew.dashboard.chat_runner as chat_runner
from kiro_crew.dashboard.handlers import redacted_reveal
from kiro_crew.security import reveal_store
from kiro_crew.security.credential_sources import bounded_credential_records

_GH = "ghp_" + "k7qmz2x9wv4tb8ncr6yd3fj5hs1gpl0aeuo2"
_SLOT = "slot-1"


class _Slot:
    key = _SLOT
    agent = "kirocrew"
    project = ""
    _app = ""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.credential_evidence = None
        self.messages: list[dict] = []
        self.segment_raw_text = ""
        self.segment_removed: dict = {}
        self.segment_shown_sha = __import__("hashlib").sha256()

    def append(self, role, content, cls="", ts="", *, meta=None, **_kw):
        row = {"role": role, "content": content, "cls": cls, "meta": meta}
        self.rows.append(row)
        return row


@pytest.fixture(autouse=True)
def _fresh_store(_floor_monkeypatch: pytest.MonkeyPatch):
    store = reveal_store.RevealStore()
    _floor_monkeypatch.setattr(reveal_store, "STORE", store)
    return store


@pytest.fixture(autouse=True)
def _quiet_sel(_floor_monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    audit = MagicMock()
    _floor_monkeypatch.setattr(chat_runner, "sel", lambda: audit)
    return audit


@pytest.fixture
def audit_rows(monkeypatch):
    """Every SEL row the reveal route writes, as the kwargs it passed."""
    rows: list[dict] = []

    class _Sel:
        def log_api_access(self, **kw):
            rows.append(kw)

    import kiro_crew.sel as sel_mod

    monkeypatch.setattr(sel_mod, "sel", lambda: _Sel())
    return rows


def _reply_with_secret() -> dict:
    slot = _Slot()
    chat_runner._append_command_reply(slot, f"your new token is {_GH}")
    (row,) = [r for r in slot.rows if r["role"] == "assistant"]
    return row


def _request(
    *,
    body=None,
    app: str | None = "",
    user: str | None = "owner-1",
    owner: str = "owner-1",
    dashboard_user: bool | None = True,
    internal: bool = False,
    headers: dict | None = None,
):
    """A request shaped the way ``token_auth`` publishes it.

    The defaults are the OWNER's browser: a cookie credential, ``app`` present
    and empty, ``is_dashboard_user`` True. Each refusal test changes one field.
    """
    req = MagicMock()
    req.path = "/api/security/redacted-value"
    store: dict = {}
    if app is not None:
        store["app"] = app
    if user is not None:
        store["user"] = user
    if dashboard_user is not None:
        store["is_dashboard_user"] = dashboard_user
    if internal:
        store["internal_auth"] = True
    req.get = lambda key, default=None: store.get(key, default)
    req.__contains__ = lambda _self, key: key in store
    req.__getitem__ = lambda _self, key: store[key]
    req.headers = headers or {}
    state = MagicMock()
    state.owner_id = owner
    req.app = {"state": state}
    req.json = AsyncMock(return_value=body if body is not None else {})
    return req


def _reveal(req):
    return asyncio.run(redacted_reveal.api_owner_value_reveal(req))


def _handle(row: dict) -> str:
    (record,) = row["meta"]["redactions"]
    return record["reveal_id"]


# ---------------------------------------------------------------- (a) the owner


def test_the_owner_reveals_a_removed_value(audit_rows) -> None:
    row = _reply_with_secret()
    assert _GH not in row["content"]
    resp = _reveal(_request(body={"slot": _SLOT, "id": _handle(row)}))
    assert resp.status == 200
    assert json.loads(resp.text) == {"value": _GH}
    assert resp.headers["Cache-Control"] == "no-store"


def test_a_handle_only_opens_in_its_own_slot() -> None:
    row = _reply_with_secret()
    resp = _reveal(_request(body={"slot": "another-slot", "id": _handle(row)}))
    assert resp.status == 410


# ---------------------------------------------------- (b) every other caller is refused


@pytest.mark.parametrize(
    "caller",
    [
        # An MCP tool / agent / crewmate / sub-agent / cron: the loopback
        # internal secret, with no app claim and no user, as token_auth leaves it.
        dict(internal=True, app=None, user=None, dashboard_user=None),
        # The same internal caller, even if every OTHER field read as the owner.
        # Only the internal-caller guard refuses this one (mutation-proven).
        dict(internal=True),
        # A request carrying the internal secret header at all.
        dict(headers={"X-Internal-Secret": "x"}),
        # An app token (an installed app's UI or its backend).
        dict(app="some-app", dashboard_user=False),
        # A dashboard user who is not the owner (a Slack teammate's !dashboard).
        dict(user="teammate-7"),
        # Anything that is not a dashboard-user credential.
        dict(dashboard_user=False),
    ],
)
def test_agents_apps_and_non_owners_are_refused(caller, audit_rows) -> None:
    row = _reply_with_secret()
    resp = _reveal(_request(body={"slot": _SLOT, "id": _handle(row)}, **caller))
    assert resp.status == 403
    assert _GH not in resp.text
    assert [r["outcome"] for r in audit_rows] == ["denied"]


def test_no_agent_reachable_name_reads_the_store() -> None:
    """The route module is the store's one reader: no MCP tool imports it."""
    import pathlib

    root = pathlib.Path(reveal_store.__file__).resolve().parents[1]
    readers = sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*.py")
        if "/tests/" not in str(p)
        and "STORE.reveal(" in p.read_text(encoding="utf-8", errors="replace")
    )
    assert readers == ["dashboard/handlers/redacted_reveal.py"]


# ------------------------------------------------- (c) nothing is written anywhere


def test_the_saved_row_holds_only_the_handle() -> None:
    row = _reply_with_secret()
    saved = json.dumps(row)
    assert _GH not in saved
    assert reveal_store.REVEAL_ID_RE.match(_handle(row))
    # The row read back keeps the handle and still never the value.
    (back,) = bounded_credential_records(json.loads(saved)["meta"]["redactions"])
    assert back["reveal_id"] == _handle(row)


def test_keeping_and_revealing_touch_no_file(monkeypatch, audit_rows) -> None:
    def _no_disk(*_a, **_kw):
        raise AssertionError("the reveal path opened a file")

    row = _reply_with_secret()
    handle = _handle(row)
    monkeypatch.setattr(builtins, "open", _no_disk)
    monkeypatch.setattr(os, "open", _no_disk)
    store = reveal_store.STORE
    assert store.stash(_GH, scope=_SLOT)
    assert store.reveal(handle, scope=_SLOT) == _GH
    assert _reveal(_request(body={"slot": _SLOT, "id": handle})).status == 200


def test_the_store_cannot_be_serialized_or_printed() -> None:
    store = reveal_store.STORE
    store.stash(_GH, scope=_SLOT)
    with pytest.raises(TypeError):
        copy.deepcopy(store)
    with pytest.raises(TypeError):
        copy.deepcopy(next(iter(store._entries.values())))
    assert _GH not in repr(store)
    assert _GH not in repr(list(store._entries.values()))


def test_no_log_line_carries_the_value(caplog, audit_rows) -> None:
    caplog.set_level(logging.DEBUG)
    row = _reply_with_secret()
    _reveal(_request(body={"slot": _SLOT, "id": _handle(row)}))
    _reveal(_request(body={"slot": _SLOT, "id": _handle(row)}, user="teammate-7"))
    assert _GH not in caplog.text


def test_only_the_saved_records_keep_a_value(_fresh_store) -> None:
    """A pass whose records are thrown away leaves nothing in the store."""
    from kiro_crew.dashboard.chat_turn import segment_flush

    slot = _Slot()
    # A pass with no pending map (the per-delta check) mints no handle at all.
    _, _, records = segment_flush._redact_segment_text(slot, f"token {_GH}")
    assert records and "reveal_id" not in records[0]
    assert len(_fresh_store) == 0
    # A whole-segment save keeps exactly the one value its one record names.
    _, _, saved = segment_flush._redact_segment(slot, f"token {_GH}")
    assert len(_fresh_store) == 1
    assert _fresh_store.reveal(saved[0]["reveal_id"], scope=_SLOT) == _GH


def test_a_flush_that_fails_closed_keeps_nothing(_fresh_store) -> None:
    from kiro_crew.dashboard.chat_turn import segment_flush

    slot = _Slot()
    slot.segment_raw_text = None  # past the raw-copy cap: the flush fails closed
    slot.segment_shown_sha = __import__("hashlib").sha256()
    slot.segment_removed = {}
    text, links, records = segment_flush._redact_segment(slot, f"token {_GH}")
    assert records == [] and _GH not in text
    assert len(_fresh_store) == 0


# ---------------------------------------------------- (d) restart and expiry


def test_after_a_restart_the_value_is_gone(monkeypatch) -> None:
    row = _reply_with_secret()
    # A new process starts with an empty store.
    monkeypatch.setattr(reveal_store, "STORE", reveal_store.RevealStore())
    resp = _reveal(_request(body={"slot": _SLOT, "id": _handle(row)}))
    assert resp.status == 410
    assert json.loads(resp.text)["code"] == "reveal_unavailable"


def test_after_the_ttl_the_value_is_gone() -> None:
    now = [1000.0]
    store = reveal_store.RevealStore(clock=lambda: now[0])
    handle = store.stash(_GH, scope=_SLOT)
    assert store.reveal(handle, scope=_SLOT) == _GH
    now[0] += reveal_store.TTL_SECONDS + 1
    assert store.reveal(handle, scope=_SLOT) is None
    assert len(store) == 0


def test_the_store_is_bounded(monkeypatch) -> None:
    monkeypatch.setattr(reveal_store, "MAX_ENTRIES", 3)
    store = reveal_store.RevealStore()
    handles = [store.stash(f"{_GH}{i}", scope=_SLOT) for i in range(5)]
    assert len(store) == 3
    assert store.reveal(handles[0], scope=_SLOT) is None
    assert store.reveal(handles[-1], scope=_SLOT) == f"{_GH}4"
    assert store.stash("x" * (reveal_store.MAX_VALUE_CHARS + 1), scope=_SLOT) is None


# --------------------------------------------------------- (e) the audit row


def test_every_reveal_is_audited_without_the_value(audit_rows) -> None:
    row = _reply_with_secret()
    handle = _handle(row)
    assert _reveal(_request(body={"slot": _SLOT, "id": handle})).status == 200
    (allowed,) = [r for r in audit_rows if r["outcome"] == "allowed"]
    assert allowed["caller"] == "owner-1"
    assert allowed["operation"] == "redacted_value.reveal"
    assert handle in allowed["resources"]
    assert allowed["critical"] is True
    assert all(_GH not in json.dumps(r) for r in audit_rows)


def test_no_audit_means_no_value(monkeypatch) -> None:
    row = _reply_with_secret()
    monkeypatch.setattr(redacted_reveal, "_audit", lambda **_kw: False)
    resp = _reveal(_request(body={"slot": _SLOT, "id": _handle(row)}))
    assert resp.status == 503
    assert _GH not in resp.text


@pytest.mark.parametrize(
    "body",
    [{}, {"slot": _SLOT}, {"slot": _SLOT, "id": "rv_short"}, {"slot": "", "id": "x"}, []],
)
def test_a_malformed_body_is_refused(body) -> None:
    assert _reveal(_request(body=body)).status == 400

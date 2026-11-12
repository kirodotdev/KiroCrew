"""The owner's engine selector: the structured detectors (default) or main's legacy passes.

It lives in the same keystone record as the credential-redaction switch
(``security.redaction_switch``) and is separate from it: the switch decides whether
the owner's Files view is redacted at all, the selector decides which engine
redacts everywhere. ``compat.use_rule_table()`` reads the selector.

Every credential here is synthetic and assembled at run time.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from redaction_structured_cases import aws_key_id

from kiro_crew.security import redaction_switch
from kiro_crew.security.redaction import redact_credentials
from kiro_crew.security.redaction_structured import compat


@pytest.fixture(autouse=True)
def store(tmp_path, _floor_monkeypatch) -> Iterator:
    """A tmp keystone; the engine choice is re-read from it and restored after."""
    path = tmp_path / "credential_redaction.json"
    _floor_monkeypatch.setattr(redaction_switch, "_path", lambda: path, raising=True)
    before = compat.use_rule_table()
    compat.reload_engine_choice()
    try:
        yield path
    finally:
        compat.publish_use_rule_table(before)


def _request(body):
    """A request shaped like a real dashboard OWNER call (as test_credential_redaction_switch)."""
    req = MagicMock()
    req.path = "/api/security/credential-redaction"
    values = {"app": "", "user": "owner-1"}
    req.get = lambda key, default=None: values.get(key, default)
    req.__contains__ = lambda _self, key: key in values
    req.__getitem__ = lambda _self, key: values[key]
    state = MagicMock()
    state.owner_id = "owner-1"
    req.app = {"state": state}
    req.json = AsyncMock(return_value=body)
    return req


@pytest.fixture
def audit(monkeypatch):
    from kiro_crew.dashboard.handlers import credential_redaction as handlers

    events: list[tuple[str, bool]] = []

    def record(*, outcome, caller, detail="", critical=False, operation=None):
        events.append((operation or outcome, critical))
        return True

    monkeypatch.setattr(handlers, "_audit", record, raising=True)
    return events


# ── the record ──────────────────────────────────────────────────────────────


def test_no_record_selects_the_structured_engine(store):
    assert not store.exists()
    assert redaction_switch.read_state().engine == redaction_switch.ENGINE_STRUCTURED
    assert compat.reload_engine_choice() is True
    assert compat.use_rule_table() is True


def test_an_old_record_without_the_field_selects_the_structured_engine(store):
    store.write_text(json.dumps({"enabled": False, "changed_at": "t0"}), encoding="utf-8")
    assert redaction_switch.read_engine() == redaction_switch.ENGINE_STRUCTURED
    assert compat.reload_engine_choice() is True


@pytest.mark.parametrize("raw", ["not json", "[]", '{"engine": "LEGACY"}', '{"engine": 0}'])
def test_anything_but_the_word_legacy_selects_the_structured_engine(store, raw):
    store.write_text(raw, encoding="utf-8")
    assert redaction_switch.read_engine() == redaction_switch.ENGINE_STRUCTURED


def test_legacy_when_chosen(store):
    redaction_switch.set_engine(redaction_switch.ENGINE_LEGACY, changed_at="t1")
    assert redaction_switch.read_engine() == redaction_switch.ENGINE_LEGACY
    assert compat.reload_engine_choice() is False
    assert compat.use_rule_table() is False
    # Main's passes run alone: an AWS key ID is removed again.
    key_id = aws_key_id(seed=95)
    assert key_id not in redact_credentials(f"id {key_id} end")[0]
    compat.publish_use_rule_table(True)
    assert redact_credentials(f"id {key_id} end")[0] == f"id {key_id} end"


def test_the_choice_round_trips_and_keeps_the_switch(store):
    redaction_switch.set_enabled(False, changed_at="t0")
    redaction_switch.set_seams_enabled(False, changed_at="t0s")
    redaction_switch.set_engine(redaction_switch.ENGINE_LEGACY, changed_at="t1")
    raw = json.loads(store.read_text(encoding="utf-8"))
    assert raw["enabled"] is False and raw["engine"] == "legacy"
    # The engine has its own time; neither switch's "off since" moves.
    assert raw["changed_at"] == "t0" and raw["seams_changed_at"] == "t0s"
    assert raw["seams_enabled"] is False and raw["engine_changed_at"] == "t1"
    redaction_switch.set_enabled(True, changed_at="t2")
    redaction_switch.set_seams_enabled(True, changed_at="t2s")
    assert redaction_switch.read_engine() == redaction_switch.ENGINE_LEGACY
    redaction_switch.set_engine(redaction_switch.ENGINE_STRUCTURED, changed_at="t3")
    assert json.loads(store.read_text(encoding="utf-8"))["enabled"] is True
    assert redaction_switch.read_engine() == redaction_switch.ENGINE_STRUCTURED


def test_set_engine_refuses_an_unknown_engine(store):
    with pytest.raises(ValueError):
        redaction_switch.set_engine("regex", changed_at="t")
    assert not store.exists()


# ── the dashboard route ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_reports_the_engine(store, audit):
    from kiro_crew.dashboard.handlers.credential_redaction import api_credential_redaction_get

    resp = await api_credential_redaction_get(_request(None))
    assert json.loads(resp.text)["engine"] == "structured"


@pytest.mark.asyncio
async def test_put_engine_records_audits_and_takes_effect(store, audit):
    from kiro_crew.dashboard.handlers.credential_redaction import api_credential_redaction_put

    req = _request({"engine": "legacy"})
    resp = await api_credential_redaction_put(req)
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["engine"] == "legacy" and body["enabled"] is True
    assert json.loads(store.read_text(encoding="utf-8"))["engine"] == "legacy"
    assert ("engine_legacy", True) in audit
    assert compat.use_rule_table() is False
    msg_type, payload = req.app["state"].broadcast_ws_owners.call_args.args
    assert msg_type == "credential_redaction_changed" and payload["engine"] == "legacy"

    resp = await api_credential_redaction_put(_request({"engine": "structured"}))
    assert resp.status == 200
    assert compat.use_rule_table() is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [{"engine": "regex"}, {"engine": True}, {"engine": "legacy", "enabled": True}],
)
async def test_put_refuses_a_bad_engine_body(store, audit, body):
    from kiro_crew.dashboard.handlers.credential_redaction import api_credential_redaction_put

    resp = await api_credential_redaction_put(_request(body))
    assert resp.status == 400
    assert not store.exists()
    assert compat.use_rule_table() is True


@pytest.mark.asyncio
async def test_choosing_legacy_whose_audit_cannot_be_written_does_not_happen(store, monkeypatch):
    from kiro_crew.dashboard.handlers import credential_redaction as handlers

    monkeypatch.setattr(handlers, "_audit", lambda **kw: False, raising=True)
    resp = await handlers.api_credential_redaction_put(_request({"engine": "legacy"}))
    assert resp.status == 503
    assert not store.exists()
    assert compat.use_rule_table() is True


@pytest.mark.asyncio
async def test_an_engine_write_that_fails_is_audited_as_failed(store, audit, monkeypatch):
    """The engine row is written before the record; a write that then fails leaves a
    ``write_failed`` row after it, as the switch writers do, and changes nothing."""
    from kiro_crew.dashboard.handlers import credential_redaction as handlers

    def refuse(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr(redaction_switch, "set_engine", refuse, raising=True)
    resp = await handlers.api_credential_redaction_put(_request({"engine": "legacy"}))
    assert resp.status == 500
    assert audit == [("engine_legacy", True), ("write_failed", True)]
    assert compat.use_rule_table() is True


@pytest.mark.asyncio
async def test_the_engine_is_published_inside_the_write_transaction(store, audit, monkeypatch):
    """Two overlapping engine PUTs publish in the order they were written.

    The publish runs under the same store lock as the write, so a PUT that wrote
    first cannot publish after one that wrote second and leave the gateway on the
    older engine while the record (and the GET) say the newer one.
    """
    from kiro_crew.dashboard.handlers import credential_redaction as handlers

    seen: list[tuple[bool, bool]] = []
    real = compat.publish_use_rule_table

    def spy(enabled: bool) -> None:
        seen.append((bool(enabled), redaction_switch._STORE_LOCK._is_owned()))
        real(enabled)

    monkeypatch.setattr(compat, "publish_use_rule_table", spy, raising=True)
    resp = await handlers.api_credential_redaction_put(_request({"engine": "legacy"}))
    assert resp.status == 200
    assert seen == [(False, True)]

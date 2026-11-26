"""A launch-approval store whose bytes are not UTF-8 approves nothing.

Windows PowerShell 5.1 writes UTF-16LE with a byte-order mark for ``>`` and
``Out-File``, so a hand-edited store can stop decoding. The authorization reads
must answer as for any other unreadable store: no approval, so a pooled acquire
takes the fallback-eligible no-target path. The writers must not read it as
empty and rewrite it. The stub's env sidecar reader had the same read shape.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging

import pytest
from mcp_launch_approval_helpers import (  # noqa: F401 -- the fixture is requested by name
    _APPROVED_CMD,
    _approval_module,
    _approvals_for,
    _declare,
    _pool_key,
    _stub_request,
    _target_spec,
    _toggle_on,
    toggle_home_fixture,
)

#: Opts this module out of the conftest fixture that approves every gatewayd launch.
ENFORCE_LAUNCH_APPROVAL = True


@contextlib.contextmanager
def _answers_without_raising():
    """A reader documented to answer for a malformed file must not raise on one."""
    try:
        yield
    except UnicodeDecodeError as exc:
        pytest.fail(
            f"raised {type(exc).__name__} ({exc.reason}) instead of answering", pytrace=False
        )


def _encodings(mod):
    text = mod._document(mod.LaunchApprovals(), {})
    return {
        "utf-16-le-bom": b"\xff\xfe" + text.encode("utf-16-le"),
        "utf-16-be-bom": b"\xfe\xff" + text.encode("utf-16-be"),
        "cp1252-byte": text.replace("{", '{"note": "caf\xe9", ', 1).encode("cp1252"),
    }


@pytest.fixture(params=["utf-16-le-bom", "utf-16-be-bom", "cp1252-byte"])
def undecodable_store(request, tmp_path, monkeypatch):
    mod = _approval_module()
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    store = mod.approvals_path()
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_bytes(_encodings(mod)[request.param])
    return mod, store


def test_an_undecodable_store_approves_nothing_and_says_why(undecodable_store, caplog):
    mod, _store = undecodable_store

    with caplog.at_level(logging.WARNING, logger=mod.logger.name), _answers_without_raising():
        approvals = mod.load_approvals()

    assert approvals.approved == {} and approvals.approved_pairs == {}
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "not valid UTF-8" in warnings[0]


def test_launch_approved_is_false_for_an_undecodable_store(undecodable_store):
    mod, _store = undecodable_store

    with _answers_without_raising():
        approved = mod.launch_approved("srv", "a" * 64, "b" * 64)

    assert approved is False


@pytest.mark.asyncio
async def test_a_pooled_acquire_over_an_undecodable_store_takes_the_no_target_path(
    undecodable_store, monkeypatch
):
    from kiro_crew.mcp_gateway import gatewayd
    from kiro_crew.mcp_gateway.daemon.admission_protocol import _classify_rejection
    from kiro_crew.mcp_gateway.pool import BackendPool

    monkeypatch.setenv("KIROCREW_MCP_TARGET_SRV", _target_spec(_APPROVED_CMD))

    with _answers_without_raising(), pytest.raises(gatewayd._TargetUnknown) as raised:
        await gatewayd._acquire_backend(
            BackendPool(max_backends=1), _pool_key("srv"), gatewayd.env_target_resolver
        )

    assert _classify_rejection(raised.value, exclusive=False) is not None


@pytest.mark.parametrize("writer", ["approve", "revoke", "save_pass", "restore"])
def test_a_writer_leaves_an_undecodable_store_untouched(undecodable_store, writer):
    mod, store = undecodable_store
    before = hashlib.sha256(store.read_bytes()).hexdigest()
    snapshot = mod.ApprovalSnapshot({"srv"}, {}, {}, {}, {})
    calls = {
        "approve": lambda: mod.approve({"srv": []}),
        "revoke": lambda: mod.revoke(["srv"]),
        "save_pass": lambda: mod.save_pass(mod.LaunchApprovals()),
        "restore": lambda: mod.restore(snapshot),
    }

    with pytest.raises(mod.ApprovalStoreUnreadable) as raised:
        calls[writer]()

    assert isinstance(raised.value, OSError)

    assert hashlib.sha256(store.read_bytes()).hexdigest() == before


def test_a_utf8_store_still_loads_and_doctor_still_names_the_bad_bytes(tmp_path):
    mod = _approval_module()
    good = tmp_path / "good.json"
    approved = _approvals_for(mod, "srv", _APPROVED_CMD, [])
    good.write_text(mod._document(approved, {}), encoding="utf-8")
    bad = tmp_path / "bad.json"
    bad.write_bytes(_encodings(mod)["utf-16-le-bom"])

    loaded = mod.load_approvals(good)
    assert loaded.approved == approved.approved
    assert loaded.approved_pairs == approved.approved_pairs
    assert mod.approvals_store_problem(good) is None
    assert mod.approvals_store_problem(bad) == "not valid UTF-8"


def test_a_stub_env_file_that_does_not_decode_drops_the_env_block(tmp_path, caplog):
    """Same read shape on the stub side: ``{}`` on a malformed sidecar, not a raise."""
    from kiro_crew.mcp_gateway import stub_register

    sidecar = tmp_path / "env.json"
    sidecar.write_bytes(b"\xff\xfe" + '{"K": "v"}'.encode("utf-16-le"))

    with caplog.at_level(logging.WARNING, logger=stub_register.logger.name):
        with _answers_without_raising():
            env = stub_register._parse_env_file(str(sidecar))

    assert env == {}
    assert any("dropping env block" in r.getMessage() for r in caplog.records)


def _audit(monkeypatch, mcp_mod) -> list[dict]:
    events: list[dict] = []
    monkeypatch.setattr(
        mcp_mod,
        "sel",
        lambda: type("_Sel", (), {"log_api_access": lambda _self, **kw: events.append(kw)})(),
    )
    return events


def _undecode(store) -> None:
    store.write_bytes(b"\xff\xfe" + store.read_text(encoding="utf-8").encode("utf-16-le"))


@pytest.mark.asyncio
async def test_stub_on_over_an_undecodable_store_writes_nothing_and_answers_503(
    tmp_path, toggle_home, monkeypatch
):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    mod = _approval_module()
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    store = mod.approvals_path()
    store.parent.mkdir(parents=True, exist_ok=True)
    mod._write(store, mod._document(mod.LaunchApprovals(), {}))
    _undecode(store)
    config = tmp_path / "config.json"
    mcp_mod.KiroCrewConfig.load()
    before_config, before_store = config.read_bytes(), store.read_bytes()
    events = _audit(monkeypatch, mcp_mod)

    status, body = await _toggle_on()

    assert (status, body["code"]) == (503, "approval_write_failed")
    assert config.read_bytes() == before_config
    assert store.read_bytes() == before_store
    assert ("mcp_stub_rejected_approval_write_failed", "denied") in [
        (event["operation"], event["outcome"]) for event in events
    ]


@pytest.mark.asyncio
async def test_stub_off_over_an_undecodable_store_writes_nothing_and_answers_503(
    tmp_path, toggle_home, monkeypatch
):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    mod = _approval_module()
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    assert (await _toggle_on())[0] == 200
    store = mod.approvals_path()
    _undecode(store)
    config = tmp_path / "config.json"
    before_config, before_store = config.read_bytes(), store.read_bytes()
    events = _audit(monkeypatch, mcp_mod)

    response = await mcp_mod.api_mcp_gateway_set_stub(_stub_request({"name": "srv", "stub": False}))

    assert response.status == 503
    assert json.loads(response.body)["code"] == "approval_write_failed"
    assert config.read_bytes() == before_config
    assert store.read_bytes() == before_store
    assert ("mcp_stub_rejected_approval_write_failed", "denied") in [
        (event["operation"], event["outcome"]) for event in events
    ]


@pytest.mark.asyncio
async def test_a_restore_the_store_refuses_keeps_the_config_failure_answer(
    toggle_home, monkeypatch
):
    import kiro_crew.config.loader as loader_mod
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    mod = _approval_module()
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    assert (await _toggle_on())[0] == 200

    def _locked(*args, **kwargs):
        raise OSError("locked")

    def _refused(snapshot):
        raise mod.ApprovalStoreUnreadable("approvals.json is not valid UTF-8")

    monkeypatch.setattr(loader_mod, "update_config_locked", _locked)
    monkeypatch.setattr(mcp_mod, "restore", _refused)

    response = await mcp_mod.api_mcp_gateway_set_stub(_stub_request({"name": "srv", "stub": False}))

    assert response.status == 503
    assert json.loads(response.body)["code"] == "config_lock_failed"

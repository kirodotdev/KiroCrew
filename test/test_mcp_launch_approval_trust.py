"""The "Always trust changes to this server" choice, per server, set only in the dashboard.

A server approved with the box checked keeps a changed launch shared at the next
gateway start; one approved without it asks again, as before. Only the approve
press with a displayed launch can set it: a card (``expected_entry``), a press
with no launch, an agent-declared key and a rewrite pass set nothing. Stopping
sharing, and a complete full pass that does not find the server, drop it; a
pass served from cache keeps it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from mcp_launch_approval_helpers import (  # noqa: F401 -- the fixture is requested by name
    _APPROVED_CMD,
    _approval_module,
    _declare,
    _next_boot_rewrite,
    _stub_request,
    _targets_running,
    toggle_home_fixture,
)

from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, until_parked

#: Opts this module out of the conftest fixture that approves every gatewayd launch.
ENFORCE_LAUNCH_APPROVAL = True


async def _press(body_extra: dict, *, with_launch: bool = True):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    body = {"name": "srv", "stub": True, **body_extra}
    if with_launch:
        launches = await asyncio.to_thread(
            mcp_mod.launch_resolve.resolve_launches, ["srv"], refresh_specs=False
        )
        body["expected_launch"] = mcp_mod.display_launches("srv", launches["srv"])[
            "expected_launch"
        ]
    resp = await mcp_mod.api_mcp_gateway_set_stub(_stub_request(body))
    return resp.status, json.loads(resp.body)


def _trusted() -> set[str]:
    return _approval_module().load_approvals().trusted


@pytest.mark.asyncio
async def test_a_trusted_server_keeps_a_changed_launch_shared(toggle_home, tmp_path):
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    status, _ = await _press({"trust_changes": True})
    assert status == 200
    assert _trusted() == {"SRV"}

    _declare(toggle_home, "agent spec", _APPROVED_CMD, ["-V"])
    target_env, approvals = _next_boot_rewrite(tmp_path, toggle_home)

    assert approvals.refused == {}
    assert _targets_running(target_env, _APPROVED_CMD)


@pytest.mark.asyncio
async def test_an_untrusted_server_asks_again_after_a_change(toggle_home, tmp_path):
    mod = _approval_module()
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    status, _ = await _press({})
    assert status == 200
    assert _trusted() == set()

    _declare(toggle_home, "agent spec", _APPROVED_CMD, ["-V"])
    target_env, approvals = _next_boot_rewrite(tmp_path, toggle_home)

    assert approvals.refused.get("SRV") == mod.REFUSED_CHANGED
    assert not _targets_running(target_env, _APPROVED_CMD)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra, with_launch",
    [
        ({"trust_changes": True, "expected_entry": {"command": _APPROVED_CMD}}, True),
        ({"trust_changes": True}, False),
        ({"trust_changes": "yes"}, True),
    ],
)
async def test_only_the_approve_press_can_set_trust(toggle_home, extra, with_launch):
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    status, body = await _press(extra, with_launch=with_launch)

    assert status == 400
    assert body["code"] == "trust_changes_invalid"
    assert _trusted() == set()


@pytest.mark.asyncio
async def test_an_agent_written_trust_key_sets_nothing(toggle_home, tmp_path):
    """An agent can write the server's entry, not the approval store: a
    ``trust_changes`` key in a spec, and a rewrite pass over it, trust nothing."""
    mod = _approval_module()
    entry = {"command": _APPROVED_CMD, "args": [], "trust_changes": True}
    (toggle_home / "a.json").write_text(
        json.dumps({"name": "a", "tools": ["*"], "mcpServers": {"srv": entry}})
    )
    status, _ = await _press({})
    assert status == 200

    _target_env, approvals = _next_boot_rewrite(tmp_path, toggle_home)
    mod.save_pass(approvals)

    assert _trusted() == set()


@pytest.mark.asyncio
async def test_stopping_sharing_drops_trust(toggle_home):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    await _press({"trust_changes": True})
    assert _trusted() == {"SRV"}

    resp = await mcp_mod.api_mcp_gateway_set_stub(_stub_request({"name": "srv", "stub": False}))

    assert resp.status == 200
    assert _trusted() == set()


@pytest.mark.asyncio
async def test_a_server_no_longer_declared_loses_trust(toggle_home, tmp_path):
    """A removed server keeps its name on the share list; one added again under
    that name must not inherit the choice."""
    mod = _approval_module()
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    await _press({"trust_changes": True})
    (toggle_home / "a.json").write_text(json.dumps({"name": "a", "tools": ["*"], "mcpServers": {}}))

    _target_env, approvals = _next_boot_rewrite(tmp_path, toggle_home)
    mod.save_pass(approvals)

    assert _trusted() == set()


def test_the_store_reads_trust_only_as_true(tmp_path: Path):
    mod = _approval_module()
    store = tmp_path / "approvals.json"
    record = {"name": "srv", "fingerprints": ["a:b"], "pairs": ["a:b"]}
    for value, trusted in ((True, {"SRV"}), ("true", set()), (1, set())):
        doc = {"version": 1, "servers": {"SRV": {**record, "trust_changes": value}}, "refused": {}}
        store.write_text(json.dumps(doc))
        assert mod.load_approvals(store).trusted == trusted


@pytest.mark.asyncio
async def test_the_sharing_row_can_stop_trusting_changes(toggle_home):
    """``trust_changes: false`` alone takes the choice off and keeps the share."""
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    await _press({"trust_changes": True})
    approved = _approval_module().load_approvals().approved["SRV"]

    resp = await mcp_mod.api_mcp_gateway_set_stub(
        _stub_request({"name": "srv", "trust_changes": False})
    )

    assert resp.status == 200
    assert _trusted() == set()
    assert _approval_module().load_approvals().approved["SRV"] == approved


@pytest.mark.parametrize(
    "content, reason",
    [(b"\xff", "not valid UTF-8"), (b"{ bad json", "not valid JSON")],
)
def test_forget_trust_refuses_an_unreadable_store(tmp_path, content, reason):
    from kiro_crew.mcp_gateway.launch_approval_heal import forget_trust

    mod = _approval_module()
    store = tmp_path / "approvals.json"
    store.write_bytes(content)

    with pytest.raises(mod.ApprovalStoreUnreadable, match=reason):
        forget_trust(["srv"], path=store)

    assert store.read_bytes() == content


def test_forget_trust_refuses_a_failed_read(tmp_path, monkeypatch):
    from kiro_crew.mcp_gateway.launch_approval_heal import forget_trust

    mod = _approval_module()
    store = _srv_approvals_file(tmp_path)
    before = store.read_bytes()
    original_read = Path.read_text

    def read(path, *args, **kwargs):
        if path == store:
            raise OSError("store unreadable")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    with pytest.raises(mod.ApprovalStoreUnreadable, match="unreadable"):
        forget_trust(["srv"], path=store)

    assert store.read_bytes() == before


@pytest.mark.parametrize("content", [None, b"", b" \n"])
def test_forget_trust_leaves_an_absent_or_blank_store_unchanged(tmp_path, content):
    from kiro_crew.mcp_gateway.launch_approval_heal import forget_trust

    store = tmp_path / "approvals.json"
    if content is not None:
        store.write_bytes(content)

    forget_trust(["srv"], path=store)

    if content is None:
        assert not store.exists()
    else:
        assert store.read_bytes() == content


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_sharing", [False, True])
async def test_trust_withdrawal_failure_returns_503_and_preserves_state(
    toggle_home, tmp_path, monkeypatch, stop_sharing
):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    status, _ = await _press({"trust_changes": True})
    assert status == 200
    mod = _approval_module()
    store_before = mod.approvals_path().read_bytes()
    config = tmp_path / "config.json"
    config_before = config.read_bytes()
    events = _capture_sel(monkeypatch)

    def fail(names):
        assert names == ["srv"]
        # Sharing-off revoked the approval before the withdrawal failed.
        assert _trusted() == (set() if stop_sharing else {"SRV"})
        raise OSError("store unavailable")

    monkeypatch.setattr(mcp_mod, "forget_trust", fail)
    body: dict[str, object] = {"name": "srv"}
    body.update({"stub": False} if stop_sharing else {"trust_changes": False})
    resp = await mcp_mod.api_mcp_gateway_set_stub(_stub_request(body))

    assert resp.status == 503
    assert isinstance(resp.body, (bytes, bytearray))
    assert json.loads(resp.body) == {
        "error": "could not record the launch approval",
        "code": "approval_write_failed",
    }
    assert mod.approvals_path().read_bytes() == store_before
    assert config.read_bytes() == config_before
    assert len(events) == 1
    assert events[0]["outcome"] == "denied"
    assert "approval_write_failed" in events[0]["resources"]


@pytest.mark.asyncio
async def test_trust_withdrawal_waits_for_revoke_rollback(toggle_home):
    """A rollback finishes before the Sharing row's trust withdrawal returns."""
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    status, _ = await asyncio.wait_for(
        _press({"trust_changes": True}), timeout=FALLBACK_TIMEOUT_SECS
    )
    assert status == 200
    assert _trusted() == {"SRV"}
    mod = _approval_module()
    approved = mod.load_approvals().approved["SRV"]
    lock = mcp_mod._MCP_GATEWAY_APPLY_LOCK
    await asyncio.wait_for(lock.acquire(), timeout=FALLBACK_TIMEOUT_SECS)
    withdrawal = None
    try:
        # Sharing-off and uses_cwd hold this lock across revoke and rollback.
        snapshot = mod.revoke(["srv"])
        assert _trusted() == set()
        withdrawal = asyncio.create_task(
            mcp_mod.api_mcp_gateway_set_stub(_stub_request({"name": "srv", "trust_changes": False}))
        )
        await asyncio.wait_for(until_parked(lock), timeout=FALLBACK_TIMEOUT_SECS)
        assert not withdrawal.done()
        mod.restore(snapshot)
        assert _trusted() == {"SRV"}
    finally:
        lock.release()
        if withdrawal is not None:
            resp = await asyncio.wait_for(withdrawal, timeout=FALLBACK_TIMEOUT_SECS)

    assert resp.status == 200
    assert _trusted() == set()
    assert mod.load_approvals().approved["SRV"] == approved


def test_a_pass_served_from_cache_keeps_trust(tmp_path: Path):
    """A restart with nothing changed reuses the cached rewrite: that pass admits
    nothing, so it cannot tell that a server is gone."""
    mod = _approval_module()
    store = tmp_path / "approvals.json"
    record = {"name": "srv", "fingerprints": ["a:b"], "pairs": ["a:b"], "trust_changes": True}
    store.write_text(json.dumps({"version": 1, "servers": {"SRV": record}, "refused": {}}))
    cached = mod.LaunchApprovals()
    assert not cached.full_pass

    mod.save_pass(cached, path=store)

    assert mod.load_approvals(store).trusted == {"SRV"}


def _capture_sel(monkeypatch) -> list[dict]:
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    events: list[dict] = []
    monkeypatch.setattr(
        mcp_mod,
        "sel",
        lambda: type("_Sel", (), {"log_api_access": lambda _self, **kw: events.append(kw)})(),
    )
    return events


@pytest.mark.asyncio
async def test_granting_and_taking_off_trust_are_audited(toggle_home, monkeypatch):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    events = _capture_sel(monkeypatch)
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    await _press({"trust_changes": True})
    await mcp_mod.api_mcp_gateway_set_stub(_stub_request({"name": "srv", "trust_changes": False}))

    ok = [e for e in events if e.get("outcome") == "ok"]
    assert any(
        e["operation"] == "mcp_gateway_set_stub" and "trust_changes=True" in e["resources"]
        for e in ok
    )
    assert any(
        e["operation"] == "mcp_gateway_trust_changes" and "trust_changes=False" in e["resources"]
        for e in ok
    )


async def _tick(*, user: str = "local-app"):
    """The Sharing row's tick: ``trust_changes: true`` alone."""
    from dashboard_owner_helpers import owner_claims

    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    request = owner_claims(_stub_request({"name": "srv", "trust_changes": True}), user=user)
    resp = await mcp_mod.api_mcp_gateway_set_stub(request)
    return resp.status, json.loads(resp.body)


@pytest.mark.asyncio
async def test_the_sharing_row_can_trust_changes_again(toggle_home, monkeypatch):
    """A tick on a shared, approved server grants trust and audits it as the Review press does."""
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    await _press({"trust_changes": True})
    approved = _approval_module().load_approvals().approved["SRV"]
    await mcp_mod.api_mcp_gateway_set_stub(_stub_request({"name": "srv", "trust_changes": False}))
    assert _trusted() == set()
    events = _capture_sel(monkeypatch)

    status, body = await _tick()

    assert (status, body["trust_changes"]) == (200, True)
    assert _trusted() == {"SRV"}
    assert _approval_module().load_approvals().approved["SRV"] == approved
    ok = [(e["operation"], e["resources"]) for e in events if e.get("outcome") == "ok"]
    assert ok == [("mcp_gateway_set_stub", "name=srv stub=True trust_changes=True")]


@pytest.mark.asyncio
async def test_a_non_owner_cannot_tick_trust(toggle_home):
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    await _press({})

    status, _ = await _tick(user="someone-else")

    assert status == 403
    assert _trusted() == set()


@pytest.mark.asyncio
async def test_a_tick_on_a_changed_launch_is_refused(toggle_home):
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    await _press({})
    approved = _approval_module().load_approvals().approved["SRV"]
    _declare(toggle_home, "agent spec", _APPROVED_CMD, ["-V"])

    status, body = await _tick()

    assert (status, body["code"]) == (409, "trust_needs_approved_launch")
    assert _trusted() == set()
    assert _approval_module().load_approvals().approved["SRV"] == approved


@pytest.mark.asyncio
async def test_a_tick_on_a_share_never_approved_is_refused(toggle_home, tmp_path):
    """A name written to the share list outside the dashboard has no approved launch."""
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    (tmp_path / "config.json").write_text(json.dumps({"mcp_gateway": {"stub_servers": ["srv"]}}))

    status, body = await _tick()

    assert (status, body["code"]) == (409, "trust_needs_approved_launch")
    assert _trusted() == set()
    assert "SRV" not in _approval_module().load_approvals().approved


@pytest.mark.asyncio
async def test_a_tick_on_an_unshared_server_is_refused(toggle_home):
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])

    status, body = await _tick()

    assert (status, body["code"]) == (409, "trust_needs_shared")
    assert _trusted() == set()
    assert "SRV" not in _approval_module().load_approvals().approved


@pytest.mark.asyncio
async def test_a_still_declared_server_keeps_trust_when_its_launch_is_skipped(
    toggle_home, tmp_path
):
    """A command the gateway cannot resolve for one pass (an upgrade moving the
    binary) skips the launch but keeps the server declared, so its trust stays."""
    mod = _approval_module()
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    await _press({"trust_changes": True})
    _declare(toggle_home, "agent spec", "no-such-mcp-binary-for-this-test", [])

    _target_env, approvals = _next_boot_rewrite(tmp_path, toggle_home)
    mod.save_pass(approvals)

    assert _trusted() == {"SRV"}


@pytest.mark.parametrize("name", ["srv", "npm:@playwright/mcp"])
def test_a_server_declared_only_in_the_global_settings_counts_as_declared(tmp_path: Path, name):
    """A server that an agent inherits from the global MCP settings is still
    declared when that agent's own ``mcpServers`` is empty, or leaves it out."""
    from kiro_crew.mcp_gateway import rewriter_spec

    mod = _approval_module()
    approvals = mod.LaunchApprovals()
    rewriter_spec._rewrite_single_spec(
        {"name": "a", "tools": [], "mcpServers": {}},
        stubs_dir=tmp_path / "stubs",
        socket_path=tmp_path / "gw.sock",
        work_dir=tmp_path / "work",
        sandbox_mode="off",
        approval_mode="normal",
        stub_servers=frozenset({"srv"}),
        inject_servers={name: {"command": _APPROVED_CMD, "args": []}},
        approvals=approvals,
    )

    assert approvals.declared == (
        {"SRV"} if name == "srv" else {"NPM:@PLAYWRIGHT/MCP", "PLAYWRIGHT_MCP"}
    )


def test_withdrawn_trust_drops_the_launch_pair_too(tmp_path: Path):
    """When trust is taken off while a pass runs, the save writes neither the
    trusted fingerprint nor the launch pair it admitted."""
    mod = _approval_module()
    store = tmp_path / "approvals.json"
    record = {"name": "srv", "fingerprints": ["c:old"], "pairs": ["c:old"]}
    store.write_text(json.dumps({"version": 1, "servers": {"SRV": record}, "refused": {}}))
    approvals = mod.LaunchApprovals()
    approvals.names["SRV"] = "srv"
    # A second launch of the same command, admitted another way, survives the save.
    approvals.captured["SRV"] = {"c:new", "c:kept"}
    approvals.captured_pairs["SRV"] = {"c:new", "c:kept"}
    approvals.trust_admitted["SRV"] = {"c:new": "c:new"}

    mod.save_pass(approvals, path=store)

    stored = mod.load_approvals(store)
    assert "c:new" not in stored.approved.get("SRV", set())
    assert "c:new" not in stored.approved_pairs.get("SRV", set())
    assert "c:kept" in stored.approved_pairs["SRV"]


def _srv_approvals_file(tmp_path: Path) -> Path:
    store = tmp_path / "approvals.json"
    record = {"name": "srv", "fingerprints": ["a:b"], "pairs": ["a:b"], "trust_changes": True}
    store.write_text(json.dumps({"version": 1, "servers": {"SRV": record}, "refused": {}}))
    return store


def test_a_settings_server_this_pass_does_not_mount_keeps_trust(tmp_path: Path):
    """A server in the global settings that this pass injects into no agent (it is
    muted) is still configured, so the pass keeps its trust."""
    from mcp_launch_approval_helpers import _rewrite

    mod = _approval_module()
    store = _srv_approvals_file(tmp_path)
    settings = tmp_path / "settings" / "mcp.json"
    settings.parent.mkdir(parents=True)
    # Muted for now: the settings filter injects it into no agent.
    settings.write_text(
        json.dumps({"mcpServers": {"srv": {"command": _APPROVED_CMD, "disabled": True}}})
    )
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "a.json").write_text(json.dumps({"name": "a", "tools": []}))
    approvals = mod.load_approvals(store)
    _rewrite(tmp_path, frozenset({"srv"}), approvals)
    assert approvals.full_pass

    mod.save_pass(approvals, path=store)

    assert mod.load_approvals(store).trusted == {"SRV"}


@pytest.mark.parametrize("source", ["settings", "spec"])
def test_a_slash_named_declaration_keeps_alias_trust_when_unresolved(tmp_path: Path, source):
    """A skipped launch keeps its alias-keyed trust while its raw name is declared."""
    from mcp_launch_approval_helpers import _rewrite

    mod = _approval_module()
    store = tmp_path / "approvals.json"
    record = {
        "name": "playwright-mcp",
        "fingerprints": ["a:b"],
        "pairs": ["a:b"],
        "trust_changes": True,
    }
    store.write_text(
        json.dumps({"version": 1, "servers": {"PLAYWRIGHT_MCP": record}, "refused": {}}),
        encoding="utf-8",
    )
    servers = {"npm:@playwright/mcp": {"command": str(tmp_path / "missing-mcp-binary")}}
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "a.json").write_text(
        json.dumps(
            {"name": "a", "tools": ["*"], "mcpServers": servers if source == "spec" else {}}
        ),
        encoding="utf-8",
    )
    if source == "settings":
        settings = tmp_path / "settings"
        settings.mkdir()
        (settings / "mcp.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    approvals = mod.load_approvals(store)
    assert approvals.trusted == {"PLAYWRIGHT_MCP"}

    _rewrite(tmp_path, frozenset({"playwright-mcp"}), approvals)
    assert approvals.full_pass
    assert not approvals.declared_incomplete
    assert not approvals.live_incomplete
    assert not approvals.captured
    mod.save_pass(approvals, path=store)

    assert mod.load_approvals(store).trusted == {"PLAYWRIGHT_MCP"}


def test_a_slash_named_spec_server_keeps_raw_stem_trust(tmp_path: Path):
    """An agent spec's own ``mcpServers`` admits a slash-named server under its raw
    stem, so a full pass keeps trust stored under that stem."""
    from mcp_launch_approval_helpers import _rewrite

    mod = _approval_module()
    store = tmp_path / "approvals.json"
    record = {
        "name": "npm:@playwright/mcp",
        "fingerprints": ["a:b"],
        "pairs": ["a:b"],
        "trust_changes": True,
    }
    store.write_text(
        json.dumps({"version": 1, "servers": {"NPM:@PLAYWRIGHT/MCP": record}, "refused": {}}),
        encoding="utf-8",
    )
    servers = {"npm:@playwright/mcp": {"command": str(tmp_path / "missing-mcp-binary")}}
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "a.json").write_text(
        json.dumps({"name": "a", "tools": ["*"], "mcpServers": servers}), encoding="utf-8"
    )
    approvals = mod.load_approvals(store)
    assert approvals.trusted == {"NPM:@PLAYWRIGHT/MCP"}

    _rewrite(tmp_path, frozenset({"npm:@playwright/mcp"}), approvals)
    assert approvals.full_pass
    assert not approvals.declared_incomplete
    mod.save_pass(approvals, path=store)

    assert mod.load_approvals(store).trusted == {"NPM:@PLAYWRIGHT/MCP"}


def test_a_pass_that_skipped_an_agent_keeps_trust(tmp_path: Path):
    """An agent spec that cannot be read is skipped; the server it declared may
    still be there, so that pass drops no trust."""
    from mcp_launch_approval_helpers import _rewrite

    mod = _approval_module()
    store = _srv_approvals_file(tmp_path)
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "a.json").write_text("{ not json")
    approvals = mod.load_approvals(store)
    _rewrite(tmp_path, frozenset({"srv"}), approvals)
    assert approvals.full_pass

    mod.save_pass(approvals, path=store)

    assert mod.load_approvals(store).trusted == {"SRV"}


def test_a_trusted_server_that_keeps_changing_stays_under_the_cap(tmp_path: Path, monkeypatch):
    """Each change to a trusted server replaces the launch it changed from, so
    more changes than the per-server cap still leave it shared."""
    from mcp_launch_approval_helpers import _rewrite

    mod = _approval_module()
    store = tmp_path / "approvals.json"
    (tmp_path / "agents").mkdir()

    def declare(version: int) -> None:
        entry = {"command": _APPROVED_CMD, "args": [f"--v{version}"]}
        (tmp_path / "agents" / "a.json").write_text(
            json.dumps({"name": "a", "tools": ["*"], "mcpServers": {"srv": entry}})
        )

    declare(0)
    probe = mod.LaunchApprovals(probe=True)
    _rewrite(tmp_path, frozenset({"srv"}), probe)
    mod.approve({"srv": list(probe.captured_launches["SRV"].values())}, path=store, trusted={"srv"})

    for version in range(1, mod._MAX_SERVER_LAUNCHES + 5):
        declare(version)
        approvals = mod.load_approvals(store)
        target_env = _rewrite(tmp_path, frozenset({"srv"}), approvals)
        mod.save_pass(approvals, path=store)
        assert approvals.refused == {}, version
        assert _targets_running(target_env, _APPROVED_CMD)

    assert len(mod.load_approvals(store).approved["SRV"]) == 1


def test_a_trusted_server_can_replace_many_launches_in_one_pass(tmp_path: Path):
    """Forty agents each run the trusted server with their own argument, and one
    update changes all forty: every replacement is admitted, because the cap
    counts the launches that are live, not the ones they replace."""
    from mcp_launch_approval_helpers import _rewrite

    mod = _approval_module()
    store = tmp_path / "approvals.json"
    agents = tmp_path / "agents"
    agents.mkdir()

    def declare(version: int) -> None:
        for i in range(40):
            entry = {"command": _APPROVED_CMD, "args": [f"--agent{i}", f"--v{version}"]}
            (agents / f"a{i}.json").write_text(
                json.dumps({"name": f"a{i}", "tools": ["*"], "mcpServers": {"srv": entry}})
            )

    declare(0)
    probe = mod.LaunchApprovals(probe=True)
    _rewrite(tmp_path, frozenset({"srv"}), probe)
    mod.approve({"srv": list(probe.captured_launches["SRV"].values())}, path=store, trusted={"srv"})
    declare(1)

    approvals = mod.load_approvals(store)
    _rewrite(tmp_path, frozenset({"srv"}), approvals)
    mod.save_pass(approvals, path=store)

    assert approvals.refused == {}
    assert len(mod.load_approvals(store).approved["SRV"]) == 40


def test_a_trust_grant_made_while_a_pass_ran_is_kept(tmp_path: Path):
    """A pass that started before the owner trusted a server cannot take that
    newer choice away."""
    mod = _approval_module()
    store = tmp_path / "approvals.json"
    record = {"name": "srv", "fingerprints": ["a:b"], "pairs": ["a:b"]}
    store.write_text(json.dumps({"version": 1, "servers": {"SRV": record}, "refused": {}}))
    older = mod.load_approvals(store)
    older.full_pass = True
    record["trust_changes"] = True
    store.write_text(json.dumps({"version": 1, "servers": {"SRV": record}, "refused": {}}))

    mod.save_pass(older, path=store)

    assert mod.load_approvals(store).trusted == {"SRV"}


@pytest.mark.parametrize("approved_overflow", [False, True])
def test_trusted_launch_overflow_is_bounded_and_retires_nothing(
    tmp_path: Path, approved_overflow: bool
):
    from kiro_crew.mcp_gateway import launch_approval_heal as heal

    mod = _approval_module()
    store = _srv_approvals_file(tmp_path)
    approvals = mod.load_approvals(store)
    approvals.full_pass = True
    approvals.declared.add("SRV")
    current = mod.load_approvals(store)
    accepted = []
    for index in range(mod._MAX_SERVER_LAUNCHES + (0 if approved_overflow else 10)):
        args = [f"--v{index}"]
        fingerprint = mod.launch_fingerprint(_APPROVED_CMD, args, {})
        accepted.append(
            approvals.admit(
                "srv",
                fingerprint,
                launch=(_APPROVED_CMD, args),
                env={},
                derived_env_hash=mod.env_fingerprint({}),
            )
        )

    if approved_overflow:
        # The stored launch is still approved after trusted changes fill the live set.
        assert approvals.admit("srv", "a:b", derived_env_hash="b")
        assert "SRV" not in approvals.refused
        assert not approvals.over_cap("SRV")

    assert sum(accepted) == mod._MAX_SERVER_LAUNCHES
    assert len(approvals.trust_live["SRV"]) == mod._MAX_SERVER_LAUNCHES
    final_fps = current.approved["SRV"] | approvals.captured["SRV"]
    final_pairs = current.approved_pairs["SRV"] | approvals.captured_pairs["SRV"]
    assert heal.retire_trusted(approvals, current, "SRV", final_fps, final_pairs) == (
        final_fps,
        final_pairs,
    )
    mod.save_pass(approvals, path=store)
    stored = mod.load_approvals(store)
    assert stored.approved == current.approved
    assert stored.approved_pairs == current.approved_pairs
    assert stored.trusted == {"SRV"}


@pytest.mark.parametrize("source", ["settings", "injected", "spec"])
@pytest.mark.parametrize("overflow", ["count", "length"])
def test_declared_overflow_is_bounded_and_forgets_no_trust(tmp_path: Path, source, overflow):
    from kiro_crew.mcp_gateway import rewriter, rewriter_spec

    mod = _approval_module()
    store = _srv_approvals_file(tmp_path)
    approvals = mod.load_approvals(store)
    approvals.full_pass = True
    names = (
        [f"server-{index}" for index in range(mod._MAX_SERVER_RECORDS + 10)]
        if overflow == "count"
        else ["x" * (mod._MAX_NAME_CHARS + 1)]
    )
    servers = {name: {"url": "https://example.invalid/mcp"} for name in names}
    if source == "settings":
        rewriter._note_declared(approvals, servers)
    else:
        rewriter_spec._rewrite_single_spec(
            {"name": "a", "tools": [], "mcpServers": servers if source == "spec" else {}},
            stubs_dir=tmp_path / "stubs",
            socket_path=tmp_path / "gw.sock",
            work_dir=tmp_path / "work",
            sandbox_mode="off",
            approval_mode="normal",
            stub_servers=frozenset(),
            inject_servers=servers if source == "injected" else {},
            approvals=approvals,
        )

    assert len(approvals.declared) <= mod._MAX_SERVER_RECORDS
    assert all(len(stem) <= mod._MAX_NAME_CHARS for stem in approvals.declared)
    assert approvals.declared_incomplete
    mod.save_pass(approvals, path=store)
    assert mod.load_approvals(store).trusted == {"SRV"}


@pytest.mark.parametrize("source", ["settings", "spec", "mate"])
@pytest.mark.parametrize(
    "content",
    [
        pytest.param("[]", id="top-list"),
        pytest.param("null", id="top-null"),
        pytest.param('"text"', id="top-string"),
        pytest.param("{ bad json", id="invalid-json"),
        pytest.param(b"\xff", id="invalid-utf8"),
        pytest.param('{"mcpServers": []}', id="servers-list"),
        pytest.param('{"mcpServers": "text"}', id="servers-string"),
        pytest.param('{"mcpServers": null}', id="servers-null"),
        pytest.param('{"mcpServers": false}', id="servers-bool"),
        pytest.param('{"mcpServers": {"other": []}}', id="entry-list"),
        pytest.param('{"mcpServers": {"other": "text"}}', id="entry-string"),
        pytest.param('{"mcpServers": {"other": null}}', id="entry-null"),
    ],
)
def test_malformed_declaration_census_preserves_trust(tmp_path, monkeypatch, source, content):
    from kiro_crew import mate_config
    from kiro_crew.mcp_gateway import launch_approval_heal, rewriter

    mod = _approval_module()
    store = _srv_approvals_file(tmp_path)
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "a.json").write_text('{"name": "a", "tools": []}', encoding="utf-8")
    mates = tmp_path / "mates"
    if source == "settings":
        path = tmp_path / "settings" / "mcp.json"
    elif source == "mate":
        path = mates / "pixel.json"
        monkeypatch.setattr(mate_config, "crewmate_file_on_record", lambda name: True)
    else:
        path = agents / "a.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    approvals = mod.load_approvals(store)

    rewriter.rewrite_agents(
        source_dir=agents,
        overlay_dir=tmp_path / "overlays",
        socket_path=tmp_path / "gw.sock",
        work_dir=tmp_path / "work",
        stub_servers=frozenset({"srv"}),
        approvals=approvals,
        mate_source_dir=mates if source == "mate" else None,
    )

    assert approvals.full_pass
    assert approvals.declared_incomplete
    # Even a launch found elsewhere cannot retire an unread source's older launch.
    approvals.trust_live["SRV"] = {"c:d"}
    assert launch_approval_heal.retire_trusted(
        approvals, mod.load_approvals(store), "SRV", {"a:b", "c:d"}, {"a:b", "c:d"}
    ) == ({"a:b", "c:d"}, {"a:b", "c:d"})
    mod.save_pass(approvals, path=store)
    stored = mod.load_approvals(store)
    assert stored.trusted == {"SRV"}
    assert stored.approved["SRV"] == {"a:b"}
    assert stored.approved_pairs["SRV"] == {"a:b"}


@pytest.mark.parametrize("source", ["settings", "spec"])
@pytest.mark.parametrize("content", ["{}", '{"mcpServers": {}}'])
def test_complete_declaration_census_retires_removed_trust(tmp_path, source, content):
    from mcp_launch_approval_helpers import _rewrite

    mod = _approval_module()
    store = _srv_approvals_file(tmp_path)
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "a.json").write_text('{"name": "a"}', encoding="utf-8")
    path = tmp_path / "settings" / "mcp.json" if source == "settings" else agents / "a.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    approvals = mod.load_approvals(store)

    _rewrite(tmp_path, frozenset({"srv"}), approvals)

    assert approvals.full_pass
    assert not approvals.declared_incomplete
    mod.save_pass(approvals, path=store)
    assert mod.load_approvals(store).trusted == set()


@pytest.mark.parametrize("failure", ["directory", "read", "stat"])
def test_unreadable_settings_census_preserves_trust(tmp_path, monkeypatch, failure):
    from mcp_launch_approval_helpers import _rewrite

    mod = _approval_module()
    store = _srv_approvals_file(tmp_path)
    (tmp_path / "agents").mkdir()
    settings = tmp_path / "settings" / "mcp.json"
    settings.parent.mkdir()
    if failure == "directory":
        settings.mkdir()
    else:
        settings.write_text("{}", encoding="utf-8")
        if failure == "read":
            original_read = Path.read_text

            def read(path, *args, **kwargs):
                if path == settings:
                    raise OSError("settings unreadable")
                return original_read(path, *args, **kwargs)

            monkeypatch.setattr(Path, "read_text", read)
        else:
            original_is_file = Path.is_file
            original_stat = Path.stat

            def stat(path, *args, **kwargs):
                if path == settings:
                    raise OSError("settings stat failed")
                return original_stat(path, *args, **kwargs)

            monkeypatch.setattr(
                Path, "is_file", lambda path: False if path == settings else original_is_file(path)
            )
            monkeypatch.setattr(Path, "stat", stat)
    approvals = mod.load_approvals(store)

    _rewrite(tmp_path, frozenset({"srv"}), approvals)

    assert approvals.full_pass
    assert approvals.declared_incomplete
    mod.save_pass(approvals, path=store)
    assert mod.load_approvals(store).trusted == {"SRV"}


def test_non_string_yaml_server_name_makes_census_incomplete(tmp_path):
    from mcp_launch_approval_helpers import _rewrite

    mod = _approval_module()
    store = _srv_approvals_file(tmp_path)
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "a.md").write_text(
        "---\nname: a\ntools: ['*']\nmcpServers:\n  7: {}\n---\nPrompt\n", encoding="utf-8"
    )
    approvals = mod.load_approvals(store)

    _rewrite(tmp_path, frozenset({"srv"}), approvals)

    assert approvals.full_pass
    assert approvals.declared_incomplete
    mod.save_pass(approvals, path=store)
    assert mod.load_approvals(store).trusted == {"SRV"}


@pytest.mark.parametrize("source", ["agent-listing", "mate-folder", "mate-file"])
def test_skipped_source_census_preserves_trust(tmp_path, monkeypatch, source):
    from kiro_crew import mate_config
    from kiro_crew.mcp_gateway import rewriter

    mod = _approval_module()
    store = _srv_approvals_file(tmp_path)
    agents = tmp_path / "agents"
    agents.mkdir()
    mates = tmp_path / "mates"
    monkeypatch.setattr(mate_config, "crewmate_file_on_record", lambda name: True)
    if source == "agent-listing":

        def unreadable(_path):
            raise PermissionError("agent listing unreadable")

        monkeypatch.setattr(rewriter, "iter_agent_spec_files_strict", unreadable, raising=False)
    elif source == "mate-folder":
        mates.write_text("not a folder", encoding="utf-8")
    else:
        mates.mkdir()
        (mates / "pixel.json").mkdir()
    approvals = mod.load_approvals(store)

    rewriter.rewrite_agents(
        source_dir=agents,
        overlay_dir=tmp_path / "overlays",
        socket_path=tmp_path / "gw.sock",
        work_dir=tmp_path / "work",
        stub_servers=frozenset({"srv"}),
        approvals=approvals,
        mate_source_dir=None if source == "agent-listing" else mates,
    )

    assert approvals.full_pass
    assert approvals.declared_incomplete
    mod.save_pass(approvals, path=store)
    assert mod.load_approvals(store).trusted == {"SRV"}


def test_app_export_shadow_keeps_census_complete_and_retires_unseen_trust(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from kiro_crew import mate_config
    from kiro_crew.apps import bridges, manager, protected_app_state
    from kiro_crew.mcp_gateway import mate_overlays, rewriter

    mod = _approval_module()
    store = _srv_approvals_file(tmp_path)
    agents = tmp_path / "agents"
    agents.mkdir()
    mates = tmp_path / "mates"
    mates.mkdir()
    (mates / "pixel.json").write_text(
        json.dumps({"mcpServers": {"srv": {"url": "https://example.invalid/mcp"}}}),
        encoding="utf-8",
    )
    app = "example-app"
    export = agents / (bridges._safe_link_name(bridges._namespace(app, "pixel")) + ".json")
    export.write_text(
        json.dumps(
            {"name": "pixel", "mcpServers": {"other": {"url": "https://example.invalid/mcp"}}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(mate_config, "crewmate_file_on_record", lambda name: name == "pixel")
    monkeypatch.setattr(
        manager, "owning_app_for_agent", lambda name: SimpleNamespace(app=app, enabled=True)
    )
    monkeypatch.setattr(protected_app_state, "owner_disabled", lambda name: False)
    assert mate_overlays._own_app_export(agents, "pixel") == app
    approvals = mod.load_approvals(store)

    rewriter.rewrite_agents(
        source_dir=agents,
        overlay_dir=tmp_path / "overlays",
        socket_path=tmp_path / "gw.sock",
        work_dir=tmp_path / "work",
        stub_servers=frozenset({"srv"}),
        approvals=approvals,
        mate_source_dir=mates,
    )

    assert approvals.full_pass
    assert not approvals.declared_incomplete
    assert not approvals.live_incomplete
    assert approvals.declared == {"OTHER"}
    assert not (mate_overlays.mate_overlay_dir(tmp_path / "overlays") / "pixel.json").exists()
    mod.save_pass(approvals, path=store)
    assert mod.load_approvals(store).trusted == set()

"""The stub toggle approves only the displayed launch, and stub off revokes it.

The first toggle is a compare-and-set on the ``expected_launch`` the launch
endpoint displays: a toggle without it, a partial display and a batch toggle-on
are refused, and a reapprove lands only while the declared launches still match
the refusal the page shows, replacing the old approval when they do. A toggle
with no launch, too many launches or a full store changes neither the config nor
the approval store, the over-cap and changed-launch refusals are audited as
denied, a failed approval write admits nothing, and the refusal store is read off
the event loop. Turning the stub off forgets the approval, and a stub off whose
config update or revocation fails leaves the config and the approval unchanged.
The launch a click approves, a spec edited after the click, and the refusal and
approved displays the page shows are pinned in ``test_mcp_launch_approval.py``.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest
from mcp_launch_approval_helpers import (  # noqa: F401 -- the fixture is requested by name
    _APPROVED_CMD,
    _ATTACK_CMD,
    _approval_module,
    _declare,
    _next_boot_rewrite,
    _stub_request,
    _targets_running,
    _toggle_on,
    toggle_home_fixture,
)

from kiro_crew.mcp_gateway import rewriter

#: Opts this module out of the conftest fixture that approves every gatewayd launch.
ENFORCE_LAUNCH_APPROVAL = True


def _declare_two(agents: Path, first: list[str], second: list[str]) -> None:
    for agent_name, args in (("a", first), ("b", second)):
        entry = {"command": _ATTACK_CMD, "args": args}
        (agents / f"{agent_name}.json").write_text(
            json.dumps({"name": agent_name, "mcpServers": {"srv": entry}})
        )


@pytest.mark.asyncio
async def test_first_toggle_requires_the_displayed_launch(toggle_home):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    response = await mcp_mod.api_mcp_gateway_set_stub(_stub_request({"name": "srv", "stub": True}))

    assert response.status == 409
    assert json.loads(response.body)["code"] == "expected_launch_required"


@pytest.mark.asyncio
async def test_launch_endpoint_returns_the_first_toggle_compare_and_set(toggle_home):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    request = _stub_request({})
    request.query = {"name": "srv"}
    response = await mcp_mod.api_mcp_gateway_server_launch(request)
    body = json.loads(response.body)

    assert response.status == 200
    assert body["name"] == "srv"
    assert body["commands"] == [[_APPROVED_CMD]]
    assert body["envs"] == [[]]
    assert body["complete"] is True
    assert body["expected_launch"]


@pytest.mark.asyncio
async def test_partial_launch_display_cannot_be_approved(toggle_home):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    padded = [str(index) for index in range(getattr(_approval_module(), "_MAX_DISPLAY_ARGS") + 1)]
    _declare(toggle_home, "agent spec", _APPROVED_CMD, padded)
    launches = await asyncio.to_thread(
        mcp_mod.launch_resolve.resolve_launches, ["srv"], refresh_specs=False
    )
    identities = {identity for launch in launches["srv"] for identity in launch.approval_identities}
    display = mcp_mod.display_launches("srv", launches["srv"])

    assert display["complete"] is False
    assert "expected_launch" not in display
    assert "partial" in display["commands"][0][-1]
    response = await mcp_mod.api_mcp_gateway_set_stub(
        _stub_request(
            {
                "name": "srv",
                "stub": True,
                "expected_launch": _approval_module().serialize_identities(identities),
            }
        )
    )
    assert response.status == 409
    assert json.loads(response.body)["code"] == "launch_display_incomplete"


@pytest.mark.asyncio
async def test_batch_toggle_on_requires_individual_launch_displays(toggle_home):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    response = await mcp_mod.api_mcp_gateway_set_stub(
        _stub_request({"names": ["srv"], "stub": True})
    )

    assert response.status == 400
    assert json.loads(response.body)["code"] == "batch_stub_requires_individual"


@pytest.mark.asyncio
async def test_a_refusal_with_two_launch_identities_round_trips_through_reapprove(
    tmp_path, toggle_home
):
    mod = _approval_module()
    assert mod is not None
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    assert (await _toggle_on())[0] == 200

    _declare_two(toggle_home, ["-c", "id"], ["-c", "whoami"])
    _target_env, refused = _next_boot_rewrite(tmp_path, toggle_home)
    assert refused.refused == {"SRV": mod.REFUSED_CHANGED}
    assert mod.save_pass(refused)
    refusal = mod.refused_servers()["srv"]
    expected_launch = refusal["expected_launch"]
    assert len(expected_launch.split(",")) == 2
    assert sorted(refusal["commands"]) == sorted(
        [
            [_ATTACK_CMD, "-c", "id"],
            [_ATTACK_CMD, "-c", "whoami"],
        ]
    )
    assert refusal["envs"] == [[], []]

    status, body = await _toggle_on(expected_launch=expected_launch)

    assert status == 200, body
    stored = mod.load_approvals()
    assert stored.stored_refused == {}
    assert stored.admits_command("SRV", rewriter.hash_command(_ATTACK_CMD, ["-c", "id"]))
    assert stored.admits_command("SRV", rewriter.hash_command(_ATTACK_CMD, ["-c", "whoami"]))


@pytest.mark.asyncio
async def test_a_changed_launch_identity_set_still_rejects_reapprove(tmp_path, toggle_home):
    mod = _approval_module()
    assert mod is not None
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    assert (await _toggle_on())[0] == 200

    _declare_two(toggle_home, ["-c", "id"], ["-c", "whoami"])
    _target_env, refused = _next_boot_rewrite(tmp_path, toggle_home)
    assert mod.save_pass(refused)
    expected_launch = mod.refused_servers()["srv"]["expected_launch"]
    _declare_two(toggle_home, ["-c", "id"], ["-c", "uname"])

    status, body = await _toggle_on(expected_launch=expected_launch)

    assert status == 409
    assert body["code"] == "launch_changed_since_display"


@pytest.mark.asyncio
async def test_toggle_reads_the_refusal_store_off_the_event_loop(
    tmp_path, toggle_home, monkeypatch
):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    event_loop_thread = threading.get_ident()
    real_refused_servers = mcp_mod.refused_servers

    def _checked_refused_servers():
        assert threading.get_ident() != event_loop_thread
        return real_refused_servers()

    monkeypatch.setattr(mcp_mod, "refused_servers", _checked_refused_servers)
    status, body = await _toggle_on()

    assert status == 200, body


@pytest.mark.asyncio
async def test_a_toggle_with_no_launch_to_approve_changes_nothing(tmp_path, toggle_home):
    (toggle_home / "a.json").write_text(json.dumps({"name": "a", "mcpServers": {}}))

    status, body = await _toggle_on()

    assert status == 409
    assert body["code"] == "launch_unresolved"
    assert body["unresolved"] == ["srv"]
    written = json.loads((tmp_path / "config.json").read_text()).get("mcp_gateway", {})
    assert "srv" not in (written.get("stub_overrides") or {})
    assert "srv" not in (written.get("stub_servers") or [])
    assert not _approval_module().approvals_path().exists()


@pytest.mark.asyncio
async def test_a_toggle_whose_launches_exceed_the_cap_changes_nothing(
    tmp_path, toggle_home, monkeypatch
):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod
    from kiro_crew.mcp_gateway import launch_resolve

    mod = _approval_module()
    cap = getattr(mod, "_MAX_SERVER_LAUNCHES")
    for index in range(cap + 1):
        entry = {"command": _APPROVED_CMD, "args": [str(index)]}
        (toggle_home / f"a{index:03d}.json").write_text(
            json.dumps({"name": f"a{index:03d}", "mcpServers": {"srv": entry}})
        )
    resolved = launch_resolve.resolve_launches(["srv"], refresh_specs=False)
    assert "srv" in getattr(resolved, "over_cap", ())
    assert "srv" not in resolved

    events = []
    monkeypatch.setattr(
        mcp_mod,
        "sel",
        lambda: type("_Sel", (), {"log_api_access": lambda _self, **kw: events.append(kw)})(),
    )

    status, body = await _toggle_on()

    assert status == 409
    assert body["code"] == "launch_over_cap"
    assert body["over_cap"] == ["srv"]
    written = json.loads((tmp_path / "config.json").read_text()).get("mcp_gateway", {})
    assert "srv" not in (written.get("stub_overrides") or {})
    assert "srv" not in (written.get("stub_servers") or [])
    assert not _approval_module().approvals_path().exists()
    assert [(e["operation"], e["outcome"]) for e in events] == [
        ("mcp_stub_rejected_launch_over_cap", "denied")
    ]


@pytest.mark.asyncio
async def test_a_toggle_at_the_store_record_cap_changes_neither_store(
    tmp_path, toggle_home, monkeypatch
):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    mod = _approval_module()
    assert mod is not None
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    approvals = mod.LaunchApprovals()
    env_hash = mod.env_fingerprint({})
    for index in range(getattr(mod, "_MAX_SERVER_RECORDS")):
        name = f"kept-{index:04d}"
        stem = mod.target_stem(name)
        args = [str(index)]
        fingerprint = mod.launch_fingerprint(_APPROVED_CMD, args, {})
        approvals.approved[stem] = {fingerprint}
        approvals.approved_pairs[stem] = {
            mod.launch_pair(rewriter.hash_command(_APPROVED_CMD, args), env_hash)
        }
        approvals.names[stem] = name
    store = mod.approvals_path()
    store.parent.mkdir(parents=True, exist_ok=True)
    mod._write(store, mod._document(approvals, {}))
    config = tmp_path / "config.json"
    mcp_mod.KiroCrewConfig.load()
    before_config = config.read_bytes()
    before_store = store.read_bytes()
    events = []
    monkeypatch.setattr(
        mcp_mod,
        "sel",
        lambda: type("_Sel", (), {"log_api_access": lambda _self, **kw: events.append(kw)})(),
    )

    status, body = await _toggle_on()

    assert status == 409
    assert body["code"] == "launch_over_cap"
    assert body["over_cap"] == ["srv"]
    assert config.read_bytes() == before_config
    assert store.read_bytes() == before_store
    assert [(event["operation"], event["outcome"]) for event in events] == [
        ("mcp_stub_rejected_launch_over_cap", "denied")
    ]


@pytest.mark.asyncio
async def test_an_approval_write_failure_does_not_admit_the_launch(
    tmp_path, toggle_home, monkeypatch
):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    monkeypatch.setattr(mcp_mod, "approve", lambda launches: (_ for _ in ()).throw(OSError()))

    status, body = await _toggle_on()

    assert status == 503
    assert body["code"] == "approval_write_failed"
    target_env, approvals = _next_boot_rewrite(tmp_path, toggle_home)
    assert _targets_running(target_env, _APPROVED_CMD) == []
    assert approvals.refused == {"SRV": _approval_module().REFUSED_UNAPPROVED}


@pytest.mark.asyncio
async def test_reapprove_rejects_a_launch_changed_after_it_was_shown(
    tmp_path, toggle_home, monkeypatch
):
    from unittest.mock import MagicMock

    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    mod = _approval_module()
    assert mod is not None
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    assert (await _toggle_on())[0] == 200

    audit = MagicMock()
    monkeypatch.setattr(mcp_mod, "sel", lambda: audit)
    _declare(toggle_home, "agent spec", _ATTACK_CMD, ["-c", "id"])
    _target_env, refused = _next_boot_rewrite(tmp_path, toggle_home)
    assert refused.refused == {"SRV": mod.REFUSED_CHANGED}
    assert mod.save_pass(refused)
    expected_launch = mod.refused_servers()["srv"]["expected_launch"]
    approval_before = mod.approvals_path().read_bytes()
    config_before = (tmp_path / "config.json").read_bytes()

    # The operator still sees the refused shell command, but an agent changes the
    # declaration before the click reaches the handler.
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    status, body = await _toggle_on(expected_launch=expected_launch)

    assert status == 409
    assert body["code"] == "launch_changed_since_display"
    event = audit.log_api_access.call_args.kwargs
    assert event["operation"] == "mcp_stub_rejected_launch_changed_since_display"
    assert event["outcome"] == "denied"
    assert event["resources"] == "code=launch_changed_since_display names=srv"
    assert mod.approvals_path().read_bytes() == approval_before
    assert (tmp_path / "config.json").read_bytes() == config_before


@pytest.mark.asyncio
async def test_reapprove_matching_the_shown_launch_replaces_the_approval(tmp_path, toggle_home):
    mod = _approval_module()
    assert mod is not None
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    assert (await _toggle_on())[0] == 200

    _declare(toggle_home, "agent spec", _ATTACK_CMD, ["-c", "id"])
    _target_env, refused = _next_boot_rewrite(tmp_path, toggle_home)
    assert refused.refused == {"SRV": mod.REFUSED_CHANGED}
    assert mod.save_pass(refused)
    expected_launch = mod.refused_servers()["srv"]["expected_launch"]

    status, body = await _toggle_on(expected_launch=expected_launch)

    assert status == 200, body
    stored = mod.load_approvals()
    assert stored.stored_refused == {}
    assert stored.admits_command("SRV", rewriter.hash_command(_ATTACK_CMD, ["-c", "id"]))
    assert not stored.admits_command("SRV", rewriter.hash_command(_APPROVED_CMD, []))


@pytest.mark.asyncio
async def test_turning_the_stub_off_forgets_the_approval(tmp_path, toggle_home):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    assert (await _toggle_on())[0] == 200
    resp = await mcp_mod.api_mcp_gateway_set_stub(_stub_request({"name": "srv", "stub": False}))
    assert resp.status == 200

    assert _approval_module().load_approvals().approved == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    ["config_read", "config_write", "config_lock", "section_not_object"],
)
async def test_stub_off_restores_approval_when_config_does_not_commit(
    tmp_path, toggle_home, monkeypatch, failure
):
    import kiro_crew.config.loader as loader_mod
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    assert (await _toggle_on())[0] == 200
    approval_mod = _approval_module()
    store_before = approval_mod.approvals_path().read_bytes()
    config_path = tmp_path / "config.json"
    if failure == "section_not_object":
        config_path.write_text(json.dumps({"mcp_gateway": "invalid"}))
    config_before = config_path.read_bytes()
    errors = {
        "config_read": loader_mod.ConfigReadError("unreadable"),
        "config_write": loader_mod.ConfigWriteRefused("refused"),
        "config_lock": OSError("locked"),
    }
    if failure in errors:
        error = errors[failure]

        def _fail(*args, **kwargs):
            raise error

        monkeypatch.setattr(loader_mod, "update_config_locked", _fail)

    response = await mcp_mod.api_mcp_gateway_set_stub(_stub_request({"name": "srv", "stub": False}))

    assert response.status != 200
    assert config_path.read_bytes() == config_before
    assert approval_mod.approvals_path().read_bytes() == store_before


@pytest.mark.asyncio
async def test_stub_off_revocation_failure_preserves_config_and_approval(
    tmp_path, toggle_home, monkeypatch
):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    assert (await _toggle_on())[0] == 200
    approval_mod = _approval_module()
    stored_before = approval_mod.load_approvals()
    config_path = tmp_path / "config.json"
    config_before = config_path.read_bytes()
    monkeypatch.setattr(
        mcp_mod,
        "revoke",
        lambda names: (_ for _ in ()).throw(OSError("approval store unavailable")),
    )

    resp = await mcp_mod.api_mcp_gateway_set_stub(_stub_request({"name": "srv", "stub": False}))

    assert resp.status == 503
    assert json.loads(resp.body)["code"] == "approval_write_failed"
    assert config_path.read_bytes() == config_before
    stored_after = approval_mod.load_approvals()
    assert stored_after.approved == stored_before.approved
    assert stored_after.approved_pairs == stored_before.approved_pairs

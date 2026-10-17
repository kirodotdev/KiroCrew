"""HTTP-layer tests for the Dev Fleet standalone backend.

This module pins what the routes serve and how they refuse a request: the fleet
snapshot's row drop and rebuild-failure log, the GET handlers, ``_json_body`` and
POST validation and dispatch, the ``_audited`` decorator's outcomes, the HMAC proxy
middleware's denials, and the ``_worktree_detail`` payload with the link-safe
directory walk behind its disk size. Requests are mocked and nothing listens on a
port. The helper, pod and rebase cases live in ``test_dev_fleet_server_coverage.py``,
prune verdicts in ``test_dev_fleet_server_coverage_prune.py``, the app lifecycle in
``test_dev_fleet_server_coverage_lifecycle.py``, and the discard-path handler checks
in ``test_dev_fleet_server_coverage_untracked_discard.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from dev_fleet_server_coverage_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _json_request,
    _pin_module_state,
    _sel_capture,
)

import kiro_crew.apps.builtins.dev_fleet.server as mod
from kiro_crew.apps.builtins.dev_fleet import (
    fleet_state,
    http_api,
    repository,
    runtime,
    worktree_ops,
)


def _raw_request(raw: bytes, *, json_error: Exception | None = None) -> MagicMock:
    request = MagicMock()
    request.read = AsyncMock(return_value=raw)
    if json_error is not None:
        request.json = AsyncMock(side_effect=json_error)
    else:
        try:
            request.json = AsyncMock(return_value=json.loads(raw or b"{}"))
        except ValueError as exc:
            request.json = AsyncMock(side_effect=exc)
    request.content_length = len(raw)
    request.can_read_body = True
    return request


# --------------------------------------------------------------------------
# fleet cache helpers
# --------------------------------------------------------------------------
def test_drop_worktrees_ignores_malformed_payload():
    assert fleet_state._drop_worktrees({"worktrees": "nope"}, {"x"}) == {"worktrees": "nope"}


def test_drop_worktrees_returns_same_object_when_nothing_matches():
    data = {"worktrees": [{"name": "a"}]}
    assert fleet_state._drop_worktrees(data, {"z"}) is data


def test_drop_worktrees_copies_without_named_rows():
    data = {"worktrees": [{"name": "a"}, {"name": "b"}], "other": 1}
    out = fleet_state._drop_worktrees(data, {"a"})
    assert out["worktrees"] == [{"name": "b"}]
    assert out["other"] == 1
    assert data["worktrees"] == [{"name": "a"}, {"name": "b"}]


@pytest.mark.asyncio
async def test_log_fleet_rebuild_failure_ignores_cancellation():
    async def _never():
        # flake-ok: pre-existing placeholder coroutine that never finishes on its own; the test cancels it, moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing sleep, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(30)

    task = asyncio.create_task(_never())
    # flake-ok: pre-existing one-turn yield so the task starts before the cancel, moved verbatim; tracked for a de-flake pass
    await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    fleet_state._log_fleet_rebuild_failure(task)  # must not raise


@pytest.mark.asyncio
async def test_log_fleet_rebuild_failure_warns_on_exception(caplog):
    async def _boom():
        raise RuntimeError("rebuild died")

    task = asyncio.create_task(_boom())
    try:
        await task
    except RuntimeError:
        pass
    with caplog.at_level("WARNING", logger=runtime.logger.name):
        fleet_state._log_fleet_rebuild_failure(task)
    assert "rebuild died" in caplog.text


# --------------------------------------------------------------------------
# GET handlers
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fleet_handler_reports_discovery_error(monkeypatch):
    monkeypatch.setattr(fleet_state, "_fleet_cached", AsyncMock(side_effect=RuntimeError("no git")))
    resp = await http_api.api_dev_fleet_fleet(make_mocked_request("GET", "/api/fleet"))
    assert json.loads(resp.text) == {"worktrees": [], "error": "no git"}


@pytest.mark.asyncio
async def test_fleet_handler_fresh_bypasses_cache(monkeypatch):
    refresh = AsyncMock(return_value={"worktrees": [{"name": "a"}]})
    monkeypatch.setattr(fleet_state, "_fleet_refresh", refresh)
    monkeypatch.setattr(fleet_state, "_fleet_cached", AsyncMock(return_value={"worktrees": []}))
    resp = await http_api.api_dev_fleet_fleet(make_mocked_request("GET", "/api/fleet?fresh=1"))
    # The row also carries the request-time `provision_run_id` overlay, which is
    # authoritative: a provision that finished after the snapshot was built has
    # no reattachable run, so the pointer must read None rather than the id the
    # snapshot froze.
    assert json.loads(resp.text)["worktrees"] == [{"name": "a", "provision_run_id": None}]
    refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_fleet_handler_overlays_runs_started_after_snapshot(monkeypatch):
    """A run started AFTER the cached snapshot was built is still reported.

    The fleet cache is stale-while-revalidate, so a run pointer baked into the
    snapshot left a freshly-mounted page with nothing to reattach its progress
    stepper to for a full cache cycle plus a rebuild -- no progress, and a button
    still inviting a second press. Both pointers share that cause: the sync's,
    and each row's provision run.
    """
    cached = {
        "worktrees": [
            {"name": "main", "provision_run_id": None},
            {"name": "feature-x", "provision_run_id": None},
        ],
        "sync_run_id": None,
    }
    monkeypatch.setattr(fleet_state, "_fleet_cached", AsyncMock(return_value=cached))
    monkeypatch.setattr(worktree_ops, "_SYNC_RID", "rid-after-snapshot")
    monkeypatch.setattr(
        fleet_state, "_provision_reattach_ids", AsyncMock(return_value={"feature-x": "prov-rid-7"})
    )
    resp = await http_api.api_dev_fleet_fleet(make_mocked_request("GET", "/api/fleet"))
    body = json.loads(resp.text)
    assert body["sync_run_id"] == "rid-after-snapshot"
    rows = {w["name"]: w["provision_run_id"] for w in body["worktrees"]}
    assert rows == {"main": None, "feature-x": "prov-rid-7"}
    # The snapshot and its rows are the cache's own objects, shared with every
    # other in-flight request, so the overlay must copy rather than write through.
    assert cached["sync_run_id"] is None
    assert [w["provision_run_id"] for w in cached["worktrees"]] == [None, None]


@pytest.mark.asyncio
async def test_worktree_handler_requires_name():
    resp = await http_api.api_dev_fleet_worktree(make_mocked_request("GET", "/api/worktree"))
    assert resp.status == 400
    assert "missing 'name'" in json.loads(resp.text)["error"]


@pytest.mark.asyncio
async def test_worktree_handler_rejects_unknown_name(monkeypatch):
    monkeypatch.setattr(repository, "_valid_worktree_names", AsyncMock(return_value={"other"}))
    resp = await http_api.api_dev_fleet_worktree(make_mocked_request("GET", "/api/worktree?name=x"))
    assert resp.status == 400
    assert "unknown worktree" in json.loads(resp.text)["error"]


@pytest.mark.asyncio
async def test_worktree_handler_returns_detail(monkeypatch):
    monkeypatch.setattr(repository, "_valid_worktree_names", AsyncMock(return_value={"x"}))
    monkeypatch.setattr(fleet_state, "_worktree_detail", AsyncMock(return_value={"name": "x"}))
    resp = await http_api.api_dev_fleet_worktree(make_mocked_request("GET", "/api/worktree?name=x"))
    assert json.loads(resp.text) == {"name": "x"}


@pytest.mark.asyncio
async def test_pod_logs_handler_requires_name():
    resp = await http_api.api_dev_fleet_pod_logs(make_mocked_request("GET", "/api/pod/logs"))
    assert resp.status == 400


@pytest.mark.asyncio
async def test_pod_logs_handler_rejects_unknown_name(monkeypatch):
    monkeypatch.setattr(repository, "_valid_worktree_names", AsyncMock(return_value=set()))
    resp = await http_api.api_dev_fleet_pod_logs(make_mocked_request("GET", "/api/pod/logs?name=x"))
    assert resp.status == 400


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "expected_n"),
    [("", 120), ("&n=notanint", 120), ("&n=0", 1), ("&n=99999", 1000), ("&n=25", 25)],
)
async def test_pod_logs_handler_clamps_line_count(monkeypatch, query, expected_n):
    monkeypatch.setattr(repository, "_valid_worktree_names", AsyncMock(return_value={"x"}))
    seen: list[int] = []

    async def fake_logs(name, n):
        seen.append(n)
        return {"ok": True, "logs": ""}

    monkeypatch.setattr(worktree_ops, "_pod_logs", fake_logs)
    await http_api.api_dev_fleet_pod_logs(
        make_mocked_request("GET", f"/api/pod/logs?name=x{query}")
    )
    assert seen == [expected_n]


@pytest.mark.asyncio
async def test_run_handler_requires_id():
    resp = await http_api.api_dev_fleet_run(make_mocked_request("GET", "/api/run"))
    assert resp.status == 400


@pytest.mark.asyncio
async def test_run_handler_unknown_id_is_404(monkeypatch):
    monkeypatch.setattr(runtime, "_RUNS", {})
    resp = await http_api.api_dev_fleet_run(make_mocked_request("GET", "/api/run?id=nope"))
    assert resp.status == 404


@pytest.mark.asyncio
async def test_run_handler_tails_and_redacts_output(monkeypatch):
    lines = [f"line {i}" for i in range(80)]
    monkeypatch.setattr(runtime, "_RUNS", {"r1": {"status": "running", "output": lines}})
    resp = await http_api.api_dev_fleet_run(make_mocked_request("GET", "/api/run?id=r1"))
    payload = json.loads(resp.text)
    assert len(payload["output"]) == 60
    assert payload["output"][0] == "line 20"


@pytest.mark.asyncio
async def test_prune_and_disk_handlers_pass_through(monkeypatch):
    monkeypatch.setattr(worktree_ops, "_prune_candidates", AsyncMock(return_value={"ok": True}))
    monkeypatch.setattr(worktree_ops, "_prune_status", AsyncMock(return_value={"running": False}))
    monkeypatch.setattr(fleet_state, "_disk", AsyncMock(return_value={"status": "idle"}))

    r1 = await http_api.api_dev_fleet_prune_candidates(
        make_mocked_request("GET", "/api/prune-candidates")
    )
    r2 = await http_api.api_dev_fleet_prune_status(make_mocked_request("GET", "/api/prune-status"))
    r3 = await http_api.api_dev_fleet_disk(make_mocked_request("GET", "/api/disk"))
    assert json.loads(r1.text) == {"ok": True}
    assert json.loads(r2.text) == {"running": False}
    assert json.loads(r3.text) == {"status": "idle"}


# --------------------------------------------------------------------------
# _json_body + POST handler validation
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_json_body_rejects_invalid_json():
    body, err = await http_api._json_body(_raw_request(b"{oops", json_error=ValueError("bad")))
    assert body is None and err is not None and err.status == 400


@pytest.mark.asyncio
async def test_json_body_rejects_non_object():
    body, err = await http_api._json_body(_raw_request(b"[1, 2]"))
    assert body is None and err is not None
    assert "must be an object" in json.loads(err.text)["error"]


@pytest.mark.asyncio
async def test_json_body_empty_request_is_empty_dict():
    request = MagicMock()
    request.content_length = 0
    body, err = await http_api._json_body(request)
    assert body == {} and err is None


@pytest.mark.asyncio
async def test_json_body_unknown_charset_is_400_not_500():
    # An unknown ``charset=`` codec makes aiohttp's decode step raise LookupError,
    # not JSONDecodeError. A ValueError-only catch would let this escape as
    # a 500; it is a client-input mistake and must answer 400. Guards the widened
    # (LookupError, RecursionError, ValueError) catch against a regression.
    body, err = await http_api._json_body(
        _raw_request(b"{}", json_error=LookupError("unknown encoding: bogus-codec"))
    )
    assert body is None and err is not None and err.status == 400


@pytest.mark.asyncio
async def test_worktree_remove_handler_rejects_non_bool_force(monkeypatch):
    _sel_capture(monkeypatch)
    monkeypatch.setattr(repository, "_valid_worktree_names", AsyncMock(return_value={"feat"}))
    resp = await http_api.api_dev_fleet_worktree_remove(
        _json_request({"name": "feat", "force": "yes"})
    )
    assert resp.status == 400
    assert "force must be a boolean" in json.loads(resp.text)["error"]


@pytest.mark.asyncio
async def test_worktree_remove_handler_forwards_force(monkeypatch):
    _sel_capture(monkeypatch)
    monkeypatch.setattr(repository, "_valid_worktree_names", AsyncMock(return_value={"feat"}))
    remove = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(worktree_ops, "_worktree_remove", remove)

    resp = await http_api.api_dev_fleet_worktree_remove(
        _json_request({"name": "feat", "force": True})
    )
    assert resp.status == 200
    remove.assert_awaited_once_with("feat", True, discard_untracked_paths=None)


def test_same_path_survives_unresolvable_operands(tmp_path):
    """Defense in depth behind the handler gate: an operand Path.resolve()
    rejects (embedded NUL, or a symlink loop — RuntimeError on some
    platform/version combinations) means "not the same path", never a crash."""
    assert repository._same_path("\x00", "/tmp") is False
    assert repository._same_path("/tmp", "a\x00b") is False
    if sys.platform != "win32":
        a, b = tmp_path / "loop-a", tmp_path / "loop-b"
        a.symlink_to(b)
        b.symlink_to(a)
        assert repository._same_path(str(a), str(tmp_path)) is False


@pytest.mark.asyncio
async def test_prune_run_handler_rejects_invalid_json(monkeypatch):
    _sel_capture(monkeypatch)
    resp = await http_api.api_dev_fleet_prune_run(_raw_request(b"{", json_error=ValueError("bad")))
    assert resp.status == 400
    assert json.loads(resp.text)["ok"] is False


@pytest.mark.asyncio
async def test_prune_run_handler_rejects_non_object_body(monkeypatch):
    _sel_capture(monkeypatch)
    resp = await http_api.api_dev_fleet_prune_run(_raw_request(b"[]"))
    assert resp.status == 400
    assert "must be an object" in json.loads(resp.text)["error"]


@pytest.mark.asyncio
async def test_prune_run_handler_rejects_non_string_names(monkeypatch):
    _sel_capture(monkeypatch)
    resp = await http_api.api_dev_fleet_prune_run(_json_request({"names": ["a", 7]}))
    assert resp.status == 400
    assert "list of strings" in json.loads(resp.text)["error"]


@pytest.mark.asyncio
async def test_prune_run_handler_rejects_when_no_name_is_valid(monkeypatch):
    _sel_capture(monkeypatch)
    monkeypatch.setattr(repository, "_valid_worktree_names", AsyncMock(return_value={"other"}))
    resp = await http_api.api_dev_fleet_prune_run(_json_request({"names": ["ghost"]}))
    assert resp.status == 400
    assert json.loads(resp.text)["error"] == "no valid names"


@pytest.mark.asyncio
async def test_prune_run_handler_filters_to_valid_names(monkeypatch):
    _sel_capture(monkeypatch)
    monkeypatch.setattr(repository, "_valid_worktree_names", AsyncMock(return_value={"a"}))
    run = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(worktree_ops, "_prune_run", run)

    resp = await http_api.api_dev_fleet_prune_run(_json_request({"names": ["a", "ghost"]}))
    assert resp.status == 200
    run.assert_awaited_once_with(["a"])


@pytest.mark.asyncio
async def test_pod_name_action_rejects_empty_name(monkeypatch):
    _sel_capture(monkeypatch)
    resp = await http_api.api_dev_fleet_pod_up(_json_request({"name": ""}))
    assert resp.status == 400
    assert "non-empty string" in json.loads(resp.text)["error"]


@pytest.mark.asyncio
async def test_pod_name_action_rejects_ambiguous_basename(monkeypatch):
    """_find_worktree, not set membership, is the validator (collision safety)."""
    _sel_capture(monkeypatch)
    monkeypatch.setattr(
        repository, "_find_worktree", AsyncMock(return_value=(None, "ambiguous name 'feat'"))
    )
    resp = await http_api.api_dev_fleet_pod_restart(_json_request({"name": "feat"}))
    assert resp.status == 400
    assert json.loads(resp.text)["error"] == "ambiguous name 'feat'"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler_name", "action_name"),
    [
        ("api_dev_fleet_pod_up", "_pod_up"),
        ("api_dev_fleet_pod_down", "_pod_down"),
        ("api_dev_fleet_pod_restart", "_pod_restart"),
        ("api_dev_fleet_pod_token", "_pod_token"),
        ("api_dev_fleet_pod_provision", "_pod_provision"),
        ("api_dev_fleet_rebase", "_rebase"),
    ],
)
async def test_pod_handlers_dispatch_to_their_action(monkeypatch, handler_name, action_name):
    _sel_capture(monkeypatch)
    monkeypatch.setattr(
        repository, "_find_worktree", AsyncMock(return_value=({"path": "/w"}, None))
    )
    action = AsyncMock(return_value={"ok": True, "via": action_name})
    monkeypatch.setattr(worktree_ops, action_name, action)

    resp = await getattr(mod, handler_name)(_json_request({"name": "feat"}))
    assert json.loads(resp.text) == {"ok": True, "via": action_name}
    action.assert_awaited_once_with("feat")


@pytest.mark.asyncio
async def test_pod_provision_dismiss_handler_validates_and_dispatches(monkeypatch):
    _sel_capture(monkeypatch)
    monkeypatch.setattr(
        repository, "_find_worktree", AsyncMock(return_value=({"path": "/w"}, None))
    )
    dismiss = AsyncMock(return_value={"ok": True, "dismissed": True})
    monkeypatch.setattr(worktree_ops, "_pod_provision_dismiss", dismiss)

    resp = await http_api.api_dev_fleet_pod_provision_dismiss(
        _json_request({"name": "feat", "run_id": "run-1"})
    )

    assert json.loads(resp.text) == {"ok": True, "dismissed": True}
    dismiss.assert_awaited_once_with("feat", "run-1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw, json_error",
    [
        (b"{oops", ValueError("bad json")),
        (b"[1, 2]", None),
    ],
)
async def test_pod_provision_dismiss_handler_codes_a_malformed_body(monkeypatch, raw, json_error):
    """Every rejection from this endpoint carries a machine-readable code.

    A malformed or non-object body is rejected by the shared body parser, whose
    default 400 has no ``code``; the dismiss handler asks for one so a client
    can branch on the failure instead of matching prose.
    """
    _sel_capture(monkeypatch)
    dismiss = AsyncMock()
    monkeypatch.setattr(worktree_ops, "_pod_provision_dismiss", dismiss)

    resp = await http_api.api_dev_fleet_pod_provision_dismiss(
        _raw_request(raw, json_error=json_error)
    )

    assert resp.status == 400
    assert json.loads(resp.text)["code"] == "invalid_body"
    dismiss.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_handler_maps_already_running_to_409(monkeypatch):
    _sel_capture(monkeypatch)
    monkeypatch.setattr(
        worktree_ops,
        "_sync",
        AsyncMock(return_value={"ok": False, "error": "sync already running"}),
    )
    request = MagicMock()
    request.content_length = 0
    request.can_read_body = False
    resp = await http_api.api_dev_fleet_sync(request)
    assert resp.status == 409


@pytest.mark.asyncio
async def test_sync_handler_success_is_200(monkeypatch):
    _sel_capture(monkeypatch)
    monkeypatch.setattr(worktree_ops, "_sync", AsyncMock(return_value={"ok": True}))
    request = MagicMock()
    request.content_length = 0
    request.can_read_body = False
    resp = await http_api.api_dev_fleet_sync(request)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_audited_bodyless_request_has_empty_target(monkeypatch):
    sink = _sel_capture(monkeypatch)

    @http_api._audited("probe")
    async def handler(request):
        return web.json_response({"ok": True})

    request = MagicMock()
    request.content_length = 0
    request.can_read_body = False
    await handler(request)
    assert sink.events[0]["resources"] == ""
    assert sink.events[0]["outcome"] == "success"


@pytest.mark.asyncio
async def test_audited_joins_list_target(monkeypatch):
    sink = _sel_capture(monkeypatch)

    @http_api._audited("probe")
    async def handler(request):
        return web.json_response({"ok": True})

    await handler(_json_request({"names": ["a", "b", "c"]}))
    assert sink.events[0]["resources"] == "a,b,c"


@pytest.mark.asyncio
async def test_audited_ignores_unparsable_body(monkeypatch):
    sink = _sel_capture(monkeypatch)

    @http_api._audited("probe")
    async def handler(request):
        return web.json_response({"ok": True})

    await handler(_raw_request(b"not json at all"))
    assert sink.events[0]["resources"] == ""


@pytest.mark.asyncio
async def test_audited_non_dict_body_has_empty_target(monkeypatch):
    sink = _sel_capture(monkeypatch)

    @http_api._audited("probe")
    async def handler(request):
        return web.json_response({"ok": True})

    await handler(_raw_request(b"[1,2,3]"))
    assert sink.events[0]["resources"] == ""


@pytest.mark.asyncio
async def test_audited_read_failure_does_not_break_handler(monkeypatch):
    sink = _sel_capture(monkeypatch)

    @http_api._audited("probe")
    async def handler(request):
        return web.json_response({"ok": True})

    request = MagicMock()
    request.content_length = 5
    request.can_read_body = True
    request.read = AsyncMock(side_effect=RuntimeError("stream gone"))
    await handler(request)
    assert sink.events[0]["resources"] == ""


@pytest.mark.asyncio
async def test_audited_handler_exception_is_audited_and_reraised(monkeypatch):
    sink = _sel_capture(monkeypatch)

    @http_api._audited("probe")
    async def handler(request):
        raise KeyError("kaboom")

    with pytest.raises(KeyError):
        await handler(_json_request({"name": "feat"}))
    assert sink.events[0]["outcome"] == "failure"
    assert sink.events[0]["error"] == "KeyError"


@pytest.mark.asyncio
async def test_audited_server_error_is_failure(monkeypatch):
    sink = _sel_capture(monkeypatch)

    @http_api._audited("probe")
    async def handler(request):
        return web.json_response({"error": "internal"}, status=500)

    await handler(_json_request({"name": "feat"}))
    assert sink.events[0]["outcome"] == "failure"
    assert sink.events[0]["error"] == "internal"


@pytest.mark.asyncio
async def test_audited_non_json_response_still_audits_success(monkeypatch):
    sink = _sel_capture(monkeypatch)

    @http_api._audited("probe")
    async def handler(request):
        return web.Response(text="plain body")

    await handler(_json_request({"name": "feat"}))
    assert sink.events[0]["outcome"] == "success"


@pytest.mark.asyncio
async def test_audited_non_dict_json_response_is_success(monkeypatch):
    sink = _sel_capture(monkeypatch)

    @http_api._audited("probe")
    async def handler(request):
        return web.json_response([1, 2])

    await handler(_json_request({"name": "feat"}))
    assert sink.events[0]["outcome"] == "success"


@pytest.mark.asyncio
async def test_audited_error_free_denial_falls_back_to_status(monkeypatch):
    sink = _sel_capture(monkeypatch)

    @http_api._audited("probe")
    async def handler(request):
        return web.json_response({}, status=403)

    await handler(_json_request({"name": "feat"}))
    assert sink.events[0]["outcome"] == "denied"
    assert sink.events[0]["error"] == "http_403"


@pytest.mark.asyncio
async def test_audited_preserves_handler_identity():
    async def original(request):
        """Docstring stays."""
        return web.json_response({})

    wrapped = http_api._audited("probe")(original)
    assert wrapped.__name__ == "original"
    assert wrapped.__doc__ == "Docstring stays."


# --------------------------------------------------------------------------
# HMAC middleware denials
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_hmac_health_path_is_exempt(monkeypatch):
    called: list[bool] = []

    async def handler(request):
        called.append(True)
        return web.json_response({"status": "ok"})

    await http_api.hmac_proxy_middleware(make_mocked_request("GET", "/health"), handler)
    assert called == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "secret", "reason"),
    [
        ({}, "", "no app secret configured"),
        ({}, "s3cr3t", "missing X-KiroCrew-Proxy header"),
        ({"X-KiroCrew-Proxy": "nocolon"}, "s3cr3t", "malformed X-KiroCrew-Proxy header"),
        ({"X-KiroCrew-Proxy": "abc:sig"}, "s3cr3t", "invalid timestamp in proxy header"),
        ({"X-KiroCrew-Proxy": "1:sig"}, "s3cr3t", "proxy signature expired"),
    ],
)
async def test_hmac_denials(monkeypatch, headers, secret, reason):
    sink = _sel_capture(monkeypatch)
    monkeypatch.setattr(http_api, "_load_app_secret", lambda: secret)

    async def handler(request):  # pragma: no cover - must never run
        raise AssertionError("handler must not be reached")

    request = make_mocked_request("GET", "/api/fleet", headers=headers)
    resp = await http_api.hmac_proxy_middleware(request, handler)
    assert resp.status == 401
    assert reason in json.loads(resp.text)["error"]
    assert sink.events[0]["outcome"] == "denied"
    assert sink.events[0]["tool_name"] == "dev-fleet:proxy-hmac"


@pytest.mark.asyncio
@pytest.mark.parametrize("codepoint", [0x00E9, 0x63D0, 0x1F600])
async def test_hmac_non_ascii_signature_is_a_clean_denial(monkeypatch, codepoint):
    """A non-ASCII signature is refused like any other wrong one, not raised.

    ``hmac.compare_digest`` rejects a ``str`` holding a non-ASCII character by
    raising ``TypeError``. The header is attacker-chosen (any local process can
    reach this loopback port) and aiohttp decodes a non-UTF-8 header byte into a
    lone surrogate, so an unhandled raise would drop the connection and skip the
    ``dev-fleet:proxy-hmac`` SEL denial record this middleware exists to write.
    Same class as the shared verifier in ``apps/proxy_auth.py``, which this
    backend reimplements inline. Code points are built rather than written
    literally so a lone surrogate stays expressible.
    """
    sink = _sel_capture(monkeypatch)
    monkeypatch.setattr(http_api, "_load_app_secret", lambda: "s3cr3t")

    async def handler(request):  # pragma: no cover - must never run
        raise AssertionError("handler must not be reached")

    # flake-ok: pre-existing real-clock read for the signed timestamp, moved verbatim; tracked for a de-flake pass
    headers = {"X-KiroCrew-Proxy": f"{int(time.time())}:{chr(codepoint)}"}
    request = make_mocked_request("GET", "/api/fleet", headers=headers)
    resp = await http_api.hmac_proxy_middleware(request, handler)
    assert resp.status == 401
    assert "invalid proxy signature" in json.loads(resp.text)["error"]
    assert sink.events[0]["outcome"] == "denied"
    assert sink.events[0]["tool_name"] == "dev-fleet:proxy-hmac"


@pytest.mark.asyncio
async def test_hmac_a_valid_signature_suffixed_with_a_lone_surrogate_is_refused(monkeypatch):
    """A surrogate must keep a signature distinct, not be dropped from it.

    This is the case that separates ``surrogatepass`` from ``ignore`` at this
    verifier. A lone surrogate cannot be encoded as UTF-8 at all, so ``ignore``
    silently DROPS it: a valid signature with one appended encodes to the same
    bytes as the valid signature alone and the request is ACCEPTED.
    ``surrogatepass`` encodes it, so the bytes differ and the request earns the
    usual denial. No encodable character exercises this, because ``ignore``
    keeps those and the two encodings agree.

    The request is a mock rather than ``make_mocked_request`` because aiohttp's
    own header builder refuses to encode a lone surrogate. A real request does
    reach the middleware with one: the parser decodes an invalid UTF-8 header
    byte into exactly this shape.
    """
    sink = _sel_capture(monkeypatch)
    monkeypatch.setattr(http_api, "_load_app_secret", lambda: "s3cr3t")

    async def handler(request):
        return web.json_response({"ok": True})

    # flake-ok: pre-existing real-clock read for the signed timestamp, moved verbatim; tracked for a de-flake pass
    ts = int(time.time())
    msg = f"{ts}:GET:/api/fleet:{hashlib.sha256(b'').hexdigest()}"
    sig = hmac.new(b"s3cr3t", msg.encode(), hashlib.sha256).hexdigest()

    request = MagicMock()
    request.path = "/api/fleet"
    request.raw_path = "/api/fleet"
    request.method = "GET"
    request.can_read_body = False

    # A signature that verifies must be accepted first, so the refusal below is
    # about the appended surrogate and not about a signature that never matched.
    request.headers = {"X-KiroCrew-Proxy": f"{ts}:{sig}"}
    accepted = await http_api.hmac_proxy_middleware(request, handler)
    assert accepted.status == 200

    request.headers = {"X-KiroCrew-Proxy": f"{ts}:{sig}{chr(0xDCFF)}"}
    resp = await http_api.hmac_proxy_middleware(request, handler)
    assert resp.status == 401
    assert "invalid proxy signature" in json.loads(resp.text)["error"]
    assert sink.events[0]["outcome"] == "denied"


@pytest.mark.asyncio
async def test_hmac_denial_survives_audit_sink_failure(monkeypatch, caplog):
    """A broken SEL sink must never mask the 401."""

    def _boom():
        raise RuntimeError("sel down")

    monkeypatch.setattr(runtime, "_sel", _boom)
    monkeypatch.setattr(http_api, "_load_app_secret", lambda: "s3cr3t")

    async def handler(request):  # pragma: no cover - must never run
        raise AssertionError("handler must not be reached")

    with caplog.at_level("WARNING", logger=runtime.logger.name):
        resp = await http_api.hmac_proxy_middleware(
            make_mocked_request("GET", "/api/fleet"), handler
        )
    assert resp.status == 401
    assert "SEL emit failed" in caplog.text


# --------------------------------------------------------------------------
# worktree detail
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_worktree_detail_unknown_name(monkeypatch):
    monkeypatch.setattr(repository, "_find_worktree", AsyncMock(return_value=(None, "gone")))
    assert await fleet_state._worktree_detail("ghost") == {"error": "gone"}


@pytest.mark.asyncio
async def test_worktree_detail_includes_pod_state_and_design_docs(monkeypatch, tmp_path):
    wt = {"path": str(tmp_path), "branch": "feat", "is_main": False}
    monkeypatch.setattr(repository, "_find_worktree", AsyncMock(return_value=(wt, None)))
    monkeypatch.setattr(
        repository,
        "_git_info",
        AsyncMock(
            return_value={
                "branch": "feat",
                "head": "abc1234",
                "dirty": False,
                "ahead": 0,
                "behind": 0,
                "last_updated_at": None,
            }
        ),
    )
    monkeypatch.setattr(fleet_state, "_pr_status_cached", AsyncMock(return_value=None))
    monkeypatch.setattr(repository, "_own_commits_count", AsyncMock(return_value=1))
    monkeypatch.setattr(fleet_state, "_context_cached", AsyncMock(return_value={}))

    async def fake_git(path, *args, **kw):
        if args[0] == "log":
            return "abc1234\x1ffeat: do a thing\x1f2 hours ago\nmalformed line"
        if args[0] == "diff":
            return "docs/design/plan.md\nsrc/app.py\ndocs/design/plan.md\n"
        return ""

    monkeypatch.setattr(repository, "_git", fake_git)
    monkeypatch.setattr(runtime, "_trusted_bin", lambda name: "/usr/bin/du")
    monkeypatch.setattr(runtime, "_run_cmd", AsyncMock(return_value=(0, "31\t.", "")))
    monkeypatch.setattr(runtime, "_load_cfg", lambda: SimpleNamespace())
    monkeypatch.setattr(runtime, "_POD_AVAILABLE", True)
    name = Path(str(tmp_path)).name
    monkeypatch.setattr(
        runtime,
        "rt",
        SimpleNamespace(
            active_names=lambda cfg: {name},
            derive_port=lambda cfg, nm: 9321,
        ),
        raising=False,
    )

    detail = await fleet_state._worktree_detail(name)
    assert detail["pod_running"] is True
    assert detail["pod_port"] == 9321
    assert detail["disk_mb"] == 31
    assert detail["commits"] == [
        {"hash": "abc1234", "subject": "feat: do a thing", "when": "2 hours ago"}
    ]
    assert detail["design_docs"] == ["docs/design/plan.md"]


@pytest.mark.asyncio
async def test_worktree_detail_survives_pod_probe_failure(monkeypatch, tmp_path):
    wt = {"path": str(tmp_path), "branch": None, "is_main": True}
    monkeypatch.setattr(repository, "_find_worktree", AsyncMock(return_value=(wt, None)))
    monkeypatch.setattr(
        repository,
        "_git_info",
        AsyncMock(
            return_value={
                "branch": "main",
                "head": "abc1234",
                "dirty": False,
                "ahead": 0,
                "behind": 0,
                "last_updated_at": None,
            }
        ),
    )
    monkeypatch.setattr(repository, "_own_commits_count", AsyncMock(return_value=0))
    monkeypatch.setattr(fleet_state, "_context_cached", AsyncMock(return_value={}))
    monkeypatch.setattr(repository, "_git", AsyncMock(return_value=""))
    monkeypatch.setattr(runtime, "_trusted_bin", lambda name: "/usr/bin/du")
    monkeypatch.setattr(runtime, "_run_cmd", AsyncMock(return_value=(1, "", "du failed")))
    monkeypatch.setattr(runtime, "_load_cfg", lambda: None)

    detail = await fleet_state._worktree_detail(Path(str(tmp_path)).name)
    assert detail["pod_running"] is False
    assert detail["disk_mb"] is None
    assert detail["commits"] == []


def test_dir_size_bytes_does_not_follow_a_directory_link():
    """A directory symlink/junction inside the tree must not be walked into.

    ``entry.is_dir(follow_symlinks=False)`` reports True for a Windows
    junction (a reparse point, not a symlink), so a naive walk would push it
    and recurse through the link's target -- unbounded on a cycle (a link
    pointing back at an ancestor) and potentially an outbound SMB connection
    if the target is a network share.

    A real junction cannot be created on this (POSIX) test host, and a plain
    symlink does not exercise the guard: ``is_dir(follow_symlinks=False)``
    reports False for a symlink on every platform, so only a junction's
    ``is_dir(False) == True`` reaches the branch the guard protects. So this
    drives that SAME branch by monkeypatching ``os.DirEntry.is_dir`` to answer
    the way a junction's DirEntry does (True even with follow_symlinks=False)
    for one specific path, and asserts ``is_link_or_junction`` -- not
    ``is_dir`` -- is what the walk consults before recursing into it.
    """
    import kiro_crew.apps.builtins.dev_fleet.fleet_state as fleet_state_mod

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = base / "walked-root"
        root.mkdir()
        (root / "real.bin").write_bytes(b"x" * 1000)
        outside = base / "outside-target"
        outside.mkdir()
        (outside / "elsewhere.bin").write_bytes(b"y" * 5000)
        junction_path = root / "junction-to-outside"
        # No real reparse point on POSIX; a symlink stands in as the on-disk
        # object so scandir yields a real DirEntry, but its is_dir() answer is
        # forced below to the junction shape the fix must handle.
        junction_path.symlink_to(outside, target_is_directory=True)
        real_is_dir = os.DirEntry.is_dir
        real_is_link_or_junction = fleet_state_mod.is_link_or_junction

        def fake_is_dir(self, *, follow_symlinks=True):
            if self.path == str(junction_path):
                return True
            return real_is_dir(self, follow_symlinks=follow_symlinks)

        def fake_is_link_or_junction(path):
            if str(path) == str(junction_path):
                return True
            return real_is_link_or_junction(path)

        with (
            mock.patch.object(os.DirEntry, "is_dir", fake_is_dir),
            mock.patch.object(fleet_state_mod, "is_link_or_junction", fake_is_link_or_junction),
        ):
            size = fleet_state_mod._dir_size_bytes(str(root))

    assert size == 1000, "the walk must count only real.bin, never through the junction"

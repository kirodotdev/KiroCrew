"""``POST /api/apps/{name}/update`` switching a local-only grant to the repository.

An app installed from a local folder holds a local grant, which never covers a
registry clone, so its registry update is refused with ``app_trust_local_only``.
That refusal carries the repository the update installs, and the update route
takes it back as ``trustRepository`` consent proof. These tests pin the switch:
the proof must match the row the install resolves and the grant must be exactly
the local one, both checked before anything stops; the repository grant is in
place before the install runs; and a failed update puts the local grant back
before the old app starts again, so the old code starts admitted.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from apps_routes_coverage_helpers import APP, _deregister_into, _install, _make_app, _setup_env

import kiro_crew.apps.registry as registry_mod
import kiro_crew.apps.routes as routes_mod
import kiro_crew.sel as sel_mod
from kiro_crew.apps.bridges import RegistrationResult
from kiro_crew.apps.manager import enable_app, set_app_provenance
from kiro_crew.apps.routes import register_app_routes
from kiro_crew.config.loader import _invalidate_config_cache
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, await_until

#: The clone URL the registry row names, and its normalized consent form.
CLONE_URL = "https://clone.example.test/Owner/cov-test-app.git"
REPOSITORY = "https://clone.example.test/Owner/cov-test-app"


def _write_agent(home: Path, agent: dict[str, Any]) -> None:
    (home / "config.json").write_text(json.dumps({"agent": agent}), encoding="utf-8")
    _invalidate_config_cache()


def _agent(home: Path) -> dict[str, Any]:
    return json.loads((home / "config.json").read_text(encoding="utf-8"))["agent"]


LOCAL_GRANT = {
    "apps_allow_third_party": False,
    "apps_trusted": [APP],
    "apps_trusted_local": [APP],
    "apps_trusted_repositories": {},
}
LEGACY_LOCAL_GRANT = {
    "apps_allow_third_party": False,
    "apps_trusted": [APP],
    "apps_trusted_repositories": {},
}


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A local-folder install of ``APP``, enabled, under a local-only grant."""
    home = _setup_env(tmp_path, monkeypatch)
    _install(tmp_path)
    assert enable_app(APP).ok
    _write_agent(home, LOCAL_GRANT)
    monkeypatch.setattr(
        registry_mod,
        "_resolve_install_entry",
        lambda name: ({"name": name, "gitUrl": CLONE_URL}, ""),
    )
    return home


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    log = MagicMock()
    monkeypatch.setattr(sel_mod, "sel", lambda: log)
    return log


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the lifecycle steps the route takes, without running any of them."""
    seen: list[str] = []

    def _register(name: str) -> RegistrationResult:
        seen.append("register")
        return RegistrationResult()

    monkeypatch.setattr(routes_mod, "stop_app_backend", lambda n: seen.append("stop"))
    monkeypatch.setattr(routes_mod, "deregister_app", _deregister_into(seen))
    monkeypatch.setattr(routes_mod, "register_app", _register)
    monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: seen.append("start"))
    return seen


#: The steps of an update that takes the app down at the copy and then brings it
#: back, after a success or a failure past the take-down.
TAKEN_DOWN_AND_BACK = ["install", "stop", "deregister (agents kept)", "register", "start"]


def _switch_body(repository: str = REPOSITORY) -> dict[str, str]:
    return {"source": f"registry:{APP}", "trustRepository": repository}


def _grant_audits(log: MagicMock) -> list[tuple[str, str]]:
    return [
        (c.kwargs["outcome"], c.kwargs["resources"])
        for c in log.log_api_access.call_args_list
        if c.kwargs.get("operation") == "security.trusted_apps.grant"
    ]


@pytest.mark.asyncio
async def test_switch_binds_the_repository_before_the_install_and_keeps_it(
    home: Path, calls: list[str], audit: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen_at_install: dict[str, Any] = {}

    async def _install_from_registry(name: str, **kwargs: Any) -> dict[str, Any]:
        calls.append("install")
        seen_at_install.update(_agent(home))
        await kwargs["before_replace"]()
        return {"ok": True, "name": name}

    monkeypatch.setattr(routes_mod, "install_from_registry", _install_from_registry)

    async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
        resp = await client.post(f"/api/apps/{APP}/update", json=_switch_body())
        assert resp.status == 200, await resp.text()

    # The repository grant is what the install ran under: no repository code is
    # fetched or run under the local grant, and the local marker is gone.
    assert seen_at_install["apps_trusted_repositories"] == {APP: REPOSITORY}
    assert APP not in seen_at_install["apps_trusted_local"]
    assert calls == TAKEN_DOWN_AND_BACK, calls
    after = _agent(home)
    assert after["apps_trusted"] == [APP]
    assert after["apps_trusted_repositories"] == {APP: REPOSITORY}
    assert after["apps_trusted_local"] == []
    assert _grant_audits(audit) == [("ok", f"{APP}=registry_update")]


@pytest.mark.asyncio
async def test_a_legacy_local_grant_switches_and_keeps_the_repository_binding(
    home: Path, calls: list[str], audit: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen_at_install: dict[str, Any] = {}
    _write_agent(home, LEGACY_LOCAL_GRANT)

    async def _install_from_registry(name: str, **kwargs: Any) -> dict[str, Any]:
        calls.append("install")
        seen_at_install.update(_agent(home))
        await kwargs["before_replace"]()
        return {"ok": True, "name": name}

    monkeypatch.setattr(routes_mod, "install_from_registry", _install_from_registry)

    async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
        resp = await client.post(f"/api/apps/{APP}/update", json=_switch_body())
        assert resp.status == 200, await resp.text()

    expected = {**LEGACY_LOCAL_GRANT, "apps_trusted_repositories": {APP: REPOSITORY}}
    assert seen_at_install == expected
    assert _agent(home) == expected
    assert calls == TAKEN_DOWN_AND_BACK, calls
    assert _grant_audits(audit) == [("ok", f"{APP}=registry_update")]


@pytest.mark.asyncio
async def test_a_failed_legacy_switch_restores_the_grant_without_a_local_marker(
    home: Path, calls: list[str], audit: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_agent(home, LEGACY_LOCAL_GRANT)

    async def _install_from_registry(name: str, **kwargs: Any) -> dict[str, Any]:
        calls.append("install")
        await kwargs["before_replace"]()
        return {"ok": False, "name": name, "error": "copy failed"}

    monkeypatch.setattr(routes_mod, "install_from_registry", _install_from_registry)

    async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
        resp = await client.post(f"/api/apps/{APP}/update", json=_switch_body())
        assert resp.status == 400

    assert _agent(home) == LEGACY_LOCAL_GRANT
    assert calls == TAKEN_DOWN_AND_BACK, calls
    assert _grant_audits(audit) == [
        ("ok", f"{APP}=registry_update"),
        ("reverted", f"{APP}=registry_update"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["refused", "refused-early", "raised", "stop-raised"])
async def test_a_failed_update_puts_the_local_grant_back_before_the_old_app_starts(
    home: Path, calls: list[str], audit: MagicMock, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    grant_at_start: dict[str, Any] = {}

    async def _install_from_registry(name: str, **kwargs: Any) -> dict[str, Any]:
        calls.append("install")
        if failure == "refused-early":
            # A clone or build failure comes before the take-down: the old app
            # never stopped, and only the grant goes back.
            return {"ok": False, "name": name, "error": "clone failed"}
        await kwargs["before_replace"]()
        if failure == "raised":
            raise RuntimeError("copy exploded")
        return {"ok": False, "name": name, "error": "copy failed"}

    def _start(name: str) -> None:
        calls.append("start")
        grant_at_start.update(_agent(home))

    def _stop(name: str) -> None:
        calls.append("stop")
        if failure == "stop-raised":
            # The take-down fails after the grant write: the grant must not stay
            # on the repository over the local code still there.
            raise RuntimeError("backend would not stop")

    monkeypatch.setattr(routes_mod, "install_from_registry", _install_from_registry)
    monkeypatch.setattr(routes_mod, "start_app_backend", _start)
    monkeypatch.setattr(routes_mod, "stop_app_backend", _stop)

    async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
        resp = await client.post(f"/api/apps/{APP}/update", json=_switch_body())
        assert resp.status == (400 if failure.startswith("refused") else 500)

    after = _agent(home)
    assert after["apps_trusted"] == [APP]
    assert after["apps_trusted_local"] == [APP]
    assert after["apps_trusted_repositories"] == {}
    if failure == "refused":
        # The restore restarted the old local code under its own grant again.
        assert calls == TAKEN_DOWN_AND_BACK, calls
        assert grant_at_start["apps_trusted_local"] == [APP]
        assert grant_at_start["apps_trusted_repositories"] == {}
    if failure == "refused-early":
        # The health watch can stop the old app while the clone runs under the
        # repository grant, so the route starts it again, under its own grant.
        assert calls == ["install", "start"], calls
        assert grant_at_start["apps_trusted_local"] == [APP]
        assert grant_at_start["apps_trusted_repositories"] == {}
    assert _grant_audits(audit) == [
        ("ok", f"{APP}=registry_update"),
        ("reverted", f"{APP}=registry_update"),
    ]


@pytest.mark.asyncio
async def test_a_failed_result_after_repository_provenance_keeps_the_repository_grant(
    home: Path, calls: list[str], audit: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _install_from_registry(name: str, **kwargs: Any) -> dict[str, Any]:
        calls.append("install")
        await kwargs["before_replace"]()
        assert set_app_provenance(
            APP,
            source=f"registry:{APP}",
            url=REPOSITORY,
        )
        return {"ok": False, "name": name, "error": "provenance bookkeeping failed"}

    monkeypatch.setattr(routes_mod, "install_from_registry", _install_from_registry)

    async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
        resp = await client.post(f"/api/apps/{APP}/update", json=_switch_body())
        assert resp.status == 400

    after = _agent(home)
    assert after["apps_trusted_repositories"] == {APP: REPOSITORY}
    assert APP not in after["apps_trusted_local"]
    assert _grant_audits(audit) == [
        ("ok", f"{APP}=registry_update"),
        ("revert_skipped", f"{APP}=registry_update"),
    ]


@pytest.mark.asyncio
async def test_a_grant_revoked_between_the_check_and_the_write_is_not_put_back(
    home: Path, calls: list[str], audit: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another process can revoke the grant after the check and before the lock.

    The locked write re-reads the file, so it refuses instead of appending the
    name again, which would undo the operator's revocation.
    """
    from kiro_crew.apps import local_trust_switch

    revoked = {"apps_allow_third_party": False, "apps_trusted": [], "apps_trusted_local": []}
    real_check = local_trust_switch._check_switch_sync

    def _check_then_revoke(name: str, consent: str) -> Any:
        checked = real_check(name, consent)
        _write_agent(home, revoked)
        return checked

    async def _never(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("a refused consent must not reach the install")

    monkeypatch.setattr(local_trust_switch, "_check_switch_sync", _check_then_revoke)
    monkeypatch.setattr(routes_mod, "install_from_registry", _never)

    async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
        resp = await client.post(f"/api/apps/{APP}/update", json=_switch_body())
        assert resp.status == 409
        assert (await resp.json())["code"] == "app_trust_switch_not_applicable"

    assert calls == []
    assert _agent(home) == revoked
    assert _grant_audits(audit) == [("denied", f"{APP}=app_trust_switch_not_applicable")]


@pytest.mark.asyncio
async def test_a_request_cancelled_mid_write_reverts_the_grant_that_landed(
    home: Path, audit: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write runs in a thread, so it can land after the request is cancelled."""
    import asyncio

    from kiro_crew.apps import local_trust_switch

    writing = asyncio.Event()
    release = asyncio.Event()
    real_bind = local_trust_switch.bind_repository

    async def _slow_bind(name: str, repository: str) -> bool:
        writing.set()
        await await_until(release, what="the test releasing the trust write")
        return await real_bind(name, repository)

    monkeypatch.setattr(local_trust_switch, "bind_repository", _slow_bind)

    request = asyncio.ensure_future(
        local_trust_switch.switch_cancel_safe(APP, REPOSITORY, caller="owner")
    )
    writing_wait = asyncio.ensure_future(writing.wait())
    try:
        done, _ = await asyncio.wait(
            {writing_wait, request},
            timeout=FALLBACK_TIMEOUT_SECS,
            return_when=asyncio.FIRST_COMPLETED,
        )
        assert done, "the trust write did not start before the lost-run deadline"
        if request in done:
            request.result()
            pytest.fail("the request completed before the trust write started")
        assert writing_wait in done, "the trust write did not signal that it started"

        # Cancelling through the shield takes effect at once; the write lands after it.
        request.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await await_until(request, what="the cancelled trust-switch request settling")
    finally:
        release.set()
        for task in (request, writing_wait):
            if not task.done():
                task.cancel()
        await await_until(
            asyncio.gather(request, writing_wait, return_exceptions=True),
            what="the cancellation test tasks settling",
        )

    assert _agent(home) == LOCAL_GRANT
    assert _grant_audits(audit) == [
        ("ok", f"{APP}=registry_update"),
        ("reverted", f"{APP}=registry_update"),
    ]


@pytest.mark.asyncio
async def test_a_failed_revert_still_restores_the_old_app(
    home: Path, calls: list[str], audit: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A trust write that fails during the revert must not skip the app restore."""
    from kiro_crew.apps import agent_config_writer

    async def _install_from_registry(name: str, **kwargs: Any) -> dict[str, Any]:
        async def _disk_full(mutate: Any) -> None:
            raise OSError(28, "No space left on device")

        # Only the revert's write fails; the switch's write already landed.
        monkeypatch.setattr(agent_config_writer, "mutate_trust_settings", _disk_full)
        await kwargs["before_replace"]()
        return {"ok": False, "name": name, "error": "copy failed"}

    monkeypatch.setattr(routes_mod, "install_from_registry", _install_from_registry)

    async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
        resp = await client.post(f"/api/apps/{APP}/update", json=_switch_body())
        assert resp.status == 400

    assert calls == ["stop", "deregister (agents kept)", "register", "start"], calls
    # The repository grant stays, which does not cover the local code: refused, not widened.
    assert _agent(home)["apps_trusted_repositories"] == {APP: REPOSITORY}
    assert ("revert_skipped", f"{APP}=registry_update") in _grant_audits(audit)


@pytest.mark.asyncio
async def test_a_grant_changed_while_the_update_ran_is_not_reverted(
    home: Path, calls: list[str], audit: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = "https://clone.example.test/Owner/elsewhere"

    async def _install_from_registry(name: str, **kwargs: Any) -> dict[str, Any]:
        # The operator re-pointed the grant from another window mid-update.
        _write_agent(home, {**_agent(home), "apps_trusted_repositories": {APP: other}})
        return {"ok": False, "name": name, "error": "clone failed"}

    monkeypatch.setattr(routes_mod, "install_from_registry", _install_from_registry)

    async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
        resp = await client.post(f"/api/apps/{APP}/update", json=_switch_body())
        assert resp.status == 400

    after = _agent(home)
    assert after["apps_trusted_repositories"] == {APP: other}
    assert APP not in after["apps_trusted_local"]
    assert ("revert_skipped", f"{APP}=registry_update") in _grant_audits(audit)


@pytest.mark.asyncio
async def test_a_stale_proof_is_refused_before_anything_stops(
    home: Path, calls: list[str], audit: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _never(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("a refused consent must not reach the install")

    monkeypatch.setattr(routes_mod, "install_from_registry", _never)

    async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
        resp = await client.post(
            f"/api/apps/{APP}/update",
            json=_switch_body("https://clone.example.test/Owner/a-different-repo"),
        )
        assert resp.status == 409
        body = await resp.json()

    assert body["code"] == "app_trust_repository_mismatch"
    # Neither coordinate is echoed back.
    assert "clone.example.test" not in body["error"]
    assert calls == []
    assert _agent(home) == LOCAL_GRANT
    assert _grant_audits(audit) == [("denied", f"{APP}=app_trust_repository_mismatch")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "agent",
    [
        # A repository-bound grant is not the local-only grant the switch replaces.
        {**LOCAL_GRANT, "apps_trusted_local": [], "apps_trusted_repositories": {APP: REPOSITORY}},
        # No grant at all: there is nothing local to move.
        {"apps_allow_third_party": False, "apps_trusted": [], "apps_trusted_local": []},
        # A local marker without its name grant is inert, so it is not a grant either.
        {"apps_allow_third_party": False, "apps_trusted": [], "apps_trusted_local": [APP]},
    ],
    ids=["repository-bound", "untrusted", "inert-marker"],
)
async def test_the_switch_needs_exactly_the_local_only_grant(
    home: Path,
    calls: list[str],
    audit: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    agent: dict[str, Any],
) -> None:
    _write_agent(home, agent)

    async def _never(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("a refused consent must not reach the install")

    monkeypatch.setattr(routes_mod, "install_from_registry", _never)

    async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
        resp = await client.post(f"/api/apps/{APP}/update", json=_switch_body())
        assert resp.status == 409
        assert (await resp.json())["code"] == "app_trust_switch_not_applicable"

    assert calls == []
    assert _agent(home) == agent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"trustRepository": REPOSITORY},
        {"source": "/some/folder", "trustRepository": REPOSITORY},
        {"source": "registry:another-app", "trustRepository": REPOSITORY},
        {"source": f"registry:{APP}", "trustRepository": ""},
        {"source": f"registry:{APP}", "trustRepository": ["not", "a", "string"]},
    ],
    ids=["no-source", "path-source", "other-app", "empty", "non-string"],
)
async def test_consent_is_accepted_only_with_this_apps_registry_source(
    home: Path, calls: list[str], monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]
) -> None:
    async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
        resp = await client.post(f"/api/apps/{APP}/update", json=body)
        assert resp.status == 400
        assert (await resp.json())["code"] == "invalid_repository_consent"

    assert calls == []
    assert _agent(home) == LOCAL_GRANT


@pytest.mark.asyncio
async def test_an_app_token_cannot_switch_its_own_trust(
    home: Path, calls: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route is owner-only, so an app cannot consent to a repository for itself."""
    from kiro_crew.dashboard.token_auth import generate_token, token_auth_middleware

    token = generate_token(APP, ttl_seconds=300, app=APP)  # flake-ok: token lifetime, no wait
    app = web.Application(middlewares=[token_auth_middleware()])
    app["state"] = SimpleNamespace(owner_id="owner")
    register_app_routes(app)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            f"/api/apps/{APP}/update", params={"token": token}, json=_switch_body()
        )
        assert resp.status == 403
        assert (await resp.json())["code"] == "owner_only"

    assert calls == []
    assert _agent(home) == LOCAL_GRANT

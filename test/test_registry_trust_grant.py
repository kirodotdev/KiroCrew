"""Operator grants of ``owner`` trust to hand-configured app registries.

The grant lives in the keystone ``registry_trust.json``, keyed by the registry's
credential-free repository URL, and is the second (and only other) source of the
``owner`` tier besides a build-pinned row. Pinned here:

- the runtime reader (``_registry_trust_tier``) honours a grant for a config row
  whose repository matches, and for nothing else;
- the grant follows the REPOSITORY, so a rewritten config row loses it;
- a pinned registry's tier is the build's, grant or no grant;
- every malformed shape of the file resolves to ``index``;
- the file is on the agent-unreadable, agent-unwritable keystone floor;
- the three ``/api/security/trusted-registries`` endpoints: snapshot, grant
  (owner-only, configured rows only, pinned refused, cache expired, audited,
  owner-only file mode), revoke (idempotent), corrupt file (500).

The aiohttp handlers run through an in-test ``TestClient`` opened with
``async with``, matching ``test_trusted_apps_api.py``.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Callable
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from conftest import requires_symlinks
from kiro_crew.apps.registry_pipeline import indexes, sources
from kiro_crew.config.loader import _invalidate_config_cache, registry_trust_path
from kiro_crew.dashboard.handlers import security
from kiro_crew.dashboard.handlers.security import (
    api_trusted_registries_list,
    api_trusted_registry_grant,
    api_trusted_registry_revoke,
    build_trusted_registries_snapshot,
)

_REPO = "https://git.example.test/team/apps-index.git"
_REPO_PUBLIC = "https://git.example.test/team/apps-index"
_OTHER = "https://git.example.test/other/index.git"
_PINNED_REPO = "https://git.example.test/build/pinned-index.git"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "kirocrew-home"
    h.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(h))
    _invalidate_config_cache()
    yield h
    _invalidate_config_cache()


@pytest.fixture
def no_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sources, "_pinned_registries", lambda: [])


@pytest.fixture
def mock_sel():
    with patch("kiro_crew.dashboard.handlers.security._sel") as m:
        instance = MagicMock()
        m.return_value = instance
        yield instance


def _write_config(home: Path, registries: list[dict]) -> None:
    (home / "config.json").write_text(json.dumps({"registries": registries}), encoding="utf-8")
    _invalidate_config_cache()


def _write_grant(*repos: str) -> None:
    """Write the keystone with *repos* granted, in the current version-1 shape.

    Version 1 stores ``owner_trusted`` as a JSON LIST of credential-free repo URLs.
    It is the only shape a reader accepts; an unknown-version or dict-shaped document
    is corrupt.
    """
    registry_trust_path().write_text(
        json.dumps({"version": 1, "owner_trusted": list(repos)}),
        encoding="utf-8",
    )


def _pinned(*, trust: str = "owner"):
    return [
        SimpleNamespace(
            name="pinned", repo=_PINNED_REPO, branch="main", trust=trust, label="", review=""
        )
    ]


class TestTheRuntimeReader:
    def test_a_config_row_is_index_with_no_grant(self, home, no_pinned) -> None:
        _write_config(home, [{"name": "mine", "repo": _REPO, "branch": "main"}])
        assert sources._registry_trust_tier("mine") == sources._TRUST_INDEX

    def test_a_grant_on_the_rows_repository_lifts_it_to_owner(self, home, no_pinned) -> None:
        _write_config(home, [{"name": "mine", "repo": _REPO, "branch": "main"}])
        _write_grant(_REPO_PUBLIC)
        assert sources._registry_trust_tier("mine") == sources._TRUST_OWNER

    def test_the_grant_follows_the_repository_not_the_name(self, home, no_pinned) -> None:
        # The agent can rewrite config.json. Pointing the granted NAME at another
        # index must not carry the grant along; renaming the row must keep it.
        _write_grant(_REPO_PUBLIC)
        _write_config(home, [{"name": "mine", "repo": _OTHER, "branch": "main"}])
        assert sources._registry_trust_tier("mine") == sources._TRUST_INDEX
        _write_config(home, [{"name": "renamed", "repo": _REPO, "branch": "main"}])
        assert sources._registry_trust_tier("renamed") == sources._TRUST_OWNER

    def test_the_rows_own_trust_field_is_never_consulted(self, home, no_pinned) -> None:
        _write_config(home, [{"name": "mine", "repo": _REPO, "branch": "main", "trust": "owner"}])
        assert sources._registry_trust_tier("mine") == sources._TRUST_INDEX

    def test_a_pinned_registry_keeps_the_builds_tier(self, home, monkeypatch) -> None:
        monkeypatch.setattr(sources, "_pinned_registries", lambda: _pinned(trust="index"))
        _write_config(home, [])
        _write_grant(_PINNED_REPO)
        # A grant naming a pinned repository is inert: the build said `index`.
        assert sources._registry_trust_tier("pinned") == sources._TRUST_INDEX
        monkeypatch.setattr(sources, "_pinned_registries", lambda: _pinned(trust="owner"))
        registry_trust_path().unlink()
        assert sources._registry_trust_tier("pinned") == sources._TRUST_OWNER

    def test_a_grant_is_inert_when_the_repo_is_also_pinned(self, home, monkeypatch) -> None:
        # A config row and a pinned row share repository R under DIFFERENT names,
        # so the config row survives the name merge; a grant on R must not lift it,
        # because the pinned row already states that repository's (index) tier.
        monkeypatch.setattr(
            sources,
            "_pinned_registries",
            lambda: [
                SimpleNamespace(
                    name="pinned", repo=_REPO, branch="main", trust="index", label="", review=""
                )
            ],
        )
        _write_config(home, [{"name": "mine", "repo": _REPO, "branch": "main"}])
        _write_grant(_REPO_PUBLIC)
        assert sources._registry_trust_tier("mine") == sources._TRUST_INDEX

    @pytest.mark.parametrize(
        "content",
        [
            "not json",
            "[]",
            json.dumps({"version": 999, "owner_trusted": [_REPO_PUBLIC]}),
            json.dumps({"version": 1, "owner_trusted": {_REPO_PUBLIC: {}}}),
            json.dumps({"version": 2, "owner_trusted": [_REPO_PUBLIC]}),
            json.dumps({"version": 1, "owner_trusted": ["https://u:p@git.example.test/team/apps"]}),
        ],
        ids=[
            "not-json",
            "not-object",
            "unknown-version",
            "v1-dict-not-list",
            "v2-list",
            "credentialed-key",
        ],
    )
    def test_every_malformed_shape_resolves_to_index(self, home, no_pinned, content) -> None:
        _write_config(home, [{"name": "mine", "repo": _REPO, "branch": "main"}])
        registry_trust_path().write_text(content, encoding="utf-8")
        assert sources._granted_owner_repos() == frozenset()
        assert sources._registry_trust_tier("mine") == sources._TRUST_INDEX

    def test_a_missing_file_is_no_grants(self, home, no_pinned) -> None:
        assert not registry_trust_path().exists()
        assert sources._granted_owner_repos() == frozenset()

    def test_an_unknown_version_or_a_dict_is_corrupt(self, home, no_pinned) -> None:
        # Only the version-1 list parses: an unknown version or a dict-shaped
        # `owner_trusted` is corrupt, so the tolerant reader yields no grants and
        # the tier falls back to index.
        _write_config(home, [{"name": "mine", "repo": _REPO, "branch": "main"}])
        registry_trust_path().write_text(
            json.dumps({"version": 2, "owner_trusted": [_REPO_PUBLIC]}), encoding="utf-8"
        )
        assert sources._granted_owner_repos() == frozenset()
        registry_trust_path().write_text(
            json.dumps({"version": 1, "owner_trusted": {_REPO_PUBLIC: {}}}), encoding="utf-8"
        )
        assert sources._granted_owner_repos() == frozenset()
        assert sources._registry_trust_tier("mine") == sources._TRUST_INDEX

    def test_a_version_one_list_reads(self, home, no_pinned) -> None:
        # The version-1 list of a repo reads to the grant set of that repo.
        _write_config(home, [{"name": "mine", "repo": _REPO, "branch": "main"}])
        _write_grant(_REPO_PUBLIC)
        assert sources._granted_owner_repos() == frozenset({_REPO_PUBLIC})
        assert sources._registry_trust_tier("mine") == sources._TRUST_OWNER

    def test_a_hand_edited_grant_on_a_plaintext_transport_is_ignored(self, home, no_pinned) -> None:
        # ``registry_trust.json`` is edited outside the handler (a keystone the
        # operator may edit by hand), so the read side must not honour an ``owner``
        # grant on a transport the grant handler would refuse: ``owner`` clones with
        # the machine's git identity, and a plaintext fetch lets the path substitute
        # the code. The reader keys the tier on ``_operator_granted_owner``.
        plain = "http://git.example.test/team/apps-index"
        _write_config(home, [{"name": "mine", "repo": plain, "branch": "main"}])
        _write_grant(plain)
        reg = SimpleNamespace(name="mine", repo=plain, branch="main")
        assert sources._operator_granted_owner(reg) is False
        assert sources._registry_trust_tier("mine") == sources._TRUST_INDEX
        # Negative control: the identical grant over https IS honoured, so the
        # refusal above is a property of the transport, not of the reader.
        secure = "https://git.example.test/team/apps-index"
        _write_config(home, [{"name": "mine", "repo": secure, "branch": "main"}])
        _write_grant(secure)
        assert sources._operator_granted_owner(SimpleNamespace(name="mine", repo=secure)) is True
        assert sources._registry_trust_tier("mine") == sources._TRUST_OWNER


class TestTheKeystoneFloor:
    def test_the_file_is_neither_readable_nor_writable_by_the_agent(self) -> None:
        from kiro_crew.security import is_sensitive_path

        assert is_sensitive_path("~/.kirocrew/registry_trust.json") is True
        assert is_sensitive_path("~/.kiro/crew/registry_trust.json") is True


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/security/trusted-registries", api_trusted_registries_list)
    app.router.add_post("/api/security/trusted-registries", api_trusted_registry_grant)
    app.router.add_post("/api/security/trusted-registries/revoke", api_trusted_registry_revoke)
    return as_owner(app)


class TestTheEndpoints:
    @pytest.mark.asyncio
    async def test_the_snapshot_lists_operator_rows_with_their_grant(self, home, no_pinned) -> None:
        _write_config(
            home,
            [
                {"name": "mine", "repo": _REPO, "branch": "main"},
                {"name": "other", "repo": _OTHER, "branch": "dev"},
            ],
        )
        _write_grant(_REPO_PUBLIC)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/security/trusted-registries")
            assert resp.status == 200
            rows = (await resp.json())["registries"]
        assert [(r["name"], r["trusted"], r["host"], r["branch"]) for r in rows] == [
            ("mine", True, "git.example.test", "main"),
            ("other", False, "git.example.test", "dev"),
        ]
        assert rows[0]["repo"] == _REPO  # as configured, credentials stripped
        # `granted_at` is not a snapshot field — it has no consumer, and the
        # keystone record body carries no such field either.
        assert "granted_at" not in rows[0]
        assert "granted_at" not in rows[1]

    @pytest.mark.asyncio
    async def test_a_grant_writes_the_keystone_expires_the_cache_and_audits(
        self, home, no_pinned, mock_sel, monkeypatch
    ) -> None:
        _write_config(home, [{"name": "mine", "repo": _REPO, "branch": "main"}])
        expired: list[object] = []
        monkeypatch.setattr(security, "_expire_registry_index_cache", expired.append)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/security/trusted-registries", json={"repo": _REPO_PUBLIC}
            )
            assert resp.status == 200
            rows = (await resp.json())["registries"]
        assert rows[0]["trusted"] is True
        data = json.loads(registry_trust_path().read_text(encoding="utf-8"))
        assert data["version"] == 1
        # Version 1 stores ``owner_trusted`` as a JSON LIST of repo URLs; the grant
        # is the entry itself, since SEL timestamps each grant and no reader
        # consumes a per-repo record body.
        assert data["owner_trusted"] == [_REPO_PUBLIC]
        assert [getattr(r, "name", None) for r in expired] == ["mine"]
        # The runtime reader now sees the grant.
        assert sources._registry_trust_tier("mine") == sources._TRUST_OWNER
        if sys.platform != "win32":
            assert stat.S_IMODE(os.stat(registry_trust_path()).st_mode) == 0o600
        ops = [c.kwargs.get("operation") for c in mock_sel.log_api_access.call_args_list]
        assert "security.trusted_registries.grant" in ops
        outcome = [
            c.kwargs
            for c in mock_sel.log_api_access.call_args_list
            if c.kwargs.get("operation") == "security.trusted_registries.grant"
        ][-1]
        assert outcome["outcome"] == "success"

    @pytest.mark.asyncio
    async def test_a_grant_is_idempotent_and_normalises_the_key(
        self, home, no_pinned, mock_sel
    ) -> None:
        _write_config(home, [{"name": "mine", "repo": _REPO, "branch": "main"}])
        async with TestClient(TestServer(_make_app())) as client:
            assert (
                await client.post("/api/security/trusted-registries", json={"repo": _REPO})
            ).status == 200
            assert (
                await client.post("/api/security/trusted-registries", json={"repo": _REPO_PUBLIC})
            ).status == 200
        data = json.loads(registry_trust_path().read_text(encoding="utf-8"))
        assert len(data["owner_trusted"]) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("body", "code"),
        [
            ({"repo": _OTHER}, "unknown_registry"),
            ({"repo": ""}, "invalid_repo"),
            ({"repo": 5}, "invalid_repo"),
            ({}, "invalid_repo"),
            ({"repo": "https://u:p@git.example.test/team/apps-index"}, "invalid_repo"),
            ({"repo": _PINNED_REPO}, "pinned_registry"),
        ],
    )
    async def test_a_refused_grant_writes_nothing(
        self, home, mock_sel, monkeypatch, body, code
    ) -> None:
        monkeypatch.setattr(sources, "_pinned_registries", _pinned)
        monkeypatch.setattr(security, "_pinned_registries", _pinned)
        _write_config(home, [{"name": "mine", "repo": _REPO, "branch": "main"}])
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/security/trusted-registries", json=body)
            assert resp.status == 400
            assert (await resp.json())["code"] == code
        assert not registry_trust_path().exists()

    @pytest.mark.asyncio
    async def test_a_plaintext_transport_grant_is_refused(self, home, no_pinned, mock_sel) -> None:
        # A plaintext ``http://`` repo must never carry an ``owner`` grant, even if
        # the operator configured it: the tier clones with the machine's git
        # identity and the fetch is unauthenticated. This is the same gate the
        # registries PUT applies. Refused BEFORE the row lookup, and nothing written.
        plain = "http://git.example.test/team/apps-index"
        _write_config(home, [{"name": "mine", "repo": plain, "branch": "main"}])
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/security/trusted-registries", json={"repo": plain})
            assert resp.status == 400
            assert (await resp.json())["code"] == "unsupported_transport"
        assert not registry_trust_path().exists()
        ops = [
            c.kwargs
            for c in mock_sel.log_api_access.call_args_list
            if c.kwargs.get("operation") == "security.trusted_registries.grant"
        ]
        assert ops and ops[-1]["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_a_non_json_body_is_refused(self, home, no_pinned, mock_sel) -> None:
        _write_config(home, [{"name": "mine", "repo": _REPO, "branch": "main"}])
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/security/trusted-registries", data=b"nope")
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_body"

    @pytest.mark.asyncio
    async def test_revoke_drops_the_grant_and_is_idempotent(
        self, home, no_pinned, mock_sel, monkeypatch
    ) -> None:
        _write_config(home, [{"name": "mine", "repo": _REPO, "branch": "main"}])
        _write_grant(_REPO_PUBLIC)
        expired: list[object] = []
        monkeypatch.setattr(security, "_expire_registry_index_cache", expired.append)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/security/trusted-registries/revoke", json={"repo": _REPO}
            )
            assert resp.status == 200
            assert (await resp.json())["registries"][0]["trusted"] is False
            resp = await client.post(
                "/api/security/trusted-registries/revoke", json={"repo": _REPO}
            )
            assert resp.status == 200
        assert json.loads(registry_trust_path().read_text(encoding="utf-8"))["owner_trusted"] == []
        assert len(expired) == 2
        assert sources._registry_trust_tier("mine") == sources._TRUST_INDEX

    @pytest.mark.asyncio
    async def test_revoke_accepts_a_repo_no_longer_in_config(
        self, home, no_pinned, mock_sel
    ) -> None:
        _write_config(home, [])
        _write_grant(_REPO_PUBLIC)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/security/trusted-registries/revoke", json={"repo": _REPO_PUBLIC}
            )
            assert resp.status == 200
        assert json.loads(registry_trust_path().read_text(encoding="utf-8"))["owner_trusted"] == []

    @pytest.mark.asyncio
    async def test_a_corrupt_keystone_refuses_to_mutate(self, home, no_pinned, mock_sel) -> None:
        _write_config(home, [{"name": "mine", "repo": _REPO, "branch": "main"}])
        registry_trust_path().write_text("{not json", encoding="utf-8")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/security/trusted-registries", json={"repo": _REPO})
            assert resp.status == 500
            assert (await resp.json())["code"] == "corrupt"
        assert registry_trust_path().read_text(encoding="utf-8") == "{not json"

    @pytest.mark.asyncio
    async def test_a_malformed_owner_trusted_refuses_to_mutate(
        self, home, no_pinned, mock_sel
    ) -> None:
        # `owner_trusted` is a dict under version 1 (the wrong shape for the
        # version): the writer must refuse rather than silently discard it on the
        # next write.
        _write_config(home, [{"name": "mine", "repo": _REPO, "branch": "main"}])
        corrupt = json.dumps({"version": 1, "owner_trusted": {_REPO_PUBLIC: {}}})
        registry_trust_path().write_text(corrupt, encoding="utf-8")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/security/trusted-registries", json={"repo": _REPO})
            assert resp.status == 500
            assert (await resp.json())["code"] == "corrupt"
        assert registry_trust_path().read_text(encoding="utf-8") == corrupt

    @pytest.mark.asyncio
    async def test_the_read_stays_open_to_an_authenticated_non_owner(
        self, home, no_pinned, mock_sel
    ) -> None:
        # The owner gate covers write verbs, never reads (the r6 owner-gate
        # regression walks every GET in this module and pins that); the snapshot
        # carries nothing GET /api/apps/registries does not already return.
        _write_config(home, [{"name": "mine", "repo": _REPO, "branch": "main"}])
        app = _make_app()
        app["state"] = SimpleNamespace(owner_id="the-owner")
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/api/security/trusted-registries", headers={"X-Test-User": "someone-else"}
            )
            assert resp.status == 200
            assert (await resp.json())["registries"][0]["name"] == "mine"

    @pytest.mark.asyncio
    async def test_a_non_owner_cannot_grant(self, home, no_pinned, mock_sel) -> None:
        _write_config(home, [{"name": "mine", "repo": _REPO, "branch": "main"}])
        app = _make_app()
        app["state"] = SimpleNamespace(owner_id="the-owner")
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/security/trusted-registries",
                json={"repo": _REPO},
                headers={"X-Test-User": "someone-else"},
            )
            assert resp.status == 403
        assert not registry_trust_path().exists()

    def test_the_snapshot_never_lists_pinned_rows(self, home, monkeypatch) -> None:
        monkeypatch.setattr(sources, "_pinned_registries", _pinned)
        _write_config(home, [{"name": "mine", "repo": _REPO, "branch": "main"}])
        assert [r["name"] for r in build_trusted_registries_snapshot()["registries"]] == ["mine"]


def _make_registries_app(monkeypatch: pytest.MonkeyPatch) -> web.Application:
    """The apps router's ``/api/apps/registries`` pair, as the dashboard owner.

    The dashboard reads the row list from the GET and sends it back whole on
    every add/remove, so the pair is exercised together: what the GET reports
    is exactly what the PUT receives.
    """
    from kiro_crew.apps import routes as routes_mod

    monkeypatch.setattr(routes_mod, "sel", lambda: MagicMock())
    monkeypatch.setattr(routes_mod, "_pinned_registries", lambda: [])
    app = web.Application()
    app.router.add_get("/api/apps/registries", routes_mod.handle_registries)
    app.router.add_put("/api/apps/registries", routes_mod.handle_registries)
    return as_owner(app)


class TestThePutNeverTakesOwnerFromConfiguration:
    """FP 0a35ae9292a8 (subtraction). The dashboard sends back only each row's
    name, repo and branch, so the PUT refuses ``trust: owner`` outright, granted
    or not: the tier lives in the keystone, never in ``config.json``."""

    @pytest.mark.asyncio
    async def test_owner_is_refused_even_on_a_granted_row(
        self, home, no_pinned, monkeypatch
    ) -> None:
        _write_config(home, [{"name": "mine", "repo": _REPO, "branch": "main"}])
        _write_grant(_REPO_PUBLIC)
        async with TestClient(TestServer(_make_registries_app(monkeypatch))) as client:
            listed = await client.get("/api/apps/registries")
            # The GET reports the tier in force.
            assert [r["trust"] for r in (await listed.json())["registries"]] == ["owner"]
            resp = await client.put(
                "/api/apps/registries",
                json={
                    "registries": [
                        {"name": "mine", "repo": _REPO, "branch": "main", "trust": "owner"}
                    ]
                },
            )
            assert resp.status == 400
            assert "Settings > Security" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_a_coordinates_only_round_trip_keeps_the_grant(
        self, home, no_pinned, monkeypatch
    ) -> None:
        # What the dashboard sends: coordinates only. Stored as index, grant intact.
        _write_config(home, [{"name": "mine", "repo": _REPO, "branch": "main"}])
        _write_grant(_REPO_PUBLIC)
        async with TestClient(TestServer(_make_registries_app(monkeypatch))) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={
                    "registries": [
                        {"name": "mine", "repo": _REPO, "branch": "main"},
                        {"name": "second", "repo": _OTHER, "branch": "main"},
                    ]
                },
            )
            assert resp.status == 200, await resp.text()
        stored = json.loads((home / "config.json").read_text(encoding="utf-8"))["registries"]
        assert [(r["repo"], r["trust"]) for r in stored] == [
            (_REPO, "index"),
            (_OTHER, "index"),
        ]
        assert sources._registry_trust_tier("mine") == sources._TRUST_OWNER


class TestTheGrantCannotDetachFromTheConfirmedRepo:
    """Item 1 (GPT BLOCKING). Two config rows that share ONE casefolded identity
    key are agent-writable and can coexist. `_registry_trust_tier` resolves the key
    the casefolded way while `_owner_tier_confirmed` resolves the exact name, so
    left unaddressed they could land on different rows: the repo whose grant passes
    the tier check would differ from the repo whose fresh index confirms it, and the
    UNGRANTED repo could receive owner tier + credentials. `_effective_registries`
    now drops BOTH colliding config rows, so the divergence cannot exist."""

    def test_colliding_config_rows_each_read_their_own_grant(self, home, no_pinned) -> None:
        _write_config(
            home,
            [
                {"name": "CollideReg", "repo": _REPO, "branch": "main"},
                {"name": "collidereg", "repo": _OTHER, "branch": "main"},
            ],
        )
        # Grant ONLY the first row's repository.
        _write_grant(_REPO_PUBLIC)
        # Both rows stay served, exactly as on a build without grants.
        rows = sources._effective_registries()
        assert {r.name for r in rows} == {"CollideReg", "collidereg"}
        # The tier is read off each row object, so the ungranted repository never
        # borrows the sibling's grant: the credential paths (`_owner_tier_confirmed`,
        # the store-art prewarm) read the tier off the row whose index they fetch.
        tiers = {r.repo: sources._registry_trust_tier_of(r) for r in rows}
        assert tiers == {_REPO: sources._TRUST_OWNER, _OTHER: sources._TRUST_INDEX}

    def test_a_lone_config_row_still_resolves_its_grant(self, home, no_pinned) -> None:
        # Negative-control anchor: with no collision, the granted row resolves owner
        # exactly as before, so the drop above is scoped to the collision case and
        # not a blanket refusal.
        _write_config(home, [{"name": "solo", "repo": _REPO, "branch": "main"}])
        _write_grant(_REPO_PUBLIC)
        assert [r.name for r in sources._effective_registries()] == ["solo"]
        assert sources._registry_trust_tier("solo") == sources._TRUST_OWNER


class TestAPrecreatedEmptyKeystoneTakesTheFirstGrant:
    """Item 2 (GPT). The sandbox pre-creates `registry_trust.json` as `{}`. The
    strict read now treats an empty document as the absent versioned-empty store,
    so the first grant lands (200) instead of 500ing on the version check; a
    NON-empty document with a wrong version is still refused."""

    @pytest.mark.asyncio
    async def test_a_grant_over_a_precreated_empty_document_succeeds(
        self, home, no_pinned, mock_sel
    ) -> None:
        _write_config(home, [{"name": "mine", "repo": _REPO, "branch": "main"}])
        registry_trust_path().write_text("{}", encoding="utf-8")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/security/trusted-registries", json={"repo": _REPO_PUBLIC}
            )
            assert resp.status == 200, await resp.text()
            assert (await resp.json())["registries"][0]["trusted"] is True
        data = json.loads(registry_trust_path().read_text(encoding="utf-8"))
        assert data["version"] == 1
        assert set(data["owner_trusted"]) == {_REPO_PUBLIC}

    @pytest.mark.asyncio
    async def test_a_nonempty_wrong_version_document_is_still_refused(
        self, home, no_pinned, mock_sel
    ) -> None:
        # The empty-document allowance must not weaken the version check: a real,
        # non-empty store with the wrong version is still corrupt.
        _write_config(home, [{"name": "mine", "repo": _REPO, "branch": "main"}])
        registry_trust_path().write_text(
            json.dumps({"version": 999, "owner_trusted": {}}), encoding="utf-8"
        )
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/security/trusted-registries", json={"repo": _REPO_PUBLIC}
            )
            assert resp.status == 500
            assert (await resp.json())["code"] == "corrupt"


class TestRemovingARegistryKeepsItsGrantVisible:
    """FP 3252133fc780 (subtraction). The registries PUT does not sweep grants:
    removing a row leaves its grant in the keystone, and the snapshot lists it as a
    ``not_configured`` row with a Revoke, so it is never alive AND invisible."""

    @pytest.mark.asyncio
    async def test_dropping_a_row_leaves_its_grant_listed_as_not_configured(
        self, home, no_pinned, monkeypatch
    ) -> None:
        _write_config(home, [{"name": "mine", "repo": _REPO, "branch": "main"}])
        _write_grant(_REPO_PUBLIC)
        before = registry_trust_path().read_text(encoding="utf-8")
        from kiro_crew.apps import routes as routes_mod

        monkeypatch.setattr(routes_mod, "sel", lambda: MagicMock())
        monkeypatch.setattr(routes_mod, "_pinned_registries", lambda: [])
        app = web.Application()
        app.router.add_put("/api/apps/registries", routes_mod.handle_registries)
        as_owner(app)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={"registries": [{"name": "other", "repo": _OTHER, "branch": "main"}]},
            )
            assert resp.status == 200, await resp.text()
        # The PUT did not touch the keystone.
        assert registry_trust_path().read_text(encoding="utf-8") == before
        # The dormant grant is surfaced, revocable and untrusted.
        orphan = [
            r
            for r in build_trusted_registries_snapshot()["registries"]
            if r["repo"] == _REPO_PUBLIC
        ]
        assert orphan and orphan[0]["not_served_reason"] == "not_configured"
        assert orphan[0]["granted"] is True and orphan[0]["trusted"] is False


class TestTheGrantIsAValueNotARecord:
    """The version-1 keystone stores ``owner_trusted`` as a LIST of repo URLs, with
    no per-repo record body: the grant is the entry itself, and SEL timestamps each
    grant. No reader consumes a record body, so a version-1 write emits none."""

    @pytest.mark.asyncio
    async def test_the_writer_stores_a_bare_repo_and_the_reader_honours_it(
        self, home, no_pinned, mock_sel
    ) -> None:
        _write_config(home, [{"name": "mine", "repo": _REPO, "branch": "main"}])
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/security/trusted-registries", json={"repo": _REPO_PUBLIC}
            )
            assert resp.status == 200, await resp.text()
        stored = json.loads(registry_trust_path().read_text(encoding="utf-8"))
        # Version 1: a JSON list of repo URLs, no per-repo body at all.
        assert stored["version"] == 1
        assert stored["owner_trusted"] == [_REPO_PUBLIC]
        assert sources._granted_owner_repos() == frozenset({_REPO_PUBLIC})
        assert sources._registry_trust_tier("mine") == sources._TRUST_OWNER
        rows = build_trusted_registries_snapshot()["registries"]
        assert rows[0]["trusted"] is True
        assert "granted_at" not in rows[0]


# --- Round-4 findings -------------------------------------------------------

_REPO_A = "https://git.example.test/team/a.git"
_REPO_A_PUBLIC = "https://git.example.test/team/a"
_REPO_B = "https://git.example.test/team/b.git"


def _reg(name: str, repo: str, branch: str = "main"):
    return SimpleNamespace(name=name, repo=repo, branch=branch, trust="index", label="", review="")


class TestOwnerTierConfirmResolvesFromOneSnapshot:
    """r4 F1 (GPT BLOCKING, security). `_owner_tier_confirmed` resolved the trust
    tier from one `_effective_registries`/config load and the fresh-index
    coordinates from a second, independent load. config.json and the index cache
    are agent-writable, so a row could be swapped between the two reads: the tier
    passing on row A while the fresh fetch ran against row B's coordinates, B's
    index confirming, B getting owner credentials it was never granted. The fix
    resolves BOTH from ONE snapshot: it loads `_effective_registries` once, selects
    the row by exact credential-free name, reads the tier off THAT row object with
    `_registry_trust_tier_of`, and fetches with THAT row's repo/branch."""

    def _entry(self):
        return {
            "_registry": "reg",
            "gitUrl": _REPO_A_PUBLIC,
            "repo": _REPO_A_PUBLIC,
            "branch": "main",
            "subdirectory": "apps/x",
            "name": "x",
        }

    @pytest.mark.asyncio
    async def test_confirmation_fetches_the_tier_resolved_rows_repo_only(self, monkeypatch) -> None:
        row_a = _reg("reg", _REPO_A_PUBLIC)
        row_b = _reg("reg", _REPO_B)  # same name, different repo, ungranted
        # A snapshot source that would hand back a DIFFERENT row on a second load,
        # so a second, independent resolution could swing onto B.
        calls = {"n": 0}

        def _effective():
            calls["n"] += 1
            return [row_a] if calls["n"] == 1 else [row_b]

        monkeypatch.setattr(indexes, "_effective_registries", _effective)
        # Only row A's repository is owner-trusted; B is index.
        monkeypatch.setattr(
            indexes,
            "_registry_trust_tier_of",
            lambda reg, rows=None: (
                sources._TRUST_OWNER if reg.repo == _REPO_A_PUBLIC else sources._TRUST_INDEX
            ),
        )
        fetched: list[str] = []

        async def _fake_fetch(repo, branch):
            fetched.append(repo)
            # The fresh index lists the entry's coordinates, so a fetch of A's
            # repo confirms; a fetch of B's would confirm nothing meaningful here.
            return [
                {
                    "name": "x",
                    "gitUrl": repo,
                    "repo": repo,
                    "branch": branch,
                    "subdirectory": "apps/x",
                }
            ]

        monkeypatch.setattr(indexes, "_fetch_external_registry_index", _fake_fetch)

        confirmed = await indexes._owner_tier_confirmed(self._entry())

        assert confirmed is True
        # The snapshot was consumed exactly ONCE — the closed window. A second,
        # independent load is what let the row swap; there is none.
        assert calls["n"] == 1
        # And the fetch ran against the tier-resolved row A's repository, never B's.
        assert fetched == [_REPO_A_PUBLIC]
        assert _REPO_B not in fetched

    def test_negative_control_the_swap_fixture_is_real(self, monkeypatch) -> None:
        # Anchors the assertion above: the snapshot source genuinely returns a
        # DIFFERENT repo on a second load, so "consumed once" is what prevents the
        # swing, not a fixture that happens to return the same row twice. If the
        # code regressed to two loads, the second would yield B's ungranted repo.
        row_a = _reg("reg", _REPO_A)
        row_b = _reg("reg", _REPO_B)
        seq = iter([[row_a], [row_b]])
        first = next(seq)[0].repo
        second = next(seq)[0].repo
        assert (first, second) == (_REPO_A, _REPO_B)


class TestGrantValidatesUnderTheLock:
    """r4 F2 (GPT BLOCKING, security). Grant validation ran BEFORE the shared
    config lock the writer takes, and the registries PUT wrote config and only
    afterwards revoked grants — so a grant could validate against a config the PUT
    was about to remove and write an orphan grant into the gap. The fix holds the
    shared config lock across validation AND the keystone write in the grant
    handler (re-reading config under the lock, refusing `unknown_registry` if the
    row is gone)."""

    @pytest.mark.asyncio
    async def test_the_grant_validates_config_while_the_lock_is_held(
        self, home, no_pinned, mock_sel, monkeypatch
    ) -> None:
        # The distinguishing property: the config-row validation runs UNDER the
        # shared lock. A pre-fix handler validated before acquiring it, so this
        # probe would see the lock free. `LoopBoundLock.locked()` answers True from
        # the executor thread when any live loop holds the lock.
        from kiro_crew.dashboard.handlers.agents import _get_config_lock

        _write_config(home, [{"name": "mine", "repo": _REPO, "branch": "main"}])
        monkeypatch.setattr(security, "_expire_registry_index_cache", lambda row: None)
        held_at_validation: list[bool] = []
        real_match = security._matching_operator_registry

        def _probe(repo: str):
            held_at_validation.append(_get_config_lock().locked())
            return real_match(repo)

        monkeypatch.setattr(security, "_matching_operator_registry", _probe)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/security/trusted-registries", json={"repo": _REPO_PUBLIC}
            )
            assert resp.status == 200, await resp.text()
        # The write-time validation (the FIRST call) ran under the lock. A later
        # call for the post-grant cache-expire row lookup is outside the lock by
        # design, so only the first is asserted.
        assert held_at_validation[0] is True

    @pytest.mark.asyncio
    async def test_a_row_removed_while_the_grant_waits_on_the_lock_is_refused(
        self, home, no_pinned, mock_sel
    ) -> None:
        # End-to-end consequence: because validation is under the lock, a row
        # removed while the grant is queued behind the lock is seen as gone and the
        # grant is refused with no orphan written.
        from kiro_crew.dashboard.handlers.agents import _get_config_lock

        _write_config(home, [{"name": "mine", "repo": _REPO, "branch": "main"}])
        assert security._matching_operator_registry(_REPO_PUBLIC) is not None
        async with TestClient(TestServer(_make_app())) as client:
            lock = _get_config_lock()
            await lock.acquire()  # hold it so the grant blocks at its own acquire
            try:
                task = asyncio.ensure_future(
                    client.post("/api/security/trusted-registries", json={"repo": _REPO_PUBLIC})
                )
                for _ in range(20):
                    await asyncio.sleep(0)
                assert not task.done()  # blocked at the lock, before validation
                _write_config(home, [])  # a concurrent PUT's removal, under the lock
                assert security._matching_operator_registry(_REPO_PUBLIC) is None
            finally:
                lock.release()
            resp = await task
            assert resp.status == 400
            assert (await resp.json())["code"] == "unknown_registry"
        assert not registry_trust_path().exists()

    @pytest.mark.asyncio
    async def test_the_writer_takes_the_shared_config_lock(
        self, home, no_pinned, mock_sel, monkeypatch
    ) -> None:
        # The keystone read-modify-write runs UNDER the shared config lock, so it
        # BLOCKS while the lock is held and lands only once it is released. Both the
        # grant and the revoke handler now carry this same concrete shape (strict
        # read -> mutate the record -> publish through the shared writer) instead of
        # a callback, so driving the grant endpoint exercises exactly that path.
        from kiro_crew.dashboard.handlers.agents import _get_config_lock

        _write_config(home, [{"name": "mine", "repo": _REPO, "branch": "main"}])
        monkeypatch.setattr(security, "_expire_registry_index_cache", lambda row: None)
        async with TestClient(TestServer(_make_app())) as client:
            lock = _get_config_lock()
            await lock.acquire()
            try:
                task = asyncio.ensure_future(
                    client.post("/api/security/trusted-registries", json={"repo": _REPO_PUBLIC})
                )
                for _ in range(20):
                    await asyncio.sleep(0)
                # Blocked at the lock: the keystone write has not happened.
                assert not task.done()
                assert not registry_trust_path().exists()
            finally:
                lock.release()
            resp = await task
            assert resp.status == 200, await resp.text()
        # Released: the grant landed as version 1 with the repo in its list.
        data = json.loads(registry_trust_path().read_text(encoding="utf-8"))
        assert data["version"] == 1
        assert set(data["owner_trusted"]) == {_REPO_PUBLIC}

    @pytest.mark.asyncio
    async def test_negative_control_without_removal_the_same_grant_succeeds(
        self, home, no_pinned, mock_sel, monkeypatch
    ) -> None:
        # Isolates the refusal above to the under-lock removal: the identical
        # sequence WITHOUT removing the row writes the grant, so the refusal is
        # caused by the removal a lock-held validation observed, not by anything
        # else in the path.
        _write_config(home, [{"name": "mine", "repo": _REPO, "branch": "main"}])
        monkeypatch.setattr(security, "_expire_registry_index_cache", lambda row: None)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/security/trusted-registries", json={"repo": _REPO_PUBLIC}
            )
            assert resp.status == 200, await resp.text()
        data = json.loads(registry_trust_path().read_text(encoding="utf-8"))
        assert set(data["owner_trusted"]) == {_REPO_PUBLIC}


class TestTheOpusRoundFourAdvisories:
    """Three edges the runtime and the dashboard must agree on."""

    def test_a_precreated_empty_keystone_reads_silently_as_no_grants(self, home, caplog) -> None:
        # The sandbox pre-creates the keystone as ``{}``; that is the ordinary
        # no-grant state on every Linux host, so the reader must not log a
        # corruption-grade warning for it. A NON-empty wrong shape still warns.
        registry_trust_path().write_text("{}", encoding="utf-8")
        with caplog.at_level("WARNING", logger="kiro_crew.apps.registry_pipeline.sources"):
            assert sources._granted_owner_repos() == frozenset()
        assert not [r for r in caplog.records if "not a usable store" in r.getMessage()]
        registry_trust_path().write_text(json.dumps({"version": 999}), encoding="utf-8")
        with caplog.at_level("WARNING", logger="kiro_crew.apps.registry_pipeline.sources"):
            assert sources._granted_owner_repos() == frozenset()
        assert [r for r in caplog.records if "not a usable store" in r.getMessage()]

    def test_the_badge_follows_the_runtime_predicate_not_the_raw_map(self, home, no_pinned) -> None:
        # A hand-edited grant on a plaintext transport sits in the keystone but the
        # runtime refuses it, so the Security page must not show it as trusted.
        plain = "http://git.example.test/team/apps-index"
        _write_config(
            home,
            [
                {"name": "plain", "repo": plain, "branch": "main"},
                {"name": "mine", "repo": _REPO, "branch": "main"},
            ],
        )
        _write_grant(plain, _REPO_PUBLIC)
        rows = {r["name"]: r["trusted"] for r in build_trusted_registries_snapshot()["registries"]}
        assert rows == {"plain": False, "mine": True}

    @pytest.mark.asyncio
    async def test_the_put_accepts_two_rows_sharing_one_identity_key(
        self, home, no_pinned, monkeypatch
    ) -> None:
        # FP c54571e0db59: a colliding pair is served as on base (it only costs
        # trust), so the PUT stores it exactly as before this feature.
        _write_config(home, [])
        async with TestClient(TestServer(_make_registries_app(monkeypatch))) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={
                    "registries": [
                        {"name": "Acme", "repo": _REPO, "branch": "main"},
                        {"name": "acme", "repo": _OTHER, "branch": "main"},
                    ]
                },
            )
            assert resp.status == 200, await resp.text()
        stored = json.loads((home / "config.json").read_text(encoding="utf-8"))["registries"]
        assert [r["name"] for r in stored] == ["Acme", "acme"]


# --- Round-5 findings -------------------------------------------------------


def _report_a_second_link(path: Path) -> pytest.MonkeyPatch:
    """Make ``os.fstat`` report ``st_nlink == 2`` for *path*'s inode, deterministically.

    The alias guard reads ``st_nlink`` off the descriptor it opened, so faking the
    count on that one inode exercises the exact refusal branch on every platform,
    including filesystems where ``os.link`` is refused. Every other stat is real."""
    target = os.stat(path)
    real_fstat = os.fstat

    def _fstat(fd):
        st = real_fstat(fd)
        if not os.path.samestat(st, target):
            return st
        fields = list(st)
        fields[3] = 2  # st_nlink
        # Carry the platform-only fields (``st_file_attributes`` on Windows, the
        # float times) across, or a reader of them sees ``None``.
        extra = {
            name: getattr(st, name)
            for name in dir(st)
            if name.startswith("st_") and name not in os.stat_result.__match_args__
        }
        return os.stat_result(tuple(fields), extra)

    # A private MonkeyPatch, not the fixture's: the caller undoes ONLY this
    # patch for its negative control, leaving the fixture's KIROCREW_HOME intact.
    mp = pytest.MonkeyPatch()
    mp.setattr(os, "fstat", _fstat)
    return mp


def _give_the_keystone_a_second_name(home: Path) -> tuple[Path | None, Callable[[], None]]:
    """A real hardlink where the platform allows one, else a reported second link.

    Returns the alias path (None when the count was reported rather than created)
    and a callable that removes the second name either way. Never skips: the
    ``st_nlink > 1`` refusal is asserted on every platform."""
    alias = home / "registry_trust_alias.json"
    try:
        os.link(registry_trust_path(), alias)
        return alias, alias.unlink
    except OSError:
        mp = _report_a_second_link(registry_trust_path())
        return None, mp.undo


class TestAnAliasBackedKeystoneConfersNoTrust:
    """r5 F2 (GPT). The sandbox's read-only mount seals the keystone's PATH, not
    its inode, so a keystone that is a symlink (its name is in the writable data
    home) or a regular file with a second hardlink (the alias is an unsealed path)
    survives the seal while a sandboxed process can still rewrite the bytes a grant
    is read from; `sandbox._warn_if_alias_backed` only warns. The strict keystone
    read (reached by the grant and revoke writers) refuses both shapes as corrupt,
    so a linked keystone is not mutated in place and confers no owner trust."""

    def test_a_hardlinked_keystone_is_refused_as_corrupt(self, home) -> None:
        from kiro_crew.apps import registry_trust as rt_mod

        _write_grant(_REPO_PUBLIC)  # a valid keystone at the real path
        _alias, drop_second_name = _give_the_keystone_a_second_name(home)  # nlink == 2
        try:
            with pytest.raises(rt_mod.RegistryTrustCorruptError):
                rt_mod.read_registry_trust_strict()
        finally:
            drop_second_name()
        # Negative control: with the second name gone (nlink back to 1) the SAME
        # file reads cleanly, so the refusal is a property of the extra link, not
        # of the content.
        data = rt_mod.read_registry_trust_strict()
        assert set(data["owner_trusted"]) == {_REPO_PUBLIC}

    def test_a_reported_second_link_is_refused_on_every_platform(self, home) -> None:
        # The same refusal, driven through the reported count alone, so the
        # `st_nlink > 1` branch is exercised deterministically even where the
        # filesystem also allowed a real link above.
        from kiro_crew.apps import registry_trust as rt_mod

        _write_grant(_REPO_PUBLIC)
        reported = _report_a_second_link(registry_trust_path())
        try:
            with pytest.raises(rt_mod.RegistryTrustCorruptError):
                rt_mod.read_registry_trust_strict()
            assert sources._granted_owner_repos() == frozenset()
        finally:
            reported.undo()
        assert set(rt_mod.read_registry_trust_strict()["owner_trusted"]) == {_REPO_PUBLIC}

    @requires_symlinks
    def test_a_symlinked_keystone_is_refused_as_corrupt(self, home) -> None:
        from kiro_crew.apps import registry_trust as rt_mod

        real = home / "registry_trust_real.json"
        real.write_text(
            json.dumps(
                {"version": 1, "owner_trusted": [_REPO_PUBLIC]},
            ),
            encoding="utf-8",
        )
        # Replace the keystone path with a symlink to the real file.
        registry_trust_path().symlink_to(real)
        with pytest.raises(rt_mod.RegistryTrustCorruptError):
            rt_mod.read_registry_trust_strict()
        # Negative control: a REAL regular file at the keystone path reads cleanly.
        registry_trust_path().unlink()
        _write_grant(_REPO_PUBLIC)
        assert set(rt_mod.read_registry_trust_strict()["owner_trusted"]) == {_REPO_PUBLIC}

    def test_the_runtime_consumer_grants_nothing_from_a_hardlinked_keystone(self, home) -> None:
        # The clone-time reader (`_granted_owner_repos`, the one that decides which
        # clone gets credentials) must refuse the alias too, not only the writers:
        # a grant the writers cannot forge but the runtime still honours through a
        # second name would leave the finding open where it matters.
        _write_grant(_REPO_PUBLIC)
        assert _REPO_PUBLIC in sources._granted_owner_repos()  # control: real file grants
        _alias, drop_second_name = _give_the_keystone_a_second_name(home)
        try:
            assert sources._granted_owner_repos() == frozenset()
        finally:
            drop_second_name()
        assert _REPO_PUBLIC in sources._granted_owner_repos()

    @requires_symlinks
    def test_the_runtime_consumer_grants_nothing_from_a_symlinked_keystone(self, home) -> None:
        real = home / "registry_trust_real.json"
        real.write_text(
            json.dumps({"version": 1, "owner_trusted": [_REPO_PUBLIC]}), encoding="utf-8"
        )
        registry_trust_path().symlink_to(real)
        assert sources._granted_owner_repos() == frozenset()
        registry_trust_path().unlink()
        _write_grant(_REPO_PUBLIC)
        assert _REPO_PUBLIC in sources._granted_owner_repos()

    def test_undecodable_bytes_read_as_corrupt_not_as_a_crash(self, home) -> None:
        from kiro_crew.apps import registry_trust as rt_mod

        registry_trust_path().write_bytes(b'{"version": 1, "owner_trusted": ["\xff\xfe"]}')
        with pytest.raises(rt_mod.RegistryTrustCorruptError):
            rt_mod.read_registry_trust_strict()
        # The clone-time reader degrades to "no grants in force" instead of raising
        # into the listing path.
        assert sources._granted_owner_repos() == frozenset()

    def test_a_fifo_at_the_keystone_path_is_refused_without_blocking(
        self, home, monkeypatch
    ) -> None:
        # A FIFO with no writer would park a blocking open forever; the reader
        # opens non-blocking and the S_ISREG check turns it into a corrupt read.
        # Every platform pins the non-blocking open through a spy; a POSIX host
        # additionally plants a real FIFO and proves the read returns.
        from kiro_crew import platform_compat
        from kiro_crew.apps import registry_trust as rt_mod

        seen: list[bool] = []
        real_open = platform_compat.open_file_no_reparse

        def _spy(path, *, nonblocking=False):
            seen.append(nonblocking)
            return real_open(path, nonblocking=nonblocking)

        monkeypatch.setattr(platform_compat, "open_file_no_reparse", _spy)
        _write_grant(_REPO_PUBLIC)
        rt_mod.read_registry_trust_strict()
        assert seen == [True], "the keystone must be opened non-blocking"

        mkfifo = getattr(os, "mkfifo", None)
        if mkfifo is not None:
            path = registry_trust_path()
            path.unlink()
            mkfifo(path)
            try:
                with pytest.raises(rt_mod.RegistryTrustCorruptError):
                    rt_mod.read_registry_trust_strict()
                assert sources._granted_owner_repos() == frozenset()
            finally:
                path.unlink()

    def test_a_regular_keystone_reads_unchanged(self, home) -> None:
        # A plain regular file (nlink == 1, not a link) is unaffected by the alias
        # guard: the ordinary path still reads.
        from kiro_crew.apps import registry_trust as rt_mod

        _write_grant(_REPO_PUBLIC)
        assert set(rt_mod.read_registry_trust_strict()["owner_trusted"]) == {_REPO_PUBLIC}
        assert os.stat(registry_trust_path()).st_nlink == 1

    @pytest.mark.asyncio
    async def test_a_grant_over_a_hardlinked_keystone_is_refused(
        self, home, no_pinned, mock_sel
    ) -> None:
        # End-to-end through the writer: a grant POST over a hardlinked keystone is
        # refused as corrupt rather than silently mutating the aliased inode.
        _write_config(home, [{"name": "mine", "repo": _REPO, "branch": "main"}])
        _write_grant(_OTHER)  # an existing keystone so a second name has a target
        _alias, drop_second_name = _give_the_keystone_a_second_name(home)
        try:
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.post(
                    "/api/security/trusted-registries", json={"repo": _REPO_PUBLIC}
                )
                assert resp.status == 500
                assert (await resp.json())["code"] == "corrupt"
        finally:
            drop_second_name()


# --- Round-7 findings -------------------------------------------------------


class TestAGrantOnAnUnservedRowIsRefused:
    """Opus 4b85d4ef047e (fix). ``_validate_and_grant`` gated only on the raw
    config row existing, so a row the MERGE drops — its name contested by a
    build-pinned registry, or an identity-key collision with another config row —
    could be granted: the keystone records it, the snapshot still reads it
    not-trusted, the button stays Grant, Revoke is never offered — an invisible
    grant that arms the moment the collision is resolved. The grant now resolves
    the row against ``_effective_registries`` (as the snapshot does) and refuses a
    not-served row with ``not_served`` (400), writing nothing."""

    @pytest.mark.asyncio
    async def test_grant_on_a_pinned_name_contest_row_is_refused(
        self, home, mock_sel, monkeypatch
    ) -> None:
        # A config row whose name is contested by a build-pinned registry is served
        # by neither. It is not a pinned REPOSITORY (that is the pinned_registry
        # refusal), so it reaches the served-state gate and is refused not_served.
        pinned = [
            SimpleNamespace(
                name="mine", repo=_PINNED_REPO, branch="main", trust="index", label="", review=""
            )
        ]
        monkeypatch.setattr(sources, "_pinned_registries", lambda: pinned)
        monkeypatch.setattr(security, "_pinned_registries", lambda: pinned)
        _write_config(home, [{"name": "mine", "repo": _REPO, "branch": "dev"}])
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/security/trusted-registries", json={"repo": _REPO_PUBLIC}
            )
            assert resp.status == 400
            body = await resp.json()
            assert body["code"] == "not_served"
            assert body["reason"] == "pinned_name"
        assert not registry_trust_path().exists()

    @pytest.mark.asyncio
    async def test_negative_control_a_grant_on_a_served_row_still_lands(
        self, home, no_pinned, mock_sel
    ) -> None:
        # Isolates the refusal to the not-served condition: a lone, served row is
        # granted exactly as before, so the gate is scoped to dropped rows.
        _write_config(home, [{"name": "mine", "repo": _REPO, "branch": "main"}])
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/security/trusted-registries", json={"repo": _REPO_PUBLIC}
            )
            assert resp.status == 200, await resp.text()
        assert set(
            json.loads(registry_trust_path().read_text(encoding="utf-8"))["owner_trusted"]
        ) == {_REPO_PUBLIC}


class TestACorruptKeystoneIsVisible:
    """Design 0de17a336bf1 + suggestion 8f1d46f326f8 (fix). A corrupt
    ``registry_trust.json`` (bad JSON / wrong version) made the PUT and the
    grant/revoke handlers refuse, and sent the operator to the Security page — but
    that page's snapshot degraded to all-untrusted, so the surface that repairs it
    looked healthy. The snapshot now carries ``corrupt``/``corrupt_detail``. No
    product writer produces a corrupt file, so there is no in-app reset (FP
    da1f705bb604): the operator fixes or deletes the file."""

    def test_snapshot_flags_a_bad_json_keystone_corrupt(self, home, no_pinned) -> None:
        _write_config(home, [{"name": "mine", "repo": _REPO, "branch": "main"}])
        registry_trust_path().write_text("{not json", encoding="utf-8")
        snap = build_trusted_registries_snapshot()
        assert snap["corrupt"] is True
        assert snap["corrupt_detail"]
        # Every row is untrusted while the store is corrupt.
        assert all(r["trusted"] is False for r in snap["registries"])

    def test_snapshot_flags_a_wrong_version_keystone_corrupt(self, home, no_pinned) -> None:
        _write_config(home, [{"name": "mine", "repo": _REPO, "branch": "main"}])
        registry_trust_path().write_text(
            json.dumps({"version": 999, "owner_trusted": [_REPO_PUBLIC]}), encoding="utf-8"
        )
        snap = build_trusted_registries_snapshot()
        assert snap["corrupt"] is True
        assert snap["registries"][0]["trusted"] is False

    def test_negative_control_a_healthy_keystone_has_no_corrupt_flag(self, home, no_pinned) -> None:
        # A valid granted store carries no corrupt flag and badges the row trusted,
        # so the flag above is a property of the corruption, not of the snapshot.
        _write_config(home, [{"name": "mine", "repo": _REPO, "branch": "main"}])
        _write_grant(_REPO_PUBLIC)
        snap = build_trusted_registries_snapshot()
        assert "corrupt" not in snap
        assert snap["registries"][0]["trusted"] is True


class TestTheOnDiskShapeIsAVersionOneList:
    """The on-disk shape is a JSON LIST at version 1: ``owner_trusted`` is a list of
    credential-free repo URLs and the grant is the entry itself, since SEL
    timestamps each grant so no reader consumes a record body. An unknown-version
    or dict-shaped document is corrupt."""

    @pytest.mark.asyncio
    async def test_a_write_emits_version_one_with_a_list(self, home, no_pinned, mock_sel) -> None:
        _write_config(home, [{"name": "mine", "repo": _REPO, "branch": "main"}])
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/security/trusted-registries", json={"repo": _REPO_PUBLIC}
            )
            assert resp.status == 200, await resp.text()
        data = json.loads(registry_trust_path().read_text(encoding="utf-8"))
        assert data["version"] == 1
        assert isinstance(data["owner_trusted"], list)
        assert data["owner_trusted"] == [_REPO_PUBLIC]

    @pytest.mark.asyncio
    async def test_a_grant_over_a_dict_shaped_document_is_refused_as_corrupt(
        self, home, no_pinned, mock_sel
    ) -> None:
        # A dict-shaped document is corrupt, so a grant over it is refused (500) and
        # the store is not rewritten in place.
        _write_config(home, [{"name": "mine", "repo": _REPO, "branch": "main"}])
        original = json.dumps({"version": 1, "owner_trusted": {_REPO_PUBLIC: {}}})
        registry_trust_path().write_text(original, encoding="utf-8")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/security/trusted-registries", json={"repo": _REPO_PUBLIC}
            )
            assert resp.status == 500
            assert (await resp.json())["code"] == "corrupt"
        # Nothing was rewritten: the corrupt document stands until it is fixed.
        assert registry_trust_path().read_text(encoding="utf-8") == original

    def test_the_reader_returns_a_frozenset(self, home, no_pinned) -> None:
        _write_config(home, [{"name": "mine", "repo": _REPO, "branch": "main"}])
        _write_grant(_REPO_PUBLIC)
        assert isinstance(sources._granted_owner_repos(), frozenset)


class TestTheSnapshotReportsGrantedIndependentlyOfServed:
    """A row can hold a stored grant AND be dropped by the merge (an operator adds
    a same-name registry): its grant is dormant and re-arms when the collision
    resolves. The snapshot emits ``granted`` off the keystone record, independent of
    ``served``, so the panel can still offer Revoke on a dropped-but-granted row;
    ``trusted`` stays served-and-owner-tier."""

    def test_an_unserved_row_with_a_stored_grant_is_granted_but_not_trusted(
        self, home, monkeypatch
    ) -> None:
        # A config row whose name is contested by a build-pinned registry is served
        # by neither claimant, yet a stored grant still names its repository. The
        # snapshot must report granted:true (so Revoke is offered) and trusted:false
        # (the merge drops it, so nothing lists it).
        pinned = [
            SimpleNamespace(
                name="mine", repo=_PINNED_REPO, branch="main", trust="index", label="", review=""
            )
        ]
        monkeypatch.setattr(sources, "_pinned_registries", lambda: pinned)
        monkeypatch.setattr(security, "_pinned_registries", lambda: pinned)
        _write_config(home, [{"name": "mine", "repo": _REPO, "branch": "dev"}])
        _write_grant(_REPO_PUBLIC)
        rows = build_trusted_registries_snapshot()["registries"]
        assert len(rows) == 1
        assert rows[0]["served"] is False
        assert rows[0]["granted"] is True
        assert rows[0]["trusted"] is False

    def test_a_served_ungranted_row_is_neither_granted_nor_trusted(self, home, no_pinned) -> None:
        # Negative control on granted: a served row with no grant reads
        # granted:false, so the panel offers Grant rather than Revoke.
        _write_config(home, [{"name": "mine", "repo": _REPO, "branch": "main"}])
        rows = build_trusted_registries_snapshot()["registries"]
        assert rows[0]["served"] is True
        assert rows[0]["granted"] is False
        assert rows[0]["trusted"] is False

    def test_a_served_granted_row_is_both_granted_and_trusted(self, home, no_pinned) -> None:
        # The ordinary trusted case still reports granted:true alongside
        # trusted:true, so the field agrees with the badge on a served row.
        _write_config(home, [{"name": "mine", "repo": _REPO, "branch": "main"}])
        _write_grant(_REPO_PUBLIC)
        rows = build_trusted_registries_snapshot()["registries"]
        assert rows[0]["granted"] is True
        assert rows[0]["trusted"] is True

    def test_a_corrupt_keystone_reports_every_row_ungranted(self, home, no_pinned) -> None:
        # A corrupt store has no readable grants, so granted follows trusted to
        # false for every row until the file is fixed.
        _write_config(home, [{"name": "mine", "repo": _REPO, "branch": "main"}])
        registry_trust_path().write_text("{not json", encoding="utf-8")
        snap = build_trusted_registries_snapshot()
        assert snap["corrupt"] is True
        assert snap["registries"][0]["granted"] is False
        assert snap["registries"][0]["trusted"] is False


# --- Round-10 findings ------------------------------------------------------


class TestOrphanGrantsAreListedAndRevocable:
    """Design a453f1137c98 (fix). Nothing drops a grant when its config row goes
    away, so a removed row keeps its keystone grant while the snapshot — which
    iterates config rows — lists nothing for it, and it re-arms the moment the row
    returns. ``build_trusted_registries_snapshot`` now appends one synthetic row
    per stored grant whose repository matches NO config row: ``served: false``,
    ``not_served_reason: not_configured``, ``granted: true``, ``trusted: false``,
    so the panel offers Revoke on it (it renders Revoke for any granted row)."""

    def test_an_orphan_grant_appears_with_the_not_configured_reason(self, home, no_pinned) -> None:
        # No config rows at all, but a stored grant: it must surface as its own row.
        _write_config(home, [])
        _write_grant(_REPO_PUBLIC)
        rows = build_trusted_registries_snapshot()["registries"]
        assert len(rows) == 1
        orphan = rows[0]
        assert orphan["repo"] == _REPO_PUBLIC
        assert orphan["served"] is False
        assert orphan["not_served_reason"] == "not_configured"
        assert orphan["granted"] is True
        assert orphan["trusted"] is False
        assert orphan["branch"] == ""
        assert orphan["host"] == "git.example.test"

    def test_an_orphan_grant_sits_beside_a_configured_row(self, home, no_pinned) -> None:
        # A configured row AND an orphan grant for a different repo: the config row
        # renders normally and the orphan is appended after it.
        _write_config(home, [{"name": "mine", "repo": _REPO, "branch": "main"}])
        _write_grant(_REPO_PUBLIC, _OTHER)  # _OTHER names no config row
        rows = build_trusted_registries_snapshot()["registries"]
        by_repo = {r["repo"]: r for r in rows}
        # The configured row is granted+trusted and served.
        assert by_repo[_REPO]["served"] is True
        assert by_repo[_REPO]["granted"] is True
        # The orphan is a not_configured, granted, not-served, untrusted row. Its
        # `repo` is the stored grant value verbatim (credential-free, `.git` kept).
        assert _OTHER in by_repo
        assert by_repo[_OTHER]["served"] is False
        assert by_repo[_OTHER]["not_served_reason"] == "not_configured"
        assert by_repo[_OTHER]["granted"] is True
        assert by_repo[_OTHER]["trusted"] is False

    @pytest.mark.asyncio
    async def test_revoking_an_orphan_grant_removes_it_from_the_snapshot(
        self, home, no_pinned, mock_sel
    ) -> None:
        # End-to-end: the orphan row's Revoke drops the grant, so the next snapshot
        # omits it entirely (revoke accepts a repo absent from config).
        _write_config(home, [])
        _write_grant(_REPO_PUBLIC)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/security/trusted-registries/revoke", json={"repo": _REPO_PUBLIC}
            )
            assert resp.status == 200, await resp.text()
            rows = (await resp.json())["registries"]
        assert rows == []
        assert json.loads(registry_trust_path().read_text(encoding="utf-8"))["owner_trusted"] == []

    def test_negative_control_a_configured_grant_is_not_duplicated(self, home, no_pinned) -> None:
        # A grant whose repository DOES name a config row must render only once, as
        # that config row — never also as an orphan. The orphan pass skips any grant
        # a listed row already covers.
        _write_config(home, [{"name": "mine", "repo": _REPO, "branch": "main"}])
        _write_grant(_REPO_PUBLIC)
        rows = build_trusted_registries_snapshot()["registries"]
        assert len(rows) == 1
        assert rows[0]["name"] == "mine"
        assert rows[0]["served"] is True
        assert rows[0]["granted"] is True
        # No not_configured row was appended for the same repository.
        assert not any(r.get("not_served_reason") == "not_configured" for r in rows)

    def test_a_corrupt_keystone_lists_no_orphan_rows(self, home, no_pinned) -> None:
        # A corrupt store has no readable grants, so ``granted_repos`` is empty and
        # no orphan rows appear until it is fixed.
        _write_config(home, [])
        registry_trust_path().write_text("{not json", encoding="utf-8")
        snap = build_trusted_registries_snapshot()
        assert snap["corrupt"] is True
        assert snap["registries"] == []


class TestTheServedProjectionIsSharedByTheSnapshotAndTheGrantGate:
    """FP 0a7b6a65cdba (fix). ``_registry_served_state`` (the grant gate) and the
    snapshot's per-row loop each projected served state against
    ``_effective_registries``; the duplicate is collapsed into one
    ``_served_state_for_key`` both call, so the gate and the snapshot can never
    disagree about whether a row is served."""

    def test_a_colliding_row_badges_only_its_own_grant(self, home, no_pinned) -> None:
        # Two config rows sharing one identity key: both served, and the snapshot
        # reads each row's tier off its OWN effective copy, so only the granted
        # repository badges trusted.
        _write_config(
            home,
            [
                {"name": "Acme", "repo": _REPO, "branch": "main"},
                {"name": "acme", "repo": _OTHER, "branch": "main"},
            ],
        )
        _write_grant(_REPO_PUBLIC)
        snap = {r["repo"]: r for r in build_trusted_registries_snapshot()["registries"]}
        assert snap[_REPO]["served"] is True and snap[_REPO]["trusted"] is True
        assert snap[_OTHER]["served"] is True and snap[_OTHER]["trusted"] is False

    def test_the_gate_and_the_snapshot_agree_on_a_pinned_name(self, home, monkeypatch) -> None:
        pinned = [
            SimpleNamespace(
                name="mine", repo=_PINNED_REPO, branch="main", trust="index", label="", review=""
            )
        ]
        monkeypatch.setattr(sources, "_pinned_registries", lambda: pinned)
        monkeypatch.setattr(security, "_pinned_registries", lambda: pinned)
        _write_config(home, [{"name": "mine", "repo": _REPO, "branch": "dev"}])
        rows = build_trusted_registries_snapshot()["registries"]
        assert rows[0]["served"] is False
        assert rows[0]["not_served_reason"] == "pinned_name"
        served, reason = security._registry_served_state(_REPO_PUBLIC)
        assert served is False and reason == "pinned_name"

    def test_a_served_row_agrees_too(self, home, no_pinned) -> None:
        # Negative control: a lone served row reports served on both sides.
        _write_config(home, [{"name": "mine", "repo": _REPO, "branch": "main"}])
        rows = build_trusted_registries_snapshot()["registries"]
        assert rows[0]["served"] is True
        served, reason = security._registry_served_state(_REPO_PUBLIC)
        assert served is True and reason is None


class TestThePrewarmReadsTheTierOffTheFetchedRow:
    """Opus cd1df3ed1f7a (fix). The store-art prewarm judged the tier by
    re-resolving the registry NAME, so a row swapped in agent-writable
    ``config.json`` between the index fetch and the prewarm (``N -> B`` fetched,
    then ``N -> A`` granted) let B's index be cloned with the owner identity. The
    prewarm now reads the tier off the very row object whose index was fetched."""

    def test_a_swapped_row_does_not_lift_the_fetched_index(
        self, home, no_pinned, monkeypatch
    ) -> None:
        from kiro_crew.apps.registry_pipeline import store_art

        # The agent's row as it was when B's index was fetched.
        fetched = SimpleNamespace(name="N", repo=_OTHER, branch="main", trust="index")
        # The config now says N -> the granted repository.
        _write_config(home, [{"name": "N", "repo": _REPO, "branch": "main"}])
        _write_grant(_REPO_PUBLIC)
        # Negative control: re-resolving by name WOULD answer owner.
        assert sources._registry_trust_tier("N") == sources._TRUST_OWNER
        # The provenance read runs only once the tier gate has passed, so reaching
        # it means the fetched index was about to be warmed with owner credentials.
        passed_gate: list[str] = []
        monkeypatch.setattr(
            store_art,
            "_prewarm_foreign_claims",
            lambda name: passed_gate.append(name) or (True, []),
        )
        entry = {"name": "x-app", "repo": "https://git.example.test/team/x.git"}
        assert asyncio.run(store_art._prewarm_owner_tier_store_assets(fetched, [entry])) == 0
        assert passed_gate == []


class TestTheGrantCoversEveryBranch:
    """Design 97d87f4c0310. The grant is keyed on the repository alone: the
    repository's authors control every ref, so a branch change on the config row
    keeps the grant, and the grant confirm says so."""

    def test_changing_the_rows_branch_keeps_the_grant(self, home, no_pinned) -> None:
        _write_config(home, [{"name": "mine", "repo": _REPO, "branch": "main"}])
        _write_grant(_REPO_PUBLIC)
        assert sources._registry_trust_tier("mine") == sources._TRUST_OWNER
        _write_config(home, [{"name": "mine", "repo": _REPO, "branch": "dev"}])
        assert sources._registry_trust_tier("mine") == sources._TRUST_OWNER

    def test_the_confirm_copy_says_every_branch(self) -> None:
        manual = Path(__file__).resolve().parents[1] / "website/src/i18n/locales/en.manual.json"
        body = json.loads(manual.read_text(encoding="utf-8"))["pages"]["settings"]["securityPanel"][
            "trustedRegistries"
        ]["grant_confirm_body"]
        assert "every branch" in body


class TestTheRegistriesGetReadsEachRowsOwnTier:
    """Opus 63305eadfce3. The GET reported each row's tier by NAME, so two rows
    sharing a name both showed the first one's grant. It now reads the tier off
    each row object."""

    @pytest.mark.asyncio
    async def test_two_same_name_rows_each_report_their_own_grant(
        self, home, no_pinned, monkeypatch
    ) -> None:
        _write_config(
            home,
            [
                {"name": "acme", "repo": _OTHER, "branch": "main"},
                {"name": "acme", "repo": _REPO, "branch": "main"},
            ],
        )
        _write_grant(_REPO_PUBLIC)
        async with TestClient(TestServer(_make_registries_app(monkeypatch))) as client:
            rows = (await (await client.get("/api/apps/registries")).json())["registries"]
        assert [(r["repo"], r["trust"]) for r in rows] == [
            (sources._strip_git_target_userinfo(_OTHER), "index"),
            (_REPO, "owner"),
        ]


class TestAnOrphanGrantNeverEchoesUserinfo:
    """GPT ba34c7cc08d7 (fix). A hand-edited orphan grant carrying userinfo (a
    token pasted with the URL) was listed verbatim in ``name`` and ``repo``. The
    snapshot now strips userinfo from it, and Revoke still clears it."""

    @pytest.mark.asyncio
    async def test_the_token_is_not_listed_and_revoke_clears_it(
        self, home, no_pinned, mock_sel
    ) -> None:
        _write_config(home, [])
        _write_grant("https://user:s3cr3t-token@git.example.test/gone/index")
        rows = build_trusted_registries_snapshot()["registries"]
        assert len(rows) == 1
        assert "s3cr3t-token" not in json.dumps(rows)
        assert rows[0]["repo"] == "https://git.example.test/gone/index"
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/security/trusted-registries/revoke", json={"repo": rows[0]["repo"]}
            )
            assert resp.status == 200, await resp.text()
        assert build_trusted_registries_snapshot()["registries"] == []

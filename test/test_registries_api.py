"""Tests for /api/apps/registries — federated registry management endpoint."""
from __future__ import annotations

import json
import re
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps.routes import _blob_cache_key, register_app_routes


class TestBlobCacheKey:
    def test_distinct_repos_get_distinct_keys(self):
        # Slugification alone collides (org/app vs org_app -> same slug); the
        # hash suffix must keep the two blob caches separate.
        a = _blob_cache_key("https://host/org/app")
        b = _blob_cache_key("https://host/org_app")
        assert a != b

    def test_key_is_stable_and_filesystem_safe(self):
        import re as _re
        key = _blob_cache_key("https://github.com/acme/apps.git")
        assert key == _blob_cache_key("https://github.com/acme/apps.git")
        assert "/" not in key and ":" not in key
        assert _re.match(r"^[A-Za-z0-9_.-]+$", key)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_env(tmp_path, monkeypatch):
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    # Create empty config
    cfg = home / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "kiro_crew.apps.routes.config_path",
        lambda: str(cfg),
    )
    # Mock SEL
    mock_sel = MagicMock()
    monkeypatch.setattr("kiro_crew.apps.routes.sel", lambda: mock_sel)
    # Mock bridges/backend to avoid side effects
    import kiro_crew.apps.bridges as bridges_mod
    kiro_agents = tmp_path / "kiro-agents"
    kiro_agents.mkdir()
    monkeypatch.setattr(bridges_mod, "KIRO_AGENTS_DIR", kiro_agents)
    import kiro_crew.apps.backend as bmod
    bmod._processes.clear()
    bmod._allocated_ports.clear()
    return home, cfg


def _make_app():
    app = web.Application()
    register_app_routes(app)
    return app


# ---------------------------------------------------------------------------
# GET /api/apps/registries
# ---------------------------------------------------------------------------


class TestGetRegistries:
    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_registries(self, tmp_path, monkeypatch):
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/apps/registries")
            assert resp.status == 200
            data = await resp.json()
            assert data["registries"] == []

    @pytest.mark.asyncio
    async def test_returns_configured_registries(self, tmp_path, monkeypatch):
        home, cfg = _setup_env(tmp_path, monkeypatch)
        cfg.write_text(json.dumps({
            "registries": [
                {"name": "myorg", "repo": "MyOrgApps", "branch": "mainline"},
            ]
        }), encoding="utf-8")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/apps/registries")
            assert resp.status == 200
            data = await resp.json()
            assert len(data["registries"]) == 1
            assert data["registries"][0]["repo"] == "MyOrgApps"


# ---------------------------------------------------------------------------
# PUT /api/apps/registries — happy path
# ---------------------------------------------------------------------------


class TestPutRegistries:
    @pytest.mark.asyncio
    async def test_add_registry(self, tmp_path, monkeypatch):
        home, cfg = _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={"registries": [
                    {"name": "identity", "repo": "IdentityApps", "branch": "mainline"},
                ]},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert len(data["registries"]) == 1
            assert data["registries"][0]["repo"] == "IdentityApps"

            # Verify persisted to config
            saved = json.loads(cfg.read_text(encoding="utf-8"))
            assert len(saved["registries"]) == 1

    @pytest.mark.asyncio
    async def test_new_url_host_emits_trust_grant_event(self, tmp_path, monkeypatch):
        # Admitting a URL registry whose host was not previously configured is a
        # genuine trust grant (the host joins the SSH-clone/loosened-sandbox set
        # and its apps become installable with gateway privileges). It MUST emit
        # a distinct, per-host audit event — not just the generic
        # registries.update — so incident response can reconstruct when/how a
        # host entered the trust set.
        _setup_env(tmp_path, monkeypatch)
        from kiro_crew.apps import routes as routes_mod
        mock_sel = routes_mod.sel()
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={"registries": [
                    {"repo": "https://github.com/acme/apps"},
                ]},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["newlyTrustedHosts"] == ["github.com"]

        grants = [
            c for c in mock_sel.log_api_access.call_args_list
            if c.kwargs.get("operation") == "registries.host_trust_granted"
        ]
        assert len(grants) == 1
        assert "host=github.com" in grants[0].kwargs["resources"]

    @pytest.mark.asyncio
    async def test_resaving_same_host_emits_no_trust_grant(self, tmp_path, monkeypatch):
        # Re-saving a registries list whose hosts were already configured must
        # NOT re-emit a trust-grant event — trust was granted when the host
        # first appeared, so only genuinely new hosts are audited.
        home, cfg = _setup_env(tmp_path, monkeypatch)
        cfg.write_text(json.dumps({
            "registries": [{"name": "acme", "repo": "https://github.com/acme/apps",
                            "branch": "main"}]
        }), encoding="utf-8")
        from kiro_crew.apps import routes as routes_mod
        mock_sel = routes_mod.sel()
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={"registries": [
                    {"repo": "https://github.com/acme/apps", "branch": "main"},
                    # A second path on the SAME host — still no new host trust.
                    {"repo": "https://github.com/acme/other"},
                ]},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["newlyTrustedHosts"] == []

        grants = [
            c for c in mock_sel.log_api_access.call_args_list
            if c.kwargs.get("operation") == "registries.host_trust_granted"
        ]
        assert grants == []

    @pytest.mark.asyncio
    async def test_name_defaults_to_repo(self, tmp_path, monkeypatch):
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={"registries": [{"repo": "SomeRepo"}]},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["registries"][0]["name"] == "SomeRepo"
            assert data["registries"][0]["branch"] == "main"

    @pytest.mark.asyncio
    async def test_replace_registries(self, tmp_path, monkeypatch):
        home, cfg = _setup_env(tmp_path, monkeypatch)
        cfg.write_text(json.dumps({
            "registries": [{"name": "old", "repo": "OldRepo", "branch": "mainline"}]
        }), encoding="utf-8")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={"registries": [
                    {"name": "new", "repo": "NewRepo", "branch": "dev"},
                ]},
            )
            assert resp.status == 200
            saved = json.loads(cfg.read_text(encoding="utf-8"))
            assert len(saved["registries"]) == 1
            assert saved["registries"][0]["repo"] == "NewRepo"

    @pytest.mark.asyncio
    async def test_empty_list_clears_registries(self, tmp_path, monkeypatch):
        home, cfg = _setup_env(tmp_path, monkeypatch)
        cfg.write_text(json.dumps({
            "registries": [{"name": "x", "repo": "X", "branch": "mainline"}]
        }), encoding="utf-8")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={"registries": []},
            )
            assert resp.status == 200
            saved = json.loads(cfg.read_text(encoding="utf-8"))
            assert saved["registries"] == []


# ---------------------------------------------------------------------------
# PUT /api/apps/registries — validation errors
# ---------------------------------------------------------------------------


class TestPutRegistriesValidation:
    @pytest.mark.asyncio
    async def test_rejects_non_array(self, tmp_path, monkeypatch):
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={"registries": "not-an-array"},
            )
            assert resp.status == 400
            data = await resp.json()
            assert "must be an array" in data["error"]

    @pytest.mark.asyncio
    async def test_rejects_missing_repo(self, tmp_path, monkeypatch):
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={"registries": [{"name": "foo"}]},
            )
            assert resp.status == 400
            data = await resp.json()
            assert "repo is required" in data["error"]

    @pytest.mark.asyncio
    async def test_rejects_invalid_repo_name(self, tmp_path, monkeypatch):
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={"registries": [{"repo": "../evil"}]},
            )
            assert resp.status == 400
            assert "invalid repo name" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_rejects_repo_with_spaces(self, tmp_path, monkeypatch):
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={"registries": [{"repo": "my repo"}]},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_rejects_blocked_repo(self, tmp_path, monkeypatch):
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={"registries": [{"repo": "KiroCrew"}]},
            )
            assert resp.status == 400
            data = await resp.json()
            assert "core registry" in data["error"]

    @pytest.mark.asyncio
    async def test_rejects_invalid_branch(self, tmp_path, monkeypatch):
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={"registries": [{"repo": "ValidRepo", "branch": "main/../evil"}]},
            )
            assert resp.status == 400
            assert "invalid branch" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_rejects_non_object_entry(self, tmp_path, monkeypatch):
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={"registries": ["not-an-object"]},
            )
            assert resp.status == 400
            assert "must be an object" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_rejects_invalid_json_body(self, tmp_path, monkeypatch):
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                data=b"not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_rejects_invalid_name(self, tmp_path, monkeypatch):
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={"registries": [{"repo": "ValidRepo", "name": "evil<script>"}]},
            )
            assert resp.status == 400
            assert "invalid registry name" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_returns_500_on_malformed_config(self, tmp_path, monkeypatch):
        home, cfg = _setup_env(tmp_path, monkeypatch)
        cfg.write_text("not valid json {{{", encoding="utf-8")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={"registries": [{"repo": "SomeRepo"}]},
            )
            assert resp.status == 500
            assert "malformed" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_null_registries_in_config_repaired_not_500(self, tmp_path, monkeypatch):
        # A config carrying an explicit ``"registries": null`` (valid JSON, so it
        # is not caught by the malformed-config guard) must not turn the repair
        # PUT into a 500: the prior-host diff iterates ``data.get("registries")
        # or []`` rather than the bare ``.get`` default, so it does not attempt
        # to loop over ``None``. The PUT must succeed and replace the null value.
        home, cfg = _setup_env(tmp_path, monkeypatch)
        cfg.write_text(json.dumps({"registries": None}), encoding="utf-8")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={"registries": [{"repo": "https://github.com/acme/apps"}]},
            )
            assert resp.status == 200
            data = await resp.json()
            # The malformed null was replaced and the new host is a fresh grant.
            assert data["newlyTrustedHosts"] == ["github.com"]
        persisted = json.loads(cfg.read_text(encoding="utf-8"))
        assert isinstance(persisted["registries"], list)
        assert len(persisted["registries"]) == 1

    @pytest.mark.asyncio
    async def test_accepts_valid_branch_with_slashes(self, tmp_path, monkeypatch):
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={"registries": [{"repo": "MyRepo", "branch": "feature/new-apps"}]},
            )
            assert resp.status == 200


# ---------------------------------------------------------------------------
# PUT /api/apps/registries — URL-aware repo validation
# ---------------------------------------------------------------------------


class TestPutRegistriesUrls:
    @pytest.mark.asyncio
    async def test_accepts_full_https_url(self, tmp_path, monkeypatch):
        home, cfg = _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={"registries": [
                    {"name": "acme", "repo": "https://github.com/acme/apps", "branch": "main"},
                ]},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["registries"][0]["repo"] == "https://github.com/acme/apps"

    @pytest.mark.asyncio
    async def test_accepts_scp_style_ssh_url(self, tmp_path, monkeypatch):
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={"registries": [
                    {"name": "acme", "repo": "git@github.com:acme/apps.git"},
                ]},
            )
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_accepts_ssh_scheme_url(self, tmp_path, monkeypatch):
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={"registries": [
                    {"name": "acme", "repo": "ssh://git@example.com:2222/org/app.git"},
                ]},
            )
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_bare_name_still_accepted(self, tmp_path, monkeypatch):
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={"registries": [{"repo": "LegacyBareName"}]},
            )
            assert resp.status == 200
            data = await resp.json()
            # Bare-name default: name == repo (legacy behavior preserved).
            assert data["registries"][0]["name"] == "LegacyBareName"

    @pytest.mark.asyncio
    async def test_rejects_owner_repo_shorthand(self, tmp_path, monkeypatch):
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={"registries": [{"repo": "acme/apps"}]},
            )
            assert resp.status == 400
            assert "invalid repo name" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_rejects_shell_metachar_junk(self, tmp_path, monkeypatch):
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={"registries": [{"repo": "https://github.com/a/b;rm -rf /"}]},
            )
            assert resp.status == 400
            assert "invalid repo name" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_rejects_plaintext_http_url(self, tmp_path, monkeypatch):
        # Plaintext http:// is a MITM vector: registry clones fetch an index +
        # app manifests whose setup code later runs with gateway privileges, so
        # only TLS-protected https:// (or explicit ssh) transports are accepted.
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={"registries": [{"repo": "http://github.com/acme/apps"}]},
            )
            assert resp.status == 400
            assert "invalid repo name" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_blocked_repo_still_blocked(self, tmp_path, monkeypatch):
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={"registries": [{"repo": "KiroCrew"}]},
            )
            assert resp.status == 400
            assert "core registry" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_default_name_derived_from_url(self, tmp_path, monkeypatch):
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={"registries": [{"repo": "https://github.com/acme/apps"}]},
            )
            assert resp.status == 200
            data = await resp.json()
            # Derived name is the host/path slug plus a stable 8-hex disambiguating
            # hash of the original URL, so slug-colliding URLs never share a name.
            name = data["registries"][0]["name"]
            assert re.fullmatch(r"github-com-acme-apps-[0-9a-f]{8}", name), name

    @pytest.mark.asyncio
    async def test_slug_colliding_urls_get_distinct_names(self, tmp_path, monkeypatch):
        # Two distinct URLs whose host/path slugify identically ('a-b' vs 'a_b'
        # both -> 'a-b') must NOT derive the same registry name, else they would
        # share one _external_registry_cache_path file and clobber each other.
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={
                    "registries": [
                        {"repo": "https://github.com/acme/a-b"},
                        {"repo": "https://github.com/acme/a_b"},
                    ]
                },
            )
            assert resp.status == 200
            data = await resp.json()
            names = [r["name"] for r in data["registries"]]
            assert len(set(names)) == 2, names

    @pytest.mark.asyncio
    async def test_branch_defaults_to_main_when_omitted(self, tmp_path, monkeypatch):
        # Backend owns the branch default; omitting it yields 'main' (not the
        # internal-forge 'mainline'), so API/config adds match the UI default.
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={"registries": [{"repo": "https://github.com/acme/apps"}]},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["registries"][0]["branch"] == "main"

    @pytest.mark.asyncio
    async def test_two_url_registries_get_distinct_default_names(self, tmp_path, monkeypatch):
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={"registries": [
                    {"repo": "https://github.com/acme/apps"},
                    {"repo": "https://gitlab.com/acme/apps"},
                ]},
            )
            assert resp.status == 200
            names = [r["name"] for r in (await resp.json())["registries"]]
            assert re.fullmatch(r"github-com-acme-apps-[0-9a-f]{8}", names[0]), names
            assert re.fullmatch(r"gitlab-com-acme-apps-[0-9a-f]{8}", names[1]), names
            assert len(set(names)) == 2


# ---------------------------------------------------------------------------
# POST /api/apps/registries/refresh
# ---------------------------------------------------------------------------


class TestRefreshRegistries:
    @pytest.mark.asyncio
    async def test_refresh_all_returns_contract_shape(self, tmp_path, monkeypatch):
        _setup_env(tmp_path, monkeypatch)
        from unittest.mock import AsyncMock
        monkeypatch.setattr(
            "kiro_crew.apps.registry.list_registry",
            AsyncMock(return_value=[{"name": "a"}, {"name": "b"}]),
        )
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/apps/registries/refresh", json={})
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert data["refreshed"] == []  # no registries configured
            assert data["apps"] == 2
            assert isinstance(data["lastSyncedAt"], str)

    @pytest.mark.asyncio
    async def test_refresh_no_body(self, tmp_path, monkeypatch):
        _setup_env(tmp_path, monkeypatch)
        from unittest.mock import AsyncMock
        monkeypatch.setattr(
            "kiro_crew.apps.registry.list_registry",
            AsyncMock(return_value=[]),
        )
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/apps/registries/refresh")
            assert resp.status == 200
            assert (await resp.json())["ok"] is True

    @pytest.mark.asyncio
    async def test_refresh_single_repo(self, tmp_path, monkeypatch):
        home, cfg = _setup_env(tmp_path, monkeypatch)
        cfg.write_text(json.dumps({
            "registries": [
                {"name": "acme", "repo": "https://github.com/acme/apps", "branch": "main"},
                {"name": "other", "repo": "https://github.com/other/apps", "branch": "main"},
            ]
        }), encoding="utf-8")
        from unittest.mock import AsyncMock
        monkeypatch.setattr(
            "kiro_crew.apps.registry._fetch_external_registry_index",
            AsyncMock(return_value=[{"name": "x", "repo": "R"}]),
        )
        monkeypatch.setattr(
            "kiro_crew.apps.registry.list_registry",
            AsyncMock(return_value=[{"name": "x"}]),
        )
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/apps/registries/refresh",
                json={"repo": "https://github.com/acme/apps"},
            )
            assert resp.status == 200
            data = await resp.json()
            # Only the matching registry is refreshed (fetch-then-swap success).
            assert data["refreshed"] == ["acme"]
            assert data["failed"] == []
            assert data["apps"] == 1

    @pytest.mark.asyncio
    async def test_refresh_unconfigured_repo_404(self, tmp_path, monkeypatch):
        # GPT 5.6 MEDIUM: a valid-but-unconfigured repo matches no registry;
        # the endpoint must return 404, not a misleading ok:true "synced".
        _setup_env(tmp_path, monkeypatch)[1].write_text(
            json.dumps({
                "registries": [
                    {"name": "acme", "repo": "https://github.com/acme/apps",
                     "branch": "main"},
                ]
            }),
            encoding="utf-8",
        )
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/apps/registries/refresh",
                json={"repo": "https://github.com/nope/absent"},
            )
            assert resp.status == 404
            assert "no configured registry" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_refresh_invalid_repo_400(self, tmp_path, monkeypatch):
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/apps/registries/refresh",
                json={"repo": "../evil;rm -rf /"},
            )
            assert resp.status == 400
            assert "invalid repo" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_refresh_invalid_json_400(self, tmp_path, monkeypatch):
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/apps/registries/refresh",
                data=b"not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_refresh_non_object_body_400(self, tmp_path, monkeypatch):
        # A valid-but-non-object JSON body (e.g. []) must 400, not silently
        # slip past the dict guard with repo=None and fan out a refresh of
        # EVERY configured registry (unintended git clones / cache writes).
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/apps/registries/refresh",
                data=b"[]",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
            assert "must be a JSON object" in (await resp.json())["error"]

"""Tests for trust grants keyed by a registry name, and for what the snapshot reports.

A registry name that is not installed yet is grantable; a repository grant binds
to the current repository, never persists clone credentials and needs a matching
consent proof; the snapshot reports an entry the gate cannot enforce as
ineffective. The handlers run through the same in-test ``TestClient`` as
``test_trusted_apps_api.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from trusted_apps_api_helpers import home_fixture  # noqa: F401
from trusted_apps_api_helpers import mock_sel_fixture  # noqa: F401
from trusted_apps_api_helpers import _APP, _OTHER, _client, _install, _stored

from kiro_crew.apps.manager import _read_installed, _write_installed
from kiro_crew.dashboard.handlers import security
from kiro_crew.dashboard.handlers.security import build_trusted_apps_snapshot

# ── 5. a registry name that is not installed yet is grantable ──

_REG_ONLY = "registry-only-app"
_REG_ONLY_REPO_ALIAS = "https://example.test/display/registry-only-app"
_REG_ONLY_CLONE_TARGET = "https://clone.example.test/Owner/registry-only-app"


@pytest.fixture
def seeded_registry(monkeypatch: pytest.MonkeyPatch):
    """Seed the BUNDLED registry index in-process (never touches the network)."""
    import kiro_crew.apps.registry as registry

    monkeypatch.setattr(
        registry,
        "_load_registry_file",
        lambda: [
            {
                "name": _REG_ONLY,
                "repo": _REG_ONLY_REPO_ALIAS,
                "gitUrl": f"{_REG_ONLY_CLONE_TARGET}.git",
                "branch": "main",
            }
        ],
    )
    # "Never touches the network" needs the catalog pinned too: resolution
    # consults the official catalog BEFORE the seed row, with a fresh uncached
    # HTTPS fetch — and with a seed row present a failed lookup refuses
    # rather than falling back to the seed.
    monkeypatch.setattr(
        "kiro_crew.apps.official_catalog.inventory_for_install",
        lambda name: None,
    )
    return _REG_ONLY


@pytest.mark.asyncio
async def test_grant_accepts_a_registry_name_that_is_not_installed(
    home: Path, mock_sel, seeded_registry: str
):
    # REGRESSION: requiring an INSTALL deadlocked the feature. ``install_from_
    # registry`` checks the execution gate BEFORE cloning (clone/build/onInstall
    # are themselves third-party code), so "no grant without an install, no
    # install without a grant" left the operator only the blanket
    # ``apps_allow_third_party`` this endpoint exists to avoid.
    from kiro_crew.apps.manager import get_app

    assert get_app(_REG_ONLY) is None, "fixture must NOT install the app"

    async with _client() as client:
        resp = await client.post(
            f"/api/security/trusted-apps/{_REG_ONLY}",
            json={"repository": _REG_ONLY_CLONE_TARGET},
        )
        assert resp.status == 200, await resp.text()
        assert (await resp.json())["apps"] == [_REG_ONLY]

    # Persisted AND effective, so the install that follows is admitted.
    assert _stored(home)["apps_trusted"] == [_REG_ONLY]
    assert _stored(home)["apps_trusted_repositories"] == {_REG_ONLY: _REG_ONLY_CLONE_TARGET}
    from kiro_crew.apps.execution import app_execution_denied

    assert (
        app_execution_denied(
            _REG_ONLY,
            action="install",
            repository=_REG_ONLY_CLONE_TARGET,
        )
        is None
    )


@pytest.mark.asyncio
async def test_repository_grant_never_persists_embedded_clone_credentials(
    home: Path,
    mock_sel,
    seeded_registry: str,
    monkeypatch: pytest.MonkeyPatch,
):
    import kiro_crew.apps.registry as registry

    secret = "SuperSecret"
    credentialed = f"HTTPS://User:{secret}@Clone.Example.test/Owner/registry-only-app.git"
    reviewed = _REG_ONLY_CLONE_TARGET
    monkeypatch.setattr(
        registry,
        "_load_registry_file",
        lambda: [{"name": _REG_ONLY, "gitUrl": credentialed, "branch": "main"}],
    )

    async with _client() as client:
        resp = await client.post(
            f"/api/security/trusted-apps/{_REG_ONLY}",
            json={"repository": reviewed},
        )
        assert resp.status == 200, await resp.text()
        assert secret not in await resp.text()

    stored_raw = (home / "config.json").read_text(encoding="utf-8")
    assert secret not in stored_raw
    assert _stored(home)["apps_trusted_repositories"] == {_REG_ONLY: reviewed}
    assert registry._same_git_target(reviewed, credentialed)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        None,
        {"repository": _REG_ONLY_REPO_ALIAS},
        {"repository": "https://clone.example.test/Owner/different-app"},
    ],
)
async def test_repository_grant_requires_matching_consent_proof(
    home: Path, mock_sel, seeded_registry: str, body: dict[str, str] | None
):
    async with _client() as client:
        if body is None:
            resp = await client.post(f"/api/security/trusted-apps/{_REG_ONLY}")
        else:
            resp = await client.post(f"/api/security/trusted-apps/{_REG_ONLY}", json=body)

        assert resp.status == 409
        assert (await resp.json())["code"] == "app_trust_repository_mismatch"

    assert _stored(home).get("apps_trusted", []) == []
    assert _stored(home).get("apps_trusted_repositories", {}) == {}


@pytest.mark.asyncio
async def test_legacy_registry_install_binds_grant_to_current_repository(
    home: Path, tmp_path: Path, mock_sel, monkeypatch: pytest.MonkeyPatch
):
    """A legacy bare-name source cannot degrade into a name-only grant."""
    _install(tmp_path, _APP)
    meta = _read_installed(_APP)
    assert meta is not None
    meta.source = f"registry:{_APP}"
    meta.sourceUrl = ""
    _write_installed(_APP, meta)

    current_repository = "https://clone.example.test/Owner/current-app"
    monkeypatch.setattr(
        "kiro_crew.apps.registry.get_registry_app",
        lambda name: {"name": name, "gitUrl": f"{current_repository}.git"},
    )

    async with _client() as client:
        stale = await client.post(
            f"/api/security/trusted-apps/{_APP}",
            json={"repository": "https://clone.example.test/Owner/replaced-app"},
        )
        assert stale.status == 409
        assert (await stale.json())["code"] == "app_trust_repository_mismatch"

        granted = await client.post(
            f"/api/security/trusted-apps/{_APP}",
            json={"repository": current_repository},
        )
        assert granted.status == 200, await granted.text()

    assert _stored(home)["apps_trusted_repositories"] == {_APP: current_repository}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source_url",
    [
        "https://clone.example.test/Owner/App.git?repo=A",
        "deploy:password@example.invalid:Owner/App.git",
        "ssh://deploy:password@example.invalid/Owner/App.git",
    ],
)
async def test_legacy_unsupported_install_cannot_receive_repository_grant(
    home: Path, tmp_path: Path, mock_sel, source_url: str
):
    """Old ambiguous provenance is unknown until reinstalled from a safe URL."""
    from kiro_crew.apps.manager import INSTALLED_META_FILENAME, app_dir

    _install(tmp_path, _APP)
    meta_path = app_dir(_APP) / INSTALLED_META_FILENAME
    legacy_meta = json.loads(meta_path.read_text(encoding="utf-8"))
    legacy_meta["source"] = f"registry:{_APP}"
    legacy_meta["sourceUrl"] = source_url
    # Deliberately bypass today's sanitized write boundary to model an upgrade
    # from a version that persisted this ambiguous source verbatim.
    meta_path.write_text(json.dumps(legacy_meta), encoding="utf-8")

    async with _client() as client:
        response = await client.post(
            f"/api/security/trusted-apps/{_APP}",
            json={"repository": "https://clone.example.test/Owner/App"},
        )
        payload = await response.json()

    assert response.status == 404
    assert payload["code"] == "app_not_installed"
    assert _stored(home).get("apps_trusted", []) == []
    assert _stored(home).get("apps_trusted_repositories", {}) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["grant", "revoke"])
@pytest.mark.parametrize(
    "legacy_binding",
    [
        "https://example.test/owner/old?repo=A",
        "deploy:password@example.invalid:owner/old.git",
        "ssh://deploy:password@example.invalid/owner/old.git",
    ],
)
async def test_unrelated_mutation_never_heals_legacy_unsupported_binding(
    home: Path,
    tmp_path: Path,
    mock_sel,
    operation: str,
    legacy_binding: str,
):
    """Changing app B drops, rather than normalizes, app A's inert binding."""
    _install(tmp_path, _APP)
    trusted = [_OTHER]
    local: list[str] = []
    if operation == "revoke":
        trusted.append(_APP)
        local.append(_APP)
    (home / "config.json").write_text(
        json.dumps(
            {
                "agent": {
                    "apps_trusted": trusted,
                    "apps_trusted_local": local,
                    "apps_trusted_repositories": {_OTHER: legacy_binding},
                }
            }
        ),
        encoding="utf-8",
    )

    async with _client() as client:
        if operation == "grant":
            response = await client.post(f"/api/security/trusted-apps/{_APP}")
        else:
            response = await client.delete(f"/api/security/trusted-apps/{_APP}")
        assert response.status == 200, await response.text()

    stored = _stored(home)
    assert _OTHER in stored["apps_trusted"]
    assert _OTHER not in stored.get("apps_trusted_repositories", {})
    assert _OTHER in build_trusted_apps_snapshot()["ineffective"]


@pytest.mark.parametrize(
    "entry_url",
    [
        "https://example.test/owner/registry-only?repo=other",
        "deploy:password@example.invalid:owner/registry-only.git",
        "ssh://deploy:password@example.invalid/owner/registry-only.git",
    ],
)
def test_uninstalled_unsupported_registry_row_is_never_reported_effective(
    home: Path, mock_sel, monkeypatch: pytest.MonkeyPatch, entry_url: str
):
    safe_repository = "https://example.test/owner/registry-only"
    (home / "config.json").write_text(
        json.dumps(
            {
                "agent": {
                    "apps_trusted": [_REG_ONLY],
                    "apps_trusted_repositories": {_REG_ONLY: safe_repository},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(security, "get_app", lambda name: None)
    monkeypatch.setattr(
        security,
        "get_registry_app",
        lambda name: {"name": name, "gitUrl": entry_url},
    )

    assert build_trusted_apps_snapshot() == {
        "apps": [],
        "ineffective": [_REG_ONLY],
        "allowAll": False,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "proof",
    [
        "https://example.test/registry-only?access_token=proof-query-secret",
        "deploy:proof-scp-secret@example.invalid:owner/registry-only.git",
        "ssh://deploy:proof-ssh-secret@example.invalid/owner/registry-only.git",
    ],
)
async def test_unsupported_consent_proof_is_rejected_without_echo(
    home: Path, mock_sel, seeded_registry: str, proof: str
):
    async with _client() as client:
        response = await client.post(
            f"/api/security/trusted-apps/{_REG_ONLY}",
            json={"repository": proof},
        )
        body = await response.text()

    assert response.status == 400
    assert json.loads(body)["code"] == "invalid_repository_consent"
    assert proof not in body
    assert "proof-query-secret" not in body
    assert "proof-scp-secret" not in body
    assert "proof-ssh-secret" not in body
    assert _stored(home).get("apps_trusted", []) == []


@pytest.mark.asyncio
async def test_grant_of_a_name_in_neither_registry_nor_installs_404s(
    home: Path, mock_sel, seeded_registry: str
):
    # The registry leg widens WHICH names are acceptable, not whether the check
    # runs: a name in neither the index nor the installs is still a 404, so an
    # arbitrary name cannot sit in config waiting for an app to claim it.
    async with _client() as client:
        resp = await client.post("/api/security/trusted-apps/not-in-any-registry")
        assert resp.status == 404
        assert (await resp.json())["code"] == "app_not_installed"

    assert _stored(home).get("apps_trusted", []) == []
    assert build_trusted_apps_snapshot()["apps"] == []


# ── 6. the snapshot tells the truth about what is enforced ──

# Stored entries the ENFORCEMENT reader (``trusted_app_names``) drops: wrong case,
# a trailing space, a fullwidth homoglyph that is not ASCII at all, traversal, and
# a glob. Every one of these renders as a grant unless the snapshot splits them
# out — a security panel claiming trust that admits nothing, with no way for the
# user to see why their app is still blocked.
_INEFFECTIVE_ENTRIES = ["LD-App", "ld-app ", "ｌd-app", "..", "*"]


@pytest.mark.asyncio
async def test_snapshot_reports_unenforceable_entries_as_ineffective(
    home: Path, tmp_path: Path, mock_sel
):
    from kiro_crew.apps.execution import app_execution_denied, trusted_app_names

    _install(tmp_path, _APP)
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_trusted": [_APP, *_INEFFECTIVE_ENTRIES]}}),
        encoding="utf-8",
    )

    snapshot = build_trusted_apps_snapshot()

    # NONE of the junk is reported as an enforced grant; ALL of it is surfaced as
    # ineffective so the user can see the entry exists and does nothing.
    assert snapshot["apps"] == [_APP]
    for entry in _INEFFECTIVE_ENTRIES:
        assert entry not in snapshot["apps"], f"{entry!r} reported as enforced"
        assert entry in snapshot["ineffective"], f"{entry!r} silently dropped"
    assert sorted(_INEFFECTIVE_ENTRIES) == snapshot["ineffective"]

    # The invariant behind the split: ``apps`` is EXACTLY what the gate enforces.
    stored = set(json.loads((home / "config.json").read_text())["agent"]["apps_trusted"])
    assert set(snapshot["apps"]) == stored & set(trusted_app_names())
    # And the gate agrees with the report, entry by entry.
    assert app_execution_denied(_APP, action="enable") is None
    assert app_execution_denied("LD-App", action="enable") is not None


@pytest.mark.asyncio
async def test_snapshot_reflected_over_the_api_matches_the_gate(
    home: Path, tmp_path: Path, mock_sel
):
    # The split must survive the wire, not just the helper: the GET is what the
    # Security panel actually renders.
    _install(tmp_path, _APP)
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_trusted": [_APP, *_INEFFECTIVE_ENTRIES]}}),
        encoding="utf-8",
    )
    async with _client() as client:
        body = await (await client.get("/api/security/trusted-apps")).json()
    assert body["apps"] == [_APP]
    assert body["ineffective"] == sorted(_INEFFECTIVE_ENTRIES)

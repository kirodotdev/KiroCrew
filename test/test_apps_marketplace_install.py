"""Marketplace app installer: verify, safe unpack, install disabled, per-app trust.

Pins the three P4 contracts of the marketplace apps spec (``marketplace-apps.md``
section 5, threat model rows T-M1, T-M4, T-M5 and T-M8):

* a malicious zip, or a zip whose SHA-256 differs from the resolved set, is
  refused, and nothing is written outside the staging folder;
* a fresh marketplace install leaves the app disabled and registers no agents,
  skills, MCP servers or crons, and starts no backend;
* with local-app trust already granted, enabling a marketplace app still asks
  for its own trust grant.

The listing server does not exist yet, so the installer talks to a small client
interface and these tests drive it with an in-memory fake.
"""

from __future__ import annotations

import hashlib
import io
import json
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import pytest
from app_manager_helpers import app_home_fixture  # noqa: F401 - fixture

APP_NAME = "ticket-digest"
REF = f"acme/{APP_NAME}"


def _manifest(**overrides: Any) -> dict[str, Any]:
    return {
        "name": APP_NAME,
        "version": "1.2.0",
        "displayName": "Ticket Digest",
        "description": "Summarise tickets every morning",
        "author": "acme",
        "agents": ["agents/digest.json"],
        "skills": ["skills/digest"],
        "crons": [{"name": "daily-digest", "every": 3600, "message": "digest"}],
        "permissions": {"cron": True},
        **overrides,
    }


def _app_files(manifest: dict[str, Any] | None = None) -> dict[str, bytes]:
    return {
        "app.json": json.dumps(manifest or _manifest()).encode(),
        "market.json": json.dumps({"schema": 1, "components": []}).encode(),
        "README.md": b"# Ticket Digest\n",
        "agents/digest.json": json.dumps(
            {"name": "digest", "description": "digest agent", "prompt": "hi"}
        ).encode(),
        "skills/digest/SKILL.md": b"---\nname: digest\ndescription: digest\n---\nbody\n",
    }


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
    return buf.getvalue()


def _zip_with(extra: list[tuple[zipfile.ZipInfo, bytes]]) -> bytes:
    """A valid app zip plus hostile entries appended after the good ones."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in _app_files().items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
        for info, data in extra:
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
    return buf.getvalue()


def _entry(name: str, mode: int = stat.S_IFREG | 0o644) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name)
    info.external_attr = mode << 16
    return info


@dataclass
class FakeMarketClient:
    """In-memory stand-in for the listing API and the signed-URL download."""

    payload: bytes
    resolved_sha: str | None = None
    download_sha: str | None = None
    url: str = "https://downloads.example.test/signed"
    permissions: dict[str, list[str]] = field(
        default_factory=lambda: {
            "commands": ["node"],
            "network": ["api.example.test"],
            "files": [],
            "background": ["cron: daily-digest 0 9 * * *"],
            "agents": ["agents/digest.json"],
            "mcpServers": [],
        }
    )
    calls: list[str] = field(default_factory=list)

    def resolve_install(self, ref: str, version: int | None) -> Any:
        from kiro_crew.apps.marketplace_install import ResolvedApp

        self.calls.append("resolve")
        return ResolvedApp(
            ref=ref,
            version=version or 3,
            content_sha256=self.resolved_sha or hashlib.sha256(self.payload).hexdigest(),
            size=len(self.payload),
            published_by="alice",
            owner_group="acme-tools",
            app_version="1.2.0",
            permissions=self.permissions,
        )

    def get_download(self, ref: str, version: int) -> Any:
        from kiro_crew.apps.marketplace_install import ListingDownload

        self.calls.append("download")
        return ListingDownload(
            url=self.url,
            sha256=self.download_sha
            or self.resolved_sha
            or hashlib.sha256(self.payload).hexdigest(),
        )

    def open_download(self, url: str) -> Iterable[bytes]:
        self.calls.append("fetch")
        for start in range(0, len(self.payload), 65536):
            yield self.payload[start : start + 65536]


def _tree(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


def _outside_staging(home: Path) -> set[str]:
    from kiro_crew.apps.marketplace_install import market_staging_root

    staging = market_staging_root()
    return {
        str(p.relative_to(home))
        for p in home.rglob("*")
        if p != staging and staging not in p.parents
    }


# ---------------------------------------------------------------------------
# (a) malicious zips and hash mismatch are refused, nothing outside staging
# ---------------------------------------------------------------------------

_HOSTILE = {
    "zip_slip": [(_entry("../escaped.txt"), b"x")],
    "nested_zip_slip": [(_entry("skills/../../escaped.txt"), b"x")],
    "absolute_path": [(_entry("/escaped-abs.txt"), b"x")],
    "drive_letter": [(_entry("C:/escaped.txt"), b"x")],
    "nested_drive_letter": [(_entry("safe/D:escaped.txt"), b"x")],
    "alternate_data_stream": [(_entry("README.txt:hidden"), b"x")],
    "backslash": [(_entry("skills\\..\\escaped.txt"), b"x")],
    "symlink": [(_entry("link", stat.S_IFLNK | 0o777), b"../../outside")],
    "hardlink_duplicate_name": [(_entry("README.md"), b"second copy")],
    "case_fold_collision": [(_entry("readme.MD"), b"collides")],
    "char_device": [(_entry("dev", stat.S_IFCHR | 0o644), b"")],
    "fifo": [(_entry("pipe", stat.S_IFIFO | 0o644), b"")],
    "socket": [(_entry("sock", stat.S_IFSOCK | 0o644), b"")],
    "setuid": [(_entry("bin/tool", stat.S_IFREG | stat.S_ISUID | 0o755), b"x")],
    "control_char": [(_entry("bad\x01name"), b"x")],
    "too_deep": [(_entry("d/" * 17 + "f"), b"x")],
    "ratio_bomb": [(_entry("bomb.bin"), b"\0" * (4 * 1024 * 1024))],
    "entry_too_large": [(_entry("big.bin"), bytes(range(256)) * (26 * 4096))],
}


@pytest.mark.parametrize("case", sorted(_HOSTILE))
def test_hostile_zip_is_refused_and_nothing_lands_outside_staging(case, app_home, tmp_path):
    from kiro_crew.apps.marketplace_install import (
        MarketInstallError,
        market_staging_root,
        prepare_market_install,
    )

    before = _outside_staging(app_home)
    client = FakeMarketClient(payload=_zip_with(_HOSTILE[case]))

    with pytest.raises(MarketInstallError):
        prepare_market_install(client, REF)

    assert _outside_staging(app_home) == before
    staging = market_staging_root()
    assert not staging.exists() or _tree(staging) == set()
    assert not (tmp_path / "escaped.txt").exists()
    assert not Path("/escaped-abs.txt").exists()


def test_too_many_entries_is_refused(app_home):
    from kiro_crew.apps.marketplace_install import MarketInstallError, prepare_market_install

    extra = [(_entry(f"many/{i}.txt"), b"x") for i in range(2001)]
    client = FakeMarketClient(payload=_zip_with(extra))
    with pytest.raises(MarketInstallError) as err:
        prepare_market_install(client, REF)
    assert err.value.code == "market_archive_refused"


def test_sha256_mismatch_is_refused_before_unpack(app_home):
    from kiro_crew.apps.marketplace_install import (
        MarketInstallError,
        market_staging_root,
        prepare_market_install,
    )

    before = _outside_staging(app_home)
    client = FakeMarketClient(payload=_zip(_app_files()), resolved_sha="0" * 64)
    with pytest.raises(MarketInstallError) as err:
        prepare_market_install(client, REF)
    assert err.value.code == "market_sha256_mismatch"
    assert "fetch" in client.calls  # it was downloaded, then refused
    assert _outside_staging(app_home) == before
    staging = market_staging_root()
    assert not staging.exists() or _tree(staging) == set()


def test_download_hash_that_differs_from_resolved_set_is_refused(app_home):
    from kiro_crew.apps.marketplace_install import MarketInstallError, prepare_market_install

    client = FakeMarketClient(payload=_zip(_app_files()), download_sha="1" * 64)
    with pytest.raises(MarketInstallError) as err:
        prepare_market_install(client, REF)
    assert err.value.code == "market_sha256_mismatch"
    assert "fetch" not in client.calls


def test_oversized_download_is_refused_while_streaming(app_home):
    from kiro_crew.apps.marketplace_install import MarketInstallError, prepare_market_install

    payload = b"\0" * (25 * 1024 * 1024 + 1)
    client = FakeMarketClient(payload=payload)
    with pytest.raises(MarketInstallError) as err:
        prepare_market_install(client, REF)
    assert err.value.code == "market_download_too_large"


@pytest.mark.parametrize("url", ["http://downloads.example.test/signed", "file:///x"])
def test_signed_url_must_be_https(url, app_home):
    from kiro_crew.apps.marketplace_install import MarketInstallError, prepare_market_install

    client = FakeMarketClient(payload=_zip(_app_files()), url=url)
    with pytest.raises(MarketInstallError) as err:
        prepare_market_install(client, REF)
    assert err.value.code == "market_download_refused"
    assert "fetch" not in client.calls


def test_confirm_data_says_permissions_are_declared_not_enforced(app_home):
    from kiro_crew.apps.marketplace_install import prepare_market_install

    payload = _zip(_app_files())
    prepared = prepare_market_install(FakeMarketClient(payload=payload), REF)
    try:
        confirm = prepared.confirm
        assert confirm["permissions"]["notice"] == "declared by the publisher, not enforced"
        assert confirm["permissions"]["commands"] == ["node"]
        assert confirm["trustLabel"] == "Community, not reviewed by Kiro Space"
        assert confirm["app"]["publisher"] == "alice"
        assert confirm["app"]["version"] == 3
        assert confirm["app"]["appVersion"] == "1.2.0"
        # What the unpacked folder itself declares, not only the server's list.
        assert confirm["inFolder"] == {
            "crons": ["daily-digest"],
            "mcpServers": [],
            "agents": ["agents/digest.json"],
        }
        assert hashlib.sha256(payload).hexdigest().startswith(confirm["app"]["sha256Prefix"])
        # Unpacked inside the staging root, and only there.
        assert prepared.app_root.is_relative_to(prepared.staging_dir)
        assert (prepared.app_root / "app.json").is_file()
    finally:
        prepared.discard()
    assert not prepared.staging_dir.exists()


_MCP_SERVER = {"tickets": {"command": "node", "args": ["server.js"]}}


@pytest.mark.parametrize(
    "case",
    ["undeclared_cron", "undeclared_mcp_server", "undeclared_agent", "cron_prefix_only"],
)
def test_folder_capability_missing_from_resolved_permissions_is_refused(case, app_home):
    from kiro_crew.apps.marketplace_install import (
        MarketInstallError,
        market_staging_root,
        prepare_market_install,
    )

    client = FakeMarketClient(payload=_zip(_app_files()))
    if case == "undeclared_cron":
        client.permissions = {**client.permissions, "background": []}
    elif case == "cron_prefix_only":
        # "daily-digest-v2" must not cover a cron named "daily-digest".
        client.permissions = {**client.permissions, "background": ["cron: daily-digest-v2"]}
    elif case == "undeclared_agent":
        client.permissions = {**client.permissions, "agents": []}
    else:
        files = _app_files(_manifest(mcpServers=_MCP_SERVER))
        files["server.js"] = b"// server\n"
        client = FakeMarketClient(payload=_zip(files))

    before = _outside_staging(app_home)
    with pytest.raises(MarketInstallError) as err:
        prepare_market_install(client, REF)

    assert err.value.code == "market_undeclared_capability"
    assert _outside_staging(app_home) == before
    staging = market_staging_root()
    assert not staging.exists() or _tree(staging) == set()


def test_declared_mcp_server_is_listed_in_confirm(app_home):
    from kiro_crew.apps.marketplace_install import prepare_market_install

    files = _app_files(_manifest(mcpServers=_MCP_SERVER))
    files["server.js"] = b"// server\n"
    client = FakeMarketClient(payload=_zip(files))
    client.permissions = {**client.permissions, "mcpServers": ["tickets"]}
    prepared = prepare_market_install(client, REF)
    try:
        assert prepared.confirm["inFolder"]["mcpServers"] == ["tickets"]
    finally:
        prepared.discard()


# ---------------------------------------------------------------------------
# (b) a fresh install stays disabled and registers / starts nothing
# ---------------------------------------------------------------------------


def test_fresh_marketplace_install_is_disabled_and_registers_nothing(app_home, monkeypatch):
    from kiro_crew.apps import backend, bridges, bridges_crons, manager
    from kiro_crew.apps.manager import _read_installed, get_app
    from kiro_crew.apps.marketplace_install import complete_market_install, prepare_market_install

    sources_written: list[str] = []
    real_write = manager._write_installed

    def _spy_write(name: str, meta: Any, **kwargs: Any) -> None:
        sources_written.append(meta.source)
        real_write(name, meta, **kwargs)

    # Through the manager facade, which reaches every owner that binds the name.
    monkeypatch.setattr(manager, "_write_installed", _spy_write)

    def _forbidden(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("marketplace install must not register or start anything")

    # Through the bridges facade, which reaches every owner that binds the name.
    monkeypatch.setattr(bridges, "register_app", _forbidden)
    monkeypatch.setattr(bridges, "_register_crons", _forbidden)
    monkeypatch.setattr(bridges, "register_app_crons_with_service", _forbidden)
    monkeypatch.setattr(backend, "start_app_backend", _forbidden)

    payload = _zip(_app_files())
    sha = hashlib.sha256(payload).hexdigest()
    prepared = prepare_market_install(FakeMarketClient(payload=payload), REF)
    staging_dir = prepared.staging_dir

    result = complete_market_install(prepared, confirmed=True)

    assert result.ok, result.error
    info = get_app(APP_NAME)
    assert info is not None and info["enabled"] is False
    meta = _read_installed(APP_NAME)
    assert meta is not None
    assert meta.enabled is False
    assert meta.origin == "local"
    assert meta.source == f"market:{REF}@3"
    assert meta.sourceCommit == sha
    assert meta.sourceUrl == ""
    # No cron file, no backend process, no agent or skill links.
    assert not bridges_crons._app_crons_path(APP_NAME).exists()
    from kiro_crew.apps.backend_runtime import tracking

    assert APP_NAME not in tracking._processes
    assert tracking.get_app_backend_port(APP_NAME) is None
    # The staging copy is gone; Kiro Crew keeps its own.
    assert not staging_dir.exists()
    # The first durable record already names the listing: no record ever
    # names the staging folder, which a local-folder grant would admit.
    assert sources_written == [f"market:{REF}@3"]


def test_install_time_trust_check_uses_the_listing_coordinate(app_home):
    from kiro_crew.apps.manager import get_app
    from kiro_crew.apps.marketplace_install import complete_market_install, prepare_market_install

    _write_agent_config(
        app_home,
        {
            "apps_allow_third_party": False,
            "apps_trusted": [APP_NAME],
            "apps_trusted_local": [],
            "apps_trusted_repositories": {APP_NAME: f"market:{REF}"},
        },
    )
    prepared = prepare_market_install(FakeMarketClient(payload=_zip(_app_files())), REF)
    result = complete_market_install(prepared, confirmed=True)
    assert result.ok, result.error
    assert get_app(APP_NAME) is not None


def test_malformed_market_source_is_refused_by_install_app(app_home, tmp_path):
    from kiro_crew.apps.manager import get_app, install_app

    src = tmp_path / "src" / APP_NAME
    src.mkdir(parents=True)
    for rel, data in _app_files().items():
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_bytes(data)
    result = install_app(src, expected_name=APP_NAME, market_source="market:../x@1")
    assert not result.ok
    assert result.error_code == "market_source_invalid"
    assert get_app(APP_NAME) is None


def test_install_without_explicit_yes_installs_nothing(app_home):
    from kiro_crew.apps.manager import get_app
    from kiro_crew.apps.marketplace_install import complete_market_install, prepare_market_install

    prepared = prepare_market_install(FakeMarketClient(payload=_zip(_app_files())), REF)
    result = complete_market_install(prepared, confirmed=False)
    assert not result.ok
    assert result.error_code == "market_install_not_confirmed"
    assert get_app(APP_NAME) is None
    assert not prepared.staging_dir.exists()


def test_listing_name_must_match_manifest_name(app_home):
    from kiro_crew.apps.manager import get_app
    from kiro_crew.apps.marketplace_install import complete_market_install, prepare_market_install

    payload = _zip(_app_files(_manifest(name="other-app")))
    prepared = prepare_market_install(FakeMarketClient(payload=payload), REF)
    result = complete_market_install(prepared, confirmed=True)
    assert not result.ok
    assert get_app("other-app") is None


# ---------------------------------------------------------------------------
# (c) local trust never covers a marketplace app
# ---------------------------------------------------------------------------


def _write_agent_config(home: Path, agent: dict[str, Any]) -> None:
    (home / "config.json").write_text(json.dumps({"agent": agent}), encoding="utf-8")


def _install_market_record(tmp_path: Path, source: str = f"market:{REF}@3") -> None:
    """Install an app from a folder and record it as a marketplace install.

    Built without the marketplace installer on purpose, so this contract is
    pinned at the trust gate itself, whichever installer wrote the record.
    """
    from kiro_crew.apps.manager import _read_installed, _write_installed, install_app

    src = tmp_path / "market-src" / APP_NAME
    src.mkdir(parents=True)
    for rel, data in _app_files().items():
        path = src / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    result = install_app(src, expected_name=APP_NAME)
    assert result.ok, result.error
    meta = _read_installed(APP_NAME)
    assert meta is not None
    from dataclasses import replace

    _write_installed(
        APP_NAME,
        replace(meta, origin="local", source=source, sourceCommit="a" * 64),
    )


@pytest.mark.parametrize("source", ["market:x", "market:acme/x", f"market:{REF}@0", "market:"])
def test_malformed_market_source_is_denied_even_with_local_trust(source, app_home, tmp_path):
    from kiro_crew.apps.manager import enable_app, get_app

    _write_agent_config(app_home, {"apps_allow_third_party": False})
    _install_market_record(tmp_path, source=source)
    _write_agent_config(
        app_home,
        {
            "apps_allow_third_party": False,
            "apps_trusted": [APP_NAME],
            "apps_trusted_local": [APP_NAME],
            "apps_trusted_repositories": {},
        },
    )

    result = enable_app(APP_NAME)

    assert not result.ok
    assert result.error_code == "app_execution_denied"
    info = get_app(APP_NAME)
    assert info is not None and info["enabled"] is False


def test_local_trust_grant_does_not_admit_a_marketplace_app(app_home, tmp_path):
    from kiro_crew.apps.manager import enable_app, get_app

    _write_agent_config(app_home, {"apps_allow_third_party": False})
    _install_market_record(tmp_path)
    # The person once trusted local code under this name.
    _write_agent_config(
        app_home,
        {
            "apps_allow_third_party": False,
            "apps_trusted": [APP_NAME],
            "apps_trusted_local": [APP_NAME],
            "apps_trusted_repositories": {},
        },
    )

    result = enable_app(APP_NAME)

    assert not result.ok
    assert result.error_code == "app_execution_denied"
    info = get_app(APP_NAME)
    assert info is not None and info["enabled"] is False


def test_marketplace_app_offers_its_own_trust_coordinate(app_home, tmp_path):
    from kiro_crew.apps.manager import get_app
    from kiro_crew.apps.registry import resolve_installed_trust_repository

    _write_agent_config(app_home, {"apps_allow_third_party": False})
    _install_market_record(tmp_path)
    info = get_app(APP_NAME)
    assert info is not None
    assert resolve_installed_trust_repository(info, allow_registry_lookup=False) == (
        True,
        f"market:{REF}",
    )


def test_per_app_marketplace_grant_admits_the_app(app_home, tmp_path):
    from kiro_crew.apps.manager import enable_app

    _write_agent_config(app_home, {"apps_allow_third_party": False})
    _install_market_record(tmp_path)
    _write_agent_config(
        app_home,
        {
            "apps_allow_third_party": False,
            "apps_trusted": [APP_NAME],
            "apps_trusted_local": [],
            "apps_trusted_repositories": {APP_NAME: f"market:{REF}"},
        },
    )

    result = enable_app(APP_NAME)

    assert result.ok, result.error

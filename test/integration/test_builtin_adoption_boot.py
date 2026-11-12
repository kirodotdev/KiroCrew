"""A former built-in moves onto its pinned repository across two real boots.

The real gateway boots in-process on a home holding a builtin-owned record of a
fixture app (``fixture-former``) shipped from a wheel root this test adds, with
data, a secret, approved grants and ``enabled=true``. The pin table is the
harness one (``KIROCREW_HARNESS_ADOPTION_PINS``, which ``integration_home``
points at ``tmp_path / "adoption-pins.json"``), and its one row names a
harness-built archive of the repository copy. Every digest check runs: the
archive against ``archiveSha256``, the copied tree against ``treeSha256``. No
git fetch runs and nothing leaves the machine.

The first boot STAGEs in the background after the listener serves; the restart
APPLYs before any route exists. Then the adopted app is driven through the
lifecycle the dashboard offers: its own backend route, disable, enable, update
and uninstall, and a last restart proves the uninstall sticks.

Owning spec: ``docs/system-specs/modules/app-kit-platform.md`` § Former built-in
adoption.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from builtin_adoption_helpers import (
    NAME,
    PACKAGE,
    make_archive,
    pin_row,
    repository_manifest,
    shipped_manifest,
    write_builtin_record,
    write_tree,
)

from kiro_crew.apps import builtin_adoption
from kiro_crew.apps import execution as exec_mod
from kiro_crew.apps import manager
from kiro_crew.apps.builtin_adoption_fetch import adoption_tree_digest
from kiro_crew.apps.builtin_adoption_pins import load_adoption_pins
from kiro_crew.apps.builtin_adoption_state import read_adoption_markers, read_ledger
from kiro_crew.dashboard.token_auth import read_app_secret

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

#: Any commit id: the archive path checks the archive and tree digests, not a ref.
COMMIT = "5" * 40

#: The repository copy's backend: a structural route (SDK level 1), no host import.
_ROUTES_PY = """from aiohttp import web


class _Route:
    def __init__(self, method, path, handler):
        self.method, self.path, self.handler = method, path, handler


async def _stored(request, ctx):
    text = (ctx.data_dir / "store.txt").read_text(encoding="utf-8")
    return web.json_response({"stored": text})


def register_routes(ctx):
    return [_Route("GET", "/stored", _stored)]
"""

_APPROVED_API = [f"/api/apps/{NAME}", f"/api/apps/{NAME}/*"]


def _write_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The wheel copy, the builtin-owned record and the harness pin to an archive."""
    wheel_root = tmp_path / "wheel"
    write_tree(wheel_root / PACKAGE, shipped_manifest())
    shipped = exec_mod._builtin_manifest_sources
    monkeypatch.setattr(
        exec_mod, "_builtin_manifest_sources", lambda: (*shipped(), wheel_root.resolve())
    )
    tree = write_tree(
        tmp_path / "repo-tree",
        repository_manifest(backend={"hooks": {"routes": "backend.routes:register_routes"}}),
        extra={"backend/__init__.py": "", "backend/routes.py": _ROUTES_PY},
    )
    archive = tmp_path / "fixture.tar.gz"
    archive.write_bytes(make_archive(tree, COMMIT))
    row = pin_row(
        COMMIT, adoption_tree_digest(tree), hashlib.sha256(archive.read_bytes()).hexdigest()
    )
    row["harnessArchive"] = str(archive)
    (tmp_path / "adoption-pins.json").write_text(
        json.dumps({"schemaVersion": 1, "pins": [row]}), encoding="utf-8"
    )
    load_adoption_pins.cache_clear()
    write_builtin_record(enabled=True, data="kept across the move", secret=b"keep-this")
    manager._write_approved_grants(NAME, {"api": _APPROVED_API, "events": []})


def _ledger_state() -> str:
    ledger = read_ledger(NAME)
    return ledger.state if ledger is not None else ""


async def _wait_staged() -> None:
    """Until the background STAGE pass has staged the pin."""
    from kiro_crew.testing.wait import async_wait_until

    await async_wait_until(
        lambda: _ledger_state() == "staged",
        timeout=60,
        off_loop=True,
        describe=lambda: f"ledger state {_ledger_state()!r}",
    )


async def _app_row(gw) -> dict:
    apps = await gw.get_json("/api/apps")
    rows = apps if isinstance(apps, list) else apps.get("apps", [])
    matching = [row for row in rows if row.get("name") == NAME]
    assert matching, f"{NAME} missing from /api/apps"
    return matching[0]


@pytest.mark.asyncio
async def test_a_pinned_built_in_stages_then_adopts_and_keeps_its_lifecycle(
    gateway_boot, tmp_path, monkeypatch
):
    from kiro_crew.apps.official_catalog import SKIP_FETCH_ENV

    # The store answers from the bundled seed, as for a harness subprocess gateway.
    monkeypatch.setenv(SKIP_FETCH_ENV, "1")
    _write_fixture(tmp_path, monkeypatch)
    home = manager.app_dir(NAME)

    async with gateway_boot() as gw:
        # STAGE runs in the background after the listener serves, and never
        # touches the record.
        await _wait_staged()
        assert manager.builtin_owns_installed(NAME)
        assert not builtin_adoption.builtin_released(NAME)

        # APPLY happens before any route is registered on the next start.
        await gw.restart()
        record = manager._read_installed(NAME)
        assert record is not None
        assert (record.origin, record.sourceCommit, record.enabled) == ("registry", COMMIT, True)
        assert builtin_adoption.builtin_released(NAME)
        assert read_adoption_markers()[NAME]["state"] == "adopted"
        assert (home / "data" / "store.txt").read_text(encoding="utf-8") == "kept across the move"
        # The adoption kept the folder secret, so the hook load on this start moved
        # it: the gateway holds a new secret and the folder copy is gone. An
        # adoption that lost the file would leave the app with no secret.
        assert not (home / ".app_secret").exists()
        assert read_app_secret(NAME) not in ("", "keep-this")
        grants = manager._read_approved_grants(NAME)
        assert grants is not None and sorted(grants["api"]) == sorted(_APPROVED_API)

        row = await _app_row(gw)
        assert row["enabled"] is True
        assert row["adoption"]["state"] == "adopted"
        assert row["adoption"]["legacyNav"]["route"] == f"/{NAME}"
        # The repository copy's own backend serves, over the data it inherited.
        assert await gw.get_json(f"/api/apps/{NAME}/stored") == {"stored": "kept across the move"}

        await gw.post_json(f"/api/apps/{NAME}/disable")
        assert (await _app_row(gw))["enabled"] is False
        await gw.post_json(f"/api/apps/{NAME}/enable")
        assert (await _app_row(gw))["enabled"] is True
        assert await gw.get_json(f"/api/apps/{NAME}/stored") == {"stored": "kept across the move"}

        # Update goes through the registry like any registry app, matched by the
        # adopted source. The store is held off the network, so the official
        # catalog cannot confirm that source: the update refuses with the outage
        # cause and changes nothing.
        update = await gw.post(f"/api/apps/{NAME}/update")
        body = await update.text()
        assert update.status == 400 and "could not be reached" in body, body
        assert "matches its recorded source" not in body, body
        after = manager._read_installed(NAME)
        assert after is not None and (after.sourceCommit, after.enabled) == (COMMIT, True)
        assert (home / "data" / "store.txt").read_text(encoding="utf-8") == "kept across the move"

        await gw.post_json(f"/api/apps/{NAME}/uninstall")
        assert manager._read_installed(NAME) is None

        # The uninstall sticks: the wheel copy is never registered again.
        await gw.restart()
        assert manager._read_installed(NAME) is None
        assert builtin_adoption.builtin_released(NAME)
        apps = await gw.get_json("/api/apps")
        rows = apps if isinstance(apps, list) else apps.get("apps", [])
        assert NAME not in {row.get("name") for row in rows}


@pytest.mark.asyncio
async def test_an_adopted_app_keeps_its_legacy_nav_after_the_pin_row_is_gone(
    gateway_boot, tmp_path, monkeypatch
):
    """The release that drops the pin row must not move the app's rail entry.

    The rail keys its hidden, order and pinned preferences by the nav id, which is
    the bare name only while the row carries ``legacyNav``; the gateway recorded it
    at adoption, so a build without the pin row still answers it.
    """
    from kiro_crew.apps.official_catalog import SKIP_FETCH_ENV

    monkeypatch.setenv(SKIP_FETCH_ENV, "1")
    _write_fixture(tmp_path, monkeypatch)

    async with gateway_boot() as gw:
        await _wait_staged()
        await gw.restart()
        pinned = (await _app_row(gw))["adoption"]
        assert pinned["state"] == "adopted"

        # The next release ships no row for the app.
        (tmp_path / "adoption-pins.json").write_text(
            json.dumps({"schemaVersion": 1, "pins": []}), encoding="utf-8"
        )
        load_adoption_pins.cache_clear()
        await gw.restart()
        assert builtin_adoption.builtin_released(NAME)
        unpinned = (await _app_row(gw))["adoption"]
        assert unpinned == pinned
        assert unpinned["legacyNav"] == pin_row(COMMIT, "", "")["legacyNav"]

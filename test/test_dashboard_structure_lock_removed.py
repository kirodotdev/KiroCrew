"""A crewmate's dashboard has no structure lock, and a lock file on disk changes nothing.

A crewmate changes its page only through its own tools and the person's Keep, and
every version stays on disk for a rollback. A ``crew-panels/<slug>.dashboard-lock``
file is not read and not rewritten: the page in force is ``instance.json``, every
structure change goes through, and the turn block says nothing about a lock.
"""

from __future__ import annotations

import json
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import agent_panel
from kiro_crew.dashboard.handlers import member_dashboard as routes
from kiro_crew.dashboard_templates import catalog, instance

SLUG = "fleet-conductor"
MEMBER = "Fleet Conductor"
PAGE = '<div><b data-dashboard-field="credits"></b><i data-dashboard-field="phase"></i></div>'
OTHER_PAGE = '<section><b data-dashboard-field="credits"></b></section>'
REPO = Path(__file__).resolve().parents[1]


def _manifest(**over: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "id": "fixture-board",
        "version": 1,
        "title": "Fixture board",
        "description": "A template these tests own.",
        "source": "builtin",
        "fields": {
            "credits": {"type": "number", "source": {"fold": "usage", "path": "credits"}},
            "phase": {"type": "string", "source": {"agentic": True}},
        },
    }
    raw.update(over)
    return raw


async def _none(*_a: Any, **_k: Any) -> None:
    return None


@pytest.fixture(autouse=True)
def _home(tmp_path, _floor_monkeypatch):
    """An isolated data home, two fixture built-ins, and the member checks stubbed."""
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    root = tmp_path / "builtin"
    other = _manifest(
        id="fixture-other",
        title="Fixture other",
        fields={"credits": {"type": "number", "source": {"fold": "usage", "path": "credits"}}},
    )
    for manifest, page in ((_manifest(), PAGE), (other, OTHER_PAGE)):
        directory = root / str(manifest["id"])
        directory.mkdir(parents=True)
        (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (directory / "template.html").write_text(page, encoding="utf-8")
    _floor_monkeypatch.setattr(catalog, "builtin_dir", lambda: root)

    cfg = SimpleNamespace(agents={MEMBER: SimpleNamespace(member_id="")})
    _floor_monkeypatch.setattr(routes.KiroCrewConfig, "load", staticmethod(lambda: cfg))
    _floor_monkeypatch.setattr(routes.members_mod, "member_slug", lambda name, config=None: SLUG)
    _floor_monkeypatch.setattr(routes.members_mod, "validate_slug", lambda slug: slug)
    _floor_monkeypatch.setattr(routes.members_mod, "is_dispatchable_member_name", bool)
    _floor_monkeypatch.setattr(
        routes.members_mod, "canonical_member_key", lambda name, cfg=None: name
    )
    _floor_monkeypatch.setattr(routes, "_member_names_for_slug", lambda cfg, slug: [MEMBER])
    _floor_monkeypatch.setattr(routes, "_deny_app_caller", _none)
    _floor_monkeypatch.setattr(routes, "_owner_only", _none)
    _floor_monkeypatch.setattr(routes, "_write_session", lambda slug, member: "")
    yield


def _legacy_lock_path() -> Path:
    """Where the removed setter wrote the lock: ``crew-panels/<slug>.dashboard-lock``."""
    return agent_panel.panel_dir() / f"{SLUG}.dashboard-lock"


def _leave_legacy_lock(record: Any = None, *, text: str | None = None) -> Path:
    """Write a lock file exactly as the removed ``set_structure_lock`` did."""
    path = _legacy_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if text is None:
        text = json.dumps({"slug": SLUG, "at_ms": 1, "record": record})
    path.write_text(text, encoding="utf-8")
    return path


def _record() -> dict[str, Any]:
    return json.loads((instance.instance_dir(SLUG) / "instance.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------------ the store


class TestALeftoverLockIsIgnored:
    def test_the_page_is_unchanged_and_reads_as_the_record(self) -> None:
        instance.adopt(SLUG, "fixture-board")
        before = instance.read(SLUG)
        lock = _leave_legacy_lock(_record())
        after = instance.read(SLUG)
        assert (after.instance_version, after.template_id, after.html) == (
            before.instance_version,
            before.template_id,
            before.html,
        )
        assert "structure_locked" not in after.wire()
        assert lock.read_text(encoding="utf-8"), "the lock file is not rewritten or removed"

    def test_every_structure_change_goes_through(self) -> None:
        instance.adopt(SLUG, "fixture-board")
        _leave_legacy_lock(_record())
        assert instance.adopt(SLUG, "fixture-other").html == OTHER_PAGE
        assert instance.rollback(SLUG, 1).html == PAGE
        assert instance.edit(SLUG, manifest=_manifest(title="Renamed")).instance_version == 4
        instance.stage_preview(SLUG, template_id="fixture-other")
        assert instance.apply_preview(SLUG).html == OTHER_PAGE

    def test_a_package_apply_goes_through(self) -> None:
        instance.adopt(SLUG, "fixture-board")
        _leave_legacy_lock(_record())
        applied = instance.apply_package(
            SLUG, package_slug="page", package_version=1, content='{"kind": "dashboard"}'
        )
        assert (applied.package_slug, applied.package_version) == ("page", 1)
        assert instance.read(SLUG).package_slug == "page"

    def test_an_unreadable_lock_file_does_not_withhold_the_page(self) -> None:
        """The old reader served the error state for a damaged lock; nothing reads it now."""
        instance.adopt(SLUG, "fixture-board")
        _leave_legacy_lock(text="{not json")
        current = instance.read(SLUG)
        assert (current.state, current.html) == (instance.STATE_LIVE, PAGE)

    def test_the_record_wins_over_the_snapshot_the_lock_held(self) -> None:
        """The lock served its snapshot; now the record is the page, and rollback undoes."""
        instance.adopt(SLUG, "fixture-board")
        snapshot = _record()
        instance.adopt(SLUG, "fixture-other")
        _leave_legacy_lock(snapshot)
        assert instance.read(SLUG).html == OTHER_PAGE
        assert instance.rollback(SLUG, 1).html == PAGE

    def test_an_older_record_keeps_its_older_package_rule(self) -> None:
        """A record with no ``package`` key looks a bound package up, lock file or not."""
        instance.adopt(SLUG, "fixture-board")
        path = instance.instance_dir(SLUG) / "instance.json"
        raw = _record()
        raw.pop("package", None)
        path.write_text(json.dumps(raw), encoding="utf-8")
        _leave_legacy_lock(raw)
        assert instance.keeps_older_package_rule(instance.read(SLUG)) is True


class TestTheCrewmateIsToldNothingAboutALock:
    def test_an_adopted_page_has_no_locked_line(self) -> None:
        from kiro_crew import dashboard_agentic

        instance.adopt(SLUG, "fixture-board")
        _leave_legacy_lock(_record())
        block = dashboard_agentic.turn_block(SLUG)
        assert block.startswith("[DASHBOARD]\n"), block
        assert "LOCKED" not in block
        assert "template fixture-board v1" in block

    def test_a_crewmate_with_no_page_is_asked_to_build_one(self, _floor_monkeypatch) -> None:
        from kiro_crew import dashboard_agentic

        _floor_monkeypatch.setattr(dashboard_agentic, "_crew_panel_on", lambda: True)
        _leave_legacy_lock(None)
        assert dashboard_agentic.turn_block(SLUG) == "[DASHBOARD]\n" + (
            dashboard_agentic.SUGGESTIONS_LINE
        )


# ------------------------------------------------------------ the routes


@asynccontextmanager
async def _client():
    app = web.Application()
    routes.register_member_dashboard_routes(app)
    c = TestClient(TestServer(app))
    await c.start_server()
    try:
        yield c
    finally:
        await c.close()


QUERY = f"?member={MEMBER.replace(' ', '+')}"


@pytest.mark.asyncio
async def test_the_lock_route_is_gone() -> None:
    async with _client() as client:
        resp = await client.post(
            f"/api/members/{SLUG}/dashboard/lock{QUERY}", json={"locked": True}
        )
        assert resp.status in (404, 405)
    assert not _legacy_lock_path().exists()


@pytest.mark.asyncio
async def test_the_read_carries_no_lock_and_serves_the_record() -> None:
    instance.adopt(SLUG, "fixture-board")
    _leave_legacy_lock(_record())
    instance.adopt(SLUG, "fixture-other")
    async with _client() as client:
        body = await (await client.get(f"/api/members/{SLUG}/dashboard{QUERY}")).json()
    assert "structure_locked" not in body
    assert body["instance_version"] == 2


def test_no_product_source_names_the_lock() -> None:
    """A source scan, because the property is an ABSENCE no route test exercises."""
    out = subprocess.run(
        [
            "git",
            "grep",
            "-l",
            "-E",
            r"set_structure_lock|LOCKED_REFUSAL|InstanceLocked|dashboard_locked|/dashboard/lock"
            r"|structure_locked|memberDashboardLock",
            "--",
            "src",
            "website/src",
            # A website test may feed an older gateway's body to prove it is ignored.
            ":(exclude,glob)website/src/**/*.test.*",
        ],
        cwd=REPO,
        capture_output=True,
        encoding="utf-8",
    ).stdout.split()
    assert out == []

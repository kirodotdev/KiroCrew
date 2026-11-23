"""``member_dashboard.package_for_reader``: which package a reader of a crewmate's page gets.

The selection ``_serve`` makes, as one helper every reader shares. The instance record
says which page is in force: a pinned package is used, a staged page never is, the older
binding rule applies only to a record written before packages were pinned, and a binding
that cannot be proved gives no package.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from kiro_crew import dashboard_agentic
from kiro_crew.dashboard.handlers import member_dashboard as md
from kiro_crew.dashboard_templates import catalog, instance

SLUG = "fleet-conductor"
CREW = "Fleet Conductor"
PAGE = '<div><b data-dashboard-field="credits"></b></div>'


@pytest.fixture(autouse=True)
def _env(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    manifest = {
        "id": "fixture-board",
        "version": 1,
        "title": "Fixture board",
        "description": "A template these tests own.",
        "source": "builtin",
        "fields": {"credits": {"type": "number", "source": {"fold": "usage", "path": "credits"}}},
    }
    builtin = tmp_path / "builtin" / "fixture-board"
    builtin.mkdir(parents=True)
    (builtin / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (builtin / "template.html").write_text(PAGE, encoding="utf-8")
    _floor_monkeypatch.setattr(catalog, "builtin_dir", lambda: builtin.parent)


@pytest.fixture
def binding(monkeypatch) -> dict[str, Any]:
    """The crewmate's binding, answered without an artifact library; counts lookups."""
    seen: dict[str, Any] = {"slug": "bound-pkg", "cut": True, "lookups": 0}

    def _bound(crewmate: str) -> str:
        seen["lookups"] += 1
        return str(seen["slug"])

    monkeypatch.setattr("kiro_crew.members.member_slug", lambda name, config=None: SLUG)
    monkeypatch.setattr(dashboard_agentic, "bound_package", _bound)
    monkeypatch.setattr(dashboard_agentic, "has_person_cut_version", lambda _s: seen["cut"])
    return seen


def _pin() -> None:
    instance.adopt(SLUG, "fixture-board")
    instance.apply_package(
        SLUG, package_slug="pinned-pkg", package_version=1, content="<p>page</p>"
    )


@pytest.mark.asyncio
async def test_the_pinned_package_is_used(binding):
    _pin()
    slug, pinned = await md.package_for_reader(SLUG, CREW)
    assert slug == "pinned-pkg"
    assert pinned is not None and (pinned.slug, pinned.version) == ("pinned-pkg", 1)
    # A pinned record never asks the binding.
    assert binding["lookups"] == 0


@pytest.mark.asyncio
async def test_the_pin_wins_over_a_named_package(binding):
    _pin()
    slug, pinned = await md.package_for_reader(SLUG, CREW, named="other-pkg")
    assert (slug, pinned is not None) == ("pinned-pkg", True)


@pytest.mark.asyncio
async def test_a_staged_page_is_never_the_package(binding):
    _pin()
    instance.stage_preview(SLUG, template_id="fixture-board")
    assert instance.staged_preview(SLUG) is not None
    slug, pinned = await md.package_for_reader(SLUG, CREW)
    # The applied page, with the bytes the apply pinned: staging moved nothing.
    assert slug == "pinned-pkg"
    assert pinned is not None and pinned.content == "<p>page</p>"
    assert binding["lookups"] == 0


@pytest.mark.asyncio
async def test_a_released_record_gives_no_package_and_skips_the_binding(binding):
    _pin()
    instance.adopt(SLUG, "fixture-board")  # back to the template: releases the package
    assert await md.package_for_reader(SLUG, CREW) == ("", None)
    assert await md.package_for_reader(SLUG, CREW, named="other-pkg") == ("", None)
    assert binding["lookups"] == 0


@pytest.mark.asyncio
async def test_an_older_record_keeps_the_binding_rule(binding):
    # No record at all is the oldest record: it has no `package` key.
    assert await md.package_for_reader(SLUG, CREW) == ("bound-pkg", None)
    assert binding["lookups"] == 1


@pytest.mark.asyncio
async def test_an_older_record_ignores_a_bound_draft(binding):
    binding["cut"] = False
    assert await md.package_for_reader(SLUG, CREW) == ("", None)


@pytest.mark.asyncio
async def test_an_older_record_serves_a_named_package_without_the_binding(binding):
    assert await md.package_for_reader(SLUG, CREW, named="asked-pkg") == ("asked-pkg", None)
    assert binding["lookups"] == 0


@pytest.mark.asyncio
async def test_an_unprovable_binding_gives_no_package(monkeypatch):
    from kiro_crew.artifact_store import dashboard_package

    asked: list[str] = []

    def _unproven(binding: str) -> str:
        asked.append(binding)
        raise dashboard_package.BindingAmbiguous("two packages claim this crewmate")

    monkeypatch.setattr("kiro_crew.members.member_slug", lambda name, config=None: SLUG)
    monkeypatch.setattr(dashboard_package, "resolve_bound_slug", _unproven)
    assert await md.package_for_reader(SLUG, CREW) == ("", None)
    # The lookup really ran and was refused: the empty answer is the refusal's.
    assert asked == [f"crewmate:{SLUG}"]


@pytest.mark.asyncio
async def test_an_unreadable_record_raises_for_the_caller(monkeypatch, binding):
    def _broken(_slug: str) -> Any:
        raise instance.InstanceRefused("unreadable record")

    monkeypatch.setattr(md, "_package_in_force", _broken)
    with pytest.raises(instance.InstanceRefused):
        await md.package_for_reader(SLUG, CREW)

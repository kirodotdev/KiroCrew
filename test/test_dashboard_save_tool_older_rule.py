"""The older-record rule and the save's own races, for `dashboard_save`.

Split from ``test_dashboard_save_tool.py`` to keep that file under the line cap. The
helpers are that file's; these tests drive the same routes with a real artifact store.
"""

from __future__ import annotations

import asyncio
import json
import threading

import pytest
from test_dashboard_save_tool import SLUG, _client, _flip, _instance, _package_body, _save

from kiro_crew.artifact_store.dashboard_package import DASHBOARD_KIND
from kiro_crew.dashboard.handlers import agent_panel as routes

pytestmark = pytest.mark.asyncio


def test_the_write_check_refuses_a_package_replaced_between_its_two_reads(monkeypatch, tmp_path):
    """An older record whose person-cut version is not the current one is read twice.
    A slug deleted and saved again in between answers with another record's version, so
    the write check names no Model rather than checking a write against that one."""
    import dataclasses

    from kiro_crew import artifacts
    from kiro_crew import dashboard_package as reader
    from kiro_crew.artifacts import ArtifactStore

    real = ArtifactStore(tmp_path / "artifacts")
    monkeypatch.setattr(artifacts, "get_default_store", lambda: real)
    pkg = {**_package_body(), "kind": DASHBOARD_KIND, "bound_to": f"crewmate:{SLUG}"}
    art = real.create(name="page", content=json.dumps(pkg), kind=DASHBOARD_KIND)
    _flip(pkg)
    read = real.get(art.slug, assign_token=True)
    real.update(art.slug, content=json.dumps(pkg), actor="user", snapshot_token=read.content_token)
    _flip(pkg)
    read = real.get(art.slug, assign_token=True)
    real.update(art.slug, content=json.dumps(pkg), actor="agent", snapshot_token=read.content_token)
    # The control: the person's version is not the current one, so a second read happens.
    assert reader.read_package_model(SLUG).from_package is True

    plain_get = real.get

    def _recreated(slug, *args, **kwargs):
        got = plain_get(slug, *args, **kwargs)
        if kwargs.get("version") is not None:
            return dataclasses.replace(got, created_at="2000-01-01T00:00:00Z")
        return got

    monkeypatch.setattr(real, "get", _recreated)
    refused = reader.read_package_model(SLUG)
    assert refused.from_package is False
    assert refused.state == reader.STATE_ERROR


async def test_two_first_saves_at_once_bind_one_package(monkeypatch, tmp_path):
    """Two first saves for one crewmate, both past the lookup before either writes.

    Without the save lock both find no package and both create one, so the binding is
    ambiguous and every later save and apply is refused until a person deletes one.
    """
    from kiro_crew import artifacts, dashboard_agentic
    from kiro_crew.artifacts import ArtifactStore

    real = ArtifactStore(tmp_path / "artifacts")
    monkeypatch.setattr(artifacts, "get_default_store", lambda: real)
    monkeypatch.setattr(routes, "_dashboard_artifact_store", lambda: real)
    lookup = routes._dashboard.resolve_bound_slug
    both_looked = threading.Barrier(2, timeout=1.0)

    def _slow_lookup(*args, **kwargs):
        answer = lookup(*args, **kwargs)
        try:
            # Hold the first lookup until a second one arrives. Under the lock no
            # second one can, so the barrier times out and the save goes on alone.
            both_looked.wait()
        except threading.BrokenBarrierError:
            pass
        return answer

    monkeypatch.setattr(routes._dashboard, "resolve_bound_slug", _slow_lookup)
    async with _client(monkeypatch) as client:
        first, second = await asyncio.gather(
            _save(client, _package_body()), _save(client, _package_body())
        )
        assert first.status == 200, await first.text()
        assert second.status == 200, await second.text()

    packages = [a for a in real.list() if getattr(a, "kind", "") == DASHBOARD_KIND]
    assert len(packages) == 1, [a.slug for a in packages]
    assert dashboard_agentic.bound_package(SLUG) == packages[0].slug


@pytest.mark.parametrize("adopted", [True, False], ids=["older-record", "never-adopted"])
async def test_a_save_without_an_apply_leaves_an_older_crewmate_on_its_template(
    monkeypatch, tmp_path, adopted
):
    """The upgrade case: a crewmate whose record predates pins saves, and does not apply.

    The save binds the package, and the older rule looks a package up by that binding.
    The draft has no version a person cut, so the page, the turn block and the write
    check must all still name the template page -- not a blank tab, and not a page the
    person cannot see.
    """
    from kiro_crew import artifacts, dashboard_agentic
    from kiro_crew import dashboard_package as reader
    from kiro_crew.artifacts import ArtifactStore

    instance_store = _instance()
    real = ArtifactStore(tmp_path / "artifacts")
    monkeypatch.setattr(artifacts, "get_default_store", lambda: real)
    monkeypatch.setattr(routes, "_dashboard_artifact_store", lambda: real)
    # The turn block also syncs the page's helper scripts; that is not under test here.
    from kiro_crew import dashboard_upkeep

    monkeypatch.setattr(dashboard_upkeep, "sync", lambda *_a, **_k: None)
    if adopted:
        instance_store.adopt(SLUG, "project-report")
        # Written before the `package` key existed.
        path = instance_store.instance_dir(SLUG) / "instance.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw.pop("package")
        path.write_text(json.dumps(raw), encoding="utf-8")
    assert instance_store.keeps_older_package_rule(instance_store.read(SLUG))

    async with _client(monkeypatch) as client:
        saved = await _save(client, _package_body())
        assert saved.status == 200, await saved.text()
    # The control: the save really bound a package, so the older rule finds it.
    bound = dashboard_agentic.bound_package(SLUG)
    assert bound, "the save bound nothing, so this test would prove nothing"

    assert dashboard_agentic.package_on_screen(SLUG) == ""
    write = reader.read_package_model(SLUG)
    assert write.from_package is False
    block = dashboard_agentic.turn_block(SLUG)
    assert "the page you saved" not in block
    if adopted:
        assert write.model is not None and write.model.manifest.id == "project-report"
        assert "template project-report" in block
    else:
        assert dashboard_agentic.SUGGESTIONS_LINE in block

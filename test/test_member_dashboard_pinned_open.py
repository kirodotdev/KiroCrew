"""A member's dashboard page is opened without following links, as a session's page is.

Every level of ``members/<slug>/dashboard/versions`` is refused when it is a link, by
every store entry point, and nothing lands in the folder the link points at. The same
holds where the platform cannot pin a directory (the ``lstat`` walk), and when a level
is replaced after the page folder was opened.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

import pytest
from test_windows_read_retry_sites import PERSISTENT, RIDDEN_OUT, held_open

from conftest import make_dir_link, requires_symlinks
from kiro_crew import atomic_write as aw
from kiro_crew.config.paths import data_home
from kiro_crew.dashboard_templates import catalog, instance

SLUG = "fleet-conductor"
PAGE = '<div><b data-dashboard-field="credits"></b></div>'
LEVELS = ("members", "slug", "dashboard", "versions")
ENTRIES = ("adopt", "apply_package", "stage_preview", "rollback")

needs_pinned_walk = pytest.mark.skipif(
    not instance.pinned_fs.supports_pinned_walk(), reason="needs descriptor-relative opens"
)


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


def _level_path(level: str) -> Path:
    base = data_home() / "members"
    return {
        "members": base,
        "slug": base / SLUG,
        "dashboard": base / SLUG / "dashboard",
        "versions": base / SLUG / "dashboard" / "versions",
    }[level]


def _call(entry: str) -> Callable[[], Any]:
    calls: dict[str, Callable[[], Any]] = {
        "adopt": lambda: instance.adopt(SLUG, "fixture-board"),
        "apply_package": lambda: instance.apply_package(
            SLUG, package_slug="pkg", package_version=1, content="<p>page</p>"
        ),
        "stage_preview": lambda: instance.stage_preview(SLUG, template_id="fixture-board"),
        "rollback": lambda: instance.rollback(SLUG, 1),
    }
    return calls[entry]


def _seed() -> None:
    instance.adopt(SLUG, "fixture-board")
    instance.adopt(SLUG, "fixture-board")


def _outside(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.json").write_text('{"keep": true}', encoding="utf-8")
    return outside


def _snapshot(root: Path) -> dict[str, bytes | None]:
    return {
        str(p.relative_to(root)): (p.read_bytes() if p.is_file() else None)
        for p in sorted(root.rglob("*"))
    }


def _link(level: str, outside: Path) -> None:
    path = _level_path(level)
    path.rename(path.with_name(path.name + ".moved"))
    make_dir_link(path, outside)


def _assert_refused(call: Callable[[], Any], outside: Path) -> None:
    before = _snapshot(outside)
    with pytest.raises(instance.InstanceError) as raised:
        call()
    assert not isinstance(raised.value, instance.InstanceRefused), "not refused for the link"
    assert _snapshot(outside) == before


@needs_pinned_walk
@pytest.mark.parametrize("entry", ENTRIES)
@pytest.mark.parametrize("level", LEVELS)
def test_member_dashboard_refuses_linked_level(tmp_path, level, entry):
    _seed()
    outside = _outside(tmp_path)
    _link(level, outside)
    _assert_refused(_call(entry), outside)


@pytest.mark.parametrize("entry", ENTRIES)
@pytest.mark.parametrize("level", LEVELS)
def test_member_dashboard_refuses_linked_level_without_pinning(tmp_path, monkeypatch, level, entry):
    monkeypatch.setattr(instance.pinned_fs, "supports_pinned_walk", lambda: False)
    _seed()
    outside = _outside(tmp_path)
    _link(level, outside)
    _assert_refused(_call(entry), outside)


@needs_pinned_walk
@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("owner_route", ["apply_preview", "discard_shown_preview"])
def test_member_dashboard_owner_routes_refuse_linked_level(tmp_path, level, owner_route):
    _seed()
    staged = instance.stage_preview(SLUG, template_id="fixture-board")
    outside = _outside(tmp_path)
    _link(level, outside)
    if owner_route == "apply_preview":
        _assert_refused(lambda: instance.apply_preview(SLUG), outside)
    else:
        _assert_refused(
            lambda: instance.discard_shown_preview(SLUG, expected_staged_ms=staged.staged_ms),
            outside,
        )


@pytest.mark.parametrize("pinned", [True, False])
def test_member_dashboard_without_a_link_still_works(monkeypatch, pinned):
    if pinned and not instance.pinned_fs.supports_pinned_walk():
        pytest.skip("needs descriptor-relative opens")
    if not pinned:
        monkeypatch.setattr(instance.pinned_fs, "supports_pinned_walk", lambda: False)
    _seed()
    instance.apply_package(SLUG, package_slug="pkg", package_version=1, content="<p>page</p>")
    instance.stage_preview(SLUG, template_id="fixture-board")
    instance.apply_preview(SLUG)
    record = instance.rollback(SLUG, 1)
    assert record.instance_version == 5
    assert instance.versions(SLUG) == (1, 2, 3, 4, 5)
    assert not (_level_path("dashboard") / "preview.json").exists()


@needs_pinned_walk
@pytest.mark.parametrize("replaced", ["versions", "dashboard"])
def test_member_dashboard_write_stays_in_opened_folder_when_a_level_is_replaced(
    tmp_path, monkeypatch, replaced
):
    """The swap lands after the page folder is opened and before the version is written."""
    _seed()
    outside = _outside(tmp_path)
    before = _snapshot(outside)
    real = instance._read_record
    swapped: list[bool] = []

    def read_then_swap(slug: str) -> Any:
        found = real(slug)
        if not swapped:
            _link(replaced, outside)
            swapped.append(True)
        return found

    monkeypatch.setattr(instance, "_read_record", read_then_swap)
    try:
        instance.adopt(SLUG, "fixture-board")
    except instance.InstanceError:
        pass
    assert swapped, "the hook never ran, so the race was not exercised"
    assert _snapshot(outside) == before
    assert os.path.isdir(str(_level_path(replaced)) + ".moved")


def _replace_level(level: str, kind: str, outside: Path) -> Path:
    """Put a link to *outside*, or a fresh empty folder, where the level was."""
    path = _level_path(level)
    path.rename(path.with_name(path.name + ".moved"))
    if kind == "link":
        make_dir_link(path, outside)
        return outside
    path.mkdir()
    return path


@needs_pinned_walk
@pytest.mark.parametrize("entry", ["adopt", "rollback"])
@pytest.mark.parametrize("kind", ["link", "folder"])
@pytest.mark.parametrize("level", ["members", "slug", "dashboard"])
def test_member_dashboard_kept_versions_survive_a_replaced_folder(
    tmp_path, monkeypatch, level, kind, entry
):
    """A read under the lock comes from the folder that was opened, so nothing is rewound."""
    _seed()
    opened = _level_path("dashboard")
    kept = {n: (opened / "versions" / n).read_bytes() for n in ("1.json", "2.json")}
    outside = _outside(tmp_path)
    real = instance._read_record
    replaced: list[tuple[Path, dict[str, bytes | None]]] = []

    def replace_then_read(slug: str) -> Any:
        if not replaced:
            target = _replace_level(level, kind, outside)
            replaced.append((target, _snapshot(target)))
        return real(slug)

    monkeypatch.setattr(instance, "_read_record", replace_then_read)
    result = _call(entry)()
    assert replaced, "the hook never ran, so the race was not exercised"
    moved = _level_path(level)
    held = moved.with_name(moved.name + ".moved")
    held = held.joinpath(*_level_path("dashboard").relative_to(moved).parts)
    assert {n: (held / "versions" / n).read_bytes() for n in kept} == kept
    record = json.loads((held / "instance.json").read_text(encoding="utf-8"))
    assert record["instance_version"] == result.instance_version == 3
    assert (held / "versions" / "3.json").is_file()
    target, before = replaced[0]
    assert _snapshot(target) == before


@needs_pinned_walk
def test_member_dashboard_read_inside_the_lock_rides_out_a_sharing_violation():
    """A read under the lock keeps the retry and the refusal the by-name read has."""
    _seed()
    with instance._locked(instance.instance_dir(SLUG)):
        with held_open(_level_path("versions") / "1.json", RIDDEN_OUT) as reads:
            assert instance._read_version(SLUG, 1)["instance_version"] == 1
        assert reads["n"] == RIDDEN_OUT + 1
        with held_open(_level_path("versions") / "1.json", PERSISTENT) as reads:
            with pytest.raises(instance.InstanceError, match="cannot be read"):
                instance._read_version(SLUG, 1)
        assert reads["n"] == aw._REPLACE_MAX_ATTEMPTS


def _swap_dashboard_for_folder() -> Path:
    """Move the page folder aside and put a fresh folder with the same files where it was."""
    path = _level_path("dashboard")
    moved = path.with_name(path.name + ".moved")
    path.rename(moved)
    path.mkdir()
    return moved


@needs_pinned_walk
def test_member_dashboard_listing_inside_the_lock_comes_from_the_opened_folder():
    _seed()
    with instance._locked(instance.instance_dir(SLUG)):
        _swap_dashboard_for_folder()
        assert instance.versions(SLUG) == (1, 2)


@needs_pinned_walk
def test_member_dashboard_staged_page_is_dropped_from_the_opened_folder():
    _seed()
    instance.stage_preview(SLUG, template_id="fixture-board")
    with instance._locked(instance.instance_dir(SLUG)):
        moved = _swap_dashboard_for_folder()
        elsewhere = _level_path("dashboard") / "preview.json"
        elsewhere.write_text("{}", encoding="utf-8")
        instance.discard_preview(SLUG)
        assert not (moved / "preview.json").exists()
        assert elsewhere.exists()


@requires_symlinks
@needs_pinned_walk
def test_member_dashboard_refuses_a_linked_version_file(tmp_path):
    _seed()
    outside = _outside(tmp_path)
    kept = _level_path("versions") / "1.json"
    (outside / "other.json").write_bytes(kept.read_bytes())
    kept.unlink()
    kept.symlink_to(outside / "other.json")
    with instance._locked(instance.instance_dir(SLUG)):
        with pytest.raises(instance.InstanceError, match="link"):
            instance._read_version(SLUG, 1)


def test_member_dashboard_without_pinning_refuses_a_versions_folder_linked_late(
    tmp_path, monkeypatch
):
    """The check after the folder is made catches a link put there in between."""
    monkeypatch.setattr(instance.pinned_fs, "supports_pinned_walk", lambda: False)
    _seed()
    outside = _outside(tmp_path)
    before = _snapshot(outside)
    real = instance.mkdirs_owner_only

    def make_then_link(path: Any, *args: Any, **kwargs: Any) -> Any:
        found = real(path, *args, **kwargs)
        if Path(path) == _level_path("versions"):
            _link("versions", outside)
        return found

    monkeypatch.setattr(instance, "mkdirs_owner_only", make_then_link)
    with pytest.raises(instance.InstanceError):
        instance.adopt(SLUG, "fixture-board")
    assert _snapshot(outside) == before


@needs_pinned_walk
def test_member_dashboard_refusal_closes_the_opened_folder(tmp_path, monkeypatch):
    _seed()
    _link("versions", _outside(tmp_path))
    events: list[tuple[str, int]] = []
    real_open = instance._pinned_session_dir
    real_close = os.close

    def note_open(directory: Path) -> int | None:
        fd = real_open(directory)
        if fd is not None:
            events.append(("open", fd))
        return fd

    def note_close(fd: int) -> None:
        events.append(("close", fd))
        real_close(fd)

    with monkeypatch.context() as mp:
        mp.setattr(instance, "_pinned_session_dir", note_open)
        mp.setattr(os, "close", note_close)
        with pytest.raises(instance.InstanceError):
            instance.adopt(SLUG, "fixture-board")
    (_, held), *_ = [e for e in events if e[0] == "open"]
    assert [e for e in events if e[1] == held][-1] == ("close", held)

"""A member's dashboard page is read without following links, as it is written.

Every reader of ``members/<slug>/dashboard`` that does not hold the page lock answers
"not set" -- what it answers for a page nobody adopted -- when a level (``members``,
the slug, ``dashboard``, ``versions``) is a link, and the folder the link points at is
left byte for byte as it was. The same holds where the platform cannot pin a directory
(the ``lstat`` walk), and when a level is replaced after the page folder was opened.
A page with no link, and a page that is not there yet, answer as before and nothing is
created by the read. A link at a file of the page folder (the record, the history, the
staged page, a kept version) answers the same; a record that cannot be read for any other
reason is refused as it always was.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Callable

import pytest
from test_windows_read_retry_sites import PERSISTENT, RIDDEN_OUT, held_open

from conftest import make_dir_link, requires_symlinks
from kiro_crew import atomic_write as aw
from kiro_crew.config.paths import data_home
from kiro_crew.dashboard.handlers import agent_panel_snapshot, member_export
from kiro_crew.dashboard_templates import catalog, instance

SLUG = "fleet-conductor"
PAGE = '<div><b data-dashboard-field="credits"></b></div>'
LEVELS = ("members", "slug", "dashboard", "versions")
READERS = ("read", "versions", "history", "staged_preview", "pinned_package")
PAGE_LEVELS = ("members", "slug", "dashboard")
#: The ``versions`` folder is only read to list versions; every other reader stays in the page folder.
CASES = [(r, lv) for r in READERS for lv in (LEVELS if r == "versions" else PAGE_LEVELS)]

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


def _seed() -> None:
    """A page with two versions, a package in force, a staged page and a history."""
    instance.adopt(SLUG, "fixture-board")
    instance.adopt(SLUG, "fixture-board")
    instance.apply_package(SLUG, package_slug="pkg", package_version=1, content="<p>page</p>")
    instance.stage_preview(SLUG, template_id="fixture-board")


def _answer(reader: str) -> Any:
    """What one reader says, reduced to a comparable value."""
    if reader == "read":
        record = instance.read(SLUG)
        return (record.state, record.instance_version, record.state_reason)
    if reader == "versions":
        return instance.versions(SLUG)
    if reader == "history":
        return tuple(row["instance_version"] for row in instance.history(SLUG))
    if reader == "staged_preview":
        preview = instance.staged_preview(SLUG)
        return None if preview is None else preview.staged_ms
    pinned = instance.pinned_package(instance.read(SLUG))
    return None if pinned is None else (pinned.slug, pinned.version, pinned.content)


def _not_set(reader: str) -> Any:
    return {
        "read": (instance.STATE_EMPTY, 0, "no template adopted yet"),
        "versions": (),
        "history": (),
        "staged_preview": None,
        "pinned_package": None,
    }[reader]


def _outside_copy(tmp_path: Path, level: str) -> Path:
    """A folder shaped like *level*, holding another page's record, versions, history and preview."""
    outside = tmp_path / "outside"
    shutil.copytree(_level_path(level), outside)
    if level == "versions":
        shutil.copy(outside / "1.json", outside / "99.json")
        return outside
    page = {
        "members": outside / SLUG / "dashboard",
        "slug": outside / "dashboard",
        "dashboard": outside,
    }[level]
    record = json.loads((page / "instance.json").read_text(encoding="utf-8"))
    record["instance_version"] = 99
    (page / "instance.json").write_text(json.dumps(record), encoding="utf-8")
    shutil.copy(page / "versions" / "1.json", page / "versions" / "99.json")
    history = json.loads((page / "history.json").read_text(encoding="utf-8"))
    history["rows"].append(dict(history["rows"][-1], instance_version=99))
    (page / "history.json").write_text(json.dumps(history), encoding="utf-8")
    staged = json.loads((page / "preview.json").read_text(encoding="utf-8"))
    staged["staged_ms"] += 1000
    (page / "preview.json").write_text(json.dumps(staged), encoding="utf-8")
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


def _no_pinning(monkeypatch) -> None:
    monkeypatch.setattr(instance.pinned_fs, "supports_pinned_walk", lambda: False)


@needs_pinned_walk
@pytest.mark.parametrize(("reader", "level"), CASES)
def test_member_dashboard_reads_a_linked_level_as_not_set(tmp_path, level, reader):
    _seed()
    outside = _outside_copy(tmp_path, level)
    before = _snapshot(tmp_path / "outside")
    _link(level, outside)
    assert _answer(reader) == _not_set(reader)
    assert _snapshot(tmp_path / "outside") == before


@pytest.mark.parametrize(("reader", "level"), CASES)
def test_member_dashboard_reads_a_linked_level_as_not_set_without_pinning(
    tmp_path, monkeypatch, level, reader
):
    _seed()
    outside = _outside_copy(tmp_path, level)
    before = _snapshot(tmp_path / "outside")
    _link(level, outside)
    _no_pinning(monkeypatch)
    assert _answer(reader) == _not_set(reader)
    assert _snapshot(tmp_path / "outside") == before


@pytest.mark.parametrize("pinned", [True, False])
@pytest.mark.parametrize("reader", READERS)
def test_member_dashboard_without_a_link_reads_as_before(monkeypatch, pinned, reader):
    if pinned and not instance.pinned_fs.supports_pinned_walk():
        pytest.skip("needs descriptor-relative opens")
    if not pinned:
        _no_pinning(monkeypatch)
    _seed()
    answer = _answer(reader)
    assert answer != _not_set(reader)
    if reader == "versions":
        assert answer == (1, 2, 3)
    if reader == "read":
        assert answer[1] == 3


def _swap_before(monkeypatch, level: str, outside: Path, *targets: tuple[Any, str]) -> list[bool]:
    """Replace *level* with a link to *outside* the first time any of *targets* is called."""
    swapped: list[bool] = []

    def hook(real: Callable[..., Any]) -> Callable[..., Any]:
        def swap_then_call(*args: Any, **kwargs: Any) -> Any:
            if not swapped:
                swapped.append(True)
                _link(level, outside)
            return real(*args, **kwargs)

        return swap_then_call

    for owner, name in targets:
        monkeypatch.setattr(owner, name, hook(getattr(owner, name)))
    return swapped


@needs_pinned_walk
@pytest.mark.parametrize("reader", ["read", "history", "staged_preview"])
@pytest.mark.parametrize("level", PAGE_LEVELS)
def test_member_dashboard_read_never_returns_a_replaced_level(tmp_path, monkeypatch, level, reader):
    """The swap lands after the page folder is opened and before the file is read."""
    _seed()
    held = _answer(reader)
    outside = _outside_copy(tmp_path, level)
    before = _snapshot(tmp_path / "outside")
    swapped = _swap_before(
        monkeypatch,
        level,
        outside,
        (instance, "read_bytes_with_retry"),
        (instance, "_read_once"),
    )
    answer = _answer(reader)
    assert swapped, "the hook never ran, so the race was not exercised"
    assert answer in (held, _not_set(reader))
    assert _snapshot(tmp_path / "outside") == before


@needs_pinned_walk
@pytest.mark.parametrize("level", [*PAGE_LEVELS, "versions"])
def test_member_dashboard_versions_never_list_a_replaced_level(tmp_path, monkeypatch, level):
    _seed()
    outside = _outside_copy(tmp_path, level)
    before = _snapshot(tmp_path / "outside")
    swapped = _swap_before(monkeypatch, level, outside, (os, "listdir"))
    answer = instance.versions(SLUG)
    assert swapped, "the hook never ran, so the race was not exercised"
    assert answer in ((1, 2, 3), ())
    assert _snapshot(tmp_path / "outside") == before


def _make_levels(present: str) -> None:
    for level in LEVELS:
        _level_path(level).mkdir(exist_ok=True)
        if level == present:
            return


@needs_pinned_walk
def test_member_dashboard_missing_level_is_never_reopened_by_name(monkeypatch):
    """A folder an agent recreates after the failed open must not be read through its name."""
    data_home().mkdir(parents=True, exist_ok=True)

    def by_name(*_args: Any, **_kwargs: Any) -> bytes:
        raise AssertionError("a missing page folder was read again by name")

    monkeypatch.setattr(instance, "read_bytes_with_retry", by_name)
    assert instance.read(SLUG).state == instance.STATE_EMPTY


@pytest.mark.parametrize("pinned", [True, False])
@pytest.mark.parametrize("level", PAGE_LEVELS)
def test_member_dashboard_a_file_where_a_folder_belongs_is_not_taken_for_a_link(
    monkeypatch, pinned, level
):
    """Only a link answers "not set"; a file at a level keeps its refusal, never an empty page."""
    if pinned and not instance.pinned_fs.supports_pinned_walk():
        pytest.skip("needs descriptor-relative opens")
    if not pinned:
        if os.name == "nt":
            pytest.skip("a path below a file is a missing path on Windows")
        _no_pinning(monkeypatch)
    _seed()
    path = _level_path(level)
    shutil.rmtree(path)
    path.write_text("not a folder", encoding="utf-8")
    with pytest.raises(instance.InstanceError):
        instance.read(SLUG)


@pytest.mark.parametrize("pinned", [True, False])
@pytest.mark.parametrize("present", ["nothing", "members", "slug", "dashboard"])
@pytest.mark.parametrize("reader", READERS)
def test_member_dashboard_missing_level_reads_as_not_set_and_creates_nothing(
    tmp_path, monkeypatch, pinned, present, reader
):
    if pinned and not instance.pinned_fs.supports_pinned_walk():
        pytest.skip("needs descriptor-relative opens")
    if not pinned:
        _no_pinning(monkeypatch)
    data_home().mkdir(parents=True, exist_ok=True)
    if present != "nothing":
        _make_levels(present)
    before = sorted(str(p) for p in data_home().rglob("*"))
    assert _answer(reader) == _not_set(reader)
    assert sorted(str(p) for p in data_home().rglob("*")) == before
    assert os.path.isdir(data_home())


def _mark_outside_version(outside: Path, level: str) -> Path:
    """Make the other folder's first kept version tell itself apart; returns that file."""
    kept = outside / "1.json" if level == "versions" else outside / "versions" / "1.json"
    payload = json.loads(kept.read_text(encoding="utf-8"))
    payload["html"] += "<!-- outside -->"
    kept.write_text(json.dumps(payload), encoding="utf-8")
    return kept


@needs_pinned_walk
@pytest.mark.parametrize("level", ["versions", "dashboard"])
def test_member_dashboard_read_inside_the_lock_opens_each_folder_as_a_folder(tmp_path, level):
    """A folder swapped for a link after the lock is taken is refused or never left."""
    _seed()
    held = (_level_path("versions") / "1.json").read_bytes()
    outside = _outside_copy(tmp_path, level)
    _mark_outside_version(outside, level)
    before = _snapshot(outside)
    with instance._locked(instance.instance_dir(SLUG)):
        _link(level, outside)
        try:
            got: bytes | None = instance._read_file(SLUG, "versions", "1.json")
        except OSError:
            got = None
    assert got in (held, None)
    assert _snapshot(outside) == before


@needs_pinned_walk
def test_member_dashboard_rollback_never_reads_a_replaced_versions_folder(tmp_path):
    _seed()
    outside = _outside_copy(tmp_path, "versions")
    _mark_outside_version(outside, "versions")
    before = _snapshot(outside)
    real = instance._read_version
    swapped: list[bool] = []

    def swap_then_read(slug: str, version: int) -> Any:
        if not swapped:
            swapped.append(True)
            _link("versions", outside)
        return real(slug, version)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(instance, "_read_version", swap_then_read)
        try:
            record = instance.rollback(SLUG, 1)
        except instance.InstanceError:
            record = None
    assert swapped, "the hook never ran, so the race was not exercised"
    assert record is None or "outside" not in record.html
    assert _snapshot(outside) == before


@needs_pinned_walk
@pytest.mark.parametrize("level", ["versions", "dashboard"])
def test_member_dashboard_list_inside_the_lock_opens_versions_as_a_folder(tmp_path, level):
    _seed()
    held = sorted(os.listdir(_level_path("versions")))
    outside = _outside_copy(tmp_path, level)
    before = _snapshot(outside)
    with instance._locked(instance.instance_dir(SLUG)):
        _link(level, outside)
        try:
            got: list[str] | None = sorted(instance._list_versions(SLUG))
        except OSError:
            got = None
    assert got in (held, None)
    assert _snapshot(outside) == before


# -- A link at a file of the page folder ----------------------------------------------

#: ``(reader, file, answer)``: the file a link replaces, and what the reader says then.
LEAF_CASES = [
    ("read", "instance.json", None),
    ("pinned_package", "instance.json", None),
    ("history", "history.json", None),
    ("staged_preview", "preview.json", None),
    ("versions", "versions/2.json", (1, 3)),
]


def _link_file(name: str, tmp_path: Path) -> Path:
    """Replace the page folder's file *name* with a link to a copy outside it; returns the copy."""
    page = _level_path("dashboard")
    outside = tmp_path / "outside-file"
    outside.write_bytes((page / name).read_bytes())
    (page / name).unlink()
    (page / name).symlink_to(outside)
    return outside


@requires_symlinks
@pytest.mark.parametrize("pinned", [True, False])
@pytest.mark.parametrize(("reader", "name", "answer"), LEAF_CASES)
def test_member_dashboard_reads_a_linked_file_as_not_set(
    tmp_path, monkeypatch, pinned, reader, name, answer
):
    if pinned and not instance.pinned_fs.supports_pinned_walk():
        pytest.skip("needs descriptor-relative opens")
    if not pinned:
        _no_pinning(monkeypatch)
    _seed()
    outside = _link_file(name, tmp_path)
    held = outside.read_bytes()
    before = sorted(str(p) for p in data_home().rglob("*"))
    assert _answer(reader) == (_not_set(reader) if answer is None else answer)
    assert outside.read_bytes() == held
    assert sorted(str(p) for p in data_home().rglob("*")) == before


@requires_symlinks
@needs_pinned_walk
@pytest.mark.parametrize("name", ["instance.json", "versions/1.json"])
def test_member_dashboard_read_inside_the_lock_still_refuses_a_linked_file(tmp_path, name):
    _seed()
    _link_file(name, tmp_path)
    with instance._locked(instance.instance_dir(SLUG)):
        with pytest.raises(instance.InstanceError):
            if name == "instance.json":
                instance._read_record(SLUG)
            else:
                instance._read_version(SLUG, 1)


def _unreadable_record(how: str) -> None:
    record = _level_path("dashboard") / "instance.json"
    record.unlink()
    if how == "folder":
        record.mkdir()
    elif how == "bad json":
        record.write_text("{not json", encoding="utf-8")
    else:
        record.write_text(json.dumps({"schema": 999}), encoding="utf-8")


@pytest.mark.parametrize("pinned", [True, False])
@pytest.mark.parametrize("how", ["folder", "bad json", "newer schema"])
def test_member_dashboard_read_of_an_unreadable_record_is_not_taken_for_a_link(
    monkeypatch, pinned, how
):
    if pinned and not instance.pinned_fs.supports_pinned_walk():
        pytest.skip("needs descriptor-relative opens")
    if not pinned:
        _no_pinning(monkeypatch)
    _seed()
    _unreadable_record(how)
    if how == "newer schema":
        assert instance.read(SLUG).state == instance.STATE_ERROR
        return
    with pytest.raises(instance.InstanceError, match="cannot be read"):
        instance.read(SLUG)


@pytest.mark.parametrize(
    ("reader", "name"), [("history", "history.json"), ("staged_preview", "preview.json")]
)
def test_member_dashboard_history_and_staged_page_of_a_folder_answer_as_before(reader, name):
    _seed()
    path = _level_path("dashboard") / name
    path.unlink()
    path.mkdir()
    assert _answer(reader) == _not_set(reader)


@requires_symlinks
def test_member_dashboard_callers_that_do_not_catch_see_no_error_for_a_linked_record(tmp_path):
    """``member_export`` and the snapshot read the record bare; a link must not reach them as an error."""
    never_adopted = (member_export._dashboard("nobody"), _no_page_message("nobody"))
    _seed()
    _link_file("instance.json", tmp_path)
    assert member_export._dashboard(SLUG) == never_adopted[0] == ("", False)
    assert _no_page_message(SLUG) == never_adopted[1]


def _no_page_message(slug: str) -> str:
    try:
        agent_panel_snapshot._page_in_force(slug)
    except agent_panel_snapshot._NoPage as exc:
        return str(exc)
    return ""


# -- The sharing-violation retry, inside the new read -----------------------------------


@needs_pinned_walk
def test_member_dashboard_read_rides_out_a_sharing_violation():
    _seed()
    record = _level_path("dashboard") / "instance.json"
    with held_open(record, RIDDEN_OUT) as reads:
        current = instance.read(SLUG)
    assert current.instance_version == 3
    assert reads["n"] == RIDDEN_OUT + 1


@needs_pinned_walk
def test_member_dashboard_read_keeps_its_refusal_when_the_hold_outlasts_the_budget():
    _seed()
    record = _level_path("dashboard") / "instance.json"
    with held_open(record, PERSISTENT) as reads:
        with pytest.raises(instance.InstanceError, match="cannot be read"):
            instance.read(SLUG)
    assert reads["n"] == aw._REPLACE_MAX_ATTEMPTS

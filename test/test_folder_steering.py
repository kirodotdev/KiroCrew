"""Folder steering reader: inclusion rules, skip rules, dedup, root containment.

``collect_folder_steering`` is the ONE reader of a chat's folder-inherited
steering roots, so every rule the Context_Builder relies on is pinned here: the
``inclusion`` filter, the double-load skip for project and global steering, the
realpath dedup across roots, the per-file admissibility check against the
declared root as trust base, and the debug-and-skip degradation for a missing
directory or an unreadable file.

Properties 1 and 2 of the design are the two ``hypothesis`` tests at the end:
the resolver's root-first / deduplicated / cycle-safe walk, and the collector's
dedup + skip + containment + order stability over real temporary trees.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from conftest import make_dir_link, requires_symlinks
from kiro_crew.dashboard.chat_folders import _resolve_folder_steering_dirs
from kiro_crew.folder_steering import (
    _MAX_FOLDER_STEERING_DOCUMENTS,
    FOLDER_STEERING_FOOTER,
    FOLDER_STEERING_HEADER,
    collect_folder_steering,
    render_folder_steering,
)

_LOGGER_NAME = "kiro_crew.folder_steering"


def _write(path: Path, inclusion: str | None, body: str = "Prefer small diffs.") -> Path:
    """Write a steering document, with or without an ``inclusion`` frontmatter."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if inclusion is None:
        path.write_text(body, encoding="utf-8")
    else:
        path.write_text(f"---\ninclusion: {inclusion}\n---\n{body}\n", encoding="utf-8")
    return path


def _fake_home(tmp_path: Path) -> Path:
    """A home that is NOT the operator's, so ``~/.kiro/steering`` is never read."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return home


# ── inclusion rules ──


@pytest.mark.parametrize("inclusion", ["always", "Always", None])
def test_always_and_absent_inclusion_are_emitted_without_frontmatter(tmp_path, inclusion):
    root = tmp_path / "standards"
    doc = _write(root / "one.md", inclusion, body="Body line.")
    docs = collect_folder_steering([str(root)], project=None, home=_fake_home(tmp_path))
    assert [path for path, _ in docs] == [str(doc.resolve())]
    body = docs[0][1]
    assert "Body line." in body
    assert "inclusion" not in body
    assert "---" not in body


@pytest.mark.parametrize("inclusion", ["manual", "auto", "fileMatch", "FILEMATCH", " manual "])
def test_non_always_inclusion_is_skipped(tmp_path, inclusion):
    root = tmp_path / "standards"
    _write(root / "gated.md", inclusion)
    _write(root / "open.md", "always", body="Always body.")
    docs = collect_folder_steering([str(root)], project=None, home=_fake_home(tmp_path))
    assert [Path(path).name for path, _ in docs] == ["open.md"]


def test_nested_documents_are_found_in_sorted_order(tmp_path):
    root = tmp_path / "standards"
    _write(root / "b.md", "always")
    _write(root / "deep" / "nested" / "a.md", None)
    _write(root / "notes.txt", None)
    docs = collect_folder_steering([str(root)], project=None, home=_fake_home(tmp_path))
    # ``sorted`` over paths, so ``b.md`` precedes ``deep/nested/a.md``; the
    # non-markdown sibling is never a candidate.
    assert [Path(path).name for path, _ in docs] == ["b.md", "a.md"]


# ── graceful degradation ──


def test_missing_directory_is_skipped_and_debug_logged(tmp_path, caplog):
    present = tmp_path / "present"
    _write(present / "one.md", "always")
    missing = tmp_path / "gone"
    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        docs = collect_folder_steering(
            [str(missing), str(present)], project=None, home=_fake_home(tmp_path)
        )
    assert [Path(path).name for path, _ in docs] == ["one.md"]
    records = [r for r in caplog.records if r.name == _LOGGER_NAME]
    assert records and all(r.levelno == logging.DEBUG for r in records)
    assert any("not a directory" in r.getMessage() for r in records)


def test_a_file_that_is_a_directory_root_contributes_nothing(tmp_path, caplog):
    """A root that is a FILE is refused the same way a missing one is."""
    not_a_dir = _write(tmp_path / "standards.md", "always")
    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        docs = collect_folder_steering([str(not_a_dir)], project=None, home=_fake_home(tmp_path))
    assert docs == []
    assert any(r.name == _LOGGER_NAME for r in caplog.records)


def test_unreadable_document_is_skipped_and_debug_logged(tmp_path, caplog, monkeypatch):
    """A read refusal skips one document, not the directory.

    The read is failed at the ``safe_read_file`` seam rather than with ``chmod``:
    a mode-based refusal does not hold for a root user or on every filesystem
    CI runs on, and the branch under test is the exception handler.
    """
    root = tmp_path / "standards"
    blocked = _write(root / "a-blocked.md", "always")
    readable = _write(root / "b-readable.md", "always")

    def _refuse(path: str) -> str:
        if os.path.realpath(path) == str(blocked.resolve()):
            raise PermissionError("Blocked: nope")
        return Path(path).read_text(encoding="utf-8")

    monkeypatch.setattr("kiro_crew.folder_steering.safe_read_file", _refuse)
    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        docs = collect_folder_steering([str(root)], project=None, home=_fake_home(tmp_path))
    assert [path for path, _ in docs] == [str(readable.resolve())]
    assert any(
        r.name == _LOGGER_NAME and "unreadable" in r.getMessage() and r.levelno == logging.DEBUG
        for r in caplog.records
    )


# ── containment and dedup ──


@requires_symlinks
def test_symlink_resolving_outside_its_root_is_refused(tmp_path):
    root = tmp_path / "standards"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    _write(outside / "secret.md", "always", body="Outside body.")
    (root / "link.md").symlink_to(outside / "secret.md")
    kept = _write(root / "inside.md", "always")
    docs = collect_folder_steering([str(root)], project=None, home=_fake_home(tmp_path))
    assert [path for path, _ in docs] == [str(kept.resolve())]
    assert all("Outside body." not in body for _, body in docs)


def test_realpath_dedup_across_two_roots(tmp_path):
    root = tmp_path / "standards"
    doc = _write(root / "one.md", "always")
    linked = tmp_path / "linked-root"
    make_dir_link(linked, root)
    docs = collect_folder_steering(
        [str(root), str(linked), str(root) + os.sep + "."],
        project=None,
        home=_fake_home(tmp_path),
    )
    assert [path for path, _ in docs] == [str(doc.resolve())]


def test_project_and_home_kiro_steering_are_skipped(tmp_path):
    """Project and global steering already reach every provider; do not resend."""
    project = tmp_path / "project"
    home = _fake_home(tmp_path)
    _write(project / ".kiro" / "steering" / "project-rule.md", "always")
    _write(home / ".kiro" / "steering" / "global-rule.md", "always")
    own = _write(tmp_path / "standards" / "own.md", "always")
    docs = collect_folder_steering(
        [
            str(project / ".kiro" / "steering"),
            str(home / ".kiro" / "steering"),
            str(tmp_path / "standards"),
        ],
        project=str(project),
        home=home,
    )
    assert [path for path, _ in docs] == [str(own.resolve())]


def test_a_root_that_merely_contains_the_project_steering_tree_still_contributes(tmp_path):
    """The skip is per DOCUMENT, so a parent root keeps its other documents."""
    project = tmp_path / "project"
    kept = _write(project / "standards" / "keep.md", "always")
    _write(project / ".kiro" / "steering" / "drop.md", "always")
    docs = collect_folder_steering([str(project)], project=str(project), home=_fake_home(tmp_path))
    assert [path for path, _ in docs] == [str(kept.resolve())]


def test_empty_steering_dirs_reads_nothing(tmp_path):
    assert collect_folder_steering([], project=None, home=_fake_home(tmp_path)) == []


# ── renderer ──


def test_render_is_empty_without_documents():
    assert render_folder_steering([]) == ""


def test_render_lists_each_document_under_its_path():
    out = render_folder_steering([("/a/one.md", "\nFirst.\n"), ("/b/two.md", "Second.")])
    assert out.splitlines() == [
        FOLDER_STEERING_HEADER,
        "# /a/one.md",
        "First.",
        "",
        "# /b/two.md",
        "Second.",
        FOLDER_STEERING_FOOTER,
    ]


# ── Property 1: resolver is root-first, deduplicated and cycle-safe ──

_FOLDER_IDS = ["f0", "f1", "f2", "f3", "f4"]
_MISSING_ID = "f-absent"
_POOL_SIZE = 4

_folder_trees = st.dictionaries(
    keys=st.sampled_from(_FOLDER_IDS),
    values=st.tuples(
        st.one_of(st.none(), st.sampled_from([*_FOLDER_IDS, _MISSING_ID])),
        st.lists(st.integers(min_value=0, max_value=_POOL_SIZE - 1), unique=True, max_size=3),
    ),
    max_size=len(_FOLDER_IDS),
)


def _dir_pool(tmp_path: Path) -> list[Path]:
    pool = []
    for index in range(_POOL_SIZE):
        one = tmp_path / "pool" / f"d{index}"
        one.mkdir(parents=True, exist_ok=True)
        pool.append(one)
    return pool


def _walk_chain(folders: list[dict[str, Any]], start: str) -> list[dict[str, Any]]:
    """The documented walk: up ``parent_id``, stopping at a cycle or a gap."""
    by_id = {str(f.get("id") or ""): f for f in folders}
    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    current = start
    while current and current not in seen:
        seen.add(current)
        folder = by_id.get(current)
        if folder is None:
            break
        chain.append(folder)
        current = str(folder.get("parent_id") or "")
    return chain


@settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(tree=_folder_trees, start=st.sampled_from([*_FOLDER_IDS, _MISSING_ID, ""]))
def test_property_resolver_is_root_first_deduped_and_cycle_safe(tmp_path, tree, start):
    pool = _dir_pool(tmp_path)
    folders: list[dict[str, Any]] = [
        {
            "id": folder_id,
            "parent_id": parent or "",
            "steering_dirs": [str(pool[i]) for i in dir_indexes],
        }
        for folder_id, (parent, dir_indexes) in tree.items()
    ]

    resolved, err = _resolve_folder_steering_dirs(folders, start)

    # Every generated directory is a real, non-sensitive temporary directory, so
    # re-validation cannot fail: an error here would be a resolver defect.
    assert err is None, err
    assert len(resolved) == len(set(resolved)), "resolver emitted a duplicate"

    # Root-first: depth 0 is the LAST folder the walk reached (closest to root).
    chain = _walk_chain(folders, start)
    first_depth: dict[str, int] = {}
    for depth, folder in enumerate(reversed(chain)):
        for raw in folder["steering_dirs"]:
            first_depth.setdefault(os.path.realpath(raw), depth)

    assert set(resolved) == set(first_depth), "resolver dropped or invented a directory"
    depths = [first_depth[one] for one in resolved]
    assert depths == sorted(depths), "an ancestor's directory came after a descendant's"
    if not first_depth:
        assert resolved == []


# ── Property 2: collector dedups, respects skip rules, never escapes its roots ──

_INCLUSIONS = st.sampled_from([None, "always", "Always", "manual", "auto", "fileMatch", "MANUAL"])
_PLACEMENTS = st.sampled_from(["root", "nested", "project_steering", "home_steering"])

_document_plans = st.lists(
    st.tuples(
        st.integers(min_value=0, max_value=1),  # which root
        st.sampled_from(["a", "b", "c"]),  # file stem
        _INCLUSIONS,
        _PLACEMENTS,
    ),
    max_size=8,
)


@settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(plans=_document_plans)
def test_property_collector_dedups_skips_and_stays_within_roots(tmp_path, plans):
    # A fresh tree per example: reusing one directory would let an earlier
    # example's files decide a later one's expected output.
    with tempfile.TemporaryDirectory(dir=tmp_path) as scratch:
        base = Path(scratch)
        home = base / "home"
        project = base / "project"
        roots = [base / "root0", base / "root1"]
        for one in (home, project, *roots):
            one.mkdir(parents=True, exist_ok=True)
        # A second spelling of root0 that resolves to it, so dedup across roots
        # is exercised on every example rather than only when a plan repeats.
        linked = base / "root0-link"
        make_dir_link(linked, roots[0])

        for root_index, stem, inclusion, placement in plans:
            root = roots[root_index]
            if placement == "root":
                target = root / f"{stem}.md"
            elif placement == "nested":
                target = root / "deep" / f"{stem}.md"
            elif placement == "project_steering":
                target = project / ".kiro" / "steering" / f"{stem}.md"
            else:
                target = home / ".kiro" / "steering" / f"{stem}.md"
            _write(target, inclusion)

        declared = [str(roots[0]), str(linked), str(roots[1])]
        docs = collect_folder_steering(declared, project=str(project), home=home)
        again = collect_folder_steering(declared, project=str(project), home=home)

        paths = [path for path, _ in docs]
        assert paths == [path for path, _ in again], "collector is not order-stable"
        assert len(paths) == len(set(paths)), "a realpath was emitted twice"

        project_steering = str((project / ".kiro" / "steering").resolve()) + os.sep
        home_steering = str((home / ".kiro" / "steering").resolve()) + os.sep
        resolved_roots = [str(Path(one).resolve()) + os.sep for one in declared]
        for path, body in docs:
            assert not path.startswith(project_steering), path
            assert not path.startswith(home_steering), path
            assert any(path.startswith(one) for one in resolved_roots), path
            assert "inclusion:" not in body
        for path, _ in docs:
            text = Path(path).read_text(encoding="utf-8")
            declared_inclusion = ""
            if text.startswith("---"):
                declared_inclusion = text.split("\n")[1].partition(":")[2].strip().casefold()
            assert declared_inclusion not in {"manual", "auto", "filematch"}, path


def test_collection_stops_at_the_document_count_ceiling(tmp_path):
    """A tree with more docs than the count ceiling stops at the ceiling.

    Pins the running document-count bound: collection must not materialize an
    unbounded tree, so it returns at most ``_MAX_FOLDER_STEERING_DOCUMENTS``
    even when many more admissible ``.md`` files exist.
    """
    root = tmp_path / "standards"
    for i in range(_MAX_FOLDER_STEERING_DOCUMENTS + 25):
        _write(root / f"doc_{i:04d}.md", None, body="Prefer small diffs.")
    docs = collect_folder_steering([str(root)], project=None, home=_fake_home(tmp_path))
    assert len(docs) == _MAX_FOLDER_STEERING_DOCUMENTS


def test_collection_bounds_aggregate_via_the_document_ceiling(tmp_path):
    """Large bodies do not defeat the bound: aggregate is count x per-doc cap.

    Each stored body is already capped at ``_MAX_SOURCE_BYTES`` on read, so the
    document-count ceiling also bounds total memory. With many near-cap docs,
    collection still stops at the ceiling.
    """
    from kiro_crew.folder_steering import _MAX_SOURCE_BYTES

    root = tmp_path / "standards"
    big = "x" * (_MAX_SOURCE_BYTES - 8)
    for i in range(_MAX_FOLDER_STEERING_DOCUMENTS + 25):
        _write(root / f"big_{i:04d}.md", None, body=big)
    docs = collect_folder_steering([str(root)], project=None, home=_fake_home(tmp_path))
    assert len(docs) == _MAX_FOLDER_STEERING_DOCUMENTS
    total = sum(len(b) for _, b in docs)
    assert total <= _MAX_FOLDER_STEERING_DOCUMENTS * _MAX_SOURCE_BYTES

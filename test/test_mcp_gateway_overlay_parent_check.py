"""The gateway overlay folder is checked before stale files are removed.

The overlay folders sit under ``<data home>/mcp-gateway``. When that folder, or
any folder below it, is a link, the pass neither lists nor removes anything
through it: every file in the link target is left exactly as it was, on a full
pass and on a cache hit alike. A plain folder is still pruned. Where descriptors
allow it the removal is relative to an opened folder, so a link made after the
check is not followed either.
"""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import sys
from pathlib import Path

import pytest

from conftest import make_dir_link, requires_symlinks
from kiro_crew.config.paths import config_dir
from kiro_crew.mcp_gateway import mate_overlays, rewriter

SERVER = "jira"
#: The stream the prune writes to, shared with the rewriter.
LOGGER = "kiro_crew.mcp_gateway.rewriter"
PRUNED = ("agents", "agents/mates", "stubs/env")

#: What the descriptor-relative removal needs; the same test the helper makes.
_DESCRIPTORS = (
    hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_DIRECTORY")
    and os.unlink in os.supports_dir_fd
    and os.listdir in os.supports_fd
)
requires_descriptors = pytest.mark.skipif(
    not _DESCRIPTORS, reason="needs O_NOFOLLOW, O_DIRECTORY and unlink(dir_fd=)"
)


def _snapshot(root: Path, pattern: str = "unrelated.json") -> dict[str, str]:
    """Every file under *root* matching *pattern* (names relative to it) with its sha256."""
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob(pattern))
    }


def _add_files(root: Path) -> None:
    """Files the pass did not write, in every folder it prunes."""
    for folder in PRUNED:
        target = root / folder
        target.mkdir(parents=True, exist_ok=True)
        (target / "unrelated.json").write_text('{"keep": true}\n', encoding="utf-8")


def make_folder_link(link: Path, target: Path) -> None:
    """A link (symlink, or junction on Windows) at *link* to the folder *target*."""
    link.parent.mkdir(parents=True, exist_ok=True)
    make_dir_link(link, target)


@pytest.fixture
def gateway() -> Path:
    """``<data home>/mcp-gateway``, which does not exist yet."""
    return config_dir() / "mcp-gateway"


@pytest.fixture
def source(tmp_path: Path) -> Path:
    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "kirocrew.json").write_text(
        '{"name": "kirocrew", "mcpServers": {"%s": {"command": "%s", "args": ["-V"],'
        ' "env": {"JIRA_TOKEN": "secret"}}}}' % (SERVER, sys.executable.replace("\\", "\\\\")),
        encoding="utf-8",
    )
    return specs


def _rewrite(source: Path, gateway: Path, tmp_path: Path) -> None:
    rewriter.rewrite_agents(
        source_dir=source,
        overlay_dir=gateway / "agents",
        socket_path=tmp_path / "gw.sock",
        work_dir=tmp_path / "work",
        stub_servers=frozenset({SERVER}),
        pooling_enabled=True,
        approvals=None,
        mate_source_dir=None,
    )


def test_a_full_pass_removes_nothing_through_a_linked_gateway_folder(
    tmp_path: Path, gateway: Path, source: Path
) -> None:
    outside_dir = tmp_path / "outside"
    _add_files(outside_dir)
    make_folder_link(gateway, outside_dir)
    before = _snapshot(outside_dir)
    _rewrite(source, gateway, tmp_path)
    assert _snapshot(outside_dir) == before


@pytest.mark.parametrize("linked", ["agents", "stubs", "stubs/env"])
def test_a_full_pass_removes_nothing_through_a_linked_overlay_subfolder(
    tmp_path: Path, gateway: Path, source: Path, linked: str
) -> None:
    outside_dir = tmp_path / "outside"
    (outside_dir / "env").mkdir(parents=True)
    for folder in (outside_dir, outside_dir / "env"):
        (folder / "unrelated.json").write_text('{"keep": true}\n', encoding="utf-8")
    make_folder_link(gateway / linked, outside_dir)
    before = _snapshot(outside_dir)
    _rewrite(source, gateway, tmp_path)
    assert _snapshot(outside_dir) == before


def test_a_cache_hit_removes_nothing_through_a_linked_gateway_folder(
    tmp_path: Path, gateway: Path, source: Path
) -> None:
    _rewrite(source, gateway, tmp_path)
    assert (gateway / "agents" / rewriter._FINGERPRINT_NAME).is_file()
    moved = tmp_path / "moved"
    os.rename(gateway, moved)
    _add_files(moved)
    make_folder_link(gateway, moved)
    before = _snapshot(moved)
    _rewrite(source, gateway, tmp_path)
    assert _snapshot(moved) == before


@pytest.mark.parametrize("linked", ["agents", "stubs/env"])
def test_a_cache_hit_removes_nothing_through_a_linked_overlay_subfolder(
    tmp_path: Path, gateway: Path, source: Path, linked: str
) -> None:
    _rewrite(source, gateway, tmp_path)
    moved = tmp_path / "moved"
    (gateway / linked).mkdir(parents=True, exist_ok=True)
    os.rename(gateway / linked, moved)
    (moved / "unrelated.json").write_text('{"keep": true}\n', encoding="utf-8")
    make_folder_link(gateway / linked, moved)
    before = _snapshot(moved)
    _rewrite(source, gateway, tmp_path)
    assert _snapshot(moved) == before


@pytest.mark.parametrize("cached", [False, True])
def test_a_plain_overlay_folder_is_still_pruned(
    tmp_path: Path, gateway: Path, source: Path, cached: bool
) -> None:
    if cached:
        _rewrite(source, gateway, tmp_path)
    _add_files(gateway)
    _rewrite(source, gateway, tmp_path)
    for folder in ("agents", "stubs/env"):
        assert not (gateway / folder / "unrelated.json").exists(), folder


def test_the_crewmate_prune_removes_nothing_through_a_linked_gateway_folder(
    tmp_path: Path, gateway: Path
) -> None:
    outside_dir = tmp_path / "outside"
    _add_files(outside_dir)
    make_folder_link(gateway, outside_dir)
    before = _snapshot(outside_dir)
    mate_overlays.drop_mate_overlays(gateway / "agents")
    assert _snapshot(outside_dir) == before


def test_the_crewmate_overlay_pass_removes_nothing_through_a_linked_gateway_folder(
    tmp_path: Path, gateway: Path
) -> None:
    outside_dir = tmp_path / "outside"
    _add_files(outside_dir)
    make_folder_link(gateway, outside_dir)
    before = _snapshot(outside_dir)
    mate_overlays.rewrite_mate_overlays(
        mate_overlays.MateSources(),
        overlay_dir=gateway / "agents",
        taken_identities=set(),
        publish=lambda spec, path: 0,
    )
    assert _snapshot(outside_dir) == before


@requires_descriptors
def test_a_link_made_after_the_check_is_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.mcp_gateway import overlay_prune

    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "a.json").write_text('{"a": 1}\n', encoding="utf-8")
    (outside_dir / "b.json").write_text('{"b": 1}\n', encoding="utf-8")
    folder = tmp_path / "folder"
    make_folder_link(folder, outside_dir)
    before = _snapshot(outside_dir, "*.json")
    with monkeypatch.context() as mp:
        mp.setattr(overlay_prune, "refuse_linked_parent", lambda path: None)
        overlay_prune.prune_stale_json(folder, lambda name: False)
    assert _snapshot(outside_dir, "*.json") == before


@requires_descriptors
def test_a_folder_swapped_for_a_link_while_pruning_keeps_the_target(tmp_path: Path) -> None:
    from kiro_crew.mcp_gateway import overlay_prune

    folder = tmp_path / "folder"
    outside_dir = tmp_path / "outside"
    moved = tmp_path / "moved"
    for root in (folder, outside_dir):
        root.mkdir()
        (root / "a.json").write_text(f'{{"in": "{root.name}"}}\n', encoding="utf-8")
        (root / "b.json").write_text(f'{{"in": "{root.name}"}}\n', encoding="utf-8")
    before = _snapshot(outside_dir, "*.json")
    swapped: list[str] = []

    def keep(name: str) -> bool:
        if not swapped:
            os.rename(folder, moved)
            make_folder_link(folder, outside_dir)
        swapped.append(name)
        return False

    overlay_prune.prune_stale_json(folder, keep)
    assert sorted(swapped) == ["a.json", "b.json"]
    assert _snapshot(outside_dir, "*.json") == before
    assert not list(moved.glob("*.json"))


def test_a_plain_folder_is_pruned_by_path_without_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.mcp_gateway import overlay_prune

    folder = tmp_path / "folder"
    folder.mkdir()
    (folder / "stale.json").write_text("{}\n", encoding="utf-8")
    (folder / "kept.json").write_text("{}\n", encoding="utf-8")
    (folder / "note.txt").write_text("x\n", encoding="utf-8")
    with monkeypatch.context() as mp:
        mp.setattr(overlay_prune, "_PINNED_PRUNE", False)
        overlay_prune.prune_stale_json(folder, lambda name: name == "kept.json")
    assert sorted(p.name for p in folder.iterdir()) == ["kept.json", "note.txt"]


@requires_descriptors
def test_a_folder_that_cannot_be_opened_is_logged_and_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from kiro_crew.mcp_gateway import overlay_prune

    not_a_folder = tmp_path / "folder"
    not_a_folder.write_text("keep\n", encoding="utf-8")
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        overlay_prune.prune_stale_json(not_a_folder, lambda name: False)
    [record] = caplog.records
    assert record.levelno == logging.WARNING
    assert str(not_a_folder) in record.getMessage()
    assert not_a_folder.read_text(encoding="utf-8") == "keep\n"


def test_a_missing_folder_is_nothing_to_prune_and_logs_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from kiro_crew.mcp_gateway import overlay_prune

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        overlay_prune.prune_stale_json(tmp_path / "absent", lambda name: False)
    assert caplog.records == []


@requires_descriptors
def test_a_folder_that_cannot_be_listed_is_logged_and_nothing_is_removed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.mcp_gateway import overlay_prune

    folder = tmp_path / "folder"
    folder.mkdir()
    (folder / "stale.json").write_text("{}\n", encoding="utf-8")

    def refuse(fd: int) -> list[str]:
        raise PermissionError(errno.EACCES, "cannot list")

    with monkeypatch.context() as mp, caplog.at_level(logging.DEBUG, logger=LOGGER):
        mp.setattr(os, "listdir", refuse)
        overlay_prune.prune_stale_json(folder, lambda name: False)
    [record] = caplog.records
    assert record.levelno == logging.WARNING
    assert str(folder) in record.getMessage()
    assert (folder / "stale.json").exists()


@requires_descriptors
def test_a_parent_folder_swapped_for_a_link_after_the_check_is_not_followed(
    tmp_path: Path, gateway: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.mcp_gateway import overlay_prune

    # Under the data home, where a link below the trust anchor is refused; outside
    # it a link is taken to be the operator's own layout and is followed.
    parent = gateway
    (parent / "folder").mkdir(parents=True)
    outside_dir = tmp_path / "outside"
    (outside_dir / "folder").mkdir(parents=True)
    for name in ("a.json", "b.json"):
        (outside_dir / "folder" / name).write_text(f'{{"{name}": 1}}\n', encoding="utf-8")
    before = _snapshot(outside_dir, "*.json")
    check = overlay_prune.refuse_linked_parent

    def check_then_swap(path: Path) -> None:
        check(path)
        os.rename(parent, tmp_path / "moved")
        make_folder_link(parent, outside_dir)

    with monkeypatch.context() as mp:
        mp.setattr(overlay_prune, "refuse_linked_parent", check_then_swap)
        overlay_prune.prune_stale_json(parent / "folder", lambda name: False)
    assert _snapshot(outside_dir, "*.json") == before


@pytest.mark.parametrize("pinned", [pytest.param(True, marks=requires_descriptors), False])
@requires_symlinks
def test_a_file_link_inside_a_plain_folder_is_removed_and_its_target_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pinned: bool
) -> None:
    from kiro_crew.mcp_gateway import overlay_prune

    target = tmp_path / "target.json"
    target.write_text('{"target": 1}\n', encoding="utf-8")
    folder = tmp_path / "folder"
    folder.mkdir()
    os.symlink(target, folder / "stale.json")
    with monkeypatch.context() as mp:
        mp.setattr(overlay_prune, "_PINNED_PRUNE", pinned)
        overlay_prune.prune_stale_json(folder, lambda name: False)
    assert not os.path.lexists(folder / "stale.json")
    assert target.read_text(encoding="utf-8") == '{"target": 1}\n'


@requires_descriptors
def test_a_folder_link_inside_a_plain_folder_is_removed_and_its_target_kept(
    tmp_path: Path,
) -> None:
    from kiro_crew.mcp_gateway import overlay_prune

    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "inner.json").write_text('{"inner": 1}\n', encoding="utf-8")
    before = _snapshot(outside_dir, "*.json")
    folder = tmp_path / "folder"
    folder.mkdir()
    make_folder_link(folder / "stale.json", outside_dir)
    overlay_prune.prune_stale_json(folder, lambda name: False)
    assert not os.path.lexists(folder / "stale.json")
    assert _snapshot(outside_dir, "*.json") == before


@pytest.mark.parametrize("pinned", [pytest.param(True, marks=requires_descriptors), False])
def test_a_directory_named_like_a_stale_file_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pinned: bool
) -> None:
    from kiro_crew.mcp_gateway import overlay_prune

    folder = tmp_path / "folder"
    (folder / "x.json").mkdir(parents=True)
    (folder / "x.json" / "inner.txt").write_text("keep\n", encoding="utf-8")
    with monkeypatch.context() as mp:
        mp.setattr(overlay_prune, "_PINNED_PRUNE", pinned)
        overlay_prune.prune_stale_json(folder, lambda name: False)
    assert (folder / "x.json" / "inner.txt").read_text(encoding="utf-8") == "keep\n"


def test_a_platform_that_cannot_open_relative_to_a_folder_prunes_by_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.mcp_gateway import overlay_prune

    folder = tmp_path / "folder"
    folder.mkdir()
    (folder / "stale.json").write_text("{}\n", encoding="utf-8")
    (folder / "kept.json").write_text("{}\n", encoding="utf-8")

    def no_descriptor_open(*args: object, **kwargs: object) -> int:
        raise TypeError("open() cannot take dir_fd here")

    with monkeypatch.context() as mp:
        mp.setattr(overlay_prune, "supports_pinned_walk", lambda: False)
        mp.setattr(overlay_prune, "pin_parent", no_descriptor_open)
        overlay_prune.prune_stale_json(folder, lambda name: name == "kept.json")
    assert sorted(p.name for p in folder.iterdir()) == ["kept.json"]


@requires_descriptors
def test_a_folder_path_that_cannot_be_resolved_is_logged_and_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.mcp_gateway import overlay_prune

    folder = tmp_path / "folder"
    folder.mkdir()
    (folder / "stale.json").write_text("{}\n", encoding="utf-8")
    resolve = Path.resolve

    def loop(self: Path, *args: object, **kwargs: object) -> Path:
        if self == folder.parent:
            raise RuntimeError("Symlink loop from " + str(self))
        return resolve(self, *args, **kwargs)  # type: ignore[arg-type]

    with monkeypatch.context() as mp, caplog.at_level(logging.DEBUG, logger=LOGGER):
        # No trust anchor, so the parent is resolved by path; the check is not under test.
        mp.setattr(overlay_prune, "anchored_parent", lambda path: None)
        mp.setattr(overlay_prune, "refuse_linked_parent", lambda path: None)
        mp.setattr(Path, "resolve", loop)
        overlay_prune.prune_stale_json(folder, lambda name: False)
    [record] = caplog.records
    assert record.levelno == logging.WARNING
    assert str(folder) in record.getMessage()
    assert (folder / "stale.json").exists()


def test_the_first_warning_says_a_link_only_when_the_folder_is_a_link(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.mcp_gateway import overlay_prune

    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    linked = tmp_path / "linked"
    make_folder_link(linked, outside_dir)
    plain = tmp_path / "plain"
    plain.mkdir()

    def cannot_check(path: Path) -> None:
        raise OSError("parent chain cannot be resolved")

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        overlay_prune.prune_stale_json(linked, lambda name: False)
        link_message = caplog.records[-1].getMessage()
        with monkeypatch.context() as mp:
            mp.setattr(overlay_prune, "refuse_linked_parent", cannot_check)
            overlay_prune.prune_stale_json(plain, lambda name: False)
        other_message = caplog.records[-1].getMessage()
    assert "is a link" in link_message
    assert "could not be checked" in other_message
    assert "parent chain cannot be resolved" in other_message
    assert "is a link" not in other_message


@requires_descriptors
def test_an_ancestor_two_levels_up_swapped_for_a_link_after_the_check_is_not_followed(
    tmp_path: Path, gateway: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.mcp_gateway import overlay_prune

    top = gateway / "top"
    (top / "middle" / "folder").mkdir(parents=True)
    outside_dir = tmp_path / "outside"
    (outside_dir / "middle" / "folder").mkdir(parents=True)
    (outside_dir / "middle" / "folder" / "a.json").write_text('{"a": 1}\n', encoding="utf-8")
    before = _snapshot(outside_dir, "*.json")
    check = overlay_prune.refuse_linked_parent

    def check_then_swap(path: Path) -> None:
        check(path)
        os.rename(top, tmp_path / "moved")
        make_folder_link(top, outside_dir)

    with monkeypatch.context() as mp:
        mp.setattr(overlay_prune, "refuse_linked_parent", check_then_swap)
        overlay_prune.prune_stale_json(top / "middle" / "folder", lambda name: False)
    assert _snapshot(outside_dir, "*.json") == before

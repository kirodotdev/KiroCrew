"""Ancestor protection on the write path, and the limit that remains.

`O_NOFOLLOW` covers the FINAL component only. Demonstrated before any of this existed:
replacing a PARENT directory with a symlink sent the write to the link's target while
every final-component check saw nothing wrong -- `victim now contains: 'CLOBBERED'`.

Three attempts, and the third is here because the first two were wrong in ways only
tests exposed:

1. Walking the guard's fully-resolved path resolved away the FINAL symlink too, so a
   link at the report name stopped being refused (two existing tests: DID NOT RAISE).
2. Replacing the walk with a single pinned parent descriptor. I called that "strictly
   stronger"; it is not, for the case being reported. `os.open(parent, O_DIRECTORY)`
   carries no `O_NOFOLLOW`, so a directory swapped between the guard and the open is
   FOLLOWED and the descriptor then pins the attacker's target. Pinning protects
   everything after the open and nothing before it.
3. What is here now: resolve the parent ONCE in the caller, then walk that chain with
   one `openat` per component, each carrying `O_NOFOLLOW`, and open the final name as
   given relative to the last descriptor.

The remaining gap is stated rather than implied: a component swapped BEFORE the parent
is resolved is followed by that resolution, and `guard_write_path` vets where it lands
for sensitivity. Closing that would mean refusing every symlinked ancestor, which also
breaks `--out-dir /tmp/...` on macOS, where `/tmp` is a link -- and this repo builds
there. The window is narrowed from guard-to-open down to resolve-to-first-openat, which
contains no I/O.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kiro_crew.eval.bench.safepath import (
    UnsafePathError,
    _supports_pinned_walk,
    open_write_nofollow,
)

pinned_only = pytest.mark.skipif(
    not _supports_pinned_walk(),
    reason="requires O_DIRECTORY, O_NOFOLLOW and dir_fd support (POSIX)",
)


@pinned_only
def test_a_component_swapped_after_resolution_is_refused(tmp_path: Path) -> None:
    """The case a single pinned parent does NOT catch, which is why the walk is back.

    The helper is called with a `resolved_parent` captured BEFORE the swap -- exactly
    the state the caller is in when an attacker wins the resolve-to-openat window.
    Simulating it another way would test the simulation.
    """
    from kiro_crew.eval.bench.safepath import _open_in_pinned_parent

    victim_dir = tmp_path / "elsewhere"
    victim_dir.mkdir()
    victim = victim_dir / "report.json"
    victim.write_text("PRECIOUS\n", encoding="utf-8")

    out = tmp_path / "reports"
    out.mkdir()
    resolved_parent = os.path.realpath(out)

    # The window.
    out.rmdir()
    out.symlink_to(victim_dir)

    with pytest.raises(UnsafePathError) as excinfo:
        _open_in_pinned_parent(
            resolved_parent,
            "report.json",
            flags=os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW,
            mode=0o600,
            what="JSON report",
        )
    assert "became a symbolic link" in str(excinfo.value)
    assert victim.read_text(encoding="utf-8") == "PRECIOUS\n", (
        "the write followed the swapped component despite the refusal"
    )


@pinned_only
def test_an_already_open_component_cannot_be_repointed(tmp_path: Path) -> None:
    """The other half: descriptors fix what has already been traversed.

    Asserted on WHERE THE BYTES LANDED rather than on an exception, because an
    exception-only check cannot tell "pinned" from "got lucky".
    """
    victim_dir = tmp_path / "elsewhere"
    victim_dir.mkdir()
    victim = victim_dir / "report.json"
    victim.write_text("PRECIOUS\n", encoding="utf-8")

    out = tmp_path / "reports"
    out.mkdir()

    fd = open_write_nofollow(out / "report.json", what="JSON report")
    out.rename(tmp_path / "reports-moved")
    (tmp_path / "reports").symlink_to(victim_dir)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("BENCHMARK OUTPUT")

    assert victim.read_text(encoding="utf-8") == "PRECIOUS\n"
    assert (tmp_path / "reports-moved" / "report.json").read_text(
        encoding="utf-8"
    ) == "BENCHMARK OUTPUT"


@pinned_only
def test_the_unpinned_path_refuses_a_linked_ancestor_instead_of_following_it(
    tmp_path: Path, monkeypatch
) -> None:
    """The fallback no longer trades ancestor protection for portability.

    This test used to assert the OPPOSITE -- that the unpinned path follows a swapped
    parent -- and said so as an honest statement of the platform gap. Round 18 closed
    that gap the only way a platform without `dir_fd` can: where the write cannot be
    pinned to an inode, a reparse point in the ancestor chain is refused outright.

    What is NOT claimed is parity. The pinned path holds a descriptor, so a component
    swapped after the check cannot be reached at all; here the scan and the write are
    still two steps, and an attacker who creates a link in between wins a microsecond
    race. That residual is documented on `_revalidate_unpinned`.
    """
    monkeypatch.setattr(
        "kiro_crew.eval.bench.safepath._supports_pinned_walk", lambda: False
    )

    victim_dir = tmp_path / "elsewhere"
    victim_dir.mkdir()
    victim = victim_dir / "report.json"
    victim.write_text("PRECIOUS\n", encoding="utf-8")

    out = tmp_path / "reports"
    out.mkdir()
    out.rmdir()
    out.symlink_to(victim_dir)

    with pytest.raises(UnsafePathError, match="link or junction"):
        open_write_nofollow(out / "fresh.json", what="JSON report")

    assert victim.read_text(encoding="utf-8") == "PRECIOUS\n"
    assert not (victim_dir / "fresh.json").exists(), (
        "the unpinned path still followed the swapped parent"
    )


def test_the_unpinned_path_still_writes_through_real_directories(
    tmp_path: Path, monkeypatch
) -> None:
    """Refusing a linked ancestor must not refuse the ordinary case as well."""
    monkeypatch.setattr(
        "kiro_crew.eval.bench.safepath._supports_pinned_walk", lambda: False
    )
    out = tmp_path / "reports"
    out.mkdir()

    fd = open_write_nofollow(out / "report.json", what="JSON report")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("{}")

    assert (out / "report.json").read_text(encoding="utf-8") == "{}"


@pinned_only
def test_a_link_at_the_final_name_is_still_refused(tmp_path: Path) -> None:
    """The regression the first two attempts introduced.

    Walking a fully-resolved path silently resolved the final symlink away. Pinned or
    not, the final component must be opened by the caller's own name.
    """
    bystander = tmp_path / "notes.txt"
    bystander.write_text("KEEP ME\n", encoding="utf-8")
    link = tmp_path / "report.json"
    try:
        link.symlink_to(bystander)
    except (OSError, NotImplementedError):  # pragma: no cover - privilege dependent
        pytest.skip("symlink creation requires privileges on this platform")

    with pytest.raises(UnsafePathError):
        open_write_nofollow(link, what="JSON report")
    assert bystander.read_text(encoding="utf-8") == "KEEP ME\n"


@pinned_only
def test_a_symlinked_parent_named_by_the_caller_is_still_allowed(tmp_path: Path) -> None:
    """The documented limit, asserted so it cannot be mistaken for a bug.

    A parent that was ALREADY a link when the caller named it is followed -- pointing
    `--out-dir` at a symlinked directory is legitimate, `/tmp` is one on macOS, and
    the guard has already vetted where it resolves to. If this ever starts failing,
    the write path has become stricter than `--out-dir` can tolerate.
    """
    real_dir = tmp_path / "real-reports"
    real_dir.mkdir()
    linked = tmp_path / "reports"
    try:
        linked.symlink_to(real_dir)
    except (OSError, NotImplementedError):  # pragma: no cover - privilege dependent
        pytest.skip("symlink creation requires privileges on this platform")

    fd = open_write_nofollow(linked / "report.json", what="JSON report")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("{}")
    assert (real_dir / "report.json").read_text(encoding="utf-8") == "{}"


@pinned_only
def test_a_relative_output_path_still_works(tmp_path: Path, monkeypatch) -> None:
    """`--out-dir reports` is what the nightly workflow passes."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "reports").mkdir()
    fd = open_write_nofollow(Path("reports/report.json"), what="JSON report")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("{}")
    assert (tmp_path / "reports" / "report.json").read_text(encoding="utf-8") == "{}"


@pinned_only
def test_a_bare_filename_in_the_current_directory_still_works(
    tmp_path: Path, monkeypatch
) -> None:
    """`Path('x.json').parent` is `.` -- an empty parent must not break the open."""
    monkeypatch.chdir(tmp_path)
    fd = open_write_nofollow(Path("report.json"), what="JSON report")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("{}")
    assert (tmp_path / "report.json").read_text(encoding="utf-8") == "{}"

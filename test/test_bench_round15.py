"""Round-15 review findings — both on the platform without ``O_NOFOLLOW``.

1. The creating writer opened an existing name for writing, so a link planted between
   the ``is_symlink`` check and the ``open`` was followed and its target truncated.
   The old code documented that window as an accepted limit; it is now closed by
   creating exclusively, which has no such moment on any platform.
2. The atomic writer's fallback branch skipped the destination alias check entirely,
   so a hardlink alias at the report name was replaced on Windows while the identical
   write was refused everywhere else. Found by this PR's own round-14 test failing on
   the Windows shard.

Both are exercised here with ``O_NOFOLLOW`` and ``dir_fd`` removed, which is the only
way this host can reach either path.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kiro_crew.eval.bench.safepath import (
    UnsafePathError,
    open_write_nofollow,
    write_text_atomic_nofollow,
)


@pytest.fixture
def windows_like(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``O_NOFOLLOW``, no ``dir_fd`` — the two things Windows does not have."""
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    monkeypatch.setattr(os, "supports_dir_fd", frozenset())


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):  # pragma: no cover - privilege dependent
        pytest.skip("symlink creation requires privileges on this platform")


def test_a_link_planted_after_the_check_can_no_longer_be_followed(
    tmp_path: Path, windows_like: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check-to-use window is closed by creation, not by checking harder.

    The link is planted DURING the ``is_symlink`` call, which is the ordering a real
    race produces and the ordering the previous version lost to: it saw "not a link",
    then opened the name that had since become one, then truncated the target.
    """
    victim = tmp_path / "precious.txt"
    victim.write_text("PRECIOUS", encoding="utf-8")
    target = tmp_path / "report.json"

    real_is_symlink = Path.is_symlink
    planted = {"done": False}

    def is_symlink_then_plant(self: Path) -> bool:
        answer = real_is_symlink(self)
        if self == target and not planted["done"]:
            planted["done"] = True
            _symlink_or_skip(target, victim)
        return answer

    monkeypatch.setattr(Path, "is_symlink", is_symlink_then_plant)

    with pytest.raises(UnsafePathError):
        open_write_nofollow(target, what="JSON report")

    monkeypatch.setattr(Path, "is_symlink", real_is_symlink)
    assert victim.read_text(encoding="utf-8") == "PRECIOUS", (
        "the write followed a link planted after the check and destroyed its target"
    )


def test_the_atomic_writer_refuses_a_hardlink_alias_without_dir_fd(
    tmp_path: Path, windows_like: None
) -> None:
    """The fallback branch must apply the same refusal as the pinned branch.

    Not a cosmetic gap: the alias is another name for a file the command was never
    pointed at, and replacing it discards that name.
    """
    victim = tmp_path / "elsewhere.json"
    victim.write_text("protected", encoding="utf-8")
    alias = tmp_path / "report.json"
    try:
        os.link(victim, alias)
    except (OSError, NotImplementedError):  # pragma: no cover - filesystem dependent
        pytest.skip("hard links are not supported here")

    with pytest.raises(UnsafePathError, match="hard link"):
        write_text_atomic_nofollow(alias, "clobber", what="JSON report")

    assert victim.read_text(encoding="utf-8") == "protected"
    assert alias.read_text(encoding="utf-8") == "protected"


def test_the_atomic_writer_still_replaces_a_plain_file_without_dir_fd(
    tmp_path: Path, windows_like: None
) -> None:
    """The refusals must not cost the ordinary re-run on that platform."""
    dest = tmp_path / "report.json"
    dest.write_text("old", encoding="utf-8")

    write_text_atomic_nofollow(dest, "new", what="JSON report")

    assert dest.read_text(encoding="utf-8") == "new"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["report.json"]


def test_a_symlink_at_the_name_still_names_itself_in_the_refusal(
    tmp_path: Path, windows_like: None
) -> None:
    """Exclusive creation reports EEXIST for a link, so the message is classified.

    "something already exists" would be a worse diagnosis than the two messages this
    used to give, and the classification happens after the refusal, so it cannot be
    raced into permitting anything.
    """
    victim = tmp_path / "precious.txt"
    victim.write_text("PRECIOUS", encoding="utf-8")
    link = tmp_path / "report.json"
    _symlink_or_skip(link, victim)

    with pytest.raises(UnsafePathError, match="symbolic link"):
        open_write_nofollow(link, what="JSON report")

    assert victim.read_text(encoding="utf-8") == "PRECIOUS"


def test_the_staging_name_carries_the_pid(tmp_path: Path) -> None:
    """Exclusive creation makes a stale staging file fatal unless the name is unique.

    A `.part` left by a killed download must not turn every later fetch into a
    refusal, so the process id is part of the name.
    """
    from kiro_crew.eval.bench import datasets

    source = Path(datasets.__file__).read_text(encoding="utf-8")
    effective = [ln for ln in source.splitlines() if not ln.lstrip().startswith("#")]
    assert any(
        'os.getpid()' in ln and '.part' in ln for ln in effective
    ), "the staging name no longer includes the pid"

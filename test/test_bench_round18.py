"""Round-18 review finding: the re-validation was applied to one branch of two.

Round 16 made the pinned branch re-check where the parent resolves to now. The
no-``dir_fd`` fallback kept acting on a verdict given earlier, so a retargeted ancestor
could still move the write -- and on that branch the write is an ``os.replace``, which
would put report JSON where a governance file was. Same rule, one of two sites: the
defect class this module keeps closing.

Both writers now re-validate on both branches, and where the parent cannot be pinned to
a descriptor a reparse point in the ancestor chain is refused outright.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import kiro_crew.eval.bench.safepath as sp
from kiro_crew.eval.bench.safepath import UnsafePathError


@pytest.fixture
def unpinnable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the branch a platform without ``dir_fd`` takes."""
    monkeypatch.setattr(sp, "_supports_pinned_walk", lambda: False)


def _linked_out_dir(tmp_path: Path) -> tuple[Path, Path]:
    victim = tmp_path / "elsewhere"
    victim.mkdir()
    out = tmp_path / "reports"
    try:
        out.symlink_to(victim)
    except (OSError, NotImplementedError):  # pragma: no cover - privilege dependent
        pytest.skip("symlink creation requires privileges on this platform")
    return out, victim


def test_the_atomic_writer_refuses_a_linked_ancestor_when_it_cannot_pin(
    tmp_path: Path, unpinnable: None
) -> None:
    """`os.replace` through a retargeted ancestor is the dangerous one.

    A rename cannot be made safe by inspecting the destination first, because the
    ancestor decides which destination the name even means.
    """
    out, victim = _linked_out_dir(tmp_path)

    with pytest.raises(UnsafePathError, match="link or junction"):
        sp.write_text_atomic_nofollow(out / "report.json", "{}", what="JSON report")

    assert not (victim / "report.json").exists()


def test_the_creating_writer_refuses_the_same_chain(
    tmp_path: Path, unpinnable: None
) -> None:
    """Both writers, one rule -- that is the whole finding."""
    out, victim = _linked_out_dir(tmp_path)

    with pytest.raises(UnsafePathError, match="link or junction"):
        sp.open_write_nofollow(out / "corpus.json.part", what="corpus file")

    assert not (victim / "corpus.json.part").exists()


def test_a_real_directory_chain_is_still_written(
    tmp_path: Path, unpinnable: None
) -> None:
    """The ordinary case on that platform must keep working.

    A guard that refused every write where pinning is unavailable would satisfy the
    finding and withdraw the command from Windows; this asserts it did not.
    """
    out = tmp_path / "reports"
    out.mkdir()

    sp.write_text_atomic_nofollow(out / "report.json", "{}", what="JSON report")

    assert (out / "report.json").read_text(encoding="utf-8") == "{}"


def test_a_junction_is_recognised_even_though_islink_is_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows junctions are reparse points that ``islink`` does not report.

    Simulated by hiding the symlink from ``islink`` and exposing a reparse tag, which
    is the shape ``os.lstat`` returns for a junction. Without the tag check the guard
    would pass on exactly the component Windows attackers actually retarget.
    """
    out, _ = _linked_out_dir(tmp_path)
    monkeypatch.setattr(os.path, "islink", lambda p: False)

    real_lstat = os.lstat

    class _Stat:
        def __init__(self, wrapped: object) -> None:
            self._wrapped = wrapped
            self.st_reparse_tag = 0xA0000003  # IO_REPARSE_TAG_MOUNT_POINT

        def __getattr__(self, name: str) -> object:
            return getattr(self._wrapped, name)

    def lstat_with_tag(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        st = real_lstat(path, *args, **kwargs)
        return _Stat(st) if Path(path) == out else st

    monkeypatch.setattr(os, "lstat", lstat_with_tag)

    assert sp._is_reparse_point(out) is True


def test_a_short_path_is_not_mistaken_for_a_link(tmp_path: Path) -> None:
    """The cheap `realpath != abspath` test would have been wrong on Windows.

    A Windows temp directory comes back as an 8.3 short path, which differs from its
    resolved form with nothing linked anywhere -- that comparison would refuse every
    write on the CI runner. The guard inspects components instead.
    """
    real = tmp_path / "reports"
    real.mkdir()

    assert sp._is_reparse_point(real) is False
    assert all(not sp._is_reparse_point(p) for p in [real, *real.parents][:3])

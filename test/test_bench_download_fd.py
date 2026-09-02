"""The `_download` descriptor leak, tested by the thing it actually leaks.

The bug: `open_write_nofollow` was evaluated BEFORE `urlopen` in the same `with`
header, so a URL error left the raw fd unowned -- `os.fdopen(fd)` never ran,
because evaluating the header is what raised.

Its loudest symptom is Windows-only (the surviving handle makes `tmp.unlink()`
raise `PermissionError [WinError 32]`, so the `CorpusFetchError` the handler
exists to raise never arrives), and CI's Windows shard caught it. But the leak
itself is platform-independent, so this counts descriptors rather than asserting
on a Windows error -- otherwise the fix would only be verifiable on the one
platform that cannot be run locally.
"""

from __future__ import annotations

import os
import urllib.error
from pathlib import Path

import pytest

from kiro_crew.eval.bench.datasets import CorpusFetchError, _download


def _open_fd_count() -> int:
    """Descriptors held by this process.

    /proc is Linux-only; the fallback keeps the test meaningful elsewhere by
    probing which low descriptors are live.
    """
    proc_fd = Path("/proc/self/fd")
    if proc_fd.is_dir():
        return len(list(proc_fd.iterdir()))
    live = 0
    for candidate in range(3, 256):
        try:
            os.fstat(candidate)
        except OSError:
            continue
        live += 1
    return live


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.HTTPError("https://x.invalid/c.json", 404, "Not Found", {}, None),
        urllib.error.URLError("connection refused"),
        TimeoutError("timed out"),
    ],
)
def test_a_failed_download_leaks_no_descriptor(
    tmp_path: Path, monkeypatch, error: Exception
) -> None:
    dest = tmp_path / "corpus.json"

    def boom(*_a: object, **_k: object):
        raise error

    monkeypatch.setattr("urllib.request.urlopen", boom)

    before = _open_fd_count()
    for _ in range(5):
        # Repeated so a single leaked descriptor is unambiguous rather than lost in
        # the noise of an unrelated allocation.
        with pytest.raises(CorpusFetchError):
            _download("https://x.invalid/c.json", dest)
    after = _open_fd_count()

    assert after <= before, f"leaked {after - before} descriptor(s) across 5 failures"


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.HTTPError("https://x.invalid/c.json", 500, "Boom", {}, None),
        urllib.error.URLError("dns"),
    ],
)
def test_a_failed_download_leaves_no_staging_file(
    tmp_path: Path, monkeypatch, error: Exception
) -> None:
    """A stale `.part` is the other half: the next run must not find debris.

    On Windows this assertion was unreachable, because `unlink` raised before it
    could run.
    """
    dest = tmp_path / "corpus.json"
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *_a, **_k: (_ for _ in ()).throw(error)
    )
    with pytest.raises(CorpusFetchError):
        _download("https://x.invalid/c.json", dest)
    assert not (tmp_path / "corpus.json.part").exists()
    assert not dest.exists()


def test_the_staging_file_is_not_created_before_the_response_arrives(
    tmp_path: Path, monkeypatch
) -> None:
    """Pins the ordering the fix depends on.

    If a future edit hoists the open back above `urlopen`, this fails -- which is
    the point, since the leak is invisible on POSIX until something counts fds.
    """
    dest = tmp_path / "corpus.json"
    seen: dict[str, bool] = {}

    def boom(*_a: object, **_k: object):
        seen["part_existed"] = (tmp_path / "corpus.json.part").exists()
        raise urllib.error.URLError("refused")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(CorpusFetchError):
        _download("https://x.invalid/c.json", dest)
    assert seen["part_existed"] is False

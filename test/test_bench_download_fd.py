"""The `_download` descriptor leak, tested by the thing it actually leaks.

The bug: `open_write_nofollow` was evaluated BEFORE `urlopen` in the same `with`
header, so a URL error left the raw fd unowned -- `os.fdopen(fd)` never ran,
because evaluating the header is what raised.

Its loudest symptom is Windows-only (the surviving handle makes `tmp.unlink()`
raise `PermissionError [WinError 32]`, so the `CorpusFetchError` the handler
exists to raise never arrives), and CI's Windows shard caught it. But the leak
itself is platform-independent.

`test_a_failed_download_leaks_no_descriptor` does not count PROCESS-WIDE
descriptors via `/proc/self/fd`: under `-n auto` the worker process has 10+
live background threads (executors, the SEL writer, the embedding inference
pool) opening and closing descriptors of their own concurrently, so a
process-wide count is not deterministic -- it flakes independently of whether
`_download` itself leaked anything. The test asserts the module's OWN
open/close pairing instead: spy `open_write_nofollow` (the one primitive
`_download` uses to acquire a raw fd) and `os.fdopen` (which takes ownership
of it), and assert that when `urlopen` fails, `open_write_nofollow` is never
even reached -- so no fd is opened at all for `_download` to leak. That is
also a stronger assertion than a fd census: it encodes the exact ordering
invariant the module docstring above describes (the response must be
acquired before the staging fd is opened), not merely "the process didn't
gain a descriptor" (which a census can miss if something else in the worker
happens to close one at the same moment).
"""

from __future__ import annotations

import urllib.error
from pathlib import Path

import pytest

from kiro_crew.eval.bench import datasets as _datasets
from kiro_crew.eval.bench.datasets import CorpusFetchError, _download


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

    opened: list[int] = []
    real_open_write_nofollow = _datasets.open_write_nofollow

    def tracking_open_write_nofollow(*args: object, **kwargs: object) -> int:
        fd = real_open_write_nofollow(*args, **kwargs)  # type: ignore[arg-type]
        opened.append(fd)
        return fd

    monkeypatch.setattr(_datasets, "open_write_nofollow", tracking_open_write_nofollow)

    for _ in range(5):
        # Repeated so a single leaked descriptor is unambiguous rather than lost in
        # the noise of an unrelated allocation.
        with pytest.raises(CorpusFetchError):
            _download("https://x.invalid/c.json", dest)

    # urlopen fails before the staging fd's open is ever reached (see the module
    # docstring's ordering argument), so nothing was opened for `_download` to
    # leak. If a future edit hoists the open back above `urlopen`, `opened`
    # gains an entry here and this assertion catches it directly, rather than
    # relying on a process-wide census to notice the fd it left behind.
    assert opened == [], f"open_write_nofollow was reached despite urlopen failing: {opened}"


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

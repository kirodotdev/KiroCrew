"""Assign whole files to CI shards before pytest imports their test modules.

Leave discovery to pytest: testpaths, filename patterns, conftest ignores and
explicit targets retain their normal semantics. Only the owning shard collects
a file's items. Unlike pytest-split, other shards never import that file merely
to discard its tests. Directory/conftest imports and imports made by tests are
still shared costs; this is not a promise of a particular wall-clock speedup.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest


def file_shard(path: Path, root: Path, count: int) -> int:
    """Return a stable, one-based owner, independent of checkout path and OS."""
    relative = path.relative_to(root).as_posix()
    digest = hashlib.sha256(relative.encode("utf-8", errors="surrogatepass")).digest()
    return int.from_bytes(digest[:8], "big") % count + 1


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("CI file sharding")
    group.addoption("--file-shards", type=int, default=None, help="Number of CI file shards")
    group.addoption("--file-shard", type=int, default=None, help="One-based CI file shard")


def pytest_configure(config: pytest.Config) -> None:
    count = config.getoption("file_shards")
    index = config.getoption("file_shard")
    if count is None and index is None:
        return
    if count is None or index is None or not 1 <= index <= count:
        raise pytest.UsageError("file sharding requires 1 <= --file-shard <= --file-shards")
    if config.getoption("splits", default=None):
        raise pytest.UsageError("file sharding cannot be combined with --splits")
    config.pluginmanager.register(FileShards(config.rootpath, count, index), "ci-file-shards")


class FileShards:
    """Filter File collectors, never directory traversal or individual items.

    Using the collect-report hook rather than replacing config.args matters:
    explicit file arguments bypass pytest's collect_ignore rules. At this seam,
    normal discovery has already applied those rules, but Module.collect has
    not yet imported the file. This also works for explicit reduced-scope files,
    non-Python File collectors, and each xdist worker's independent collection.
    """

    def __init__(self, root: Path, count: int, index: int) -> None:
        self.root = root
        self.count = count
        self.index = index

    @pytest.hookimpl(tryfirst=True)
    def pytest_make_collect_report(
        self, collector: pytest.Collector
    ) -> pytest.CollectReport | None:
        if not isinstance(collector, pytest.File):
            return None
        try:
            owner = file_shard(collector.path, self.root, self.count)
        except ValueError as exc:
            raise pytest.UsageError("file-sharded targets must be inside pytest's rootdir") from exc
        if owner == self.index:
            return None
        # Empty collection, not a skipped test: this file is run by its owner.
        # Keep pytest's ordinary exit 5 if the selected shard collects no tests;
        # an empty or broken selection must never silently fall back to all tests.
        return pytest.CollectReport(collector.nodeid, "passed", None, [])

    def pytest_report_header(self) -> str:
        return f"CI file shard {self.index}/{self.count}: whole files, assigned before import"

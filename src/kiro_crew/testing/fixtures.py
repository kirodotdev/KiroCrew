"""Test helpers for consumers that need a seeded ``$KIROCREW_HOME``.

Two entry points for the same underlying behaviour:

1. ``seeded_home(fixture_name)`` — plain context manager. Use from any
   test harness (unittest, nose, hand-rolled scripts) or from production
   code that wants a throwaway populated home. Yields the ``Path`` to a
   freshly-created tempdir pre-loaded with the named fixture; restores
   the previous ``KIROCREW_HOME`` env var on exit.
2. ``seeded_home_fixture`` — pytest fixture wrapping (1). Parameterised
   over fixture name via indirect request; default is ``"empty"``.
   Only defined when pytest is importable; non-pytest consumers use
   :func:`seeded_home` directly. Auto-discovered by tests that add
   ``kiro_crew.testing.fixtures`` to their ``conftest.py`` via
   ``pytest_plugins`` or import it directly.

Rationale for two entry points: the plain context manager keeps the
helper usable outside pytest (the PRD explicitly calls out scripts and
non-pytest harnesses). The pytest fixture makes the common case a
one-liner.

Also here, pytest-only like (2): ``one_pool_thread``, which gives the test
loop a one-thread default executor.

SQLite deferral — see ``tests_fixtures/rich/README.md``. The helper
treats fixtures as opaque directory trees, so once the SQLite builder
lands the helper itself needs no change.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# ``pytest`` is OPTIONAL: ``kiro_crew.testing`` ships in the runtime wheel so any
# test harness can use ``seeded_home``. Only ``seeded_home_fixture`` needs pytest,
# so it (and its ``__all__`` entry) exists only when pytest imports.
try:
    import pytest
except ImportError:  # pragma: no cover — covered indirectly by import tests.
    pytest = None  # type: ignore[assignment]

from kiro_crew.seed import seed

__all__ = ["seeded_home"]
if pytest is not None:
    __all__ += ["one_pool_thread", "seeded_home_fixture"]


def _retry_readonly_removal(func: object, path: str, _exc_info: object) -> None:
    """Let ``rmtree`` remove fixture files copied read-only on Windows."""
    os.chmod(path, stat.S_IWRITE)
    func(path)  # type: ignore[operator]


@contextmanager
def seeded_home(fixture_name: str) -> Iterator[Path]:
    """Context manager yielding a populated ``$KIROCREW_HOME`` tempdir.

    Points ``KIROCREW_HOME`` at a fresh temp directory and runs
    ``seed(fixture_name)``; on exit restores the previous value (or unsets it)
    and removes the tempdir.

    Propagates ``SeedError``: ``fixture_name`` is still validated (unknown
    fixture, path traversal), so this helper cannot bypass the seed guardrails.

    Example:
        with seeded_home("minimal") as home:
            assert (home / "fixture.yaml").is_file()
            # agent code under test reads $KIROCREW_HOME here
    """
    previous = os.environ.get("KIROCREW_HOME")
    tmpdir = Path(tempfile.mkdtemp(prefix="kirocrew-seeded-home-"))
    try:
        # ``mkdtemp`` returns an empty dir, so seed's non-empty guardrail
        # cannot fire; seed rmdirs it and lets ``copytree`` recreate it.
        os.environ["KIROCREW_HOME"] = str(tmpdir)
        seed(fixture_name)
        yield tmpdir
    finally:
        # Restore the env var first so a cleanup failure still unblocks
        # later tests. The test may already have removed ``tmpdir``.
        if previous is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = previous
        if tmpdir.exists():
            # Plain rmtree is safe: ``mkdtemp`` never returns a symlink.
            shutil.rmtree(tmpdir, onerror=_retry_readonly_removal)


if pytest is not None:

    @pytest.fixture
    def seeded_home_fixture(request: "pytest.FixtureRequest") -> Iterator[Path]:
        """Pytest fixture wrapping :func:`seeded_home` (scope=function).

        Defined only when pytest is importable. Parameterise via
        ``indirect=["seeded_home_fixture"]``:

            @pytest.mark.parametrize(
                "seeded_home_fixture", ["minimal", "rich"], indirect=True
            )
            def test_something(seeded_home_fixture: Path) -> None:
                ...

        Without ``indirect`` it seeds ``"empty"``. Function scope gives every
        test a fresh tempdir, so no state crosses tests.
        """
        fixture_name = getattr(request, "param", "empty")
        with seeded_home(fixture_name) as home:
            yield home

    @pytest.fixture
    def one_pool_thread(event_loop: asyncio.AbstractEventLoop) -> None:
        """Run the test on a loop whose default executor has ONE thread.

        For code under test that writes on the default executor. A wait parked
        on that executor (``asyncio.to_thread(event.wait, N)``) takes the only
        thread the write needs, so with one thread such a test fails every time
        instead of under load. ``kiro_crew.testing.wait``'s ``async_wait_until``
        and ``await_until`` hold no pool thread.

        Not for a test that holds a pool thread on purpose (a patched write
        blocked on a ``threading.Event``): it needs a second thread.

        Needs pytest-asyncio's ``event_loop``; the executor is shut down with the
        loop. Use it by name (``@pytest.mark.usefixtures("one_pool_thread")``)
        after importing it into the test module or a conftest.
        """
        # Imported here so a bare ``import kiro_crew.testing`` stays light.
        from concurrent.futures import ThreadPoolExecutor

        event_loop.set_default_executor(ThreadPoolExecutor(max_workers=1))

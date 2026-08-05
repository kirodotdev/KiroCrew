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

SQLite deferral — see ``tests_fixtures/rich/README.md``. The helper
treats fixtures as opaque directory trees, so once the SQLite builder
lands the helper itself needs no change.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# ``pytest`` is an OPTIONAL runtime dependency. ``kiro_crew.testing`` ships in
# the runtime wheel (not ``dev_requirements``) so downstream consumers can do
# ``pip install kirocrew`` and use ``seeded_home`` from any test harness
# (unittest, nose, hand-rolled scripts). A hard ``import pytest`` at module
# level would break that contract — ``seeded_home`` itself needs no pytest,
# only ``seeded_home_fixture`` does. Lazy-import with a ``None`` sentinel,
# then guard the ``@pytest.fixture`` decorator so ``seeded_home_fixture``
# only exists when pytest is available. Pytest users get the decorator;
# non-pytest users just never see it (and ``__all__`` filters it out of the
# star-import list).
try:
    import pytest
except ImportError:  # pragma: no cover — covered indirectly by import tests.
    pytest = None  # type: ignore[assignment]

from kiro_crew.seed import seed

__all__ = ["seeded_home"]
if pytest is not None:
    __all__.append("seeded_home_fixture")


@contextmanager
def seeded_home(fixture_name: str) -> Iterator[Path]:
    """Context manager yielding a populated ``$KIROCREW_HOME`` tempdir.

    On enter: creates a fresh temp directory, points ``KIROCREW_HOME`` at
    it, and runs ``seed(fixture_name)`` to populate the tree. On exit:
    restores the previous ``KIROCREW_HOME`` (or unsets if it wasn't set)
    and removes the tempdir.

    Propagates ``SeedError`` from ``seed()`` — guardrails still apply (the
    helper does NOT pass ``--seed-replace`` since the tempdir is fresh
    and empty, so the non-empty guardrail cannot fire). ``fixture_name`` is
    still validated (unknown fixture, path traversal) so callers cannot
    use this helper to bypass the seed-side guardrails.

    Example:
        with seeded_home("minimal") as home:
            assert (home / "fixture.yaml").is_file()
            # agent code under test reads $KIROCREW_HOME here
    """
    previous = os.environ.get("KIROCREW_HOME")
    tmpdir = Path(tempfile.mkdtemp(prefix="kirocrew-seeded-home-"))
    try:
        # ``seed()`` itself will create files inside tmpdir but not
        # re-create tmpdir. ``mkdtemp`` returns an empty dir, so the
        # non-empty guardrail won't fire; the empty-dir branch in ``seed()``
        # will ``rmdir`` it and let ``copytree`` recreate.
        os.environ["KIROCREW_HOME"] = str(tmpdir)
        seed(fixture_name)
        yield tmpdir
    finally:
        # Restore env var first so a cleanup failure still unblocks
        # follow-up tests. ``tmpdir`` may have been removed already
        # (e.g. test swapped it out) — ignore missing.
        if previous is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = previous
        if tmpdir.exists():
            # Use shutil.rmtree — tmpdir holds the seeded tree, not a
            # symlink we need to preserve. The seed-side SYMLINK guardrails
            # don't apply here because we created tmpdir ourselves via
            # ``mkdtemp`` which never returns a symlink.
            shutil.rmtree(tmpdir, ignore_errors=True)


if pytest is not None:

    @pytest.fixture
    def seeded_home_fixture(request: "pytest.FixtureRequest") -> Iterator[Path]:
        """Pytest fixture wrapping :func:`seeded_home` (scope=function).

        Defined only when pytest is importable — ``kiro_crew.testing`` is in
        the runtime wheel and must stay importable without pytest installed.
        Non-pytest consumers see no ``seeded_home_fixture`` symbol and use
        :func:`seeded_home` directly.

        Parameterise via ``indirect=["seeded_home_fixture"]``:

            @pytest.mark.parametrize(
                "seeded_home_fixture", ["minimal", "rich"], indirect=True
            )
            def test_something(seeded_home_fixture: Path) -> None:
                ...

        Defaults to the ``empty`` fixture when called without a parameter.
        The param is read from ``request.param`` so a direct invocation
        without ``indirect`` falls back to ``"empty"``. Scope is the
        pytest default (``function``) so the seeded tempdir is created
        fresh for every test that depends on it — no cross-test state.
        """
        fixture_name = getattr(request, "param", "empty")
        with seeded_home(fixture_name) as home:
            yield home

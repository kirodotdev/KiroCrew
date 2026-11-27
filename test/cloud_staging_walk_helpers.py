"""Keep the host's own directories out of the source-tarball staging walk.

``cloud/source.py`` refuses to stage a tarball under a directory another account
could replace, and for a data home outside the operator's ``$HOME`` it walks the
whole chain up to ``/``. A test's data home sits under ``tmp_path``, so that walk
also judges every directory ABOVE the pytest base temp: ``/``, the temp root and
whatever ``TMPDIR`` names. Those belong to the host. Inside a user namespace (a
container, or a Kiro Crew agent sandbox) ``/`` reads as the overflow uid 65534,
and with ``TMPDIR`` outside ``$HOME`` every staging test then fails on ``/``
instead of on the directory it planted. A CI runner's ``/`` is root's, so the
same tests pass there.

Import the fixture by name and request it with ``pytest.mark.usefixtures``.
"""

from __future__ import annotations

import pytest

from kiro_crew.cloud import source

#: The walk as production defines it, for the tests that pin the walk itself.
real_chain_the_launcher_owns = source._chain_the_launcher_owns


@pytest.fixture
def host_directories_out_of_the_staging_walk(_floor_monkeypatch, tmp_path_factory):
    """Drop the strict ancestors of the pytest base temp from the POSIX staging walk.

    Everything a test plants, the base temp included, is still walked and judged
    by the real policy. Only the host's directories above it leave the walk, so a
    refusal a test expects names the directory it planted on every host.
    """
    host_directories = set(tmp_path_factory.getbasetemp().resolve().parents)

    def _chain_without_the_host(base):
        return [node for node in real_chain_the_launcher_owns(base) if node not in host_directories]

    _floor_monkeypatch.setattr(source, "_chain_the_launcher_owns", _chain_without_the_host)

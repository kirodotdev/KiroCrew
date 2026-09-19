"""Shared gate for Project tests whose Git remote is a local path.

``GitProjectStore._validate_remote`` refuses every drive-letter spelling of a
local remote, and ``test_project_bundle_gate`` pins that refusal: ``urlsplit``
reads ``C:\\repos\\bundle``'s drive as the URL scheme, and ``file://C:\\...``
reads it as a remote host, so neither names a local repository. Windows also has
no sandbox backend Kiro Crew can enforce, which every Project Git operation
requires. A test that builds its remote out of ``tmp_path`` therefore describes
behaviour reachable only where such a remote is valid at all, and it is skipped
there rather than relaxing the validation it would otherwise fail against.

Gate a whole module with ``pytestmark = requires_local_git_remote``, or one test
with ``@requires_local_git_remote``.
"""

from __future__ import annotations

import sys

import pytest

SKIP_REASON = (
    "Project git operations require an enforcing sandbox backend; Windows has "
    "none and drive-letter file remotes are invalid"
)


def local_git_remotes_supported() -> bool:
    """Whether a Git remote built from ``tmp_path`` can be a valid remote here."""
    return sys.platform != "win32"


def skip_unless_local_git_remotes() -> None:
    """Skip the calling test where a ``tmp_path`` remote is invalid by design."""
    if not local_git_remotes_supported():
        pytest.skip(SKIP_REASON)


@pytest.fixture
def local_git_remote() -> None:
    """Read the platform gate at SETUP time, not at module import.

    A fixture rather than a boolean ``skipif`` so the predicate is a call the
    test suite can patch and assert on, instead of a value frozen while the test
    module was imported.
    """
    skip_unless_local_git_remotes()


requires_local_git_remote = pytest.mark.usefixtures("local_git_remote")

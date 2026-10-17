"""The platform mark, staging keyword and symlink helper the pinned staging tests share.

``test_pinned_staging.py`` and ``test_pinned_staging_skips_before_delete.py`` import what
they use from here by name: ``pinned_only``, the skip for a host that cannot pin a tree
walk; ``UNPINNED_OK``, the keyword a snapshot or restore passes to assert behaviour that
holds on both platforms; and ``_symlink_or_skip``, which creates a link or skips where the
host needs privilege for one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew import pinned_fs

pinned_only = pytest.mark.skipif(
    not pinned_fs.supports_pinned_tree_walk(),
    reason="requires O_DIRECTORY, O_NOFOLLOW, dir_fd and fd-listdir support (POSIX)",
)


#: Staging refuses by default where a directory cannot be pinned, so a test that drives
#: a real snapshot or restore has to say which platform contract it is asserting. Tests
#: that assert product behaviour true on BOTH platforms pass this and exercise the
#: declared by-name path on Windows; tests that assert the pinning itself use
#: `pinned_only` instead.
UNPINNED_OK = {"allow_unpinned": True}


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
        pytest.skip("creating a symlink needs privilege on this host")

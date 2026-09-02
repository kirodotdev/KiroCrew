"""Pins the credential dot-dir denylist to ONE shared owner (#6613).

`design_critique` (local render target) and `design_tweak` (previewed project
folder) each screen operator-picked paths against a set of credential dot-dirs
that the `is_sensitive_path()` floor does not enumerate. That set is owned by
:data:`kiro_crew.security.DENIED_ROOT_PARTS`; both apps must consume the SAME
object — identity, not equality — so a credential directory added for one app
can never silently miss the other.
"""

from __future__ import annotations

from kiro_crew.apps.builtins.design_critique.backend import routes
from kiro_crew.apps.builtins.design_tweak.backend import server
from kiro_crew.security import DENIED_ROOT_PARTS


class TestDeniedRootPartsSharedOwner:
    def test_design_critique_consumes_the_shared_object(self):
        # `is`, not `==`: a byte-identical local copy would pass equality while
        # re-creating exactly the drift this refactor removes.
        assert routes._DENIED_ROOT_PARTS is DENIED_ROOT_PARTS

    def test_design_tweak_consumes_the_shared_object(self):
        assert server._DENIED_ROOT_PARTS is DENIED_ROOT_PARTS

    def test_project_secret_dirs_derives_from_the_shared_set(self):
        # The static-file-serving guard is a strict superset: every shared entry
        # plus the preview-specific extras. Pinning the exact membership keeps
        # this refactor behavior-preserving; the subset assertion keeps a future
        # DENIED_ROOT_PARTS addition from silently missing this guard.
        assert DENIED_ROOT_PARTS <= server._PROJECT_SECRET_DIRS
        assert server._PROJECT_SECRET_DIRS == DENIED_ROOT_PARTS | {
            ".git",
            ".hg",
            ".svn",
            ".gpg",
            ".azure",
        }

    def test_owner_is_an_immutable_frozenset_of_dot_dirs(self):
        # The guard's semantics depend on immutability (no consumer can mutate
        # the shared set) and on every entry being a dot-dir path *part*.
        assert isinstance(DENIED_ROOT_PARTS, frozenset)
        assert DENIED_ROOT_PARTS  # never empty — an empty set silently disables the guard
        for part in DENIED_ROOT_PARTS:
            assert part.startswith("."), part
            assert "/" not in part and "\\" not in part, part

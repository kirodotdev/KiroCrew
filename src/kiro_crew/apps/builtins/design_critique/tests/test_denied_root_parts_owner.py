"""The local-target guard screens against the core's ONE credential dot-dir denylist.

``_DENIED_ROOT_PARTS`` must be :data:`kiro_crew.security.DENIED_ROOT_PARTS` itself —
identity, not equality — so a credential directory the core adds for any app is
denied here too. ``test/test_denied_root_parts_shared.py`` pins the owner's own shape.
"""

from __future__ import annotations

from kiro_crew.apps.builtins.design_critique.backend import routes
from kiro_crew.security import DENIED_ROOT_PARTS


def test_the_local_target_guard_consumes_the_shared_object() -> None:
    # `is`, not `==`: a byte-identical local copy would pass equality while
    # re-creating exactly the drift the shared owner removes.
    assert routes._DENIED_ROOT_PARTS is DENIED_ROOT_PARTS

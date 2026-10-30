"""Which edition this build is: ``"oss"`` or ``"enterprise"``.

The value is fixed when the package is BUILT, never at run time. The checked-in
source always says ``"oss"``; ``setup.py``'s ``build_py`` step rewrites the
``EDITION = ...`` line in the built copy from ``KIROCREW_BUILD_EDITION``
(``make wheel|backend-bin|desktop EDITION=enterprise``), and refuses a value
outside ``EDITIONS``. Runtime code reads only this module: no environment
variable or config file can change the edition of an installed build.

Read it through ``is_enterprise()`` (or ``kiro_crew.edition.EDITION``) at call
time, not with ``from kiro_crew.edition import EDITION``: a copied binding
ignores the test hook. To exercise the enterprise branch in a test::

    monkeypatch.setattr("kiro_crew.edition.EDITION", "enterprise")
"""

from __future__ import annotations

EDITIONS = frozenset({"oss", "enterprise"})

# Rewritten in the built copy by setup.py; keep it one line, a plain string.
EDITION = "oss"


def is_enterprise() -> bool:
    """Return True when this build is the enterprise edition."""
    return EDITION == "enterprise"

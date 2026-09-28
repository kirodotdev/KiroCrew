"""Re-export shim for :class:`StubProfile` — the no-op profile behind ``--dry-run``.

The class itself lives in :mod:`.profile` next to the field-alias base it derives
from, but ``driver._run_dry`` imports it from ``.stub_profile`` (as upstream did,
where it was its own module). Without this shim that import raises ImportError and
``--dry-run`` dies on entry — the whole smoke-test path was unreachable, and no test
covered it, so the suite stayed green while the feature was broken.

Kept as a shim rather than moving the class: ``StubProfile`` shares
``ProfileFieldAliases`` and the contract wiring with the real profile, and splitting
it out would either duplicate that or invert the dependency.
"""

from __future__ import annotations

from .profile import StubProfile

__all__ = ["StubProfile"]

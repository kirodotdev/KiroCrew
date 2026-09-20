"""Base directory for fixtures that need a SHORT, low-entropy temp dir.

Two independent constraints push a handful of fixtures off ``tmp_path``:

- a filesystem path that ends up asserted in message metadata must not trip
  ``redact_credentials()``, and a macOS ``tmp_path`` carries high-entropy
  directory ids that do;
- an ``AF_UNIX`` ``sun_path`` is capped at 104 bytes on macOS, which a
  ``tmp_path`` under xdist exceeds.

``/tmp`` satisfies both on POSIX. On Windows it is not a path at all: it resolves
against the current drive as ``<drive>\\tmp``, and ``mkdtemp`` does not create its
``dir`` argument, so the call raises ``FileNotFoundError`` unless something
unrelated happened to create that directory first. That made the Windows shards
pass or fail on runner state rather than on the code under test.

Windows therefore uses the platform temp base, which carries neither constraint:
it has no random path component, and ``AF_UNIX`` is not in play there.
"""

from __future__ import annotations

from kiro_crew import platform_compat

#: ``prefix=`` for every ``mkdtemp`` that takes :func:`short_tmp_base`.
#:
#: A short-rooted dir sits in the SHARED ``/tmp``, outside the per-test root the conftest
#: floor pins, so a hygiene probe cannot tell one from a test leaking into the host unless
#: its name says whose it is. The stdlib default name is what an accidental bare
#: ``mkdtemp()`` also produces, so a probe that sanctions it is blind to exactly the leaks
#: it exists to catch; a per-fixture name instead forces the probe to carry a list of
#: invented prefixes, which is wrong the moment a fixture is added.
#:
#: One prefix for all of them means one anchored stem in ``probe_plugin`` and
#: ``host_snapshot``, and nothing to keep in sync. Keep a label after it
#: (``kc-tmp-pod-``) to say which fixture: the stem matches the leading component.
SHORT_TMP_PREFIX = "kc-tmp-"


def short_tmp_base() -> str | None:
    """``dir=`` for ``mkdtemp``: ``/tmp`` on POSIX, platform default on Windows.

    ``None`` makes ``mkdtemp`` fall back to ``tempfile.gettempdir()``, which
    exists by construction.
    """
    return None if platform_compat.IS_WINDOWS else "/tmp"

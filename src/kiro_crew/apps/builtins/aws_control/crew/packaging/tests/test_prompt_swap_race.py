"""A prompt file swapped for a link AFTER the fences pass must not be read.

``_resolve_prompt_path`` applies every prompt fence -- pseudo-filesystem, the repo's
sensitive-path predicate, the credential name and location checks -- against a PATH,
and then the caller opened that path again. The agents directory is writable, so the
entry can become a link to a credential file in between, and the bundle would carry
the target's bytes with every fence reporting a pass.

The reader now opens once with ``O_NOFOLLOW`` and reads from that descriptor, so the
checks are binding rather than advisory. These tests stage the swap directly.
"""

from __future__ import annotations

import os

import pytest

from .test_producer import load_build

# The refusal is ``O_NOFOLLOW``, which Windows does not have -- so on a platform
# without it there is no descriptor-level refusal to assert and these three tests
# would be asserting a guarantee the code cannot make. Skipped rather than weakened,
# because a test that passes by asserting less is worse than one that says why it did
# not run. The two tests below the marker are platform-independent and still run.
#
# This is also the marker that was MISSING when five of these went red on the Windows
# shard: the reader guarded ``O_NOFOLLOW`` with getattr but not ``O_NONBLOCK``, so it
# raised AttributeError before reaching any behaviour worth testing.
_needs_nofollow = pytest.mark.skipif(
    not hasattr(os, "O_NOFOLLOW"),
    reason="O_NOFOLLOW is POSIX-only; there is no descriptor-level refusal to assert",
)


def test_the_flag_set_is_guarded_on_every_platform():
    """Both constants must be getattr'd, not just one.

    ``O_NOFOLLOW`` was guarded and ``O_NONBLOCK`` was not, on the same line. On
    Windows that raised AttributeError before any check ran, so the reader failed
    where it was meant to be strict. Asserted by VALUE rather than by reading the
    source: on a platform missing both, the flag set is 0 and the module still
    imports, which is the property that broke.
    """
    mod = load_build()
    assert isinstance(mod._NOFOLLOW_READ_FLAGS, int)
    expected = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    assert mod._NOFOLLOW_READ_FLAGS == expected

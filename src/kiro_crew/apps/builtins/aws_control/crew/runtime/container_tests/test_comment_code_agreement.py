"""Two comments that had drifted from the code beside them, and a guard against a third.

Both were flagged by review, and both had the same history: they described an earlier
version of the line under them and stopped being true when that line changed. A comment
that contradicts its own code is worse than no comment, because a reader who trusts it
reasons from a premise the program does not hold.

* ``front/app.py`` said the control header's name was "not pinned by the shared base",
  directly above ``CONTROL_SECRET_HEADER = common.CONTROL_SECRET_HEADER``.
* ``front/transcript.py`` said the backup layout was "not ours to import", directly above
  an import of it. The key derivation IS shared, by design: one definition serves both the
  writer and the reader, because a disagreement between them is invisible in both
  directions -- a GET simply misses, and a customer whose history was not found looks
  exactly like a new customer.

Neither code line was wrong. The header alias is the better choice than a second copy, and
so is the shared key derivation. So the comments are corrected to the code rather than the
other way round.

These tests are the cheap half of keeping them honest. They cannot check that prose is
accurate, only that the specific claims which HAD drifted cannot come back while the code
they contradict is still there.

The guard is a plain substring match and it caught the first attempt at this file: the
replacement comments explained their own history by QUOTING the retired phrase, which the
match cannot tell from asserting it. The comments were reworded to describe the old claim
instead of repeating it, rather than teaching the guard to allow a quoted form -- an
exception for "in quotes" is an exception a future edit can sit inside.
"""

from __future__ import annotations

import pathlib

from container import common
from container.common import keys
from container.front import app as app_mod
from container.front import transcript as transcript_mod

from ._settings_helper import make_settings

APP_SOURCE = pathlib.Path(app_mod.__file__)
TRANSCRIPT_SOURCE = pathlib.Path(transcript_mod.__file__)


def test_the_control_header_has_one_definition() -> None:
    """The alias must stay an alias, so the deploy integration has one name to match."""
    assert app_mod.CONTROL_SECRET_HEADER is common.CONTROL_SECRET_HEADER


def test_the_header_comment_does_not_deny_the_shared_definition() -> None:
    """The retired claim must not return while the line below it is an alias.

    Keyed on the phrase that was wrong rather than on the whole comment: pinning the exact
    prose would make every rewording a test failure, which trains people to edit the test.
    """
    text = APP_SOURCE.read_text(encoding="utf-8")
    assert "not pinned by the shared base" not in text, (
        "the comment denies the shared definition again, but CONTROL_SECRET_HEADER is "
        "still an alias for common.CONTROL_SECRET_HEADER"
    )


def test_the_transcript_key_derivation_is_the_shared_one(tmp_path) -> None:
    """The front's key comes from the module the writer also reads.

    Pinned two ways, because either alone is weak. The key the front derives must equal
    the shared derivation's, and the front must hold no private copy of the parts that
    make one up: a local prefix helper is exactly how the two sides start to disagree
    while each keeps passing its own tests.
    """
    settings = make_settings(tmp_path, crew="c1", prefix="p1")
    assert transcript_mod.object_key(settings, "dashboard_s1") == keys.transcript_key(
        settings, "dashboard_s1"
    )
    for private in ("_full_key", "_sessions_prefix", "_object_prefix"):
        assert not hasattr(transcript_mod, private), (
            f"{private} is a second copy of a derivation container.common.keys already "
            "owns, and a reader that disagrees with the writer only ever misses"
        )


def test_the_transcript_comment_does_not_claim_a_backup_import() -> None:
    """No stale reference to the extracted backup layout may return.

    The old comment described importing the key scheme from the backup layout; that import
    is gone with the subsystem. Neither the import nor the retired "not ours to import"
    claim about it may reappear while the helpers live here.
    """
    text = TRANSCRIPT_SOURCE.read_text(encoding="utf-8")
    assert "from ..backup.layout import" not in text, "the extracted backup import is back"
    assert (
        "not ours to import" not in text
    ), "the retired claim about importing the layout has returned"

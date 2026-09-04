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
import pathlib

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


@_needs_nofollow
def test_a_prompt_swapped_for_a_credential_link_is_refused(tmp_path):
    mod = load_build()
    agents = tmp_path / "agents"
    agents.mkdir()
    secret = tmp_path / "credentials"
    secret.write_text("aws_secret_access_key = not-a-real-key\n", encoding="utf-8")

    prompt = agents / "persona.md"
    prompt.write_text("You are the front desk.\n", encoding="utf-8")

    # The swap, after everything that inspects the path by name has run.
    prompt.unlink()
    prompt.symlink_to(secret)

    with pytest.raises(mod.ExportRefused) as excinfo:
        mod._read_text_nofollow(prompt)
    assert "changed" in str(excinfo.value) or "plain file" in str(excinfo.value)


@_needs_nofollow
def test_the_linked_bytes_never_come_back(tmp_path):
    """The property is the bytes, not the message."""
    mod = load_build()
    secret = tmp_path / "credentials"
    secret.write_text("aws_secret_access_key = leaked-marker\n", encoding="utf-8")
    prompt = tmp_path / "persona.md"
    prompt.write_text("ok\n", encoding="utf-8")
    prompt.unlink()
    prompt.symlink_to(secret)

    try:
        text = mod._read_text_nofollow(prompt)
    except mod.ExportRefused:
        text = ""
    assert "leaked-marker" not in (text or "")


def test_a_missing_prompt_still_says_so(tmp_path):
    """The distinct 'does not exist' refusal must survive the rewrite."""
    mod = load_build()
    with pytest.raises(mod.ExportRefused, match="does not exist"):
        mod._read_text_nofollow(tmp_path / "nope.md")


def test_a_real_prompt_reads_unchanged(tmp_path):
    mod = load_build()
    prompt = tmp_path / "persona.md"
    body = "You are the front desk.\n" * 4000  # spans the read loop
    # write_BYTES, not write_text. On Windows write_text goes through text mode and
    # stores "\n" as "\r\n", while this reader is deliberately byte-exact -- so the
    # comparison failed on the Windows shard against correct code. The fix belongs
    # here: teaching the reader to fold newlines would destroy the property it exists
    # to have, which is returning the file's bytes. The other writes in this file are
    # unaffected because none of them compares content byte for byte.
    prompt.write_bytes(body.encode("utf-8"))
    assert mod._read_text_nofollow(prompt) == body


def test_undecodable_content_returns_none_not_a_refusal(tmp_path):
    """The caller distinguishes 'not text' from 'not allowed'; keep that split."""
    mod = load_build()
    p = tmp_path / "persona.md"
    p.write_bytes(b"\xff\xfe not utf-8 \x00")
    assert mod._read_text_nofollow(p) is None


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


def test_the_container_twin_agrees():
    """``sidecar.py`` keeps its own copy (separate image, cannot import this one).

    Two spellings of one rule drift, so pin them equal. The duplicate is deliberate
    and explained at both sites.

    Read from SOURCE, never imported. The first version of this test did
    ``from container.backup import sidecar``, which trips
    ``test_spawn_audit.py::test_container_image_assets_are_not_imported`` -- the very
    invariant this branch spent a round restoring. That guard's point is that nothing
    outside the image may import that tree, because an import turns its unrouted
    spawns into gateway spawns the audit cannot see. A test is not exempt from it, and
    comparing two integer literals never needed an import.
    """
    sidecar_src = (
        pathlib.Path(__file__).resolve().parents[2]
        / "runtime"
        / "container"
        / "backup"
        / "sidecar.py"
    ).read_text(encoding="utf-8")
    mod = load_build()
    build_src = pathlib.Path(mod.__file__ or "").read_text(encoding="utf-8") if mod.__file__ else ""

    expr = 'getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)'
    assert (
        f"_NOFOLLOW_READ_FLAGS: int = {expr}" in sidecar_src
    ), "the container twin no longer computes the flag set the same way"
    if build_src:
        assert f"_NOFOLLOW_READ_FLAGS: int = {expr}" in build_src

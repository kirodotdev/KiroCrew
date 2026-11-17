"""The restart-restore lookup resolves a cron tab to the file its writer uses.

A cron-born tab is named ``cron-<job_id>`` while its conversation runs under the
session key ``cron:<job_id>``, whose transcript folds to ``cron_<job_id>.jsonl``.
The restore path knows a tab only by its NAME and resolves the file through
:func:`slot_transcript_key`; if that returns a key folding to a different file,
restore reads an absent transcript and silently drops the tab.

The cron rule is keyed on the minted job-id SHAPE (``cron-<eight lowercase hex>``)
rather than the ``cron-`` prefix, because the restore site has only the name and
``POST /api/chat/slots`` admits a client-chosen name. These tests pin the real
cron tab onto the writer's file, pin an ordinary ``cron-foo`` tab onto its own
``dashboard_`` file, document the residual ``cron-<hex>`` collision, and pin the
unaffected dashboard and channel cases.
"""

from __future__ import annotations

from kiro_crew.dashboard.chat_utils_slots import slot_transcript_key
from kiro_crew.history import ConversationLog
from kiro_crew.history_files import _safe_key


def test_restore_finds_cron_tab_transcript_after_restart(tmp_path):
    """Restore resolves ``cron-<id>`` to the file written under ``cron:<id>``.

    Writes a cron transcript through the live key the writer uses, then reads it
    back exactly as the restore drivers do -- ``session_mtime`` and
    ``get_metadata_status`` on ``slot_transcript_key(<slot name>)``. On the
    buggy lookup the slot name folds to ``dashboard_cron-<id>.jsonl``, which is
    absent, so both reads miss and the tab is left in history.
    """
    conv_log = ConversationLog(base_dir=tmp_path)
    job_id = "6a1db664"  # uuid4().hex[:8] shape: eight lowercase hex characters
    write_key = f"cron:{job_id}"
    slot_name = f"cron-{job_id}"

    conv_log.update_metadata(write_key, {"title": "Cron: nightly digest"})

    # The one file the writer created, named by folding the live session key.
    written = tmp_path / f"{_safe_key(write_key)}.jsonl"
    assert written.exists(), "precondition: the writer created cron_<id>.jsonl"
    # The name the buggy lookup pointed at was never written.
    phantom = tmp_path / f"dashboard_{slot_name}.jsonl"
    assert not phantom.exists()

    resolved = slot_transcript_key(slot_name)
    assert resolved == write_key
    assert conv_log.session_mtime(resolved) is not None
    meta, readable = conv_log.get_metadata_status(resolved)
    assert readable is True
    assert meta.get("title") == "Cron: nightly digest"


def test_user_slot_named_cron_foo_restores_from_own_file(tmp_path):
    """A client tab named ``cron-foo`` is NOT mistaken for a cron transcript.

    ``POST /api/chat/slots`` admits a client-chosen name; one that starts with
    ``cron-`` but is not the minted job-id shape must keep resolving to its own
    ``dashboard_cron-foo.jsonl`` -- the file the write path gives an unbound
    dashboard slot -- so the cron rule stays scoped to real cron tabs.
    """
    conv_log = ConversationLog(base_dir=tmp_path)
    slot_name = "cron-foo"  # not eight hex: an ordinary dashboard tab name

    resolved = slot_transcript_key(slot_name)
    assert resolved == f"dashboard:{slot_name}"

    conv_log.update_metadata(resolved, {"title": "My cron notes"})
    own = tmp_path / f"dashboard_{slot_name}.jsonl"
    assert own.exists()
    assert conv_log.session_mtime(resolved) is not None


def test_user_slot_named_like_a_job_id_resolves_as_cron(tmp_path):
    """The residual collision, pinned so it is a known, documented outcome.

    A client tab whose name happens to match the minted job-id shape exactly
    (``cron-<eight lowercase hex>``) is indistinguishable from a real cron tab at
    the restore site, which holds only the name. It resolves to the cron
    transcript, not ``dashboard_cron-<hex>.jsonl``. This is the same name-shape
    tradeoff the restore read path already makes for channel slots; the PR body
    states it. The test fixes the behaviour rather than asserting it cannot
    happen.
    """
    assert slot_transcript_key("cron-1a2b3c4d") == "cron:1a2b3c4d"
    # Upper-case hex is not a minted id, so it is left on the dashboard path.
    assert slot_transcript_key("cron-1A2B3C4D") == "dashboard:cron-1A2B3C4D"
    # A longer or shorter run of hex is not the id shape either.
    assert slot_transcript_key("cron-1a2b3c4") == "dashboard:cron-1a2b3c4"
    assert slot_transcript_key("cron-1a2b3c4d5") == "dashboard:cron-1a2b3c4d5"


def test_normal_dashboard_slot_transcript_name_unchanged(tmp_path):
    """A plain dashboard tab still resolves to its ``dashboard_<name>.jsonl``.

    The cron rule must not touch the default path: a ``chat-N-<ts>`` slot keeps
    the ``dashboard:`` prefix and its own transcript file.
    """
    conv_log = ConversationLog(base_dir=tmp_path)
    slot_name = "chat-3-1699999999"

    resolved = slot_transcript_key(slot_name)
    assert resolved == f"dashboard:{slot_name}"

    conv_log.update_metadata(resolved, {"title": "My chat"})
    expected = tmp_path / f"dashboard_{slot_name}.jsonl"
    assert expected.exists()
    assert conv_log.session_mtime(resolved) is not None


def test_channel_slot_transcript_name_unchanged():
    """A channel-born slot name is still returned as its own transcript stem."""
    slot_name = "slack_1785370133.085469"
    assert slot_transcript_key(slot_name) == slot_name

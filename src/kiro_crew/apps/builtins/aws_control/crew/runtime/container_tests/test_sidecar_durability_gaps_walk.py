"""What a cycle publishes when the transcript tree changes under its walk.

A conversation the walk cannot reach is not one the owner deleted, so a missing directory is
refused rather than read as a deletion, and the listing goes through the descriptor the check
examined rather than a second lookup by name. A conversation deleted during the cycle must not
leave the authority pair naming bytes the bucket never held, and an index the cycle cannot
read, or one over its entry or byte caps, is refused rather than guessed at. Items 4 and 5 of
the list in ``test_sidecar_durability_gaps.py`` are the same walk's other refusals.
"""

from __future__ import annotations

import json
import shutil

import pytest
from container.common import keys
from container.sidecar import backup as backup_mod

from .sidecar_durability_gaps_helpers import (
    SLOT_KEY,
    STEM,
    _authority_keys_in,
    _Recorder,
    _settings,
    _transcript,
)

# --- 13. an unreachable tree is not a deleted conversation ------------------------


def test_a_missing_directory_is_refused_rather_than_read_as_a_deletion(tmp_path):
    """``gone`` means the owner deleted a conversation, not that the tree went away.

    One errno covers both: the walk down to a transcript meets ENOENT whether the leaf was
    unlinked or a directory above it vanished -- an unmounted data home, a removed archive
    directory -- and the bytes behind that directory may be perfectly live. Recorded as a
    deletion it raises nothing, so the cycle publishes an index for conversations it never
    looked at and the front then serves empty history for every one of them.
    """
    settings = _settings(tmp_path)
    path = _transcript(settings, b"a turn\n")
    plan = backup_mod.objects_to_back_up(settings)
    result = backup_mod.CycleResult()
    shutil.rmtree(path.parent)
    try:
        backup_mod._upload_phase(
            plan.data,
            settings=settings,
            store=_Recorder(),
            state={},
            result=result,
            committed_index={},
        )
    finally:
        plan.close_authority()

    assert result.gone == []
    assert [name for name, _why in result.refused] == [path.name]
    assert not result.complete


def test_an_unlinked_leaf_is_still_a_deletion_rather_than_a_refusal(tmp_path):
    """The over-strict direction: an owner deleting a conversation is not a failure.

    This is the case the distinction exists to keep. Refusing here would fail every cycle
    that raced a deletion, which is a routine thing for a customer to do.
    """
    settings = _settings(tmp_path)
    path = _transcript(settings, b"a turn\n")
    plan = backup_mod.objects_to_back_up(settings)
    result = backup_mod.CycleResult()
    path.unlink()
    try:
        backup_mod._upload_phase(
            plan.data,
            settings=settings,
            store=_Recorder(),
            state={},
            result=result,
            committed_index={},
        )
    finally:
        plan.close_authority()

    assert result.gone == [path.name]
    assert result.refused == []
    assert result.complete


def test_the_sessions_root_is_listed_through_the_descriptor_it_checked(tmp_path):
    """One directory throughout, here too. A second lookup is a second directory.

    Checking a descended descriptor and then re-resolving the root by name lets the root be
    swapped between the two, so the listing walks the very tree the check refused while the
    check reports it sound -- the refusal is worth nothing. Listing through the descriptor is
    what makes the check and the walk address one inode.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / f"stranger{keys.TRANSCRIPT_SUFFIX}").write_bytes(b"not ours\n")
    real_descend = backup_mod._descend

    def swap_the_root_after_the_check(root, parts):
        fd = real_descend(root, parts)
        # The check has passed on the real directory. It is moved aside rather than removed,
        # so the descriptor still addresses it with its entries intact, and the NAME now
        # means the stranger's tree -- exactly what a concurrent rename leaves behind.
        settings.sessions_dir.rename(tmp_path / "moved-aside")
        settings.sessions_dir.symlink_to(elsewhere, target_is_directory=True)
        return fd

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(backup_mod, "_descend", swap_the_root_after_the_check)
        found, refused = backup_mod._live_transcripts(settings)

    assert refused == []
    assert [p.name for p in found] == [
        f"{STEM}{keys.TRANSCRIPT_SUFFIX}"
    ], "the listing followed the swapped name instead of the checked descriptor"


# --- 19. a conversation that vanishes mid-cycle must not leave the index ahead of it ----
#
# The pair is captured BEFORE the enumeration, so it still names a conversation deleted
# during the cycle. The skew argument says an OLDER index is harmless -- but only because
# every slot it names already has bytes in the bucket, and a conversation created and
# deleted inside one interval was never uploaded by any cycle. Withholding here cannot
# freeze the index the way an unreachable entry would: a deleted file is not listed by the
# next cycle at all, so the verdict is a race within one cycle rather than a shape on disk.


def _vanish_after_listing(settings, monkeypatch, victim: str):
    """Delete *victim* between the listing and its open, which is the whole race."""
    real_open = backup_mod.open_snapshot

    def open_then_vanish(path, *, root):
        if path.name == victim and path.exists():
            path.unlink()
        return real_open(path, root=root)

    monkeypatch.setattr(backup_mod, "open_snapshot", open_then_vanish)


def _index_naming(settings, *slots: str) -> None:
    """Write a session map and open-slots pair that NAME *slots*, as the backend would.

    *slots* are SLOT KEYS (``cust-91``), which is what the backend writes -- NOT transcript
    stems (``dashboard_cust-91``). Passing a stem here is what made this suite agree with a
    comparison that could never match in production.
    """
    for slot in slots:
        assert not slot.startswith("dashboard_"), (
            f"{slot!r} is a transcript stem, not a slot key: the index names conversations "
            "by slot key and the file carries the prefix"
        )
    (settings.config_dir / "session_map.json").write_bytes(
        json.dumps({slot: {"sid": f"sid-{slot}"} for slot in slots}).encode()
    )
    (settings.config_dir / "open_slots.json").write_bytes(
        json.dumps({"keys": list(slots)}).encode()
    )


def test_a_gone_transcript_the_index_names_withholds_the_pair(tmp_path, monkeypatch):
    """The hole: the captured pair names it, the bucket has never held it, and it commits.

    The replacement then restores an index naming a slot that resolves to nothing, and the
    front reads that as a conversation that never had history.
    """
    settings = _settings(tmp_path)
    victim = f"{STEM}{keys.TRANSCRIPT_SUFFIX}"
    _transcript(settings, b"a turn\n")
    _index_naming(settings, SLOT_KEY)
    _vanish_after_listing(settings, monkeypatch, victim)
    store = _Recorder()

    with pytest.raises(backup_mod.BackupIncomplete) as caught:
        backup_mod.run_cycle(settings, store, state={})

    result = caught.value.result
    assert result.gone_referenced == [victim]
    assert not result.complete
    assert set(result.withheld) == _authority_keys_in(settings, result.withheld)
    assert len(result.withheld) == len(keys.AUTHORITY_NAMES)
    assert not any(key.endswith("generation.json") for key in store.objects)
    assert not any("session_map.json" in key for key in store.objects)
    assert "captured index still named it" in str(caught.value)


def test_a_gone_transcript_the_index_does_not_name_still_publishes(tmp_path, monkeypatch):
    """The routine case, and the reason membership is the test rather than the deletion.

    An owner deleting a conversation is ordinary use. If the captured index does not name
    it, the published pair cannot send a reader to it, so withholding would cost every
    other conversation its index update and protect nothing.
    """
    settings = _settings(tmp_path)
    victim = f"{STEM}{keys.TRANSCRIPT_SUFFIX}"
    _transcript(settings, b"a turn\n")
    _index_naming(settings, "some-other-conversation")
    _vanish_after_listing(settings, monkeypatch, victim)
    store = _Recorder()

    result = backup_mod.run_cycle(settings, store, state={})

    assert result.gone == [victim]
    assert result.gone_undurable == [(victim, victim)]
    assert result.gone_referenced == []
    assert result.complete
    assert result.withheld == []
    assert len(_authority_keys_in(settings, store.objects)) == len(keys.AUTHORITY_NAMES)


def test_a_gone_transcript_the_bucket_already_holds_still_publishes(tmp_path, monkeypatch):
    """The harmless skew, which must stay harmless: those bytes ARE in the bucket.

    An older index naming a conversation the bucket still holds resolves to real bytes, so
    the name is not a candidate at all even though the index names it.
    """
    settings = _settings(tmp_path)
    victim = f"{STEM}{keys.TRANSCRIPT_SUFFIX}"
    _transcript(settings, b"a turn\n")
    _index_naming(settings, SLOT_KEY)
    store = _Recorder()

    state: dict = {}
    first = backup_mod.run_cycle(settings, store, state=state)
    assert first.complete

    _vanish_after_listing(settings, monkeypatch, victim)
    result = backup_mod.run_cycle(settings, store, state=state)

    assert result.gone == [victim]
    assert result.gone_undurable == []
    assert result.gone_referenced == []
    assert result.complete


def test_a_captured_index_that_cannot_be_read_withholds_rather_than_assuming_empty(
    tmp_path, monkeypatch
):
    """Unknown is not "names nothing". Guessing empty commits the one pair least checkable.

    The candidate is undurable either way, so reading the index as empty would publish an
    index this cycle could not read against bytes it knows are absent.
    """
    settings = _settings(tmp_path)
    victim = f"{STEM}{keys.TRANSCRIPT_SUFFIX}"
    _transcript(settings, b"a turn\n")
    (settings.config_dir / "session_map.json").write_bytes(b"{ not json")
    (settings.config_dir / "open_slots.json").write_bytes(b"{}")
    _vanish_after_listing(settings, monkeypatch, victim)

    with pytest.raises(backup_mod.BackupIncomplete) as caught:
        backup_mod.run_cycle(settings, _Recorder(), state={})

    assert caught.value.result.gone_referenced == [victim]
    assert not caught.value.result.complete


def test_a_gone_transcript_does_not_withhold_on_the_following_cycle(tmp_path, monkeypatch):
    """Why this is not the frozen index an unreachable entry would cause.

    A file that is really deleted is not listed by the next cycle, so the verdict cannot
    recur and the pair publishes without any intervention.
    """
    settings = _settings(tmp_path)
    victim = f"{STEM}{keys.TRANSCRIPT_SUFFIX}"
    _transcript(settings, b"a turn\n")
    _index_naming(settings, SLOT_KEY)
    store = _Recorder()
    state: dict = {}

    # Scope the race to the first cycle only: a bare undo() would also
    # revert every autouse fixture's patch and run the second cycle on host values.
    with monkeypatch.context() as mp:
        _vanish_after_listing(settings, mp, victim)
        with pytest.raises(backup_mod.BackupIncomplete):
            backup_mod.run_cycle(settings, store, state=state)
    assert not (settings.sessions_dir / victim).exists(), "the victim really is deleted"

    second = backup_mod.run_cycle(settings, store, state=state)

    assert second.gone_referenced == []
    assert second.complete
    assert len(_authority_keys_in(settings, store.objects)) == len(keys.AUTHORITY_NAMES)


def test_an_open_slot_alone_is_enough_to_name_a_conversation(tmp_path, monkeypatch):
    """Both files name conversations, so reading only the session map would miss one.

    An open tab is a slot the front will fetch on the next turn, which is exactly the read
    that must not meet an absent object.
    """
    settings = _settings(tmp_path)
    victim = f"{STEM}{keys.TRANSCRIPT_SUFFIX}"
    _transcript(settings, b"a turn\n")
    (settings.config_dir / "session_map.json").write_bytes(b"{}")
    (settings.config_dir / "open_slots.json").write_bytes(json.dumps({"keys": [SLOT_KEY]}).encode())
    _vanish_after_listing(settings, monkeypatch, victim)

    with pytest.raises(backup_mod.BackupIncomplete) as caught:
        backup_mod.run_cycle(settings, _Recorder(), state={})

    assert caught.value.result.gone_referenced == [victim]


def test_an_authority_file_that_is_simply_absent_is_not_this_case(tmp_path):
    """The over-strict direction: a first boot has no index to preserve and nothing to say.

    ``authority_gone`` is a different list from the data phase's, and reading the two as one
    would make every task that has served no turn a failing cycle.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    for name in keys.AUTHORITY_NAMES:
        (settings.config_dir / name).unlink()
    store = _Recorder()

    result = backup_mod.run_cycle(settings, store, state={})

    assert result.gone_referenced == []
    assert result.complete


def test_an_over_entry_cap_transcript_index_refuses_publication(tmp_path, monkeypatch):
    """GPT F2: an over-cap live set is refused DURING the scan; the pair is withheld, count reported.

    A slot repeatedly closed and reopened leaves transcripts behind, so the live set and the
    index it builds grow with the whole slot history. ``_live_transcripts`` bounds the listing
    AT the entry cap WHILE it scans -- it never materialises the whole directory -- and refuses
    the moment the directory holds more than the cap, withholding the authority pair so the
    last readable generation stays committed and reporting the over-count. This is the memory
    bound: enforcing the cap only downstream (on the already-materialised merged index) would
    let a pathological directory OOM the sidecar before a byte is uploaded.

    It fails if the cap is removed from the scan: the oversized listing materialises and the
    cycle reports clean (or OOMs on a real pathological directory).
    """
    monkeypatch.setattr(backup_mod, "_TRANSCRIPT_INDEX_MAX_ENTRIES", 2)
    settings = _settings(tmp_path)
    for i in range(3):  # three live transcripts, cap is two
        _transcript(settings, b"turn\n", stem=f"dashboard_cust-{i}")
    store = _Recorder()

    with pytest.raises(backup_mod.BackupIncomplete) as caught:
        backup_mod.run_cycle(settings, store, state={})

    result = caught.value.result
    assert not result.complete
    # The authority pair is withheld, not committed.
    assert set(result.withheld) == _authority_keys_in(settings, store.puts) or not any(
        _authority_keys_in(settings, store.objects)
    )
    # The refusal is raised during the scan and reports the over-count: 3 transcripts, 1 over
    # the 2-entry cap. It names the sessions directory, not the index object, because the
    # bound is now enforced before the index is ever built.
    assert any(
        "more than the 2-entry transcript cap" in why and "1 over" in why
        for _k, why in result.refused
    )


def test_an_over_byte_cap_transcript_index_refuses_publication(tmp_path, monkeypatch):
    """The serialized-SIZE cap, independent of the entry count: a restore reads with a limit.

    The restore reads the index with ``limit=MAX_OBJECT_BYTES``; an index past that is
    ``ObjectTooLarge`` -- an unreadable bucket. So publication is refused on the byte cap too,
    well under that ceiling, keeping the committed index readable.

    It fails if only the entry count is capped: a few very long stems could still serialize
    past the reader's limit and publish.
    """
    monkeypatch.setattr(backup_mod, "_TRANSCRIPT_INDEX_MAX_BYTES", 50)
    settings = _settings(tmp_path)
    _transcript(settings, b"turn\n", stem="dashboard_" + "x" * 200)
    store = _Recorder()

    with pytest.raises(backup_mod.BackupIncomplete) as caught:
        backup_mod.run_cycle(settings, store, state={})

    result = caught.value.result
    assert not result.complete
    assert any("over the" in why and "byte cap" in why for _k, why in result.refused)
    assert not _authority_keys_in(settings, store.objects)

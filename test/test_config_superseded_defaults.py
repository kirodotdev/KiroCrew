"""Reporting of stored config values that still hold a superseded default.

``config.json`` is a full materialization of the schema and the loader resolves
each field as ``data.get(key, DEFAULT)``, so a stored value always beats a
changed dataclass default. A change to ``mcp_gateway.forward_declared_env``
False->True with no migration, so every pre-existing install stayed False and
nothing said so.

These tests pin that the drift is DETECTED and REPORTED and that the stored value
is never touched. The read-only posture is the load-bearing part: the same key has
a documented escape hatch (``test_a_real_false_still_turns_it_off`` in the gateway
env suite pins that a stored ``false`` is honoured), and on disk that escape hatch
and a stale materialized default are identical, so a rewrite cannot correct one
without overriding the other.
"""

from __future__ import annotations

import json
import logging
import os

import pytest

from kiro_crew import platform_compat
from kiro_crew.config import loader as L
from kiro_crew.config import superseded_defaults as SD
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.superseded_defaults import (
    SupersededDefault,
    drift_summary,
    superseded_default_drift,
)

# The one shipped entry, resolved by key so the tests describe the real change
# rather than hard-coding a duplicate of the registry.
FDE_ENTRY = next(
    e for e in SD.SUPERSEDED_DEFAULTS if e.dotted_key == "mcp_gateway.forward_declared_env"
)

AUTOCOMPACT_ENTRY = next(
    e for e in SD.SUPERSEDED_DEFAULTS if e.dotted_key == "session.autocompact_pct"
)

LOOP_STALL_ENTRY = next(
    e for e in SD.SUPERSEDED_DEFAULTS if e.dotted_key == "dashboard.loop_stall_exit_after_secs"
)


@pytest.fixture(autouse=True)
def _forget_process_warnings():
    """The warned-keys set is process-global; each test starts from empty."""
    L._REPORTED_SUPERSEDED_KEYS.clear()
    yield
    L._REPORTED_SUPERSEDED_KEYS.clear()


def _point_home(tmp_path, monkeypatch) -> None:
    """Redirect every config path at *tmp_path* so nothing touches the real home.

    ``render_doctor_section`` resolves ``config_path`` lazily out of the loader
    module, so patching it there covers the doctor surface too. ``config_dir`` is
    what ``ack_file_path`` resolves, so the acknowledgment file lands here as well.
    """
    cfgp = tmp_path / "config.json"
    monkeypatch.setattr(L, "config_path", lambda: cfgp)
    monkeypatch.setattr(L, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(L, "config_local_path", lambda: tmp_path / "config.local.json")


def _write_config(tmp_path, data: dict) -> None:
    (tmp_path / "config.json").write_text(json.dumps(data), encoding="utf-8")
    # Same-second edits can share an mtime with the cached fingerprint, so drop
    # the process cache explicitly to force a real re-read every load().
    L._invalidate_config_cache()


def _write_local(tmp_path, data: dict) -> None:
    (tmp_path / "config.local.json").write_text(json.dumps(data), encoding="utf-8")
    L._invalidate_config_cache()


def _on_disk(tmp_path) -> dict:
    return json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Detection: pure, and precise about what counts as drift.
# --------------------------------------------------------------------------


def test_stored_old_default_is_reported_as_drift():
    base = {"mcp_gateway": {"forward_declared_env": False}}
    assert superseded_default_drift(base) == [FDE_ENTRY]
    # Detection is pure: the document is not touched.
    assert base == {"mcp_gateway": {"forward_declared_env": False}}


def test_stored_current_default_is_not_drift():
    assert superseded_default_drift({"mcp_gateway": {"forward_declared_env": True}}) == []


def test_absent_key_and_absent_section_are_not_drift():
    """An absent key already resolves to the current default, so there is nothing to say."""
    assert superseded_default_drift({"mcp_gateway": {}}) == []
    assert superseded_default_drift({}) == []
    assert KiroCrewConfig().mcp_gateway.forward_declared_env is True


def test_zero_is_not_read_as_false():
    """bool is an int subclass; a stored 0 must not be reported as the False default."""
    assert superseded_default_drift({"mcp_gateway": {"forward_declared_env": 0}}) == []


def test_malformed_registry_key_raises_loudly():
    bad = SupersededDefault(
        dotted_key="no_dot", old_default=False, new_default=True, changed_in="#0"
    )
    with pytest.raises(ValueError):
        SD._split_dotted(bad.dotted_key)


def test_drift_summary_names_value_default_and_release():
    text = drift_summary(FDE_ENTRY)
    assert "mcp_gateway.forward_declared_env" in text
    assert "#4566" in text
    # Both sides of the change are stated, so the reader can judge it themselves.
    assert "False" in text and "True" in text


# --------------------------------------------------------------------------
# The load path: reports, and never writes.
# --------------------------------------------------------------------------


def test_load_reports_drift_and_leaves_the_value_alone(tmp_path, monkeypatch, caplog):
    """The escape hatch is honoured: a stored false still resolves false.

    This is the same contract the gateway env suite pins, and the reason this
    mechanism reports instead of correcting.
    """
    _point_home(tmp_path, monkeypatch)
    _write_config(tmp_path, {"mcp_gateway": {"forward_declared_env": False}})

    with caplog.at_level(logging.WARNING, logger=L.__name__):
        cfg = KiroCrewConfig.load()

    assert cfg.mcp_gateway.forward_declared_env is False
    assert _on_disk(tmp_path)["mcp_gateway"]["forward_declared_env"] is False
    assert any("forward_declared_env" in r.getMessage() for r in caplog.records)


def test_load_warns_once_per_process_not_once_per_load(tmp_path, monkeypatch, caplog):
    """A line the operator already read is noise; doctor is the re-readable surface."""
    _point_home(tmp_path, monkeypatch)
    _write_config(tmp_path, {"mcp_gateway": {"forward_declared_env": False}})

    with caplog.at_level(logging.WARNING, logger=L.__name__):
        KiroCrewConfig.load()
        first = len([r for r in caplog.records if "forward_declared_env" in r.getMessage()])
        L._invalidate_config_cache()
        KiroCrewConfig.load()
        second = len([r for r in caplog.records if "forward_declared_env" in r.getMessage()])

    assert first == 1
    assert second == 1


def test_load_says_nothing_when_the_stored_value_is_current(tmp_path, monkeypatch, caplog):
    _point_home(tmp_path, monkeypatch)
    _write_config(tmp_path, {"mcp_gateway": {"forward_declared_env": True}})

    with caplog.at_level(logging.WARNING, logger=L.__name__):
        cfg = KiroCrewConfig.load()

    assert cfg.mcp_gateway.forward_declared_env is True
    assert not [r for r in caplog.records if "forward_declared_env" in r.getMessage()]


def test_base_drift_is_reported_even_when_an_overlay_masks_it(tmp_path, monkeypatch, caplog):
    """The base is what was materialized; an overlay hides it from the resolved view.

    Reporting on the merged document would miss exactly the case worth reporting:
    the operator removes the overlay one day and silently inherits the old value.
    """
    _point_home(tmp_path, monkeypatch)
    _write_config(tmp_path, {"mcp_gateway": {"forward_declared_env": False}})
    _write_local(tmp_path, {"mcp_gateway": {"forward_declared_env": True}})

    with caplog.at_level(logging.WARNING, logger=L.__name__):
        cfg = KiroCrewConfig.load()

    # The overlay is the operator's live choice and still wins for this load.
    assert cfg.mcp_gateway.forward_declared_env is True
    # The base drift underneath it is still reported.
    assert any("forward_declared_env" in r.getMessage() for r in caplog.records)


def test_an_overlay_only_value_is_not_reported_as_base_drift(tmp_path, monkeypatch, caplog):
    """The base has no opinion, so there is no materialized value to report on."""
    _point_home(tmp_path, monkeypatch)
    _write_config(tmp_path, {"mcp_gateway": {}})
    _write_local(tmp_path, {"mcp_gateway": {"forward_declared_env": False}})

    with caplog.at_level(logging.WARNING, logger=L.__name__):
        cfg = KiroCrewConfig.load()

    assert cfg.mcp_gateway.forward_declared_env is False
    assert not [r for r in caplog.records if "forward_declared_env" in r.getMessage()]


# --------------------------------------------------------------------------
# doctor: the durable, re-readable rendering.
# --------------------------------------------------------------------------


def test_doctor_reports_drift_without_calling_it_an_issue(tmp_path, monkeypatch, capsys):
    """Informational: this cannot tell a stale default from a deliberate opt-out."""
    _point_home(tmp_path, monkeypatch)
    _write_config(tmp_path, {"mcp_gateway": {"forward_declared_env": False}})

    issues: list[str] = []
    SD.render_doctor_section(issues)
    out = capsys.readouterr().out

    assert "Stored Defaults" in out
    assert "forward_declared_env" in out
    assert "#4566" in out
    assert issues == []


def test_doctor_says_clean_when_nothing_drifted(tmp_path, monkeypatch, capsys):
    _point_home(tmp_path, monkeypatch)
    _write_config(tmp_path, {"mcp_gateway": {"forward_declared_env": True}})

    issues: list[str] = []
    SD.render_doctor_section(issues)
    out = capsys.readouterr().out

    assert "no stored value holds a superseded default" in out
    assert issues == []


def test_doctor_handles_a_missing_config_file(tmp_path, monkeypatch, capsys):
    _point_home(tmp_path, monkeypatch)
    issues: list[str] = []
    SD.render_doctor_section(issues)
    assert "no config file yet" in capsys.readouterr().out
    assert issues == []


def test_doctor_flags_an_unreadable_config(tmp_path, monkeypatch, capsys):
    """A malformed file IS an issue -- unlike drift, it is unambiguously wrong."""
    _point_home(tmp_path, monkeypatch)
    (tmp_path / "config.json").write_text("{not json", encoding="utf-8")

    issues: list[str] = []
    SD.render_doctor_section(issues)
    assert "could not read" in capsys.readouterr().out
    assert issues == ["stored defaults unreadable"]


def test_every_registered_key_ends_at_the_live_default():
    """The NEWEST entry per key must name the default the loader actually applies.

    Both sides of an entry are history -- a later change appends a new entry
    rather than editing an old one, so the 90->70 row stays true even once the
    default moves again. What must not drift is the END of each key's chain: if
    it names a value the loader does not apply, the report tells operators to
    adopt a default that does not exist. Registry order is the append order, so
    the last entry for a key is its newest.
    """
    from dataclasses import fields as dc_fields

    newest: dict[str, SupersededDefault] = {}
    for entry in SD.SUPERSEDED_DEFAULTS:
        newest[entry.dotted_key] = entry

    for dotted, entry in newest.items():
        section, field = dotted.split(".")
        live = getattr(getattr(KiroCrewConfig(), section), field)
        assert live == entry.new_default, (
            f"{dotted}: registry says the current default is "
            f"{entry.new_default!r} but the loader applies {live!r} -- append a "
            f"new entry for the later change instead of leaving this one stale"
        )
        assert any(
            f.name == field for f in dc_fields(getattr(KiroCrewConfig(), section))
        ), f"{dotted}: no such field on the {section} config"


def test_an_install_still_storing_the_old_ceiling_is_reported():
    """A stored 90.0 that was declared but never migrated keeps
    compacting at the window ceiling, and this is what finally says so."""
    drifted = superseded_default_drift({"session": {"autocompact_pct": 90.0}})
    assert AUTOCOMPACT_ENTRY in drifted


def test_an_install_on_the_new_default_is_not_reported():
    assert superseded_default_drift({"session": {"autocompact_pct": 70.0}}) == []


def test_a_deliberately_chosen_value_is_not_reported():
    """Only the exact superseded default is drift. An operator who picked 85 is
    not holding a stale default and must not be nagged about one."""
    assert superseded_default_drift({"session": {"autocompact_pct": 85.0}}) == []


def test_the_autocompact_summary_names_both_values_and_the_release():
    text = drift_summary(AUTOCOMPACT_ENTRY)
    assert "session.autocompact_pct" in text
    assert "90.0" in text and "70.0" in text
    assert "#4388" in text


def test_loop_stall_old_default_is_reported_without_rewriting():
    base = {"dashboard": {"loop_stall_exit_after_secs": 25}}
    assert LOOP_STALL_ENTRY in superseded_default_drift(base)
    assert base == {"dashboard": {"loop_stall_exit_after_secs": 25}}


def test_loop_stall_summary_explains_automatic_default():
    text = drift_summary(LOOP_STALL_ENTRY)
    assert "dashboard.loop_stall_exit_after_secs" in text
    assert "25s desktop / 90s managed service" in text
    assert "JSON null" in text
    assert "None" not in text
    assert "#6651" in text


# --------------------------------------------------------------------------
# Acknowledgment: the report is falsifiable.
# --------------------------------------------------------------------------


def test_an_acked_value_is_not_reported(tmp_path, monkeypatch):
    """A value the operator affirmed is a choice, not drift."""
    _point_home(tmp_path, monkeypatch)
    base = {"session": {"autocompact_pct": 90.0}}
    SD.write_acked_superseded({"session.autocompact_pct": 90.0})
    assert superseded_default_drift(base) == []


def test_changing_an_acked_value_reports_it_again(tmp_path, monkeypatch):
    """The ack records the VALUE, so it cannot silence a different one later.

    Storing the old default again after acking a different value is exactly the
    case a key-only ack would hide forever.
    """
    _point_home(tmp_path, monkeypatch)
    SD.write_acked_superseded({"mcp_gateway.forward_declared_env": True})
    drifted = superseded_default_drift({"mcp_gateway": {"forward_declared_env": False}})
    assert FDE_ENTRY in drifted


def test_an_acked_zero_does_not_silence_a_stored_false(tmp_path, monkeypatch):
    """bool is an int subclass on the ack side too."""
    _point_home(tmp_path, monkeypatch)
    SD.write_acked_superseded({"mcp_gateway.forward_declared_env": 0})
    assert FDE_ENTRY in superseded_default_drift({"mcp_gateway": {"forward_declared_env": False}})


def test_an_explicit_empty_ack_map_shows_the_unacked_truth(tmp_path, monkeypatch):
    """``acked={}`` answers "what am I holding?" even for affirmed values."""
    _point_home(tmp_path, monkeypatch)
    SD.write_acked_superseded({"session.autocompact_pct": 90.0})
    base = {"session": {"autocompact_pct": 90.0}}
    assert superseded_default_drift(base, acked={}) == [AUTOCOMPACT_ENTRY]


def test_a_corrupt_ack_file_fails_soft(tmp_path, monkeypatch):
    """An unreadable ack file means the operator is told again, never a crash."""
    _point_home(tmp_path, monkeypatch)
    SD.ack_file_path().write_text("{not json", encoding="utf-8")
    assert SD.acked_superseded() == {}
    assert FDE_ENTRY in superseded_default_drift({"mcp_gateway": {"forward_declared_env": False}})


def test_the_ack_file_lives_outside_config_json(tmp_path, monkeypatch):
    """A to_dict() rewrite carries only schema fields, so an ack inside the config
    document would be dropped by the same materialization this module reports on."""
    _point_home(tmp_path, monkeypatch)
    _write_config(tmp_path, {"session": {"autocompact_pct": 90.0}})
    SD.write_acked_superseded({"session.autocompact_pct": 90.0})

    KiroCrewConfig().save()

    assert SD.acked_superseded() == {"session.autocompact_pct": 90.0}
    assert "acked_superseded" not in json.dumps(_on_disk(tmp_path))


def test_record_acks_stores_what_is_stored(tmp_path, monkeypatch):
    _point_home(tmp_path, monkeypatch)
    _write_config(tmp_path, {"session": {"autocompact_pct": 90.0}, "stt": {}})
    # stt.streaming is not stored, so there is no choice to affirm.
    recorded = SD.record_acks(["session.autocompact_pct", "stt.streaming"])
    assert recorded == ["session.autocompact_pct"]
    assert SD.acked_superseded() == {"session.autocompact_pct": 90.0}


def test_record_acks_reads_the_config_fresh_not_a_callers_snapshot(tmp_path, monkeypatch):
    """A value changed since it was listed must not be acked at its old snapshot --
    that would silence the report for a value the operator never affirmed."""
    _point_home(tmp_path, monkeypatch)
    _write_config(tmp_path, {"session": {"autocompact_pct": 55.0}})
    # 55.0 is not the superseded default, so it is not drift and not ackable.
    assert SD.record_acks(["session.autocompact_pct"]) == []
    assert SD.acked_superseded() == {}


def test_recording_an_ack_merges_with_what_is_already_on_disk(tmp_path, monkeypatch):
    """The read-modify-write runs inside the file's lock, so a concurrent ack of a
    different key is not dropped by the second writer's replacement."""
    _point_home(tmp_path, monkeypatch)
    _write_config(tmp_path, {"session": {"autocompact_pct": 90.0}})
    SD.write_acked_superseded({"stt.model": "turbo"})
    SD.record_acks(["session.autocompact_pct"])
    assert SD.acked_superseded() == {
        "stt.model": "turbo",
        "session.autocompact_pct": 90.0,
    }


def test_a_concurrent_ack_written_mid_transaction_survives(tmp_path, monkeypatch):
    """Proves the merge reads what is on DISK inside the lock, not a stale snapshot."""
    _point_home(tmp_path, monkeypatch)
    real = SD._acked_from_document
    fired: list[int] = []

    def _sneak(raw):
        # Fires on the locked read; write a rival ack before the merge computes.
        result = real(raw)
        if not fired:
            fired.append(1)
            SD.ack_file_path().write_text(
                json.dumps({"acked": {"stt.model": "turbo"}}), encoding="utf-8"
            )
        return result

    monkeypatch.setattr(SD, "_acked_from_document", _sneak)
    SD.write_acked_superseded({"session.autocompact_pct": 90.0})
    monkeypatch.setattr(SD, "_acked_from_document", real)
    assert SD.acked_superseded()["session.autocompact_pct"] == 90.0


def test_dropping_an_ack_leaves_the_others(tmp_path, monkeypatch):
    _point_home(tmp_path, monkeypatch)
    SD.write_acked_superseded({"stt.model": "turbo", "session.autocompact_pct": 90.0})
    SD.drop_acks(["session.autocompact_pct"])
    assert SD.acked_superseded() == {"stt.model": "turbo"}


def test_dropping_an_unacked_key_never_touches_the_file(tmp_path, monkeypatch):
    _point_home(tmp_path, monkeypatch)
    SD.drop_acks(["session.autocompact_pct"])
    assert not SD.ack_file_path().exists()


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="os.symlink needs elevation on Windows")
def test_a_link_at_the_ack_path_is_refused_not_written_through(tmp_path, monkeypatch):
    """The config writers FOLLOW a link on purpose (dotfiles repos). That is wrong for
    a path the agent can name: following it would overwrite the link's target."""
    _point_home(tmp_path, monkeypatch)
    victim = tmp_path / "victim.json"
    victim.write_text('{"keep": "me"}', encoding="utf-8")
    SD.ack_file_path().symlink_to(victim)

    with pytest.raises(OSError):
        SD.write_acked_superseded({"session.autocompact_pct": 90.0})

    assert victim.read_text(encoding="utf-8") == '{"keep": "me"}'


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="os.symlink needs elevation on Windows")
def test_a_link_swapped_in_after_the_check_is_replaced_not_followed(tmp_path, monkeypatch):
    """The check reports the condition; the rename-over-leaf is what makes it
    unexploitable, so a link winning the race still cannot reach its target."""
    _point_home(tmp_path, monkeypatch)
    victim = tmp_path / "victim.json"
    victim.write_text('{"keep": "me"}', encoding="utf-8")
    ack = SD.ack_file_path()

    def _plant_then_pass(path):
        ack.symlink_to(victim)
        return False  # simulate losing the race: the check saw no link

    monkeypatch.setattr(SD.platform_compat, "is_link_or_junction", _plant_then_pass)
    SD.write_acked_superseded({"session.autocompact_pct": 90.0})

    assert victim.read_text(encoding="utf-8") == '{"keep": "me"}'
    assert not ack.is_symlink()
    assert SD.acked_superseded() == {"session.autocompact_pct": 90.0}


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="no os.mkfifo on Windows")
def test_a_fifo_at_the_ack_path_is_refused_instead_of_blocking(tmp_path, monkeypatch):
    """The read runs on the config-load path, which is an event-loop path. `open()` on
    a FIFO waits for a writer forever, which would wedge the gateway."""
    _point_home(tmp_path, monkeypatch)
    os.mkfifo(SD.ack_file_path())
    assert SD.acked_superseded() == {}


def test_a_directory_at_the_ack_path_is_refused(tmp_path, monkeypatch):
    """Only a REGULAR file is read, so no other path shape reaches json.loads."""
    _point_home(tmp_path, monkeypatch)
    SD.ack_file_path().mkdir()
    assert SD.acked_superseded() == {}


def test_an_oversized_ack_file_is_refused(tmp_path, monkeypatch):
    """A capped single read is what keeps the load-path read bounded."""
    _point_home(tmp_path, monkeypatch)
    SD.ack_file_path().write_text(" " * (SD.ACK_MAX_BYTES + 1), encoding="utf-8")
    assert SD.acked_superseded() == {}


def test_the_ack_file_carries_no_unread_version_field(tmp_path, monkeypatch):
    """The soft read already tolerates any shape, so a version nobody checks is a
    field with no reader."""
    _point_home(tmp_path, monkeypatch)
    SD.write_acked_superseded({"session.autocompact_pct": 90.0})
    raw = json.loads(SD.ack_file_path().read_text(encoding="utf-8"))
    assert set(raw) == {"acked"}


# --------------------------------------------------------------------------
# Adoption: removing the key is what un-materializes it.
# --------------------------------------------------------------------------


def test_dropping_a_key_makes_the_current_default_apply(tmp_path, monkeypatch):
    _point_home(tmp_path, monkeypatch)
    base = {"session": {"autocompact_pct": 90.0}}
    assert SD.drop_drifted_keys(base, ["session.autocompact_pct"]) == ["session.autocompact_pct"]
    assert base == {"session": {}}
    # An emptied section resolves identically to an absent one.
    assert superseded_default_drift(base) == []
    _write_config(tmp_path, base)
    assert KiroCrewConfig.load().session.autocompact_pct == 70.0


def test_dropping_an_absent_key_reports_nothing_removed(tmp_path, monkeypatch):
    _point_home(tmp_path, monkeypatch)
    base: dict = {"session": {}}
    assert SD.drop_drifted_keys(base, ["session.autocompact_pct"]) == []


# --------------------------------------------------------------------------
# The load path says it ONCE, in one line, whatever the registry size.
# --------------------------------------------------------------------------


def test_many_drifted_keys_produce_one_warning_line(tmp_path, monkeypatch, caplog):
    """The registry is append-only, so a per-key line grows without bound on the
    very installs with the most real drift -- and lands on every CLI invocation."""
    _point_home(tmp_path, monkeypatch)
    _write_config(
        tmp_path,
        {
            "mcp_gateway": {"forward_declared_env": False},
            "session": {"autocompact_pct": 90.0},
            "stt": {"streaming": False, "model": "turbo"},
            "dashboard": {"loop_stall_exit_after_secs": 25},
        },
    )

    with caplog.at_level(logging.WARNING, logger=L.__name__):
        KiroCrewConfig.load()

    warnings = [r for r in caplog.records if "superseded default" in r.getMessage()]
    assert len(warnings) == 1
    text = warnings[0].getMessage()
    # Every drifted key is named, and the line says what to do about it.
    for key in (
        "mcp_gateway.forward_declared_env",
        "session.autocompact_pct",
        "stt.streaming",
        "stt.model",
        "dashboard.loop_stall_exit_after_secs",
    ):
        assert key in text
    assert "kirocrew config defaults" in text


def test_an_acked_key_is_not_named_on_the_load_path(tmp_path, monkeypatch, caplog):
    _point_home(tmp_path, monkeypatch)
    _write_config(tmp_path, {"session": {"autocompact_pct": 90.0}})
    SD.write_acked_superseded({"session.autocompact_pct": 90.0})

    with caplog.at_level(logging.WARNING, logger=L.__name__):
        KiroCrewConfig.load()

    assert not [r for r in caplog.records if "superseded default" in r.getMessage()]


def test_doctor_lists_an_acked_entry_instead_of_hiding_it(tmp_path, monkeypatch, capsys):
    """``doctor`` answers "what does this install hold?", so an affirmed value is
    part of the answer; only the unsolicited load-path line is silenced."""
    _point_home(tmp_path, monkeypatch)
    _write_config(tmp_path, {"session": {"autocompact_pct": 90.0}})
    SD.write_acked_superseded({"session.autocompact_pct": 90.0})

    issues: list[str] = []
    SD.render_doctor_section(issues)
    out = capsys.readouterr().out

    assert "session.autocompact_pct" in out
    assert "acknowledged as intentional" in out
    # Nothing is left to act on, so no fix hint is offered.
    assert "--adopt" not in out
    assert issues == []


def test_doctor_offers_the_commands_when_something_is_unacked(tmp_path, monkeypatch, capsys):
    _point_home(tmp_path, monkeypatch)
    _write_config(tmp_path, {"session": {"autocompact_pct": 90.0}})

    issues: list[str] = []
    SD.render_doctor_section(issues)
    out = capsys.readouterr().out

    assert "kirocrew config defaults --adopt" in out
    assert "kirocrew config defaults --keep" in out
    assert issues == []


# --------------------------------------------------------------------------
# One-shot auto-adoption: the entries whose old value has no second meaning.
# --------------------------------------------------------------------------

SUBAGENT_ENTRY = next(
    e for e in SD.SUPERSEDED_DEFAULTS if e.dotted_key == "agent.subagent_timeout_secs"
)

TURN_ENTRY = next(
    e for e in SD.SUPERSEDED_DEFAULTS if e.dotted_key == "agent.chat_turn_timeout_secs"
)


def test_only_unpinned_broken_budgets_adopt_themselves():
    """A row adopts only when no other suite pins its old value as supported.

    The dividing line is NOT how wide the value's range is. That test was tried and
    is wrong: ``instances.warm_set_cap`` is numeric with a range, and an operator
    running five crews who types 5 stores exactly the old default. What separates the
    two groups is whether the repository already GUARANTEES the stored value
    elsewhere -- a value some other suite pins as a supported configuration is not
    stale noise, whatever its type.

    Pinned as a property of the whole registry, because both failures it guards
    against are registry-shaped: a row that adopts by accident, and a row losing its
    exemption without the suite that pins it being consulted.
    """
    assert (
        SupersededDefault(
            dotted_key="a.b", old_default=1, new_default=2, changed_in="#1"
        ).auto_adopt
        is False
    ), "the dataclass default stays False, so an appended row adopts only when it says so"
    adopting = {e.dotted_key for e in SD.SUPERSEDED_DEFAULTS if e.auto_adopt}
    assert adopting == {
        "agent.subagent_timeout_secs",
        "agent.chat_turn_timeout_secs",
    }
    # Each of these has its stored value pinned as supported by a named test
    # elsewhere in the suite; adopting one turns that suite red, which is how this
    # line was found.
    for pinned in (
        "session.autocompact_pct",
        "dashboard.loop_stall_exit_after_secs",
        "stt.streaming",
        "stt.model",
        "mcp_gateway.forward_declared_env",
        "instances.warm_set_cap",
    ):
        assert pinned not in adopting, f"{pinned}'s stored value is guaranteed elsewhere"


def test_every_adopting_key_clears_in_one_load(tmp_path, monkeypatch):
    """One load clears both stale timeout budgets an upgraded install holds.

    Written as one load over one document rather than a test per key: what an
    operator actually has is both at once, and the failure worth catching is a key
    that silently sits out the sweep.
    """
    _point_home(tmp_path, monkeypatch)
    _write_config(
        tmp_path,
        {"agent": {"subagent_timeout_secs": 1800, "chat_turn_timeout_secs": 7200}},
    )

    cfg = KiroCrewConfig.load()

    assert cfg.agent.subagent_timeout_secs == 10800
    assert cfg.agent.chat_turn_timeout_secs == 14400
    assert _on_disk(tmp_path).get("agent", {}) == {}


def test_a_pinned_stored_value_is_never_adopted(tmp_path, monkeypatch):
    """The rows other suites guarantee survive a load untouched, on disk and in memory.

    Four at once, because the regression that set this boundary surfaced them one CI
    round at a time: a dashboard voice opt-out, a maxed compaction slider, a managed
    service's explicit watchdog budget, and a warm-set cap.
    """
    _point_home(tmp_path, monkeypatch)
    stored = {
        "stt": {"streaming": False, "model": "turbo"},
        "session": {"autocompact_pct": 90.0},
        "dashboard": {"loop_stall_exit_after_secs": 25},
        "instances": {"warm_set_cap": 5},
    }
    _write_config(tmp_path, stored)

    cfg = KiroCrewConfig.load()

    assert cfg.stt.streaming is False
    assert cfg.session.autocompact_pct == 90.0
    assert cfg.dashboard.loop_stall_exit_after_secs == 25
    assert cfg.instances.warm_set_cap == 5
    on_disk = _on_disk(tmp_path)
    for section, fields in stored.items():
        for field, value in fields.items():
            assert on_disk[section][field] == value, f"{section}.{field} was rewritten"
    assert SD.adopted_superseded() == {}


def test_the_ledger_write_never_blocks_the_event_loop(tmp_path, monkeypatch):
    """``record_adoptions`` must take the sidecar lock SINGLE-SHOT.

    The config load path runs on the asyncio event-loop thread in places, so a
    blocking acquire here stalls every gateway request and the heartbeat for as long
    as another writer keeps the lock. Asserted on the flag passed to ``file_lock``
    rather than by timing a real contended acquire: a timing test would be the flaky
    way to check the same one bit.
    """
    _point_home(tmp_path, monkeypatch)
    seen: list[bool] = []
    real = platform_compat.file_lock

    def _spy(fd, **kwargs):
        seen.append(bool(kwargs.get("wait", True)))
        return real(fd, **kwargs)

    monkeypatch.setattr(platform_compat, "file_lock", _spy)

    SD.record_adoptions({"agent.subagent_timeout_secs": 1800})
    assert seen == [False], "the load path must not wait on the sidecar lock"

    seen.clear()
    SD.write_acked_superseded({"session.autocompact_pct": 90.0})
    assert seen == [True], "a CLI caller has no loop to stall and keeps the wait"


def test_a_contended_sidecar_defers_the_adoption(tmp_path, monkeypatch):
    """A held sidecar lock defers the adoption instead of stalling or half-applying.

    Deferral is the correct outcome, not a compromise: the config keeps its value,
    the next load retries, and nothing is written in between.
    """
    _point_home(tmp_path, monkeypatch)
    _write_config(tmp_path, {"agent": {"subagent_timeout_secs": 1800}})

    def _contended(_values):
        raise BlockingIOError("another writer holds the sidecar")

    monkeypatch.setattr(L, "record_adoptions", _contended)

    KiroCrewConfig.load()

    assert _on_disk(tmp_path)["agent"]["subagent_timeout_secs"] == 1800
    assert SD.adopted_superseded() == {}


def test_the_loader_has_no_second_spelling_of_the_stored_value_lookup():
    """One dotted-key lookup, exported, rather than a copy in the loader.

    Two spellings of "read `<section>.<field>` out of the stored document" can come
    to disagree about an edge case (a non-dict section, an absent key) while both
    look correct in isolation.
    """
    import inspect

    from kiro_crew.config import loader

    assert not hasattr(loader, "_stored_scalar")
    assert SD.stored_value_or_none({"agent": {"x": 1}}, "agent.x") == 1
    assert SD.stored_value_or_none({"agent": {}}, "agent.x") is None
    assert SD.stored_value_or_none({"agent": "not-a-dict"}, "agent.x") is None
    assert "stored_value_or_none" in inspect.getsource(loader._apply_document_migrations)


def test_a_deferred_adoption_is_not_frozen_behind_the_cache(tmp_path, monkeypatch):
    """A deferred adoption drops the validated-data cache so the next load retries.

    Only a load that READS the base document can decide an adoption, so a
    read-and-defer that left its document cached would have every later load serve
    the stale value from that cache and never retry -- the ceiling the operator
    upgraded to fix would come back and stay.
    """
    _point_home(tmp_path, monkeypatch)
    _write_config(tmp_path, {"agent": {"subagent_timeout_secs": 1800}})

    calls: list[int] = []

    def _contended(_values):
        calls.append(1)
        raise BlockingIOError("another writer holds the sidecar")

    monkeypatch.setattr(L, "record_adoptions", _contended)

    first = KiroCrewConfig.load()
    # Deferred means deferred on BOTH halves: the write did not land, so the running
    # config keeps the stored value rather than diverging from it.
    assert first.agent.subagent_timeout_secs == 1800
    assert _on_disk(tmp_path)["agent"]["subagent_timeout_secs"] == 1800

    # A second load with no file change: it must RE-READ (and try again) rather than
    # serve the cached document, which is what the retry depends on.
    KiroCrewConfig.load()
    assert len(calls) == 2, "the deferred adoption was never retried"


def test_an_unreadable_ledger_adopts_nothing(tmp_path, monkeypatch, caplog):
    """A sidecar that exists but cannot be parsed fails CLOSED on adoption.

    Reading it as empty would re-arm the one-shot over a value the operator restored,
    which is the single outcome the ledger exists to prevent. Declining costs at most
    a deferral; the ACK half stays fail-soft, because a missed ack costs one report
    line.
    """
    _point_home(tmp_path, monkeypatch)
    _write_config(tmp_path, {"agent": {"subagent_timeout_secs": 1800}})
    SD.ack_file_path().write_text("{ this is not json", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger=SD.__name__):
        cfg = KiroCrewConfig.load()

    assert cfg.agent.subagent_timeout_secs == 1800
    assert _on_disk(tmp_path)["agent"]["subagent_timeout_secs"] == 1800
    assert any("could not be read" in r.getMessage() for r in caplog.records)


def test_a_missing_ledger_is_not_an_unreadable_one(tmp_path, monkeypatch):
    """No file means nothing was ever adopted, which is a trustworthy answer.

    The negative control for the test above: if the fail-closed branch also caught
    the absent-file case, adoption could never happen at all on a fresh install --
    green on the safety test, silently dead as a feature.
    """
    _point_home(tmp_path, monkeypatch)
    assert not SD.ack_file_path().exists()
    _write_config(tmp_path, {"agent": {"subagent_timeout_secs": 1800}})

    cfg = KiroCrewConfig.load()

    assert cfg.agent.subagent_timeout_secs == 10800


def test_a_malformed_sidecar_refuses_the_write_instead_of_erasing_it(tmp_path, monkeypatch):
    """A write must not rebuild the sidecar from a document it could not parse.

    Both maps are serialized from one read, so an unparsable document would be
    replaced by one built from two empty maps -- and dropping the ADOPTED map re-arms
    the one-shot over a value the operator restored, which is the outcome the ledger
    exists to prevent. Fixing only the read path left this hole: `--keep` would have
    erased the adoption history on its way past.
    """
    _point_home(tmp_path, monkeypatch)
    SD.ack_file_path().write_text("{ not json at all", encoding="utf-8")

    with pytest.raises(OSError):
        SD.write_acked_superseded({"session.autocompact_pct": 90.0})

    # The bytes are still there for a human to look at, not replaced by our own.
    assert SD.ack_file_path().read_text(encoding="utf-8") == "{ not json at all"


def test_a_non_plain_leaf_is_still_replaced_rather_than_refused(tmp_path, monkeypatch):
    """Negative control for the refusal above, and a real availability property.

    A leaf that is not a plain file holds no ledger of ours, and the write path
    renames OVER it rather than following it. If the refusal also covered this case,
    anyone who can create that path could permanently block every ack and every
    adoption -- the safety fix would have bought a denial of service.
    """
    _point_home(tmp_path, monkeypatch)
    victim = tmp_path / "victim.json"
    victim.write_text('{"keep": "me"}', encoding="utf-8")
    SD.ack_file_path().symlink_to(victim)

    monkeypatch.setattr(SD.platform_compat, "is_link_or_junction", lambda _p: False)
    SD.write_acked_superseded({"session.autocompact_pct": 90.0})

    assert victim.read_text(encoding="utf-8") == '{"keep": "me"}'
    assert not SD.ack_file_path().is_symlink()
    assert SD.acked_superseded() == {"session.autocompact_pct": 90.0}


def test_a_malformed_sidecar_does_not_let_the_load_adopt(tmp_path, monkeypatch):
    """The read and write halves agree: nothing adopts while the ledger is unreadable.

    The load path's ``record_adoptions`` would now raise on the same document, so
    this pins the whole-path outcome rather than one guard: the stored value stays.
    """
    _point_home(tmp_path, monkeypatch)
    _write_config(tmp_path, {"agent": {"subagent_timeout_secs": 1800}})
    SD.ack_file_path().write_text("}}not json", encoding="utf-8")

    KiroCrewConfig.load()

    assert _on_disk(tmp_path)["agent"]["subagent_timeout_secs"] == 1800
    assert SD.ack_file_path().read_text(encoding="utf-8") == "}}not json"


def test_an_exception_during_write_back_still_drops_the_cache(tmp_path, monkeypatch):
    """Every path that skips the write leaves the same stale cache entry.

    Three of them exist -- a contended lock, a degraded load, an exception caught by
    the best-effort handler -- and only the first was covered when the invalidation
    sat beside the write. A load that read the document and then skipped the adoption
    must not leave that document cached, or later cache-hit loads serve the stale
    ceiling forever (a cache hit can never decide an adoption, by design).

    The exception path stands in for all three: the invalidation now lives in one
    ``finally`` they share, so covering the one a test can trigger deterministically
    covers the branch. Degrading a section on purpose from a test needs the loader to
    discard it, which the schema layer rejects earlier, and the contended-lock path
    has its own test above.
    """
    _point_home(tmp_path, monkeypatch)
    _write_config(tmp_path, {"agent": {"subagent_timeout_secs": 1800}})

    def _boom(*_a, **_kw):
        raise RuntimeError("write-back exploded")

    monkeypatch.setattr(L, "_persist_config_migration", _boom)

    KiroCrewConfig.load()

    # Asserted on the CACHE itself, not on a second load's behaviour: a cache-hit
    # load still runs the other pending migrations, so counting write-back calls
    # cannot tell a hit from a re-read. An empty cache IS the retry.
    assert L._CONFIG_CACHE._entry is None, "the stale document stayed cached"
    assert _on_disk(tmp_path)["agent"]["subagent_timeout_secs"] == 1800


def test_the_running_config_never_diverges_from_the_stored_one(tmp_path, monkeypatch):
    """In-memory adoption applies only to keys the write CONFIRMED it removed.

    Applied eagerly, a failed write left the gateway running 10800 while
    `config.json` still said 1800 -- a divergence neither side reveals: the operator
    reads the file and sees their value, the process behaves as if it had changed.
    Both halves now move together or neither does.

    Written as a paired assertion over two loads rather than one: the same code must
    ALSO still change memory on the success path, and a test that only pinned the
    failure could be satisfied by never touching memory at all.
    """
    _point_home(tmp_path, monkeypatch)
    _write_config(tmp_path, {"agent": {"subagent_timeout_secs": 1800}})

    def _refuse(_values):
        raise OSError("read-only data home")

    monkeypatch.setattr(L, "record_adoptions", _refuse)
    failed = KiroCrewConfig.load()
    assert failed.agent.subagent_timeout_secs == 1800
    assert _on_disk(tmp_path)["agent"]["subagent_timeout_secs"] == 1800

    monkeypatch.undo()
    _point_home(tmp_path, monkeypatch)
    L._invalidate_config_cache()
    ok = KiroCrewConfig.load()
    assert ok.agent.subagent_timeout_secs == 10800
    assert "subagent_timeout_secs" not in _on_disk(tmp_path).get("agent", {})


def test_a_failed_write_keeps_the_value_and_does_not_re_adopt_later(tmp_path, monkeypatch, caplog):
    """The chosen side of an unavoidable two-file window, pinned end to end.

    ``config.json`` and the sidecar have no shared transaction, so exactly one of two
    windows exists. Marker-first means a durable marker can outlive a failed removal:
    the operator KEEPS the value they had and the adoption is simply not retried.
    Removal-first would instead let a landed removal outlive a lost marker, and then a
    value the operator deliberately restored is deleted a SECOND time -- which is what
    the one-shot guarantee exists to prevent.

    A missed improvement is recoverable by one documented command; a destroyed choice
    is not recoverable at all. This test pins that the failure lands on the recoverable
    side, including the part that matters most: a later load must not delete the value
    again.
    """
    _point_home(tmp_path, monkeypatch)
    _write_config(tmp_path, {"agent": {"subagent_timeout_secs": 1800}})

    def _fail_backup(_path):
        raise OSError("no space left on device")

    monkeypatch.setattr(L, "_write_migration_backup", _fail_backup)
    with caplog.at_level(logging.WARNING, logger=L.__name__):
        failed = KiroCrewConfig.load()

    # Nothing was destroyed, and memory did not run ahead of the file.
    assert _on_disk(tmp_path)["agent"]["subagent_timeout_secs"] == 1800
    assert failed.agent.subagent_timeout_secs == 1800
    assert not any("adopted current default" in r.getMessage() for r in caplog.records)

    monkeypatch.undo()
    _point_home(tmp_path, monkeypatch)
    L._invalidate_config_cache()
    later = KiroCrewConfig.load()

    # The marker is durable, so the value is left alone from here on -- the operator's
    # 1800 survives every later load rather than being taken on a retry.
    assert later.agent.subagent_timeout_secs == 1800
    assert _on_disk(tmp_path)["agent"]["subagent_timeout_secs"] == 1800


def test_the_unapplied_adoption_is_still_reported_so_it_is_recoverable(
    tmp_path, monkeypatch, caplog
):
    """A missed improvement must not be silent: the drift line is the recovery path.

    The marker suppresses the automatic retry, not the report -- ``--adopt`` remains
    available and the startup line still names the key, which is what makes "recoverable
    by one documented command" true rather than aspirational.
    """
    _point_home(tmp_path, monkeypatch)
    _write_config(tmp_path, {"agent": {"subagent_timeout_secs": 1800}})
    SD.record_adoptions({"agent.subagent_timeout_secs": 1800})

    with caplog.at_level(logging.WARNING, logger=L.__name__):
        KiroCrewConfig.load()

    warnings = " ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    assert "agent.subagent_timeout_secs" in warnings
    assert "config defaults" in warnings


def test_the_escape_hatch_key_never_auto_adopts():
    """``forward_declared_env``'s stored False is an opt-out, so it stays report-only.

    The one entry the whole read-only design was reasoned from. If a future change
    ever flips this, the gateway env suite's ``test_a_real_false_still_turns_it_off``
    is what breaks, and this test is the earlier warning.
    """
    assert FDE_ENTRY.auto_adopt is False
    base = {"mcp_gateway": {"forward_declared_env": False}}
    assert SD.auto_adoptable(base, acked={}, adopted={}) == []


def test_load_adopts_the_stale_timeout_on_disk_and_in_memory(tmp_path, monkeypatch, caplog):
    """The complaint this fixes: upgrade, restart, still cut at 30 minutes.

    In memory as well as on disk, because the subagent manager reads the budget once
    at gateway start -- a disk-only fix would leave the run that performed it still
    reaping at the old value.
    """
    _point_home(tmp_path, monkeypatch)
    _write_config(tmp_path, {"agent": {"subagent_timeout_secs": 1800}})

    with caplog.at_level(logging.WARNING, logger=L.__name__):
        cfg = KiroCrewConfig.load()

    assert cfg.agent.subagent_timeout_secs == 10800
    assert "subagent_timeout_secs" not in _on_disk(tmp_path).get("agent", {})
    assert SD.adopted_superseded() == {"agent.subagent_timeout_secs": 1800}
    notices = [
        r.getMessage() for r in caplog.records if "adopted current default" in r.getMessage()
    ]
    assert len(notices) == 1
    assert "agent.subagent_timeout_secs" in notices[0]
    assert "1800" in notices[0]
    assert "kirocrew config set agent.subagent_timeout_secs 1800" in notices[0]


def test_a_deliberately_chosen_value_survives_the_adoption(tmp_path, monkeypatch):
    """Only the exact old default is adopted; any other number is a real choice."""
    _point_home(tmp_path, monkeypatch)
    _write_config(tmp_path, {"agent": {"subagent_timeout_secs": 2400}})

    cfg = KiroCrewConfig.load()

    assert cfg.agent.subagent_timeout_secs == 2400
    assert _on_disk(tmp_path)["agent"]["subagent_timeout_secs"] == 2400
    assert SD.adopted_superseded() == {}


def test_adoption_is_one_shot_so_a_restored_value_is_left_alone(tmp_path, monkeypatch):
    """The property that makes an automatic rewrite safe at all.

    Setting the key back to the old default AFTER an adoption is the operator's own
    choice, and the ledger is what tells the two apart -- without it every load
    would re-remove the value they just restored.
    """
    _point_home(tmp_path, monkeypatch)
    _write_config(tmp_path, {"agent": {"subagent_timeout_secs": 1800}})
    KiroCrewConfig.load()
    assert SD.adopted_superseded() == {"agent.subagent_timeout_secs": 1800}

    _write_config(tmp_path, {"agent": {"subagent_timeout_secs": 1800}})
    cfg = KiroCrewConfig.load()

    assert cfg.agent.subagent_timeout_secs == 1800
    assert _on_disk(tmp_path)["agent"]["subagent_timeout_secs"] == 1800


def test_keep_pre_empts_an_adoption_that_has_not_happened_yet(tmp_path, monkeypatch):
    """An acknowledged value is not drift, so ``--keep`` still wins over adoption.

    This is the answer for an operator who genuinely did choose 1800: affirm it, and
    the automatic path never touches it.
    """
    _point_home(tmp_path, monkeypatch)
    _write_config(tmp_path, {"agent": {"subagent_timeout_secs": 1800}})
    SD.write_acked_superseded({"agent.subagent_timeout_secs": 1800})

    cfg = KiroCrewConfig.load()

    assert cfg.agent.subagent_timeout_secs == 1800
    assert _on_disk(tmp_path)["agent"]["subagent_timeout_secs"] == 1800
    assert SD.adopted_superseded() == {}


def test_an_overlay_value_outranks_the_adoption_in_memory(tmp_path, monkeypatch):
    """The base's stale bytes are cleared; the overlay's live choice is not replaced."""
    _point_home(tmp_path, monkeypatch)
    _write_config(tmp_path, {"agent": {"subagent_timeout_secs": 1800}})
    _write_local(tmp_path, {"agent": {"subagent_timeout_secs": 3600}})

    cfg = KiroCrewConfig.load()

    assert cfg.agent.subagent_timeout_secs == 3600
    assert "subagent_timeout_secs" not in _on_disk(tmp_path).get("agent", {})


def test_both_timeout_keys_adopt_in_one_load(tmp_path, monkeypatch):
    """A real upgraded install holds both, so both clear in a single write."""
    _point_home(tmp_path, monkeypatch)
    _write_config(
        tmp_path, {"agent": {"subagent_timeout_secs": 1800, "chat_turn_timeout_secs": 7200}}
    )

    cfg = KiroCrewConfig.load()

    assert cfg.agent.subagent_timeout_secs == 10800
    assert cfg.agent.chat_turn_timeout_secs == 14400
    assert _on_disk(tmp_path).get("agent", {}) == {}  # both keys cleared in one write
    assert set(SD.adopted_superseded()) == {
        "agent.subagent_timeout_secs",
        "agent.chat_turn_timeout_secs",
    }


def test_the_load_does_not_tell_the_operator_to_fix_what_it_just_fixed(
    tmp_path, monkeypatch, caplog
):
    """An adopted key is excluded from the warning; a report-only one still appears."""
    _point_home(tmp_path, monkeypatch)
    _write_config(
        tmp_path,
        {
            "agent": {"subagent_timeout_secs": 1800},
            "mcp_gateway": {"forward_declared_env": False},
        },
    )

    with caplog.at_level(logging.WARNING, logger=L.__name__):
        KiroCrewConfig.load()

    drift_warnings = " ".join(
        r.getMessage()
        for r in caplog.records
        if "still hold a superseded default" in r.getMessage()
    )
    assert "forward_declared_env" in drift_warnings
    assert "subagent_timeout_secs" not in drift_warnings
    assert any(
        "adopted current default for agent.subagent_timeout_secs" in r.getMessage()
        for r in caplog.records
    )


def test_a_failed_ledger_write_aborts_the_adoption(tmp_path, monkeypatch):
    """Record-then-remove: a removal whose record was lost would repeat forever.

    Repeating is the one failure mode that can override a value the operator
    restored, so a ledger that cannot be written leaves the config untouched and the
    key reported like any other drift.
    """
    _point_home(tmp_path, monkeypatch)
    _write_config(tmp_path, {"agent": {"subagent_timeout_secs": 1800}})

    def _refuse(_values):
        raise OSError("read-only data home")

    monkeypatch.setattr(L, "record_adoptions", _refuse)

    cfg = KiroCrewConfig.load()

    assert _on_disk(tmp_path)["agent"]["subagent_timeout_secs"] == 1800
    # Memory follows disk. An earlier revision applied the in-memory half eagerly and
    # left the running config on 10800 while the file still said 1800 -- a divergence
    # the operator cannot see from either side. Nothing moves unless the write landed.
    assert cfg.agent.subagent_timeout_secs == 1800


def test_the_ledger_and_the_acks_share_one_file_without_erasing_each_other(tmp_path, monkeypatch):
    """Both maps live in one document, so writing one must carry the other through.

    A dropped adoption record is not cosmetic: it re-opens the repeat-adoption path
    the ledger exists to close.
    """
    _point_home(tmp_path, monkeypatch)
    SD.write_acked_superseded({"session.autocompact_pct": 90.0})
    SD.record_adoptions({"agent.subagent_timeout_secs": 1800})

    assert SD.acked_superseded() == {"session.autocompact_pct": 90.0}
    assert SD.adopted_superseded() == {"agent.subagent_timeout_secs": 1800}

    SD.write_acked_superseded({"stt.streaming": False})

    assert SD.adopted_superseded() == {"agent.subagent_timeout_secs": 1800}


def test_the_registered_new_defaults_match_the_live_dataclass_defaults():
    """Negative control for the in-memory half.

    ``_adopt_in_memory`` reads the field's OWN default rather than the row's
    ``new_default``, so this pins that the two agree for the auto-adopting keys --
    if they ever diverge, the disk and memory halves would resolve different
    numbers and the divergence would be invisible.
    """
    from kiro_crew.config.sections import AgentConfig

    assert AgentConfig.__dataclass_fields__["subagent_timeout_secs"].default == (
        SUBAGENT_ENTRY.new_default
    )
    assert AgentConfig.__dataclass_fields__["chat_turn_timeout_secs"].default == (
        TURN_ENTRY.new_default
    )

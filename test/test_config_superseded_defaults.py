"""Reporting of stored config values that still hold a superseded default.

``config.json`` is a full materialization of the schema and the loader resolves
each field as ``data.get(key, DEFAULT)``, so a stored value always beats a
changed dataclass default. #4566 changed ``mcp_gateway.forward_declared_env``
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
    it names a value the loader no longer applies, the report tells operators to
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
    """The case #4388 declared and did not migrate: a stored 90.0 keeps
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
# Acknowledgment: the report is falsifiable (#7559).
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

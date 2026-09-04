"""``kirocrew config defaults`` -- review, adopt, or affirm superseded defaults.

The load path can only REPORT drift: a stored old default and a deliberate opt-out
are the same bytes, so it must not rewrite either. This command is the surface
where the operator resolves that ambiguity themselves, which is what lets the
report be one answerable line instead of a permanent per-key notice (#7559).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.config import superseded_defaults as SD

DRIFTED = {
    "session": {"autocompact_pct": 90.0},
    "mcp_gateway": {"forward_declared_env": False},
}


def _args(*, keys: list[str] | None = None, adopt: bool = False, keep: bool = False):
    return argparse.Namespace(config_action="defaults", keys=keys or [], adopt=adopt, keep=keep)


def _run(args: argparse.Namespace, config_dir: Path) -> None:
    from kiro_crew.cli_config import _config_cmd

    with (
        patch("kiro_crew.cli_config.config_path", return_value=config_dir / "config.json"),
        patch(
            "kiro_crew.cli_config.config_local_path",
            return_value=config_dir / "config.local.json",
        ),
        patch("kiro_crew.config.loader.config_path", return_value=config_dir / "config.json"),
        patch("kiro_crew.config.loader.config_dir", return_value=config_dir),
        patch("kiro_crew.cli_config.sel"),
    ):
        _config_cmd(args)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    d = tmp_path / "crew"
    d.mkdir()
    (d / "config.json").write_text(json.dumps(DRIFTED), encoding="utf-8")
    return d


def _stored(home: Path) -> dict:
    return json.loads((home / "config.json").read_text(encoding="utf-8"))


def test_listing_names_every_drifted_key_and_both_commands(home, capsys):
    _run(_args(), home)
    out = capsys.readouterr().out
    assert "session.autocompact_pct" in out
    assert "mcp_gateway.forward_declared_env" in out
    assert "--adopt" in out and "--keep" in out
    # Listing changes nothing.
    assert _stored(home) == DRIFTED


def test_adopt_removes_the_keys_so_the_current_default_applies(home, capsys):
    _run(_args(adopt=True), home)
    saved = _stored(home)
    assert "autocompact_pct" not in saved["session"]
    assert "forward_declared_env" not in saved["mcp_gateway"]
    out = capsys.readouterr().out
    assert "current default now applies" in out


def test_adopt_touches_only_the_named_key(home):
    _run(_args(keys=["session.autocompact_pct"], adopt=True), home)
    saved = _stored(home)
    assert "autocompact_pct" not in saved["session"]
    # The escape-hatch key is left exactly as the operator stored it.
    assert saved["mcp_gateway"]["forward_declared_env"] is False


def test_adopt_leaves_a_value_that_changed_since_it_was_listed(home):
    """Re-detection happens inside the lock, so a concurrent edit is not discarded."""
    from kiro_crew import cli_config
    from kiro_crew.cli_config import _config_cmd

    def _steal(existing: dict) -> dict:
        existing["session"]["autocompact_pct"] = 55.0
        return existing

    real = SD.superseded_default_drift
    calls: list[int] = []

    def _drift(base_data, acked=None):
        # First call is the snapshot read; mutate the file before the locked pass.
        calls.append(1)
        if len(calls) == 1:
            from kiro_crew.config.loader import update_config_locked

            update_config_locked(home / "config.json", mutate=_steal)
        return real(base_data, acked)

    with (
        patch("kiro_crew.cli_config.config_path", return_value=home / "config.json"),
        patch(
            "kiro_crew.cli_config.config_local_path",
            return_value=home / "config.local.json",
        ),
        patch("kiro_crew.config.loader.config_dir", return_value=home),
        patch("kiro_crew.cli_config.sel"),
        patch.object(cli_config, "superseded_default_drift", _drift),
    ):
        _config_cmd(_args(keys=["session.autocompact_pct"], adopt=True))

    assert _stored(home)["session"]["autocompact_pct"] == 55.0


def test_keep_records_the_stored_values_and_silences_the_report(home, capsys):
    _run(_args(keep=True), home)
    with patch("kiro_crew.config.loader.config_dir", return_value=home):
        assert SD.acked_superseded() == {
            "session.autocompact_pct": 90.0,
            "mcp_gateway.forward_declared_env": False,
        }
        assert SD.superseded_default_drift(DRIFTED) == []
    # Nothing in the config document changed -- an ack is bookkeeping, not a setting.
    assert _stored(home) == DRIFTED
    assert "recorded as intentional" in capsys.readouterr().out


def test_adopting_an_acked_key_drops_its_ack(home):
    """The ack recorded a value that is no longer stored, so keeping it would
    silence a genuinely deliberate choice made later."""
    _run(_args(keys=["session.autocompact_pct"], keep=True), home)
    _run(_args(keys=["session.autocompact_pct"], adopt=True), home)
    with patch("kiro_crew.config.loader.config_dir", return_value=home):
        assert "session.autocompact_pct" not in SD.acked_superseded()


def test_a_bare_adopt_leaves_an_acknowledged_value_alone(home, capsys):
    """The operator already answered for that key; sweeping it up would undo it."""
    _run(_args(keys=["session.autocompact_pct"], keep=True), home)
    _run(_args(adopt=True), home)
    saved = _stored(home)
    assert saved["session"]["autocompact_pct"] == 90.0
    assert "forward_declared_env" not in saved["mcp_gateway"]


def test_naming_an_acknowledged_key_still_adopts_it(home):
    """An explicit key is the operator saying so again."""
    _run(_args(keys=["session.autocompact_pct"], keep=True), home)
    _run(_args(keys=["session.autocompact_pct"], adopt=True), home)
    assert "autocompact_pct" not in _stored(home)["session"]


def test_a_bare_adopt_with_everything_acknowledged_changes_nothing(home, capsys):
    _run(_args(keep=True), home)
    _run(_args(adopt=True), home)
    assert _stored(home) == DRIFTED
    assert "Nothing left to act on" in capsys.readouterr().out


def test_adopt_says_the_overlay_still_wins_when_it_carries_the_key(home, capsys):
    """Removing a base key an overlay also carries changes no effective value, so the
    report must not claim the current default now applies."""
    (home / "config.local.json").write_text(
        json.dumps({"session": {"autocompact_pct": 80.0}}), encoding="utf-8"
    )
    _run(_args(keys=["session.autocompact_pct"], adopt=True), home)
    out = capsys.readouterr().out
    assert "config.local.json still overrides it" in out
    assert "the current default now applies" not in out


def test_an_unknown_key_is_refused_rather_than_silently_ignored(home, capsys):
    with pytest.raises(SystemExit) as e:
        _run(_args(keys=["session.timeout_secs"], adopt=True), home)
    assert e.value.code == 1
    assert "Not holding a superseded default" in capsys.readouterr().err
    assert _stored(home) == DRIFTED


def test_a_clean_install_says_so(tmp_path, capsys):
    d = tmp_path / "crew"
    d.mkdir()
    (d / "config.json").write_text(json.dumps({"session": {"autocompact_pct": 70.0}}), "utf-8")
    _run(_args(), d)
    assert "No stored value holds a superseded default" in capsys.readouterr().out


def test_a_missing_config_needs_no_action(tmp_path, capsys):
    d = tmp_path / "crew"
    d.mkdir()
    _run(_args(adopt=True), d)
    assert "current defaults already apply" in capsys.readouterr().out


def test_a_corrupt_config_is_refused_not_overwritten(tmp_path, capsys):
    d = tmp_path / "crew"
    d.mkdir()
    (d / "config.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        _run(_args(adopt=True), d)
    assert e.value.code == 1
    assert "Could not read" in capsys.readouterr().err
    assert (d / "config.json").read_text(encoding="utf-8") == "{not json"


def test_invalid_utf8_in_config_gives_an_error_not_a_traceback(tmp_path, capsys):
    """UnicodeDecodeError is a ValueError but not a JSONDecodeError, so a tuple naming
    only the latter lets it escape as a traceback."""
    d = tmp_path / "crew"
    d.mkdir()
    (d / "config.json").write_bytes(b'{"session": {"autocompact_pct": 90.0}, "x": "\xff\xfe"}')
    with pytest.raises(SystemExit) as e:
        _run(_args(), d)
    assert e.value.code == 1
    assert "Could not read" in capsys.readouterr().err


def test_invalid_utf8_in_the_overlay_does_not_break_adopt(home, capsys):
    """An unreadable overlay is treated as carrying nothing, never as a crash."""
    (home / "config.local.json").write_bytes(b'{"session": "\xff\xfe"}')
    _run(_args(keys=["session.autocompact_pct"], adopt=True), home)
    assert "autocompact_pct" not in _stored(home)["session"]


# --------------------------------------------------------------------------
# A coerced value: inert bytes that cost a warning on every load.
# --------------------------------------------------------------------------


@pytest.fixture
def retired(tmp_path: Path) -> Path:
    d = tmp_path / "crew"
    d.mkdir()
    (d / "config.json").write_text(json.dumps({"stt": {"provider": "whisper"}}), "utf-8")
    return d


def test_a_retired_provider_is_listed_as_coerced(retired, capsys):
    _run(_args(), retired)
    out = capsys.readouterr().out
    assert "stt.provider" in out
    assert "'whisper'" in out
    assert "cannot take effect" in out


def test_adopt_drops_a_retired_provider(retired, capsys):
    _run(_args(adopt=True), retired)
    saved = json.loads((retired / "config.json").read_text(encoding="utf-8"))
    assert "provider" not in saved["stt"]
    assert "stt.provider removed" in capsys.readouterr().out


def test_a_coerced_value_cannot_be_kept(retired, capsys):
    """Affirming it would promise a setting the loader replaces regardless."""
    _run(_args(keep=True), retired)
    out = capsys.readouterr().out
    assert "cannot be kept" in out
    saved = json.loads((retired / "config.json").read_text(encoding="utf-8"))
    assert saved["stt"]["provider"] == "whisper"
    with patch("kiro_crew.config.loader.config_dir", return_value=retired):
        assert SD.acked_superseded() == {}


def test_a_selectable_provider_is_not_coerced(tmp_path):
    assert SD.coerced_value_drift({"stt": {"provider": "local"}}) == []
    assert SD.coerced_value_drift({"stt": {}}) == []


def test_an_appended_coerced_entry_is_detected_without_editing_the_detector(monkeypatch):
    """The predicate rides on the entry, so appending a retirement is sufficient.

    A detector switching on dotted_key would leave this entry silently unreported --
    no test red, and an operator stuck with a warning nothing can clear.
    """
    appended = SD.CoercedValue(
        dotted_key="agent.provider",
        resolves_to="acp",
        reason="names a withdrawn provider",
        is_coerced=lambda v: v == "gone",
    )
    monkeypatch.setattr(SD, "COERCED_VALUES", SD.COERCED_VALUES + (appended,))
    found = SD.coerced_value_drift({"agent": {"provider": "gone"}})
    assert [e.dotted_key for e, _ in found] == ["agent.provider"]


def test_an_unwritable_data_home_gives_an_error_not_a_traceback(home, capsys):
    """A read-only or full data home must not surface as an uncaught OSError."""
    from kiro_crew.config import superseded_defaults as sd

    with patch.object(sd, "_update_acked", side_effect=OSError("read-only file system")):
        with pytest.raises(SystemExit) as e:
            _run(_args(keep=True), home)
    assert e.value.code == 1
    assert "Could not record acknowledgments" in capsys.readouterr().err


def test_an_unwritable_config_on_adopt_gives_an_error_not_a_traceback(home, capsys):
    from kiro_crew import cli_config

    with patch.object(cli_config, "update_config_locked", side_effect=OSError("disk full")):
        with pytest.raises(SystemExit) as e:
            _run(_args(adopt=True), home)
    assert e.value.code == 1
    assert "Could not write" in capsys.readouterr().err

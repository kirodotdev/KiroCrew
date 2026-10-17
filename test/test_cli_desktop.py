"""Tests for ``kirocrew desktop metrics``."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import pytest

from kiro_crew import cli_desktop, perf_sampler, platform_compat


def _doc(**over) -> dict:
    doc = {
        "version": 1,
        "platform": "darwin",
        "electron": "31.0.0",
        "intervalMs": 5000,
        "samples": [
            {
                "at": "2026-08-02T12:00:00.000Z",
                "processes": [
                    {"pid": 1, "type": "Browser", "cpuPercent": 3.0, "workingSetKb": 4096},
                    {"pid": 2, "type": "Tab", "cpuPercent": 9.0, "workingSetKb": 2048},
                ],
                "totalCpuPercent": 12.0,
                "totalWorkingSetKb": 6144,
            },
            {
                "at": "2026-08-02T12:00:05.000Z",
                "processes": [
                    {"pid": 1, "type": "Browser", "cpuPercent": 1.0, "workingSetKb": 4096},
                ],
                "totalCpuPercent": 1.0,
                "totalWorkingSetKb": 4096,
            },
        ],
    }
    doc.update(over)
    return doc


def _args(path: Path | None = None, **over) -> argparse.Namespace:
    ns = argparse.Namespace(desktop_cmd="metrics", path=path, json=False, top=5)
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


class TestGate:
    def test_refuses_without_the_debug_flag(self, monkeypatch, capsys, tmp_path):
        monkeypatch.delenv(perf_sampler.DEBUG_ENV_VAR, raising=False)
        assert cli_desktop.desktop_cmd(_args(tmp_path / "x.json")) == 1
        assert "KIROCREW_DEBUG" in capsys.readouterr().err

    def test_an_explicit_falsey_value_is_off(self, monkeypatch, tmp_path):
        monkeypatch.setenv(perf_sampler.DEBUG_ENV_VAR, "0")
        assert cli_desktop.desktop_cmd(_args(tmp_path / "x.json")) == 1


class TestLogDirResolution:
    def test_macos_uses_library_logs(self, monkeypatch):
        monkeypatch.setattr(platform_compat, "IS_MACOS", True)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        dirs = cli_desktop.desktop_log_dirs({})
        assert any(d.as_posix().endswith("Library/Logs/KiroCrew") for d in dirs)

    def test_windows_uses_appdata(self, monkeypatch):
        monkeypatch.setattr(platform_compat, "IS_MACOS", False)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        # Compared with as_posix so the assertion does not depend on the host
        # separator: str() of a path is backslash-separated on Windows.
        dirs = [d.as_posix() for d in cli_desktop.desktop_log_dirs({"APPDATA": "/roam"})]
        assert "/roam/KiroCrew/logs" in dirs

    def test_linux_honours_xdg_config_home(self, monkeypatch):
        monkeypatch.setattr(platform_compat, "IS_MACOS", False)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        dirs = [d.as_posix() for d in cli_desktop.desktop_log_dirs({"XDG_CONFIG_HOME": "/cfg"})]
        assert "/cfg/KiroCrew/logs" in dirs

    def test_unresolvable_home_degrades_instead_of_raising(self, monkeypatch):
        """No HOME and no passwd entry must not raise out of a helper.

        Patches the module seam, not ``pathlib.Path.home``: patching the class
        attribute mutates pathlib process-wide and corrupts WindowsPath internals
        on the Windows shard.
        """
        monkeypatch.setattr(platform_compat, "IS_MACOS", False)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)

        def _no_home():
            raise RuntimeError("no home")

        monkeypatch.setattr(cli_desktop, "_home_dir", _no_home)
        assert cli_desktop.desktop_log_dirs({}) == ()


class TestReading:
    def test_reports_latest_and_peak(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv(perf_sampler.DEBUG_ENV_VAR, "1")
        art = tmp_path / "desktop-metrics.json"
        art.write_text(json.dumps(_doc()), encoding="utf-8")
        assert cli_desktop.desktop_cmd(_args(art)) == 0
        out = capsys.readouterr().out
        # The peak is the earlier sample, so a report that only showed "latest"
        # would hide the spike this command exists to find.
        assert "peak total" in out
        assert "12.0%" in out
        assert "1.0%" in out

    def test_json_mode_emits_the_raw_document(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv(perf_sampler.DEBUG_ENV_VAR, "1")
        art = tmp_path / "desktop-metrics.json"
        art.write_text(json.dumps(_doc()), encoding="utf-8")
        assert cli_desktop.desktop_cmd(_args(art, json=True)) == 0
        assert json.loads(capsys.readouterr().out)["version"] == 1

    def test_missing_artifact_explains_the_debug_requirement(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv(perf_sampler.DEBUG_ENV_VAR, "1")
        assert cli_desktop.desktop_cmd(_args(tmp_path / "absent.json")) == 3
        err = capsys.readouterr().err
        assert "KIROCREW_DEBUG" in err, "the usual cause must be named, not just 'not found'"

    def test_malformed_json_is_reported_not_raised(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv(perf_sampler.DEBUG_ENV_VAR, "1")
        art = tmp_path / "desktop-metrics.json"
        art.write_text("{not json", encoding="utf-8")
        assert cli_desktop.desktop_cmd(_args(art)) == 5
        assert "not valid JSON" in capsys.readouterr().err

    def test_a_future_version_is_refused_rather_than_misread(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv(perf_sampler.DEBUG_ENV_VAR, "1")
        art = tmp_path / "desktop-metrics.json"
        art.write_text(json.dumps(_doc(version=2)), encoding="utf-8")
        assert cli_desktop.desktop_cmd(_args(art)) == 5
        assert "version" in capsys.readouterr().err

    def test_a_json_array_is_refused(self, monkeypatch, capsys, tmp_path):
        """Valid JSON that is not an object must not crash the formatter."""
        monkeypatch.setenv(perf_sampler.DEBUG_ENV_VAR, "1")
        art = tmp_path / "desktop-metrics.json"
        art.write_text("[]", encoding="utf-8")
        assert cli_desktop.desktop_cmd(_args(art)) == 5

    def test_an_empty_sample_list_is_handled(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv(perf_sampler.DEBUG_ENV_VAR, "1")
        art = tmp_path / "desktop-metrics.json"
        art.write_text(json.dumps(_doc(samples=[])), encoding="utf-8")
        assert cli_desktop.desktop_cmd(_args(art)) == 0
        assert "no samples" in capsys.readouterr().out

    def test_a_directory_passed_as_path_is_not_found_rather_than_a_crash(
        self, monkeypatch, capsys, tmp_path
    ):
        monkeypatch.setenv(perf_sampler.DEBUG_ENV_VAR, "1")
        assert cli_desktop.desktop_cmd(_args(tmp_path)) == 3

    def test_missing_numeric_fields_render_as_dashes(self, monkeypatch, capsys, tmp_path):
        """A partial record must not raise a formatting error."""
        monkeypatch.setenv(perf_sampler.DEBUG_ENV_VAR, "1")
        doc = _doc(
            samples=[{"at": "t", "processes": [{"pid": 1}], "totalCpuPercent": None}]
        )
        art = tmp_path / "desktop-metrics.json"
        art.write_text(json.dumps(doc), encoding="utf-8")
        assert cli_desktop.desktop_cmd(_args(art)) == 0

    def test_unknown_subcommand_returns_two(self, monkeypatch, capsys):
        monkeypatch.setenv(perf_sampler.DEBUG_ENV_VAR, "1")
        assert cli_desktop.desktop_cmd(_args(desktop_cmd="bogus")) == 2


class TestMalformedSamples:
    """The artifact is a file on disk; it can be truncated or hand-edited."""

    def test_a_null_sample_entry_does_not_crash(self, monkeypatch, capsys, tmp_path):
        """The exact payload that reached first.get() and raised AttributeError."""
        monkeypatch.setenv(perf_sampler.DEBUG_ENV_VAR, "1")
        art = tmp_path / "desktop-metrics.json"
        art.write_text('{"version": 1, "samples": [null]}', encoding="utf-8")
        assert cli_desktop.desktop_cmd(_args(art)) == 0
        assert "no usable samples" in capsys.readouterr().out

    def test_mixed_valid_and_junk_entries_keep_the_valid_ones(
        self, monkeypatch, capsys, tmp_path
    ):
        monkeypatch.setenv(perf_sampler.DEBUG_ENV_VAR, "1")
        doc = _doc()
        doc["samples"] = [None, doc["samples"][0], "junk", 42]
        art = tmp_path / "desktop-metrics.json"
        art.write_text(json.dumps(doc), encoding="utf-8")
        assert cli_desktop.desktop_cmd(_args(art)) == 0
        out = capsys.readouterr().out
        assert "12.0%" in out, "the one real sample should still be reported"

    def test_a_non_list_samples_field_is_refused(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv(perf_sampler.DEBUG_ENV_VAR, "1")
        art = tmp_path / "desktop-metrics.json"
        art.write_text('{"version": 1, "samples": {"not": "a list"}}', encoding="utf-8")
        assert cli_desktop.desktop_cmd(_args(art)) == 5
        assert "malformed" in capsys.readouterr().err

    def test_a_sample_with_junk_processes_does_not_crash(self, monkeypatch, tmp_path):
        monkeypatch.setenv(perf_sampler.DEBUG_ENV_VAR, "1")
        doc = _doc(
            samples=[{"at": "t", "processes": [None, "x", 3], "totalCpuPercent": 1.0}]
        )
        art = tmp_path / "desktop-metrics.json"
        art.write_text(json.dumps(doc), encoding="utf-8")
        assert cli_desktop.desktop_cmd(_args(art)) == 0


class TestNonNumericValues:
    """max()/sort() compare key values, so one string among numbers raises TypeError.

    The `or 0` idiom does not save it: a non-empty string is truthy and is
    returned as the key.
    """

    def test_mixed_string_and_numeric_totals_do_not_crash(self, monkeypatch, tmp_path):
        monkeypatch.setenv(perf_sampler.DEBUG_ENV_VAR, "1")
        doc = _doc(
            samples=[
                {"at": "a", "processes": [], "totalCpuPercent": "high"},
                {"at": "b", "processes": [], "totalCpuPercent": 4.0},
            ]
        )
        art = tmp_path / "desktop-metrics.json"
        art.write_text(json.dumps(doc), encoding="utf-8")
        assert cli_desktop.desktop_cmd(_args(art)) == 0

    def test_mixed_string_and_numeric_process_cpu_does_not_crash(
        self, monkeypatch, capsys, tmp_path
    ):
        monkeypatch.setenv(perf_sampler.DEBUG_ENV_VAR, "1")
        doc = _doc(
            samples=[
                {
                    "at": "a",
                    "processes": [
                        {"pid": 1, "type": "Browser", "cpuPercent": "lots"},
                        {"pid": 2, "type": "Tab", "cpuPercent": 7.0},
                    ],
                    "totalCpuPercent": 7.0,
                }
            ]
        )
        art = tmp_path / "desktop-metrics.json"
        art.write_text(json.dumps(doc), encoding="utf-8")
        assert cli_desktop.desktop_cmd(_args(art)) == 0
        out = capsys.readouterr().out
        # The junk value sorts as 0 and renders as a dash, not as "lots".
        assert "lots" not in out
        assert "7.0%" in out

    def test_a_non_list_processes_value_is_not_iterated(self, monkeypatch, tmp_path):
        monkeypatch.setenv(perf_sampler.DEBUG_ENV_VAR, "1")
        for junk in ({"a": 1}, "text", 5):
            doc = _doc(samples=[{"at": "a", "processes": junk, "totalCpuPercent": 1.0}])
            art = tmp_path / "desktop-metrics.json"
            art.write_text(json.dumps(doc), encoding="utf-8")
            assert cli_desktop.desktop_cmd(_args(art)) == 0, junk

    def test_booleans_are_not_treated_as_numbers(self):
        """bool is an int subclass; `true` in a CPU field is corruption, not 1."""
        assert cli_desktop._num(True) == 0.0
        assert cli_desktop._fmt_pct(True) == "-"
        assert cli_desktop._fmt_int(True) == "-"

    def test_non_finite_values_are_rejected(self):
        assert cli_desktop._num(float("inf")) == 0.0
        assert cli_desktop._num(float("nan")) == 0.0
        assert cli_desktop._fmt_pct(float("nan")) == "-"


class TestNightlyProductDirectory:
    """Nightly installs under its own productName and so its own log directory.

    packaging/build-desktop.sh passes `-c.productName=KiroCrew Nightly` for nightly
    stamps, so probing only "KiroCrew" reported "not found" against a nightly app
    that was recording correctly -- and nightly users are the likeliest profilers.
    """

    def test_both_product_directories_are_probed_on_macos(self, monkeypatch):
        monkeypatch.setattr(platform_compat, "IS_MACOS", True)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        dirs = [d.as_posix() for d in cli_desktop.desktop_log_dirs({})]
        assert any(d.endswith("Library/Logs/KiroCrew") for d in dirs)
        assert any(d.endswith("Library/Logs/KiroCrew Nightly") for d in dirs)

    def test_both_product_directories_are_probed_on_linux(self, monkeypatch):
        monkeypatch.setattr(platform_compat, "IS_MACOS", False)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        dirs = [d.as_posix() for d in cli_desktop.desktop_log_dirs({"XDG_CONFIG_HOME": "/cfg"})]
        assert "/cfg/KiroCrew/logs" in dirs
        assert "/cfg/KiroCrew Nightly/logs" in dirs

    def test_both_product_directories_are_probed_on_windows(self, monkeypatch):
        monkeypatch.setattr(platform_compat, "IS_MACOS", False)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        dirs = [d.as_posix() for d in cli_desktop.desktop_log_dirs({"APPDATA": "/roam"})]
        assert "/roam/KiroCrew/logs" in dirs
        assert "/roam/KiroCrew Nightly/logs" in dirs

    def test_the_newest_artifact_wins_when_both_exist(self, monkeypatch, tmp_path):
        """With both installed, the run just reproduced is the one written last."""
        release = tmp_path / "KiroCrew"
        nightly = tmp_path / "KiroCrew Nightly"
        release.mkdir()
        nightly.mkdir()
        old = release / cli_desktop.ARTIFACT_NAME
        new = nightly / cli_desktop.ARTIFACT_NAME
        old.write_text("{}", encoding="utf-8")
        new.write_text("{}", encoding="utf-8")
        os.utime(old, (1000, 1000))
        os.utime(new, (2000, 2000))
        monkeypatch.setattr(
            cli_desktop, "desktop_log_dirs", lambda env=None: (release, nightly)
        )
        assert cli_desktop.find_metrics_artifact() == new
        # And the reverse ordering, so the result is mtime-driven not list-order.
        os.utime(old, (3000, 3000))
        assert cli_desktop.find_metrics_artifact() == old

    def test_the_missing_message_names_every_searched_directory(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv(perf_sampler.DEBUG_ENV_VAR, "1")
        monkeypatch.setattr(platform_compat, "IS_MACOS", True)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        assert cli_desktop.desktop_cmd(_args(tmp_path / "absent.json")) == 3
        err = capsys.readouterr().err
        assert "KiroCrew Nightly" in err, "the nightly path must be discoverable from the error"

    def test_product_names_stay_in_sync_with_the_packaging_script(self):
        """A rename in build-desktop.sh silently breaks discovery for that build."""
        sh = Path(__file__).resolve().parents[1] / "packaging" / "build-desktop.sh"
        if not sh.is_file():  # pragma: no cover - source checkout only
            pytest.skip("packaging scripts not present in this build")
        text = sh.read_text(encoding="utf-8")
        for name in cli_desktop.PRODUCT_NAMES:
            assert name in text, f"{name} not found in build-desktop.sh"


class TestSensitivePathGate:
    """--path is caller-supplied, and this command prints file contents."""

    def test_the_read_goes_through_the_centralized_gate(self, monkeypatch, tmp_path):
        """Not just "it works": the specific helper must be on the path.

        Reading with Path.read_text would bypass is_sensitive_path entirely, and
        that bypass is invisible in output-based assertions.
        """
        monkeypatch.setenv(perf_sampler.DEBUG_ENV_VAR, "1")
        art = tmp_path / "desktop-metrics.json"
        art.write_text(json.dumps(_doc()), encoding="utf-8")
        called: list[str] = []
        real = cli_desktop.safe_read_file

        def _spy(p: str) -> str:
            called.append(p)
            return real(p)

        monkeypatch.setattr(cli_desktop, "safe_read_file", _spy)
        assert cli_desktop.desktop_cmd(_args(art)) == 0
        assert called == [str(art)]

    def test_a_refused_sensitive_path_reports_instead_of_raising(
        self, monkeypatch, capsys, tmp_path
    ):
        monkeypatch.setenv(perf_sampler.DEBUG_ENV_VAR, "1")
        art = tmp_path / "desktop-metrics.json"
        art.write_text("{}", encoding="utf-8")

        def _refuse(_p: str) -> str:
            raise PermissionError("sensitive path")

        monkeypatch.setattr(cli_desktop, "safe_read_file", _refuse)
        assert cli_desktop.desktop_cmd(_args(art)) == 5
        err = capsys.readouterr().err
        assert "protected or resolves to a sensitive location" in err

    def test_the_discovered_path_is_gated_too(self, monkeypatch, tmp_path):
        """The app's log directory is user-writable, so a planted symlink there
        must be refused exactly as one passed on the command line."""
        monkeypatch.setenv(perf_sampler.DEBUG_ENV_VAR, "1")
        art = tmp_path / cli_desktop.ARTIFACT_NAME
        art.write_text(json.dumps(_doc()), encoding="utf-8")
        monkeypatch.setattr(cli_desktop, "desktop_log_dirs", lambda env=None: (tmp_path,))
        called: list[str] = []
        real = cli_desktop.safe_read_file

        def _spy(p: str) -> str:
            called.append(p)
            return real(p)

        monkeypatch.setattr(cli_desktop, "safe_read_file", _spy)
        # No --path: force the discovery route.
        assert cli_desktop.desktop_cmd(_args(None)) == 0
        assert called == [str(art)]


class TestParserWiring:
    def test_subcommand_dest_does_not_shadow_the_top_level_command(self):
        """A nested dest of "command" silently overwrites the top-level value.

        That exact collision made `kirocrew perf sample` fall through to the
        argparse help during layer 1, so it is pinned here too.
        """
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        cli_desktop.register_desktop_parser(sub)
        args = parser.parse_args(["desktop", "metrics"])
        assert args.command == "desktop"
        assert args.desktop_cmd == "metrics"

    def test_top_and_json_flags_parse(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        cli_desktop.register_desktop_parser(sub)
        args = parser.parse_args(["desktop", "metrics", "--json", "--top", "3"])
        assert args.json is True and args.top == 3

    def test_metrics_subcommand_is_required(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        cli_desktop.register_desktop_parser(sub)
        with pytest.raises(SystemExit):
            parser.parse_args(["desktop"])


class TestArtifactNameStaysInSync:
    def test_python_and_electron_agree_on_the_filename(self):
        """A rename on either side makes this command report a false negative."""
        js = (
            Path(__file__).resolve().parents[1]
            / "website"
            / "electron"
            / "perf-metrics.js"
        )
        if not js.is_file():  # pragma: no cover - source checkout only
            pytest.skip("electron sources not present in this build")
        text = js.read_text(encoding="utf-8")
        assert f'ARTIFACT_NAME = "{cli_desktop.ARTIFACT_NAME}"' in text

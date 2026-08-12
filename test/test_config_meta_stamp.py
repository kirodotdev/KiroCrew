"""``config.json`` always carries a ``meta`` stamp naming the build that wrote it.

The stamp is the first thing to check when a config looks like it came from an
older schema, so a write must never leave the file without one. The hazard is
specific to writers that rebuild the file from ``KiroCrewConfig.to_dict()``:
that mapping models the schema only, so any top-level key the dataclass does
not carry — ``meta`` among them — is absent from the output and the write drops
it. Writers that mutate the raw dict they read keep the block for free.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew import __version__
from kiro_crew.config.loader import stamp_config_meta

_OLD_STAMP = {"lastTouchedVersion": "0.0.1", "lastTouchedAt": "2020-01-01T00:00:00+00:00"}

# Values a user would notice losing, spread over several sections so a write
# that rebuilds the file has something to preserve besides the stamp.
_REAL_SETTINGS = {
    "agent": {"approval_mode": "interactive", "max_subagents": 8},
    "dashboard": {"theme_mode": "dark", "language": "zh-CN"},
    "session": {"timeout_secs": 7200},
    "timezone": "Asia/Shanghai",
}


def _set_args(key: str, value: str, *, local: bool = False) -> argparse.Namespace:
    return argparse.Namespace(config_action="set", key=key, value=value, file=None, local=local)


def _run_config_cmd(args: argparse.Namespace, config_dir: Path) -> None:
    from kiro_crew.cli_config import _config_cmd

    with (
        patch("kiro_crew.cli_config.config_path", return_value=config_dir / "config.json"),
        patch(
            "kiro_crew.cli_config.config_local_path",
            return_value=config_dir / "config.local.json",
        ),
        patch("kiro_crew.config.loader.config_dir", return_value=config_dir),
        patch("kiro_crew.cli_config.sel"),
    ):
        _config_cmd(args)


class TestStampConfigMeta:
    def test_stamps_current_version_and_a_timestamp(self):
        out = stamp_config_meta({"timezone": "UTC"})
        assert out["meta"]["lastTouchedVersion"] == __version__
        assert out["meta"]["lastTouchedAt"]

    def test_replaces_an_existing_stamp_rather_than_merging(self):
        """The block names the build writing now, so a stale one cannot survive."""
        out = stamp_config_meta({"meta": _OLD_STAMP, "timezone": "UTC"})
        assert out["meta"]["lastTouchedVersion"] == __version__
        assert out["meta"]["lastTouchedAt"] != _OLD_STAMP["lastTouchedAt"]

    def test_keeps_every_other_key(self):
        out = stamp_config_meta({"meta": _OLD_STAMP, **_REAL_SETTINGS})
        assert {k: v for k, v in out.items() if k != "meta"} == _REAL_SETTINGS

    def test_meta_is_written_first(self):
        """Leading position keeps the stamp readable in a hand-opened config."""
        assert next(iter(stamp_config_meta(_REAL_SETTINGS))) == "meta"

    def test_does_not_mutate_the_input(self):
        source = {"meta": _OLD_STAMP, "timezone": "UTC"}
        stamp_config_meta(source)
        assert source["meta"] == _OLD_STAMP


class TestCliConfigSetKeepsMeta:
    """``kirocrew config set <key> <value>`` rebuilds the file from the dataclass."""

    def test_set_refreshes_the_stamp_instead_of_dropping_it(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "crew"
        config_dir.mkdir()
        (config_dir / "config.json").write_text(
            json.dumps({"meta": _OLD_STAMP, **_REAL_SETTINGS}), encoding="utf-8"
        )

        _run_config_cmd(_set_args("timezone", "Europe/Berlin"), config_dir)

        saved = json.loads((config_dir / "config.json").read_text(encoding="utf-8"))
        assert saved["meta"]["lastTouchedVersion"] == __version__
        assert saved["meta"]["lastTouchedAt"] != _OLD_STAMP["lastTouchedAt"]
        assert saved["timezone"] == "Europe/Berlin"

    def test_set_stamps_a_config_that_never_had_meta(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "crew"
        config_dir.mkdir()
        (config_dir / "config.json").write_text(json.dumps(_REAL_SETTINGS), encoding="utf-8")

        _run_config_cmd(_set_args("timezone", "Europe/Berlin"), config_dir)

        saved = json.loads((config_dir / "config.json").read_text(encoding="utf-8"))
        assert saved["meta"]["lastTouchedVersion"] == __version__

    def test_set_from_file_stamps_the_written_config(self, tmp_path: Path) -> None:
        """``--file`` replaces the whole config, so it owns the new stamp too."""
        config_dir = tmp_path / "crew"
        config_dir.mkdir()
        source = tmp_path / "incoming.json"
        source.write_text(json.dumps({"meta": _OLD_STAMP, **_REAL_SETTINGS}), encoding="utf-8")

        args = argparse.Namespace(
            config_action="set", key=None, value=None, file=str(source), local=False
        )
        _run_config_cmd(args, config_dir)

        saved = json.loads((config_dir / "config.json").read_text(encoding="utf-8"))
        assert saved["meta"]["lastTouchedVersion"] == __version__
        assert saved["timezone"] == "Asia/Shanghai"

    def test_local_set_does_not_stamp_the_overlay(self, tmp_path: Path) -> None:
        """Only the base file carries the block; the overlay holds settings alone."""
        config_dir = tmp_path / "crew"
        config_dir.mkdir()

        _run_config_cmd(_set_args("timezone", "Europe/Berlin", local=True), config_dir)

        overlay = json.loads((config_dir / "config.local.json").read_text(encoding="utf-8"))
        assert "meta" not in overlay
        assert overlay["timezone"] == "Europe/Berlin"

    def test_set_from_file_refuses_a_non_object_payload(self, tmp_path: Path, capsys) -> None:
        """A JSON array parses but is not a config: refuse, leaving the file alone."""
        config_dir = tmp_path / "crew"
        config_dir.mkdir()
        existing = {"meta": _OLD_STAMP, **_REAL_SETTINGS}
        (config_dir / "config.json").write_text(json.dumps(existing), encoding="utf-8")
        source = tmp_path / "incoming.json"
        source.write_text("[1, 2, 3]", encoding="utf-8")

        args = argparse.Namespace(
            config_action="set", key=None, value=None, file=str(source), local=False
        )
        with pytest.raises(SystemExit) as exc:
            _run_config_cmd(args, config_dir)

        assert exc.value.code == 1
        assert "Not a config object" in capsys.readouterr().err
        assert json.loads((config_dir / "config.json").read_text(encoding="utf-8")) == existing


class TestConfigSaveKeepsMeta:
    def test_save_stamps_the_current_build(self, tmp_path: Path) -> None:
        from kiro_crew.config import KiroCrewConfig

        config_dir = tmp_path / "crew"
        config_dir.mkdir()
        (config_dir / "config.json").write_text(
            json.dumps({"meta": _OLD_STAMP, **_REAL_SETTINGS}), encoding="utf-8"
        )

        with (
            patch(
                "kiro_crew.config.loader.config_path", return_value=config_dir / "config.json"
            ),
            patch(
                "kiro_crew.config.loader.config_local_path",
                return_value=config_dir / "config.local.json",
            ),
            patch("kiro_crew.config.loader.config_dir", return_value=config_dir),
        ):
            KiroCrewConfig.load().save()
            saved = json.loads((config_dir / "config.json").read_text(encoding="utf-8"))

        assert saved["meta"]["lastTouchedVersion"] == __version__
        assert saved["agent"]["approval_mode"] == "interactive"


class TestRefreshConfigMetaStamp:
    """Upgrade-staleness repair for the stamp (#3102).

    The stamp is only written as a side effect of a config write, so an
    upgrade that never touches ``config.json`` leaves ``lastTouchedVersion``
    naming the previous build. ``refresh_config_meta_stamp`` (called once per
    gateway start, off the boot path) re-stamps exactly when the stored
    version differs — and must never rewrite, create, or clobber otherwise.
    """

    def _patch_home(self, config_dir: Path):
        return patch(
            "kiro_crew.config.loader.config_path", return_value=config_dir / "config.json"
        )

    def test_stale_stamp_is_refreshed_and_settings_survive(self, tmp_path: Path) -> None:
        from kiro_crew.config.loader import refresh_config_meta_stamp

        (tmp_path / "config.json").write_text(
            json.dumps({"meta": _OLD_STAMP, **_REAL_SETTINGS}), encoding="utf-8"
        )
        with self._patch_home(tmp_path):
            assert refresh_config_meta_stamp() is True
        saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert saved["meta"]["lastTouchedVersion"] == __version__
        assert saved["meta"]["lastTouchedAt"] != _OLD_STAMP["lastTouchedAt"]
        assert {k: v for k, v in saved.items() if k != "meta"} == _REAL_SETTINGS

    def test_missing_meta_block_is_stamped(self, tmp_path: Path) -> None:
        from kiro_crew.config.loader import refresh_config_meta_stamp

        (tmp_path / "config.json").write_text(json.dumps(_REAL_SETTINGS), encoding="utf-8")
        with self._patch_home(tmp_path):
            assert refresh_config_meta_stamp() is True
        saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert saved["meta"]["lastTouchedVersion"] == __version__

    def test_current_stamp_is_not_rewritten(self, tmp_path: Path) -> None:
        """No mtime churn / ``lastTouchedAt`` bump when nothing is stale."""
        from kiro_crew.config.loader import refresh_config_meta_stamp, stamp_config_meta

        current = stamp_config_meta(dict(_REAL_SETTINGS))
        raw = json.dumps(current)
        (tmp_path / "config.json").write_text(raw, encoding="utf-8")
        with self._patch_home(tmp_path):
            assert refresh_config_meta_stamp() is False
        assert (tmp_path / "config.json").read_text(encoding="utf-8") == raw

    def test_absent_file_is_left_absent(self, tmp_path: Path) -> None:
        from kiro_crew.config.loader import refresh_config_meta_stamp

        with self._patch_home(tmp_path):
            assert refresh_config_meta_stamp() is False
        assert not (tmp_path / "config.json").exists()

    def test_unreadable_file_is_never_clobbered(self, tmp_path: Path) -> None:
        """A torn/garbage config must not be replaced with a stamped empty one."""
        from kiro_crew.config.loader import refresh_config_meta_stamp

        (tmp_path / "config.json").write_text("{not json", encoding="utf-8")
        with self._patch_home(tmp_path):
            assert refresh_config_meta_stamp() is False
        assert (tmp_path / "config.json").read_text(encoding="utf-8") == "{not json"

    def test_refresh_routes_through_the_locked_primitive(self) -> None:
        """The refresh must hold the sidecar advisory lock for its read-modify-write.

        update_config_locked is 'the required path for new config.json
        mutations' (its own docstring): it serializes the whole read→write
        under the <config>.lock sidecar, so a boot-time refresh can never
        revert a concurrent settings write with its own earlier snapshot.
        A bare read_config_for_update + write_config_atomically pair
        reintroduces exactly that lost-update race.
        """
        import inspect

        from kiro_crew.config import loader

        src = inspect.getsource(loader.refresh_config_meta_stamp)
        assert "update_config_locked(" in src
        assert "write_config_atomically(" not in src

    def test_gateway_schedules_the_refresh_off_the_boot_path(self) -> None:
        """start_dashboard fires the refresh as a deferred, off-loop task.

        Structural on purpose: booting a full gateway to observe one config
        write is a heavyweight harness for a wiring fact. The refresh must be
        (a) present in start_dashboard, (b) wrapped in to_thread inside a
        created task, never awaited inline, and (c) scheduled AFTER the
        socket binds — an inline `await asyncio.to_thread(...)` placed
        before _start_site would satisfy (a) and (b) alone, so the ordering
        assertion is what actually pins the boot-path rule
        (AUTOSDE: no-new-work-on-gateway-boot-path).
        """
        import inspect

        from kiro_crew.dashboard import server

        src = inspect.getsource(server.start_dashboard)
        assert "asyncio.to_thread(refresh_config_meta_stamp)" in src
        assert "await refresh_config_meta_stamp" not in src
        assert "_stamp_task = asyncio.create_task(" in src
        assert src.index("await _start_site(") < src.index(
            "asyncio.to_thread(refresh_config_meta_stamp)"
        )


class TestDashboardNeverSurfacesThePersistedMarker:
    """The version the dashboard reports is the running build's, not the stamp.

    Issue #3102's reporter found ``lastTouchedVersion: 0.1.3`` in their config
    and reasonably suspected it fed the Settings header. It does not — every
    status producer reports ``kiro_crew.__version__`` — and this locks that
    in: wiring the persisted marker into any dashboard module fails here.
    """

    def test_status_producers_report_the_running_version(self) -> None:
        import kiro_crew
        from kiro_crew.dashboard import ws
        from kiro_crew.dashboard.handlers import updates

        assert ws._local_version == kiro_crew.__version__
        assert updates._local_version == kiro_crew.__version__

    def test_api_status_reads_the_module_version(self) -> None:
        import inspect

        from kiro_crew.dashboard import handlers_system

        src = inspect.getsource(handlers_system.api_status)
        assert '"version": kiro_crew.__version__' in src

    def test_no_dashboard_module_reads_the_marker(self) -> None:
        import re

        dashboard_dir = (
            Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "dashboard"
        )
        # Match an actual READ of the field — subscript or .get() — not a bare
        # mention: a comment documenting "the running version, NOT the
        # persisted lastTouchedVersion marker" is the natural way to record
        # this invariant and must not turn the suite red.
        read_pattern = re.compile(
            r"""\[\s*["']lastTouchedVersion["']\s*\]|get\(\s*["']lastTouchedVersion["']"""
        )
        offenders = [
            p
            for p in dashboard_dir.rglob("*.py")
            if read_pattern.search(p.read_text(encoding="utf-8", errors="ignore"))
        ]
        assert offenders == [], (
            "the persisted config stamp must never be surfaced as the app "
            f"version; remove the read from: {offenders}"
        )

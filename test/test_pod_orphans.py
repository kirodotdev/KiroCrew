"""Orphan pod homes and ``pod prune``.

Listing them with their age; ``--older-than``; routing every prune delete through
``stop_pod``; and never following a symlink or junction planted under the pod root. The
fixtures come from ``pod_helpers.py``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from pod_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _cp,
    _fake_node_toolchain,
    _isolate_pod_host_state,
    _systemd_backend,
)

from kiro_crew import platform_compat
from kiro_crew.pod import cli as pod_cli
from kiro_crew.pod import runtime as rt
from kiro_crew.pod.config import PodConfig


class TestOrphanHomes:
    """Neither platform reclaims from a post-stop hook, so a pod that goes away
    without a ``down`` leaves its HOME on either OS — and the report was gated to
    macOS, which is why the residue accumulated invisibly on Linux."""

    def _plane(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PodConfig:
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        c = PodConfig.load()
        for n in ("orphan", "running"):
            (c.pod_root / n).mkdir(parents=True)
        (c.pod_root / ".e2e-artifacts").mkdir()  # dot dirs are not pods
        monkeypatch.setattr(rt, "active_names", lambda cc: {"running"})
        return c

    def test_reported_on_linux(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        c = self._plane(tmp_path, monkeypatch)
        assert rt.orphan_homes(c) == ["orphan"]

    def test_ls_surfaces_them_with_the_reclaim_command(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        c = self._plane(tmp_path, monkeypatch)
        monkeypatch.setattr(rt, "health", lambda cfg, name, port, timeout=3: 200)
        pod_cli._ls(c, argparse.Namespace(json=False))
        out = capsys.readouterr().out
        assert "1 orphaned pod HOME(s)" in out
        assert "kirocrew pod down orphan" in out

    def test_the_json_shape_stays_live_pods_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """Three callers parse this array; orphans are human-output only."""
        c = self._plane(tmp_path, monkeypatch)
        monkeypatch.setattr(rt, "health", lambda cfg, name, port, timeout=3: 200)
        pod_cli._ls(c, argparse.Namespace(json=True))
        assert [r["name"] for r in json.loads(capsys.readouterr().out)] == ["running"]

    def test_ls_shows_each_orphans_age(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """An orphan left 5 minutes ago and one left 3 weeks ago need different
        handling; the HOME's mtime is the signal the report was missing."""
        c = self._plane(tmp_path, monkeypatch)
        monkeypatch.setattr(rt, "health", lambda cfg, name, port, timeout=3: 200)
        # flake-ok: wall-clock backdate; the age floors to whole days -- moved verbatim
        three_days = time.time() - 3 * 86400
        os.utime(c.pod_root / "orphan", (three_days, three_days))
        pod_cli._ls(c, argparse.Namespace(json=False))
        assert "3d ago" in capsys.readouterr().out

    def test_an_unstattable_orphan_still_gets_its_row(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """A HOME that vanished between enumeration and the report must not
        crash the listing — age is a hint, never a gate, on the read path."""
        c = self._plane(tmp_path, monkeypatch)
        pod_cli._print_orphans(c, ["ghost"])
        out = capsys.readouterr().out
        assert "ghost" in out
        assert "age unknown" in out


class TestRelativeAge:
    @pytest.mark.parametrize(
        "seconds,expected",
        [
            (0, "0s ago"),
            (59, "59s ago"),
            (60, "1m ago"),
            (3600, "1h ago"),
            (86400 * 3 + 3600, "3d ago"),
            (-10, "0s ago"),  # future mtime (clock skew) clamps, never negative
        ],
    )
    def test_largest_whole_unit(self, seconds: float, expected: str) -> None:
        assert pod_cli._relative_age(seconds) == expected


class TestParseOlderThan:
    @pytest.mark.parametrize(
        "spec,expected",
        [("3d", 3 * 86400.0), ("12h", 12 * 3600.0), ("30m", 1800.0), ("45s", 45.0)],
    )
    def test_accepted_forms(self, spec: str, expected: float) -> None:
        assert pod_cli._parse_older_than(spec) == expected

    @pytest.mark.parametrize("spec", ["", "3", "d", "3w", "1.5h", "3d12h", "-3d", "9" * 400 + "d"])
    def test_rejects_anything_else(self, spec: str) -> None:
        with pytest.raises(rt.PodError, match="invalid --older-than"):
            pod_cli._parse_older_than(spec)

    def test_the_digit_cap_keeps_the_timestamp_arithmetic_finite(self) -> None:
        """An unbounded count (a 400-digit day value) survives int arithmetic
        but overflows the float timestamp subtraction in _prune with an
        uncaught OverflowError; the largest accepted count must stay finite."""
        biggest = pod_cli._parse_older_than("999999999d")
        # flake-ok: two wall-clock reads well inside approx's tolerance -- moved verbatim
        assert time.time() - biggest == pytest.approx(time.time() - biggest)  # no OverflowError


class TestPrune:
    """`prune` is the N-at-once `down` for orphans: every delete routes through
    stop_pod (drain + verify), liveness is re-checked per name at delete time,
    and one bad name never aborts the sweep."""

    def _plane(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, orphans: tuple[str, ...]
    ) -> PodConfig:
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
        c = PodConfig.load()
        for n in orphans:
            (c.pod_root / n).mkdir(parents=True)
        monkeypatch.setattr(rt, "require_backend", lambda: None)
        monkeypatch.setattr(rt, "active_names", lambda cc: set())
        monkeypatch.setattr(rt, "is_active", lambda cc, n: False)
        monkeypatch.setattr(rt, "unit_state", lambda cc, n: ("inactive", 0))
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)
        # cleanup_home directly from prune would BE the race stop_pod exists to
        # close — any call that does not come through stop_pod is a regression.
        monkeypatch.setattr(
            rt, "cleanup_home", lambda *a, **k: pytest.fail("prune bypassed stop_pod")
        )
        return c

    def _ns(self, **kw) -> argparse.Namespace:
        # prune_all=True keeps the reclaim-behavior tests on a full sweep; the
        # age-gate default is covered by its own dedicated tests below.
        base = {"older_than": "3d", "json": False, "dry_run": False, "prune_all": True}
        base.update(kw)
        return argparse.Namespace(**base)

    def test_reclaims_every_orphan_through_stop_pod(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        c = self._plane(tmp_path, monkeypatch, ("a", "b"))
        stopped: list[str] = []
        monkeypatch.setattr(rt, "stop_pod", lambda cc, n: stopped.append(n) or _cp())
        rt.pin_checkout(c, "a", tmp_path / "co")
        pod_cli._prune(c, self._ns())
        out = capsys.readouterr().out
        assert stopped == ["a", "b"]
        assert not c.env_file("a").exists(), "the stale checkout pin must be cleared"
        assert "pruned: 2 reclaimed, 0 kept, 0 skipped, 0 failed" in out

    def test_older_than_keeps_the_young_orphan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        c = self._plane(tmp_path, monkeypatch, ("old", "young"))
        # flake-ok: wall-clock backdate two days past the threshold -- moved verbatim
        five_days = time.time() - 5 * 86400
        os.utime(c.pod_root / "old", (five_days, five_days))
        stopped: list[str] = []
        monkeypatch.setattr(rt, "stop_pod", lambda cc, n: stopped.append(n) or _cp())
        pod_cli._prune(c, self._ns(older_than="3d", prune_all=False))
        out = capsys.readouterr().out
        assert stopped == ["old"]
        assert "1 reclaimed, 1 kept" in out

    def test_liveness_is_rechecked_at_delete_time(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """A name can go active between enumeration and its turn in the loop —
        deleting under a live unit is the one unrecoverable outcome."""
        c = self._plane(tmp_path, monkeypatch, ("woke-up",))
        monkeypatch.setattr(rt, "is_active", lambda cc, n: True)
        monkeypatch.setattr(
            rt, "stop_pod", lambda cc, n: pytest.fail("stopped a live pod during prune")
        )
        pod_cli._prune(c, self._ns())
        assert "pod is now active" in capsys.readouterr().out

    def test_one_failure_does_not_abort_the_sweep(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """Per-name results: a prune where one of two succeeded must say which,
        keep going, and exit nonzero."""
        c = self._plane(tmp_path, monkeypatch, ("bad", "good"))
        monkeypatch.setattr(
            rt,
            "stop_pod",
            lambda cc, n: _cp(returncode=1, stderr="still writing") if n == "bad" else _cp(),
        )
        with pytest.raises(SystemExit) as exc:
            pod_cli._prune(c, self._ns())
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "still writing" in out
        assert "1 reclaimed" in out and "1 failed" in out

    def test_a_pod_error_on_one_name_is_contained(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        c = self._plane(tmp_path, monkeypatch, ("erroring", "fine"))

        def _stop(cc: PodConfig, n: str) -> subprocess.CompletedProcess:
            if n == "erroring":
                raise rt.PodError("launchctl cannot answer")
            return _cp()

        monkeypatch.setattr(rt, "stop_pod", _stop)
        with pytest.raises(SystemExit):
            pod_cli._prune(c, self._ns())
        out = capsys.readouterr().out
        assert "launchctl cannot answer" in out
        assert "1 reclaimed" in out

    def test_a_name_reclaimed_by_a_new_pod_keeps_its_pin(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """RECLAIMED_MARKER means the env file now pins the NEW pod's checkout."""
        c = self._plane(tmp_path, monkeypatch, ("handover",))
        rt.pin_checkout(c, "handover", tmp_path / "co")
        monkeypatch.setattr(rt, "stop_pod", lambda cc, n: _cp(stdout=rt.RECLAIMED_MARKER))
        pod_cli._prune(c, self._ns())
        assert c.env_file("handover").exists(), "a live pod's checkout pin must survive"
        assert "claimed by a new pod" in capsys.readouterr().out

    def test_macos_installed_plist_is_refused_at_delete_time(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The plist can be written by an `up` AFTER the orphan enumeration (which
        already excludes installed names) — the delete-time recheck is what
        stands between that pod and a bootout of its live service."""
        c = self._plane(tmp_path, monkeypatch, ("mid-up",))
        monkeypatch.setattr(rt, "IS_MACOS", True)
        plist = tmp_path / "mid-up.plist"
        plist.write_text("")
        monkeypatch.setattr(rt.launchd, "plist_path", lambda cc, n: plist)
        monkeypatch.setattr(
            rt, "stop_pod", lambda cc, n: pytest.fail("booted-out an installed pod")
        )
        res = pod_cli._prune_one(c, "mid-up")
        assert res["status"] == "skipped"
        assert "pod is now installed" in res["detail"]

    def test_json_shape_is_per_name_results(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        c = self._plane(tmp_path, monkeypatch, ("a",))
        monkeypatch.setattr(rt, "stop_pod", lambda cc, n: _cp())
        pod_cli._prune(c, self._ns(json=True))
        rows = json.loads(capsys.readouterr().out)
        assert rows == [{"name": "a", "status": "reclaimed", "detail": ""}]

    def test_json_stays_valid_when_the_delete_path_prints(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """stop_pod/cleanup_home print diagnostics to stdout; interleaved with
        the machine output they would corrupt the JSON document. They must be
        rerouted to stderr, not swallowed."""
        c = self._plane(tmp_path, monkeypatch, ("noisy",))

        def _stop(cc: PodConfig, n: str) -> subprocess.CompletedProcess:
            print("pod cleanup did not fully remove /x: still present")
            return _cp(returncode=1, stderr="teardown incomplete")

        monkeypatch.setattr(rt, "stop_pod", _stop)
        with pytest.raises(SystemExit):
            pod_cli._prune(c, self._ns(json=True))
        captured = capsys.readouterr()
        rows = json.loads(captured.out)  # the whole stdout must parse
        assert rows[0]["status"] == "failed"
        assert "still present" in captured.err

    def test_a_refused_invocation_is_still_audited(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bulk-destructive verb that exits on a refusal (dead backend, bad
        duration) must reach the audit trail — an unrecorded refused prune is
        invisible to the log."""
        c = self._plane(tmp_path, monkeypatch, ())
        events: list[tuple[str, str]] = []
        monkeypatch.setattr(
            pod_cli, "_audit", lambda op, outcome, res="", error="": events.append((op, outcome))
        )
        with pytest.raises(rt.PodError):
            pod_cli._prune(c, self._ns(older_than="banana", prune_all=False))
        assert ("pod.prune", "denied") in events

    def test_a_failed_enumeration_is_audited_before_the_refusal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        c = self._plane(tmp_path, monkeypatch, ())
        events: list[str] = []
        monkeypatch.setattr(
            pod_cli, "_audit", lambda op, outcome, res="", error="": events.append(outcome)
        )

        def _boom(cc: PodConfig) -> list[str]:
            raise rt.PodError("launchctl cannot enumerate")

        monkeypatch.setattr(rt, "orphan_homes", _boom)
        with pytest.raises(rt.PodError):
            pod_cli._prune(c, self._ns())
        assert "denied" in events

    def test_every_prune_one_path_emits_exactly_one_audit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The audit is the single exit chokepoint of _prune_one: no decision
        path — including the invalid-name refusal — may return without a SEL
        event, and none may double-audit."""
        c = self._plane(tmp_path, monkeypatch, ("plain",))
        events: list[tuple[str, str]] = []
        monkeypatch.setattr(
            pod_cli, "_audit", lambda op, outcome, res="", error="": events.append((outcome, res))
        )
        scenarios: list[tuple[str, dict, str]] = [
            ("invalid name", {}, "denied"),
            ("active", {"is_active": lambda cc, n: True}, "denied"),
            ("mid-restart", {"unit_state": lambda cc, n: ("activating", 1)}, "denied"),
            ("stop fails", {"stop_pod": lambda cc, n: _cp(returncode=1, stderr="x")}, "failure"),
            ("reclaimed", {"stop_pod": lambda cc, n: _cp()}, "allowed"),
            (
                "handover",
                {"stop_pod": lambda cc, n: _cp(stdout=rt.RECLAIMED_MARKER)},
                "allowed",
            ),
        ]
        for label, patches, expected in scenarios:
            # Re-pin the baseline before layering the scenario's patch, so a
            # previous scenario's monkeypatch cannot leak forward.
            monkeypatch.setattr(rt, "is_active", lambda cc, n: False)
            monkeypatch.setattr(rt, "unit_state", lambda cc, n: ("inactive", 0))
            for attr, fn in patches.items():
                monkeypatch.setattr(rt, attr, fn)
            events.clear()
            name = "not a pod" if label == "invalid name" else "plain"
            pod_cli._prune_one(c, name)
            assert len(events) == 1, f"{label}: expected exactly one audit, got {events}"
            assert events[0][0] == expected, f"{label}: outcome {events[0][0]} != {expected}"

    def test_nothing_to_prune_says_so(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        c = self._plane(tmp_path, monkeypatch, ())
        pod_cli._prune(c, self._ns())
        assert "no orphaned pod HOMEs to prune" in capsys.readouterr().out

    def test_prune_is_dispatchable(self) -> None:
        assert pod_cli._VERBS["prune"] is pod_cli._prune

    def test_a_bare_prune_keeps_a_fresh_crash_by_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """The CLI default is --older-than 3d: a bare `pod prune` must not
        sweep the minutes-old crash HOME an operator is still debugging — its
        logs are the only postmortem evidence. --all is the explicit opt-in."""
        c = self._plane(tmp_path, monkeypatch, ("fresh",))
        monkeypatch.setattr(rt, "stop_pod", lambda cc, n: pytest.fail("swept a fresh crash"))
        pod_cli._prune(c, self._ns(prune_all=False))  # CLI defaults
        assert "kept" in capsys.readouterr().out

    def test_a_restarting_unit_is_refused_at_delete_time(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """During the Restart=on-failure backoff the unit is `activating` — not
        active — so both the orphan scan and is_active miss it, and a stop here
        would cancel the pending restart and delete a running pod's HOME."""
        c = self._plane(tmp_path, monkeypatch, ("mid-backoff",))
        monkeypatch.setattr(rt, "unit_state", lambda cc, n: ("activating", 1))
        monkeypatch.setattr(
            rt, "stop_pod", lambda cc, n: pytest.fail("cancelled a pending restart")
        )
        pod_cli._prune(c, self._ns())
        assert "mid-transition or restarting" in capsys.readouterr().out

    def test_a_crash_looping_unit_is_refused_even_when_terminal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """restarts>0 means the service manager is still involved with this
        name — fail closed and leave it to an explicit `pod down`."""
        c = self._plane(tmp_path, monkeypatch, ("crashy",))
        monkeypatch.setattr(rt, "unit_state", lambda cc, n: ("failed", 2))
        monkeypatch.setattr(rt, "stop_pod", lambda cc, n: pytest.fail("reclaimed a managed unit"))
        pod_cli._prune(c, self._ns())
        assert "not orphaned" in capsys.readouterr().out

    def test_an_unknown_unit_state_fails_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        c = self._plane(tmp_path, monkeypatch, ("murky",))
        monkeypatch.setattr(rt, "unit_state", lambda cc, n: ("unknown", 0))
        monkeypatch.setattr(rt, "stop_pod", lambda cc, n: pytest.fail("deleted on no evidence"))
        pod_cli._prune(c, self._ns())
        assert "skipped" in capsys.readouterr().out

    def test_an_os_error_on_one_name_is_contained(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """Raw filesystem/subprocess failures (not just PodError) must become a
        per-name failed row — an escaped exception would hide every result
        already earned and strand the remaining orphans unprocessed."""
        c = self._plane(tmp_path, monkeypatch, ("cursed", "fine"))

        def _stop(cc: PodConfig, n: str) -> subprocess.CompletedProcess:
            if n == "cursed":
                raise PermissionError("env dir is read-only")
            return _cp()

        monkeypatch.setattr(rt, "stop_pod", _stop)
        with pytest.raises(SystemExit):
            pod_cli._prune(c, self._ns())
        out = capsys.readouterr().out
        assert "env dir is read-only" in out
        assert "1 reclaimed" in out

    def test_dry_run_classifies_without_deleting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        c = self._plane(tmp_path, monkeypatch, ("a", "b"))
        (c.pod_root / "not a pod").mkdir()  # invalid name: same verdict as a real run
        monkeypatch.setattr(rt, "stop_pod", lambda cc, n: pytest.fail("dry run deleted"))
        pod_cli._prune(c, self._ns(dry_run=True))
        out = capsys.readouterr().out
        assert "would-reclaim" in out
        assert "not a valid pod name" in out, "preview must match the real run's classification"
        assert "dry run: 2 would be reclaimed" in out
        assert (c.pod_root / "a").exists() and (c.pod_root / "b").exists()

    def test_a_stray_non_pod_directory_is_skipped_by_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """A directory that fails validate_name can never have a unit and can
        never be reclaimed by `down`; shelling a bogus systemctl stop for it
        would report failed and pin every future prune's exit at 1."""
        c = self._plane(tmp_path, monkeypatch, ())
        (c.pod_root / "not a pod").mkdir(parents=True)
        monkeypatch.setattr(rt, "stop_pod", lambda cc, n: pytest.fail("stopped a bogus unit"))
        pod_cli._prune(c, self._ns())
        assert "not a valid pod name" in capsys.readouterr().out

    def test_backend_absent_is_one_refusal_not_n_failures(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without a usable service manager the sweep must refuse once (the
        documented one-line `pod:` error), not report N stuck HOMEs."""
        c = self._plane(tmp_path, monkeypatch, ("a", "b"))

        def _absent() -> None:
            raise rt.PodBackendAbsent("no session bus")

        monkeypatch.setattr(rt, "require_backend", _absent)
        with pytest.raises(rt.PodError):
            pod_cli._prune(c, self._ns())

    def test_older_than_measures_activity_not_creation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """A pod created 5 days ago that crashed 10 minutes ago is the orphan
        being debugged — its fresh log write must keep it, even though the HOME
        directory's own mtime froze at creation."""
        c = self._plane(tmp_path, monkeypatch, ("fresh-crash",))
        home = c.pod_root / "fresh-crash"
        (home / "logs").mkdir()
        (home / "logs" / "gateway.log").write_text("boom")  # fresh mtime (now)
        # flake-ok: wall-clock backdate two days past the threshold -- moved verbatim
        five_days = time.time() - 5 * 86400
        os.utime(home / "logs", (five_days, five_days))
        os.utime(home, (five_days, five_days))
        monkeypatch.setattr(rt, "stop_pod", lambda cc, n: pytest.fail("reclaimed a fresh crash"))
        pod_cli._prune(c, self._ns(older_than="3d", prune_all=False))
        assert "kept" in capsys.readouterr().out


class TestOrphanSymlinkSafety:
    """A symlink under pod_root can point at a LIVE pod's HOME (or anywhere);
    following it — in the enumeration or at the delete chokepoint — turns a
    bulk reclaim into deleting a directory that was never an orphan."""

    def test_orphan_homes_never_lists_a_symlink(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        c = PodConfig.load()
        (c.pod_root / "live").mkdir(parents=True)
        (c.pod_root / "alias").symlink_to(c.pod_root / "live")
        monkeypatch.setattr(rt, "active_names", lambda cc: {"live"})
        assert rt.orphan_homes(c) == []

    def test_cleanup_home_refuses_to_follow_a_symlink(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        c = PodConfig.load()
        victim = c.pod_root / "live"
        victim.mkdir(parents=True)
        (victim / "sessions.db").write_text("precious")
        (c.pod_root / "alias").symlink_to(victim)
        assert rt.cleanup_home(c, "alias") == 2
        assert (victim / "sessions.db").exists(), "followed the link and deleted the target"
        assert "symlink" in capsys.readouterr().out

    def test_cleanup_home_deletes_by_name_never_by_resolved_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The symlink pre-check can be raced (entry swapped to a symlink
        between check and delete). What makes that race harmless is that
        rmtree receives the UNRESOLVED name — stdlib rmtree refuses a
        top-level symlink — never the resolved path, which at swap time is
        the live sibling the link points at. Pin the argument."""
        real = tmp_path / "real-pods"
        real.mkdir()
        (tmp_path / "pods").symlink_to(real)  # unresolved != resolved spelling
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        c = PodConfig.load()
        (c.pod_root / "demo").mkdir()
        seen: list[Path] = []
        monkeypatch.setattr(
            rt.shutil, "rmtree", lambda p, ignore_errors=False: seen.append(Path(p))
        )
        # flake-ok: the retry loop's real 0.1 s pauses never reach an assert -- moved verbatim
        rt.cleanup_home(c, "demo")
        # Asserted over EVERY call rather than as a one-element list. The reclaim
        # retries while the OS releases handles, and a faked rmtree never makes the
        # entry disappear, so the bounded window runs to its cap here. The property
        # under test is WHICH path is passed, not how many times: quantifying over
        # all attempts pins it harder, because a retry that resolved the name would
        # now fail too.
        assert seen, "rmtree must be called at least once"
        assert all(
            p == c.pod_root / "demo" for p in seen
        ), "rmtree must get the unresolved name on every attempt"
        assert seen[0] != (c.pod_root / "demo").resolve(), "test setup lost the distinction"

    def test_a_dangling_symlink_swap_is_reported_not_swallowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """rmtree refuses a symlink SILENTLY under ignore_errors, and a
        DANGLING link's resolved target does not exist — so verifying by
        target existence would report a clean reclaim while the link remains
        as residue the orphan scan (which skips symlinks) can never surface
        again. The verification must be lexists on the entry itself."""
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        c = PodConfig.load()
        entry = c.pod_root / "swapped"
        entry.mkdir(parents=True)  # a real dir at pre-check time

        def _swap_then_refuse(p, ignore_errors=False):
            # Net effect of the worst-case interleaving: by the time rmtree
            # runs, the entry is a dangling symlink, which rmtree refuses
            # (silently, under ignore_errors).
            entry.rmdir()
            entry.symlink_to(c.pod_root / "gone")

        monkeypatch.setattr(rt.shutil, "rmtree", _swap_then_refuse)
        rc = rt.cleanup_home(c, "swapped")
        assert rc == 1, "a surviving entry must be a reported failure, never rc 0"
        assert "symlink" in capsys.readouterr().out


class TestOrphanJunctionSafety:
    """The same threat as :class:`TestOrphanSymlinkSafety`, in the shape it takes on
    Windows. A directory symlink there needs SeCreateSymbolicLinkPrivilege, so the
    only link an unelevated same-user writer can plant under pod_root is a
    JUNCTION -- which answers True to ``is_dir()`` and False to ``is_symlink()``.
    Every ``is_symlink()`` guard in the orphan / reclaim path was therefore blind
    to exactly the link that can occur. Each case runs twice: once
    through the product's own ``symlink_or_junction`` helper (the real shape on the
    Windows shards, a symlink elsewhere), once with the junction shape SIMULATED so
    POSIX shards pin the guard as well -- the OS junction oracle
    ``platform_compat._ISJUNCTION`` recognises one real directory while every
    ``pathlib`` predicate keeps its true answer."""

    @staticmethod
    def _plane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PodConfig:
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        c = PodConfig.load()
        c.pod_root.mkdir(parents=True, exist_ok=True)
        return c

    @staticmethod
    def _fake_junction(monkeypatch: pytest.MonkeyPatch, entry: Path) -> None:
        entry.mkdir()
        monkeypatch.setattr(platform_compat, "_ISJUNCTION", lambda p: Path(p) == entry)
        assert platform_compat.is_link_or_junction(entry)
        assert entry.is_dir() and not entry.is_symlink()

    def test_orphan_homes_never_lists_a_directory_link(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        c = self._plane(tmp_path, monkeypatch)
        (c.pod_root / "live").mkdir()
        platform_compat.symlink_or_junction(str(c.pod_root / "live"), str(c.pod_root / "alias"))
        monkeypatch.setattr(rt, "active_names", lambda cc: {"live"})
        assert rt.orphan_homes(c) == []

    def test_orphan_homes_never_lists_a_junction_shaped_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        c = self._plane(tmp_path, monkeypatch)
        self._fake_junction(monkeypatch, c.pod_root / "alias")
        monkeypatch.setattr(rt, "active_names", lambda cc: set())
        assert rt.orphan_homes(c) == []

    def test_cleanup_home_refuses_a_directory_link_as_a_link(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """rc 2 and named as a link. On Windows the old guard resolved the
        junction to the live sibling, passed containment, and -- because stdlib
        rmtree refuses a junction root -- spun the whole retry window before
        reporting the sibling's files as "something is still writing there"."""
        c = self._plane(tmp_path, monkeypatch)
        victim = c.pod_root / "live"
        victim.mkdir()
        (victim / "sessions.db").write_text("precious")
        platform_compat.symlink_or_junction(str(victim), str(c.pod_root / "alias"))

        assert rt.cleanup_home(c, "alias") == 2

        assert (victim / "sessions.db").exists(), "followed the link and deleted the target"
        out = capsys.readouterr().out
        assert "junction" in out and "still writing" not in out

    def test_cleanup_home_refuses_a_junction_shaped_entry_on_every_platform(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """Simulated shape. The old guard passed the entry as a plain child of
        pod_root and rmtree'd it: rc 0 over a planted link."""
        c = self._plane(tmp_path, monkeypatch)
        entry = c.pod_root / "alias"
        self._fake_junction(monkeypatch, entry)
        monkeypatch.setattr(
            rt.shutil, "rmtree", lambda p, ignore_errors=False: pytest.fail("deleted a link")
        )

        assert rt.cleanup_home(c, "alias") == 2

        assert entry.exists()
        assert "junction" in capsys.readouterr().out

    def test_a_dangling_junction_shaped_swap_is_reported_as_a_link(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """The post-delete residue check. An entry swapped to a junction between
        the pre-check and the delete survives rmtree (refused, silently under
        ignore_errors); the old ``is_symlink()`` residue test then spun the whole
        retry window and reported the entry's contents as survivors instead of
        naming the link. Simulated so every platform pins it: the swap happens
        inside the faked rmtree, and the oracle recognises the swapped entry."""
        c = self._plane(tmp_path, monkeypatch)
        entry = c.pod_root / "swapped"
        entry.mkdir()  # a real dir at pre-check time
        calls: list[int] = []

        def _swap_then_refuse(p, ignore_errors=False):
            calls.append(1)
            # Net effect of the interleaving: the entry is now a junction, which
            # rmtree refuses. Teach the oracle that from here on.
            monkeypatch.setattr(platform_compat, "_ISJUNCTION", lambda q: Path(q) == entry)

        monkeypatch.setattr(rt.shutil, "rmtree", _swap_then_refuse)
        rc = rt.cleanup_home(c, "swapped")
        assert rc == 1, "a surviving entry must be a reported failure, never rc 0"
        assert len(calls) == 1, "a swapped link must stop the retry window at once"
        out = capsys.readouterr().out
        assert "junction" in out and "still writing" not in out

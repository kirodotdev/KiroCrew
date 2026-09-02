"""The last three parity gaps: reconciler, orphan-clone sweep, autoPublish gate.

* **Reconciler** — a watcher exits when it runs out of nudges or the PR looks done, so a
  filed PR whose CI went red AFTERWARDS had nobody driving it. Upstream swept for this
  (``reconcile_failing_crs`` + ``promote_deferred`` + a global cap); the port had no
  equivalent and watchers only started from an explicit route call or ``cr_filed``.
* **Orphan-clone sweep** — a watcher removes its own clone on a clean exit, but a crash
  or a gateway restart leaves a full checkout behind until the disk fills.
* **autoPublish** — upstream could mark a fully-green DRAFT ready-for-review (opt-in,
  off by default); the port dropped it, so every green draft waited on a manual click.

The reconciler is tested by INJECTION (``findings`` + ``status_for``), so no forge or
network is involved.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.apps.builtins.auto_improvement.backend import pr_watchers as W


@pytest.fixture()
def registry(tmp_path: Path) -> W.PRWatcherRegistry:
    """A registry that never spawns a thread or touches git."""
    return W.PRWatcherRegistry(
        autostart=False, isolate_clone=False, clones_root=str(tmp_path / "clones")
    )


def _finding(fp: str, *, status: str = "filed", pr: str | None = None) -> dict:
    return {
        "fp": fp,
        "status": status,
        "pr": pr if pr is not None else f"https://github.com/o/r/pull/{abs(hash(fp)) % 9000 + 1}",
        "kind": "bug",
        "target": "src/m.py::f",
    }


def _red(**over: object) -> dict:
    base = {"ok": True, "state": "OPEN", "verdict": "PROGRESS", "checks": {"failingCount": 2}}
    base.update(over)
    return base


def _green(**over: object) -> dict:
    base = {
        "ok": True,
        "state": "OPEN",
        "draft": True,
        "verdict": "READY",
        "checks": {"failingCount": 0, "total": 5},
        "unresolvedThreads": 0,
    }
    base.update(over)
    return base


class TestNeedsAttention:
    def test_failing_checks_need_attention(self) -> None:
        assert W._needs_attention(_red()) is True

    def test_blocked_verdict_needs_attention(self) -> None:
        assert W._needs_attention({"ok": True, "verdict": "BLOCKED", "checks": {}}) is True

    def test_a_green_pr_does_not(self) -> None:
        assert W._needs_attention(_green()) is False

    def test_a_merged_or_closed_pr_is_left_alone(self) -> None:
        """Re-driving a finished change would nudge something already decided."""
        assert W._needs_attention(_red(state="MERGED")) is False
        assert W._needs_attention(_red(state="CLOSED")) is False
        assert W._needs_attention(_red(merged=True)) is False

    def test_an_unavailable_status_is_not_actionable(self) -> None:
        assert W._needs_attention({"ok": False}) is False
        assert W._needs_attention({}) is False


class TestReconcile:
    def test_a_red_filed_pr_is_restarted(self, registry: W.PRWatcherRegistry) -> None:
        out = registry.reconcile_failing_prs(
            findings=[_finding("aa")], status_for=lambda _u: _red(), force=True
        )
        assert out["started"] == ["aa"]

    def test_a_green_pr_is_left_alone(self, registry: W.PRWatcherRegistry) -> None:
        out = registry.reconcile_failing_prs(
            findings=[_finding("aa")], status_for=lambda _u: _green(), force=True
        )
        assert out["started"] == []

    def test_a_non_filed_finding_is_ignored(self, registry: W.PRWatcherRegistry) -> None:
        """failed_gate/no_defect have no PR to fix."""
        out = registry.reconcile_failing_prs(
            findings=[_finding("aa", status="failed_gate")],
            status_for=lambda _u: _red(),
            force=True,
        )
        assert out["started"] == []

    def test_a_queued_pr_without_a_url_is_ignored(self, registry: W.PRWatcherRegistry) -> None:
        out = registry.reconcile_failing_prs(
            findings=[_finding("aa", pr="QUEUED:aa")], status_for=lambda _u: _red(), force=True
        )
        assert out["started"] == []

    def test_a_status_fetch_failure_does_not_abort_the_sweep(
        self, registry: W.PRWatcherRegistry
    ) -> None:
        """One unreachable PR must not stop the others being reconciled."""
        calls: list[str] = []

        bad = _finding("bb")

        def _status(url: str) -> dict:
            calls.append(url)
            if url == bad["pr"]:
                raise RuntimeError("forge unreachable")
            return _red()

        out = registry.reconcile_failing_prs(
            findings=[_finding("aa"), bad], status_for=_status, force=True
        )
        assert len(calls) == 2
        assert out["started"] == ["aa"]  # bb's fetch raised and was skipped

    def test_the_sweep_is_rate_limited(self, registry: W.PRWatcherRegistry) -> None:
        first = registry.reconcile_failing_prs(findings=[], status_for=lambda _u: {}, force=False)
        second = registry.reconcile_failing_prs(findings=[], status_for=lambda _u: {}, force=False)
        assert "skipped" not in first
        assert second.get("skipped") == "rate-limited"


class TestConcurrencyCap:
    def test_findings_over_the_cap_are_deferred_not_dropped(
        self, registry: W.PRWatcherRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The difference between a bounded queue and a lost signal."""
        monkeypatch.setattr(W, "MAX_ACTIVE_WATCHERS", 1)
        # Every registered watcher reports alive so the cap actually binds (the code
        # asks _live_fps, which snapshots threads outside the lock).
        monkeypatch.setattr(registry, "_live_fps", lambda: list(registry._watchers))
        out = registry.reconcile_failing_prs(
            findings=[_finding("aa"), _finding("bbb"), _finding("cccc")],
            status_for=lambda _u: _red(),
            force=True,
        )
        assert len(out["started"]) == 1
        assert len(out["deferredNow"]) == 2
        assert out["deferred"] == 2  # queue DEPTH, distinct from this sweep's list
        assert out["active"] <= 1

    def test_active_summary_reports_the_cap_and_slots(self, registry: W.PRWatcherRegistry) -> None:
        s = registry.active_summary()
        assert s["cap"] == W.MAX_ACTIVE_WATCHERS
        assert s["slots"] == W.MAX_ACTIVE_WATCHERS - s["active"]

    def test_promote_deferred_is_a_noop_with_nothing_queued(
        self, registry: W.PRWatcherRegistry
    ) -> None:
        assert registry.promote_deferred() == 0


class TestOrphanCloneSweep:
    def test_a_clone_with_no_live_watcher_is_removed(self, tmp_path: Path) -> None:
        root = tmp_path / "clones"
        root.mkdir()
        orphan = root / ("a" * 8 + "-" + "0" * 12)
        (orphan / ".git").mkdir(parents=True)
        assert W.sweep_orphan_clones(clones_root=str(root)) == 1
        assert not orphan.exists()

    def test_a_path_this_module_did_not_create_is_never_touched(self, tmp_path: Path) -> None:
        """The sweep deletes directories, so it only matches its own naming scheme."""
        root = tmp_path / "clones"
        root.mkdir()
        for name in ("important-data", "notahash-zzzzzzzzzzzz", "aa-0", "-000000000000"):
            (root / name).mkdir()
        assert W.sweep_orphan_clones(clones_root=str(root)) == 0
        assert len(list(root.iterdir())) == 4

    def test_a_symlink_is_not_followed(self, tmp_path: Path) -> None:
        root = tmp_path / "clones"
        root.mkdir()
        real = tmp_path / "precious"
        real.mkdir()
        (root / ("b" * 8 + "-" + "1" * 12)).symlink_to(real, target_is_directory=True)
        assert W.sweep_orphan_clones(clones_root=str(root)) == 0
        assert real.exists()

    def test_a_missing_root_is_not_an_error(self, tmp_path: Path) -> None:
        assert W.sweep_orphan_clones(clones_root=str(tmp_path / "nope")) == 0

    def test_a_watcher_that_registers_mid_sweep_keeps_its_clone(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """TOCTOU: the sweep snapshots ownership once, releases the lock, then deletes. A
        watcher that registers (and creates its clone) in that gap is NOT in the stale snapshot,
        so a naive sweep deleted an active watcher's tree and lost its unexported work. The fix
        holds `reg._lock` across the final ownership check AND the `rmtree` together
        (`_delete_clone_if_unowned`), so a directory it is about to delete cannot become owned in
        between. Simulated by making that atomic check-and-delete report the directory as owned
        (the watcher that registered in the gap), so the sweep must spare it. Raised by the GPT
        review.
        """
        root = tmp_path / "clones"
        root.mkdir()
        # A directory matching the clone-name shape, holding a repo. The bulk snapshot sees NO
        # live watchers (none are registered), so this directory reaches the removal loop as an
        # apparent orphan — exactly the state after the snapshot but before a mid-sweep register.
        clone = root / ("c" * 8 + "-" + "2" * 12)
        (clone / ".git").mkdir(parents=True)

        # `_delete_clone_if_unowned` is the atomic (lock-held) check-and-delete the removal loop
        # calls. Have it report this directory as still owned (returns False = not deleted) — the
        # watcher that registered inside the lock window — so the sweep spares it.
        def _not_deleted(registry, child):
            return False

        monkeypatch.setattr(W, "_delete_clone_if_unowned", _not_deleted)
        removed = W.sweep_orphan_clones(clones_root=str(root))
        assert removed == 0, "an active watcher's clone was swept despite the in-lock check"
        assert clone.exists(), "the just-registered watcher's clone was deleted mid-sweep"

    def test_the_sweep_still_removes_a_genuinely_unowned_clone_after_the_recheck(
        self, tmp_path: Path
    ) -> None:
        """The recheck must not disable the sweep: a clone no watcher owns at the moment of
        deletion is still reclaimed, or the fix would leak disk without bound."""
        root = tmp_path / "clones"
        root.mkdir()
        orphan = root / ("d" * 8 + "-" + "3" * 12)
        (orphan / ".git").mkdir(parents=True)
        assert W.sweep_orphan_clones(clones_root=str(root)) == 1
        assert not orphan.exists()

    def test_the_ownership_check_and_delete_are_under_one_held_lock(self) -> None:
        """Structural: the residual race was a check-then-release-then-delete. The ownership
        test and the `rmtree` must both be INSIDE a single `with reg._lock:` block, and the
        check must inspect `_threads`/`_watchers` directly rather than calling `is_alive` /
        the bulk helper (which take the same non-reentrant lock and would self-deadlock)."""
        import inspect

        src = inspect.getsource(W._delete_clone_if_unowned)
        lock_at = src.index("with reg._lock:")
        rmtree_at = src.index("shutil.rmtree(")
        assert lock_at < rmtree_at, "the rmtree is not inside the held lock"
        # The rmtree must not be dedented back out of the `with` block: everything from the lock
        # to the rmtree stays in one block, so no `return`-then-delete-outside pattern.
        between = src[lock_at:rmtree_at]
        assert "reg._lock" not in between.replace("with reg._lock:", "", 1), (
            "the lock is released and re-taken between the check and the delete"
        )
        # Must read thread state directly, not via the lock-taking helpers.
        assert "reg._threads.get(" in src, "does not inspect thread state directly under the lock"
        assert "reg.is_alive(" not in src, (
            "calls is_alive() inside the held non-reentrant lock — self-deadlock"
        )

    def test_a_watcher_registering_during_the_sweep_is_not_swept(self, tmp_path, monkeypatch):
        """Behavioral race check: a watcher whose registration lands WHILE the sweep holds the
        lock (modeled by registering it the instant the sweep inspects `_watchers`) keeps its
        clone. Exercises the real `_delete_clone_if_unowned`, not a stub."""
        import threading

        reg = W.get_registry()
        # ISOLATE the clone root to tmp_path so the sweep sees ONLY this test's directory — the
        # real shared scratch dir may hold stray clone-shaped dirs from other tests, which the
        # sweep would legitimately reclaim and perturb the count.
        root = tmp_path / "clones"
        root.mkdir()
        monkeypatch.setattr(reg, "_clones_root", str(root))
        # Give the racing watcher a real fingerprint and point its clone at a directory the
        # sweep will encounter.
        fp = "racyfp"
        clone = Path(reg._clone_dir(fp))
        clone.parent.mkdir(parents=True, exist_ok=True)
        (clone / ".git").mkdir(parents=True, exist_ok=True)
        # Sweep the clone's OWN root so `_clone_dir(fp)` is the directory under inspection.
        sweep_root = str(clone.parent)

        st = W.WatcherState(fp=fp, pr="https://github.com/o/r/pull/7")
        # A live-looking thread so the ownership predicate treats the watcher as owning its clone.
        live = threading.Thread(target=lambda: __import__("time").sleep(2.0))
        live.start()
        try:
            with reg._lock:
                reg._watchers[fp] = st
                reg._threads[fp] = live
            removed = W.sweep_orphan_clones(clones_root=sweep_root)
            assert clone.exists(), "a live watcher's clone was swept"
            assert removed == 0
        finally:
            with reg._lock:
                reg._watchers.pop(fp, None)
                reg._threads.pop(fp, None)
            live.join(timeout=3.0)


class TestAutoPublishGate:
    def test_a_fully_green_draft_is_publishable(self) -> None:
        allowed, reason = W.auto_publish_gate(_green())
        assert allowed is True and "green" in reason

    @pytest.mark.parametrize(
        "status,why",
        [
            (_green(draft=False), "not a draft"),
            (_green(verdict="PROGRESS"), "CI still running"),
            (_green(verdict="BLOCKED"), "hard problem"),
            (_green(checks={"failingCount": 1, "total": 3}), "failing checks"),
            (_green(checks={"failingCount": 0, "total": 0}), "no checks ran"),
            (_green(unresolvedThreads=2), "unresolved review threads"),
            (_green(state="MERGED"), "already merged"),
            ({"ok": False}, "status unavailable"),
            ({}, "no data"),
        ],
    )
    def test_anything_short_of_green_is_refused(self, status: dict, why: str) -> None:
        """Fail-CLOSED: a wrong 'yes' publishes an unvouched change; a wrong 'no' costs
        one click."""
        allowed, _ = W.auto_publish_gate(status)
        assert allowed is False, f"should refuse when {why}"

    def test_publish_is_skipped_while_the_flag_is_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(W, "auto_publish_enabled", lambda: False)
        ok, reason = W.publish_if_authorized("https://github.com/o/r/pull/1", _green())
        assert ok is False and "disabled" in reason

    def test_the_flag_alone_does_not_publish_a_red_pr(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two independent conditions: the flag AND the gate."""
        monkeypatch.setattr(W, "auto_publish_enabled", lambda: True)
        called: list[tuple] = []
        monkeypatch.setattr(W, "_gh", lambda *a, **k: called.append(a))
        ok, _ = W.publish_if_authorized("https://github.com/o/r/pull/1", _red())
        assert ok is False and called == []

    def test_publishing_only_ever_runs_pr_ready(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Never merge, never enable auto-merge."""
        import subprocess as sp

        monkeypatch.setattr(W, "auto_publish_enabled", lambda: True)
        seen: list[tuple] = []

        def _fake_gh(*args: str, **_kw: object) -> sp.CompletedProcess:
            seen.append(args)
            return sp.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")

        monkeypatch.setattr(W, "_gh", _fake_gh)
        ok, _ = W.publish_if_authorized("https://github.com/o/r/pull/7", _green())
        assert ok is True
        assert seen == [("pr", "ready", "https://github.com/o/r/pull/7")]
        assert not any("merge" in a for call in seen for a in call)

    def test_a_gh_failure_is_reported_not_raised(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess as sp

        monkeypatch.setattr(W, "auto_publish_enabled", lambda: True)
        monkeypatch.setattr(
            W,
            "_gh",
            lambda *a, **k: sp.CompletedProcess(
                args=list(a), returncode=1, stdout="", stderr="no auth"
            ),
        )
        ok, reason = W.publish_if_authorized("https://github.com/o/r/pull/7", _green())
        assert ok is False and "gh pr ready failed" in reason


class TestAutoPublishIsActuallyReachable:
    """`autoPublish` was a DEAD SWITCH: `publish_if_authorized` had no production caller, so
    turning the config key on left green drafts untouched. Raised by review of this branch.

    The reconcile sweep is the right home — it already fetches each PR's status, and the
    branch it takes for a PR that needs no fixing is exactly the one a fully-green draft
    lands in.
    """

    def test_the_sweep_calls_publish_for_a_healthy_pr(self, monkeypatch) -> None:
        from kiro_crew.apps.builtins.auto_improvement.backend import pr_watchers

        seen: list[str] = []

        def _record(pr: str, status: dict) -> tuple[bool, str]:
            seen.append(pr)
            return True, "green"

        monkeypatch.setattr(pr_watchers, "publish_if_authorized", _record)
        reg = pr_watchers.PRWatcherRegistry()
        monkeypatch.setattr(reg, "_should_reconcile", lambda: True, raising=False)
        healthy = {
            "ok": True,
            "state": "OPEN",
            "draft": True,
            "verdict": "READY",
            "checks": {"failingCount": 0, "total": 3},
            "unresolvedThreads": 0,
        }
        out = reg.reconcile_failing_prs(
            findings=[{"fp": "abc", "pr": "https://github.com/o/r/pull/1", "status": "filed"}],
            status_for=lambda _url: healthy,
        )
        assert seen == ["https://github.com/o/r/pull/1"], "the sweep never tried to publish"
        assert out.get("published") == ["abc"]

    def test_a_publish_fault_does_not_fail_the_sweep(self, monkeypatch) -> None:
        """One bad PR must not stop the reconcile pass."""
        from kiro_crew.apps.builtins.auto_improvement.backend import pr_watchers

        def _boom(pr, status):
            raise RuntimeError("gh exploded")

        monkeypatch.setattr(pr_watchers, "publish_if_authorized", _boom)
        reg = pr_watchers.PRWatcherRegistry()
        healthy = {
            "ok": True,
            "state": "OPEN",
            "draft": True,
            "verdict": "READY",
            "checks": {"failingCount": 0, "total": 3},
            "unresolvedThreads": 0,
        }
        out = reg.reconcile_failing_prs(
            findings=[{"fp": "abc", "pr": "https://github.com/o/r/pull/1", "status": "filed"}],
            status_for=lambda _url: healthy,
        )
        assert isinstance(out, dict)  # swept without raising

    def test_publish_is_wired_from_the_reconcile_path(self) -> None:
        """Structural: the call must exist in production code, not only in tests."""
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.backend import pr_watchers

        src = inspect.getsource(pr_watchers.PRWatcherRegistry.reconcile_failing_prs)
        assert "publish_if_authorized(" in src, "autoPublish is a dead switch again"

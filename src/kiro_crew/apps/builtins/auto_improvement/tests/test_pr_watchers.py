"""Per-PR watcher sessions — the nudge loop, its bounds, and its isolation invariant.

Nothing here spawns a real agent or touches a network remote: the runner is a stub
returning a canned :class:`AgentResult`-shaped object, and ``fetch_pr_status`` is
replaced with a coroutine yielding scripted verdicts. The clone-isolation tests do
use real ``git`` against a repo built in ``tmp_path`` — that is the point of them,
and the "remote" they are pointed at is a ``.invalid`` URL that cannot resolve.
"""

from __future__ import annotations

import asyncio
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps.builtins.auto_improvement.backend import pr_checks, pr_watchers, store

WAIT_S = 10.0


# ── helpers ──────────────────────────────────────────────────────────────────


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=60, check=False
    )


def _tiny_repo(root: Path, *, branch: str = "fix/thing") -> Path:
    """A real one-commit repo with a live-looking origin and a feature branch.

    The origin URL is deliberately live-SHAPED (and unresolvable) so the
    neutralization tests are asserting on something that would otherwise be a
    reachable remote rather than on an already-dead value.
    """
    repo = root / "shared-clone"
    repo.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.invalid", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "initial", cwd=repo)
    _git("remote", "add", "origin", "https://example.invalid/owner/repo.git", cwd=repo)
    _git("update-ref", "refs/remotes/origin/main", "HEAD", cwd=repo)
    _git("checkout", "-q", "-b", branch, cwd=repo)
    (repo / "app.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    _git("commit", "-q", "-am", "candidate change", cwd=repo)
    return repo


class StubResult:
    """Duck-types :class:`~..spine.agent_runner.AgentResult`."""

    def __init__(self, ok: bool = True, text: str = "fixed the failing test") -> None:
        self.ok = ok
        self.text = text
        self.error = "" if ok else "stub failure"
        self.cost_usd = 0.0
        self.duration_s = 0.0


class StubRunner:
    """Records every ``run`` call. Never launches anything."""

    def __init__(self, result: StubResult | None = None) -> None:
        self.result = result or StubResult()
        self.calls: list[dict[str, Any]] = []

    def run(self, prompt: str, **kwargs: Any) -> StubResult:
        self.calls.append({"prompt": prompt, **kwargs})
        return self.result


def _status(
    verdict: str = pr_checks.VERDICT_PROGRESS,
    *,
    failing: list[str] | None = None,
    reason: str = "failing checks: ci",
) -> dict[str, Any]:
    names = failing if failing is not None else ["ci"]
    return {
        "ok": True,
        "url": "https://github.com/owner/repo/pull/7",
        "number": 7,
        "title": "speed up f()",
        "state": "OPEN",
        "draft": True,
        "mergeable": "mergeable",
        "headBranch": "fix/thing",
        "baseBranch": "main",
        "unresolvedThreads": 0,
        "checks": pr_checks.summarize_checks([{"name": n, "conclusion": "FAILURE"} for n in names]),
        "verdict": verdict,
        "verdictReason": reason,
    }


@pytest.fixture()
def loop() -> Any:
    """A real event loop on its own thread — the gateway loop the watcher bridges to."""
    made = asyncio.new_event_loop()
    thread = threading.Thread(target=made.run_forever, daemon=True)
    thread.start()
    yield made
    made.call_soon_threadsafe(made.stop)
    thread.join(timeout=5.0)
    made.close()


@pytest.fixture()
def scripted(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Replace ``fetch_pr_status`` with a coroutine serving a scripted queue.

    The last entry repeats, so a test can say "PROGRESS forever" with one item.
    """
    script: list[dict[str, Any]] = []
    seen: list[str] = []

    async def _fake(url: str, *, refresh: bool = False) -> dict[str, Any]:
        seen.append(url)
        if not script:
            return _status()
        return script[min(len(seen) - 1, len(script) - 1)]

    monkeypatch.setattr(pr_watchers.pr_checks, "fetch_pr_status", _fake)
    return type("Scripted", (), {"script": script, "seen": seen})()


def _registry(loop: Any, runner: StubRunner, **kwargs: Any) -> pr_watchers.PRWatcherRegistry:
    return pr_watchers.PRWatcherRegistry(
        loop=loop, runner_factory=lambda: runner, isolate_clone=False, **kwargs
    )


def _await_status(
    reg: pr_watchers.PRWatcherRegistry, fp: str, statuses: set[str], timeout: float = WAIT_S
) -> dict[str, Any]:
    """Poll until the watcher reaches one of ``statuses``. Fails the test on timeout."""
    deadline = time.monotonic() + timeout
    snapshot: dict[str, Any] = {}
    while time.monotonic() < deadline:
        snapshot = reg.status(fp) or {}
        if snapshot.get("status") in statuses:
            return snapshot
        time.sleep(0.02)
    raise AssertionError(f"watcher never reached {statuses}; last={snapshot}")


def _await_gone(path: Path, timeout: float = WAIT_S) -> None:
    """Poll until ``path`` no longer exists. Fails the test on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"{path} was never removed")


# ── clone isolation: the load-bearing safety control ─────────────────────────


class TestCloneIsolation:
    def test_both_fetch_and_push_urls_are_neutralized(self, tmp_path: Path) -> None:
        """A per-PR clone must have NO live origin URL — fetch or push.

        Disabling only push would leave the real URL in the tree's config, one
        explicit-URL push away from being reachable by an auto-approved agent.
        """
        shared = _tiny_repo(tmp_path)
        clone, err = pr_watchers.setup_isolated_clone(
            str(shared), str(tmp_path / "iso"), branch="fix/thing", base_ref="main"
        )
        assert err == ""
        fetch_url = _git("remote", "get-url", "origin", cwd=Path(clone)).stdout.strip()
        push_url = _git("remote", "get-url", "--push", "origin", cwd=Path(clone)).stdout.strip()
        assert fetch_url == pr_watchers.DISABLED_NO_PUSH
        assert push_url == pr_watchers.DISABLED_NO_PUSH
        assert "example.invalid" not in fetch_url + push_url

    def test_assert_origin_neutralized_accepts_a_clean_clone(self, tmp_path: Path) -> None:
        shared = _tiny_repo(tmp_path)
        clone, err = pr_watchers.setup_isolated_clone(str(shared), str(tmp_path / "iso"))
        assert err == ""
        ok, offenders = pr_watchers.assert_origin_neutralized(clone)
        assert ok and offenders == []

    def test_assert_origin_neutralized_catches_a_re_pointed_remote(self, tmp_path: Path) -> None:
        """The post-turn check must catch an agent re-pointing origin at a real remote."""
        shared = _tiny_repo(tmp_path)
        clone, err = pr_watchers.setup_isolated_clone(str(shared), str(tmp_path / "iso"))
        assert err == ""
        _git("remote", "set-url", "origin", "https://example.invalid/x/y.git", cwd=Path(clone))
        ok, offenders = pr_watchers.assert_origin_neutralized(clone)
        assert not ok
        assert any("example.invalid" in url for url in offenders)
        # …and re-neutralizing must make it clean again.
        pr_watchers.neutralize_origin(clone)
        assert pr_watchers.assert_origin_neutralized(clone)[0]

    def test_push_url_alone_re_pointed_is_caught(self, tmp_path: Path) -> None:
        """A push-only re-point is the subtler breach: fetch still reads as disabled."""
        shared = _tiny_repo(tmp_path)
        clone, err = pr_watchers.setup_isolated_clone(str(shared), str(tmp_path / "iso"))
        assert err == ""
        _git(
            "remote",
            "set-url",
            "--push",
            "origin",
            "https://example.invalid/x/y.git",
            cwd=Path(clone),
        )
        ok, offenders = pr_watchers.assert_origin_neutralized(clone)
        assert not ok and offenders

    def test_clone_carries_the_feature_branch_and_base_ref(self, tmp_path: Path) -> None:
        shared = _tiny_repo(tmp_path)
        clone, err = pr_watchers.setup_isolated_clone(
            str(shared), str(tmp_path / "iso"), branch="fix/thing", base_ref="main"
        )
        assert err == ""
        head = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=Path(clone)).stdout.strip()
        assert head == "fix/thing"
        # The base must be reachable locally so the agent can rebase/diff without a remote.
        assert _git("rev-parse", "refs/remotes/origin/main", cwd=Path(clone)).returncode == 0

    def test_missing_shared_clone_is_refused_not_degraded(self, tmp_path: Path) -> None:
        """A clone failure must NOT fall back to the shared tree, which has a live URL."""
        clone, err = pr_watchers.setup_isolated_clone(str(tmp_path / "nope"), str(tmp_path / "iso"))
        assert clone == ""
        assert "not a directory" in err

    def test_symlinked_destination_is_refused(self, tmp_path: Path) -> None:
        shared = _tiny_repo(tmp_path)
        target = tmp_path / "real"
        target.mkdir()
        link = tmp_path / "link"
        link.symlink_to(target)
        clone, err = pr_watchers.setup_isolated_clone(str(shared), str(link))
        assert clone == "" and "symlink" in err


# ── the nudge loop ───────────────────────────────────────────────────────────


class TestNudgeLoop:
    def test_ready_verdict_stops_the_watcher_without_an_agent_call(
        self, loop: Any, scripted: Any
    ) -> None:
        """A green PR must end the watcher immediately — no agent turn, no cost."""
        scripted.script.append(_status(pr_checks.VERDICT_READY, failing=[], reason="checks green"))
        runner = StubRunner()
        reg = _registry(loop, runner)
        reg.start(fp="fp-ready", pr="https://github.com/owner/repo/pull/7")
        snap = _await_status(reg, "fp-ready", {pr_watchers.STATUS_READY})
        assert snap["verdict"] == pr_checks.VERDICT_READY
        assert runner.calls == []
        assert not reg.is_alive("fp-ready")

    def test_blocked_verdict_stops_the_watcher(self, loop: Any, scripted: Any) -> None:
        """A closed PR cannot be fixed by editing code — stop and surface it."""
        scripted.script.append(
            _status(pr_checks.VERDICT_BLOCKED, failing=[], reason="pull request was closed")
        )
        runner = StubRunner()
        reg = _registry(loop, runner)
        reg.start(fp="fp-blocked", pr="https://github.com/owner/repo/pull/7")
        snap = _await_status(reg, "fp-blocked", {pr_watchers.STATUS_BLOCKED})
        assert "closed" in snap["lastNote"]
        assert runner.calls == []

    def test_nudge_bound_is_respected(self, loop: Any, scripted: Any) -> None:
        """PROGRESS forever must stop at max_nudges, not run unbounded agent turns."""
        scripted.script.append(_status(pr_checks.VERDICT_PROGRESS))
        runner = StubRunner()
        reg = _registry(loop, runner)
        reg.start(
            fp="fp-bound",
            pr="https://github.com/owner/repo/pull/7",
            max_nudges=3,
            interval_s=0.0,
        )
        snap = _await_status(reg, "fp-bound", {pr_watchers.STATUS_EXHAUSTED})
        assert snap["nudges"] == 3
        assert len(runner.calls) == 3
        assert not reg.is_alive("fp-bound")

    def test_progress_then_ready_stops_after_one_pass(self, loop: Any, scripted: Any) -> None:
        scripted.script.extend(
            [
                _status(pr_checks.VERDICT_PROGRESS),
                _status(pr_checks.VERDICT_READY, failing=[], reason="checks green"),
            ]
        )
        runner = StubRunner()
        reg = _registry(loop, runner)
        reg.start(
            fp="fp-then-ready",
            pr="https://github.com/owner/repo/pull/7",
            max_nudges=6,
            interval_s=0.0,
        )
        _await_status(reg, "fp-then-ready", {pr_watchers.STATUS_READY})
        assert len(runner.calls) == 1

    def test_agent_gets_the_allowlisted_tools_and_the_clone_cwd(
        self, loop: Any, scripted: Any, tmp_path: Path
    ) -> None:
        scripted.script.append(_status(pr_checks.VERDICT_PROGRESS))
        runner = StubRunner()
        reg = _registry(loop, runner)
        reg.start(
            fp="fp-tools",
            pr="https://github.com/owner/repo/pull/7",
            clone=str(tmp_path),
            max_nudges=1,
            interval_s=0.0,
        )
        _await_status(reg, "fp-tools", {pr_watchers.STATUS_EXHAUSTED})
        call = runner.calls[0]
        assert call["cwd"] == str(tmp_path)
        assert call["allowed_tools"] == ["Bash", "Read", "Edit", "Write", "Grep", "Glob"]
        assert call["timeout_s"] == pr_watchers.DEFAULT_NUDGE_TIMEOUT_S

    def test_a_failed_status_fetch_consumes_a_pass_and_does_not_wedge(
        self, loop: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A provider outage for the whole budget must end the watcher, not spin."""

        async def _broken(url: str, *, refresh: bool = False) -> dict[str, Any]:
            return {"ok": False, "error": "provider down", "url": url}

        monkeypatch.setattr(pr_watchers.pr_checks, "fetch_pr_status", _broken)
        runner = StubRunner()
        reg = _registry(loop, runner)
        reg.start(
            fp="fp-down",
            pr="https://github.com/owner/repo/pull/7",
            max_nudges=2,
            interval_s=0.0,
        )
        _await_status(reg, "fp-down", {pr_watchers.STATUS_EXHAUSTED})
        assert runner.calls == []

    def test_a_raising_runner_does_not_kill_the_watcher(self, loop: Any, scripted: Any) -> None:
        scripted.script.append(_status(pr_checks.VERDICT_PROGRESS))

        class Boom:
            def run(self, prompt: str, **kwargs: Any) -> StubResult:
                raise RuntimeError("agent exploded")

        reg = pr_watchers.PRWatcherRegistry(loop=loop, runner_factory=Boom, isolate_clone=False)
        reg.start(
            fp="fp-boom", pr="https://github.com/owner/repo/pull/7", max_nudges=1, interval_s=0.0
        )
        snap = _await_status(reg, "fp-boom", {pr_watchers.STATUS_EXHAUSTED})
        assert snap["status"] == pr_watchers.STATUS_EXHAUSTED
        log = reg.get_log("fp-boom")
        assert any("agent exploded" in line["text"] for line in log["lines"])

    def test_an_isolation_breach_mid_loop_is_terminal(
        self, loop: Any, scripted: Any, tmp_path: Path
    ) -> None:
        """If a turn re-points origin, the loop must stop rather than grant more turns."""
        shared = _tiny_repo(tmp_path)
        scripted.script.append(_status(pr_checks.VERDICT_PROGRESS))
        breaches: list[str] = []

        class Breacher:
            """Stands in for an agent whose Bash call re-points the remote."""

            def run(self, prompt: str, **kwargs: Any) -> StubResult:
                cwd = kwargs.get("cwd") or ""
                breaches.append(cwd)
                _git(
                    "remote", "set-url", "origin", "https://example.invalid/x/y.git", cwd=Path(cwd)
                )
                return StubResult()

        reg = pr_watchers.PRWatcherRegistry(
            loop=loop,
            runner_factory=Breacher,
            isolate_clone=True,
            clones_root=str(tmp_path / "clones"),
        )
        reg.start(
            fp="fp-breach",
            pr="https://github.com/owner/repo/pull/7",
            branch="fix/thing",
            base_ref="main",
            clone=str(shared),
            max_nudges=5,
            interval_s=0.0,
        )
        snap = _await_status(reg, "fp-breach", {pr_watchers.STATUS_ERROR})
        assert "re-pointed" in snap["lastNote"]
        assert len(breaches) == 1  # stopped after the breaching pass, not 5 passes
        log = reg.get_log("fp-breach")
        assert any("re-pointed" in line["text"] for line in log["lines"])


# ── start / stop / status / log surface ──────────────────────────────────────


class TestRegistrySurface:
    def test_status_and_list_shapes(self, loop: Any, scripted: Any) -> None:
        scripted.script.append(_status(pr_checks.VERDICT_READY, failing=[], reason="green"))
        reg = _registry(loop, StubRunner())
        reg.start(
            fp="fp-shape",
            pr="https://github.com/owner/repo/pull/7",
            kind="perf",
            target="app.f",
            title="speed up f()",
            branch="fix/thing",
            base_ref="main",
        )
        _await_status(reg, "fp-shape", {pr_watchers.STATUS_READY})
        snap = reg.status("fp-shape")
        assert snap is not None
        for key in (
            "fp",
            "pr",
            "kind",
            "target",
            "title",
            "branch",
            "baseRef",
            "status",
            "nudges",
            "maxNudges",
            "intervalSeconds",
            "lastNote",
            "verdict",
            "verdictReason",
            "fixing",
            "clone",
            "startedAt",
            "updatedAt",
        ):
            assert key in snap, key
        assert snap["kind"] == "perf" and snap["baseRef"] == "main"
        listed = reg.list_sessions()
        assert [row["fp"] for row in listed] == ["fp-shape"]

    def test_status_of_an_unknown_fp_is_none(self, loop: Any) -> None:
        assert _registry(loop, StubRunner()).status("nope") is None

    def test_get_log_shape_and_incremental_since(self, loop: Any, scripted: Any) -> None:
        scripted.script.append(_status(pr_checks.VERDICT_PROGRESS))
        reg = _registry(loop, StubRunner())
        reg.start(
            fp="fp-log", pr="https://github.com/owner/repo/pull/7", max_nudges=2, interval_s=0.0
        )
        _await_status(reg, "fp-log", {pr_watchers.STATUS_EXHAUSTED})
        first = reg.get_log("fp-log")
        assert first["lines"] and first["nextSince"] == len(first["lines"])
        assert set(first["lines"][0]) == {"ts", "kind", "text"}
        # A follow-up poll at nextSince must return nothing new.
        assert reg.get_log("fp-log", since=first["nextSince"])["lines"] == []

    def test_get_log_of_an_unknown_fp_reports_an_error(self, loop: Any) -> None:
        out = _registry(loop, StubRunner()).get_log("nope")
        assert out["lines"] == [] and out["error"]

    def test_log_ring_is_bounded(self, loop: Any) -> None:
        """The UI polls and never drains, so an overnight watcher must not grow forever."""
        reg = _registry(loop, StubRunner())
        st = pr_watchers.WatcherState(fp="fp-ring", pr="x")
        for i in range(pr_watchers.MAX_LOG_LINES + 25):
            reg._log(st, "stage", f"line {i}")
        assert len(st.log) == pr_watchers.MAX_LOG_LINES
        assert st.log_total == pr_watchers.MAX_LOG_LINES + 25

    def test_since_survives_ring_overflow(self, loop: Any) -> None:
        """A poller that fell behind a full ring must resume, not replay from zero."""
        reg = _registry(loop, StubRunner())
        st = pr_watchers.WatcherState(fp="fp-over", pr="x")
        with reg._lock:
            reg._watchers["fp-over"] = st
        for i in range(pr_watchers.MAX_LOG_LINES + 10):
            reg._log(st, "stage", f"line {i}")
        out = reg.get_log("fp-over", since=5)
        assert out["nextSince"] == pr_watchers.MAX_LOG_LINES + 10
        assert len(out["lines"]) == pr_watchers.MAX_LOG_LINES

    def test_start_is_idempotent_per_fingerprint(self, loop: Any, scripted: Any) -> None:
        """A re-filed finding must not race a second thread onto the same PR."""
        scripted.script.append(_status(pr_checks.VERDICT_PROGRESS))
        reg = _registry(loop, StubRunner())
        first = reg.start(
            fp="fp-dup", pr="https://github.com/owner/repo/pull/7", max_nudges=1, interval_s=0.0
        )
        second = reg.start(fp="fp-dup", pr="https://github.com/owner/repo/pull/7")
        assert first is second
        assert len(reg.list_sessions()) == 1

    def test_a_queued_pr_is_not_watchable(self, loop: Any) -> None:
        """``QUEUED:<fp>`` means no PR was created — there is nothing live to read."""
        reg = _registry(loop, StubRunner())
        st = reg.start(fp="fp-queued", pr="QUEUED:fp-queued")
        assert st.status == pr_watchers.STATUS_BLOCKED
        assert not reg.is_alive("fp-queued")
        assert pr_watchers.is_watchable_pr("QUEUED:abc") is False
        assert pr_watchers.is_watchable_pr("https://github.com/o/r/pull/1") is True
        assert pr_watchers.is_watchable_pr("https://gitlab.com/o/r/-/merge_requests/2") is True
        assert pr_watchers.is_watchable_pr("") is False

    def test_start_without_a_loop_is_an_error_not_a_crash(self, scripted: Any) -> None:
        reg = pr_watchers.PRWatcherRegistry(runner_factory=StubRunner, isolate_clone=False)
        st = reg.start(fp="fp-noloop", pr="https://github.com/owner/repo/pull/7")
        assert st.status == pr_watchers.STATUS_ERROR
        assert "loop" in st.last_note

    def test_stop_ends_the_watcher_and_leaks_no_live_thread(self, loop: Any, scripted: Any) -> None:
        """A stopped watcher must actually release its thread."""
        scripted.script.append(_status(pr_checks.VERDICT_PROGRESS))
        reg = _registry(loop, StubRunner())
        reg.start(
            fp="fp-stop",
            pr="https://github.com/owner/repo/pull/7",
            max_nudges=50,
            interval_s=0.05,
        )
        _await_status(reg, "fp-stop", {pr_watchers.STATUS_NUDGING})
        assert reg.stop("fp-stop") is True
        stopped = reg.status("fp-stop") or {}
        assert stopped["status"] == pr_watchers.STATUS_STOPPED
        deadline = time.monotonic() + WAIT_S
        while reg.is_alive("fp-stop") and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not reg.is_alive("fp-stop"), "stopped watcher leaked a live thread"

    def test_stop_of_an_unknown_fp_is_false(self, loop: Any) -> None:
        assert _registry(loop, StubRunner()).stop("nope") is False

    def test_stop_all_signals_every_active_watcher(self, loop: Any, scripted: Any) -> None:
        scripted.script.append(_status(pr_checks.VERDICT_PROGRESS))
        reg = _registry(loop, StubRunner())
        for fp in ("fp-a", "fp-b"):
            reg.start(
                fp=fp,
                pr="https://github.com/owner/repo/pull/7",
                max_nudges=50,
                interval_s=0.05,
            )
        _await_status(reg, "fp-a", {pr_watchers.STATUS_NUDGING})
        _await_status(reg, "fp-b", {pr_watchers.STATUS_NUDGING})
        assert reg.stop_all() == 2
        deadline = time.monotonic() + WAIT_S
        while (reg.is_alive("fp-a") or reg.is_alive("fp-b")) and (time.monotonic() < deadline):
            time.sleep(0.02)
        assert not reg.is_alive("fp-a") and not reg.is_alive("fp-b")

    def test_watcher_threads_are_daemon_threads(self, loop: Any, scripted: Any) -> None:
        """A 30-minute agent turn must never hold up gateway shutdown."""
        scripted.script.append(_status(pr_checks.VERDICT_PROGRESS))
        reg = _registry(loop, StubRunner())
        reg.start(
            fp="fp-daemon",
            pr="https://github.com/owner/repo/pull/7",
            max_nudges=50,
            interval_s=0.05,
        )
        _await_status(reg, "fp-daemon", {pr_watchers.STATUS_NUDGING})
        with reg._lock:
            thread = reg._threads["fp-daemon"]
        assert thread.daemon is True
        reg.stop("fp-daemon")

    def test_module_singleton_is_stable(self) -> None:
        assert pr_watchers.get_registry() is pr_watchers.get_registry()


# ── clone lifecycle + fix export ─────────────────────────────────────────────


class TestGitOutputDecoding:
    """``_git`` must survive a repository that holds non-UTF-8 bytes.

    `git diff` prints the CONTENT of changed files, and repositories legitimately contain
    binary (a PNG fixture) or non-UTF-8 text (a latin-1 source). Under a strict decode the
    UnicodeDecodeError is raised inside ``subprocess.communicate``, so it is NOT something
    callers can read off ``returncode`` as data: it propagated out of `_export_is_durable`,
    past `_run_agent_pass`, and killed the whole watcher with STATUS_ERROR — every PR in a
    repo containing one binary file. Regression for the strict-decode default.
    """

    def _repo_with_binary(self, tmp_path: Path) -> Path:
        repo = tmp_path / "binrepo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
        (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, check=True)
        # 0x89 is the PNG magic byte — the exact byte that raised in the field.
        (repo / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 8)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "add binary"], cwd=repo, check=True)
        return repo

    def test_a_binary_diff_does_not_raise(self, tmp_path: Path) -> None:
        repo = self._repo_with_binary(tmp_path)
        # Must not raise UnicodeDecodeError, and must report the diff SUCCEEDED so callers
        # read "cannot tell" only when git actually failed.
        proc = pr_watchers._git("-C", str(repo), "diff", "HEAD~1...HEAD", timeout=60)
        assert proc.returncode == 0

    def test_a_binary_diff_is_not_reported_empty(self, tmp_path: Path) -> None:
        """The dangerous failure is the opposite one: lenient decoding must not fabricate
        emptiness. `_export_is_durable` reads an empty diff as "this pass produced nothing"
        and lets the clone holding the only copy of the agent's commits be deleted."""
        repo = self._repo_with_binary(tmp_path)
        proc = pr_watchers._git("-C", str(repo), "diff", "HEAD~1...HEAD", timeout=60)
        assert (proc.stdout or "").strip() != ""
        assert "shot.png" in proc.stdout


class TestCloneLifecycleAndExport:
    def test_clone_dir_is_collision_resistant(self, loop: Any, tmp_path: Path) -> None:
        """Sanitizing alone can collapse two fingerprints onto one dir — and this code
        deletes that dir."""
        reg = _registry(loop, StubRunner(), clones_root=str(tmp_path))
        a = reg._clone_dir("perf/app.py::f")
        b = reg._clone_dir("perf/app.py__f")
        assert a != b
        assert Path(a).parent == tmp_path

    def test_cleanup_only_removes_this_watchers_own_clone(self, loop: Any, tmp_path: Path) -> None:
        reg = _registry(loop, StubRunner(), clones_root=str(tmp_path / "clones"))
        st = pr_watchers.WatcherState(fp="fp-clean", pr="x")
        mine = Path(reg._clone_dir("fp-clean"))
        mine.mkdir(parents=True)
        (mine / "f.txt").write_text("x", encoding="utf-8")
        someone_else = tmp_path / "not-mine"
        someone_else.mkdir()
        reg._cleanup_clone(st, str(someone_else))
        assert someone_else.is_dir(), "cleanup must not touch a foreign path"
        reg._cleanup_clone(st, str(mine))
        assert not mine.exists()

    def test_the_pass_fix_is_exported_to_the_pr_queue(
        self, loop: Any, scripted: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The clone is disposable and cannot push, so the diff must survive it."""
        monkeypatch.setattr(store, "data_dir", lambda: tmp_path / "data")
        shared = _tiny_repo(tmp_path)
        scripted.script.append(_status(pr_checks.VERDICT_PROGRESS))

        class Editor:
            """An agent that makes and commits a real change in the clone."""

            def run(self, prompt: str, **kwargs: Any) -> StubResult:
                cwd = Path(kwargs["cwd"])
                (cwd / "app.py").write_text("def f():\n    return 3\n", encoding="utf-8")
                _git("commit", "-q", "-am", "fix the failing check", cwd=cwd)
                return StubResult()

        reg = pr_watchers.PRWatcherRegistry(
            loop=loop,
            runner_factory=Editor,
            isolate_clone=True,
            clones_root=str(tmp_path / "clones"),
        )
        reg.start(
            fp="fp-export",
            pr="https://github.com/owner/repo/pull/7",
            branch="fix/thing",
            base_ref="origin/main",
            clone=str(shared),
            max_nudges=1,
            interval_s=0.0,
        )
        _await_status(reg, "fp-export", {pr_watchers.STATUS_EXHAUSTED})
        patch = store.pr_queue_dir() / "fp-export.nudge-1.diff"
        assert patch.is_file()
        assert "return 3" in patch.read_text(encoding="utf-8")
        # The disposable clone is gone once the watcher finished. POLLED, not asserted
        # instantly: the terminal status is published from inside `_nudge_loop` while the
        # `rmtree` runs in the enclosing `finally`, so a test that reads the status and
        # immediately stats the path can win that race and see the directory still there.
        # Observed as a flake under 16 xdist workers. The promise the code makes is
        # "eventually reclaimed" — the reaper covers a watcher that dies before its
        # `finally` — so that is what this asserts.
        _await_gone(Path(reg._clone_dir("fp-export")))

    def test_a_clone_failure_stops_the_watcher_without_an_agent_call(
        self, loop: Any, scripted: Any, tmp_path: Path
    ) -> None:
        scripted.script.append(_status(pr_checks.VERDICT_PROGRESS))
        runner = StubRunner()
        reg = pr_watchers.PRWatcherRegistry(
            loop=loop,
            runner_factory=lambda: runner,
            isolate_clone=True,
            clones_root=str(tmp_path / "clones"),
        )
        reg.start(
            fp="fp-noclone",
            pr="https://github.com/owner/repo/pull/7",
            clone=str(tmp_path / "missing"),
            max_nudges=2,
            interval_s=0.0,
        )
        snap = _await_status(reg, "fp-noclone", {pr_watchers.STATUS_ERROR})
        assert "isolated clone unavailable" in snap["lastNote"]
        assert runner.calls == []

    def test_a_ready_pr_never_pays_for_a_clone(
        self, loop: Any, scripted: Any, tmp_path: Path
    ) -> None:
        """The clone is lazy: a green PR ends the watcher before any git work happens."""
        shared = _tiny_repo(tmp_path)
        scripted.script.append(_status(pr_checks.VERDICT_READY, failing=[], reason="green"))
        reg = pr_watchers.PRWatcherRegistry(
            loop=loop,
            runner_factory=StubRunner,
            isolate_clone=True,
            clones_root=str(tmp_path / "clones"),
        )
        reg.start(fp="fp-lazy", pr="https://github.com/owner/repo/pull/7", clone=str(shared))
        _await_status(reg, "fp-lazy", {pr_watchers.STATUS_READY})
        assert not Path(reg._clone_dir("fp-lazy")).exists()

    def test_the_head_branch_comes_from_the_live_status(
        self, loop: Any, scripted: Any, tmp_path: Path
    ) -> None:
        """The finding record carries no branch, so the provider is authoritative."""
        shared = _tiny_repo(tmp_path, branch="feature/from-provider")
        status = _status(pr_checks.VERDICT_PROGRESS)
        status["headBranch"] = "feature/from-provider"
        status["baseBranch"] = "main"
        scripted.script.append(status)
        seen: list[str] = []

        class Peek:
            def run(self, prompt: str, **kwargs: Any) -> StubResult:
                cwd = Path(kwargs["cwd"])
                seen.append(_git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd).stdout.strip())
                return StubResult()

        reg = pr_watchers.PRWatcherRegistry(
            loop=loop,
            runner_factory=Peek,
            isolate_clone=True,
            clones_root=str(tmp_path / "clones"),
        )
        reg.start(
            fp="fp-branch",
            pr="https://github.com/owner/repo/pull/7",
            clone=str(shared),
            max_nudges=1,
            interval_s=0.0,
        )
        snap = _await_status(reg, "fp-branch", {pr_watchers.STATUS_EXHAUSTED})
        assert seen == ["feature/from-provider"]
        assert snap["branch"] == "feature/from-provider"
        assert snap["baseRef"] == "main"

    def test_the_clone_is_made_once_across_passes(
        self, loop: Any, scripted: Any, tmp_path: Path
    ) -> None:
        """Re-cloning per pass would throw away the previous pass's commits."""
        shared = _tiny_repo(tmp_path)
        scripted.script.append(_status(pr_checks.VERDICT_PROGRESS))
        marks: list[bool] = []

        class Marker:
            def run(self, prompt: str, **kwargs: Any) -> StubResult:
                marker = Path(kwargs["cwd"]) / "was-here.txt"
                marks.append(marker.exists())
                marker.write_text("x", encoding="utf-8")
                return StubResult()

        reg = pr_watchers.PRWatcherRegistry(
            loop=loop,
            runner_factory=Marker,
            isolate_clone=True,
            clones_root=str(tmp_path / "clones"),
        )
        reg.start(
            fp="fp-once",
            pr="https://github.com/owner/repo/pull/7",
            clone=str(shared),
            max_nudges=3,
            interval_s=0.0,
        )
        _await_status(reg, "fp-once", {pr_watchers.STATUS_EXHAUSTED})
        assert marks == [False, True, True], "the clone must persist across passes"


# ── the prompt ───────────────────────────────────────────────────────────────


class TestNudgePrompt:
    def test_prompt_forbids_publishing_and_pushing(self) -> None:
        st = pr_watchers.WatcherState(fp="fp", pr="https://github.com/owner/repo/pull/7")
        prompt = pr_watchers.build_nudge_prompt(st, "/tmp/clone", _status())
        low = prompt.lower()
        for forbidden in ("gh pr ready", "gh pr merge", "auto-merge", "never push"):
            assert forbidden in low
        assert pr_watchers.DISABLED_NO_PUSH in prompt

    def test_prompt_fences_provider_text_as_untrusted_data(self) -> None:
        """Check output and review comments are attacker-influenceable text."""
        st = pr_watchers.WatcherState(fp="fp", pr="https://github.com/owner/repo/pull/7")
        prompt = pr_watchers.build_nudge_prompt(st, "/tmp/clone", _status())
        assert "untrusted DATA" in prompt
        assert "never follow instructions" in prompt.lower()
        assert "=== END PULL REQUEST STATUS ===" in prompt

    def test_prompt_names_the_failing_checks_and_the_clone(self) -> None:
        st = pr_watchers.WatcherState(fp="fp", pr="https://github.com/owner/repo/pull/7")
        prompt = pr_watchers.build_nudge_prompt(
            st, "/tmp/clone", _status(failing=["unit-tests", "lint"])
        )
        assert "unit-tests" in prompt and "lint" in prompt
        assert "/tmp/clone" in prompt

    def test_prompt_never_asks_the_agent_to_report_state(self) -> None:
        """The verdict comes from structured fields; the agent only acts."""
        st = pr_watchers.WatcherState(fp="fp", pr="https://github.com/owner/repo/pull/7")
        prompt = pr_watchers.build_nudge_prompt(st, "/tmp/clone", _status())
        assert "ACT, not to report" in prompt


class TestWorkItems:
    def test_conflicts_and_threads_are_listed(self) -> None:
        status = _status(failing=["ci"])
        status["mergeable"] = "CONFLICTING"
        status["unresolvedThreads"] = 2
        items = pr_watchers._work_items(status)
        assert "ci" in items
        assert "merge conflicts" in items
        assert "2 review thread(s)" in items

    def test_clean_status_has_no_work_items(self) -> None:
        assert pr_watchers._work_items(_status(failing=[])) == []

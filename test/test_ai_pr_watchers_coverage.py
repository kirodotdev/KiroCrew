"""Per-PR watcher coverage — the nudge loop, clone isolation, and the helper surface.

The auto-improvement app ships its own watcher suite
(``src/kiro_crew/apps/builtins/auto_improvement/tests/test_pr_watchers.py``), but CI
deselects it: those tests drive a REAL ``git`` through the OS sandbox, which the hosted
runners cannot provide. Everything the deselected file guards is therefore unmeasured on
every shard, and the module's loop body, its isolation control, and most of its helpers
sit at zero coverage.

This file re-covers that surface with the subprocess boundary stubbed out instead of
exercised, so it runs everywhere the backend runs — Linux, macOS, Windows, 3.10 through
3.13 — with no ``git``, no ``gh``, no network, and no writes outside ``tmp_path``:

* ``pr_watchers._git`` / ``pr_watchers._gh`` are replaced module-wide by recording fakes
  (:class:`FakeGit`), so a test asserts on the argv a code path *would* run rather than
  on the state a real repository ends up in;
* the two functions that own that boundary are still tested directly, against a patched
  ``subprocess.run``, using the originals captured at import time;
* ``store.data_dir`` and ``AUTO_IMPROVEMENT_SCRATCH`` are redirected per test, so the
  app's config, PR queue, and clone scratch all live under ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from conftest import requires_symlinks
from kiro_crew.apps.builtins.auto_improvement.backend import pr_checks
from kiro_crew.apps.builtins.auto_improvement.backend import pr_watchers as W
from kiro_crew.apps.builtins.auto_improvement.backend import store

#: Captured BEFORE any fixture patches the module, so the two subprocess-owning helpers
#: can still be tested on their own terms while every other test sees the fake.
_REAL_GIT = W._git
_REAL_GH = W._gh

WAIT_S = 10.0


# ── fakes ────────────────────────────────────────────────────────────────────


class FakeGit:
    """Stands in for ``pr_watchers._git`` / ``_gh``: records argv, runs nothing.

    ``rule`` registers argv WORDS that must all appear in a call for it to be answered with
    the given ``CompletedProcess``; the first matching rule wins, and anything unmatched
    succeeds with empty output. ``raise_on`` makes a matching call raise, which is how the
    callers' own ``except (OSError, SubprocessError)`` branches are reached.

    Matching is per-argument rather than a substring of the joined command line, because
    ``tmp_path`` embeds the test's own name: a test named ``..._checkout_...`` would
    otherwise have every git call in it match a ``checkout`` rule through its paths.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.rules: list[tuple[tuple[str, ...], int, str, str]] = []
        self.raise_on: tuple[str, ...] = ()

    def rule(self, *words: str, rc: int = 0, out: str = "", err: str = "") -> FakeGit:
        self.rules.append((tuple(words), rc, out, err))
        return self

    def joined(self) -> list[str]:
        return [" ".join(call) for call in self.calls]

    def __call__(self, *args: str, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
        self.calls.append(tuple(args))
        if self.raise_on and all(word in args for word in self.raise_on):
            raise subprocess.SubprocessError("stub refuses this call")
        for words, rc, out, err in self.rules:
            if all(word in args for word in words):
                return subprocess.CompletedProcess(
                    args=["git", *args], returncode=rc, stdout=out, stderr=err
                )
        return subprocess.CompletedProcess(args=["git", *args], returncode=0, stdout="", stderr="")


class StubResult:
    """Duck-types the spine's ``AgentResult``."""

    def __init__(self, ok: bool = True, text: str = "fixed the failing check") -> None:
        self.ok = ok
        self.text = text
        self.error = "" if ok else "stub failure"


class StubRunner:
    """Records every ``run`` call. Never launches an agent."""

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
    **over: Any,
) -> dict[str, Any]:
    names = ["ci"] if failing is None else failing
    payload: dict[str, Any] = {
        "ok": True,
        "url": "https://github.com/owner/repo/pull/7",
        "title": "speed up f()",
        "state": "OPEN",
        "draft": True,
        "mergeable": "mergeable",
        "headBranch": "fix/thing",
        "baseBranch": "main",
        "unresolvedThreads": 0,
        "checks": {
            "label": f"{len(names)} failing",
            "failing": list(names),
            "failingCount": len(names),
            "total": max(1, len(names)),
        },
        "verdict": verdict,
        "verdictReason": reason,
    }
    payload.update(over)
    return payload


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the app's data root and clone scratch into ``tmp_path``.

    ``store.data_dir`` is the single seam every other path helper in that module derives
    from, so patching it covers ``config_path``, ``pr_queue_dir``, and the per-repo
    subtree. Without it a test that reads ``watcherAcceptEgressRisk`` or writes a nudge
    patch would touch the operator's live app directory.
    """
    data = tmp_path / "app-data"
    data.mkdir(parents=True, exist_ok=True)
    home = tmp_path / "crew-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setenv("AUTO_IMPROVEMENT_SCRATCH", str(tmp_path / "scratch"))
    monkeypatch.setattr(store, "data_dir", lambda: data)
    return data


@pytest.fixture(autouse=True)
def git(monkeypatch: pytest.MonkeyPatch) -> FakeGit:
    """Replace BOTH subprocess helpers module-wide. Autouse, so nothing here can spawn."""
    fake = FakeGit()
    monkeypatch.setattr(W, "_git", fake)
    monkeypatch.setattr(W, "_gh", fake)
    return fake


@pytest.fixture()
def loop() -> Any:
    """A real event loop on its own thread — the gateway loop a watcher bridges onto."""
    made = asyncio.new_event_loop()
    thread = threading.Thread(target=made.run_forever, daemon=True)
    thread.start()
    yield made
    made.call_soon_threadsafe(made.stop)
    thread.join(timeout=5.0)
    made.close()


@pytest.fixture()
def scripted(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Serve ``fetch_pr_status`` from a scripted queue; the last entry repeats."""
    script: list[dict[str, Any]] = []
    seen: list[str] = []

    async def _fake(url: str, *, refresh: bool = False) -> dict[str, Any]:
        seen.append(url)
        if not script:
            return _status()
        return script[min(len(seen) - 1, len(script) - 1)]

    monkeypatch.setattr(W.pr_checks, "fetch_pr_status", _fake)
    return type("Scripted", (), {"script": script, "seen": seen})()


def _reg(**kwargs: Any) -> W.PRWatcherRegistry:
    kwargs.setdefault("autostart", False)
    kwargs.setdefault("isolate_clone", False)
    return W.PRWatcherRegistry(**kwargs)


def _await_status(
    reg: W.PRWatcherRegistry, fp: str, statuses: set[str], timeout: float = WAIT_S
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    snapshot: dict[str, Any] = {}
    while time.monotonic() < deadline:
        snapshot = reg.status(fp) or {}
        if snapshot.get("status") in statuses:
            return snapshot
        time.sleep(0.02)
    raise AssertionError(f"watcher never reached {statuses}; last={snapshot}")


# ── the subprocess boundary ──────────────────────────────────────────────────


class TestGitHelper:
    def test_argv_carries_the_hardened_config_and_pins_the_tree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``-C <tree>`` must refresh the attributes pin BEFORE git is spawned: the tree is
        agent-writable, and an unpinned one can bind a filter/diff driver that executes."""
        pinned: list[str] = []
        seen: dict[str, Any] = {}

        def _run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(W, "require_pinned", lambda cwd: pinned.append(str(cwd)))
        monkeypatch.setattr(subprocess, "run", _run)
        proc = _REAL_GIT("-C", str(tmp_path), "status", "--porcelain", timeout=5)
        assert proc.returncode == 0
        assert pinned == [str(tmp_path)]
        assert seen["argv"][0] == "git"
        for flag in W.GIT_SAFE_CONFIG:
            assert flag in seen["argv"]
        # Lenient decoding is load-bearing: a repo legitimately holds non-UTF-8 bytes, and a
        # strict decode raises from inside communicate() where no caller can read it as data.
        assert seen["kwargs"]["errors"] == "replace"
        assert seen["kwargs"]["text"] is True

    def test_a_dangling_dash_c_does_not_pin_or_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pinned: list[str] = []
        monkeypatch.setattr(W, "require_pinned", lambda cwd: pinned.append(str(cwd)))
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""),
        )
        assert _REAL_GIT("-C", timeout=5).returncode == 0
        assert pinned == []

    def test_a_call_without_dash_c_is_not_pinned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pinned: list[str] = []
        monkeypatch.setattr(W, "require_pinned", lambda cwd: pinned.append(str(cwd)))
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""),
        )
        _REAL_GIT("clone", "--local", "a", "b", timeout=5)
        assert pinned == []


class TestGhHelper:
    def test_success_is_passed_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "ok", ""),
        )
        proc = _REAL_GH("pr", "view", "https://github.com/o/r/pull/1", timeout=5)
        assert proc.returncode == 0 and proc.stdout == "ok"

    @pytest.mark.parametrize(
        "exc", [OSError("gh is not installed"), subprocess.SubprocessError("timed out")]
    )
    def test_a_missing_or_wedged_gh_is_synthesized_as_a_failure(
        self, exc: Exception, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Callers read ``returncode`` as data, so this helper must never raise at them."""

        def _boom(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            raise exc

        monkeypatch.setattr(subprocess, "run", _boom)
        proc = _REAL_GH("pr", "ready", "https://github.com/o/r/pull/1", timeout=5)
        assert proc.returncode == 127
        assert proc.stdout == ""
        assert str(exc) in proc.stderr


# ── clone isolation: the safety control ──────────────────────────────────────


class TestNeutralizeOrigin:
    def test_both_the_fetch_and_the_push_url_are_set(self, git: FakeGit) -> None:
        """Setting only the push URL leaves the real URL in the tree's config, one
        explicit-URL push away from being reachable by an auto-approved agent."""
        W.neutralize_origin("/clone")
        assert git.joined() == [
            f"-C /clone remote set-url origin {W.DISABLED_NO_PUSH}",
            f"-C /clone remote set-url --push origin {W.DISABLED_NO_PUSH}",
        ]

    def test_a_disabled_remote_reads_as_neutral(self, git: FakeGit) -> None:
        git.rule("get-url", out=f"{W.DISABLED_NO_PUSH}\n")
        ok, offenders = W.assert_origin_neutralized("/clone")
        assert ok is True and offenders == []

    def test_a_live_url_is_reported_as_an_offender(self, git: FakeGit) -> None:
        git.rule("get-url", out="https://example.invalid/x/y.git\n")
        ok, offenders = W.assert_origin_neutralized("/clone")
        assert ok is False
        assert offenders == ["https://example.invalid/x/y.git"] * 2

    def test_a_missing_remote_counts_as_neutral(self, git: FakeGit) -> None:
        """Nothing to push to is not a breach."""
        git.rule("get-url", rc=2, err="error: No such remote 'origin'")
        assert W.assert_origin_neutralized("/clone") == (True, [])

    def test_an_unreadable_config_is_not_treated_as_a_breach(self, git: FakeGit) -> None:
        """The clone is gone (swept, or the watcher lost the race) — there is nothing to
        assert, and refusing here would strand a watcher on a directory that no longer
        exists."""
        git.raise_on = ("get-url",)
        assert W.assert_origin_neutralized("/clone") == (True, [])


class TestSetupIsolatedClone:
    def test_an_unset_shared_clone_is_named_in_the_reason(self) -> None:
        clone, err = W.setup_isolated_clone("", "/dest")
        assert clone == "" and "(unset)" in err

    def test_a_missing_shared_clone_is_refused_not_degraded(self, tmp_path: Path) -> None:
        """Degrading to the shared tree would hand an agent a live fetch URL."""
        clone, err = W.setup_isolated_clone(str(tmp_path / "nope"), str(tmp_path / "iso"))
        assert clone == "" and "not a directory" in err

    @requires_symlinks
    def test_a_symlinked_destination_is_refused(self, tmp_path: Path) -> None:
        shared = tmp_path / "shared"
        shared.mkdir()
        target = tmp_path / "real"
        target.mkdir()
        link = tmp_path / "link"
        link.symlink_to(target, target_is_directory=True)
        clone, err = W.setup_isolated_clone(str(shared), str(link))
        assert clone == "" and "symlink" in err

    def test_a_failed_clone_is_reported_with_git_stderr(
        self, tmp_path: Path, git: FakeGit
    ) -> None:
        shared = tmp_path / "shared"
        shared.mkdir()
        git.rule("clone", "--local", rc=128, err="fatal: repository not found")
        clone, err = W.setup_isolated_clone(str(shared), str(tmp_path / "iso"))
        assert clone == ""
        assert "git clone --local failed" in err and "repository not found" in err

    def test_a_failed_checkout_fails_closed(self, tmp_path: Path, git: FakeGit) -> None:
        """A failed checkout leaves the clone on the BASE branch and nothing downstream can
        tell, so the watcher would 'fix' code the pull request never touched."""
        shared = tmp_path / "shared"
        shared.mkdir()
        dest = tmp_path / "iso"
        dest.mkdir()
        git.rule("checkout", rc=1, err="error: pathspec 'fix/gone' did not match")
        clone, err = W.setup_isolated_clone(str(shared), str(dest), branch="fix/gone")
        assert clone == ""
        assert "could not check out the pull request head" in err
        assert not dest.exists(), "a refused clone must not be left on disk"

    def test_a_clone_that_cannot_be_neutralized_is_deleted(
        self, tmp_path: Path, git: FakeGit
    ) -> None:
        shared = tmp_path / "shared"
        shared.mkdir()
        dest = tmp_path / "iso"
        dest.mkdir()
        git.rule("get-url", out="https://example.invalid/x/y.git\n")
        clone, err = W.setup_isolated_clone(str(shared), str(dest))
        assert clone == ""
        assert "could not neutralize origin" in err
        assert not dest.exists()

    def test_an_os_error_during_setup_is_returned_as_a_reason(
        self, tmp_path: Path, git: FakeGit
    ) -> None:
        shared = tmp_path / "shared"
        shared.mkdir()
        git.raise_on = ("clone", "--local")
        clone, err = W.setup_isolated_clone(str(shared), str(tmp_path / "iso"))
        assert clone == "" and "clone setup failed" in err

    def test_the_happy_path_fetches_the_base_then_neutralizes(
        self, tmp_path: Path, git: FakeGit
    ) -> None:
        """Order matters: the base is fetched from the shared clone (a local path) BEFORE
        neutralization, so the real remote URL never appears in this tree's config."""
        shared = tmp_path / "shared"
        shared.mkdir()
        dest = tmp_path / "iso"
        git.rule("get-url", out=f"{W.DISABLED_NO_PUSH}\n")
        clone, err = W.setup_isolated_clone(
            str(shared), str(dest), branch="fix/thing", base_ref="origin/main"
        )
        assert err == "" and clone == str(dest)
        lines = git.joined()
        fetch_at = next(i for i, line in enumerate(lines) if "fetch origin" in line)
        seturl_at = next(i for i, line in enumerate(lines) if "remote set-url origin" in line)
        assert fetch_at < seturl_at
        assert any("checkout fix/thing" in line for line in lines)

    def test_an_existing_destination_is_replaced(self, tmp_path: Path, git: FakeGit) -> None:
        shared = tmp_path / "shared"
        shared.mkdir()
        dest = tmp_path / "iso"
        dest.mkdir()
        (dest / "stale.txt").write_text("old\n", encoding="utf-8", newline="\n")
        git.rule("get-url", out=f"{W.DISABLED_NO_PUSH}\n")
        clone, err = W.setup_isolated_clone(str(shared), str(dest))
        assert err == "" and clone == str(dest)
        assert not (dest / "stale.txt").exists()


class TestFetchBaseRef:
    def test_no_base_ref_is_a_noop(self, git: FakeGit) -> None:
        W._fetch_base_ref("/clone", "")
        assert git.calls == []

    def test_the_remote_prefix_is_stripped_from_the_refspec(self, git: FakeGit) -> None:
        """``origin/main`` and a bare ``main`` name the same branch — the identity is the
        name, not the remote."""
        W._fetch_base_ref("/clone", "origin/main")
        assert git.joined() == [
            "-C /clone fetch origin +refs/remotes/origin/main:refs/remotes/origin/main"
        ]

    def test_the_second_spelling_is_tried_when_the_first_is_absent(self, git: FakeGit) -> None:
        """``clone --local`` brings over refs/heads but not refs/remotes, so the base a PR
        targets may be held under either name."""
        git.rule("+refs/remotes/origin/main:refs/remotes/origin/main", rc=128, err="fatal: couldn't find remote ref")
        W._fetch_base_ref("/clone", "main")
        assert git.joined() == [
            "-C /clone fetch origin +refs/remotes/origin/main:refs/remotes/origin/main",
            "-C /clone fetch origin +refs/heads/main:refs/remotes/origin/main",
        ]


# ── state and the log ring ───────────────────────────────────────────────────


class TestWatcherState:
    def test_as_dict_exposes_the_camel_case_ui_contract(self) -> None:
        st = W.WatcherState(
            fp="fp1",
            pr="https://github.com/o/r/pull/3",
            kind="perf",
            target="app.py::f",
            title="speed up f()",
            branch="fix/thing",
            base_ref="main",
            nudges=2,
            fixing=["ci"],
            clone="/clone",
        )
        snap = st.as_dict()
        assert snap["baseRef"] == "main"
        assert snap["maxNudges"] == W.DEFAULT_MAX_NUDGES
        assert snap["intervalSeconds"] == W.DEFAULT_NUDGE_INTERVAL_S
        assert snap["fixing"] == ["ci"]
        # A copy, not the live list: a UI snapshot must not mutate under the caller.
        snap["fixing"].append("lint")
        assert st.fixing == ["ci"]
        assert set(snap) == {
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
        }


class TestSetAndLog:
    def test_set_applies_every_field_and_stamps_the_clock(self) -> None:
        ticks = iter([100.0, 200.0])
        reg = _reg(clock=lambda: next(ticks))
        st = W.WatcherState(fp="fp1", pr="x")
        reg._set(
            st,
            status=W.STATUS_NUDGING,
            note="working",
            nudges=3,
            verdict="PROGRESS",
            verdict_reason="ci still running",
            fixing=["ci", "lint"],
        )
        assert st.status == W.STATUS_NUDGING
        assert (st.last_note, st.nudges, st.verdict) == ("working", 3, "PROGRESS")
        assert st.verdict_reason == "ci still running"
        assert st.fixing == ["ci", "lint"]
        assert st.updated_at == 100.0

    def test_a_terminal_status_clears_the_fixing_list(self) -> None:
        """A finished watcher is not 'fixing lint' — the UI must not show work beside a
        terminal status."""
        reg = _reg()
        st = W.WatcherState(fp="fp1", pr="x", fixing=["lint"])
        reg._set(st, status=W.STATUS_READY)
        assert st.fixing == []

    def test_notes_and_lines_are_length_capped(self) -> None:
        reg = _reg()
        st = W.WatcherState(fp="fp1", pr="x")
        reg._set(st, note="n" * 900)
        reg._log(st, "stage", "l" * (W.MAX_LOG_CHARS + 400))
        assert len(st.last_note) == 300
        assert len(st.log[0]["text"]) == W.MAX_LOG_CHARS

    def test_empty_log_lines_are_dropped(self) -> None:
        reg = _reg()
        st = W.WatcherState(fp="fp1", pr="x")
        reg._log(st, "stage", "   ")
        reg._log(st, "stage", "")
        assert list(st.log) == [] and st.log_total == 0

    def test_the_ring_is_bounded_but_the_total_keeps_counting(self) -> None:
        reg = _reg()
        st = W.WatcherState(fp="fp1", pr="x")
        for i in range(W.MAX_LOG_LINES + 7):
            reg._log(st, "stage", f"line {i}")
        assert len(st.log) == W.MAX_LOG_LINES
        assert st.log_total == W.MAX_LOG_LINES + 7

    def test_lines_are_redacted_at_the_sink(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The log route serves these straight to the browser with no second pass."""
        monkeypatch.setattr(W, "_redact", lambda text: "[scrubbed]")
        reg = _reg()
        st = W.WatcherState(fp="fp1", pr="x")
        reg._log(st, "tool", "Bash export TOKEN=hunter2")
        assert st.log[0]["text"] == "[scrubbed]"


class TestRedact:
    def test_the_real_redactor_is_used_when_it_imports(self) -> None:
        assert W._redact("plain text") == "plain text"

    def test_a_broken_redactor_withholds_the_line_rather_than_serving_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail CLOSED: this is the only scan between agent output and the operator's
        screen, so an unscannable line must not be served."""
        import kiro_crew.security as security

        def _boom(text: str) -> str:
            raise RuntimeError("redaction engine unavailable")

        monkeypatch.setattr(security, "redact", _boom)
        assert W._redact("anything") == "[withheld: redaction unavailable]"


# ── snapshots the route handlers call ────────────────────────────────────────


class TestSnapshots:
    def test_list_sessions_is_newest_first(self) -> None:
        reg = _reg()
        for fp, started in (("old", 10.0), ("new", 30.0), ("mid", 20.0)):
            with reg._lock:
                reg._watchers[fp] = W.WatcherState(fp=fp, pr="x", started_at=started)
        assert [row["fp"] for row in reg.list_sessions()] == ["new", "mid", "old"]

    def test_status_of_an_unknown_fingerprint_is_none(self) -> None:
        assert _reg().status("nope") is None

    def test_get_log_of_an_unknown_fingerprint_reports_an_error(self) -> None:
        out = _reg().get_log("nope")
        assert out == {"lines": [], "nextSince": 0, "status": "", "error": "no such watcher"}

    def test_get_log_is_incremental(self) -> None:
        reg = _reg()
        st = W.WatcherState(fp="fp1", pr="x", status=W.STATUS_NUDGING)
        with reg._lock:
            reg._watchers["fp1"] = st
        for i in range(3):
            reg._log(st, "stage", f"line {i}")
        first = reg.get_log("fp1")
        assert [line["text"] for line in first["lines"]] == ["line 0", "line 1", "line 2"]
        assert first["nextSince"] == 3 and first["status"] == W.STATUS_NUDGING
        assert reg.get_log("fp1", since=first["nextSince"])["lines"] == []

    def test_since_survives_ring_overflow_without_replaying(self) -> None:
        """``since`` counts lines ever appended, not ring positions, so a poller that fell
        behind a full ring resumes at the oldest line still held."""
        reg = _reg()
        st = W.WatcherState(fp="fp1", pr="x")
        with reg._lock:
            reg._watchers["fp1"] = st
        for i in range(W.MAX_LOG_LINES + 10):
            reg._log(st, "stage", f"line {i}")
        out = reg.get_log("fp1", since=5)
        assert out["nextSince"] == W.MAX_LOG_LINES + 10
        assert len(out["lines"]) == W.MAX_LOG_LINES
        assert out["lines"][0]["text"] == "line 10"


# ── loop binding ─────────────────────────────────────────────────────────────


class TestLoopBinding:
    def test_no_running_loop_resolves_to_none(self) -> None:
        assert _reg()._resolve_loop() is None

    def test_attach_loop_is_remembered(self, loop: Any) -> None:
        reg = _reg()
        reg.attach_loop(loop)
        assert reg._resolve_loop() is loop

    @pytest.mark.asyncio
    async def test_an_async_caller_adopts_its_own_running_loop(self) -> None:
        """``start`` may be called from a coroutine, and a watcher thread must never make a
        loop of its own."""
        reg = _reg()
        resolved = reg._resolve_loop()
        assert resolved is asyncio.get_running_loop()
        assert reg._loop is resolved

    def test_the_module_helper_binds_the_singleton(self, loop: Any) -> None:
        assert W.get_registry() is W.get_registry()
        previous = W.get_registry()._loop
        try:
            W.attach_loop(loop)
            assert W.get_registry()._loop is loop
        finally:
            # The fixture closes this loop at teardown, and the registry is process-wide.
            with W.get_registry()._lock:
                W.get_registry()._loop = previous


# ── start / stop ─────────────────────────────────────────────────────────────


class TestStartAndStop:
    def test_bounds_are_clamped_on_the_way_in(self, loop: Any) -> None:
        reg = _reg(loop=loop)
        st = reg.start(
            fp="fp1",
            pr="https://github.com/o/r/pull/1",
            max_nudges=0,
            interval_s=-5.0,
        )
        assert st.max_nudges == W.DEFAULT_MAX_NUDGES
        assert st.interval_s == 0.0

    def test_a_queued_pull_request_is_terminal_not_raised(self, loop: Any) -> None:
        """The caller is a route reporting a list; one unwatchable PR must not fail it."""
        reg = _reg(loop=loop)
        st = reg.start(fp="fp1", pr="QUEUED:fp1")
        assert st.status == W.STATUS_BLOCKED
        assert "no live pull request" in st.last_note

    def test_start_without_a_loop_records_an_error(self) -> None:
        reg = _reg()
        st = reg.start(fp="fp1", pr="https://github.com/o/r/pull/1")
        assert st.status == W.STATUS_ERROR and "attach_loop" in st.last_note

    def test_start_is_idempotent_per_fingerprint(self, loop: Any) -> None:
        reg = _reg(loop=loop)
        first = reg.start(fp="fp1", pr="https://github.com/o/r/pull/1")
        second = reg.start(fp="fp1", pr="https://github.com/o/r/pull/2")
        assert first is second
        assert first.pr == "https://github.com/o/r/pull/1"
        assert len(reg.list_sessions()) == 1

    def test_stop_of_an_unknown_fingerprint_is_false(self) -> None:
        assert _reg().stop("nope") is False

    def test_stop_marks_an_active_watcher_stopped_and_sets_its_flag(self, loop: Any) -> None:
        reg = _reg(loop=loop)
        st = reg.start(fp="fp1", pr="https://github.com/o/r/pull/1")
        event = threading.Event()
        with reg._lock:
            reg._stop_flags["fp1"] = event
        assert reg.stop("fp1") is True
        assert event.is_set()
        assert st.status == W.STATUS_STOPPED

    def test_stop_does_not_rewrite_an_already_terminal_status(self, loop: Any) -> None:
        reg = _reg(loop=loop)
        st = reg.start(fp="fp1", pr="https://github.com/o/r/pull/1")
        reg._set(st, status=W.STATUS_READY, note="green")
        assert reg.stop("fp1") is True
        assert st.status == W.STATUS_READY and st.last_note == "green"

    def test_stop_all_counts_only_the_active_watchers(self, loop: Any) -> None:
        reg = _reg(loop=loop)
        active = reg.start(fp="fp1", pr="https://github.com/o/r/pull/1")
        done = reg.start(fp="fp2", pr="https://github.com/o/r/pull/2")
        reg._set(done, status=W.STATUS_EXHAUSTED)
        event = threading.Event()
        with reg._lock:
            reg._stop_flags["fp1"] = event
        assert reg.stop_all() == 1
        assert event.is_set()
        assert active.status == W.STATUS_STOPPED
        assert done.status == W.STATUS_EXHAUSTED

    def test_is_alive_is_false_without_a_thread(self) -> None:
        assert _reg().is_alive("nope") is False

    def test_launch_spawns_one_daemon_thread_per_watcher(self, loop: Any) -> None:
        """A 30-minute agent turn must never hold up gateway shutdown."""
        reg = _reg(loop=loop)
        st = W.WatcherState(fp="fp1", pr="https://github.com/o/r/pull/1")
        with reg._lock:
            reg._watchers["fp1"] = st
        started = threading.Event()
        reg._run_watcher = lambda *a: started.set()  # type: ignore[method-assign]
        reg._launch(st, loop)
        with reg._lock:
            thread = reg._threads["fp1"]
        assert thread.daemon is True
        assert thread.name.startswith("pr-watcher-")
        assert started.wait(timeout=WAIT_S)
        thread.join(timeout=WAIT_S)


# ── the concurrency cap ──────────────────────────────────────────────────────


class TestDeferralQueue:
    def test_a_deferred_item_is_recorded_once_per_fingerprint(self) -> None:
        reg = _reg()
        reg._defer({"fp": "fp1", "pr": "https://github.com/o/r/pull/1"})
        reg._defer({"fp": "fp1", "pr": "https://github.com/o/r/pull/9"})
        reg._defer({"fp": "", "pr": "https://github.com/o/r/pull/2"})
        assert list(reg._deferred) == ["fp1"]
        assert reg._deferred["fp1"]["pr"] == "https://github.com/o/r/pull/1"

    def test_promote_drains_the_queue_into_free_slots(self, loop: Any) -> None:
        reg = _reg(loop=loop)
        for fp in ("fp1", "fp2"):
            reg._defer({"fp": fp, "pr": f"https://github.com/o/r/pull/{fp[-1]}", "kind": "bug"})
        assert reg.promote_deferred() == 2
        assert reg._deferred == {}
        assert {row["fp"] for row in reg.list_sessions()} == {"fp1", "fp2"}

    def test_promote_stops_at_the_cap_and_keeps_the_rest_queued(
        self, loop: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Deferral is what makes the cap a bounded queue rather than a lost signal."""
        monkeypatch.setattr(W, "MAX_ACTIVE_WATCHERS", 1)
        reg = _reg(loop=loop)
        monkeypatch.setattr(reg, "_live_fps", lambda: list(reg._watchers))
        for fp in ("fp1", "fp2", "fp3"):
            reg._defer({"fp": fp, "pr": f"https://github.com/o/r/pull/{fp[-1]}"})
        assert reg.promote_deferred() == 1
        assert len(reg._deferred) == 2

    def test_one_bad_item_does_not_stall_the_queue(
        self, loop: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reg = _reg(loop=loop)
        seen: list[str] = []

        def _start_item(item: dict[str, Any]) -> W.WatcherState:
            fp = str(item.get("fp") or "")
            seen.append(fp)
            if fp == "bad":
                raise RuntimeError("registry rejected this item")
            return W.WatcherState(fp=fp, pr="x")

        monkeypatch.setattr(reg, "_start_item", _start_item)
        reg._defer({"fp": "bad", "pr": "https://github.com/o/r/pull/1"})
        reg._defer({"fp": "good", "pr": "https://github.com/o/r/pull/2"})
        assert reg.promote_deferred() == 1
        assert seen == ["bad", "good"]
        assert reg._deferred == {}

    def test_live_fps_only_counts_running_threads(self) -> None:
        reg = _reg()
        finished = threading.Thread(target=lambda: None)
        finished.start()
        finished.join()
        with reg._lock:
            reg._threads["done"] = finished
            reg._threads["never"] = None  # type: ignore[assignment]
        assert reg._live_fps() == []

    def test_active_summary_reports_the_cap_slots_and_queue_depth(self) -> None:
        reg = _reg()
        reg._defer({"fp": "fp1", "pr": "https://github.com/o/r/pull/1"})
        summary = reg.active_summary()
        assert summary == {
            "active": 0,
            "cap": W.MAX_ACTIVE_WATCHERS,
            "deferred": 1,
            "slots": W.MAX_ACTIVE_WATCHERS,
        }

    def test_the_sweep_is_rate_limited_by_the_injected_clock(self) -> None:
        """The sweep costs one status fetch per filed finding and is called from a polled
        route, so without this a chatty UI would hammer the forge's API."""
        start = W.RECONCILE_MIN_INTERVAL_S * 10
        ticks = iter([start, start + 1.0, start + W.RECONCILE_MIN_INTERVAL_S + 1.0])
        reg = _reg(clock=lambda: next(ticks))
        assert reg.should_reconcile() is True
        assert reg.should_reconcile() is False
        assert reg.should_reconcile() is True

    def test_a_reconcile_record_starts_a_watcher_with_explicit_fields(self, loop: Any) -> None:
        reg = _reg(loop=loop)
        st = reg._start_item(
            {
                "fp": "fp1",
                "pr": "https://github.com/o/r/pull/4",
                "kind": "bug",
                "target": "app.py::f",
                "title": "fix f()",
            }
        )
        assert (st.kind, st.target, st.title) == ("bug", "app.py::f", "fix f()")
        assert st.pr == "https://github.com/o/r/pull/4"

    def test_a_watcher_already_being_driven_is_skipped(
        self, loop: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reg = _reg(loop=loop)
        monkeypatch.setattr(reg, "is_alive", lambda fp: True)
        out = reg.reconcile_failing_prs(
            findings=[
                {"fp": "fp1", "pr": "https://github.com/o/r/pull/1", "status": "filed"},
            ],
            status_for=lambda _url: _status(),
            force=True,
        )
        assert out["started"] == [] and out["deferredNow"] == []

    def test_a_rate_limited_sweep_still_reports_the_summary(self) -> None:
        """A clock pinned at zero is inside the interval from the start, which is the same
        state a second sweep lands in — the caller still needs the counts back."""
        reg = _reg(clock=lambda: 0.0)
        out = reg.reconcile_failing_prs(findings=[], status_for=lambda _url: {})
        assert out["skipped"] == "rate-limited"
        assert out["cap"] == W.MAX_ACTIVE_WATCHERS

    def test_a_start_failure_does_not_abort_the_sweep(
        self, loop: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The sweep runs from a polled route: one finding that cannot be started must not
        cost the others their reconcile."""
        reg = _reg(loop=loop)
        real_start = reg._start_item

        def _start_item(item: dict[str, Any]) -> W.WatcherState:
            if str(item.get("fp")) == "bad":
                raise RuntimeError("registry rejected this item")
            return real_start(item)

        monkeypatch.setattr(reg, "_start_item", _start_item)
        out = reg.reconcile_failing_prs(
            findings=[
                {"fp": "bad", "pr": "https://github.com/o/r/pull/1", "status": "filed"},
                {"fp": "good", "pr": "https://github.com/o/r/pull/2", "status": "filed"},
            ],
            status_for=lambda _url: _status(),
            force=True,
        )
        assert out["started"] == ["good"]

    def test_the_cr_key_is_still_read_for_older_ledger_rows(self, loop: Any) -> None:
        """The vocabulary moved from CR to PR; a ledger written before that still has ``cr``."""
        reg = _reg(loop=loop)
        out = reg.reconcile_failing_prs(
            findings=[{"fp": "fp1", "cr": "https://github.com/o/r/pull/8", "status": "committed"}],
            status_for=lambda _url: _status(),
            force=True,
        )
        assert out["started"] == ["fp1"]


# ── the loop body ────────────────────────────────────────────────────────────


class TestFetchStatus:
    def test_a_dict_payload_is_returned_unchanged(self, loop: Any, scripted: Any) -> None:
        scripted.script.append(_status())
        reg = _reg(loop=loop)
        st = W.WatcherState(fp="fp1", pr="https://github.com/o/r/pull/1")
        assert reg._fetch_status(st, loop)["verdict"] == pr_checks.VERDICT_PROGRESS

    def test_a_non_dict_payload_is_rejected(
        self, loop: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _bad(url: str, *, refresh: bool = False) -> Any:
            return ["not", "a", "dict"]

        monkeypatch.setattr(W.pr_checks, "fetch_pr_status", _bad)
        reg = _reg(loop=loop)
        st = W.WatcherState(fp="fp1", pr="https://github.com/o/r/pull/1")
        assert reg._fetch_status(st, loop) == {"ok": False, "error": "bad status payload"}

    def test_a_provider_fault_becomes_data_not_an_exception(
        self, loop: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A provider hiccup is one failed pass, not a dead watcher."""

        async def _boom(url: str, *, refresh: bool = False) -> Any:
            raise RuntimeError("forge unreachable")

        monkeypatch.setattr(W.pr_checks, "fetch_pr_status", _boom)
        reg = _reg(loop=loop)
        st = W.WatcherState(fp="fp1", pr="https://github.com/o/r/pull/1")
        out = reg._fetch_status(st, loop)
        assert out["ok"] is False and "forge unreachable" in out["error"]


class TestWait:
    def test_a_stop_during_the_interval_ends_the_loop(self) -> None:
        reg = _reg()
        st = W.WatcherState(fp="fp1", pr="x", interval_s=30.0)
        event = threading.Event()
        event.set()
        assert reg._wait(st, event) is True
        assert st.status == W.STATUS_STOPPED

    def test_an_elapsed_interval_continues_the_loop(self) -> None:
        reg = _reg()
        st = W.WatcherState(fp="fp1", pr="x", interval_s=0.0)
        assert reg._wait(st, threading.Event()) is False
        assert st.status == W.STATUS_STARTING


class TestNudgeLoop:
    def _drive(
        self,
        loop: Any,
        runner: Any,
        *,
        st: W.WatcherState | None = None,
        stop_ev: threading.Event | None = None,
        shared: str = "",
        **kwargs: Any,
    ) -> tuple[W.PRWatcherRegistry, W.WatcherState]:
        reg = _reg(loop=loop, **kwargs)
        state = st or W.WatcherState(
            fp="fp1", pr="https://github.com/o/r/pull/1", max_nudges=3, interval_s=0.0
        )
        with reg._lock:
            reg._watchers[state.fp] = state
        reg._nudge_loop(state, shared, stop_ev or threading.Event(), loop, runner)
        return reg, state

    def test_a_stop_before_the_first_pass_costs_nothing(self, loop: Any, scripted: Any) -> None:
        runner = StubRunner()
        event = threading.Event()
        event.set()
        _, st = self._drive(loop, runner, stop_ev=event)
        assert st.status == W.STATUS_STOPPED and runner.calls == []

    def test_a_ready_verdict_ends_the_watcher_without_an_agent_turn(
        self, loop: Any, scripted: Any
    ) -> None:
        scripted.script.append(
            _status(pr_checks.VERDICT_READY, failing=[], reason="checks are green")
        )
        runner = StubRunner()
        reg, st = self._drive(loop, runner)
        assert st.status == W.STATUS_READY and st.last_note == "checks are green"
        assert runner.calls == []
        assert any("READY" in line["text"] for line in reg.get_log("fp1")["lines"])

    def test_a_blocked_verdict_is_surfaced_not_nudged(self, loop: Any, scripted: Any) -> None:
        """A closed pull request cannot be fixed by editing code."""
        scripted.script.append(
            _status(pr_checks.VERDICT_BLOCKED, failing=[], reason="pull request was closed")
        )
        runner = StubRunner()
        _, st = self._drive(loop, runner)
        assert st.status == W.STATUS_BLOCKED and "closed" in st.last_note
        assert runner.calls == []

    def test_progress_forever_stops_at_the_nudge_bound(self, loop: Any, scripted: Any) -> None:
        """An always-red PR would otherwise buy an unbounded number of agent turns."""
        scripted.script.append(_status())
        runner = StubRunner()
        _, st = self._drive(loop, runner)
        assert st.status == W.STATUS_EXHAUSTED
        assert st.nudges == 3 and len(runner.calls) == 3

    def test_the_agent_gets_the_allowlisted_tools_and_the_clone_cwd(
        self, loop: Any, scripted: Any, tmp_path: Path
    ) -> None:
        scripted.script.append(_status())
        runner = StubRunner()
        st = W.WatcherState(
            fp="fp1", pr="https://github.com/o/r/pull/1", max_nudges=1, interval_s=0.0
        )
        self._drive(loop, runner, st=st, shared=str(tmp_path))
        call = runner.calls[0]
        assert call["cwd"] == str(tmp_path)
        assert call["allowed_tools"] == ["Bash", "Read", "Edit", "Write", "Grep", "Glob"]
        assert call["timeout_s"] == W.DEFAULT_NUDGE_TIMEOUT_S

    def test_the_fixing_list_tracks_what_this_pass_is_working_on(
        self, loop: Any, scripted: Any
    ) -> None:
        scripted.script.append(
            _status(failing=["unit-tests"], mergeable="CONFLICTING", unresolvedThreads=2)
        )
        runner = StubRunner()
        _, st = self._drive(loop, runner)
        # Terminal statuses clear the list, so read it from the log instead.
        assert st.status == W.STATUS_EXHAUSTED and st.fixing == []
        assert st.verdict == pr_checks.VERDICT_PROGRESS

    def test_a_status_outage_consumes_a_pass_rather_than_spinning(
        self, loop: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _down(url: str, *, refresh: bool = False) -> dict[str, Any]:
            return {"ok": False, "error": "provider down"}

        monkeypatch.setattr(W.pr_checks, "fetch_pr_status", _down)
        runner = StubRunner()
        reg, st = self._drive(loop, runner)
        assert st.status == W.STATUS_EXHAUSTED and runner.calls == []
        assert any("provider down" in line["text"] for line in reg.get_log("fp1")["lines"])

    def test_a_stop_arriving_between_passes_ends_the_loop(
        self, loop: Any, scripted: Any
    ) -> None:
        scripted.script.append(_status())
        event = threading.Event()

        class StopAfterOne:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            def run(self, prompt: str, **kwargs: Any) -> StubResult:
                self.calls.append(kwargs)
                event.set()
                return StubResult()

        runner = StopAfterOne()
        _, st = self._drive(loop, runner, stop_ev=event)
        assert st.status == W.STATUS_STOPPED and len(runner.calls) == 1

    def test_a_stop_during_a_status_outage_ends_the_loop(
        self, loop: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The outage path waits out the interval like any other, so a stop must land there
        too rather than being deferred to the next pass."""

        async def _down(url: str, *, refresh: bool = False) -> dict[str, Any]:
            return {"ok": False, "error": "provider down"}

        monkeypatch.setattr(W.pr_checks, "fetch_pr_status", _down)
        event = threading.Event()
        st = W.WatcherState(
            fp="fp1", pr="https://github.com/o/r/pull/1", max_nudges=3, interval_s=0.0
        )
        reg = _reg(loop=loop)
        with reg._lock:
            reg._watchers["fp1"] = st
        # Set the flag only AFTER the first status read, so the pass is entered and the stop
        # is observed inside `_wait` rather than at the top of the loop.
        monkeypatch.setattr(reg, "_fetch_status", lambda *a: event.set() or {"ok": False})
        reg._nudge_loop(st, "", event, loop, StubRunner())
        assert st.status == W.STATUS_STOPPED
        assert st.nudges == 1

    def test_an_isolation_breach_mid_loop_grants_no_further_turns(
        self, loop: Any, scripted: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scripted.script.append(_status())
        st = W.WatcherState(
            fp="fp1", pr="https://github.com/o/r/pull/1", max_nudges=5, interval_s=0.0
        )
        reg = _reg(loop=loop)
        with reg._lock:
            reg._watchers["fp1"] = st
        passes: list[int] = []

        def _breach(state: Any, clone: str, status: dict, runner: Any, attempt: int) -> bool:
            passes.append(attempt)
            return False

        monkeypatch.setattr(reg, "_run_agent_pass", _breach)
        reg._nudge_loop(st, "", threading.Event(), loop, StubRunner())
        assert passes == [1], "the loop kept nudging a tree that reached a live remote"
        assert st.status != W.STATUS_EXHAUSTED

    def test_a_refused_clone_is_terminal(
        self, loop: Any, scripted: Any, tmp_path: Path
    ) -> None:
        scripted.script.append(_status())
        runner = StubRunner()
        reg, st = self._drive(
            loop,
            runner,
            shared=str(tmp_path / "missing"),
            isolate_clone=True,
            clones_root=str(tmp_path / "clones"),
        )
        assert st.status == W.STATUS_ERROR
        assert "isolated clone unavailable" in st.last_note
        assert runner.calls == []


class TestEnsureClone:
    def test_isolation_off_hands_back_the_shared_tree(self) -> None:
        reg = _reg()
        st = W.WatcherState(fp="fp1", pr="x")
        assert reg._ensure_clone(st, "/shared", {}) == ("/shared", True)

    def test_an_existing_clone_is_reused_across_passes(self, tmp_path: Path) -> None:
        """Re-cloning per pass would throw away the previous pass's commits."""
        reg = _reg(isolate_clone=True, clones_root=str(tmp_path / "clones"))
        existing = tmp_path / "iso"
        existing.mkdir()
        st = W.WatcherState(fp="fp1", pr="x", clone=str(existing))
        assert reg._ensure_clone(st, "/shared", {}) == (str(existing), True)

    def test_the_head_branch_comes_from_the_live_status(
        self, tmp_path: Path, git: FakeGit
    ) -> None:
        """The finding record carries no branch, so the provider is authoritative about
        which branch the pull request is actually built on."""
        shared = tmp_path / "shared"
        shared.mkdir()
        git.rule("get-url", out=f"{W.DISABLED_NO_PUSH}\n")
        reg = _reg(isolate_clone=True, clones_root=str(tmp_path / "clones"))
        st = W.WatcherState(fp="fp1", pr="x")
        clone, ok = reg._ensure_clone(
            st, str(shared), {"headBranch": "feature/from-provider", "baseBranch": "main"}
        )
        assert ok is True
        assert clone == reg._clone_dir("fp1")
        assert st.branch == "feature/from-provider" and st.base_ref == "main"
        assert any("checkout feature/from-provider" in line for line in git.joined())

    def test_a_refusal_is_recorded_as_an_error_and_returns_not_ok(self, tmp_path: Path) -> None:
        reg = _reg(isolate_clone=True, clones_root=str(tmp_path / "clones"))
        st = W.WatcherState(fp="fp1", pr="x")
        with reg._lock:
            reg._watchers["fp1"] = st
        clone, ok = reg._ensure_clone(st, str(tmp_path / "missing"), {})
        assert (clone, ok) == ("", False)
        assert st.status == W.STATUS_ERROR


class TestRunAgentPass:
    def _reg_and_state(self, tmp_path: Path, **kwargs: Any) -> tuple[Any, W.WatcherState]:
        reg = _reg(**kwargs)
        st = W.WatcherState(fp="fp1", pr="https://github.com/o/r/pull/1", base_ref="main")
        with reg._lock:
            reg._watchers["fp1"] = st
        return reg, st

    def test_a_successful_pass_logs_the_agent_summary(self, tmp_path: Path) -> None:
        reg, st = self._reg_and_state(tmp_path)
        runner = StubRunner(StubResult(True, "rewrote the flaky assertion"))
        assert reg._run_agent_pass(st, str(tmp_path), _status(), runner, 1) is True
        lines = [line["text"] for line in reg.get_log("fp1")["lines"]]
        assert any("rewrote the flaky assertion" in text for text in lines)

    def test_a_raising_runner_is_one_bad_pass_not_a_dead_watcher(self, tmp_path: Path) -> None:
        reg, st = self._reg_and_state(tmp_path)

        class Boom:
            def run(self, prompt: str, **kwargs: Any) -> StubResult:
                raise RuntimeError("agent exploded")

        assert reg._run_agent_pass(st, str(tmp_path), _status(), Boom(), 1) is True
        assert "agent exploded" in st.last_note

    def test_a_faulted_pass_still_asks_whether_its_work_is_durable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pass that edited and COMMITTED before it faulted holds the only copy of that
        work — the clone's origin is dead — so an early return is how it gets deleted."""
        reg, st = self._reg_and_state(tmp_path)
        asked: list[int] = []
        monkeypatch.setattr(
            reg, "_export_is_durable", lambda state, clone, attempt: asked.append(attempt) or False
        )

        class Boom:
            def run(self, prompt: str, **kwargs: Any) -> StubResult:
                raise RuntimeError("agent exploded")

        reg._run_agent_pass(st, str(tmp_path), _status(), Boom(), 4)
        assert asked == [4]
        assert st.unexported_work is True

    def test_a_failed_result_also_checks_durability(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A timeout is the likelier path in practice, and a timed-out pass can have
        committed first."""
        reg, st = self._reg_and_state(tmp_path)
        monkeypatch.setattr(reg, "_export_is_durable", lambda *a: False)
        result = StubResult(ok=False)
        result.error = "timeout after 1800s"
        assert reg._run_agent_pass(st, str(tmp_path), _status(), StubRunner(result), 2) is True
        assert "timeout after 1800s" in st.last_note
        assert st.unexported_work is True

    def test_a_result_without_an_error_field_still_reports_something(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reg, st = self._reg_and_state(tmp_path)
        monkeypatch.setattr(reg, "_export_is_durable", lambda *a: True)
        assert reg._run_agent_pass(st, str(tmp_path), _status(), StubRunner(object()), 1) is True
        assert "unknown" in st.last_note

    def test_an_isolation_breach_is_terminal(self, tmp_path: Path, git: FakeGit) -> None:
        """The loop must not keep handing turns to a tree that reached a live remote."""
        reg, st = self._reg_and_state(tmp_path, isolate_clone=True)
        git.rule("get-url", out="https://example.invalid/x/y.git\n")
        assert reg._run_agent_pass(st, str(tmp_path), _status(), StubRunner(), 1) is False
        assert st.status == W.STATUS_ERROR and "re-pointed" in st.last_note
        # …and the breach must be re-closed, not merely reported.
        assert any(f"set-url origin {W.DISABLED_NO_PUSH}" in line for line in git.joined())

    def test_a_clean_tree_passes_the_post_turn_assertion(
        self, tmp_path: Path, git: FakeGit
    ) -> None:
        reg, st = self._reg_and_state(tmp_path, isolate_clone=True)
        git.rule("get-url", out=f"{W.DISABLED_NO_PUSH}\n")
        assert reg._verify_isolation(st, str(tmp_path)) is True
        assert st.status == W.STATUS_STARTING


class TestExportDurability:
    def _reg_and_state(self, **kwargs: Any) -> tuple[Any, W.WatcherState]:
        reg = _reg(**kwargs)
        st = W.WatcherState(fp="fp1", pr="https://github.com/o/r/pull/1", base_ref="main")
        with reg._lock:
            reg._watchers["fp1"] = st
        return reg, st

    def test_a_written_patch_is_the_strongest_signal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reg, st = self._reg_and_state()
        monkeypatch.setattr(reg, "_export_fix", lambda *a: None)
        (store.pr_queue_dir() / "fp1.nudge-2.diff").write_text(
            "diff --git a/x b/x\n", encoding="utf-8", newline="\n"
        )
        assert reg._export_is_durable(st, "/clone", 2) is True

    def test_a_failing_diff_reads_as_cannot_tell_not_as_no_work(
        self, git: FakeGit, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failing ``git diff`` writes to stderr and leaves stdout EMPTY, so reading
        stdout alone turned 'cannot tell' into 'no work' and deleted the clone holding the
        only copy of the agent's commits."""
        reg, st = self._reg_and_state()
        monkeypatch.setattr(reg, "_export_fix", lambda *a: None)
        git.rule("diff", rc=128, err="fatal: ambiguous argument")
        assert reg._export_is_durable(st, "/clone", 1) is False
        assert any("keeping the clone" in line["text"] for line in reg.get_log("fp1")["lines"])

    def test_committed_work_with_no_artifact_is_not_durable(
        self, git: FakeGit, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reg, st = self._reg_and_state()
        monkeypatch.setattr(reg, "_export_fix", lambda *a: None)
        git.rule("diff", out="diff --git a/app.py b/app.py\n")
        assert reg._export_is_durable(st, "/clone", 1) is False

    def test_uncommitted_work_is_not_durable_either(
        self, git: FakeGit, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``base...HEAD`` sees committed history only, so a fix the agent left uncommitted
        is invisible there while the clone holds the only copy."""
        reg, st = self._reg_and_state()
        monkeypatch.setattr(reg, "_export_fix", lambda *a: None)
        git.rule("status", "--porcelain", out=" M app.py\n")
        assert reg._export_is_durable(st, "/clone", 1) is False

    def test_a_failing_status_call_keeps_the_clone(
        self, git: FakeGit, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reg, st = self._reg_and_state()
        monkeypatch.setattr(reg, "_export_fix", lambda *a: None)
        git.rule("status", "--porcelain", rc=128, err="fatal: not a git repository")
        assert reg._export_is_durable(st, "/clone", 1) is False
        assert any("git status failed" in line["text"] for line in reg.get_log("fp1")["lines"])

    def test_an_empty_diff_and_a_clean_tree_mean_this_pass_produced_nothing(
        self, git: FakeGit, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reg, st = self._reg_and_state()
        monkeypatch.setattr(reg, "_export_fix", lambda *a: None)
        assert reg._export_is_durable(st, "/clone", 1) is True

    def test_a_raising_export_is_reported_as_not_durable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reg, st = self._reg_and_state()

        def _boom(state: Any, clone: str, attempt: int) -> None:
            raise OSError("the queue directory is read-only")

        monkeypatch.setattr(reg, "_export_fix", _boom)
        assert reg._export_is_durable(st, "/clone", 1) is False
        assert any("export the fix patch" in line["text"] for line in reg.get_log("fp1")["lines"])

    def test_a_raising_git_call_is_reported_as_not_durable(
        self, git: FakeGit, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reg, st = self._reg_and_state()
        monkeypatch.setattr(reg, "_export_fix", lambda *a: None)
        git.raise_on = ("diff",)
        assert reg._export_is_durable(st, "/clone", 1) is False

    def test_retain_marks_the_clone_only_when_the_work_is_undurable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reg, st = self._reg_and_state()
        monkeypatch.setattr(reg, "_export_is_durable", lambda *a: True)
        reg._retain_if_work_is_undurable(st, "/clone", 1)
        assert st.unexported_work is False
        monkeypatch.setattr(reg, "_export_is_durable", lambda *a: False)
        reg._retain_if_work_is_undurable(st, "/clone", 1)
        assert st.unexported_work is True


class TestExportFix:
    def _reg_and_state(self) -> tuple[Any, W.WatcherState]:
        reg = _reg()
        st = W.WatcherState(fp="fp1", pr="https://github.com/o/r/pull/1", base_ref="origin/main")
        with reg._lock:
            reg._watchers["fp1"] = st
        return reg, st

    def test_no_clone_is_a_noop(self, git: FakeGit) -> None:
        reg, st = self._reg_and_state()
        reg._export_fix(st, "", 1)
        assert git.calls == []

    def test_the_patch_lands_in_the_durable_queue(self, git: FakeGit) -> None:
        """The clone is disposable and its origin is dead, so without this the agent's work
        is deleted along with the directory."""
        reg, st = self._reg_and_state()
        git.rule("diff", out="diff --git a/app.py b/app.py\n+return 3\n")
        reg._export_fix(st, "/clone", 3)
        patch = store.pr_queue_dir() / "fp1.nudge-3.diff"
        assert patch.is_file()
        assert "return 3" in patch.read_text(encoding="utf-8")
        assert any("exported this pass's fix" in line["text"] for line in reg.get_log("fp1")["lines"])

    def test_an_empty_or_failing_diff_writes_nothing(self, git: FakeGit) -> None:
        reg, st = self._reg_and_state()
        git.rule("diff", rc=128, err="fatal: bad revision")
        reg._export_fix(st, "/clone", 1)
        assert list(store.pr_queue_dir().iterdir()) == []

    def test_a_write_failure_is_logged_not_raised(
        self, git: FakeGit, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A lost patch is a lost patch, not a failed watcher."""
        reg, st = self._reg_and_state()
        git.rule("diff", out="diff --git a/app.py b/app.py\n")

        def _boom() -> Path:
            raise OSError("no space left on device")

        monkeypatch.setattr(W.store, "pr_queue_dir", _boom)
        reg._export_fix(st, "/clone", 1)
        assert any("export the fix patch" in line["text"] for line in reg.get_log("fp1")["lines"])

    @pytest.mark.parametrize(
        "base_ref,expected",
        [("origin/main", "origin/main"), ("main", "origin/main"), ("", "origin/HEAD")],
    )
    def test_the_base_revision_is_spelled_as_the_clone_holds_it(
        self, base_ref: str, expected: str
    ) -> None:
        """Callers configure a base as either ``main`` or ``origin/main``, and the clone
        holds it as a remote-tracking ref, so the configured string alone may not resolve."""
        st = W.WatcherState(fp="fp1", pr="x", base_ref=base_ref)
        assert W.PRWatcherRegistry._base_rev(st) == expected


class TestCloneDirAndCleanup:
    def test_the_directory_name_is_collision_resistant(self, tmp_path: Path) -> None:
        """Sanitizing alone can collapse two distinct fingerprints onto one directory — and
        this code deletes that directory."""
        reg = _reg(clones_root=str(tmp_path))
        first = reg._clone_dir("perf/app.py::f")
        second = reg._clone_dir("perf/app.py__f")
        assert first != second
        assert Path(first).parent == tmp_path

    def test_an_unprintable_fingerprint_still_gets_a_directory(self, tmp_path: Path) -> None:
        reg = _reg(clones_root=str(tmp_path))
        assert Path(reg._clone_dir("///")).name.startswith("pr-")

    def test_the_default_root_is_the_scratch_directory(self, tmp_path: Path) -> None:
        reg = _reg()
        expected = store.scratch_dir() / "pr_clones"
        assert Path(reg._clone_dir("fp1")).parent == expected

    def test_cleanup_removes_only_this_watchers_own_clone(self, tmp_path: Path) -> None:
        reg = _reg(clones_root=str(tmp_path / "clones"))
        st = W.WatcherState(fp="fp1", pr="x")
        mine = Path(reg._clone_dir("fp1"))
        mine.mkdir(parents=True)
        (mine / "f.txt").write_text("x\n", encoding="utf-8", newline="\n")
        foreign = tmp_path / "not-mine"
        foreign.mkdir()
        reg._cleanup_clone(st, str(foreign))
        assert foreign.is_dir(), "cleanup must never touch a foreign path"
        reg._cleanup_clone(st, str(mine))
        assert not mine.exists()

    def test_a_cleanup_failure_is_swallowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reg = _reg(clones_root=str(tmp_path / "clones"))
        st = W.WatcherState(fp="fp1", pr="x")

        def _boom(fp: str) -> str:
            raise ValueError("embedded null byte in the fingerprint")

        monkeypatch.setattr(reg, "_clone_dir", _boom)
        reg._cleanup_clone(st, str(tmp_path))  # must not raise


class TestRunWatcher:
    def test_an_unbuildable_runner_ends_the_watcher_before_any_git_work(
        self, loop: Any, git: FakeGit
    ) -> None:
        reg = _reg(loop=loop, runner_factory=None)
        st = W.WatcherState(fp="fp1", pr="https://github.com/o/r/pull/1")
        with reg._lock:
            reg._watchers["fp1"] = st
        reg._run_watcher(st, threading.Event(), loop)
        # No provider is configured in a test process, and the egress flag is off, so the
        # runner build must refuse rather than fall back to a subprocess agent.
        assert st.status == W.STATUS_ERROR and "runner unavailable" in st.last_note
        assert git.calls == []

    def test_a_loop_body_fault_is_recorded_as_state_not_a_crash(self, loop: Any) -> None:
        reg = _reg(loop=loop, runner_factory=StubRunner)
        st = W.WatcherState(fp="fp1", pr="https://github.com/o/r/pull/1")
        with reg._lock:
            reg._watchers["fp1"] = st
        reg._nudge_loop = lambda *a: (_ for _ in ()).throw(  # type: ignore[method-assign]
            RuntimeError("loop body exploded")
        )
        reg._run_watcher(st, threading.Event(), loop)
        assert st.status == W.STATUS_ERROR
        assert "loop body exploded" in st.last_note

    def test_teardown_removes_the_isolated_clone(self, loop: Any, tmp_path: Path) -> None:
        reg = _reg(
            loop=loop,
            runner_factory=StubRunner,
            isolate_clone=True,
            clones_root=str(tmp_path / "clones"),
        )
        st = W.WatcherState(fp="fp1", pr="https://github.com/o/r/pull/1", clone=str(tmp_path))
        with reg._lock:
            reg._watchers["fp1"] = st
        mine = Path(reg._clone_dir("fp1"))
        mine.mkdir(parents=True)

        def _loop_body(state: W.WatcherState, *a: Any) -> None:
            state.clone = str(mine)

        reg._nudge_loop = _loop_body  # type: ignore[method-assign]
        reg._run_watcher(st, threading.Event(), loop)
        assert not mine.exists()

    def test_teardown_keeps_a_clone_holding_unexported_work(
        self, loop: Any, tmp_path: Path
    ) -> None:
        """Deleting it is unrecoverable — the origin is dead by design — so the directory is
        the only copy and must survive."""
        reg = _reg(
            loop=loop,
            runner_factory=StubRunner,
            isolate_clone=True,
            clones_root=str(tmp_path / "clones"),
        )
        st = W.WatcherState(fp="fp1", pr="https://github.com/o/r/pull/1", clone=str(tmp_path))
        with reg._lock:
            reg._watchers["fp1"] = st
        mine = Path(reg._clone_dir("fp1"))
        mine.mkdir(parents=True)

        def _loop_body(state: W.WatcherState, *a: Any) -> None:
            state.clone = str(mine)
            state.unexported_work = True

        reg._nudge_loop = _loop_body  # type: ignore[method-assign]
        reg._run_watcher(st, threading.Event(), loop)
        assert mine.is_dir()
        assert any("only copy" in line["text"] for line in reg.get_log("fp1")["lines"])

    def test_start_drives_a_watcher_end_to_end_on_its_own_thread(
        self, loop: Any, scripted: Any
    ) -> None:
        scripted.script.append(_status(pr_checks.VERDICT_READY, failing=[], reason="green"))
        runner = StubRunner()
        reg = W.PRWatcherRegistry(
            loop=loop, runner_factory=lambda: runner, isolate_clone=False
        )
        reg.start(fp="fp1", pr="https://github.com/o/r/pull/1", interval_s=0.0)
        snap = _await_status(reg, "fp1", {W.STATUS_READY})
        assert snap["verdict"] == pr_checks.VERDICT_READY
        assert runner.calls == []
        deadline = time.monotonic() + WAIT_S
        while reg.is_alive("fp1") and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not reg.is_alive("fp1"), "a finished watcher leaked a live thread"


# ── the runner build (the fail-closed egress gate) ───────────────────────────


class TestMakeRunner:
    def _accept_egress(self) -> None:
        store.write_json_atomic(store.config_path(), {"watcherAcceptEgressRisk": True})

    def test_an_injected_factory_short_circuits_the_gate(self) -> None:
        runner = StubRunner()
        reg = _reg(runner_factory=lambda: runner)
        st = W.WatcherState(fp="fp1", pr="x")
        assert reg._make_runner(st, threading.Event()) is runner

    def test_the_egress_flag_is_off_by_default_and_refuses(self) -> None:
        """An unattended agent driven by untrusted review text can reach the network with
        the host's credentials, and this path cannot isolate egress."""
        reg = _reg()
        st = W.WatcherState(fp="fp1", pr="x")
        with pytest.raises(RuntimeError, match="watcherAcceptEgressRisk"):
            reg._make_runner(st, threading.Event())

    @pytest.mark.parametrize("value", [False, "true", 1, None])
    def test_only_the_explicit_boolean_opts_in(self, value: Any) -> None:
        """Compared with ``is True``, so a stray string or 1 does not acknowledge the risk."""
        store.write_json_atomic(store.config_path(), {"watcherAcceptEgressRisk": value})
        assert W._watcher_egress_accepted() is False

    def test_the_flag_reads_true_only_for_the_boolean(self) -> None:
        self._accept_egress()
        assert W._watcher_egress_accepted() is True

    def test_no_provider_refuses_the_subprocess_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nudging a PR through an unguarded subprocess agent exactly when the platform is
        unhealthy is worse than not nudging it."""
        self._accept_egress()
        from kiro_crew.apps.builtins.auto_improvement.spine import agent_runner

        class Unavailable:
            @staticmethod
            def available() -> bool:
                return False

        monkeypatch.setattr(agent_runner, "SessionAgentRunner", Unavailable)
        reg = _reg()
        st = W.WatcherState(fp="fp1", pr="x")
        with pytest.raises(RuntimeError, match="refusing the subprocess fallback"):
            reg._make_runner(st, threading.Event())

    def test_an_unregisterable_agent_refuses_rather_than_falling_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Falling through would bypass the provider's own permission gate."""
        self._accept_egress()
        from kiro_crew.apps.builtins.auto_improvement.spine import agent_runner

        class Unregisterable:
            def __init__(self, **kwargs: Any) -> None:
                pass

            @staticmethod
            def available() -> bool:
                return True

            def ensure_agent_registered(self) -> bool:
                return False

        monkeypatch.setattr(agent_runner, "SessionAgentRunner", Unregisterable)
        reg = _reg()
        st = W.WatcherState(fp="fp1", pr="x")
        with pytest.raises(RuntimeError, match="tool-restricted agent"):
            reg._make_runner(st, threading.Event())

    def test_a_registered_runner_is_wired_to_the_stop_flag_and_the_log(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._accept_egress()
        from kiro_crew.apps.builtins.auto_improvement.spine import agent_runner

        built: dict[str, Any] = {}

        class Ready:
            def __init__(self, **kwargs: Any) -> None:
                built.update(kwargs)

            @staticmethod
            def available() -> bool:
                return True

            def ensure_agent_registered(self) -> bool:
                return True

        monkeypatch.setattr(agent_runner, "SessionAgentRunner", Ready)
        reg = _reg()
        st = W.WatcherState(fp="fp1", pr="x")
        with reg._lock:
            reg._watchers["fp1"] = st
        stop_ev = threading.Event()
        runner = reg._make_runner(st, stop_ev)
        assert isinstance(runner, Ready)
        assert built["default_timeout_s"] == W.DEFAULT_NUDGE_TIMEOUT_S
        # The stop flag must abort an in-flight turn; otherwise a stop waits out 30 minutes.
        assert built["stop_check"]() is False
        stop_ev.set()
        assert built["stop_check"]() is True
        activity = built["on_activity"]
        activity({"kind": "tool", "tool": "Bash", "detail": "pytest -q"})
        activity({"kind": "text", "detail": "reading the failing check"})
        activity({"kind": "other", "detail": "ignored"})
        lines = [(line["kind"], line["text"]) for line in reg.get_log("fp1")["lines"]]
        assert ("tool", "Bash pytest -q") in lines
        assert ("thought", "reading the failing check") in lines
        assert not any(text == "ignored" for _kind, text in lines)


# ── module helpers ───────────────────────────────────────────────────────────


class TestIsWatchablePr:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("https://github.com/owner/repo/pull/7", True),
            ("https://gitlab.com/group/proj/-/merge_requests/2", True),
            ("  https://github.com/owner/repo/pull/7  ", True),
            ("https://GITHUB.com/owner/repo/PULL/7", True),
            ("QUEUED:abc123", False),
            ("queued:abc123", False),
            ("", False),
            ("   ", False),
            ("http://github.com/owner/repo/pull/7", False),
            ("https://github.com/owner/repo/issues/7", False),
            ("https://github.com/owner/repo/pull/notanumber", False),
        ],
    )
    def test_only_a_live_pull_request_url_is_watchable(self, value: str, expected: bool) -> None:
        assert W.is_watchable_pr(value) is expected


class TestWorkItems:
    def test_failing_checks_conflicts_and_threads_are_all_listed(self) -> None:
        items = W._work_items(
            _status(failing=["ci", "lint"], mergeable="CONFLICTING", unresolvedThreads=2)
        )
        assert items == ["ci", "lint", "merge conflicts", "2 review thread(s)"]

    def test_a_clean_status_has_no_work_items(self) -> None:
        assert W._work_items(_status(failing=[], mergeable="mergeable")) == []

    def test_a_status_without_a_checks_block_is_tolerated(self) -> None:
        assert W._work_items({}) == []


class TestConfigFlags:
    def test_auto_publish_is_off_without_a_config_file(self) -> None:
        assert W.auto_publish_enabled() is False

    def test_auto_publish_reads_the_explicit_boolean(self) -> None:
        store.write_json_atomic(store.config_path(), {"autoPublish": True})
        assert W.auto_publish_enabled() is True
        store.write_json_atomic(store.config_path(), {"autoPublish": "yes"})
        assert W.auto_publish_enabled() is False

    def test_the_configured_clone_defaults_to_empty(self) -> None:
        assert W.configured_clone() == ""

    def test_the_configured_clone_is_read_from_config(self, tmp_path: Path) -> None:
        store.write_json_atomic(store.config_path(), {"clone": str(tmp_path / "shared")})
        assert W.configured_clone() == str(tmp_path / "shared")


class TestAutoPublishGate:
    def test_a_status_missing_a_checks_block_is_refused(self) -> None:
        allowed, reason = W.auto_publish_gate(
            {"ok": True, "state": "OPEN", "draft": True, "verdict": "READY"}
        )
        assert allowed is False and reason == "no check summary available"

    def test_a_non_watchable_url_is_never_published(
        self, monkeypatch: pytest.MonkeyPatch, git: FakeGit
    ) -> None:
        monkeypatch.setattr(W, "auto_publish_enabled", lambda: True)
        green = {
            "ok": True,
            "state": "OPEN",
            "draft": True,
            "verdict": "READY",
            "checks": {"failingCount": 0, "total": 3},
            "unresolvedThreads": 0,
        }
        ok, reason = W.publish_if_authorized("QUEUED:fp1", green)
        assert ok is False and reason == "not a pull-request url"
        assert git.calls == []


class TestDeleteCloneIfUnowned:
    def test_an_unowned_directory_is_deleted_under_the_lock(self, tmp_path: Path) -> None:
        reg = _reg(clones_root=str(tmp_path / "clones"))
        orphan = tmp_path / "orphan"
        (orphan / ".git").mkdir(parents=True)
        assert W._delete_clone_if_unowned(reg, orphan) is True
        assert not orphan.exists()

    def test_a_clone_holding_unexported_work_is_spared(self, tmp_path: Path) -> None:
        """Reclaiming disk is housekeeping; losing a completed agent pass is not an
        acceptable price for it."""
        reg = _reg(clones_root=str(tmp_path / "clones"))
        st = W.WatcherState(fp="fp1", pr="x", unexported_work=True)
        with reg._lock:
            reg._watchers["fp1"] = st
        mine = Path(reg._clone_dir("fp1"))
        mine.mkdir(parents=True)
        assert W._delete_clone_if_unowned(reg, mine) is False
        assert mine.is_dir()

    def test_a_delete_failure_leaves_the_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A sweep that cannot prove a directory unowned — or cannot remove it — must leave
        it rather than report a reclaim it did not make."""
        reg = _reg(clones_root=str(tmp_path / "clones"))
        orphan = tmp_path / "orphan"
        orphan.mkdir()

        def _boom(path: Any, *args: Any, **kwargs: Any) -> None:
            raise OSError("directory is busy")

        monkeypatch.setattr(W.shutil, "rmtree", _boom)
        assert W._delete_clone_if_unowned(reg, orphan) is False
        assert orphan.is_dir()


class TestSweepOrphanClones:
    def test_a_sweep_that_cannot_establish_liveness_removes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "clones"
        root.mkdir()
        (root / ("a" * 8 + "-" + "0" * 12)).mkdir()

        def _boom(fp: str) -> bool:
            raise RuntimeError("registry is wedged")

        monkeypatch.setattr(W.get_registry(), "is_alive", _boom)
        with W.get_registry()._lock:
            W.get_registry()._watchers["probe"] = W.WatcherState(fp="probe", pr="x")
        try:
            assert W.sweep_orphan_clones(clones_root=str(root)) == 0
        finally:
            with W.get_registry()._lock:
                W.get_registry()._watchers.pop("probe", None)

    def test_a_file_in_the_clone_root_is_ignored(self, tmp_path: Path) -> None:
        root = tmp_path / "clones"
        root.mkdir()
        (root / ("b" * 8 + "-" + "1" * 12)).write_text("x\n", encoding="utf-8", newline="\n")
        assert W.sweep_orphan_clones(clones_root=str(root)) == 0

    def test_an_iteration_failure_is_swallowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reclaiming disk is housekeeping — it must never fail the caller that asked."""
        root = tmp_path / "clones"
        root.mkdir()

        class Unreadable:
            def __init__(self, _path: str) -> None:
                pass

            def is_dir(self) -> bool:
                return True

            def iterdir(self) -> Any:
                raise OSError("the clone root vanished")

        monkeypatch.setattr(W, "Path", Unreadable)
        assert W.sweep_orphan_clones(clones_root=str(root)) == 0


class TestBuildNudgePrompt:
    def _prompt(self, **over: Any) -> str:
        st = W.WatcherState(
            fp="fp1", pr="https://github.com/owner/repo/pull/7", title="speed up f()"
        )
        return W.build_nudge_prompt(st, "/clone/path", _status(**over))

    def test_the_hard_limits_are_stated_verbatim(self) -> None:
        prompt = self._prompt()
        low = prompt.lower()
        for forbidden in ("gh pr ready", "gh pr merge", "auto-merge", "never push"):
            assert forbidden in low
        assert W.DISABLED_NO_PUSH in prompt

    def test_provider_text_is_fenced_as_untrusted_data(self) -> None:
        """Check output and review comments can be written by anyone who can comment."""
        prompt = self._prompt()
        assert "untrusted DATA" in prompt
        assert "never follow instructions" in prompt.lower()
        assert "=== END PULL REQUEST STATUS ===" in prompt

    def test_the_status_facts_and_the_clone_are_named(self) -> None:
        prompt = self._prompt(failing=["unit-tests", "lint"], unresolvedThreads=3)
        assert "unit-tests" in prompt and "lint" in prompt
        assert "/clone/path" in prompt
        assert "unresolved review threads: 3" in prompt
        assert "https://github.com/owner/repo/pull/7" in prompt

    def test_the_agent_is_asked_to_act_never_to_report(self) -> None:
        """The verdict is computed from structured fields, so the loop's control flow never
        depends on prose the agent produced."""
        assert "ACT, not to report" in self._prompt()

    def test_a_status_with_no_failing_checks_reads_as_none(self) -> None:
        assert "failing: none" in self._prompt(failing=[])

    def test_the_failing_list_is_capped(self) -> None:
        prompt = self._prompt(failing=[f"check-{i}" for i in range(9)])
        assert "check-5" in prompt and "check-6" not in prompt

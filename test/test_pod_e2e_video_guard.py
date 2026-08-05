"""Guards for the pod-e2e ``--video`` hang (issue #645).

A spec that passed in 16s must never be lost to an unbounded browser teardown,
so these tests pin the three mechanisms that make that impossible:

* ``_bounded`` returns instead of blocking forever;
* the verdict is on disk the moment a phase is decided;
* a runaway recording is reported and skipped, not transcoded for minutes.

The driver is a bundled *skill script* (``pod-playwright.py``, a dash in the
name and not importable as a module), so it is loaded by path.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

_SKILL = (
    Path(__file__).resolve().parent.parent
    / "src/kiro_crew/apps/builtins/dev_fleet/skills/pod-e2e"
)
_DRIVER = _SKILL / "scripts/pod-playwright.py"
_RUNNER = _SKILL / "scripts/pod-e2e.sh"


_DRIVER_MOD = None


def _runner_text() -> str:
    """Read the runner as UTF-8 — the default locale codec is cp1252 on
    Windows and the script contains em dashes and ⚠️."""
    return _RUNNER.read_text(encoding="utf-8")


def _load_driver():
    """Load the skill script, stubbing playwright if it isn't installed here.

    The driver only needs ``playwright.sync_api`` at import time; every helper
    under test is pure Python, so CI does not need a browser stack to pin them.
    The stub is removed from ``sys.modules`` afterwards so nothing else in the
    worker sees a fake playwright.
    """
    global _DRIVER_MOD
    if _DRIVER_MOD is not None:
        return _DRIVER_MOD

    saved: dict[str, object] = {}
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        pw = types.ModuleType("playwright")
        sync_api = types.ModuleType("playwright.sync_api")
        # Give both stubs a real __spec__: importlib.util.find_spec() raises
        # ValueError on a sys.modules entry whose __spec__ is None.
        pw.__spec__ = importlib.machinery.ModuleSpec("playwright", loader=None)
        sync_api.__spec__ = importlib.machinery.ModuleSpec(
            "playwright.sync_api", loader=None
        )
        sync_api.expect = types.SimpleNamespace(set_options=lambda **k: None)
        sync_api.sync_playwright = lambda: None
        pw.sync_api = sync_api
        for name, stub in (("playwright", pw), ("playwright.sync_api", sync_api)):
            saved[name] = sys.modules.get(name)
            sys.modules[name] = stub

    try:
        spec = importlib.util.spec_from_file_location(
            "pod_playwright_under_test", _DRIVER
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        for name, prev in saved.items():
            if prev is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = prev

    _DRIVER_MOD = mod
    return mod


@pytest.fixture(scope="module")
def driver():
    return _load_driver()


# --- bounded teardown -------------------------------------------------------


def test_bounded_returns_true_when_call_completes(driver):
    calls = []
    assert driver._bounded("noop", 5, lambda: calls.append(1)) is True
    assert calls == [1]


@pytest.mark.skipif(
    not hasattr(signal, "setitimer"), reason="SIGALRM-based bound is POSIX-only"
)
def test_bounded_gives_up_on_a_blocking_call(driver, capsys):
    """The defect: context.close() blocks forever. It must now be abandoned."""
    started = time.monotonic()

    def _wedged():
        time.sleep(30)          # stands in for the never-finalizing recording

    assert driver._bounded("context.close", 0.5, _wedged) is False
    assert time.monotonic() - started < 10, "bounded call did not return early"
    assert "TIMEOUT" in capsys.readouterr().out


def test_bounded_reports_but_swallows_exceptions(driver, capsys):
    def _boom():
        raise RuntimeError("browser already gone")

    assert driver._bounded("browser.close", 5, _boom) is False
    assert "browser.close raised RuntimeError" in capsys.readouterr().out


# --- incremental verdict ----------------------------------------------------


def test_verdict_rows_land_on_disk_immediately(driver, tmp_path):
    verdict = driver._Verdict(tmp_path)
    verdict.record("smoke", True, "SPA shell rendered")
    verdict.record("spec", False, "boom")

    rows = [
        json.loads(line)
        for line in (tmp_path / "verdict.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [(r["phase"], r["status"]) for r in rows] == [
        ("smoke", "pass"),
        ("spec", "fail"),
    ]
    assert rows[1]["detail"] == "boom"


def test_verdict_is_reset_per_run(driver, tmp_path):
    (tmp_path / "verdict.jsonl").write_text('{"phase": "stale"}\n')
    driver._Verdict(tmp_path).record("smoke", True)
    assert "stale" not in (tmp_path / "verdict.jsonl").read_text(encoding="utf-8")


# --- runaway recording ------------------------------------------------------


def test_oversized_recording_is_skipped_not_transcoded(driver, tmp_path, monkeypatch, capsys):
    """386MB for a 16s spec is a defect signal — don't burn minutes on ffmpeg."""
    monkeypatch.setenv("POD_E2E_FFMPEG", "/bin/false")
    (tmp_path / "page@runaway.webm").write_bytes(b"\0" * (2 * 1024 * 1024))

    ran: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **k: ran.append(list(argv)))

    driver._transcode_videos(tmp_path, max_mb=1)

    assert ran == [], "ffmpeg invoked for an oversized recording"
    assert "skipping transcode" in capsys.readouterr().out


def test_normal_recording_is_transcoded(driver, tmp_path, monkeypatch):
    monkeypatch.setenv("POD_E2E_FFMPEG", "/bin/true")
    (tmp_path / "page@small.webm").write_bytes(b"\0" * 16)

    argvs: list[list[str]] = []

    def _fake_run(argv, **kwargs):
        argvs.append(list(argv))
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    driver._transcode_videos(tmp_path, max_mb=200)

    assert len(argvs) == 1
    assert argvs[0][0] == "/bin/true"
    assert argvs[0][-1].endswith("page@small.mp4")


# --- orphan cleanup ---------------------------------------------------------


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="/proc walk is Linux-only")
def test_descendant_pids_finds_a_grandchild(driver):
    """A wedged run used to leave the driver + ~19 chromium processes behind."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import subprocess,sys,time;"
         "subprocess.Popen([sys.executable,'-c','import time;time.sleep(20)']);"
         "time.sleep(20)"]
    )
    try:
        deadline = time.monotonic() + 5
        pids: list[int] = []
        while time.monotonic() < deadline:
            pids = driver._descendant_pids(os.getpid())
            if len(pids) >= 2:
                break
            time.sleep(0.1)
        assert child.pid in pids
        assert len(pids) >= 2, "grandchild (chromium stand-in) not walked"
    finally:
        child.kill()
        child.wait(timeout=10)


@pytest.mark.skipif(not hasattr(signal, "SIGTERM"), reason="POSIX only")
def test_termination_cleanup_is_installed(driver):
    """`timeout` SIGTERMs us — we must take chromium with us, not orphan it."""
    prev = signal.getsignal(signal.SIGTERM)
    try:
        driver._install_termination_cleanup()
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler) and handler is not prev
    finally:
        signal.signal(signal.SIGTERM, prev)


# --- the orchestrator bounds the phase --------------------------------------


# --- spec-facing record() affects the exit code ------------------------------


class _FakeKeyboard:
    def press(self, _key):
        pass


class _FakeLocator:
    first = None

    def __init__(self):
        self.first = self

    def is_visible(self):
        return False


class _FakePage:
    def __init__(self):
        self.keyboard = _FakeKeyboard()
        self.closed = False
        self.unrouted = False

    def goto(self, *_a, **_k):
        pass

    def locator(self, _sel):
        return _FakeLocator()

    def screenshot(self, path=None):
        Path(path).write_bytes(b"")

    def text_content(self, _sel):
        return "KiroCrew dashboard shell rendered fine"

    def unroute_all(self):
        self.unrouted = True

    def close(self):
        self.closed = True


class _FakeContext:
    def __init__(self, hang_close: bool = False):
        self.page = _FakePage()
        self.closed = False
        self.hang_close = hang_close

    def add_init_script(self, _script):
        pass

    def new_page(self):
        return self.page

    def close(self):
        if self.hang_close:
            time.sleep(30)                      # the defect: never finalizes
        self.closed = True


class _FakeBrowser:
    def __init__(self, hang_close: bool = False):
        self.context = _FakeContext(hang_close=hang_close)
        self.closed = False

    def new_context(self, **_k):
        return self.context

    def close(self):
        self.closed = True


class _FakePlaywright:
    def __init__(self, hang_close: bool = False):
        self.browser = _FakeBrowser(hang_close=hang_close)
        self.chromium = types.SimpleNamespace(launch=lambda **_k: self.browser)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _run_spec(driver, tmp_path, spec_body: str):
    """Drive run() end-to-end against a fake browser, with a trusted spec."""
    spec_dir = tmp_path / ".pod-e2e"
    spec_dir.mkdir()
    spec = spec_dir / "feature.spec.py"
    spec.write_text(spec_body, encoding="utf-8")

    fake = _FakePlaywright()
    original = driver.sync_playwright
    driver.sync_playwright = lambda: fake
    try:
        rc = driver.run(
            base_url="http://127.0.0.1:1",
            token="t",
            artifact_dir=str(tmp_path / "art"),
            spec=str(spec),
            video=False,
            default_timeout_ms=1000,
            checkout=str(tmp_path),
        )
    finally:
        driver.sync_playwright = original
    rows = [
        json.loads(line)
        for line in (tmp_path / "art" / "verdict.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip()
    ]
    return rc, rows, fake


def test_spec_recorded_failure_fails_the_run(driver, tmp_path):
    """A recorded ok=False row must not exit 0 (GPT 5.6 finding on aedafbff)."""
    rc, rows, _ = _run_spec(driver, tmp_path, 'record("widget", False, "boom")\n')

    assert rc >= 1, "recorded failure produced a passing verdict"
    assert ("widget", "fail") in [(r["phase"], r["status"]) for r in rows]


def test_spec_recorded_pass_keeps_the_run_green(driver, tmp_path):
    rc, rows, fake = _run_spec(driver, tmp_path, 'record("widget", True, "ok")\n')

    assert rc == 0
    assert ("widget", "pass") in [(r["phase"], r["status"]) for r in rows]
    # teardown ran in order: routes off, page closed, context + browser closed
    assert fake.browser.context.page.unrouted is True
    assert fake.browser.context.page.closed is True
    assert fake.browser.context.closed is True
    assert fake.browser.closed is True


def _usable_bash() -> str | None:
    """Return a bash that can actually execute a script, else None.

    `shutil.which("bash")` is not enough: on GitHub's Windows runners it finds
    the WSL launcher stub, which exits 1 with a UTF-16 "no installed
    distributions" message instead of running anything. Probe it.
    """
    candidates = ["bash", r"C:\Program Files\Git\bin\bash.exe"]
    for cand in candidates:
        exe = shutil.which(cand)
        if exe is None and Path(cand).exists():
            exe = cand
        if exe is None:
            continue
        try:
            probe = subprocess.run(
                [exe, "-c", "echo ok"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
        except OSError:                         # pragma: no cover - env guard
            continue
        if probe.returncode == 0 and "ok" in (probe.stdout or ""):
            return exe
    return None


@pytest.mark.skipif(
    not hasattr(signal, "setitimer"),
    reason="the teardown bound needs SIGALRM; it degrades to unbounded here "
           "(the harness itself is POSIX-only)",
)
def test_unfinalized_recording_is_not_transcoded(driver, tmp_path, monkeypatch):
    """A wedged context.close() must not hand an incomplete .webm to ffmpeg.

    (GPT 5.6 finding on b29f392a: that costs ffmpeg's 600s timeout and yields a
    truncated .mp4.) The run must bail immediately instead.
    """
    class _Bail(Exception):
        pass

    transcodes: list[tuple] = []
    monkeypatch.setattr(
        driver, "_transcode_videos", lambda *a, **k: transcodes.append(a)
    )
    monkeypatch.setattr(driver, "_kill_browser_tree", lambda: None)

    def _fake_exit(code):
        raise _Bail(code)

    monkeypatch.setattr(driver.os, "_exit", _fake_exit)

    fake = _FakePlaywright(hang_close=True)
    monkeypatch.setattr(driver, "sync_playwright", lambda: fake)

    art = tmp_path / "art"
    with pytest.raises(_Bail):
        driver.run(
            base_url="http://127.0.0.1:1",
            token="t",
            artifact_dir=str(art),
            spec=None,
            video=True,
            default_timeout_ms=1000,
            teardown_timeout=0.2,
        )

    assert transcodes == [], "ffmpeg invoked on an unfinalized recording"
    rows = [
        json.loads(line)
        for line in (art / "verdict.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    teardown = [r for r in rows if r["phase"] == "teardown"]
    assert teardown and teardown[-1]["status"] == "fail"
    assert "not finalized" in teardown[-1]["detail"]
    assert fake.browser.closed is False, "browser.close attempted after the bail"


def test_exit_code_cannot_wrap_to_zero(driver):
    """A raw count of 256 would be reported as exit 0 (GPT on 62d4fd38)."""
    assert driver._exit_code(0) == 0
    assert driver._exit_code(3) == 3
    assert driver._exit_code(256) == driver._MAX_EXIT_CODE
    assert 0 < driver._exit_code(100000) < 255


def test_both_exit_sites_are_clamped(driver, monkeypatch, tmp_path):
    """The bail path and main() must route through _exit_code, not the raw count."""
    source = _DRIVER.read_text(encoding="utf-8")
    assert "os._exit(failures)" not in source
    assert "sys.exit(rc)" not in source

    codes: list[int] = []
    monkeypatch.setattr(driver, "_kill_browser_tree", lambda: None)
    monkeypatch.setattr(driver.os, "_exit", lambda code: codes.append(code))
    driver._bail_teardown(driver._Verdict(tmp_path), 300, "wedged")
    assert codes == [driver._MAX_EXIT_CODE]


def test_verdict_is_truncated_on_every_invocation():
    """A stale verdict must never be presented as this run's.

    GPT on `e4618909` caught the driver-dies-early case; the Arbiter on
    `0a853308` caught that truncating inside the Playwright branch still leaves
    stale rows for every skip path (--api-only, unhealthy pod, no
    KIROCREW_PW_PY). So it must run unconditionally, right after mkdir.
    """
    lines = _runner_text().splitlines()

    def _index(needle: str):
        return next((i for i, ln in enumerate(lines) if needle in ln), None)

    mkdir = _index('mkdir -p "$ARTIFACT_DIR"')
    truncate = _index(': > "$ARTIFACT_DIR/verdict.jsonl"')
    fe_branch = _index('if [ "$RUN_FE" -eq 1 ]')
    launch = _index('KIROCREW_POD_TOKEN="$TOKEN" "${PW_CMD[@]}"')

    assert truncate is not None, "verdict.jsonl is never truncated"
    assert mkdir is not None and fe_branch is not None and launch is not None
    assert mkdir < truncate, "truncated before its directory exists"
    assert truncate < fe_branch, (
        "truncation is inside the FE phase — skip paths would serve a stale verdict"
    )
    assert truncate < launch


def test_warnings_do_not_inflate_the_passed_count():
    """A ⚠️ line is neither a pass nor a fail (GPT 5.6 finding on 82ff4f3a).

    Runs the runner's OWN helper definitions and summary arithmetic under bash,
    so this fails if either side drifts.
    """
    bash = _usable_bash()
    if bash is None:                            # pragma: no cover - Windows
        pytest.skip("no working bash on this host")

    lines = _runner_text().splitlines()
    helpers = [ln for ln in lines if ln.startswith(("pass()", "fail()", "warn()"))]
    assert len(helpers) == 3, "pass/fail/warn helpers not found"

    start = next(i for i, ln in enumerate(lines) if ln.startswith("PASSED="))
    end = next(i for i, ln in enumerate(lines) if ln.startswith('echo "$SUMMARY"'))
    summary = lines[start:end + 1]

    script = "\n".join(
        [
            "set -uo pipefail",
            "declare -a RESULTS=(); FAILURES=0; WARNINGS=0",
            *helpers,
            'pass "phase-a"; fail "phase-b"; warn "phase-c"',
            *summary,
        ]
    )
    out = subprocess.run(
        [bash, "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert out.returncode == 0, f"{out.stdout}\n{out.stderr}"
    assert "result:       1 passed, 1 failed, 1 warning(s)" in out.stdout, out.stdout


def test_pod_e2e_sh_bounds_the_playwright_phase():
    body = _runner_text()
    assert "POD_E2E_PW_TIMEOUT" in body
    assert "timeout --kill-after" in body
    # unbuffered driver output, so playwright.log is diagnosable mid-hang
    assert '"$PW_PY" -u' in body
    # a timeout is a distinct reportable outcome, not a silent stall
    assert "TIMED OUT" in body
    assert "-eq 124" in body


def test_teardown_bail_row_matches_the_summary_grep(driver, tmp_path):
    """The summary warning greps verdict.jsonl — keep the two in lockstep."""
    import re

    driver._Verdict(tmp_path).record("teardown", False, "graceful close timed out")
    line = (tmp_path / "verdict.jsonl").read_text(encoding="utf-8").strip()

    pattern = re.search(
        r"grep -q '([^']+)' \"\$ARTIFACT_DIR/verdict\.jsonl\"", _runner_text()
    )
    assert pattern, "summary no longer greps verdict.jsonl"
    assert re.search(pattern.group(1), line), (
        f"grep pattern {pattern.group(1)!r} no longer matches a teardown row"
    )


def test_missing_playwright_interpreter_fails_the_run():
    """A run that captured ZERO screenshots must not report a green summary.

    The FE phase used to `warn` when KIROCREW_PW_PY was unset, so the summary
    said "N passed, 0 failed" with no evidence on disk — which is how "capture
    is in flight" becomes a believable but false statement. It must `fail`.
    """
    lines = _runner_text().splitlines()

    def _index(needle: str):
        return next((i for i, ln in enumerate(lines) if needle in ln), None)

    fe_branch = _index('if [ "$RUN_FE" -eq 1 ]')
    no_interp = _index('if [ -z "$PW_PY" ] || [ ! -x "$PW_PY" ]; then')
    no_runner = _index('elif [ ! -f "$PW_RUNNER" ]; then')
    assert fe_branch is not None and no_interp is not None and no_runner is not None
    assert fe_branch < no_interp < no_runner

    # Both zero-screenshot branches sit between the interpreter test and the
    # `else` that launches the driver — neither may warn.
    launch_else = next(
        i for i, ln in enumerate(lines) if i > no_runner and ln.strip() == "else"
    )
    branch = "\n".join(lines[no_interp:launch_else])
    assert "warn " not in branch, "a zero-screenshot branch still only warns"
    assert branch.count("fail ") == 2, "both zero-screenshot branches must fail"
    assert "KIROCREW_PW_PY" in branch


def test_missing_playwright_message_names_the_pinned_version_and_the_opt_out():
    """The fix must be copy-pasteable, and the pin must match the chromium build
    the Node Playwright MCP server has already downloaded (1.61.0 → chromium-1228);
    any other version triggers a fresh ~170MB download."""
    body = _runner_text()
    assert "playwright==1.61.0" in body
    assert "export KIROCREW_PW_PY=" in body
    # --api-only stays the one clean way to skip the frontend phase.
    assert "--api-only" in body
    assert "--api-only) RUN_FE=0 ;;" in body


def test_api_only_skips_the_whole_fe_phase():
    """`--api-only` sets RUN_FE=0, so the FE block (and its new failures) is
    never entered — the graceful skip survives."""
    lines = _runner_text().splitlines()
    assert any("--api-only) RUN_FE=0 ;;" in ln for ln in lines)
    fe_branch = next(i for i, ln in enumerate(lines) if 'if [ "$RUN_FE" -eq 1 ]' in ln)
    no_interp = next(
        i for i, ln in enumerate(lines) if 'if [ -z "$PW_PY" ] || [ ! -x "$PW_PY" ]' in ln
    )
    assert fe_branch < no_interp, "the interpreter check escaped the RUN_FE guard"

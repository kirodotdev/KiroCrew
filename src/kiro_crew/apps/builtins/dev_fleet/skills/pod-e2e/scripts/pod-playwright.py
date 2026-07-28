#!/usr/bin/env python3
"""pod-playwright.py — headless-chromium frontend check for a KiroCrew pod.

Run by pod-e2e.sh with a Playwright venv python (Playwright + bundled
chromium at ~/.cache/ms-playwright/chromium-*). The KiroCrew gateway serves its
own SPA bundle (src/kiro_crew/static/dist), so we point chromium straight at the
pod port with ?token=<t> — no separate FE dev server.

Two phases:
  1. smoke  — always: load `/?token=`, assert the SPA shell rendered (no 401/blank),
              screenshot to <artifact-dir>/fe-smoke.png.
  2. spec   — optional: if --spec <file> is given, exec it with a live authed `page`
              in scope. The spec asserts feature-specific UI. Keeps the e2e flow
              hands-off: drop a .py spec next to the feature, no test runner needed.

Names in scope for a spec (no imports needed):
  page          — Playwright sync Page, already loaded on the authed dashboard
  context       — the BrowserContext
  base_url      — http://127.0.0.1:<pod-port>  (NOT the live port)
  token         — dashboard token
  artifact_dir  — where to drop screenshots
  expect        — Playwright's NATIVE web-first assertion (auto-retries!).
                  e.g. expect(page.locator('[role=dialog]')).to_be_visible()
  expect_true   — tiny boolean assert: expect_true(cond, "why") (no auto-retry)

--video records the whole session into <artifact-dir> (opt-in). It records at
1080p with paced (slow-mo) actions for clarity and also writes a shareable .mp4
beside the .webm. Exit 0 = all phases passed.
Never touches the live gateway — only the URL passed in --base-url.

Teardown is BOUNDED: `context.close()` is what finalizes a recording and has
been observed to block forever (unbounded .webm growth) after a spec has already
passed. Every teardown step therefore runs under a hard wall-clock cap
(--teardown-timeout); on expiry the already-determined verdict is kept, browser
descendants are killed, and the process exits instead of hanging. The verdict is
also written incrementally to <artifact-dir>/verdict.jsonl as each phase is
decided, so even a wedged run leaves a readable result.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import traceback
from pathlib import Path

try:
    from playwright.sync_api import expect as pw_expect
    from playwright.sync_api import sync_playwright
except Exception as e:  # pragma: no cover - environment guard
    print(f"FATAL: playwright not importable in this interpreter: {e}", file=sys.stderr)
    sys.exit(2)


# First-run UI that a FRESH browser context always triggers.
_FIRST_RUN_LS = {
    "kc-onboarded": "1",   # theme onboarding
}

# Defaults for the guards added for the "--video wedges the run" defect:
# a spec that passes in 16s must never be lost to an unbounded teardown.
_DEFAULT_TEARDOWN_TIMEOUT = 30.0   # seconds per teardown step
_DEFAULT_MAX_VIDEO_MB = 200        # a sane .webm for a short spec is single-digit MB
# A process exit status is truncated to its low 8 bits, so a raw failure count
# of 256 would be reported as 0 — a silent pass. Clamp every exit through
# _exit_code() and leave headroom below 255 for shell-reserved codes.
_MAX_EXIT_CODE = 250


def _exit_code(failures: int) -> int:
    """Map a failure count to a safe process exit status (0 stays 0)."""
    return min(failures, _MAX_EXIT_CODE)


def _log(msg: str) -> None:
    """Print immediately — a wedged run must still leave a diagnosable log."""
    print(msg, flush=True)


class _Verdict:
    """Append-only per-phase result log.

    Written as each phase is decided (and flushed), so a hang during teardown
    still leaves a usable verdict on disk instead of an empty log file.
    """

    def __init__(self, art_dir: Path):
        self.path = art_dir / "verdict.jsonl"
        try:
            self.path.write_text("")   # fresh per run
        except OSError:
            pass

    def record(self, phase: str, ok: bool, detail: str = "") -> None:
        _log(f"{'PASS' if ok else 'FAIL'} {phase}: {detail}" if detail
             else f"{'PASS' if ok else 'FAIL'} {phase}")
        row = {
            "ts": time.time(),
            "phase": phase,
            "status": "pass" if ok else "fail",
            "detail": detail,
        }
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError:
            pass


class _Deadline(Exception):
    """Raised inside a bounded block when its wall-clock budget expires."""


def _bounded(label: str, seconds: float, fn) -> bool:
    """Run fn() under a hard wall-clock cap. True = completed, False = gave up.

    SIGALRM is the mechanism because the sync Playwright API is greenlet-based
    and must be driven from the thread that created it — a worker thread cannot
    be used to bound a blocking call like context.close().
    """
    if not hasattr(signal, "setitimer") or seconds <= 0:
        if seconds > 0:                          # pragma: no cover - env guard
            _log(f"[teardown] {label}: UNBOUNDED on this platform "
                 "(no SIGALRM) — a wedged close cannot be interrupted here")
        try:
            fn()
            return True
        except Exception as exc:                     # pragma: no cover - env guard
            _log(f"[teardown] {label} raised {type(exc).__name__}: {exc}")
            return False

    def _fire(_signum, _frame):
        raise _Deadline(label)

    prev = signal.signal(signal.SIGALRM, _fire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        fn()
        return True
    except _Deadline:
        _log(f"[teardown] TIMEOUT: {label} exceeded {seconds:g}s — abandoning graceful close")
        return False
    except Exception as exc:
        _log(f"[teardown] {label} raised {type(exc).__name__}: {exc}")
        return False
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, prev)


def _descendant_pids(root: int) -> list[int]:
    """Best-effort descendant walk via /proc (Linux). [] where /proc is absent."""
    children: dict[int, list[int]] = {}
    proc = Path("/proc")
    if not proc.is_dir():
        return []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            # comm can contain ')' — ppid is the field right after the last ')'
            ppid = int(stat[stat.rindex(")") + 1:].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        children.setdefault(ppid, []).append(int(entry.name))
    out: list[int] = []
    stack = list(children.get(root, []))
    while stack:
        pid = stack.pop()
        out.append(pid)
        stack.extend(children.get(pid, []))
    return out


def _kill_browser_tree() -> None:
    """Kill the playwright driver + chromium descendants of THIS process.

    A wedged teardown previously left orphaned drivers and ~19 chromium
    processes behind that had to be killed by hand.
    """
    pids = _descendant_pids(os.getpid())
    if not pids:
        return
    _log(f"[teardown] force-killing {len(pids)} browser descendant process(es)")
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in pids:
            try:
                os.kill(pid, sig)
            except OSError:
                pass
        if sig is signal.SIGTERM:
            time.sleep(1.0)


def _bail_teardown(verdict: "_Verdict", failures: int, detail: str) -> None:
    """Record the abandoned teardown, kill the browser, and exit immediately.

    Deliberately skips sync_playwright().__exit__, which would block on the same
    wedged connection. Closing our pipes makes the driver process exit too.
    """
    verdict.record("teardown", False, detail)
    _kill_browser_tree()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(_exit_code(failures))


def _install_termination_cleanup() -> None:
    """On SIGTERM/SIGINT (e.g. pod-e2e.sh's `timeout`), take the browser with us.

    Without this, killing a stalled driver leaves the playwright driver and its
    chromium processes running until someone notices them.
    """
    def _bail(signum, _frame):
        _log(f"[teardown] received signal {signum} — killing browser tree and exiting")
        _kill_browser_tree()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(128 + signum)

    for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
        if sig is not None:
            try:
                signal.signal(sig, _bail)
            except (OSError, ValueError):       # pragma: no cover - env guard
                pass


def _transcode_videos(art_dir, max_mb: int = _DEFAULT_MAX_VIDEO_MB):
    """Best-effort .webm -> .mp4 transcode for shareability (per SKILL.md:
    requires ffmpeg; when absent the .webm is kept and no .mp4 is produced).

    Recordings larger than max_mb are reported and skipped: a short spec that
    produced hundreds of MB is a defect signal, and transcoding it would burn
    minutes for an unusable artifact.
    """
    import shutil as _shutil
    import subprocess as _sp
    from pathlib import Path as _P
    ffmpeg = os.environ.get("POD_E2E_FFMPEG") or _shutil.which("ffmpeg")
    if not ffmpeg:
        _log("[video] ffmpeg not found - keeping .webm only")
        return
    for webm in sorted(_P(art_dir).glob("*.webm")):
        try:
            size_mb = webm.stat().st_size / (1024 * 1024)
        except OSError:
            size_mb = 0.0
        if max_mb > 0 and size_mb > max_mb:
            _log(f"[video] {webm.name} is {size_mb:.0f}MB (> {max_mb}MB cap) - "
                 "recording ran away, skipping transcode")
            continue
        mp4 = webm.with_suffix(".mp4")
        try:
            # ffmpeg path comes from POD_E2E_FFMPEG (operator env) or
            # shutil.which; inputs are glob results in our own artifact dir;
            # argv-array exec, no shell. Dev-only harness script.
            argv = [ffmpeg, "-y", "-i", str(webm), "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(mp4)]
            r = _sp.run(argv, capture_output=True, timeout=600)  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args
            if r.returncode == 0:
                _log(f"[video] wrote {mp4.name}")
            else:
                _log(f"[video] ffmpeg failed for {webm.name} - keeping .webm")
        except (OSError, _sp.TimeoutExpired):
            _log(f"[video] transcode error for {webm.name} - keeping .webm")


def run(base_url: str, token: str, artifact_dir: str, spec: str | None,
        video: bool, default_timeout_ms: int, suppress_first_run: bool = True,
        slow_mo_ms: int = 0, checkout: str | None = None,
        teardown_timeout: float = _DEFAULT_TEARDOWN_TIMEOUT,
        max_video_mb: int = _DEFAULT_MAX_VIDEO_MB) -> int:
    art = Path(artifact_dir)
    art.mkdir(parents=True, exist_ok=True)
    authed = f"{base_url}/?token={token}"
    failures = 0
    verdict = _Verdict(art)
    started = time.monotonic()

    pw_expect.set_options(timeout=default_timeout_ms)

    with sync_playwright() as p:
        launch_opts: dict = {"headless": True}
        ctx_opts: dict = {"viewport": {"width": 1600, "height": 1000}}
        if slow_mo_ms:
            launch_opts["slow_mo"] = slow_mo_ms
        if video:
            ctx_opts["record_video_dir"] = str(art)
            ctx_opts["record_video_size"] = {"width": 1920, "height": 1080}

        browser = p.chromium.launch(**launch_opts)
        context = browser.new_context(**ctx_opts)
        page = context.new_page()

        # Pre-seed localStorage to suppress first-run modals
        if suppress_first_run:
            context.add_init_script(
                "() => { " +
                " ".join(f'localStorage.setItem("{k}","{v}");' for k, v in _FIRST_RUN_LS.items()) +
                " }"
            )

        # --- Phase 1: Smoke ---
        try:
            page.goto(authed, wait_until="networkidle", timeout=30000)
            # Dismiss any first-run modal that slipped through
            if suppress_first_run:
                page.keyboard.press("Escape")
                close_btn = page.locator('[aria-label="Close"]').first
                if close_btn.is_visible():
                    close_btn.click()
            page.screenshot(path=str(art / "fe-smoke.png"))
            # Assert SPA shell rendered (not a blank/error page)
            body_text = page.text_content("body") or ""
            if "Cannot GET" in body_text or len(body_text.strip()) < 20:
                verdict.record("smoke", False,
                               f"body too short or error page: {body_text[:100]}")
                failures += 1
            else:
                verdict.record("smoke", True, "SPA shell rendered")
        except Exception as exc:
            verdict.record("smoke", False, str(exc))
            traceback.print_exc()
            page.screenshot(path=str(art / "fe-smoke-FAIL.png"))
            failures += 1

        # --- Phase 2: Spec ---
        if spec and failures == 0:
            spec_path = Path(spec).resolve()
            # Trust model: accept specs from TWO locations:
            # 1. This skill's own specs/ directory
            # 2. The worktree's .pod-e2e/ directory (if --checkout provided)
            _skill_dir = (Path(__file__).resolve().parent.parent / "specs").resolve()
            contained = _skill_dir.is_dir() and (
                spec_path == _skill_dir or spec_path.is_relative_to(_skill_dir)
            )
            if not contained and checkout:
                _checkout_e2e = (Path(checkout).resolve() / ".pod-e2e").resolve()
                contained = _checkout_e2e.is_dir() and spec_path.is_relative_to(
                    _checkout_e2e
                )
            if not contained:
                allowed = f"skill specs/ ({_skill_dir})"
                if checkout:
                    allowed += f" or checkout .pod-e2e/ ({Path(checkout).resolve() / '.pod-e2e'})"
                verdict.record(
                    "spec", False,
                    f"path {spec_path} is outside allowed directories: "
                    f"{allowed} — refusing to execute",
                )
                failures += 1
            elif not spec_path.exists():
                verdict.record("spec", False, f"file not found: {spec}")
                failures += 1
            else:
                def expect_true(condition: bool, msg: str = "assertion failed"):
                    if not condition:
                        raise AssertionError(msg)

                def record_result(name: str, ok: bool, detail: str = "") -> None:
                    """Spec-facing record: an ok=False row FAILS the run.

                    Without this, a spec could report a failed assertion and
                    still exit 0 — a silent pass on a real defect.
                    """
                    nonlocal failures
                    verdict.record(name, ok, detail)
                    if not ok:
                        failures += 1

                scope = {
                    "page": page,
                    "context": context,
                    "base_url": base_url,
                    "token": token,
                    "artifact_dir": str(art),
                    "expect": pw_expect,
                    "expect_true": expect_true,
                    # Specs may append their own per-assertion rows so a later
                    # stall still leaves the decided results on disk. A False
                    # row counts as a failure, so it cannot pass silently.
                    "record": record_result,
                }
                try:
                    # Dev-only E2E harness: spec files are local, developer-authored
                    # Playwright scripts loaded from this skill's own directory or
                    # the worktree under test — not external/user input. Path is
                    # containment-checked above against skill specs/ dir and
                    # KiroCrew worktree roots.
                    exec(spec_path.read_text(encoding="utf-8"), scope)  # nosemgrep: python.lang.security.audit.exec-detected.exec-detected
                    verdict.record("spec", True, spec)
                except Exception as exc:
                    verdict.record("spec", False, f"{spec}: {exc}")
                    traceback.print_exc()
                    page.screenshot(path=str(art / "fe-spec-FAIL.png"))
                    failures += 1

        # --- Teardown (bounded) ---------------------------------------------
        # The verdict above is already decided and on disk. context.close() is
        # what finalizes a video recording and has been observed to block
        # forever, so every step below runs under a hard cap and a stall can
        # only cost the .mp4 — never the result.
        _log(f"[teardown] closing browser (cap {teardown_timeout:g}s per step) ...")
        elapsed = time.monotonic() - started
        if video:
            _log(f"[video] session length {elapsed:.0f}s — finalizing recording")

        # Give the recording a defined end: stop route handlers and page
        # activity BEFORE asking the context to finalize.
        unroute_all = getattr(page, "unroute_all", None)   # playwright >= 1.41
        if unroute_all is not None:
            _bounded("page.unroute_all", min(5.0, teardown_timeout), unroute_all)
        _bounded("page.close", min(10.0, teardown_timeout), page.close)

        ctx_closed = _bounded("context.close", teardown_timeout, context.close)
        if not ctx_closed:
            # The recording never reached a defined end, so the .webm is
            # incomplete: transcoding it would spend up to ffmpeg's 600s
            # timeout to produce a truncated .mp4. Keep the partial file and
            # get out instead of doing more bounded work on a wedged browser.
            if video:
                _log("[video] recording never finalized — keeping the partial "
                     ".webm, skipping transcode")
            _bail_teardown(verdict, failures,
                           "context.close timed out — recording not finalized; "
                           "artifacts kept, browser killed, exiting hard")

        if video:
            _transcode_videos(art, max_video_mb)

        if not _bounded("browser.close", teardown_timeout, browser.close):
            _bail_teardown(verdict, failures,
                           "browser.close timed out — artifacts kept, "
                           "browser killed, exiting hard")

    return failures


def main():
    # A wedged run must still be diagnosable: pod-e2e.sh redirects our stdout to
    # a file, which would otherwise stay block-buffered and empty mid-hang.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:                # pragma: no cover - env guard
            continue
        try:
            reconfigure(line_buffering=True)
        except (OSError, ValueError):          # pragma: no cover - env guard
            pass

    _install_termination_cleanup()

    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--token", default=None,
                    help="Dashboard token (prefer KIROCREW_POD_TOKEN env — argv is world-readable)")
    ap.add_argument("--artifact-dir", required=True)
    ap.add_argument("--spec", default=None)
    ap.add_argument("--checkout", default=None,
                    help="Worktree checkout path; specs under <checkout>/.pod-e2e/ are trusted")
    ap.add_argument("--video", action="store_true")
    ap.add_argument("--timeout", type=int, default=10000)
    ap.add_argument("--teardown-timeout", type=float, default=_DEFAULT_TEARDOWN_TIMEOUT,
                    help="Hard cap (seconds) per browser-teardown step; 0 disables the cap")
    ap.add_argument("--max-video-mb", type=int, default=_DEFAULT_MAX_VIDEO_MB,
                    help="Skip transcoding recordings larger than this (runaway guard); 0 disables")
    ap.add_argument("--no-suppress-first-run", action="store_true")
    ap.add_argument("--slow-mo", type=int, default=0)
    args = ap.parse_args()

    token = os.environ.get("KIROCREW_POD_TOKEN") or args.token
    if not token:
        ap.error("token required: set KIROCREW_POD_TOKEN env or pass --token")

    rc = run(
        base_url=args.base_url,
        token=token,
        artifact_dir=args.artifact_dir,
        spec=args.spec,
        video=args.video,
        default_timeout_ms=args.timeout,
        suppress_first_run=not args.no_suppress_first_run,
        slow_mo_ms=args.slow_mo,
        checkout=args.checkout,
        teardown_timeout=args.teardown_timeout,
        max_video_mb=args.max_video_mb,
    )
    sys.exit(_exit_code(rc))


if __name__ == "__main__":
    main()

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
"""
from __future__ import annotations

import argparse
import os
import sys
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


def _transcode_videos(art_dir):
    """Best-effort .webm -> .mp4 transcode for shareability (per SKILL.md:
    requires ffmpeg; when absent the .webm is kept and no .mp4 is produced)."""
    import shutil as _shutil
    import subprocess as _sp
    from pathlib import Path as _P
    ffmpeg = os.environ.get("POD_E2E_FFMPEG") or _shutil.which("ffmpeg")
    if not ffmpeg:
        print("[video] ffmpeg not found - keeping .webm only")
        return
    for webm in sorted(_P(art_dir).glob("*.webm")):
        mp4 = webm.with_suffix(".mp4")
        try:
            # ffmpeg path comes from POD_E2E_FFMPEG (operator env) or
            # shutil.which; inputs are glob results in our own artifact dir;
            # argv-array exec, no shell. Dev-only harness script.
            argv = [ffmpeg, "-y", "-i", str(webm), "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(mp4)]
            r = _sp.run(argv, capture_output=True, timeout=600)  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args
            if r.returncode == 0:
                print(f"[video] wrote {mp4.name}")
            else:
                print(f"[video] ffmpeg failed for {webm.name} - keeping .webm")
        except (OSError, _sp.TimeoutExpired):
            print(f"[video] transcode error for {webm.name} - keeping .webm")


def run(base_url: str, token: str, artifact_dir: str, spec: str | None,
        video: bool, default_timeout_ms: int, suppress_first_run: bool = True,
        slow_mo_ms: int = 0, checkout: str | None = None) -> int:
    art = Path(artifact_dir)
    art.mkdir(parents=True, exist_ok=True)
    authed = f"{base_url}/?token={token}"
    failures = 0

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
                print(f"FAIL smoke: body too short or error page: {body_text[:100]}")
                failures += 1
            else:
                print("PASS smoke: SPA shell rendered")
        except Exception as exc:
            print(f"FAIL smoke: {exc}")
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
                print(
                    f"FAIL spec: path {spec_path} is outside allowed directories: "
                    f"{allowed} — refusing to execute"
                )
                failures += 1
            elif not spec_path.exists():
                print(f"FAIL spec: file not found: {spec}")
                failures += 1
            else:
                def expect_true(condition: bool, msg: str = "assertion failed"):
                    if not condition:
                        raise AssertionError(msg)

                scope = {
                    "page": page,
                    "context": context,
                    "base_url": base_url,
                    "token": token,
                    "artifact_dir": str(art),
                    "expect": pw_expect,
                    "expect_true": expect_true,
                }
                try:
                    # Dev-only E2E harness: spec files are local, developer-authored
                    # Playwright scripts loaded from this skill's own directory or
                    # the worktree under test — not external/user input. Path is
                    # containment-checked above against skill specs/ dir and
                    # KiroCrew worktree roots.
                    exec(spec_path.read_text(), scope)  # nosemgrep: python.lang.security.audit.exec-detected.exec-detected
                    print(f"PASS spec: {spec}")
                except Exception as exc:
                    print(f"FAIL spec: {exc}")
                    traceback.print_exc()
                    page.screenshot(path=str(art / "fe-spec-FAIL.png"))
                    failures += 1

        context.close()
        if video:
            _transcode_videos(art)
        browser.close()

    return failures


def main():
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
    )
    sys.exit(rc)


if __name__ == "__main__":
    main()

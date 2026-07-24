---
name: pod-e2e
description: "Run end-to-end tests (backend API + frontend Playwright) for a KiroCrew feature worktree against an ISOLATED throwaway pod instance, without touching the live gateway. Use when asked to e2e-test / smoke-test / verify a worktree's feature hands-off, run API + browser tests on a pod, or prove a new backend route + UI work together. NOT for testing the live instance."
triggers: e2e test, smoke test, pod test, verify worktree, test pod, run e2e, end to end
---

# pod-e2e — test a worktree against an isolated full-stack pod

A worktree's full stack (backend API **and** frontend SPA) runs as **one process
on one port** — exactly like a Docker container. The `kirocrew pod` CLI is the
only interface you need: spin one up, get a `{base_url, token}` handle, test
against it, tear it down to zero residue. The live gateway is **never touched**.

## Quickstart — run the bundled e2e suite

```bash
bash <app-skills-dir>/pod-e2e/scripts/pod-e2e.sh <worktree-name> --video
```

**Expected success output** — a `POD-E2E SUMMARY` ending like:
```
  ✅ auth — GET /api/sessions → 200 with token, 403 without
  ✅ api-tests — … → exit 0
  ✅ playwright — headless chromium loaded dashboard …
result:       3 passed, 0 failed
ARTIFACT_DIR=~/.kirocrew-pods/.e2e-artifacts/<worktree-name>
```
Exit code = number of failed phases (0 = all green). Then **look at the
evidence**: `Read` the screenshots in that `ARTIFACT_DIR`
(`fe-smoke.png`, plus any spec screenshots) to confirm the real UI rendered —
not a 403/blank page.

To smoke-test isolation after it finishes:
```bash
kirocrew pod ls          # should be empty (torn down)
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5476/api/sessions   # live plane still alive
```

## The interface — `kirocrew pod` CLI

```bash
# 1. bring the pod up, get a handle (JSON: base_url + token + port)
kirocrew pod up <wt> --json
#    → {"name":"<wt>","status":"up","port":7958,
#       "base_url":"http://127.0.0.1:7958","token":"…","ttl":"2h"}

# 2. test against the handle — full stack, ONE port:
curl -s "$base_url/api/<anything>?token=$token"      # backend API
#    open  $base_url/?token=$token  in Playwright     # frontend SPA (same port)

# 3. destroy it — zero residue, live gateway untouched
kirocrew pod down <wt>
```

Other verbs: `ls` (list running pods) · `status <wt>` · `token <wt>` · `url <wt>`
· `logs <wt>` · `provision <wt>`. Run `kirocrew pod --help` for the full list.

**Isolation guarantees** (enforced by the pod runtime):
own `KIROCREW_HOME`, own port, **no tunnel** (can't grab the real Slack identity),
`--no-crons`, cgroup `MemoryMax=4G`/`CPUQuota=200%`, and cleanup on stop.
A pod can **never** collide with the live gateway and many can run at once.

## Bundled suite (quick path)

```bash
bash <app-skills-dir>/pod-e2e/scripts/pod-e2e.sh <worktree-name>
```

Runs the bundled orchestrator end-to-end. Prints a `POD-E2E SUMMARY` ending in
`ARTIFACT_DIR=<path>` and exits with the **number of failed phases** (0 = all
green). Flags:

| flag | effect |
|---|---|
| `--keep` / `--no-stop` | leave the pod running after tests (debug) |
| `--api-only` | skip the Playwright phase |
| `--fe-only` | skip the API-test phase |
| `--video` | record the session at 1080p → `.webm` + `.mp4` |

## What each phase does

1. **up** — `kirocrew pod up <wt> --json`. If already active, reuses it (and
   won't stop it on exit). Boots the worktree's own gateway with `--no-crons`,
   blank-seed DB, isolated HOME.
2. **health** — polls `base_url/api/health` until 200/401/403 (≤45s). On timeout
   it dumps logs to `boot-fail.log` and aborts.
3. **auth** — proves auth: `/api/sessions` → 200 with token, 403 without.
   Token comes from `kirocrew pod up --json` output (no manual minting needed).
4. **API tests** — runs the fixed command `python -m pytest -q` with cwd=the
   worktree root, the worktree's `.venv` on PATH, and `POD_BASE_URL` +
   `POD_TOKEN` in env so tests can hit the live pod.
5. **Playwright** — `pod-playwright.py` (run with a Playwright venv + bundled
   chromium) loads `/?token=` headless, asserts the SPA rendered (screenshots
   `fe-smoke.png`), then exec's the optional `PLAYWRIGHT_SPEC` with a live authed
   `page` in scope.
   - If the Playwright interpreter is not executable, the FE phase **skips
     gracefully** (no failure, just a warning).
   - `--video` requires `ffmpeg` for mp4 transcoding; if absent, the `.webm` is
     kept but no `.mp4` is produced.
6. **collect** — all logs + screenshots land in
   `~/.kirocrew-pods/.e2e-artifacts/<wt>/`.
7. **stop** — `kirocrew pod down <wt>` (zero residue). Skipped if `--keep` or if
   the pod was already up.

## The test manifest (`.pod-test.sh`)

Optional per-worktree file declaring how THIS feature is tested. Searched at
`<worktree>/.pod-test.sh` then `<worktree>/src/kiro_crew/.pod-test.sh`.
The manifest is parsed **declaratively** (a `PLAYWRIGHT_SPEC=` line is
extracted textually) — it is **never sourced or eval'd** on the host.

```sh
# .pod-test.sh
PLAYWRIGHT_SPEC=".pod-e2e/feature.spec.py" # frontend spec, relative to the manifest's dir
```

### Trust model

The **pod** isolates the gateway under test (own `KIROCREW_HOME`, own port,
no tunnel, resource caps). The **test runner** is not a sandbox: running a
worktree's tests executes that worktree's code as your user — exactly like
running `pytest` in the checkout yourself. Only run pod-e2e against branches
you would be willing to build and test locally.

### Playwright spec contract

A spec is plain Python exec'd with these names in scope (no imports needed):
`page` (already on the authed app), `context`, `base_url`, `token`,
`artifact_dir`, `expect` (Playwright's **native web-first assertion** —
`expect(locator).to_be_visible()`, auto-retries), and `expect_true(cond, msg)`
(boolean fallback, raises `AssertionError`). Assert UI, take screenshots into
`artifact_dir`. Run with `--video` to also record a `.webm` (+ a shareable
`.mp4`) at 1080p, paced.

### First-run noise is auto-suppressed

A fresh pod = a real first-run: every Playwright context starts with empty
`localStorage`, so onboarding/changelog modals would pop up and overlay the
feature you're testing. The runner handles this automatically:
- **pre-seeds** `localStorage` so theme modal never mounts;
- **dismisses** any modal that still appears by pressing Escape + clicking
  `[aria-label="Close"]` if present.

Pass `--no-suppress-first-run` to let those modals appear (only if testing the
onboarding flow itself).

## PRIMARY USE — dev agent delegates to a QA agent

When you (the agent building a feature) want it tested, spawn a *separate QA
agent* via `spawn_run` and hand it the worktree name. You keep coding; the QA
agent runs the isolated pod, inspects the evidence, triages failures, and reports
a verdict back as a completion event.

### The QA agent prompt (copy, fill `<wt>` + the feature one-liner)

```
spawn_run(task="""
You are a QA engineer verifying the KiroCrew feature in worktree '<wt>'.
Feature under test: <one-line description of what this branch adds>.

Run the isolated end-to-end suite (it spins a throwaway pod on its own port,
never touches the live instance, and tears it down after):

    bash <app-skills-dir>/pod-e2e/scripts/pod-e2e.sh <wt> --video

Rules:
- Do NOT `cat` any .local_secret yourself (credential-read blocked). The script
  mints the token internally via the CLI — just run the one command above.
- After it finishes, READ the artifacts in the printed ARTIFACT_DIR:
  api-tests.log, playwright.log, fe-*.png screenshots (use the Read tool on the
  .png to actually look at the UI), and boot-fail.log if present.

Then return a QA VERDICT, not a raw dump:
  1. Overall: PASS / FAIL / BLOCKED (couldn't even boot the pod).
  2. Per check (auth / api-tests / playwright): pass|fail + one-line evidence.
  3. For each FAIL: triage it — is it (a) a real regression in the feature,
     (b) a flaky/timing issue, or (c) an environment problem (missing venv,
     missing dist, port clash)? Cite the log line or screenshot that proves it.
  4. The ARTIFACT_DIR path so the dev can open screenshots/video.
""")
```

### When to delegate vs run inline
- **Delegate to a QA agent** (default): you're mid-feature and want it verified
  without derailing your own context; or the suite is long (Playwright + video).
- **Run inline** yourself: a quick smoke where you want the result in your own turn.

Parallel QA across branches: spawn one QA agent per worktree in a single
`spawn_run` `tasks=[...]` call — each pod gets its own port and isolated HOME,
so they don't collide.

## Prerequisites & the provisioning on-ramp

A worktree must be **built** before it can be podded — its own
`.venv/bin/kirocrew` (editable install) + a built SPA bundle (`static/dist`).
The pod boot refuses without them.

```bash
kirocrew pod up <wt>                 # auto-builds the venv; FAILS LOUD if no dist
kirocrew pod up <wt> --provision     # full on-ramp: venv + build, then up
kirocrew pod provision <wt>          # just the on-ramp (venv + dist)
kirocrew pod provision <wt> --venv-only
```

Every failure teaches the next step: no worktree → create one; no venv → auto;
no dist → build-or-`--provision`.

- Playwright venv: controlled by env `KIROCREW_PW_PY`.
  The FE phase skips gracefully if this interpreter is not executable.
- `--video` needs `ffmpeg` on PATH (or pointed to by `POD_FFMPEG` env).
  If absent, `.webm` is kept but no `.mp4` transcoding occurs.

## Hands off the live plane

This skill only ever talks to pod ports (78xx). It must never restart or touch
the live gateway. If the derived port ever resolves to the production port the
orchestrator refuses and exits.

## Attach approved QA media to the PR (MANDATORY workflow)

QA screenshots and demo videos follow a **review-then-attach** contract:

1. **Deliver to the user first.** Send the screenshots/video for review (after
   the usual frame inspection for overlays -- first-run modals, toasts, theme
   pickers). Never attach media the user has not seen.
2. **Wait for explicit approval of the media.** A silent user is NOT approval.
3. **On approval, attach to the PR automatically -- do NOT ask again.**
   - Copy the approved files into `<worktree>/temp-screenshots/<feature>/`
     (top-level ephemeral dir; NEVER under `docs/` or `src/kiro_crew/**` --
     those trees ship in the wheel/sdist and desktop DMG).
   - Stage `temp-screenshots/<feature>/`, **amend into the PR's single
     commit**, and force-push with lease (standalone push command naming the
     feature branch).
   - Update the PR body with **commit-SHA-pinned** raw URLs:
     - Images inline: `![alt](https://github.com/<owner>/<repo>/raw/<sha>/temp-screenshots/<feature>/<name>.png)`
       -- put the 2-3 most telling shots inline, fold the rest into `<details>`.
     - Videos: GitHub does not inline-play raw blob mp4s -- add a labelled link
       line instead: `[Demo video (Ns, XMB)](https://github.com/<owner>/<repo>/raw/<sha>/temp-screenshots/<feature>/<name>.mp4)`.
   - After ANY later amend, re-pin every media URL to the new SHA.
   - Verify the body update landed (`gh api repos/<o>/<r>/pulls/<n> --jq .body | grep temp-screenshots`).
4. **Batch to minimize approval resets.** A force-push resets PR approvals --
   attach media BEFORE asking the user to approve the PR itself, and fold the
   media amend into any pending code amend rather than pushing twice.

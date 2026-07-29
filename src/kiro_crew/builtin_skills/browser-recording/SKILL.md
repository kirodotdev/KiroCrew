---
name: browser-recording
description: Record a browser flow as a video/GIF for evidence — animations, transitions, and multi-step interactions that a still screenshot cannot prove. Drives the project's own Playwright through a bundled runner, then converts to mp4 + GIF via ffmpeg. Use when the user asks to record a demo, capture a GIF or video of the UI, or when a UI change involves motion or a sequence of steps.
triggers: record, recording, screen recording, gif, record a video, video capture, demo video, record the flow, capture the animation
---

# Browser Recording — video/GIF evidence for UI flows

A still frame cannot prove motion or sequence correctness. When a UI change
involves **animation, transitions, or a multi-step flow** (wizard steps, a
modal opening, drag interactions), record it. This skill is the *how* for the
evidence rule in the `frontend-design-workflow` skill's Phase 3.

## What it does

`scripts/record_browser.py` drives a headless Chromium through the **target
project's own Playwright install**, records the session as webm, and — when
ffmpeg is available — converts it to mp4 and a palette-optimized GIF.

```
python3 <skill-dir>/scripts/record_browser.py \
  --url http://127.0.0.1:5173/settings \
  --scenario /tmp/demo-scenario.mjs \
  --project /path/to/frontend \
  --size 1280x800 --name settings-flow --out /tmp/rec
```

Last lines of stdout are machine-readable:

```
WEBM /tmp/rec/settings-flow.webm
MP4 /tmp/rec/settings-flow.mp4
GIF /tmp/rec/settings-flow.gif
```

## Workflow

1. **Get the UI reachable at a URL.** A dev server, a preview build behind a
   static server, or any live page. Starting the server is not this skill's
   job — use the project's normal dev loop (and the `web-preview` skill to
   surface it to the user).
2. **Author the scenario** — a small `.mjs` module you write per task:

   ```js
   export default async (page) => {
     await page.click('text=Open settings')
     await page.waitForSelector('[role="dialog"]')   // wait on state
     await page.click('text=Notifications')
     await page.waitForTimeout(400)                  // let the transition play
   }
   ```

   Rules: one flow per recording; wait on selectors/state, not bare
   timeouts (except to let a transition visibly finish); keep it under
   ~30 seconds of wall time.
3. **Run the recorder** (command above). `--project` must point at a
   directory whose `node_modules` has Playwright with Chromium installed.
4. **Look at the result** before presenting: read the GIF/mp4 (or key frames)
   to confirm the flow actually shows what you claim.
5. **Deliver**: embed the GIF in chat with `![what it shows](/abs/path.gif)`;
   for PRs follow the repository's screenshot-embedding convention (in this
   repo: commit under `.github/screenshots/<feature>/`, embed with
   commit-SHA-pinned `github.com/<owner>/<repo>/raw/<sha>/...` URLs). Attach
   the mp4 as a file when higher fidelity matters.

## Dependencies (probe-first, never auto-installed)

| Dependency | Required? | If missing |
|---|---|---|
| Node.js | yes | script fails with install pointer |
| Playwright in the target project | yes | script fails with `npm i -D playwright && npx playwright install chromium` |
| ffmpeg | no | webm still produced; mp4/gif skipped with a note (macOS: `brew install ffmpeg`) |

If the project has no Playwright and adding it is not acceptable, fall back
to a screenshot sequence and say explicitly that motion could not be captured.

## When NOT to use

- Static changes — a screenshot is cheaper and clearer (`web-verify` skill).
- Live interactive browsing for the user — that is the Browser panel /
  `web-browse` path, not an offline recording.

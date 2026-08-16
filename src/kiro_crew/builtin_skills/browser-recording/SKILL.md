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

## Recording evidence for GUI PRs (provenance)

`frontend-design-workflow` Phase 3 owns *which* evidence a GUI change needs: a
still screenshot for a static layout or styling change, a **recording** for
motion, a transition, or a multi-step flow a still frame cannot prove. This
section does not widen that rule — do not record a change a screenshot already
covers — it adds the one thing Phase 3 leaves open: how to make a recording
*reproducible and honest about how it was produced*.

When a change does need a recording, it must come from the **real server and
model flow**: the actual dev/preview server running the branch, driven through
a real interaction, never a fixture, a mock, or a hand-faked event stream. A
recording of a stubbed surface proves nothing about the code under review.

Embed the recording with a provenance line beside it disclosing how it was
produced:

```
recorded from <full-SHA> · <tree/origin> · mode: <flags> · real server + model, no fixtures
```

State the commit SHA, the tree or origin it came from, any mode flags that
change behaviour, and an explicit real-vs-fixture disclosure. The line is
written by the same agent that made the recording, so it discloses provenance
rather than proving it — the real check is the SHA-pinned embed a reviewer can
open and a re-record from that SHA. A recording with no provenance line leaves
how it was produced unstated.

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
4. **Verify the encoded artifact, not the source frames.** Decode
   representative frames back out of the *final GIF/mp4* you are about to
   ship — not the raw webm or an intermediate capture — and confirm the flow
   actually shows what you claim. An encode step can drop frames, crush
   colours, or truncate; only the encoded artifact is what the reviewer sees.
   When a scenario asserts a completion state, match on **exact text** through
   a precise locator (`getByText('Saved', { exact: true })`), never a
   substring — a substring predicate silently passes on the wrong element.
   While you have those frames decoded, **check them for anything the flow was
   not meant to expose** — secrets, tokens, real names or emails, other users'
   data — because publishing a recording sends real UI pixels to an
   outward-facing, often permanent place. If any is present, sanitize the
   *source* (scrub the fixture, mask the field, record against throwaway data)
   and re-record; do not crop or blur the encoded artifact, which leaves the
   data recoverable underneath.
5. **Deliver**: embed the GIF in chat with `![what it shows](/abs/path.gif)`.
   If a GIF is too large to embed, trim the scenario or lower the frame rate
   and re-record rather than shipping a blob no one can open.
   For a PR, follow the repository's own screenshot/media convention when it
   has one — many repos commit PR media on the feature branch under a known
   path so their review lanes and cleanup workflows can see it. Only when a
   repo has no such convention (and you have push access), publish the binary
   to a dedicated assets branch instead of bloating the feature branch or main
   (see *Publishing to an assets branch* below). Then embed the raw blob URL
   and attach the mp4 as a file when higher fidelity matters.

## Publishing to an assets branch

This is the **fallback for a repo that has no media convention of its own** —
prefer the repository's documented path when one exists. Recordings are
binaries. Committing them onto the feature branch — or worse, main — bloats
history permanently, since git keeps every revision of every blob forever.
When a repo offers no place for media, publish them to a throwaway **assets
branch** instead so main's history stays text-only. **Get the user's explicit OK
before creating or pushing an assets branch — on any repo, owned or not.** An
orphan assets branch is a permanent, outward-facing side effect, so it is a push
like any other, and this repo's standing rule is that committing and pushing each
require explicit user authorization and are never done proactively. On a repo you
do not own you need the owner's OK as well, since some orgs restrict branch
creation outright.

1. Shallow-clone the repo into a scratch directory and create (or check out)
   an **orphan** `<series>-assets` branch — one branch per recording series,
   holding only media, sharing no history with main.
2. Copy the encoded GIF/mp4 in, commit, and push to that branch through an
   **authenticated** remote. Never force-push an assets branch: appends only,
   so previously embedded URLs never break.
3. Embed in the PR body with the raw-blob form (`...?raw=true`) pinned to the
   commit you just pushed.
4. Verify the embed resolves: fetch the raw URL through the authenticated
   remote and confirm a 200, the expected content-type, and a byte count or
   checksum matching the local file. A GitHub-rendered Markdown preview
   confirms it displays. Re-read the PR head SHA before and after editing the
   body so the embed stays pinned to the right commit.

## State isolation

A recording is only trustworthy evidence for a specific commit if nothing
outside that commit leaked into it. Record from an isolated, throwaway
context:

- Use a **fresh scratch home / session / browser context** per recording — no
  reused cookies, cached state, or ambient config from a prior run — so the
  flow reflects the branch alone.
- **Record the exact SHA** the server is running (the same SHA that goes in
  the provenance line), and check the working tree is clean before capture so
  no uncommitted change sneaks into the frames.

This is what lets a GIF provably attribute to one commit rather than to
whatever happened to be on the machine.

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

## Related skills

- `feature-demo-recording` — a cinematic, narrated mp4 demo for Slack
  (auto-zoom, caption cards, camera polish). Use it for showing off a
  finished feature. (Ships with the dev_fleet app, not as a built-in skill,
  so it is only available where that app is installed.)
- `browser-recording` (this skill) — the GIF-for-PR evidence gate. Use it to
  prove a GUI change works, embedded in the PR from the real server flow.

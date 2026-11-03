---
name: web-verify
description: Use after a user-visible UI edit to verify your OWN change on an isolated loopback server. Capture, read and show real screenshots. Backends: the `browser` MCP tool (live in chat; origin must be listed), agent-browser (pixel diff, a11y), or scripted Playwright. Not web-preview or web-browse.
triggers: verify the ui, check my change, does it look right, screenshot my change, verify front-end, visual check, self-verify, look at the change, prove the ui works, screenshots for the pr, agent-browser, playwright screenshot
---

# Web Verify: look at your own front-end change

Tests prove the code runs; they do not prove the pixels are right. A passing
`vitest` run is compatible with a clipped label, a wrapped flex row, a blank panel
behind a gate, or an empty state that never mounts. When you change UI, **open it
and look at it**, then put the frame in chat so the user sees the same thing you
did.

This is the **self-verification** path: the screenshot is evidence, not decoration.
It is view-only (navigate plus screenshot); clicking and typing through a flow uses
the same tool with more ops. Keep the frame count bounded (see below). Restraint
here is about context cost, not permission.

## Three ways to capture, and name the one you used

| backend | how | notes |
|---|---|---|
| **`browser` MCP tool** | `browser(op="navigate", args={"url": ...})` then `browser(op="screenshot")`. It writes a PNG and returns its absolute path; read that file. | The live path: the user watches the page in the chat while you verify. It refuses a loopback URL unless that exact `host:port` is listed under **Settings → Browser → Local preview origins** (every URL in this skill is loopback), so this backend works only when the user listed your dev server's origin. A refusal means it is not listed: say so and use a row below, or ask them to add it. |
| **agent-browser** (`vercel-labs/agent-browser`) | `agent-browser open <url>` then `screenshot <path>`; `snapshot -i` for refs, `screenshot --full` for a whole page, `screenshot --annotate` for numbered element labels, `diff screenshot --baseline before.png` for a pixel diff, `a11y` for an axe-core audit. Batch a whole flow in one call with `agent-browser batch`. | A standalone Rust CLI (`npm install -g agent-browser` plus `agent-browser install`); it drives its own Chrome, so frames land on disk and do **not** appear in the chat's live view. Reach for it when it is already installed, or when you specifically want annotated frames, a baseline pixel diff, or the a11y audit. |
| **Scripted Playwright** | the `pod-e2e` runner, or a repo capture harness under `website/scripts/`, writing PNGs to a directory. | The right choice for many deterministic frames or a repeatable harness in CI, and it keeps this loop working on a host with no browser set up at all. |

All three end the same way: read the frame, judge it, embed it in chat. **Say which
backend produced the screenshots** ("live in the chat via the `browser` tool",
"captured with agent-browser", "scripted Playwright via pod-e2e") so the user
knows whether they could have watched it happen. Never imply a live view that did
not exist.

> **Keep every frame you read under 2000px on both edges.** A single image past
> that wedges the session permanently: the provider rejects the WHOLE request once
> a conversation carries many images, kiro-cli replays the full history every turn,
> and the offending block sits at a fixed history index that nothing can evict, so
> the same error re-fires on every later turn no matter what you do next. The
> `browser` tool's `screenshot` captures the viewport, which stays under the cap;
> with agent-browser prefer an element or viewport frame over a full-page one on a
> long page. If a file is already oversized, downscale it BEFORE reading it:
>
> ```bash
> python3 "${KIROCREW_HOME:-$HOME/.kiro/crew}/skills/web-verify/scripts/downscale_image.py" "/abs/path/shot.png"
> ```
>
> On native Windows, run the same script with that machine's launcher (`py` or
> `python`); the script itself is OS-agnostic. It takes paths as arguments (so a
> path with an apostrophe or a space is the shell's problem, not the script's),
> rewrites only files actually over the cap, and re-execs itself under Kiro Crew's
> own venv interpreter when the Python you invoked has no Pillow.
>
> The error is asymmetric, which is why the cap is not a nicety: downscaling only
> costs detail, while one oversized read costs the rest of the conversation.

## Precondition: a browser must actually be available

The `browser` tool answers "browsing is not set up on this host" when the engine
is not installed; `command -v agent-browser` tells you about the second row.
Either may be absent.

If **none** of the three backends is available, do NOT fake it and do NOT claim
visual verification you did not do. Say plainly that you verified the code but not
the rendering, and name the fix. Give the USER the one-click route rather than only
a command they have to paste: **Settings → Browser** installs the browser engine
on the gateway's machine (and connects their own Chrome on a desktop). The same
page holds **Local preview origins**, where they list the `host:port` your dev
server binds so the `browser` tool may open it. `agent-browser` installs with its
own commands, and scripted Playwright via `pod-e2e` needs no browser set up at
all. There is no Browser Mode setting to switch on.

The steps below describe the `browser` tool path. Steps 1, 2, 4, 5, and 6 apply
to the other two backends unchanged; only the navigate and screenshot calls differ.

## Steps

1. **Get a loopback URL serving your change.** Never the live gateway. For the
   Kiro Crew repo that means an isolated instance from the worktree you edited
   (`./dev-backend.sh`, or a pod: from an agent session call the `pod_up` MCP tool
   for a `{base_url, token}` handle, since `kirocrew pod up <worktree> --json`
   fails with `Permission denied` behind a sandbox that denies the systemd user
   bus; see the `kirocrew-worktree-dev` and `pod-e2e` skills). For a user's own
   project it is their dev server (`npm run dev`, and so
   on). Rebuild the frontend first if the server serves a built bundle, otherwise
   you will screenshot the old UI and pass it off as the new one.
2. **Confirm it is actually up** (HTTP 200/401/403) before navigating, and emit the
   `web-preview` marker so the user's Preview tab points at the same URL:
   `<!-- kirocrew:preview url="http://127.0.0.1:PORT" -->`
3. **Open it:** `browser(op="navigate", args={"url": "http://127.0.0.1:PORT/?token=…"})`.
   Include the auth token in the URL when the app needs one; a screenshot of a 403
   page verifies nothing. The result carries the URL and title, enough to confirm
   you landed on the right page. **A refusal here is the loopback gate**, not a
   broken install: the origin is not in the user's list. Say so and either ask
   them to add it or switch to another backend; never try to widen the gate.
4. **Screenshot** with `browser(op="screenshot")` and take the path it returns,
   then **read the file** and actually check it: is the surface you changed
   present, laid out, and legible? A screenshot you never looked at is not
   verification.
5. **Show it in chat**: `![what it shows](/absolute/path.png)`. State what you
   confirmed and what you could not see.
6. **Fix and re-shoot** if the frame contradicts your change. Iterate, then report
   the final state.

For an element ref, run `browser(op="snapshot")` and read the outline it returns.
Take a fresh snapshot after navigation or a click that changes the page.

## Keep it bounded

- One or two frames **per surface you changed**, not a tour of the app. Reading
  images is the expensive part of this loop.
- Prefer meaningful variants over more of the same: empty vs populated, collapsed
  vs expanded, error state, and the narrow width if layout was the bug.
- Same-surface before/after only when the *point* is the delta (a layout fix).

## Blank page? Suspect the gate, not your change

A first-run instance renders behind onboarding and prerequisite gates and starts
with empty `localStorage`, so a naive load often yields a blank or modal-covered
page. Before assuming your change broke: check the snapshot for a mounted
gate/modal, dismiss it (`browser(op="press_key", args={"key": "Escape"})`, or
click the close control), and confirm the token was accepted. The `pod-e2e`
runner pre-seeds `localStorage` and dismisses first-run modals for exactly this
reason.

## Fallback: when the `browser` tool cannot open your server

Use the installed `agent-browser` or Scripted Playwright row above. Their frames
land on disk, not in the chat; say so, then read and embed the PNGs. Capture
harnesses can miss new gates: for a blank frame, stub the gate's status endpoint
in the isolated harness rather than accepting it as evidence.

## Screenshots for the PR

The frames you captured here are the ones a user-visible UI change needs on its PR:
see the `kirocrew-prepare-pr` skill for how they get attached, and the repo's constraint
against committing binaries. Capture once, use twice.

## Not this skill

- **Showing the user an external page**: `web-browse`.
- **Just giving the user a live preview to click** with no verification of your
  own: `web-preview` (the Preview tab, no screenshot).
- **Driving a multi-step flow** (click, type, submit): the same tool, using
  `snapshot` for refs then `click` / `type` / `press_key`.
- **Backend-only changes**: tests are the evidence; do not invent a screenshot.

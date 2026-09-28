---
name: web-verify
description: Use after a user-visible UI edit to verify your OWN change on an isolated loopback server. Capture, read and show real screenshots. Backends: playwright-cli (live Browser panel), agent-browser (annotated frames, pixel diff, a11y), or scripted Playwright. Not web-preview or external-page web-browse.
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
the same CLI with more verbs. Keep the frame count bounded (see below). Restraint
here is about context cost, not permission.

## Three ways to capture, and name the one you used

Not a backend here: the **`browser` MCP tool**. It opens public `http(s)` URLs
only and refuses loopback, which is every URL in this skill — so
`playwright-cli` is the local-verification path by design, not by preference.

| backend | how | notes |
|---|---|---|
| **`playwright-cli`** | `playwright-cli open <url>` then `playwright-cli screenshot`. It prints the path it wrote; read that. The positional argument is an element **ref**, not a path, and `--filename` resolves against the CWD (so it can clobber a repo file and is not auto-approved) -- take the printed path instead of naming the file. | The panel-integrated path: the session is what the dashboard's **Browser** panel shows, so the user watches the verification instead of waiting for a summary. Prefer it when `playwright-cli` is on PATH. |
| **agent-browser** (`vercel-labs/agent-browser`) | `agent-browser open <url>` then `screenshot <path>`; `snapshot -i` for refs, `screenshot --full` for a whole page, `screenshot --annotate` for numbered element labels, `diff screenshot --baseline before.png` for a pixel diff, `a11y` for an axe-core audit. Batch a whole flow in one call with `agent-browser batch`. | A standalone Rust CLI (`npm install -g agent-browser` plus `agent-browser install`); it drives its own Chrome, so frames land on disk and do **not** appear in the Browser panel. Reach for it when it is already installed, or when you specifically want annotated frames, a baseline pixel diff, or the a11y audit. |
| **Scripted Playwright** | the `pod-e2e` runner, or a repo capture harness under `website/scripts/`, writing PNGs to a directory. | The right choice for many deterministic frames or a repeatable harness in CI, and it keeps this loop working on a host with no browser CLI at all. |

All three end the same way: read the frame, judge it, embed it in chat. **Say which
backend produced the screenshots** ("in the Browser panel session via
`playwright-cli`", "captured with agent-browser", "scripted Playwright via
pod-e2e") so the user knows whether they could have watched it happen. Never imply
a panel session that did not exist.

> **Keep every frame you read under 2000px on both edges.** A single image past
> that wedges the session permanently: the provider rejects the WHOLE request once
> a conversation carries many images, kiro-cli replays the full history every turn,
> and the offending block sits at a fixed history index that nothing can evict, so
> the same error re-fires on every later turn no matter what you do next. Capture
> at a 1400-1500px viewport (`playwright-cli resize 1440 900`) and prefer an
> element screenshot (`screenshot <ref>`) over a full-page one on a long page. If a
> file is already oversized, downscale it BEFORE reading it:
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

```bash
command -v playwright-cli    # or: command -v agent-browser
```

`playwright-cli` is a global npm install (`npm install -g @playwright/cli@latest`,
Node.js 20 or newer) and `agent-browser` is a separate CLI, so either may be
absent.

If **none** of the three backends is available, do NOT fake it and do NOT claim
visual verification you did not do. Say plainly that you verified the code but not
the rendering, and name the fix. Give the USER the one-click route rather than only
a command they have to paste: **Settings → Browser** has an Install button that
performs the global npm install and fetches a browser. The command-line remedy is
`npm install -g @playwright/cli@latest` (or `agent-browser`), and scripted
Playwright via `pod-e2e` needs no browser CLI at all. Presence of the binary is
what makes browsing available, so installing it is the whole remedy; there is no
Browser Mode setting to switch on.

The steps below describe the `playwright-cli` path. Steps 1, 2, 4, 5, and 6 apply
to the other two backends unchanged; only the navigate and screenshot calls differ.

## Steps

1. **Get a loopback URL serving your change.** Never the live gateway. For the
   Kiro Crew repo that means an isolated instance from the worktree you edited
   (`./dev-backend.sh`, or `kirocrew pod up <worktree> --json` for a
   `{base_url, token}` handle: see the `kirocrew-worktree-dev` and `pod-e2e`
   skills). For a user's own project it is their dev server (`npm run dev`, and so
   on). Rebuild the frontend first if the server serves a built bundle, otherwise
   you will screenshot the old UI and pass it off as the new one.
2. **Confirm it is actually up** (HTTP 200/401/403) before navigating, and emit the
   `web-preview` marker so the user's Browser panel points at the same URL:
   `<!-- kirocrew:preview url="http://127.0.0.1:PORT" -->`
3. **Open it:** `playwright-cli open "http://127.0.0.1:PORT/?token=…"`. Include the
   auth token in the URL when the app needs one; a screenshot of a 403 page
   verifies nothing. The command prints the page URL, the title, and a snapshot
   path, which is enough to confirm you landed on the right page without opening
   the YAML. **Expect one approval prompt here.** Navigation to loopback is not
   auto-approved: every local control plane lives there, Kiro Crew's own
   dashboard included, and driving that dashboard is how an agent would widen its
   own permissions. Approve it and carry on — the prompt is the boundary working,
   not a broken install. Everything after it (`snapshot`, `click`, `screenshot`)
   runs without prompting as usual.
4. **Screenshot** with `playwright-cli screenshot` and take the path it prints (pass a `[ref]`
   from a `snapshot` to capture one element), then **read the file** and actually
   check it: is the surface you changed present, laid out, and legible? A
   screenshot you never looked at is not verification.
5. **Show it in chat**: `![what it shows](/absolute/path.png)`. State what you
   confirmed and what you could not see.
6. **Fix and re-shoot** if the frame contradicts your change. Iterate, then report
   the final state.

`PLAYWRIGHT_CLI_SESSION` names your process's browser; bare commands use it.
A parent and its subagents normally share one session family's browser. If a
subagent may browse alongside its parent or siblings, it must choose ONE
task-specific `-s=<name>` and use it on every command, `open` / `attach` included.
Do not reuse, navigate away from, or close the user's borrowed attached browser.
Use a separate context for verification.

For an element ref, run `playwright-cli snapshot` and read its YAML. Printed paths
are relative to the command's working directory; after moving, use
`$PLAYWRIGHT_MCP_OUTPUT_DIR/<printed filename>`, never a guessed filename.
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
gate/modal, dismiss it (`playwright-cli press Escape`, or click the close
control), and confirm the token was accepted. The `pod-e2e` runner pre-seeds
`localStorage` and dismisses first-run modals for exactly this reason.

## Fallback: when `playwright-cli` is absent

Use the installed `agent-browser` or Scripted Playwright row above. Their frames
land on disk, not in the Browser panel; say so, then read and embed the PNGs.
Capture harnesses can miss new gates: for a blank frame, stub the gate's status
endpoint in the isolated harness rather than accepting it as evidence.

## Screenshots for the PR

The frames you captured here are the ones a user-visible UI change needs on its PR:
see the `prepare-pr` skill for how they get attached, and the repo's constraint
against committing binaries. Capture once, use twice.

## Not this skill

- **Showing the user an external page**: `web-browse`.
- **Just giving the user a live preview to click** with no verification of your
  own: `web-preview` (loopback iframe, no screenshot).
- **Driving a multi-step flow** (click, type, submit): the same CLI, using
  `snapshot` for refs then `click` / `fill` / `press`.
- **Backend-only changes**: tests are the evidence; do not invent a screenshot.

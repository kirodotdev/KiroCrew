---
name: web-verify
description: Look at your OWN front-end change before claiming it works -- navigate the loopback URL of a dev server or pod you started, screenshot the surface you changed, read the image to judge it, and embed it in chat. Three capture backends: Playwright MCP browser_* tools (streams into the dashboard Browser panel), the agent-browser CLI (vercel-labs, annotated frames + baseline pixel diff + a11y audit), or scripted Playwright via pod-e2e as the fallback. Use after any user-visible UI edit (component, layout, theme, empty/error state) and to produce the screenshots a PR needs. Distinct from web-preview (iframe for the user to look at) and web-browse (render an external page for the user).
triggers: verify the ui, check my change, does it look right, screenshot my change, verify front-end, visual check, self-verify, look at the change, prove the ui works, screenshots for the pr, agent-browser, playwright screenshot
---

# Web Verify — look at your own front-end change

Tests prove the code runs; they do not prove the pixels are right. A passing
`vitest` run is compatible with a clipped label, a wrapped flex row, a blank
panel behind a gate, or an empty state that never mounts. When you change UI,
**open it and look at it** — then put the frame in chat so the user sees the
same thing you did.

This is the **self-verification** path: the screenshot is evidence, not
decoration. It is view-only (navigate + screenshot); clicking and typing through
a flow uses the `browser_*` tools directly. Keep the frame count
bounded (see below) — restraint here is about context cost, not permission.

## Three ways to capture — name the one you used

| backend | how | notes |
|---|---|---|
| **Playwright MCP** (`browser_*`) | `browser_navigate` + `browser_take_screenshot`. | The panel-integrated path: frames **stream live into the dashboard's Browser panel**, so the user watches the verification instead of waiting for a summary. Prefer it when the `browser_*` tools are in your tool list. |
| **agent-browser** (`vercel-labs/agent-browser`) | `agent-browser open <url>` → `screenshot <path>`; `snapshot -i` for refs, `screenshot --annotate` for numbered element labels, `diff screenshot --baseline before.png` for a pixel diff, `a11y` for an axe-core audit. Batch a whole flow in one call with `agent-browser batch`. | A standalone Rust CLI (`npm install -g agent-browser` + `agent-browser install`); it drives its own Chrome, so screenshots land on disk and do **not** appear in the Browser panel. Reach for it when it's already installed, or when you specifically want annotated frames, a baseline pixel diff, or the a11y audit. It also ships its own MCP server (`agent-browser mcp`) if the user wires it up. |
| **Scripted Playwright** | the `pod-e2e` runner, or a repo capture harness under `website/scripts/`, writing PNGs to a directory. | The fallback that keeps this loop working with no MCP browser at all, and the right choice for many deterministic frames or a repeatable harness in CI. |

All three end the same way: read the frame, judge it, embed it in chat. **Say which
backend produced the screenshots** — "streamed into the Browser panel via Playwright
MCP", "captured with agent-browser", "scripted Playwright via pod-e2e" — so the user
knows whether they could have watched it happen, and never imply a panel stream that
didn't occur.

## Precondition — a browser must actually be available (the guard)

`browser_navigate` / `browser_take_screenshot` come from the external
`@playwright/mcp` package, and `agent-browser` is a separate CLI install. Either
may be absent.

- Check before you commit to a path: are the `browser_*` tools in your tool list?
  Is `agent-browser` on `PATH` (`command -v agent-browser`)?
- If **none** of the three backends is available, do NOT fake it and do NOT claim
  visual verification you didn't do. Say plainly that you verified the code but
  not the rendering, and point at the cheapest fix: `kirocrew browse setup` for
  the Playwright MCP browser, or `npm install -g agent-browser && agent-browser
  install` for the CLI.
- The steps below describe the **Playwright MCP** path. Steps 1, 2, 4, 5 and 6
  apply to the other two backends unchanged — only the navigate/screenshot calls
  differ (`agent-browser open <url>` + `agent-browser screenshot <path>`).

## Steps

1. **Get a loopback URL serving your change.** Never the live gateway. For the
   KiroCrew repo that means an isolated instance from the worktree you edited
   (`./dev-backend.sh`, or `kirocrew pod up <worktree> --json` for a
   `{base_url, token}` handle — see the `kirocrew-worktree-dev` and `pod-e2e`
   skills). For a user's own project it's their dev server (`npm run dev`, …).
   Rebuild the frontend first if the server serves a built bundle — otherwise
   you will screenshot the old UI and pass it off as the new one.
2. **Confirm it's actually up** (HTTP 200/401/403) before navigating, and emit
   the `web-preview` marker so the user's Browser panel points at the same URL:
   `<!-- kirocrew:preview url="http://127.0.0.1:PORT" -->`
3. **Navigate** with `browser_navigate` (`waitUntil: "domcontentloaded"` for
   SPAs). Include the auth token in the URL when the app needs one
   (`http://127.0.0.1:PORT/?token=…`) — a screenshot of a 403 page verifies
   nothing.
4. **Screenshot** with `browser_take_screenshot`, then **`Read` the file** and
   actually check it: is the surface you changed present, laid out, and legible?
   A screenshot you never looked at is not verification.
5. **Show it in chat**: `![what it shows](/absolute/path.png)`. State what you
   confirmed and what you could not see.
6. **Fix and re-shoot** if the frame contradicts your change. Iterate, then
   report the final state.

## Keep it bounded

- One or two frames **per surface you changed** — not a tour of the app.
  Reading images is the expensive part of this loop.
- Prefer the meaningful variants over more of the same: empty vs populated,
  collapsed vs expanded, error state, and the narrow width if layout was the
  bug.
- Same-surface before/after only when the *point* is the delta (a layout fix).

## Blank page? Suspect the gate, not your change

A first-run instance renders behind onboarding/prerequisite gates and starts
with empty `localStorage`, so a naive load often yields a blank or modal-covered
page. Before assuming your change broke: check for a mounted gate/modal, dismiss
it (Escape / the close control), and confirm the token was accepted. The
`pod-e2e` runner pre-seeds `localStorage` and dismisses first-run modals for
exactly this reason.

## Fallback — when the Playwright MCP browser is absent

Keep going; don't skip verification. Two ways to still get real frames:

- **`agent-browser`** if it's installed: `agent-browser open "http://127.0.0.1:PORT/?token=…"`
  then `agent-browser screenshot /tmp/<name>.png` (add `--full` for the whole
  page, `--annotate` for numbered element labels). `agent-browser batch` runs the
  whole open→wait→screenshot sequence in one call, and `diff screenshot
  --baseline <before>.png` gives a pixel diff when you are proving a layout fix.
  Frames land on disk, not in the Browser panel — say so.
- **Scripted Playwright**: the `pod-e2e` skill boots an isolated pod and captures
  screenshots into an artifact dir, and repos may carry their own harness under
  `website/scripts/`.

Read the resulting PNGs and embed them the same way. Capture harnesses go stale
when new gates land upstream — if a frame comes back blank, stub the gate's
status endpoint rather than trusting the blank frame.

## Screenshots for the PR

The frames you captured here are the ones a user-visible UI change needs on its
PR — see the `prepare-pr` skill for how they get attached (and the repo's
constraint against committing binaries). Capture once, use twice.

## Not this skill

- **Showing the user an external page** → `web-browse` (Playwright, public URL).
- **Just giving the user a live preview to click** with no verification of your
  own → `web-preview` (loopback iframe, no screenshot).
- **Driving a multi-step flow** (click, type, submit) → use the `browser_*`
  tools directly; they are present whenever Browser Mode is on.
- **Backend-only changes** → tests are the evidence; don't invent a screenshot.

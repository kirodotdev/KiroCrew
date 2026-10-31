---
name: web-preview
description: Emit a hidden preview marker so the right-panel Preview tab opens a Load-preview card for a local server URL (local only; the agent does not drive it). Use whenever you start a dev or static server the user should see (Vite, Next, `npm run dev`, `python -m http.server`, etc.).
triggers: preview, live preview, dev server, serve, http.server, npm run dev, run the site, run the app, view in browser, see the site, localhost
---

# Web Preview marker

Kiro Crew's chat right-side panel has a **Preview** tab (+ menu → Preview)
that embeds a LOCAL URL in a live iframe. It is the user's own preview surface:
loopback only, no agent driving, kept for front-end work. When you start a local web server so the user
can preview a site/app, tell the dashboard which URL to load by emitting a single
hidden marker in your reply.

## The marker

Emit exactly this, on its own line, once the server is confirmed listening:

```
<!-- kirocrew:preview url="http://127.0.0.1:PORT" -->
```

- It's an **HTML comment**, so the user never sees it in the **dashboard** chat.
  Only the dashboard has a Preview tab: in a Slack, Discord or other channel
  conversation the marker is not stripped and reaches the user as literal text,
  so do not emit it there — give the URL in prose instead.
  The dashboard opens the Preview tab and shows a **Load preview** card for that
  URL, contextual to the current session. Nothing is fetched until the user
  clicks Load, so tell them to click it rather than saying the preview is
  already showing.
- Use the **actual URL the server binds to**, including the real port
  (`http://localhost:5173`, `http://127.0.0.1:8080`, …). Only loopback is
  usable: a non-loopback marker is dropped **silently** — you get no error, the
  panel just stays empty — so emit only `127.0.0.1`, `localhost` or `[::1]`. A
  URL on the dashboard's own loopback port is refused too (it would frame the
  dashboard), so serve previews on a different port.
- The tab opens only the origins listed under **Settings → Browser → Local
  preview** (exact `host:port` entries). An unlisted
  loopback address shows a one-click **Allow** in the tab itself, which writes
  the list; tell the user to click it the first time a new dev-server port is
  used. You cannot write that list: config is read-only inside the sandbox.
- The panel may rewrite the host it loads: `[::1]` becomes `127.0.0.1`, and
  `localhost` and `127.0.0.1` are swapped against each other for cookie
  isolation. Same server, different spelling — do not read the difference as a
  failure.
- Emit it **after** you've confirmed the server is up (e.g. it returned HTTP 200),
  not before.
- If you **restart on a different port**, emit a new marker with the new URL — the
  panel re-points to the newest one. Re-emitting the *same* URL does nothing (it
  is applied once per session), so if the user closed the tab, point them back to
  it instead of re-emitting.

## When NOT to emit it

- When you're only *discussing* previews, explaining this feature, or showing an
  example URL — emit it solely when a server is genuinely running for the user to
  view.
- You don't need it for public/remote URLs the user opens themselves; it's for
  the embedded local preview.

## Fallback

If you forget the marker but mention a localhost URL in prose (e.g. "serving at
http://localhost:5173"), the dashboard picks up the newest one as an **offer
only**: it pre-fills the panel without opening the tab and without fetching
anything, and only when that session has no preview target yet. The marker is
what opens the tab, so emit it.

## The marker is for the user — verify it yourself too

The marker only points the user's panel at the URL; it gives you no pixels. If
the server is showing a front-end change **you** just made, also run the
`web-verify` skill: open the same loopback URL (the `browser` tool, when the
user listed that origin, or another capture backend), screenshot the surface
you changed, read the frame, and embed it in chat. Don't hand over a preview
and call the change verified.

## Example

After starting a static server:

> The server is up and serving HTTP 200.
> <!-- kirocrew:preview url="http://127.0.0.1:8080" -->
> Click **Load preview** in the **Preview** tab of the right panel to view it.

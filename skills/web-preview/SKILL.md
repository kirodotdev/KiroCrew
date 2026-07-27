---
name: web-preview
description: Emit a hidden preview marker so KiroCrew's right-panel "Web Preview" tab auto-opens at the URL when you start a local web server for the user to preview. Use whenever you start a dev server or static server so the user can see a site/app you're working on (Vite, Next, `npm run dev`, `python -m http.server`, etc.).
triggers: preview, live preview, dev server, serve, http.server, npm run dev, run the site, run the app, view in browser, see the site, localhost
---

# Web Preview marker

KiroCrew's chat right-side panel has a **Web Preview** tab (+ menu → Web Preview)
that embeds a URL in a live iframe. When you start a local web server so the user
can preview a site/app, tell the dashboard which URL to load by emitting a single
hidden marker in your reply.

## The marker

Emit exactly this, on its own line, once the server is confirmed listening:

```
<!-- kirocrew:preview url="http://127.0.0.1:PORT" -->
```

- It's an **HTML comment**, so the user never sees it in the rendered message —
  the dashboard parses it and auto-opens the Web Preview tab at that URL,
  contextual to the current session.
- Use the **actual URL the server binds to**, including the real port
  (`http://localhost:5173`, `http://127.0.0.1:8080`, …). Prefer `127.0.0.1` /
  `localhost` — only loopback URLs are embeddable.
- Emit it **after** you've confirmed the server is up (e.g. it returned HTTP 200),
  not before.
- If you **restart on a different port**, emit a new marker with the new URL — the
  panel re-points to the newest one.

## When NOT to emit it

- When you're only *discussing* previews, explaining this feature, or showing an
  example URL — emit it solely when a server is genuinely running for the user to
  view.
- You don't need it for public/remote URLs the user opens themselves; it's for
  the embedded local preview.

## Fallback

If you forget the marker but mention a localhost URL in prose (e.g. "serving at
http://localhost:5173"), the dashboard will still try to pick up the newest such
URL — but the marker is precise, so prefer it.

## Example

After starting a static server:

> Preview is live and serving HTTP 200.
> <!-- kirocrew:preview url="http://127.0.0.1:8080" -->
> Open the **Web Preview** tab in the right panel to view it.

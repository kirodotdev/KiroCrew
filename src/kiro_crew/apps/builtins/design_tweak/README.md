# Design Tweak

*(app id: `design-tweak` — a bundled builtin, enabled from the Apps page)*

**Point, describe, and watch the code catch up.**

Visually select elements in a live preview of your web app and turn them into
scoped, source-mapped edit requests for the agent — like Figma comments, for
code.

For **designers and front-end engineers** iterating on UI: switch the preview to
Edit mode, right-click an element, type "make this sticky on scroll", and the
agent edits the exact source file — no describing "the button in the top nav,
second from the right".

---

## Features

- **Multi-app workspace** — register any number of local web app folders
  (`+ load new app`). On macOS this opens the native folder chooser; elsewhere,
  or if the chooser is unavailable, you type the path instead. All registered
  folders are served simultaneously by the app's backend at per-project URLs, so
  switching apps in the dropdown is instant.
- **Static sites and framework apps** — a folder that can be served from disk is
  previewed immediately. One whose entry script is TypeScript/JSX cannot be
  (the browser can't run it), so the preview says so and points at the project's
  own dev server; the app can start it, or adopt one you already started, and
  frames it through an injecting proxy so select-to-edit and hot reload both keep
  working.
- **One app, one body of work** — requests, history and the agent's chat session
  are scoped per web app. Switching apps shows only that app's requests, numbered
  from 1 in its own sequence, and its edit requests land in a chat session
  dedicated to that folder — so two apps never cross-influence each other.
- **Batched edit requests** — in Edit mode, right-click an element and type a
  comment. It joins the current **request** as a numbered sub-item (`3.1`, `3.2`,
  …) instead of firing immediately. Keep commenting, then send the whole batch as
  one request. Each comment carries `projectRoot` and `sourceFile`, so the agent
  edits the right file without searching — and a batch may span several pages.
- **Pins that outlive their element** — the pin resolves through a chain, so it
  is never silently dropped: `[data-kiro-cid]` (stamped by the agent on an
  element it created) → the CSS locator captured at comment time → the removed
  element's former parent → the click point → the page's bottom-left. A pin that
  isn't on its own element is drawn dashed and says why in its tooltip.
- **Per-comment progress** — every comment has its own status dot
  (`new` → `in progress` → `done`) and its own thread bubble, in the left panel
  and on its in-preview pin. A request's status is *derived* from its comments,
  never stored.
- **Seal-on-send** — sending closes a request for good. The next comment opens a
  fresh request even while the previous batch is still being worked, so a late
  thought is never appended to something the agent already has.
- **Linked follow-ups** — replying on a comment's pin creates a new comment in
  the *current* draft, linked to the original via `followUpTo` and shown as a
  follow-up to `3.1`. Finished requests are never mutated.
- **Nested left panel** — requests are collapsible groups with their comments
  indented beneath a connector line, matching the host's Sessions folder view.
- **History** — archived requests collect in a History section, comments intact.
- **Preview controls** — a per-app **Dimensions** preset (Desktop / Tablet 768px /
  Mobile 390px) and the **Preview | Edit** mode switcher. The preview reloads
  itself each time a comment flips to `done`.
- **Fully themed** — every color derives from the host's theme tokens
  (`--accent`, `--accent-fg`, `--panel`, `--border`, …), including the selection
  overlay inside the preview.

## Use it

1. Open **Design Tweak** in the dashboard sidebar.
2. Click the app dropdown → **+ load new app** → pick your web app's folder (or
   type its path). It previews immediately.
3. Flip the mode switcher to **Edit**, then **right-click** any element in the
   preview → type the change → **Enter**. It lands in the left panel as a
   sub-item of the current request (`3.1`), *not* sent yet.
4. Repeat for as many changes as you want — they collect under the same request.
5. Send the batch as one request. The agent works the comments one at a time and
   each comment's dot turns green as it lands; the preview reloads on every
   completion.
6. The request is now sealed — your next comment starts a new one, even if the
   previous is still running. Reply on a pin to file a linked follow-up.

Static HTML/CSS/JS folders work out of the box. A project whose entry point isn't
a top-level `index.html` is handled too — `public/`, `dist/`, `build/`, `out/`,
`app/`, `src/`, `site/`, `www/` and `docs/` are tried automatically, and if there
is no entry at all the preview explains what it found instead of failing blank.

**Framework projects (Vite, React Router, Next, …) are detected, not fumbled.**
Their entry script is TypeScript/JSX, so serving the files from disk renders an
empty page. Design Tweak recognises that and uses the project's own dev server
instead: it runs the project's dev script (`npm run dev`, or the pnpm/yarn/bun
equivalent from its lockfile), finds the port the tool chose rather than forcing
one, and frames it **through an injecting proxy** that adds the select-to-edit
overlay to the HTML and relays the hot-reload WebSocket as raw bytes. A dev
server you started yourself is adopted rather than duplicated, and stopping only
ever kills a server Design Tweak started.

That proxy maps paths **1:1 on its own port** rather than sitting under a
`/proxy/<id>/` prefix. A dev server's HTML refers to root-absolute URLs
(`/src/main.tsx`, `/@vite/client`) and its client builds more at runtime; behind
a path prefix every one of them would miss. Identity mapping means nothing needs
rewriting except the one injected script tag.

## How it works

| Concern | Implementation |
|---|---|
| Preview (static) | An ephemeral loopback server on its own OS-assigned port serves each registered folder, injecting a `<base href>` pointing at the served file's own directory, plus the overlay. It is a **different origin** from the dashboard (ports separate origins under the same-origin policy), which is what makes the sandboxed frame safe |
| Preview (dev server) | An injecting reverse proxy on its own ephemeral port, mapping paths 1:1; HTML gains the overlay, WebSocket upgrades are relayed as raw bytes so HMR survives. Its port is resolved live, never persisted — it dies with the backend |
| Dev-server discovery | `lsof` maps a listening port → pid → working directory, so an already-running server is matched back to its project folder |
| Selection overlay | `inject/select-to-edit.js`, auto-injected into served HTML — no manual wiring |
| Pin anchoring | A chain, best first: `[data-kiro-cid]` → the captured CSS locator → the element's former parent → the click point → page bottom-left. It never fails, so a pin is never deleted for failing to resolve |
| Panel ↔ overlay bridge | `window.postMessage` both ways (comments up; mode + theme colors down). A pin's id **is** its comment's `cid` |
| Request model | One queue file per *request*, holding many comments as sub-items. Comment statuses are authoritative; the request's status is derived from them |
| Per-app scoping | Each request carries `projectId` + `projectRoot`; the panel shows only the previewed app's requests, and numbering is derived per project (not a global counter) |
| Agent delivery target | One chat slot per app folder, keyed by a hash of its path — created idempotently, so a request can never open a second session |
| Source mapping | `projectRoot` per request + `sourceFile` per comment, stamped from the serving path; where an element carries `data-kiro-source="file:line:col"` the agent gets a high-confidence target, otherwise it falls back to React Fiber `_debugSource` (medium) or HTML-snippet matching (low) and verifies before editing |
| Delivery into agent | The batch is queued as JSON in the app's data dir and handed to that app's chat session; the bundled `visual-edit` skill teaches the agent to work a batch and report per comment via `POST /thread?id=…&cid=…` |
| Node toolchain | The gateway spawns the backend with a minimal PATH, so `npm` is resolved by absolute path from a list of known install dirs (homebrew, MacPorts, volta, bun, asdf, fnm, nvm, `/usr/local/bin`, `/usr/bin`) and that dir is put on the child's PATH |

## Structure

```
design_tweak/
├── app.json                       ← manifest (UI page + backend + skill + perms)
├── backend/server.py              ← project registry, multi-root static serving,
│                                    dev-server detect/start/proxy, request queue,
│                                    folder picker, per-comment threads
├── inject/select-to-edit.js       ← selection overlay (auto-injected into previews)
├── skills/visual-edit/SKILL.md    ← teaches the agent to work a batch + report per comment
└── README.md
```

The dashboard page is compiled into the host bundle rather than shipped here:
`website/src/apps/design-tweak/` (`DesignTweakPage.tsx`, `api.ts`, `types.ts`),
registered in `website/src/apps/builtinRegistry.ts`.

Request/queue state lives in the app's data dir,
`~/.kiro/crew/apps/design-tweak/data/` — see `skills/visual-edit/SKILL.md` for
the request schema and the reporting protocol.

## Platform notes

macOS and Linux. The only macOS-specific piece is the **native folder chooser**
(`POST /pick-folder`, AppleScript): on any other platform it answers `501` and
the panel falls back to typing the folder path, which registers a project
identically. Dev-server discovery needs `lsof` on PATH; without it, detection is
skipped and you can point a project at a dev-server URL yourself.

## Coming from the external `poke-and-prose` app

This app previously shipped as an externally-installed app from its own repo. If
you installed that version, the builtin **stands down** rather than taking your
install over — `register_builtin_apps()` refuses to touch an app directory it
did not write (see `_builtin_owns_install()`), because that directory holds your
own state, not ours.

To move to the builtin, uninstall the external app once from **Apps → Library →
Uninstall**. That is deliberately all it takes: `uninstall_app()` keeps `data/`
by default, so the app directory is left holding your queue and history, and the
builtin registers into that same directory on the next gateway start and picks
them up. Enable it from the Apps page afterwards.

There is intentionally **no automatic takeover**. Doing it for you would mean
moving a user-owned app directory during gateway startup, which is a large
failure surface (crash-interrupted renames, symlinked `data/`, enabled-state
carry-over) for an audience of users who already have the external app — a
two-click manual path costs them almost nothing and costs everyone else no
startup risk at all.


---
title: App contribution points on one sandboxed frame model
status: draft
author: buluoray, with kirocrew-worker
created: 2026-10-30
last-audited: 2026-10-31
audited-at: 4537582b9b
doc-pr:
implementation-prs: [26479]
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: App contribution points on one sandboxed frame model

- Status: draft. The sandboxed frame, its capability, its content security
  policy, the link-preview grant families and the text-pattern budget shipped in
  [#26479](https://github.com/kirodotdev/KiroCrew/pull/26479); every passage
  that describes them is marked **Shipped in #26479**. Everything else is a
  proposal. The repository owner's decisions on the scope and on the questions in
  §13 are recorded there; the status flips to `accepted` when the document merges
  with them.
- Builds on: `contributes.linkPreviews` from #26479, which runs an app's hover
  card in a cross-origin sandboxed frame. This document generalizes that frame and
  its message protocol into the one model every frame-backed point uses.
- Related: [rfc-app-session-controls.md](rfc-app-session-controls.md) (the last
  composer seam, and the "field and reader land together" rule this document
  keeps), [rfc-app-sandbox-isolation.md](rfc-app-sandbox-isolation.md) (app-token
  scope), `docs/system-specs/modules/app-kit-platform.md` (manifest validation,
  approved grants, `permissions.mcpTools`), `docs/app-kit/manifest-reference.md`
  (the manifest contract this extends, including `contributes.linkPreviews`).

## Summary

Version 1 is three things:

1. **One hardened frame model** (§2). The frame, its capability token and its
   content security policy are the ones #26479 shipped for link previews. This
   document adds the parts a second point needs: a point name and feature
   negotiation in the handshake, a cancel and an acknowledged-effect step in each
   invocation, host-owned Copy and Open buttons, and a backend fetch helper.
2. **Semantic grants** (§2.7). What the owner approves is what a point can see,
   not just its id, and any widening waits for approval. #26479 shipped this for
   link previews (hosts, text patterns, `fullUrl`); this document applies the same
   rule to `messageActions`.
3. **One new point, `messageActions`** (§3): a button on one chat message that
   hands the app that message's text.

`linkPreviews` is restated on the same model (§4). `selectionActions`,
`composerActions` and `linkOpen` are designed but deferred (§6).

The next major direction is not more UI. It is **agent-callable app tools**
(`contributes.agentTools`, §7): an agent in a crew or session calls an app's
tool, built on the app's existing `mcpServers`, with host-enforced grants. After
that come context providers and message cards, and event handlers (§8).

| Point | State | Trigger | The app gets |
|---|---|---|---|
| `linkPreviews` | shipped (#26479) | Hover on a claimed link or matched text | That URL (query and fragment removed unless `fullUrl` is approved) or text |
| `messageActions` | v1 proposal | Click on the app's button on one message | That message's text |
| `selectionActions` | deferred | Pick an item on a selection | The selected text |
| `composerActions` | deferred | Click in the composer | Nothing; returns text to insert |
| `linkOpen` | deferred | Click on a claimed link | That URL |

The iframe is a presentation boundary. It is not the execution model for tools
or automation; those run in the app's backend under host grants (§7, §8).

## 1. Problem

Main has five points (`src/kiro_crew/apps/manifest_contributes.py`):
`commands`, `panelTabs`, `fileMenuItems`, `sessionControls` and, since #26479,
`linkPreviews`. The first four let an app add a launcher row, a side-panel tab, a
file-menu row or a composer chip. Only `linkPreviews` lets an app see anything in
the conversation, and only a link the pointer rests on. An app that wants "save
this answer" has two options today, and both are bad:

1. Ask for broad access. With `permissions.events` (`slots:own`) and
   `permissions.api` an app page can read transcripts, but only for its own
   slots, and the grant reads as "this app can see my chats". The job needs one
   message the user pointed at.
2. Run in the dashboard page. The in-process path (`ui.pages`) runs app code in
   the dashboard origin, which the manifest reference says is not a sandbox: the
   module can read the page and call any route with the user's session.

#26479 solved this for hover cards with a sandboxed frame. This document fixes
that frame as the shared model, adds what a click-driven point needs, and proves
it with the smallest deliberate disclosure: one message, on one click.

## 2. Shared model

### 2.1 Trust boundaries

The frame isolates **browser state**. It does not, by itself, limit what the
app's backend can do. Three trust zones stay separate and this document changes
only the first:

| Zone | Runs | Bounded by |
|---|---|---|
| Frame | App UI for a point | Sandbox, CSP, frame capability (§2.2, §2.3) |
| App backend | App's hook routes or backend process | `apps_allow_third_party` / `apps_trusted`, `permissions.api`, its own code |
| In-process pages | `ui.pages`, `panelTabs`, `sessionControls` | Nothing beyond admission; they share the dashboard origin |

An app admitted to run a backend process can already reach the filesystem and
network as the user. A frame does not make that app safe; it makes sure the
**UI for these points** cannot reach the dashboard session.

### 2.2 The frame

**Shipped in #26479** (`website/src/linkPreview/linkPreviewFrames.ts`,
`src/kiro_crew/apps/link_preview_frame.py`):

- **Element.** `sandbox="allow-scripts"` and nothing else, so the origin is
  opaque; `credentialless`, `referrerpolicy="no-referrer"`, and an empty `allow`
  (no feature policy at all). The frame's title is the app's name.
- **Document.** The dashboard asks `POST /api/apps/<app>/link-preview` (dashboard
  sessions only; an app token, an internal caller or a link token is refused) for
  a frame URL and a capability. The frame document is served at
  `/link-preview-frame/<app>/<frame_id>/<token>`, where the HMAC path token is the
  credential, with `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: no-referrer` and a `<meta name="referrer"
  content="no-referrer">`.
- **Content security policy** of that document (`frame_csp`):

  ```text
  sandbox allow-scripts;
  default-src 'none';
  script-src 'self' 'nonce-<per-response nonce>';
  style-src 'self' 'unsafe-inline';
  img-src 'self' https: data: blob:;
  font-src 'self' data:;
  connect-src 'self';
  media-src 'none'; worker-src 'none'; frame-src 'none'; child-src 'none';
  manifest-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none';
  frame-ancestors <the dashboard's frame ancestors>
  ```

  The `sandbox` directive keeps the document opaque even when it is opened
  top-level. `'self'` is the gateway, so scripts and network stay on it.
- **Self-navigation.** A frame that navigates away from its card document is
  closed and logged.
- **Lifetime.** The module must say `ready` within 10 seconds or the frame
  failed; a failed provider is retried no sooner than 30 seconds later. One hover
  frame per provider, plus a frame per pinned card (at most 8 pinned; pinning
  one more closes the oldest). Every frame is disposed when the app is disabled.

**Proposed for v1:**

- The mint route and the frame route are generalized from link previews to every
  frame-backed point: the mint request names the point and point id, and the
  frame document imports that entry.
- `img-src` stays `https:` for `linkPreviews`, whose cards show remote avatars and
  icons from the link's own site. A frame for a point that is handed message text
  (`messageActions`) gets `img-src 'self' data: blob:` instead, because an
  `https:` image request is a way to send that text to any host.
- A frame for a click is created on the click and disposed when its popover
  closes.
- Revoking a point's grant disposes its live frames at once (§2.7).

### 2.3 Frame requests and app routes

**Shipped in #26479:**

- **Capability.** The mint route returns a capability bound to the app, the
  requesting client address and that frame's id. It lives **15 minutes**
  (`_CAPABILITY_TTL_SECS = 900`; the frame token has the same lifetime). The
  signers are per process, so a gateway restart invalidates every capability and
  the dashboard mints again. The dashboard renews it with a `capability` message;
  a frame whose capability was refused sends `capabilityExpired`.
- **Header only.** The frame's `ctx.fetch` sends the capability in the
  `X-KiroCrew-Capability` header with `credentials: 'omit'`. No other route, and
  not the auth middleware, accepts it.
- **Own backend only.** Frame requests go to `/link-preview-fetch/<app>/<path>`,
  which reaches exactly the app's own backend: the process backend through the
  same signed reverse proxy as `/apps/<app>/api/`, or the routes the app
  registered through `backend.routes`. Core routes under `/api/apps/<app>/`
  (config, update, enable) are not reachable. Methods are `GET`, `POST`, `PUT`,
  `PATCH` and `DELETE`; a path with `..`, a leading `/` or `\` is refused.
- **`Origin: null`.** CORS headers (`Access-Control-Allow-Origin: null`, no
  `Allow-Credentials`, `Vary: Origin`) are added only to a preflight, which grants
  nothing, and to a request whose capability verified. A refusal carries none, so
  the opaque origin cannot read it. The route reads no cookie, which is why it is
  exempt from the CSRF Origin check.
- **Audit.** Each granted fetch is audited as `link_preview_fetch` for that app.

**Proposed for v1:**

- **Method and resource authorization.** The capability also names the point,
  the point id and the current invocation. The fetch route passes them to the app
  as headers it writes itself (`X-KiroCrew-Frame-Point`,
  `X-KiroCrew-Frame-Invocation`), stripping any the frame sent. The frame SDK's
  route helper refuses a write method unless the route declares the point it
  serves, so reaching the app's prefix is never the whole authorization.
- **Revocation.** Revoking a point's grant makes its live capability fail on its
  next use, not only stop renewing.

### 2.4 Backend fetch destinations

Proposed. The frame's CSP stops the frame itself from reaching other hosts, but
an app backend that fetches a URL it was handed is a server-side request forger
unless it checks the destination, and no browser-side hostname check can prevent
that. The host therefore offers, and the manifest reference requires app backends
to use, one fetch helper for any URL that came from a point:

- Only `https:`; default port only; no userinfo.
- The destination host must match the point's approved `hosts` after the
  normalization in §2.8.
- DNS is resolved once, and the request goes to the resolved address. Loopback,
  link-local, private (RFC 1918, unique local IPv6), CGNAT, multicast and
  metadata addresses are refused.
- Every redirect is re-checked against all of the above, at most 5 hops.
- Response size and time are capped (2 MB, 10 s by default).

An app backend can still ignore the helper; §2.1 says what that backend is
trusted with. The helper makes the safe path the easy one, and an official
registry review checks for it.

### 2.5 The protocol

**Shipped in #26479** (`website/src/linkPreview/linkPreviewProtocol.ts`):

Dashboard and frame talk over `postMessage`. Every message is
`{ "kirocrew-link-preview": 1, type, ...fields }`; the frame-side reader
(`readFrameMessage`) drops anything else.

| Direction | `type` | Fields |
|---|---|---|
| dashboard → frame | `init` | `capability`, `theme` |
| dashboard → frame | `capability` | `capability` (renewed) |
| dashboard → frame | `show` | `invocation`, `url` or `text`, `rect`, `theme`, `modifiers` |
| dashboard → frame | `leave` | none |
| dashboard → frame | `hide` | `reason` |
| dashboard → frame | `theme` | `theme` |
| dashboard → frame | `dispose` | none |
| dashboard → frame | `key` | `key`, `code`, `shift` |
| frame → dashboard | `ready` | none |
| frame → dashboard | `failed` | `message` (cut to 200 characters) |
| frame → dashboard | `open`, `close` | `invocation` |
| frame → dashboard | `resize` | `invocation`, `width`, `height` |
| frame → dashboard | `pin` | `invocation`, `pinned` |
| frame → dashboard | `drag` | `invocation`, `dx`, `dy` |
| frame → dashboard | `dock` | `invocation`, `edge` (`left`, `right` or `null`) |
| frame → dashboard | `textPatternsOff` | `ids` (at most 64) |
| frame → dashboard | `escape`, `capabilityExpired` | none |
| frame → dashboard | `regions` | `rects` (at most 32) |

- `invocation` is an integer the dashboard assigns to each `show`; a card message
  for an older invocation is dropped.
- `key` is sent only while a card shows, never while the dashboard's focus is in
  a text field, and never with Ctrl, Alt or Meta held.
- Numbers are coerced to finite values; `rect` is
  `{ left, top, width, height }` in viewport CSS px.
- A frame message larger than 8,192 bytes as JSON, or past the frame's burst
  allowance (120 messages, refilled at 60 per second), is dropped unread.
- The dashboard positions, sizes and clips the frame itself. No frame message
  reaches the dashboard's DOM.

**Proposed for v1.** The envelope is generalized and extended, keeping every
shipped message:

- **Envelope.** `{ "kirocrew-app-frame": 1, type, ...fields }` for every point.
  The dashboard keeps accepting and sending `kirocrew-link-preview` to link-preview
  frames for the support window in §2.10.
- **Handshake.** `init` gains `point`, `pointId`, `versions` (protocol majors the
  host speaks, newest first), `features` (for example `message.v1`, `offer.v1`),
  `hostVersion` and `locale`. `ready` gains `v` and `features`: the frame names
  the major it speaks and the features it uses, and the host enables only those.
- **Invocations.** Every user act opens one invocation:

  ```text
  host:  show | invoke (invocation, deadline) ─▶ OPEN
  frame: offer (invocation) ──────────────────▶ host checks: current, OPEN, first of its kind
  host:  offerAck | offerRefused ─────────────▶ frame shows the right state
  host:  hide (invocation, reason) ───────────▶ CLOSED  (popover closed, deadline passed,
                                                         session switched, revoked, disabled)
  ```

  `invocation` stays host-assigned. `hide` gains `invocation` and is the cancel:
  its `reason` set grows by `deadline`, `session-switched` and `revoked`. An
  `offer` for a closed or unknown invocation, a second offer of the same kind, or
  one that arrives out of order is answered with `offerRefused { code: "stale" }`
  and has no effect. Each operation sets its own deadline (§3, §4).
- **New messages.**

  | Direction | `type` | Fields |
  |---|---|---|
  | host → frame | `invoke` | `invocation`, point-specific fields (§3) |
  | frame → host | `offer` | `invocation`, `kind` (`copy` or `open`), `value` |
  | host → frame | `offerAck` | `invocation`, `kind` |
  | host → frame | `offerRefused` | `invocation`, `kind`, `code`: `stale`, `too_large`, `not_allowed` |

- **Values.** Messages carry JSON-shaped values only (objects, arrays, strings,
  finite numbers, booleans, `null`), nested at most 16 deep; anything else is
  dropped. The 8,192-byte cap stays for every message except `offer`, which may
  be up to 64 KB. A frame that sends more than 512 KB in 10 seconds is disposed.
- **Text.** Text the host shows from a frame is plain text. The host strips C0
  controls other than tab and newline, and the bidirectional override and isolate
  characters (U+202A to U+202E, U+2066 to U+2069).

### 2.6 Host-owned effects

Proposed. A frame never writes the clipboard or opens a window itself; the
shipped empty `allow` and sandbox already deny both. It **offers** a value, and
the host renders the button that acts on it, in host chrome outside the frame:

| `kind` | `value` | Host button | Rule |
|---|---|---|---|
| `copy` | text, at most 32,000 UTF-16 units | **Copy** | Writes the clipboard only on the user's click on that button |
| `open` | an `https:` URL | **Open** | Opens in a new tab with `noopener,noreferrer`; shows the host name on the button |

One offer per kind per invocation. Because the click is on host chrome, popup
blocking does not apply and a frame cannot overwrite the clipboard in a loop.

### 2.7 Grants and consent

**Shipped in #26479** (`src/kiro_crew/apps/link_preview_grants.py`, the
`approved-grants.json` flow in `docs/system-specs/modules/app-kit-platform.md`):

- A link-preview provider's authority is three grant families stored per app in
  `approved-grants.json`, beside `api` and `events`:

  | Family | Entries |
  |---|---|
  | `linkPreviewHosts` | Each host after normalization (§2.8); a leading dot keeps "this domain and every subdomain" |
  | `linkPreviewTextPatterns` | `<id>:<flags>:<source>` per pattern, flags sorted, implied `g` dropped |
  | `linkPreviewFullUrl` | `["fullUrl"]` when the provider asks for whole URLs, else empty |

- Installing approves what is declared. An update that adds a host, changes a
  pattern (any change is a new entry, because one regex cannot be judged narrower
  than another) or turns `fullUrl` on waits on the app's page for the owner, and
  until then the app previews only what was approved before. Narrowing needs
  nothing.
- When no host and no pattern is approved, the provider is off: no frame is made,
  a live frame's capability stops renewing, and its fetches are refused.

**Proposed for v1:**

- **Admission.** Unchanged: a point renders only for an app that is installed,
  enabled, and admitted by **Allow Third-Party Apps** (`apps_allow_third_party`)
  or named in `apps_trusted`.
- **`messageActions` family.** One family, `messageActions`, with one entry per
  widenable attribute of each entry: `<id>:role:assistant`, `<id>:role:user`,
  `<id>:format:markdown`, `<id>:maxChars:<n>`, `<id>:effect:copy`,
  `<id>:protocol:<major>`. Comparing entry sets gives the shipped widening rule
  for free: a new role, the markdown format, a larger cap, a new effect or a new
  protocol major is a new entry and waits for approval.
- **Revocation.** Turning a point off, revoking its grant, or disabling the app
  disposes every live frame for that point with `hide { reason: "revoked" }`,
  makes its capability fail at once, and closes open invocations. It cannot take
  back data already handed over; the detail page says so.
- **Per-point consent.** A click-driven point needs no extra prompt: the labelled
  click is the consent for that one value, and the button names the app. A
  hover-driven point needs explicit opt-in (§4), because a hover is not a
  deliberate act.

### 2.8 Host and pattern matching

**Shipped in #26479:**

- **Host normalization** (`normalize_host`). Lowercase, one trailing dot
  dropped, each label in IDNA (punycode) form, a leading dot kept. A host with a
  character that transitional and non-transitional IDNA map differently (`ß`,
  `ς`, ZWJ, ZWNJ) is refused rather than normalized to a different domain than
  the browser would reach.
- **Public suffixes** (`host_refusal`). A bare top-level domain, a host on a
  curated list of shared-hosting suffixes (`github.io`, `appspot.com` and
  others), or `<common second level>.<two-letter ccTLD>` (`co.uk`, `com.au`) is
  refused. This is not the full Public Suffix List; the dashboard mirrors both
  rules in `linkPreviewRegistry.ts`.
- **Text patterns.** Patterns run in a dedicated Worker, off the dashboard's
  thread. Each pattern has a **250 ms** budget (`SCAN_BUDGET_MS`), re-armed when
  the worker reports it is starting that pattern. On an overrun the worker is
  terminated, the pattern that used its budget is disabled for the rest of the
  session, the overrun is logged once, and the rest of the batch is retried. The
  install-time refusals stay as a first filter, not as the guarantee.

**Proposed for v1:**

- Refuse IP-literal hosts (an all-numeric host such as `127.0.0.1` has valid
  labels and is not a public suffix) and the dashboard's own host.
- Vendor the full Public Suffix List and refresh it each release, in place of the
  curated list.
- Report a pattern the budget disabled on the app's detail page, not only in the
  console.

### 2.9 Shared failure, timeout and rate rules

| Rule | Value | What happens | State |
|---|---|---|---|
| `ready` deadline | 10 s | Frame failed; retried no sooner than 30 s later | shipped |
| Message size, frame → host | 8,192 bytes (64 KB for `offer`) | Dropped unread | shipped / `offer` proposed |
| Message burst, frame → host | 120, refilled at 60 per second | Dropped unread | shipped |
| Byte rate, frame → host | 512 KB per 10 s | Frame disposed | proposed |
| Self-navigation | any | Frame closed | shipped |
| `failed` message | none | Notice under the trigger; frame closed | shipped |
| Data rate, host → frame | Per point (§3, §4) | Excess triggers are dropped, not queued | proposed |
| Three failures in 10 minutes | none | Point paused for that app until reload; detail page says why | proposed |

### 2.10 Versioning and compatibility

Proposed. The shipped protocol has no version field; it is treated as major 1.

- **Features.** `init.features` lists the host's features, each with its own
  version. A frame lists what it uses in `ready`. An additive change is a new
  feature or a new optional field; old frames ignore what they do not know.
- **Manifest floor.** Each point entry may declare `minHostVersion`. A host older
  than that does not render the point, and the app's detail page says which
  version it needs.
- **Major bumps.** Removing, renaming or changing the meaning of a field bumps the
  major. The host offers every supported major in `init.versions`. A superseded
  major, and the `kirocrew-link-preview` envelope, stay supported for **12
  months** after the release that superseded them. The end date is published in
  `docs/app-kit/` at that release, and the manifest linter warns on frames that
  only speak a major within 3 months of its end. A migration guide and a codemod
  for the frame SDK ship with each bump.
- A small frame SDK (its package name is decided when §10 step 1 lands) wraps the
  messages, the handshake and the invocation state machine, so most app code sees
  none of this.

### 2.11 Manifest rules every point shares

Each point is a list under `contributes`, parsed and validated in
`manifest_contributes.py` the way `panelTabs` and `linkPreviews` are:

- `id`: storage-safe slug (`_PANEL_TAB_ID_RE`), unique within the point list.
- `title`: 1 to 40 UTF-16 units, measured with `_mirrored_len`.
- `icon`: a name from the host glyph set.
- `entry`: a path under `ui/`, refused by `_app_path_refusal` if it could leave
  the app root.
- `minHostVersion`: optional, a release version string.
- A field present with the wrong JSON type is refused, never coerced.
- Every cap is mirrored on the dashboard side and enforced on both sides.
- Every point entry is serialized into the signed manifest.

## 3. `messageActions` (v1 proposal)

A button on one chat message. The app gets that message's text.

**Trigger.** The user clicks the app's button in a message's action row (the row
that holds Copy). Shown on hover and on keyboard focus of the message, and
always on touch devices. Each button shows the app's icon and its name in the
tooltip and accessible label. At most three app buttons inline per message
across all apps; the rest go under **More**, ordered by app name.

**Data given** (in `invoke`):

| Field | Value |
|---|---|
| `text` | The message as rendered plain text by default; markdown source only when the entry declares `format: "markdown"` and that is approved. At most `maxChars` (≤ 32,000) UTF-16 units |
| `format` | `"plain"` or `"markdown"` |
| `truncated` | `true` when `text` was cut |
| `role` | `"assistant"` or `"user"` |

**Data never given.** Any other message, the session key, title, folder, agent,
model or project, attachments, tool calls and their output, the message's id or
position, the DOM.

**Manifest.**

```json
"messageActions": [
  { "id": "save", "title": "Save to notes", "icon": "bookmark",
    "entry": "dist/frame.mjs", "roles": ["assistant"], "format": "plain",
    "maxChars": 32000, "popover": { "width": 360, "maxHeight": 480 } }
]
```

| Field | Bounds |
|---|---|
| entries per app | at most 2 |
| `roles` | non-empty subset of `assistant`, `user`; default `["assistant"]`. Each role is its own grant entry |
| `format` | `plain` (default) or `markdown`; `markdown` is its own grant entry |
| `maxChars` | 1 to 32,000; default 32,000 |
| `popover.width` | 200 to 480 |
| `popover.maxHeight` | 120 to 600 |

The detail page's line for the point says, in plain words, which roles and
which format it gets: "Gets the text of an assistant reply you click it on."

**Protocol.**

| Direction | `type` | Fields |
|---|---|---|
| host → frame | `invoke` | `invocation`, `text`, `format`, `truncated`, `role` |
| frame → host | `resize` | `invocation`, `width`, `height` (clamped to `popover`) |
| frame → host | `offer` | `invocation`, `kind: "copy"`, `value` |
| frame → host | `close` | `invocation` |

The frame renders inside a popover anchored to the button; the host's Copy button
sits in the popover's footer, outside the frame.

**Failure, timeout, rate.** §2.9. Invocation deadline 2 minutes, reset by each
`resize` or `offer`. One invocation per click; a second click on the same button
while the popover is open closes it with `hide { reason: "toggle" }`.

**Example app.** *Notes*: "Save to notes" posts the message text to the app's
own backend, which appends it to a notes file, and the popover shows "Saved" with
a title field and an offered Copy of the note link.

## 4. `linkPreviews`

**Shipped in #26479:** the manifest shape (one provider per app; `id`, `entry`,
`hosts` 1 to 64, optional `textPatterns` at most 16 of at most 200 characters,
optional `fullUrl`), the 300 ms hover detector, card and overlay (`regions`)
modes, pinning, dragging and docking, the `key` forwarding rule, and the grant
families in §2.7. By default `show` gets the URL with its query and fragment
removed, because they can carry signed tokens; `fullUrl: true` sends the whole
URL only when the owner approved it. `docs/app-kit/manifest-reference.md` is the
reference for the shipped surface.

**Proposed for v1:**

- **Opt-in.** Today installing approves the declared hosts, so previews start on
  install. Proposed: a provider renders nothing until the owner turns it on in the
  app's detail page. The switch reads: "Show \<App\> cards when you hover links to
  \<hosts\>. Hovering sends the link to \<App\>." Off by default for every app.
- **Attribution.** Every card gets a host-drawn strip with the app's icon and
  name, outside the frame, so a user always knows which app saw the link. Today
  the frame carries the app's name only as its `title`.
- **Query keys.** Between "no query" and `fullUrl`, an optional `queryKeys` (at
  most 16 names) sends only those query parameters. Each key is a grant entry in
  `linkPreviewFullUrl` (`query:<name>`), so adding one waits for approval like
  `fullUrl` does. The fragment is never sent unless `fullUrl` is approved.
- **Rate.** At most 2 `show` per second per provider and 60 per minute; excess
  hovers show nothing. The same URL re-hovered within 30 s reuses the open card.
- **Warm frames.** At most 3 hidden hover frames across all providers, kept by
  least recent use and disposed after 10 minutes idle. A hidden frame gets no
  `show`, `key` or `capability`, and one that sends any message other than
  `ready` while hidden is disposed.
- **Envelope and handshake.** Link-preview frames move to the §2.5 envelope,
  with `hide` as the invocation's cancel and `offer` for Copy and Open of the
  hovered URL.

**Example app.** *Ticket cards*: shows status, owner and title for links to
`tickets.example.com` and for plain `TK-1234` text, without `fullUrl`.

## 5. Deliberately not exposed

| Not exposed | Why |
|---|---|
| Raw mouse, keyboard or scroll streams | A stream of input events is a keylogger: it sees what the user types in the composer, in other apps and in Settings. Every point is triggered by the host's own detector and hands over one value. The shipped `key` message is narrow on purpose: only while a card shows, never from a text field, never with Ctrl, Alt or Meta. |
| Subscribing to all chat | A point lets an app act on what the user points at. A feed of every message is broad access to every conversation, which `slots:own` already refuses outside the app's own sessions. |
| Reading other messages | The user picked one message. Its neighbours may hold secrets the user never chose to share. |
| Auto-sending | A sent message runs an agent with tools on the user's behalf. App text must be read by the user first. `commands.autoSend` keeps its own preview step and is not extended to frames. |
| The dashboard token or cookies | Either one is the user's whole session. The opaque origin, `credentialless`, `credentials: 'omit'` and the capability-only fetch route (§2.3) keep both out of reach. |
| DOM access, including the hovered or clicked element | Any element reference is a path to the whole page. The frame gets a `rect` and values. |
| Session identity | A session key lets an app address the session through any route it has a grant for. `sessionControls` is the point that is handed it, and says so. |
| Clipboard read or unprompted write | The clipboard can hold anything the user copied anywhere. Writes go through the host's Copy button (§2.6). |
| Navigation, popups and modal dialogs | These take the page from the user. Opens go through the host's Open button. |
| The frame capability, to anything but the frame | It is scoped to one frame's UI and one app's backend. Agent tools (§7) never receive it. |

These limits do not conflict with Tenet 8. The built-in counterparts, such as
Copy on a message, also use only the one value the user acted on. An app that
needs more still has a full page with declared, approved grants, and agent tools
(§7) for work an agent does.

## 6. Deferred points

Designed here so v1's protocol leaves room for them. None ships until
`messageActions` has run with an external app and §10's tests pass.

**`selectionActions`.** The user selects text in one message, then picks an app
item; the app gets only the selected text (`Selection.toString()`, bounded by the
entry's `minLength`/`maxLength`, at most 8,000). On touch devices the native
selection is preserved and nothing intercepts long-press; the action is reached
through an explicit, accessible button that appears with the selection. Chat
only at first; other surfaces later through typed source adapters with their own
grant, never generic DOM selection.

**`composerActions`.** A button in the composer's add menu; the app returns text
to insert and never sends. When it ships: the host captures a draft revision and
selection at invocation; if the draft changed before the insert offer arrives,
the host shows the text with an **Insert anyway** button instead of inserting.
Inserted text is marked literal: on Send it is not parsed for slash commands,
file mentions or internal attachment tokens. The app never sees the draft in this
point; sharing a selected part of the draft is a later, separate grant.

**`linkOpen`.** A plain click on a claimed `https:` link opens the app's frame in
a panel or dialog with that URL (per §4's URL rules). Modifier clicks and the
context menu keep browser behaviour. The first click asks "Open \<host\> links
in \<App\>?"; **Always** binds to that provider plus that one host, not to every
host the app claims. A failed frame always offers the host's **Open in
browser**.

## 7. Agent-callable app tools (`contributes.agentTools`)

The next major direction. Users should be able to ask an agent to search
tickets, update notes or inspect a deployment without opening each app. This is
worth more than any UI point, and it needs no new runtime.

**Built on `mcpServers`.** An app already declares `mcpServers`. `agentTools`
names which of those servers' tools an agent **outside the app** may call; a tool
not listed is never exposed. The host proxies every call through its existing
MCP gateway, so tool discovery, approval and audit stay in the core.

```json
"agentTools": [
  { "server": "notes-mcp", "tool": "search_notes",
    "title": "Search notes",
    "description": "Full-text search over the user's saved notes.",
    "inputSchema": { "type": "object",
      "properties": { "query": { "type": "string", "maxLength": 200 } },
      "required": ["query"], "additionalProperties": false },
    "outputSchema": { "type": "object",
      "properties": { "hits": { "type": "array", "maxItems": 50 } } },
    "effect": "read" }
]
```

- **Explicit schemas.** `inputSchema` and `outputSchema` are required, JSON
  Schema with bounded strings and arrays and `additionalProperties: false`. The
  host validates inputs before the call and outputs after it, and drops a tool
  whose live MCP schema no longer matches the manifest's.
- **Grants per crew or session.** Approving the app does not expose its tools.
  The owner grants each tool to a crew, a crewmate or a session, stored as an
  `agentTools` family in `approved-grants.json` with the same widening rule as
  §2.7 (a new tool, a looser schema or a new effect needs approval).
- **Effective authority** = app grant ∩ caller authority ∩ invocation approval.
  The app grant says which tools exist for this caller; the caller's own tool
  policy, governance ceiling and sandbox still apply; and each call goes through
  the caller's approval mode like any other tool.
- **No self-declared read-only.** `effect: "read"` is a label for display, never
  a trust input. Whether a call needs approval is decided by the caller's
  approval policy for that tool name, as for any MCP tool. A tool is "read" only
  if the owner marks it so.
- **No frame capability.** A tool call carries the host's identity for the caller
  (session, crew, invocation id), never a frame capability or a dashboard token.
  Outputs enter the agent's context as untrusted tool output.
- **Audit.** Every call is logged as caller → app → tool, with the grant that
  allowed it.

A separate RFC will specify this section in full before any of it is built.

## 8. Roadmap

**Context providers and message cards.** `contributes.contextProviders` lets a
user attach an app-owned document, ticket or dataset to a turn: the provider
answers `context.offer` with a bounded excerpt, its provenance, its freshness and
its token cost, and only content the user or host approves enters the agent's
context, marked as untrusted reference data. `contributes.messageCards` lets an
app draw structured results inline: a card gets only its own payload through
`card.render`, never the transcript, and `card.action` asks for a separately
authorized host action. Each card stores a static fallback, so history stays
readable after the app is uninstalled. Both use typed, versioned envelopes on §2.

**Event handlers.** `contributes.eventHandlers` lets an app react to events such
as a finished turn, extending today's `crons`. Each handler has an approved
session scope and a field projection; `event.deliver` carries metadata by
default, not message bodies or tool arguments. Handlers run in a background
execution service, never in hidden frames, with event ids, idempotency, bounded
retries, budgets and a visible pause control. Cross-app handoffs use
host-brokered typed artifacts and explicit destination grants, never shared
credentials or an open event bus.

## 9. Adding a new point

A new point is a change to this document first, then one change that lands the
field and its reader together. It must:

1. Name the trigger, and show it is a user act or a host-owned, bounded detector.
2. List the exact data given, with caps, and the data never given.
3. Define its grant family and which entries widen it.
4. Add the manifest dataclass in `manifest_contributes.py` with §2.11's rules,
   and the mirrored caps on the dashboard side.
5. Add its messages as a new feature in the protocol (§2.10), with its invocation
   deadline and data rate.
6. Use host-owned effects (§2.6) for anything that leaves the frame.
7. Document it in `docs/app-kit/manifest-reference.md` and the app-kit-platform
   spec, with an example app outside `src/kiro_crew/apps/builtins/`.

A proposed point that cannot say what it never gives is not ready.

## 10. Rollout

1. **Generalize the shipped frame.** Point-aware mint and frame routes, the
   `kirocrew-app-frame` envelope beside the shipped one, the handshake fields,
   `invoke`, `offer`, `offerAck` and `offerRefused`, the point and invocation
   headers on the fetch route, immediate revocation, IP-literal refusal and the
   full Public Suffix List. Gate: sandbox integration tests in real Chromium,
   Firefox and WebKit that prove no dashboard cookie or token reaches an app route
   from a frame, a self-navigating frame is closed, an oversize or non-JSON
   message is dropped, a stale offer is refused, and a revoked grant fails the
   live capability.
2. **`linkPreviews` additions** (§4): opt-in, attribution, `queryKeys`, rate and
   warm-frame caps.
3. **`messageActions`**, behind a default-off feature preview until its first
   external example app has run against it.
4. **`agentTools`**, after its own RFC.
5. Deferred points (§6) and the roadmap (§8), each by its own change.

## 11. Verification notes

Read at `4537582b9b`, which carries #26479 (`04f118c525`): the message types in
`website/src/linkPreview/linkPreviewProtocol.ts` (with `MAX_MESSAGE_BYTES`,
`BURST_CAPACITY`, `BURST_PER_SECOND`, `MAX_REGIONS`); the frame element
attributes, `READY_TIMEOUT_MS` and `MAX_PINNED` in `linkPreviewFrames.ts`;
`RETRY_AFTER_MS` in `LinkPreviewLayer.tsx`; `frame_csp`, `CAPABILITY_HEADER`,
`_CAPABILITY_TTL_SECS`, `_FETCH_METHODS` and the CORS hook in
`src/kiro_crew/apps/link_preview_frame.py`; the grant families,
`normalize_host` and `host_refusal` in `src/kiro_crew/apps/link_preview_grants.py`;
`SCAN_BUDGET_MS` and the overrun handling in
`website/src/linkPreview/textPatterns.ts`; `fullUrl` and the provider caps in
`src/kiro_crew/apps/manifest_contributes.py`; `mcpServers` and `crons` on
`AppManifest` in `src/kiro_crew/apps/manifest.py`; `apps_allow_third_party` in
`src/kiro_crew/config/sections_agent.py`. No entry under `docs/decisions/`
covers app contribution points.

## 12. Scope decision

The repository owner chose, on 2026-10-30, the narrow v1: the hardened frame
protocol, semantic grants and `messageActions`, with `linkPreviews` restated
because it already ships; `selectionActions`, `composerActions` and `linkOpen`
deferred; agent-callable tools as the next major direction.

## 13. Decisions

| # | Question | Decision |
|---|---|---|
| 1 | Prompt on first use? | A clearly labelled click action needs no repeated prompt. Hover disclosure needs explicit opt-in (§4). |
| 2 | User messages in `messageActions`? | Allowed, through a separate `user` role grant. Assistant replies can hold secrets too, so neither role is free. |
| 3 | Composer draft? | Deferred. Later, only explicit sharing of a selected part of the draft, under its own grant (§6). |
| 4 | Text form? | Rendered plain text by default. Markdown only as a declared, disclosed format that is its own grant (§3). |
| 5 | Selection outside chat? | Later, through typed source adapters with their own grants. Never generic DOM selection (§6). |
| 6 | Warm frames? | Yes, with a global LRU cap of 3, 10-minute idle expiry and no messages while hidden (§4). |
| 7 | Touch selection? | Keep native selection. Offer an explicit, accessible action. Never intercept long-press (§6). |
| 8 | Support window? | A published, time-based window (12 months per superseded major), with migration tooling and feature negotiation (§2.10). |

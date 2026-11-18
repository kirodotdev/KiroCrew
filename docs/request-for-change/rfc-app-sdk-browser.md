---
title: "App SDK browser: let an app show, read and annotate the browser that its Bot uses"
status: draft
author: chenmingwei23
created: 2026-11-17
last-audited: 2026-11-17
audited-at: 5d18d11c70
doc-pr:
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: App SDK Browser

- Status: draft. Nothing in this document is built. Each step in the Migration
  plan lands as its own pull request after a maintainer signs the design, and
  each of those pull requests carries its own App Kit doc update.
- Related: `rfc-app-agent-tools-third-party-grant.md` (accepted; owns the
  third-party tool path this document depends on and does not redo),
  `rfc-app-sdk-crew-webview.md` and `rfc-app-session-controls.md` (precedents
  for a manifest field that lands with its reader), `rfc-app-sandbox-isolation.md`
  (the app trust model this document inherits), `docs/app-kit/manifest-reference.md`
  and `docs/app-kit/api-reference.md` (the contracts the implementation pull
  requests extend).

## Summary

Crew has a browser: the `browser` tool, a live view of the page, and a way for
the user to take over. An App Kit app has none of it. The one app that needs it
today, Browser Collab, gets it by importing about a dozen private gateway
modules and by carrying three patches against Crew. This RFC makes the browser
a part of the App SDK, so that an app declares a permission and uses a stable
surface, and needs no patch.

The first phase is deliberately small. An app can **view** the page (a live
view with user takeover), **read** it (outline, text, screenshot, point pick)
and **annotate** it (a cursor and marks drawn over the live view, never inside
the page). It can also **act** (navigate, click, type) through the same gates
the Bot has. It cannot write content into a page. That capability, `inject`, is
left to a second RFC (see Non-goals and section 8).

| Surface | What the app gets | Built on |
|---|---|---|
| `permissions.browser` in the manifest | A verb list: `view`, `read`, `act` | the staged grant family pattern of `permissions.api`, stored in `app-grants` |
| `ctx.browser` (backend) | `page(slot)` returning a `PagePort` | one new gateway function, `run_browser_op`, that the `/api/browser/op` route also calls |
| `BrowserView`, `useBrowserView`, `useBrowserCoords` (frontend) | The live view, takeover, coordinate mapping | the dashboard's existing live-view component and hooks, wrapped; no new channel, no new route |
| App agent tools | Browser Collab's own tools reach its Bot | `kirocrew-apps` of #17015 and `rfc-app-agent-tools-third-party-grant.md`; this RFC adds none |

One preview flag, `app_browser_sdk`, default off, covers the whole surface.

## Motivation

### What exists

Crew's browser is an internal feature:

- `src/kiro_crew/browser_harness/` holds the engine, the per-session tab
  ledger (`LiveHub.targets_of`), the URL gate (`url_gate.py`), the takeover
  lock (`takeover.py`) and the operations (`ops.py`, 17 entries in `OPS`).
- `src/kiro_crew/mcp_tools/browser.py` is the Bot-side shell. It checks the
  requested URL before it posts to `/api/browser/op`.
- `src/kiro_crew/dashboard/handlers/browser_ops.py` `api_browser_op` is the
  gateway door for every Bot operation. It owns the on/off switch check, the
  takeover hold, the gateway-wide lock, the per-session tab binding, and the
  per-caller checks of `caller_checked_call` and `_GatedRelay`.
- The live view runs over `browser_live` events to the owner's websocket, with
  the owner-only routes in `dashboard/routes/browser_live.py`. The front end is
  `BrowserLiveCardCanvas` and the `useBrowserLive*` hooks.

None of it is in `@kirocrew/app-sdk`. `ctx` has `chat_slots`, `turns`,
`crewmate` and others, and no `browser`.

### The problem

Browser Collab (a private app, `chenmingwei23/browser-collab`) lets a person and
their Bot work on the same web page side by side. It works today only because
the repository carries three patches:

| Patch | Content | Size |
|---|---|---|
| 0001 | browser fixes, a second live view, step text in the chat (not carried forward; the tool row shows the manifest title), a tool result that carries an image (not carried forward; tools return a PNG path instead) | 33 files, +2315 / -74 |
| 0002 | `browserCollabHost.ts`, which puts 12 internal front-end modules on `window.__kirocrew_modules` | 2 files, +41 |
| 0003 | a managed `kirocrew-apps` MCP server that lists a third-party app's tools with no `autoApprove` | 10 files, +209 / -2 |

Its backend also imports `browser_harness.live_hub`, `browser_harness.quick_ask`,
`browser_harness.takeover`, `browser_harness.url_gate`,
`browser_harness.frames`, `dashboard.handlers.browser_policy`,
`dashboard.handlers.browser_ops`, `dashboard.state` and `history`. Each import
is a private function that can move without notice. A patch that only one user
carries cannot be reviewed, versioned or removed as a unit.

Every future app that wants to show or read a page has the same problem. The
fix is a public surface with identity checks in one place, which is what
`chat_slot_sdk.py` did for chat slots.

## Goals

- An app can show the browser page of one of its own slots, let the user take
  over, read the page and annotate it, with no private import and no patch.
- Every existing browser gate (the on/off switch, the URL gate, the password
  field refusal, the takeover lock, the per-session tab binding) applies to an
  SDK call exactly as it applies to a Bot call, from one code path.
- An app reaches only the tabs under its own slots.
- The user grants each verb. A verb added by an update waits for the owner.
- The whole surface is off until the user turns on one preview flag.

## Non-goals

- `inject`: writing content into a page. Phase 1 has none. Section 8 lists the
  minimum a later RFC must meet.
- Third-party app agent tools. `rfc-app-agent-tools-third-party-grant.md` and
  #17015 own them. This RFC adds no managed server, no grant family and no
  preview flag for them (section 7).
- Isolation of an app from the gateway. An app with a backend runs trusted in
  the gateway process (section 3). `rfc-app-sandbox-isolation.md` is the place
  for isolation.
- An app reaching a tab the user opened, or a tab of another app (section 4.3).
- Raw frames or raw CDP for an app.
- Fixes to Crew's own browser (smooth scrolling, popups, shadow DOM). They are
  independent pull requests and need no RFC (Migration plan, step 0).

## Design

### 1. Manifest and grant

```json
"permissions": { "browser": ["view", "read", "act"] }
```

| Verb | The app may | API |
|---|---|---|
| `view` | embed the live view of its own slot, let the user take over, draw a cursor and marks over the view | `BrowserView`, `useBrowserView`, `useBrowserCoords`, `PagePort.aim`, `PagePort.view_state` |
| `read` | read the page of its own slot | `PagePort.tabs`, `outline`, `text`, `screenshot`, `pick` |
| `act` | operate the page of its own slot | `PagePort.navigate`, `click`, `type` |

`browser` is a staged grant family, registered through `apps/point_grants.py`
the way `permissions.api` and `permissions.events` are. The approved set lives
in `member-records/app-grants/<app>.json`, which the OS sandbox masks and the
agent file tools refuse (`apps/protected_app_state.py`), so an app cannot
approve itself. `bot_grant_store.py` is not used: it stores the Bots an owner
ticked for an app, and its own header says that permissions do not live there.

- Install lists each verb in plain words ("see the page you and the Bot are
  browsing", "click and type for you"). Like the explicit-only families
  (`reachableByUserSessions`, and `agentTools` in the accepted third-party
  grant RFC), installing approves no verb. The owner approves verbs in a
  separate step on the app detail page.
- An update that adds a verb holds that verb back. The app keeps the verbs it
  already had. This is the existing staged-family update rule
  (`_approved_grants_after_manifest_change`), so it needs no new code path.
- `act` returns `user_holds` while the user has taken over the page.

### 2. One gateway function for every browser operation

The review of the first draft of this design found that `ctx.browser` calling
`ops.run_op` directly would skip most of the gates. The gates are not in
`run_op`:

| Gate | Where it lives on main |
|---|---|
| URL check on the requested address | only in the shim, `mcp_tools/browser.py`. `OpContext.url_allowed` defaults to allow-all (`browser_harness/ops.py`), and the gateway checks only the address the page landed on. A call that builds its own `OpContext` has no URL gate, and a request to an internal address is sent before the landing check can refuse it |
| turn opening and replay recording, the takeover `hold`, the gateway-wide lock, a second on/off check after the lock, the fence shared with takeover | `api_browser_op` |
| binding the supervisor to this session's own tab | `_bind_target` |
| per-caller on/off checks for helpers and in-frame operations | `caller_checked_call`, `_GatedRelay` |

Design: extract the body of `api_browser_op` into one gateway-internal
function, `run_browser_op(session_key, op, args)`. The route calls it. `ctx.browser`
calls it. The requested-URL check moves into it; the shim keeps its own copy so
the Bot gets the error sooner. The Bot and the SDK then share one door, and no
gate has two copies.

Tests that must exist (and go red if the extraction is skipped):

- `ctx.browser.page(slot).navigate("http://169.254.169.254/")` is refused before
  any request leaves the gateway.
- While a Bot operation holds the lock, an SDK `click` waits for the lock and
  does not run inside it.
- With the on/off switch off, every `PagePort` method returns `policy_denied`,
  and the engine is stopped as it is for the Bot.
- The on/off check uses the slot's own session key (the key `api_browser_op`
  uses for the Bot in that slot), never the app's name. `browse_denied` finds
  the governance profile through the session key
  (`browser_harness/governance.py`); an app name with no bound profile could
  otherwise be looser than the slot it drives.

### 3. Trust model

`permissions.browser` is the owner's consent to a stable interface. It is not
isolation. This follows the wording of `chat_slot_sdk.py`: the SDK "is not a
boundary against the app's own in-gateway Python, which runs trusted", and is
the supported surface and the place the identity checks live.

What is true on main today, and so true of this design:

- An app page runs in the dashboard's own React tree, with no iframe
  (`website/src/components/AppHost.tsx`). The SDK `fetch` carries the owner's
  cookie, and the path limit of `scopedApi.ts` is checked only in the front end.
  App page code can call a `/api/browser/*` owner route directly.
- An app hooks backend runs inside the gateway process
  (`apps/hook_reconcile.py`). It can import `LiveHub` and read any session's
  frames. Browser Collab does exactly this today.
- The real boundary is the execution trust the owner gives at install
  (`apps/execution.py`, `agent.apps_trusted`). An app with no execution trust
  has no hooks backend and no way to import a gateway module.

So the identity checks in this design (slot prefix and stamp, verb checks,
masking, "no raw frames") bind an app that uses the SDK. They do not stop an
app that goes around it, and this RFC does not claim they do. Two consequences:

- `BrowserView` does not pass raw frames to the app because that keeps the
  interface small and the password-field mask in one place. It is not a defence
  against a hostile app.
- Real isolation needs the app page in a sandboxed frame, as
  `apps/app_frame.py` does for apps that opt into `webOrigin`. That is
  `rfc-app-sandbox-isolation.md`, not this document.

### 4. Backend: `ctx.browser`

#### 4.1 Shape

`ctx.browser` exists only when the manifest declares `permissions.browser` and
the preview flag is on, as `ctx.chat_slots` exists only with
`permissions.chatSlots`.

```python
class BrowserSDK:
    def page(self, slot: str) -> PagePort: ...   # BrowserRefused when the slot is not this app's
    def slots(self) -> list[str]: ...            # this app's slots that have a browser tab

class PagePort:
    # view
    def aim(self, x: float, y: float, label: str = "") -> None
    def view_state(self) -> ViewState
    # read
    async def tabs(self) -> list[TabInfo]
    async def outline(self, *, budget: int = 6000, ref: str | None = None) -> Outline
    async def text(self, ref: str | None = None) -> str
    async def screenshot(self, *, region: Box | None = None) -> bytes
    async def pick(self, point: Point) -> PickResult
    # act
    async def navigate(self, url: str) -> NavResult
    async def click(self, ref: str) -> OpResult
    async def type(self, ref: str, text: str) -> OpResult
```

Results are values, not exceptions, as in `ops.py`. A refusal carries a fixed
`code`: `policy_denied`, `url_blocked`, `password_field`, `user_holds`,
`not_your_slot`, `permission_denied`, `preview_off`. A verb the grant does not
hold returns `permission_denied`, never a silent no-op.

#### 4.2 What is existing code and what is new

The first draft said the SDK writes no browser logic. That is true only for
some methods:

| Method | Source |
|---|---|
| `navigate`, `click`, `type`, `tabs` | forwards an existing operation through `run_browser_op` |
| `outline` | forwards the existing `snapshot` operation, with a size budget |
| `screenshot` | forwards the existing `screenshot` operation, with a byte cap on the result |
| `aim`, `view_state` | forwards `LiveHub.aim` (move from the Browser Collab patch) and the takeover state |
| `text`, `pick` | built from `snapshot` plus coordinate mapping if that is enough; otherwise a new read-only operation in `OPS`. Step 2 decides, with the test that shows the gap |

`OPS` has no `evaluate`, on purpose (`ops.py`). Phase 1 adds none. A new
read-only operation, if step 2 needs one, runs through `run_browser_op` like the
others and ships with its own gate test.

#### 4.3 Which tabs an app can touch

One rule: an app touches only the tabs under its own slots.

- A slot key is `<app>-<id>`, the grammar `chat_slots` uses. The slot must also
  carry the app's own stamp (`slot._app`). Both must hold. A prefix alone is not
  enough, because an app name can extend another's (`foo` and `foo-bar`), and
  `chat_slot_sdk.py` states this. A test shows that `foo` cannot reach
  `foo-bar-1`.
- Tabs are found through `LiveHub.targets_of(session_key)`. The relay's
  `_agent_targets` is a global ledger of tabs the daemon opened, and is not used.
- The user's own tabs, other apps' tabs and the dashboard chat's tabs do not
  appear in `tabs()`.
- A tab that a page of this slot opens (`window.open`, `target=_blank`) belongs
  to this slot. This is fix F3 of step 0.
- A slot has a cap on tabs. `VISIBLE_TABS_CAP = 32` limits the tabs shown on a
  live card and is not a per-slot cap. Step 1 adds a separate per-slot cap
  (proposed 8).

The `slots()` method lists only this app's slots.

### 5. Frontend: `BrowserView`

```ts
<BrowserView
  slot="myapp-123"                 // required, this app's slot key
  tabId?: string                   // a tab of that slot; default is the current tab
  interactive?: boolean            // may the user take over; default true
  onTakeover?: () => void
  onHandBack?: (note?: string) => void
  onNavigate?: (e: { url: string; title: string }) => void
  marks?: Mark[]                   // annotations in view coordinates, drawn over the frame
  aria-label: string               // required
/>
useBrowserView(slot): { state, url, title, tabs, claim(), release(note?) }
useBrowserCoords(slot): { toPage(x, y), toView(x, y) }
```

- `BrowserView` is the dashboard's existing `BrowserLiveCardCanvas`,
  `useBrowserLive` and `useBrowserLive.input`, wrapped. It reads the state the
  dashboard already holds. It adds no new websocket channel, no new route and no
  change to `ws_event_scope.py`.
  - `browser_live` already goes to the owner's websocket, and an app page is
    inside the owner's page. `ws_event_scope.py` governs clients that connect to
    `/api/ws` with an app token; an in-dashboard app page does not use it.
  - The takeover routes require an owner request and a slot in the live slot
    table (`dashboard/handlers/browser_live.py`). An app page carries the owner
    cookie and its slot is in the table, so the existing routes already work.
  - If an app one day runs in a sandboxed frame and needs a token-scoped
    channel, that is a later RFC with the sandbox.
- Before it renders, the component checks that the slot is stamped with this
  app's name. The check is a convenience for honest apps and gives a clear
  `denied` state. Section 3 says why it is not a boundary.
- Marks are drawn by the component in the app's page, over the frame, in view
  coordinates. They never enter the page. `useBrowserCoords` converts between
  view and page coordinates for an app that wants to mark an element.
- The password-field mask stays in the frame source (`screencast.py`) and is not
  an option on the component.
- A second view of one slot (two tabs side by side, for a comparison) uses
  `tabId` and the `LiveHub.start_view / stop_view` pair from patch 0001. It
  ships in the step that adds `BrowserView`, not in an earlier fix.
- Strings come from SDK i18n, so an app does not carry the 14 locale files.

### 6. Where Browser Collab ends up

| Today | After |
|---|---|
| `host.useBrowserLive`, `useBrowserLiveInput`, `BrowserLiveCardCanvas`, `BrowserLiveReplayStage` | `BrowserView`, `useBrowserView`, `useBrowserCoords` |
| chat pane through `host.ChatPane`, `store`, `chatSlice` | existing SDK exports (`ChatEmbed` or `ChatPanel`, `useChatSession`); no new SDK code |
| `host.i18nT`, `sanitize`, `thunkError`, `ErrorNotice` | the app's own bundle, or existing SDK exports (`activeLocale`, `fmt*`, `useNotify`) |
| backend `live_hub`, `PageProbe`, `takeover.USER`, `url_gate` | `ctx.browser.page(slot)` |
| patch 0003 `kirocrew-apps` | `kirocrew-apps` of #17015 and the third-party grant RFC |
| `ui/src/host.ts` and `patches/` | deleted; the app reads `APP_SDK_LEVEL` at start and shows "upgrade Crew" when it is too low (the manifest has no `minSdkLevel` for an ordinary app) |

What Browser Collab does with `inject` today (its edit-the-page feature) is
removed from its first SDK release and returns with the second RFC.

Acceptance, on a Crew with no patch: install the app, turn on `app_browser_sdk`,
and run view, takeover, Bot-opens-page, compare and annotate. Remove one verb
from the grant and the matching call returns `permission_denied`. Turn the
preview off and the app installs and shows "not enabled".

### 7. App Bot tools: depend on the accepted RFC

A Bot in an app's slot uses Crew's own `browser` tool to open and read pages.
Browser Collab's companion agent has tools of its own (compare tabs, annotate).
Those are `agentTools` of the app, reached through `kirocrew-apps`.

This RFC builds none of it. `rfc-app-agent-tools-third-party-grant.md` is
accepted, and #17015 is its base. The companion agent of Browser Collab is the
acceptance case for that RFC's phase 2 (an app's own scoped agent reaches its
own approved tools): its `agents/companion.json` names `@kirocrew-apps/browser_collab_*`.

Per-call approval is the line this RFC cares about most, so it states the rule
that the work above must meet:

- The user's own choices work as the user set them: Trust mode, YOLO, and
  "always trust" on an approval card (`dashboard/chat_turn/tool_approval.py`;
  `website/src/components/ApprovalCard.tsx`). A person who chose them keeps
  them for third-party tools too.
- Server configuration and the app itself can never make a third-party tool
  skip approval. `kirocrew-apps` is in the prompt-only list
  (`PROMPT_ONLY_MCP_SERVERS` in the third-party grant RFC; it is not yet on
  main, #17015 adds it). An app's manifest and an app's own agent
  `allowedTools` cannot waive the prompt.
- That last line is not true on main today. An app agent can write
  `"allowedTools": ["@kirocrew-apps"]`, and `bridges_agents.py` filters the list
  only against the governance ceiling. With no governance policy installed,
  `platform/governance.py` keeps an `@server` entry. The work for #17015 and the
  grant RFC needs a test that materializes such an agent and finds the entry
  removed.

The third-party grant RFC says `may_skip_gate` never skips the gate for
`kirocrew-apps`. Read next to the user choices above, "never" means "never for
server configuration or an app". Whoever updates that RFC next should say so in
one sentence. This RFC does not edit it.

No `app_third_party_tools` preview flag is added. If the grant RFC wants one, it
should add it there.

### 8. `inject` is a second RFC

Phase 1 stops at view, read, act and annotate. `inject` (an app writes content
into a page: changes text, inserts a working control) is a different class of
risk, and the reasons that supported it in the first draft do not hold:

- "The Bot can already read the page" is about reading. `inject` is a write.
- An isolated JavaScript world separates variables, not the DOM. The page's own
  scripts can read what an app writes into it. A Bot that copies data from site
  A into site B hands it to site B.
- The model decides what to write, and it reads untrusted pages. After a prompt
  injection it can draw a fake sign-in form on a real site, or add an image
  whose address carries data out.
- By section 3, an app with a hooks backend can already do much of this, so a
  verb grant is consent and not a fence.

A second RFC must at least specify:

1. An SDK-owned sanitizer: no script, no event attributes, no external
   resources, no forms.
2. A visible mark on every written node, and revert per layer.
3. Data from one tab is not written into another tab by default.
4. Refusal on sign-in and checkout pages, with a `page_kind` signal that the SDK
   owns (core has only the password-field refusal today).
5. A separate verb, a separate preview flag, and a trust prompt that says what it
   allows in plain words.

### 9. Preview flag

`dashboard.feature_previews.app_browser_sdk`, default off, in Settings >
Developer > Feature previews. It is added to `FEATURE_PREVIEW_FLAGS`
(`config/sections_dashboard.py`) and to `website/src/utils/previewFlags.ts`; the
existing test pins the two lists together.

With the flag off: `ctx.browser` is absent; `BrowserView` renders a "not
enabled" placeholder; an app that declares `permissions.browser` still installs,
its trust prompt says the feature is off, and the grant has no effect.

With the flag on and the on/off switch (`capabilities.browse`) off, every
`PagePort` method returns `policy_denied`.

No default is changed. Turning a default on is a later decision.

### 10. SDK level

`APP_SDK_LEVEL` is 10 on main (`apps/app_sdk_level.py`, with
`website/src/app-sdk/sdkLevel.ts`); #38333 took 9 to 10. The level goes up by one,
to the next free number, in the last pull request of this RFC's step 1 that makes
the surface usable (the front-end one), not in the first. Phase 1 of the third-party
grant RFC also raises it by one. The rule between the two RFCs is unchanged: whichever
merges first takes the next number. An app reads the level to learn whether the host
has a capability, so it must not be announced before it is real.

**Relation to #38333.** #38333 (merged) lets an app's own agent use Crew's
`browser` tool without a card per step, on the sites the owner approved in
`permissions.browserSites`. It also gives the app page a `useBrowserMedia` hook
(play, pause, seek, volume, state of the tab's media). This RFC covers the app's own
page and backend: the `BrowserView` component and `ctx.browser`. The two are
complementary. `browserSites` is approved by the owner on the app's page and an
install never approves it, so it is a grant from the owner. It does not break this
RFC's rule that an app cannot make its own tools skip approval. This RFC adds no media
control: an app that needs it reuses `useBrowserMedia` and `POST /api/browser/media`,
and nothing here duplicates them. Both touch `browser_harness/ops.py`,
`snapshot_walker.js` and the SDK level, so implementation pull requests rebase on
what #38333 added.

## Migration plan

One pull request per row. The pull requests for the new surface do not start
until a maintainer signs this RFC.

| Step | Change | Exit criteria |
|---|---|---|
| 0 | Crew browser fixes with no SDK dependence, each its own pull request: F1 scroll and frame smoothness, F2 page read and click (shadow DOM, coordinate click), F3 popup tabs follow the session. | Each has its own tests. None needs this RFC. There is no F4 and no F5: a tool that returns an image writes a PNG file and gives its path in the text, as `dashboard_snapshot` and computer-use do. No inline-image result path is added. The chat step text of the old patch is dropped too, because the running-step label it changed is not shown on main. The tool row shows the title from the app manifest instead, which needs a backend change and is part of step 1b. F1 and F3 merge before step 1 because step 1 builds on `live_hub.py` and `relay.py`. |
| 1a | Manifest field `permissions.browser`, the `browser` grant family, the preview flag, an empty `ctx.browser` gated by both. Docs for these in the same pull request. | A manifest with `browser` installs and holds no verb until approved. An update that adds `act` holds `act` back. Preview off: `ctx.browser` is absent. |
| 1b | `run_browser_op` extracted from `api_browser_op`; `PagePort` on top of it; slot check (prefix and stamp); per-slot tab cap; `aim`; the tool row in chat shows the app manifest title (backend change). Docs in the same pull request. | Every test in section 2 and the `foo` / `foo-bar` test of section 4.3. The `/api/browser/op` route behaves as before (existing tests unchanged). |
| 1c | `BrowserView`, `useBrowserView`, `useBrowserCoords`, second view, SDK i18n, `APP_SDK_LEVEL` bump. Docs in the same pull request. | The component renders the live view of an app's own slot in a pod, and a takeover and hand-back work end to end. A slot of another app shows `denied`. Level number as in section 10. |
| 2 | Browser Collab moves to the SDK in its own repository. | The acceptance run of section 6, on a Crew with no patch. |
| 3 | Third-party app tools. Owned by the accepted third-party grant RFC, not built here. | Its phase 2 passes with Browser Collab's companion agent as the case. |
| 4 | `inject`. Separate RFC (section 8). | Not specified here. |

There is no stand-alone documentation pull request. The feature-map check and
the App Kit habit tie each document to its implementation pull request.

## Backward compatibility

- A new optional manifest field, a new flag that is off, and an `APP_SDK_LEVEL`
  bump. No app that works today changes.
- `api_browser_op` keeps its behavior; step 1b moves its body and must keep its
  existing tests green with no edit.
- The Browser Collab patches are not part of Crew. Nothing in Crew stops
  working when they are no longer used.

## Security considerations

- Section 3 states what the grant is and is not. The SDK is the supported
  surface, the place the identity checks live, and the stable contract. Execution
  trust is the boundary.
- One gateway door (section 2) keeps the URL gate, the lock, the takeover hold,
  the tab binding and the on/off check in one place. This is the main protection
  the SDK gives, and it is also what keeps the Bot and an app from drifting
  apart.
- A new write surface is the largest risk, so phase 1 has none.
- `screenshot` and `outline` return page content to an app's backend. The Bot
  reads the same content. With `read` granted, an app sees what the Bot sees in
  that slot. The password-field refusal and the mask apply.
- Per-call approval for third-party tools is as section 7 says. This RFC does not
  weaken it and does not build it.

## Alternatives considered

### Keep the patches

Rejected. They cannot be reviewed as a unit, they break on every main move, and
they give one app a back door that every other app is denied.

### Give the app a token channel for `browser_live`

The first draft added an app route variant and a `ws_event_scope.py` scope.
Rejected for phase 1: an app page is inside the owner's page, which already gets
`browser_live` and already passes the owner routes. The channel is worth building
when apps run in sandboxed frames.

### A new `kirocrew-apps` server in this RFC

Rejected. #17015 and the accepted grant RFC cover it, with a per-tool owner
grant that the first draft did not have.

### Offer `inject` now, behind a verb

Rejected for phase 1 (section 8).

### Let an app adopt a user's tab

A verb that moves a tab the user chose into an app slot would let an app work on
a signed-in page. Rejected for the first version: it opens the cross-tab door.
It can be added as its own verb later without changing these interfaces.

### Give an app raw frames

Rejected. Frames can hold signed-in content, and the mask lives in the frame
source. A `thumbnail()` that the SDK masks can come later if an app needs one. A
replay component can come later too, because the core already has an owner-only
turn replay (`dashboard/routes/browser_live.py`).

## Open questions

1. Is a per-slot tab cap of 8 right? It is a new constant, separate from
   `VISIBLE_TABS_CAP`.
2. Should `act` be in phase 1, or should phase 1 be `view` and `read` only? The
   Bot already navigates and clicks through Crew's own `browser` tool, so the
   first app may not need it. The design keeps `act` because the gates are the
   same, and the question is whether the added surface earns its keep.
3. `text` and `pick`: is `snapshot` plus coordinate mapping enough? Step 1b
   answers this with a test and, if not, adds one read-only operation.
4. Does the maintainer want the one-sentence clarification of "never" in the
   third-party grant RFC (section 7) to land in that RFC or in this one?
5. Who builds phases 1 and 2 of the third-party grant RFC, and can Browser
   Collab be the phase 2 acceptance case? The authors of #17015 and of that RFC
   are being asked; this RFC takes whichever split they choose.

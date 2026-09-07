---
title: Extract a chat core shared by every chat surface
status: partial
author: zezhexu
created: 2026-08-22
last-audited: 2026-09-05
audited-at: 8ed028b0b
doc-pr:
implementation-prs: ["#5128", "#5909", "#8599", "#8631", "#8655", "#8689"]
tracking-issues: ["#8651"]
supersedes: []
superseded-by: []
---
# RFC: Extract a chat core shared by every chat surface

- Status: **partial** — P1 (renderer registry) merged; P2 (transport) merged for ChatPane, in review for ChatEmbed, SideChat and ChatPage; P3 (composer) merged for SideChat, in review for ChatEmbed. See §4.2 for the per-PR table.
- Author: zezhexu (drafted with Kiro)
- Created: 2026-08-22 · Last audited: 2026-09-05 at `8ed028b0b`
- Related: [`rfc-everything-is-an-app.md`](rfc-everything-is-an-app.md) (apps need first-class chat embeds); the error-to-agent hand-off work ([PR #5002](https://github.com/kirodotdev/KiroCrew/pull/5002)), whose review concluded the per-page draft-risk audit only disappears once a non-destructive side-panel chat exists.

## 1. Summary

The dashboard ships at least five chat UIs. Only the message list is partially shared; the send discipline, the streaming transport, the composer, and half of the role renderers are re-implemented per surface. This RFC proposes extracting a **chat core** — transport, message model, renderer registry, composer — so every surface composes the same primitives and differs only in chrome. The extraction is a strangler migration in five phases, each independently shippable.

## 2. The problem

### 2.1 Inventory (measured on `main` at creation, 2026-08-22)

| Surface | File | Lines | Send logic | Transport | Composer | Renderers |
|---|---|---|---|---|---|---|
| Main chat | `pages/ChatPage.tsx` | 7,347 | own (`ChatPage.send`) | WebSocket | real `ChatInput` | own `renderMessage` (9 role cases) |
| Session grid pane | `components/ChatPane.tsx` | 626 | own (mirrors `ChatPage.send`, comment says so) | shared store | real `ChatInput` | app-sdk `ChatMessageList` |
| Side panel chat | `pages/chat/SideChat.tsx` | 647 | own submit queue | shared store | bare `<textarea>` | app-sdk `ChatMessageList` |
| App embed | `app-sdk/ChatEmbed.tsx` | 220 | own | **polling** (1s/5s `refetchInterval`) | bare `<input>` | app-sdk `ChatMessageList` |
| Full-page app mount | `app-sdk/ChatPanel.tsx` | 37 | delegates | (mounts entire ChatPage) | (entire ChatPage) | (entire ChatPage) |

The real composer, `components/ChatInput.tsx`, was **2,981 lines** (queue stack, IME guard, attachments, slash menu, skill picker, browser-use toggle) and reached only two of the five surfaces. `app-sdk/` is an earlier partial extraction (`ChatMessageList`, `useChatSession`, `useComposerDraft`) that stopped at the message list.

**Inventory correction (2026-09-05, found during P2).** Beyond the five surfaces there are further hand-written `POST /api/chat` senders: `app-sdk/useChatSession.ts` (seed send; converted in #8599), `App.tsx` (the feedback send), `apps/mochi/panel/panelBridge.ts` (echoes the user bubble before the POST, never reads the receipt), `apps/design-tweak/api.ts` (resolves a non-JSON 2xx as `{ok:true}`), `hooks/useSceneInteraction.tsx` (re-implements the receipt classifier without the deadline; also the last `api.steerChat` caller), `apps/design-critique/api.ts` (the same `.catch(SyntaxError)` swallow ChatEmbed had), and the two non-interactive `api.sendChat` callers `apps/issue-radar/agentSession.ts` and `apps/auto-improvement/agentSession.ts` (no composer to restore into; they need the receipt classification, not the recovery policy). Each is its own P2 slot after ChatPage.

### 2.2 Evidence this hurts

- **Double-wiring defect class (already shipped).** A new message role had to be handled in BOTH `ChatPage.renderMessage` and `app-sdk/ChatMessageList` — `mcp_oauth` was wired only in app-sdk and rendered as raw text in the main chat. There was no catch-all; every future role repeated the trap. *(Closed by P1.)*
- **Send-receipt defect class (already shipped).** The `sendChat` `?ws=1` receipt contract (`{ok,queued,error}`; HTTP failures RESOLVE) had to be re-learned per call site; a refused send silently reached "running" state in one caller, and ChatEmbed reported an SSE stream that failed JSON parsing as a *successful* send. *(Closed per surface by P2.)* P2 also found a second-order instance: the client's `j()` helper collapses a post-2xx body-read failure into the same `TypeError` a never-sent request throws, so "accepted, receipt lost" and "never left" were indistinguishable — the transport now tags the former (`AcceptedBodyUnreadable`, #8655).
- **Capability cliff.** Companion/side/app chats silently lacked queueing, attachments, slash commands, skills, approvals affordances — users experienced an arbitrarily degraded agent depending on which pane they typed into.
- **Product features blocked.**
  - *Non-destructive error hand-off*: "Ask the agent" navigates to `/chat` today, so it must stay off every form-adjacent error. The durable fix (per the Design review on PR #5002) is handing off into a side-panel chat — which requires the side panel to be a full-capability chat, not a 647-line textarea fork.
  - *Everything is an app*: apps get either a polling toy (`ChatEmbed`) or the entire page (`ChatPanel`); nothing in between.
  - *Artifact companion chat*: same cliff.

### 2.3 Root cause

`ChatPage.tsx` grew as the product; each new surface copied the smallest usable subset instead of extracting, because there was no seam to extract into. `app-sdk/` proved partial extraction works (five surfaces share `ChatMessageList`) and also where it stopped: rendering was extracted, behavior was not.

## 3. Proposal

Extract `website/src/chat-core/` as four headless layers plus per-surface chrome. Dependency rule: each layer depends only on layers below it; **no Redux imports inside chat-core** (app contexts have no store) — surfaces adapt their own state at the edge.

| Layer | Contents | Replaces today |
|---|---|---|
| 1. `transport` | one session handle: `send / steer / queue / abort`, the `?ws=1` receipt contract, streaming events (WebSocket, polling fallback for app iframes). **Landed shape:** `sendTurn` classifies every outcome into a receipt (`dispatched / queued / refused / unknown / response-late / transport-error`) and never rejects; an injectable per-surface *wire* (dashboard client, permission-scoped app-sdk wire, `/side/*` adapter) carries endpoint-specific flags; `mintSendId` and the abort-race helper `settleUnderSignal` are shared. | 4+ hand-rolled send paths; ChatEmbed's polling |
| 2. `model` | headless message store: role/turn grouping, tool-call collapsing, queue state, optimistic submits. **Includes the composer's slot-state seam (#8651)** so layer 4 stops reading Redux directly. | per-surface ad-hoc state |
| 3. `render` | **single role-renderer registry** (register once, every surface renders it), transcript, turn block, tool group, approval cards. **Landed shape:** `app-sdk/messageRenderers.tsx` with a parity contract test; `ChatPage.renderMessage` still dispatches 29 `role ===` branches of its own (P5-a). | `renderMessage` + `ChatMessageList` double wiring |
| 4. `composer` | `ChatInput` moved behind capability flags so a slim surface disables features instead of forking the input. **Landed shape:** capabilities are props; the app embed uses a fail-closed `embedded` preset that forces every default-on capability off (source-shape test pins it). | input reachable by only 2 surfaces |
| chrome (stays per-surface) | ChatPage's sidebars/URL sync/panels; SideChat's rail; ChatEmbed's card | — |

## 4. Migration plan (strangler; each phase ships alone)

- **P0 — Contract inventory.** Enumerate roles, send/receipt semantics, streaming event shapes; pin them with tests. *Status: receipt contract pinned as tests (#5909, extended in #8599/#8655/#8689); streaming event shape not yet inventoried.*
- **P1 — Renderer registry.** One role registry consumed by both `ChatPage.renderMessage` and `ChatMessageList`; a role registered in one path but not the other fails a contract test. *Status: merged (#5128).*
- **P2 — Transport.** One send/steer/receipt implementation; every composer becomes a caller. *Status: ChatPane merged (#5909); ChatEmbed + app-sdk seed (#8599), SideChat (#8655) and ChatPage (#8689) in review — with #8689 every user-facing composer send is on `sendTurn`. Remaining: the app-local and background senders in §2.1.*
- **P3 — Composer.** `ChatInput` extraction with capability flags; SideChat and ChatEmbed adopt it. *Status: SideChat merged (#5128); ChatEmbed in review (#8631, merged into #8599's branch). Model-layer follow-on: the store-free seam, #8651.*
- **P4 — Surface adoption + the payoffs.** SideChat becomes full-capability → error hand-off retargets to the side panel → the `askAgent` draft-risk audit dies and the button becomes default-on. `ChatEmbed` becomes the recommended app chat.
- **P5 — Deletion.** Remove the superseded forks; `ChatPage.tsx` shrinks to chrome + composition. *First cut, P5-a: `ChatPage.renderMessage`'s 29 `role ===` branches consume the registry (ChatPage-only chrome injected as extra renderers / props), narrowing the parity allowlist.*

### 4.1 Pre-publish gate: store-free composer seam (added and ratified 2026-09-05)

**Decision: `ChatEmbed` is host-only for now.** Decoupling `ChatInput` from the store is P3 model-layer work, not a P2 prerequisite. Tracked in **#8651**.

Background: P3's ChatEmbed adoption (#8631) mounts the real `ChatInput`, whose subtree reads slot state from the dashboard Redux store. That flips `ChatEmbed`'s contract from "no Redux dependency" to "must mount under the host store", and points the `app-sdk → components/providers/hooks` dependency opposite to the standalone-publish plan in `app-sdk/index.ts`. Both in-tree hosts (spec-builder, ops-mission-control) already mount in-host, so nothing breaks today.

**Gate that remains:** `@kirocrew/app-sdk` must not publish standalone until #8651 lands (ChatInput obtains slot state through a composer-owned seam; ChatEmbed mounts without a `Provider`) — or the published SDK declares `ChatEmbed` host-only with `ChatPanel` as the app-facing composer surface.

### 4.2 Progress (2026-09-05)

| Phase | Slice | PR | State |
|---|---|---|---|
| P1 | Renderer registry + parity contract test | #5128 | merged |
| P2 | ChatPane → `sendTurn` | #5909 | merged |
| P2 | ChatEmbed + app-sdk seed → `sendTurn` over an app-sdk wire; receipt policy; injectable `SendWire` | [#8599](https://github.com/kirodotdev/KiroCrew/pull/8599) | review-ready (carries #8631) |
| P3 | ChatEmbed mounts the real `ChatInput` (fail-closed `embedded` preset) | [#8631](https://github.com/kirodotdev/KiroCrew/pull/8631) | merged into #8599's branch |
| P2 | SideChat → `sendTurn` over a `/side/*` wire; shared core-owned receipt copy; `AcceptedBodyUnreadable` | [#8655](https://github.com/kirodotdev/KiroCrew/pull/8655) | in review (stacked on #8599) |
| P2 | ChatPage send + steer → `sendTurn` (`steer`, `colorTheme` flags) | [#8689](https://github.com/kirodotdev/KiroCrew/pull/8689) | in review |
| P2 | `App.tsx`, `useSceneInteraction`, mochi `panelBridge`, design-tweak, design-critique, issue-radar / auto-improvement `agentSession` | — | not started |
| P3 | Store-free `ChatInput` seam | [#8651](https://github.com/kirodotdev/KiroCrew/issues/8651) | design draft pending |
| P5-a | `ChatPage.renderMessage` → registry | — | next |
| P4 | Error hand-off → side panel; `askAgent` default-on | — | after P5-a |

Follow-ups recorded during review, not yet scheduled: route SideChat's four remaining panel-local statuses (queue cancel/edit failure, question-too-long, demotion notice) through the per-slot `sideSendStatus` store channel (#8655 FP); design-critique's `SyntaxError` swallow (its own P2 slot).

### 4.3 P4 scoping — non-destructive error hand-off, `askAgent` default-on (2026-09-05, measured at `main` e76341d16)

**Today.** `ErrorNotice` takes `askAgent?: boolean`, **default `false`**, and the doc comment names that default as the safety property: the hand-off (`AskAgentButton` → `sendErrorToChat` → stage the prompt in `sessionStorage` → `softNavigate('/chat')`, or a hard reload for crash boundaries) unmounts whatever rendered the banner, so an opt-out default would turn "forgot the prop" into silent loss of a half-filled form. 23 files opt in explicitly (aws-control drive/console/page, Schedule, Instances, Skills, Browser, RemoteCrew, JobLogs, Prompts, Cfg, Agents, CommentsSidebar, Executions, PullRequestPanel, ChatSidebar, AwsConsentGate). `ChatPage` drains the staged prompt on mount and via `subscribeChatHandoff` when already mounted.

**The constraint the RFC assumed away.** "Hand off into the side-panel chat" presumes a side panel exists where the error is. It does not: `SidePanel` (and the `ActivityViewer` `side` tab that hosts `SideChat`) is mounted **only by `ChatPage`** (`ChatPage.tsx:9964/10004`). Every `askAgent` opt-in site above is on a non-chat route. So the P3 work that made `SideChat` full-capability (#5128, and #8655's transport) is necessary but not sufficient: P4 needs a **host surface for a companion chat on non-chat routes** before the default can flip.

**Options for the host (product decision, not mine to make):**

| | Host | Destroys the page? | Cost | Notes |
|---|---|---|---|---|
| A | App-level right drawer mounting `ChatEmbed` (host-only, per §4.1) for a fresh or last-used slot, opened by the hand-off; stays over the current route | No | Medium — one drawer in `App.tsx`, a `handoffToDrawer` seam beside `sendErrorToChat`, drawer state in `dashboardSlice`; reuses `ChatEmbed` as-is | Closes the "artifact companion chat" cliff (§2.2) at the same time. Needs the store-free seam (#8651) only if the drawer must work inside app iframes — in-host it does not |
| B | Open `/chat` in a **new browser tab** with the prompt staged in `localStorage` (sessionStorage does not cross tabs) | No | Small — a `target` option on `sendErrorToChat` + a cross-tab staging key ChatPage also drains | Cheapest; loses the "beside the error" affordance, and popups may be blocked |
| C | Keep navigating, but stage the page's unsaved form state so the user can come back (draft-preserving hand-off) | Yes, but recoverable | High — every form needs a draft store | Rejected by the #5002 Design review as whack-a-mole; listed for completeness |

**Recommendation:** A, staged as (P4-a) the drawer + `handoffToDrawer` with `askAgent` still opt-in; (P4-b) flip `askAgent`'s default to `true` once every existing opt-in site renders through the drawer and the `ErrorNotice` doc comment's safety argument no longer applies (the button no longer unmounts anything); (P4-c) retire the per-page draft-risk audit. `askAgentHard` (crash boundaries) keeps the full-page hand-off — a drawer over a tree that threw is not a recovery. Sequence after #8599 (the drawer wants `ChatEmbed`'s real composer and receipt policy) and #8713.

**Open question for the owner:** A vs B — does the companion chat need to sit *beside* the error (A), or is "the agent already knows what broke, in a new tab" enough (B)? A is the RFC's stated payoff; B ships in a day.

## 5. Non-goals

- No backend/session-protocol changes; chat-core is a frontend refactor over existing APIs.
- No visual redesign; pixels stay put per surface.
- Slack/Telegram/CLI surfaces (different runtimes) and the mochi Electron renderer are out of scope; mochi may adopt chat-core later.

## 6. Risks

- `ChatPage.tsx` is entangled with Redux slots, URL sync, and panels; extraction pulls threads. Mitigation: strangler order (registry first, page last), per-surface capture-harness screenshots as regression evidence, and contract tests before any move.
- `app-sdk` modules are exposed to third-party apps via `window.__kirocrew_modules`; renaming/removing exports is a compat break. Mitigation: keep `app-sdk` exports as thin re-exports over chat-core.
- P2 touches every send path. Mitigation: receipt-contract tests run against old and new implementations before cutover; each surface's existing send tests pass unmodified as compatibility evidence (the rule every P2 slice has held to).
- Layer-4 adoption by an app surface couples app-sdk to the dashboard store until #8651 — see §4.1.

## 7. Open questions

1. Does chat-core live in `website/src/chat-core/` or become the new backbone of `app-sdk/`? *Landed: `website/src/chat-core/transport/` exists; app-sdk imports it.*
2. Should the renderer registry allow apps to register custom roles (plugin surface), or stay internal for now? (Leaning: internal first; plugin API is a separate RFC.)
3. P4 sequencing with the error-hand-off default-on decision — same PR series or separate?
4. ~~§4.1 — store-free composer seam vs host-only `ChatEmbed`~~ *Decided 2026-09-05: host-only now; seam is P3 model-layer work, #8651.*

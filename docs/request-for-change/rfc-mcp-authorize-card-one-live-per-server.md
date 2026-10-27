---
title: A chat shows one live MCP Authorize card per server; replaced and ended cards are removed, not greyed out
status: accepted
author: kyleseaman, with kirocrew-worker
created: 2026-10-27
last-audited: 2026-10-27
audited-at: 2ec42c9dc1
doc-pr:
implementation-prs: [22907, 24701]
tracking-issues: [24871]
supersedes: []
superseded-by: []
---

# RFC: One live MCP Authorize card per server

- Status: accepted. This is the product owner's decision, given by Kyle Seaman on 2026-10-26 and recorded on [#24871](https://github.com/kirodotdev/KiroCrew/issues/24871). Nothing is built on main yet. The document lands on its own first because the change removes a user-facing state, and the First Principles lane reads that decision off the base branch. Claims about today's code were checked at `2ec42c9dc1` (main).
- It replaces the "retired cards stay visible" rule that the fixes for [#7580](https://github.com/kirodotdev/KiroCrew/issues/7580) and [#7654](https://github.com/kirodotdev/KiroCrew/issues/7654) wrote into code comments. Neither was an RFC or a `docs/decisions/` entry, so `supersedes` stays empty.
- Implementation: [#22907](https://github.com/kirodotdev/KiroCrew/pull/22907) (retire a card once its sign-in attempt ends) and [#24701](https://github.com/kirodotdev/KiroCrew/pull/24701) (offer a KAS MCP sign-in only when the user names the server).

## Context

An MCP server that needs OAuth posts an Authorize card into the chat. The link works only while the sign-in attempt that minted it is alive: the engine owns the loopback callback listener, and the KAS engine keeps it open for one connect attempt (the server's connect timeout, 60 s by default). A card's attempt ends in one of three ways:

1. **Replaced.** A newer attempt for the same server posts a new card. The backend marks the old row `meta.superseded` (`_supersede_open_mcp_oauth_banners` in `src/kiro_crew/dashboard/chat_turn/mcp_session.py`).
2. **Timed out.** The attempt ends with no replacement yet; the next offer mints a fresh card. #22907 adds this state as `meta.expired_reason: attempt_ended`.
3. **Process gone.** A gateway restart or session reset kills the process that hosts the flow, and nothing will re-offer a link until the user sends a message. The read path marks the row `meta.expired` (`_expire_dead_child_oauth_meta` in `src/kiro_crew/dashboard/chat_utils_display.py`).

On main, `renderMcpOAuthMessage` in `website/src/pages/chat/McpOAuthBanner.tsx` renders every one of these as a grey notice and never hides a retired card. The comment above its gate pins that choice: "A vanished banner is the worse failure: it is indistinguishable from the sign-in having succeeded." The `superseded` notice tells the user to "Use the latest Authorize button" (#7580); the `expired` notice tells them to send a message for a fresh link (#7654).

With several unauthenticated servers, offers rotate about once a minute, so a chat collects a grey card per server per minute. The one live button is buried among notices about attempts the user never acted on.

## Decision

1. **A replaced card is removed from the transcript.** A row with `meta.superseded` renders nothing. The newer card for the same server is the one to use, and it is already in the chat.
2. **A timed-out card is removed from the transcript.** A row whose attempt ended (`expired_reason: attempt_ended`) renders nothing. The next offer for that server mints a fresh card.
3. **A chat shows at most one live Authorize card per server.**
4. **The dead-process recovery card stays.** A row that is `expired` because its hosting process is gone keeps its notice and its advice to send a message. Nothing else will offer a link in that case, so hiding it would leave the user with no way forward.
5. **A late success still shows.** If the engine connects after a card was retired, the backend flips the row to `completed` and the green authenticated card renders. A terminal outcome (`completed` or `failed`) wins over decisions 1 and 2.
6. **No transition is required.** The retired card unmounts and the new card appears where its offer arrives, as cards are appended on main today.

The `superseded` notice string and its translations go with decision 1. The `expired` string stays for decision 4.

## Consequences

- Users lose the visible history of failed or replaced attempts. A chat no longer shows that an earlier link existed and lapsed.
- A card that times out while the user is away disappears with nothing in its place. A fresh card for that server appears only if a later offer is made, and none may come; the authenticated card, not a missing card, is the success signal.
- The transcript stays short: one live card per waiting server, plus any dead-process recovery card.
- Review lanes that read the #7580 comment as a standing rule should cite this document instead.

## Alternatives considered

- **Keep greyed retired cards (main today).** It keeps an audit trail of every attempt and never leaves a gap. It was rejected because the trail is noise the user cannot act on: a retired link can never be redeemed, and with rotation the grey cards outnumber the live one.
- **Collapse retired cards into one history line per server** (for example "lennysdata sign-in timed out, a new link will appear here"). It keeps a trace without the clutter. It was rejected for now because it adds a new surface and copy to maintain for information the user does not need to finish signing in. It can be revisited if users report that a vanished card reads as success.

## Rollout

1. This document. Exit: it is on main with `status: accepted`.
2. #22907 and #24701 rebase onto a main that carries it, so their First Principles and UX lanes read it from the base. Exit: on main, `renderMcpOAuthMessage` returns `null` for a `superseded` row and for an `attempt_ended` row that is neither `completed` nor `failed`, still renders the `expired` notice for a dead-process row, and the `superseded` notice string is gone from every locale. When a card is removed and no later offer arrives for that server, the chat shows no card for it, and this outcome is accepted: with #24701 a card is offered only when the user's message names an unsigned server, so no follow-up offer is the normal case. To sign in, the user names the server in a message, which mints a fresh card; Settings -> MCP sign-in becomes a second path once #24693 lands.

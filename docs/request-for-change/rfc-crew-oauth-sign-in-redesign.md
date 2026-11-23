---
title: Crew-owned MCP sign-in, redesigned for first use, remote gateways and chat
status: draft
author: buluoray, with kirocrew agent session
created: 2026-11-16
last-audited: 2026-11-20
audited-at: c157748343
doc-pr: null
implementation-prs: [41513]
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Crew-owned MCP sign-in, redesigned for first use, remote gateways and chat

- Status: draft. Phase 3 (section 2) is built in [#41513](https://github.com/kirodotdev/KiroCrew/pull/41513); nothing else is.
- Builds on [rfc-crew-owned-oauth.md](rfc-crew-owned-oauth.md) (the gateway runs OAuth and holds the tokens) and its 2026-11-10 Slack amendment, shipped in [#34811](https://github.com/kirodotdev/KiroCrew/pull/34811). It changes how a person reaches and finishes that sign-in. It does not change custody, the bridge or the proxy gate.
- Per-person Connection tokens in a shared Crew (this RFC's former section 7.4 and Phases 5a and 5b) moved to their own RFC, `rfc-crew-oauth-per-person-connections.md`, proposed in [#39049](https://github.com/kirodotdev/KiroCrew/pull/39049).
- Measured at `origin/main` `3c9f33cdc80`; re-audited 2026-11-18 at `224959b4ade`. Paths are under `src/kiro_crew/` or `website/src/` unless they start with `docs/`.

## Summary

The repository owner set up Slack through **Settings → Connections** on a
remote gateway: a server with no browser, reached from a laptop. Every step
worked in the end, but seven things went wrong on the way. This RFC fixes each
one:

| # | What went wrong | Fix |
|---|---|---|
| 1 | Connect only appears after a preview switch in another tab | Turn the switch on from the card, in one click |
| 2 | Making the Slack app takes five pages | One **Create the Slack app** button that opens the shipped manifest link, with the MCP settings from #38169 |
| 3 | Connect opened no tab; **Re-open approval** was needed | Keep the tab open from the click; never close it mid-sign-in; make the approve link the main button when no tab is held |
| 4 | After **Allow**, the browser says "This site can't be reached" | Know the gateway is remote before Connect; show the paste box at once; use an https return address when the gateway has one |
| 5 | "Return address" means nothing to people | Plain words: "Copy the whole address from the browser bar and paste it here" |
| 6 | Card said Connected, first tool call failed | Say Connected only after `tools/list` answers and every needed scope was granted; refresh open chats |
| 7 | The person has to leave the chat to connect | The agent can show the same Connect card inside the chat |

The agent may only **show** the card in item 7. It never sees the client
secret, the code or a token, and only a person's own click starts their own
sign-in. Per-person tokens in a shared Crew are proposed separately in
[#39049](https://github.com/kirodotdev/KiroCrew/pull/39049).

## Motivation

### Current state

How the Slack card behaves on main today:

- **The preview switch.** Crew does the Slack sign-in only when
  `dashboard.feature_previews.crew_oauth` is on and chats run on a bridged
  backend (`_crew_sign_in_off_slugs` in `connections/status.py`). Until then the
  card shows a button, **Turn on Kiro Crew sign-in**, that navigates to the
  Feature previews row (`crewSignInSetup` in
  `pages/connections/ConnectionsPageCard.tsx`, the `Link` to
  `settingsPath({ tab: 'developer', sub: 'feature-previews', ... })`). It does
  not turn anything on. The person must find the switch, flip it, and come back.
  This link landed with #34811 today, so the walk-through may have run on a
  build without it. Either way, it is two pages and a hunt for one row.
- **The Slack app.** `docs/guides/oauth-app-registration/slack.md` sends the
  person through five places: create the app, the Agents MCP toggle, PKCE on
  **OAuth & Permissions**, the redirect URL, and 18 user scopes added one by
  one. The guide warns that PKCE must be on before the `localhost` redirect.
- **The Slack manifest already exists.** `slack_manifest.py` is the one
  source of the Slack app manifest. Its `deep_link()` returns a prefilled
  `https://api.slack.com/apps?new_app=1&manifest_yaml=<encoded>` create link,
  served by `kirocrew manifest` and `GET /api/slack/manifest`, and the exfil
  redactor exempts only that exact rebuilt link. It makes the Slack channel
  app. Open [#38169](https://github.com/kirodotdev/KiroCrew/pull/38169) adds an opt-in switch that puts Slack's MCP setting,
  PKCE, the `localhost:48106` redirect and the MCP user scopes into the same
  manifest.
- **Opening consent.** The gateway never opens a browser for this sign-in. The
  card's click opens a blank tab at once (`window.open('', '_blank')` in
  `startMint`) and fills it when the poll brings the URL. When the browser
  refuses that tab, the card falls back to a small **Re-open approval** link
  (`clickTab === 'refused'`). On the desktop app the URL goes to the system
  browser when it arrives. No path in `connections/` calls `webbrowser` or
  `xdg-open`.
- **The return trip.** The Slack app's redirect is
  `http://localhost:48106/callback`. `SignIn._bind` in `connections/crew_oauth.py`
  listens there on `127.0.0.1`, on the gateway host only, for one consent
  (600 s). A browser on another machine lands on its own `localhost`, where
  nothing listens. The waiting card already has a paste box
  (`remote_gateway_help`, `return_address`), and
  `POST /api/mcp/oauth/relay` replays the pasted address to the gateway's own
  listener. The box shows only after Connect, under a label that reads
  "Return address".
- **The https return route.** DCR cards already avoid the paste when the
  gateway has a public HTTPS `dashboard.url`: they register
  `https://<host>/api/connections/oauth/callback` instead
  (`_public_origin` and `redirect_uri` in `connections/crew_oauth_dcr.py`).
  Pre-registered cards (Slack, GitHub, Asana, Google) never use it.
- **Connected.** The card reads Connected as soon as a token pair is in the
  vault (`holds_grant`, `grantPresent` in `connections/status.py`). Nothing
  calls the vendor with the new token first. The card's Connect starts the
  sign-in with no `on_granted` hook (`dashboard/handlers/connections.py`, the
  `crew_owned` branch), so open chats are not told to pick up the bridge. The
  owner-added path does pass one (`_mark_live_sessions_for_new_bridge` in
  `dashboard/handlers/connections_custom_oauth.py`).
- **Cards in chat.** A change-card kind for this already exists:
  `connection.connect` (`change_card_catalog_kinds.py`). Its plan posts
  `/api/connections/mint`, polls it, and shows the consent link
  (`onRepeat` in `cards/ChangeCard.tsx`). It reads "connected" from
  `grantPresent` and fingerprints the grant by stat-ing the kiro-cli token file
  (`_connection_grant_fingerprint` in `dashboard/change_cards_readers.py`),
  which a Crew-owned grant does not have. It has no paste box. Change cards
  render only for a turn sent from the dashboard; a channel message, a
  subagent or a scheduled run is refused (`_OFF_DASHBOARD_CODES` in
  `mcp_guide.py`).
- **Who may sign in.** The Connection routes call
  `require_owner_dashboard_request` (`dashboard/handlers/connections.py`).
  Since [#38209](https://github.com/kirodotdev/KiroCrew/pull/38209) and
  [#38437](https://github.com/kirodotdev/KiroCrew/pull/38437), that gate also
  admits a Shared Crew teammate (`teammate_passes_owner_gate` in
  `shared_crew/teammate_access.py`). So in a shared Crew the owner and
  teammates can all start a sign-in, and they share the Crew's one grant.
- **A call without a grant.** The proxy answers a bridged call with no stored
  pair as `409 needs_auth`, "not connected: reconnect it on the Connections
  page" (`connections/crew_oauth_proxy.py`). The agent sees that text and
  nothing else.

### Problems

1. Two settings pages stand between a new user and the first Connect.
2. Five manual app steps, one of them in an order that is easy to get wrong.
3. When the browser blocks the pre-opened tab, the only way on is a small
   link that does not look like the next step.
4. On a remote gateway, the step after **Allow** always looks like a failure,
   and the help appears only after it.
5. The label for the paste box is OAuth jargon.
6. Connected can be shown for a sign-in that cannot make a call yet. The
   chat where the person then tries it may still hold the old entry.
7. The agent knows a connection is missing but can only send the person to
   Settings.

## Goals

- A new user on a local gateway goes from a fresh Slack card to a working
  Slack tool call without opening another settings page.
- A remote gateway's user is told before **Allow** what the next page will
  look like, and has the paste box ready.
- Connected means a real `tools/list` with the stored token answered, and
  the token holds every scope the provider needs.
- An agent can put the same Connect card in the chat where it needs the
  connection, without ever touching a secret.
- Every fix has a UI path. No step needs a terminal.

## Non-goals

- No change to the bridge or the proxy approval gate, and no new custody
  model. [rfc-crew-owned-oauth.md](rfc-crew-owned-oauth.md) owns those.
- No per-person tokens. [#39049](https://github.com/kirodotdev/KiroCrew/pull/39049) proposes them.
- No fix for item 6's "The user rejected this tool call". Its cause is the
  approval option id Crew sent KAS, fixed in
  [#38117](https://github.com/kirodotdev/KiroCrew/pull/38117) (section 6).
- No new built-in app, and no per-vendor code beyond registry data.
- No running a person's harness under their own OS account. That is
  [rfc-per-person-execution.md](rfc-per-person-execution.md).
- No default-on for the preview flag. That stays with the parent RFC's
  security sign-off (its step 3g).
- No change to the kiro-cli native sign-in path.

## Design

### 1. Turn Crew sign-in on from the card

The card already knows why it cannot connect: `crewMintRefused` is
`sign_in_off`. Today that shows a navigation link. It becomes an action:

- **One switch on the card.** In place of the link, the card shows one
  checkbox-style button: **Let Kiro Crew handle the Slack sign-in**. One plain
  line under it says what it does: "Kiro Crew keeps the sign-in for you and
  passes it to your chats. You can turn this off in Settings → Developer."
- **What the click writes.** The same config write, behind the same gate, that
  the Feature previews row uses (`setPreviewFlag(PREVIEW_CREW_OAUTH, true)`).
  No new route.
- **Two blockers it cannot clear alone.**
  - The preview main switch is off (`previewsOff` in
    `FeaturePreviewsSection.tsx`), or an edition lock pins the flag
    (`edition_locks.feature_previews`). The card then keeps today's link and
    names the reason. It never flips a main switch or a lock from a card.
  - Chats do not run on a bridged backend. The card says which backend chats
    use and links to that setting, as `needs_crew_sign_in_help` does today.
- **After the click**, the status poll runs at once, and the card shows
  **Connect** in the same place.
- **Live sessions.** Turning the flag on marks every live session for
  re-projection, the same mark the owner-added path uses after a grant.
  That way an open chat does not keep the harness's own `slack` entry.

### 2. Make the Slack app with one manifest

Slack's manifest schema
([reference](https://docs.slack.dev/reference/app-manifest/), read
2026-11-16) has a field for each of the five manual steps:

| Manual step today | Manifest field |
|---|---|
| Agents → MCP toggle | `settings.is_mcp_enabled: true` |
| OAuth & Permissions → PKCE | `oauth_config.pkce_enabled: true` |
| Redirect URL | `oauth_config.redirect_urls: ["http://localhost:48106/callback"]` |
| 18 user scopes | `oauth_config.scopes.user: [...]` (the registry's `recommended_scopes`) |
| No bot scopes | leave `oauth_config.scopes.bot` out |

This RFC builds no second generator. The MCP fields come from
`slack_manifest.py` once [#38169](https://github.com/kirodotdev/KiroCrew/pull/38169) adds them, so one manifest serves the
Slack channel and the Slack Connection.

- **The card.** The Slack card's "Before you connect" tip gets a
  **Create the Slack app** button that opens `slack_manifest.deep_link()`
  with the MCP switch from [#38169](https://github.com/kirodotdev/KiroCrew/pull/38169) on, and a **Copy manifest** button for
  the same text. One line goes with them: "Slack opens with the app filled
  in. Pick your workspace and click **Create**." No vendor name appears in
  card code; the link comes from the gateway.
- **No new registry field.** The manifest is not built from the registry,
  so `app_manifest` and `redirect_urls_extra` are not added. The https
  return address (section 4.3) stays a manual redirect the person adds.
- **Phase 0 verdict, 2026-11-19: Slack accepts it.** Slack's docs say a
  `localhost` redirect counts as a desktop redirect only "if the app has
  opted into PKCE", and do not say whether `pkce_enabled` may arrive in the
  same manifest as the redirect. The repository owner tested it in a test
  Slack workspace with [#38169](https://github.com/kirodotdev/KiroCrew/pull/38169)'s
  manifest, `slack.slack_mcp` on, and posted the results on
  [#38169 (2026-11-19)](https://github.com/kirodotdev/KiroCrew/pull/38169#issuecomment-7016161038):
  Slack created the app from that one manifest with PKCE on, the
  `localhost:48106` redirect and the bot scopes, with no error; **Install to
  Workspace** issued the `xoxb-` bot token and the bot answered a DM; and
  **Connect** on the Slack card succeeded and a chat read the workspace
  through Slack's MCP server. The review lane accepted these results on
  that PR's head `674fb0f0`. So the card needs no extra PKCE or redirect
  step.
- **Bot scopes.** The table's last row assumed a Connection-only app. With
  one app for the channel bot and the Connection, the manifest keeps the
  bot's scopes, and the Connection asks only for user scopes when it signs
  in (`https://slack.com/oauth/v2_user/authorize`), which is the shape the
  Phase 0 run proved.
- **Steps that stay manual**, each with one cropped picture in the guide:
  copying the Client ID and Client Secret from **Basic Information**, and an
  admin approval where the workspace needs one. The pictures live under
  `docs/guides/oauth-app-registration/assets/` and are linked from the tip.

### 3. A consent tab that never stalls

What the owner saw, in a browser on a Mac (not the desktop app): Connect
opened nothing, not even a blank tab. **Re-open approval** then opened the
consent page. That is a lost pop-up. **Re-open approval** is a plain link
the person clicks, so no pop-up blocker stops it.

The usual cause, `window.open` called after an `await`, is not what main
does. `startMint` already opens `about:blank` in the click handler, before
its first `await` (`window.open('', '_blank')`), and sets the tab's
`location` when the poll brings the URL. So on main the tab is lost in one
of two ways:

| Way the tab is lost | Where in the code | What the card shows |
|---|---|---|
| The browser refuses even the synchronous open (a strict pop-up blocker or setting) | `window.open` returns `null`; `clickTab` becomes `refused` | the small **Re-open approval** link |
| The card closes the blank tab it opened | the reclaim effect closes a held tab whenever the card is neither busy with `connect` nor `waiting-for-approval`; one render in that gap during the mint closes it | the waiting card, once the state settles, with the link |

Both end where the owner was, and the walk-through cannot tell them apart.
Phase 1 first reproduces the click in a Mac browser, with the poll timing
recorded, to name the one that happened. The fix covers both:

- **Keep the synchronous open.** The click handler keeps opening
  `about:blank` before any `await`, then sets its `location` once the URL
  arrives. A test pins that order: no `await` and no state update that
  re-renders comes before `window.open` in `startMint`.
- **Close the held tab only on an end state.** The reclaim effect closes
  the blank tab only after Cancel, `failed`, `expired`, a refused mint
  request or an unmount. A render in the middle of the mint never closes it.
- **The approve link is the main button whenever no tab is held.** When
  `clickTab` is `refused`, or the held tab is gone before the URL comes, the
  waiting card shows **Open Slack to approve** as its primary button, in the
  place Connect was. That is a plain link, so it works under any blocker.
  The small **Re-open approval** link stays for the other states.
- The blank tab's holding text names the provider: "Opening Slack…", not
  "Connecting".
- A test pins that no sign-in route calls a server-side browser opener.
  `cli_open.open_url` and `webbrowser` stay out of `connections/`.

### 4. Remote gateways: warn first, paste at once, or skip the paste

#### 4.1 Know the gateway is remote

No single signal is reliable, because a tunnel can make a remote gateway
look local (`localhost:<port>` in the address bar). The card uses any of
these, from strongest to weakest:

1. **Reachability probe.** When Connect starts, the listener is up. The tab
   sends `fetch('http://localhost:48106/reachable', { mode: 'no-cors' })` to a
   new fixed route on that same listener, which answers 204 with no body.
   A network error means the browser cannot reach the gateway's listener:
   remote. An opaque answer means reachable: local. The route reads no
   query, changes no state and is gone with the listener. It is not the
   callback path, so no `state` is ever tried.
2. **Dashboard origin.** The tab's own host is not loopback
   (`window.location.hostname`), so the redirect lands on another machine.
3. **Gateway hint.** The status answer carries `gatewayHeadless: true` when
   the gateway host has no display on Linux, the same test
   `_browser_open_supported` uses in `cloud/connect.py`.

When the probe and the dashboard origin disagree, the dashboard origin
wins: a non-loopback dashboard host means remote, even if the probe got an
answer. The probe can be wrong the other way: the person's own laptop may
run a Crew with a pending sign-in on the same port. If the probe fails (a
browser that blocks loopback fetches, see Security), signals 2 and 3 still
decide. With no signal at all, the card stays in local
mode, and the paste box is one click away under "Did the last page say it
can't be reached?"

#### 4.2 Warn before Allow, show the box at once

In remote mode the card says, before the person leaves:

> After you click **Allow** in Slack, your browser shows "This site can't be
> reached". That is expected. Copy the whole address from the browser bar and
> paste it here.

The paste box is open at once, under that text. Nothing else about the relay
changes: `POST /api/mcp/oauth/relay` stays loopback-only and delivers only
to a pending sign-in, and
a spent address keeps the
[2026-11-07 decision](../decisions/2026-11-07-a-connections-card-names-its-own-next-step-for-a-spent-approval.md)
(the card names Cancel, then Connect again).

#### 4.3 Skip the paste when the gateway has an https address

When `dashboard.url` is a public HTTPS origin, a pre-registered card can
return to the gateway's existing route `GET /api/connections/oauth/callback`,
as DCR cards already do. Then the browser lands on the gateway and the card
turns Connected with no paste.

- **Opt-in per provider.** The provider must accept that https redirect, and
  the person must add it to their app. The card shows the extra redirect next
  to the localhost one, with its own **Copy**, and the manifest carries both
  (`redirect_urls` takes up to 1000).
- **Which one Crew sends.** Crew sends the https redirect only when the card
  records that the person added it (a checkbox: "I added this address to my
  Slack app"). Otherwise it sends the loopback one. Crew cannot read the
  vendor's app settings, and sending an unregistered redirect fails at the
  vendor.
- **Route rules stay as written** in the parent RFC's 2026-11-09 amendment:
  admitted only by a pending `state`, single use, 600 s, fixed page,
  `no-store`, redirect host from config and never from the request `Host`.

What each pre-registered provider accepts, from the vendor docs:

| Provider | Loopback return | https return | Several redirects | Source |
|---|---|---|---|---|
| Slack | `http://localhost` only, and only with PKCE on | yes | yes | [Using PKCE](https://docs.slack.dev/authentication/using-pkce), [manifest](https://docs.slack.dev/reference/app-manifest/) |
| GitHub (OAuth app) | `127.0.0.1` with any port, path must match | yes | one callback URL; loopback port is free | [Authorizing OAuth apps](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps) |
| Google (Web client) | `http://localhost` / `127.0.0.1` | yes | yes | vendor docs, not re-read for this RFC |
| Asana | not re-read | yes | not re-read | vendor docs, not re-read for this RFC |
| DCR cards | Crew registers it | Crew registers it | n/a | `crew_oauth_dcr.redirect_uri` |

GitHub's single callback URL means a GitHub OAuth app must pick the loopback
or the https return, not both. Phase 0 re-reads the Google and Asana rows.

### 5. Plain words on the paste box

| Key | Today | New |
|---|---|---|
| `return_address` | Return address | Address from the browser bar |
| `remote_gateway_help` | "If the final browser page could not connect, that is expected when your gateway is on another machine. Copy the full address from the browser and paste it here." | "After you approve, your browser may say the page can't be reached. That is normal here. Copy the whole address from the browser bar and paste it here." |
| `return_address_placeholder` | `http://127.0.0.1:PORT/…?code=…` | `http://localhost:48106/callback?code=…` (the provider's own redirect, so it looks like what they will see) |
| `invalid_return_address` | "Paste the full http://127.0.0.1:PORT/…?code=… address from your browser." | "That doesn't look like the address Slack sent you to. It starts with http://localhost." |

Slack stands in for `{{provider}}` above; the real strings take the
provider's name and its own redirect.

The chat banner and the MCP Servers row use `OAuthRelayAffordance` with their
own keys (`pages.chat.mcpOAuthBanner.remote_gateway_hint` and its siblings).
They get the same words, so every surface says the same thing. All locales
are updated in the same PR.

### 6. Connected means it works

- **A real check after the grant.** When the code swap stores the pair, the
  gateway sends `initialize` and `tools/list` to the provider's `mcp_url` with
  the new access token, from the gateway, with the same vetted egress the
  proxy uses. `tools/list` proves the token authenticates, not that its
  scopes work: Slack can list tools and still answer `missing_scope` on a
  call. So the check also compares the scopes the token response granted
  (the stored pair's `scope`, from `tokens_from_response` in
  `connections/crew_oauth.py`) with the registry's `recommended_scopes`.
- **No tool call.** The check makes no `tools/call`. `connections.md`
  ("The sweep never calls a tool") rules that out: a call from the
  gateway would skip the tool approval, the policy and the SEL record.
  The registry's `smoke_fixture` is used for its name only, as in the
  sweep: `tools/list` must still list it.
- **The result.** The mint row goes to `granted` only when `tools/list`
  answers with at least one tool, lists the `smoke_fixture` tool, and
  every recommended scope was granted. When the token response names no
  scopes, the card says "Connected. Scopes not reported by the provider."
  Otherwise it goes to `failed`
  with a reason the card already knows how to show (`mint_grant_unproven`),
  or a new `vendor_refused` with the vendor's own short error (for example
  Slack's `missing_scope`, or an IP allowlist refusal). The pair stays
  stored, so a retry does not need a new consent.
- **The badge.** Between the grant and the check the card shows "Checking…".
  The 30-second status poll stays a cheap read, as
  `connections.md` "A grant on disk is not a grant that works" requires. The
  check runs once per sign-in, at the moment the click asked for it.
- **Open chats.** The card's Connect passes the same `on_granted` hook as the
  owner-added path, so live sessions re-project and mount the bridge without
  a reset. A session whose harness cannot re-project mid-chat is listed by
  name under the Connected badge: "Open chats started before this sign-in:
  start a new chat to use Slack there." The list comes from the same marks,
  so it is true, not a guess.
- **The tool call that came back "rejected".** The open-chat gap did not
  cause it. Crew's approval answer did. For a Connection server, Crew turns
  an "allow always" answer into "allow once" (`once_for_always`, from
  [#33597](https://github.com/kirodotdev/KiroCrew/pull/33597)). On KAS it
  answered with the id `allow_once`, which KAS had not offered on that ask.
  KAS reads an unknown option id as a refusal and reports "The user rejected
  this tool call." The fix is
  [#38117](https://github.com/kirodotdev/KiroCrew/pull/38117): answer with an
  option id KAS offered. KAS's misleading wording for an unknown id is
  [#38118](https://github.com/kirodotdev/KiroCrew/issues/38118). Neither is
  part of this RFC. The Connected check and the open-chat refresh above stay:
  they fix two other gaps, a Connected badge with no working call and an
  open chat that keeps the old entry.

### 7. The agent can show the Connect card in chat

#### 7.1 Use the existing kind

`connection.connect` already exists and already does the safe thing: the
agent sends only `{slug}`, the gateway builds the plan, and a person's
**Connect** press runs it for that person. This RFC extends that kind. It
does not add a tool or a second kind.

Changes to the kind:

- **The right grant.** `read_state` reads `crew_oauth.holds_grant(slug)` and
  the vault revision of the token entry when Crew owns the slug, in place of
  the kiro-cli file stat. Undo (Disconnect) then refuses a grant newer than
  the one the card made, as it does today for kiro-cli.
- **The same card body.** The chat card renders the Connections card's
  waiting body: the consent button (section 3), the remote warning and paste
  box (section 4), and "Checking…" then Connected (section 6). One shared
  component, not a copy.
- **What the card can say before Connect.** If the provider needs an app
  first (`client_not_configured`) or Crew sign-in is off (`sign_in_off`), the
  chat card shows that same next step with a link to the Slack card in
  Settings → Connections. It does not take the Client Secret in chat.
- **Reconnect.** `connected` is no longer a `no_change` refusal when the
  grant is `needs_auth` or within its expiry warning (section 7.3). The
  card's title says "Reconnect Slack".

#### 7.2 Who raises it, where

| Where | What happens |
|---|---|
| Dashboard chat, a person's turn | The agent calls `propose_change` with `connection.connect`. The card shows inline. |
| A Bot chat (the conductor's own chat) | Same as above. The Bot raises it when a worker reports that it needs a connection. |
| A Bot's worker, a subagent, a scheduled run | No dashboard turn, so the card is refused, as today. The worker reports `blocked` with the provider name. Its Bot raises the card in its own chat. |
| Side chats (a chat in the side panel) | Same as a dashboard chat when the turn came from the dashboard. |
| Slack, Discord and other channels | Cards do not render there. The gateway answers with one line it builds itself, addressed to the person whose message needs the connection: "Slack isn't connected for you. Connect it in Kiro Crew: Settings → Connections." It adds the link to `<dashboard.url>/settings/connections#connection-slack` only when `dashboard.url` is set. The agent never writes the link. |
| A shared Crew | Whoever today's gate admits (the owner and teammates) can press **Connect**, and the Crew keeps one grant. Per-person tokens are [#39049](https://github.com/kirodotdev/KiroCrew/pull/39049). |

#### 7.3 The gateway raises it too

The agent cannot always tell why a call failed. So the proxy's
`needs_auth` answer becomes a trigger the gateway acts on, not text for the
agent:

- When the proxy refuses a bridged call with `needs_auth`, and the session's
  current turn came from the dashboard, the gateway raises the
  `connection.connect` card in that chat itself, for the person driving the
  turn. The agent still sees the same refusal text, plus one
  line: "A Connect card is shown to the user."
- At most one open card per chat, provider and person. A second refusal
  while one is open does nothing.
- **Expiry.** Slack refresh tokens die 30 days after consent when PKCE is on.
  A new optional registry field, `refresh_lifetime_days: 30`, plus the
  stored consent time, let the gateway warn three days before: that person's
  Connections card shows "Sign-in ends in 3 days" with **Reconnect**, and a
  bell notice to that person links to it. After expiry the first
  `invalid_refresh_token` marks that person's grant `needs_auth`, and their
  next call raises the card as above.

#### 7.4 Per-person tokens moved

Each person connecting their own account in a shared Crew, and a call
spending only the token of the person the run is for, are proposed in
their own RFC, `rfc-crew-oauth-per-person-connections.md` ([#39049](https://github.com/kirodotdev/KiroCrew/pull/39049)). That
RFC changes token custody and identity, which this RFC's goals exclude.
Until it lands, this RFC keeps the Crew's one grant per provider.

## Migration plan

Each phase is one PR, in this order. Every PR keeps the preview flag
default-off and changes nothing for a user who never turns it on.

| Phase | PR | Exit criteria |
|---|---|---|
| 0 | Probe, no code: create a Slack app from the section 2 manifest on a throwaway workspace; re-read Google and Asana redirect rules. Verdict written into this RFC. | The RFC states whether Slack accepts `pkce_enabled` and the `localhost` redirect in one manifest, with the date and the error text if not. |
| 1 | Plain words (section 5) and the consent tab fixes (section 3): reproduce the lost tab in a Mac browser first, then the reclaim fix and the primary approve link. Frontend and locales only. | The waiting card shows the new strings in every locale. A test pins `window.open` before any `await` in `startMint`. A render during the mint never closes the held tab. A refused or lost tab shows **Open Slack to approve** as the primary button. A test pins no server-side browser opener under `connections/`. |
| 2 | One-click enable on the card (section 1), and live-session marks when the flag turns on. | With previews on and a bridged backend, one click on the Slack card shows **Connect** with no page change. With previews off or locked, the card shows the link and the reason. |
| 3 | Create and copy the Slack app (section 2): the tip's buttons from `slack_manifest.deep_link()` with the MCP switch on; guide pictures. Blocked on [#38169](https://github.com/kirodotdev/KiroCrew/pull/38169) merging and on Phase 0 for the redirect line. | The button opens Slack's create page with the MCP settings, PKCE, the redirect and the registry's user scopes, and no bot scopes for the Connection. The guide's section 1 to 3 shrink to that step plus the manual steps. |
| 4 | Remote mode (section 4.1, 4.2): the `/reachable` route on the sign-in listener, `gatewayHeadless` in the status answer, the warning and the open paste box. | On a gateway the browser cannot reach, the paste box is open before **Allow**. On a local gateway it is closed. The route answers 204 only while a sign-in is pending. |
| 5 | Verified Connected (section 6): the post-grant `tools/list` and granted-scope check, "Checking…", `on_granted` on the card path, the open-chats line. | A grant whose `tools/list` fails, or that lacks a recommended scope, never shows Connected. An open KAS chat can call a Slack tool after Connect with no reload. |
| 6 | Chat card (section 7.1, 7.2): `connection.connect` reads the Crew's grant, renders the shared waiting body, channel fallback line. | In a dashboard chat the agent's card connects Slack end to end, paste box included. On a channel the reply carries the gateway's line and no card. |
| 7 | Gateway-raised card and expiry (section 7.3). | A `needs_auth` refusal in a dashboard turn shows one card. A grant 3 days from its refresh lifetime shows the warning. |
| 8 | Optional: https return for pre-registered cards (section 4.3). Blocked on open question 6. | With an https `dashboard.url` and the checkbox set, Slack returns to the gateway and the card connects with no paste. |

Phase 1 needs nothing else. Phases 2 to 5 can go in any order after it.
Phase 6 needs Phases 1, 4 and 5, because it reuses their card body. Phase 7
needs Phase 6. Per-person tokens ([#39049](https://github.com/kirodotdev/KiroCrew/pull/39049)) come after Phase 5 and change what
Phase 6's card reads.

## Backward compatibility

- A user with the flag off sees today's cards, plus the new wording.
- A Slack app made by hand keeps working: the redirect is the same.
- An existing grant stays Connected. The post-grant check runs only on a new
  sign-in. **Test** keeps its behaviour.
- `connection.connect` keeps its parameters (`{slug}`). Old cards in history
  still render. Only the state read and the body change.
- The 2026-11-07 decision on a spent paste is kept as written.
- The paste box still accepts the old `127.0.0.1` spelling.

Reader list for what this changes:

- The **Return address** label is renamed. Readers: the card, the chat
  banner, the Slack guide and the GitHub, Google and Asana guides that name
  it. All are updated in Phase 1.
- Connected now waits for a check. Readers: the card, `connection.connect`'s
  `poll_outcome`, and the Connections spec. Updated in Phase 5.

## Security considerations

This touches routes and sign-in. The checks below follow the repository's
security model and an exposure review of each change.

- **No new listener, no wider bind.** The sign-in listener stays on
  `127.0.0.1`, the registered port, for one consent. The `/reachable` route
  lives on that same listener and dies with it. It answers 204 with no body
  and no CORS headers, reads no input, and changes no state. A page on
  another site can learn that a sign-in is pending on this machine for up to
  600 s. That reveals no secret and no account. Browsers that block a public
  page's loopback fetch (Private Network Access) make the probe fail, and the
  card falls back to the other two signals.
- **The https return** reuses the existing state-gated route with its
  existing rules. It is off unless the app's https address is registered and
  the box is ticked. The redirect host comes from `dashboard.url`, never from
  a request.
- **The one-click enable** is the same config write as the Settings row,
  behind the same gate, which on main also admits Shared Crew teammates. It cannot clear the preview main switch or an
  edition lock. It widens nothing beyond what the Settings row already
  allows.
- **The agent's card.** The agent's input is `{slug}`, validated against the
  registry pattern. It sees no client id, secret, code, verifier or token:
  the plan is built by the gateway, the consent URL is sent to the
  pressing person's browser (in a shared Crew, any admitted teammate can
  also read a pending URL from `GET /api/connections/mint` today; [#39049](https://github.com/kirodotdev/KiroCrew/pull/39049)
  scopes that to the person who started the sign-in), and the paste goes from that browser to the
  relay route, which delivers only into a sign-in the same person started.
  The agent cannot press Connect, and a channel or worker turn cannot raise
  the card at all. The channel fallback is a gateway-built line with a
  gateway-built link, so a model cannot plant a URL in it.
- **The gateway-raised card** is bounded to one open card per chat,
  provider and person, and only on a dashboard turn. A prompt-injected agent
  can at most cause the same card the person could already open from
  Settings.
- **Per-person tokens** are out of scope here; [#39049](https://github.com/kirodotdev/KiroCrew/pull/39049) covers their
  security.
- **The post-grant check** calls only the registry's `mcp_url`, with the
  same SSRF vetting and no redirects as the proxy. It sends `initialize`
  and `tools/list` only, never a `tools/call`, and reads the granted scopes
  from the token response it already has. It logs one SEL event, as the
  sign-in does.
- **The manifest** carries no secret. It comes from the shipped
  `slack_manifest.py`, so the redactor's one exempt link stays the only
  manifest link. It asks for the same user scopes as
  today and no bot scopes.
- **Unchanged:** custody, the bridge, the proxy approval gate's binding to
  session, server, tool and arguments, and the relay's loopback-only
  target.

## Alternatives considered

- **Open the consent page from the gateway** (`webbrowser.open`). Rejected:
  it opens on the gateway host, which is the wrong machine for every remote
  user. The card already opens it from the person's tab.
- **A new tool, `connection_offer`, in place of the card kind.** Rejected:
  `connection.connect` already has the click-to-run plan, Undo, and the
  dashboard-only rule. A second path would need all three again.
- **Let the agent finish the sign-in** (paste the address in chat for the
  agent to relay). Rejected: the address holds a live code. It must go from
  the browser to the relay route, never through the transcript.
- **Always show the paste box.** Rejected as the default: a local user would
  see a box they never need. It stays one click away in local mode.
- **Detect remote from the dashboard host only.** Rejected: an ssh tunnel
  makes a remote gateway's dashboard read `localhost`. The probe catches it.
- **Turn the preview flag on by default.** Out of scope: the parent RFC ties
  it to a security sign-off.
- **A second Slack manifest generator** built from the registry. Rejected:
  `slack_manifest.py` already makes the manifest and its prefilled create
  link, and [#38169](https://github.com/kirodotdev/KiroCrew/pull/38169) adds the MCP fields to it. A second generator would mean
  two designs for one app, and the redactor would block its link.
- **Name `connection.connect` in the `needs_auth` text** so the agent raises
  the card itself, in place of the gateway raising it (section 7.3). Simpler,
  but it relies on the agent reading the text and acting on it, which is the
  gap section 7.3 closes. Weighed in open question 2.

## Open questions

For the repository owner.

1. **One-click enable (section 1).** May the Slack card turn on the Crew
   sign-in preview with one click, or should it only link to the switch until
   the flag is default-on? Recommendation: one click, behind the Settings
   row's own gate, with the
   plain line naming where to turn it off.
2. **Gateway-raised card (section 7.3).** May the gateway raise the Connect
   card on its own after a `needs_auth` refusal, or only when the agent asks?
   Recommendation: the gateway raises it, once per chat and provider,
   because the agent often cannot tell why the call failed. The simpler
   option, naming `connection.connect` in the refusal text, is listed under
   Alternatives.
3. **Shared Crew.** Moved with section 7.4 to
   [#39049](https://github.com/kirodotdev/KiroCrew/pull/39049), which lists
   the repository owner's rulings for a maintainer to confirm.
4. **Expiry warning (section 7.3).** Warn 3 days before Slack's 30-day end,
   on the card and with one bell notice? Recommendation: yes, 3 days.
5. **Remote probe (section 4.1).** Is a fixed `/reachable` route on the
   sign-in listener acceptable? Recommendation: yes; it lives only while a
   sign-in is pending and returns nothing. Accepted in review, with one
   change: when the probe and the dashboard origin disagree, the dashboard
   origin wins (section 4.1).
6. **https return for pre-registered cards (section 4.3).** Worth building
   (Phase 8), given that it needs the person to add a second redirect?
   Recommendation: build it after Phases 1 to 7, for pods and HTTPS tailnet
   gateways.
7. **Item 6's cause.** Settled: the rejection is the approval option id,
   fixed by [#38117](https://github.com/kirodotdev/KiroCrew/pull/38117), with
   KAS's wording tracked in
   [#38118](https://github.com/kirodotdev/KiroCrew/issues/38118). It is not
   the open-chat gap. No decision is left here; Phase 5 keeps its scope.
8. **The tab during the walk-through.** Answered: in a browser on a Mac,
   Connect opened no tab at all, and **Re-open approval** worked. Section 3
   now treats it as a lost pop-up. Main already opens the tab before any
   `await`, so Phase 1 first names which of section 3's two ways lost it,
   then ships both fixes. No decision is left here.
9. **Prefilled Slack link.** Answered: `slack_manifest.py` already makes
   one, and section 2 uses it with [#38169](https://github.com/kirodotdev/KiroCrew/pull/38169)'s MCP switch.
10. **A person who leaves a shared Crew.** Moved to [#39049](https://github.com/kirodotdev/KiroCrew/pull/39049). Accepted in
    review: revoking a person also deletes and revokes their Connection
    tokens, in that RFC's Phase A.
11. **Connected means it works (section 6).** Accepted in review, in a
    changed form: `tools/list` alone does not prove the scopes work, so the
    check also compares the granted scopes with the registry's. It makes no
    real read call, because the gateway never calls a tool on its own: that
    call would skip the tool approval and the SEL record.

---
title: Crew-owned MCP sign-in, redesigned for first use, remote gateways and chat
status: draft
author: buluoray, with kirocrew agent session
created: 2026-11-16
last-audited: 2026-11-16
audited-at: 3c9f33cdc80
doc-pr: null
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Crew-owned MCP sign-in, redesigned for first use, remote gateways and chat

- Status: draft. Nothing is built, and this document changes no code.
- Builds on [rfc-crew-owned-oauth.md](rfc-crew-owned-oauth.md) (the gateway runs OAuth and holds the tokens) and its 2026-11-10 Slack amendment, shipped in [#34811](https://github.com/kirodotdev/KiroCrew/pull/34811). It changes how a person reaches and finishes that sign-in. It does not change custody, the bridge or the proxy gate.
- Measured at `origin/main` `3c9f33cdc80`. Paths are under `src/kiro_crew/` or `website/src/` unless they start with `docs/`.

## Summary

The repository owner set up Slack through **Settings → Connections** on a
remote gateway: a server with no browser, reached from a laptop. Every step
worked in the end, but seven things went wrong on the way. This RFC fixes each
one:

| # | What went wrong | Fix |
|---|---|---|
| 1 | Connect only appears after a preview switch in another tab | Turn the switch on from the card, in one click |
| 2 | Making the Slack app takes five pages | One **Copy manifest** button that sets all five |
| 3 | Connect opened no tab; **Re-open approval** was needed | Keep the tab open from the click; never close it mid-sign-in; make the approve link the main button when no tab is held |
| 4 | After **Allow**, the browser says "This site can't be reached" | Know the gateway is remote before Connect; show the paste box at once; use an https return address when the gateway has one |
| 5 | "Return address" means nothing to people | Plain words: "Copy the whole address from the browser bar and paste it here" |
| 6 | Card said Connected, first tool call failed | Say Connected only after a real `tools/list`; refresh open chats |
| 7 | The person has to leave the chat to connect | The agent can show the same Connect card inside the chat |

The agent may only **show** the card in item 7. It never sees the client
secret, the code or a token, and only a person's own click starts their own
sign-in. In a shared Crew each person connects their own account, and a call
uses the token of the person who drives it.

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
- Connected means a real `tools/list` with the stored token answered.
- An agent can put the same Connect card in the chat where it needs the
  connection, without ever touching a secret.
- In a shared Crew, each person connects their own account. No call ever
  uses another person's token.
- Every fix has a UI path. No step needs a terminal.

## Non-goals

- No change to the bridge or the proxy approval gate, and no new custody
  model. [rfc-crew-owned-oauth.md](rfc-crew-owned-oauth.md) owns those. The
  vault entry gains a person id (section 7.4); nothing else about the vault
  changes.
- No fix for item 6's "The user rejected this tool call". Its cause is the
  approval option id Crew sent KAS, fixed in
  [#38117](https://github.com/kirodotdev/KiroCrew/pull/38117) (section 6).
- No new built-in app, and no per-vendor code beyond registry data.
- No running a person's harness under their own OS account. That is
  [rfc-per-person-execution.md](rfc-per-person-execution.md). This RFC keeps
  only each person's Connection token apart.
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
  - The preview master switch is off (`previewsOff` in
    `FeaturePreviewsSection.tsx`), or an edition lock pins the flag
    (`edition_locks.feature_previews`). The card then keeps today's link and
    names the reason. It never flips a master switch or a lock from a card.
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

So the card's "Before you connect" tip gets a **Copy manifest** button and
one line: "In Slack, choose **Create New App → From a manifest**, pick your
workspace, and paste this."

- **Built from data.** The manifest is generated from the registry entry:
  scopes from `recommended_scopes`, the redirect from `redirect_host` and
  `redirect_port`, plus an optional `redirect_urls_extra` for the https
  return address (section 4). A new optional registry field,
  `app_manifest: "slack"`, names the shape. No vendor name appears in card
  code.
- **Name.** `display_information.name` is "Kiro Crew". The person can rename
  it before saving.
- **Not proven yet.** Slack's docs say a `localhost` redirect counts as a
  desktop redirect only "if the app has opted into PKCE". Whether Slack
  accepts the redirect when `pkce_enabled` arrives in the same manifest is
  not documented. Phase 0 tests it on a throwaway workspace. If Slack refuses,
  the manifest leaves the redirect out, and the card shows one extra step
  with a short picture: "Open **OAuth & Permissions**, check that PKCE is on,
  add this redirect URL." The **Copy** button for the redirect is already on
  the card.
- **Steps that stay manual**, each with one cropped picture in the guide:
  copying the Client ID and Client Secret from **Basic Information**, and an
  admin approval where the workspace needs one. The pictures live under
  `docs/guides/oauth-app-registration/assets/` and are linked from the tip.
- **A prefilled create link** (a URL that opens Slack's create page with the
  manifest in it) is not documented by Slack. It is left as an open question,
  not built.

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

If the probe fails (a browser that blocks loopback fetches, see Security),
signals 2 and 3 still decide. With no signal at all, the card stays in local
mode, and the paste box is one click away under "Did the last page say it
can't be reached?"

#### 4.2 Warn before Allow, show the box at once

In remote mode the card says, before the person leaves:

> After you click **Allow** in Slack, your browser shows "This site can't be
> reached". That is expected. Copy the whole address from the browser bar and
> paste it here.

The paste box is open at once, under that text. Nothing else about the relay
changes: `POST /api/mcp/oauth/relay` stays loopback-only and delivers only
to a pending sign-in (section 7.4 lets each person use it for their own), and
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
  proxy uses. It sends no `tools/call`. The mint row goes to `granted` only
  when that answers with at least one tool. Otherwise it goes to `failed`
  with a reason the card already knows how to show (`mint_grant_unproven`),
  or a new `vendor_refused` with the vendor's own short error (for example
  Slack's `missing_scope`, or an IP allowlist refusal). The pair stays
  stored, so **Test** and a retry do not need a new consent.
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
  the kiro-cli file stat. Both are the pressing person's own entry
  (section 7.4). Undo (Disconnect) then refuses a grant newer than the one
  the card made, as it does today for kiro-cli, and only the person whose
  grant the card made can press it.
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
| A shared Crew | Every collaborator can press **Connect**, and the press connects that person's own account. With roles on, every role with `chat` and `run_as: self` (section 7.4). A person who has not connected sees their own card. |

#### 7.3 The gateway raises it too

The agent cannot always tell why a call failed. So the proxy's
`needs_auth` answer becomes a trigger the gateway acts on, not text for the
agent:

- When the proxy refuses a bridged call with `needs_auth`, and the session's
  current turn came from the dashboard, the gateway raises the
  `connection.connect` card in that chat itself, for the person driving the
  turn (section 7.4). The agent still sees the same refusal text, plus one
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

#### 7.4 Each person connects their own account

The repository owner ruled on 2026-11-16 (open question 3): in a shared Crew
every person has the same rights, so every person can press **Connect**.
Each person signs in with their own account, and the token is theirs. No
person's call ever uses another person's token.

This section builds on the accepted
[rfc-per-person-execution.md](rfc-per-person-execution.md) (cited below as
"per-person") and on Shared Crew
([shared-crew.md](../system-specs/modules/shared-crew.md)). It adds no
identity, role or consent model of its own.

**Where the token lives: a Crew-held grant, per person (option b).**
Per-person R5 says "The Crew never reads a person's credentials", and its
opening says a turn runs "with their own credentials, in their own home".
A Connection token is a different kind of thing:

| | A person's own credential (R5) | A Connection token |
|---|---|---|
| Issued to | the person (their model login, their cloud or ssh keys) | the Crew's OAuth app (the Client ID on the card), acting for the person |
| Who runs the flow | the person's harness, as them (per-person §4 step 2) | the gateway: consent URL, code swap with the app's secret, refresh |
| Who may use it | the person's runtime | only the gateway's proxy, which adds the bearer (parent RFC §3) |

So the token stays a Crew-held grant in the gateway vault, one per person and
provider. It never enters the person's home, harness or sandbox. Keeping it
in the person's home (option a) was weighed and rejected:

- Slack's token endpoint wants the app's `client_secret` on every refresh
  (2026-11-10 amendment of the parent RFC). So with option a, either the
  gateway still sees each refreshed token, or every run-as account holds the
  Crew's client secret.
- A token in the person's home is readable by the person's own runtime uid.
  The parent RFC's custody exists to keep it out of reach of the agent's
  shell there.
- Under run-as, the bridge already reaches the proxy over a socket whose
  peer uid the gateway checks (per-person R11). That gives a stronger
  binding of call to person than a file in a home could.

**This needs an amendment to per-person R5.** R5 as written covers "a
person's credentials" with no exception. The amendment, in that RFC's own
document, adds one sentence: a grant issued to the Crew's own OAuth app for
a person is held by the gateway for that person. It is used only in runs
that are that person's (the table below), and never enters the person's
runtime. The amendment is for Ray to accept (open question 3).

**What is shared, what is per person.**

| Part | Scope |
|---|---|
| The OAuth app (Client ID and Client Secret on the card) | one per Crew, as today. Saving it keeps today's gate; with roles on, it needs `settings.write` (per-person §7 Rights) |
| The token pair in the vault | one per person and provider |
| Connected, account name, expiry on the card | the person looking at it |
| Disconnect | removes and revokes the presser's own pair only |

**Storage, keyed by the Shared Crew Principal.** The key is the person's
existing `Principal.id` (shared-crew §2 Principal:
`principal_id_for(provider, issuer, subject)`). It is read from the request
(`identity.actor_of`) or from the turn's server-stamped author
(`attribution.message_author`), never from a body, header or display name
(shared-crew §2 Derivation, per-person R2). It is not a new id. Today's
`CONNECTIONS_<SLUG>_OAUTH_TOKENS` becomes
`CONNECTIONS_<SLUG>_OAUTH_TOKENS__<Principal.id>`. Only a `human` principal
gets an entry. `machine`, `system` (apps, the internal caller, automation)
and `unknown` never do. The Google grant group (one grant shared by the
Google slugs) is kept per person the same way.

**Who may Connect: roles.** "Equal rights" is per-person §7's state with no
`people_policy.json`: "every admitted person keeps the rights Shared Crew
gives them today" (§7 Built-in roles, §12 decision 6). There, every
collaborator may Connect. A collaborator is a person whom
`collaborators.is_collaborator_request` admits (shared-crew §5): the owner,
a relay teammate, or a `tailscale_serve` or `tailscale_listener` peer. An
app token, a Slack-allowlist dashboard token, an internal caller and Unknown
are not collaborators and cannot Connect. Once the operator turns roles on:

| Role (per-person §7) | May Connect? | Why |
|---|---|---|
| `owner`, `member`, or any role with `chat` and `run_as: self` | yes, for their own account | they send turns, so their token has turns to serve |
| `viewer` (`run_as: none`) | no | sends no turns, so a token would never be used |
| `guest` (`run_as: guest`) | no | R10 gives guests "no enterprise credentials", and every guest runs as the one guest account |

No new right is added. The rights list is closed and enforcer-owned
(per-person §7), and Connect follows from `chat` plus `run_as`. A role limits
which providers a person may use through its governance profile (§7 "role
profiles", phase R1): a profile that denies every tool of the `slack` server
hides that person's Slack **Connect**, and its calls are refused at the gate
anyway. A per-provider allow-list as a right would be an amendment to that
closed list; this RFC does not propose one.

Ray's "everyone can Connect" holds as written while roles are off. With roles
on, `viewer` and `guest` cannot Connect. That carve-out follows from
per-person §7 and R10, and Ray confirms it in open question 3.

**Whose token a call uses.** The token belongs to the person the run is
for. Per-person names that person on each surface (§3 table, R7, R9, R10),
and this table follows it. The person is fixed when the turn starts and
does not change mid-turn (R7). It is never read from a name the agent wrote,
a header the harness sent, or a field a client supplied (R2).

| Run | Whose token | Rule |
|---|---|---|
| A dashboard turn | the sender (`attribution.message_author`) | per-person §3 "Dashboard turn", R7 |
| The next turn, sent by someone else | that person's, from their turn on | R7 (decision 2: the runtime is replaced on a change of person) |
| A subagent, fork, workflow step, or a Bot worker dispatched from a turn | the person who drove that turn | R7 "inherit the initiator's account" |
| A cron job | the person the job runs as: its owner, or a person who consented | R9 and §5: consent checked at save and at every fire, against everyone who shaped the job; each use for someone else is logged where that person sees it |
| A Bot's own wake (patrol, check-in) with no person sending | the person the Bot runs for, under the same R9 consent as a cron job | R9 names cron jobs and channel answers, not Bot wakes: extending it to Bot wakes is an amendment (open question 3) |
| A channel message from a bound person | that person's | per-person §6: binding `(channel, user id) -> Principal` |
| An unbound channel user, a shared-thread turn that names no single person (a merged queue row from several people, shared-crew §3), a webhook, Crew automation | no token | R10: never the owner, never "whoever has the freshest login" |

Until per-person phase 3 (cron run-as with consent) ships, a cron job and a
Bot wake carry no consent record, so in a shared Crew they get no token. In a
one-person Crew the only principal is the owner, who is the job's owner, so
nothing changes there. Until per-person phase 5 (channel binding) ships,
channel rows name the bounded Unknown principal (shared-crew §3), so a
channel turn gets no token in a shared Crew either.

"No token" never falls back to the owner, to the Crew's first grant, or to
the last person who connected. It is the same as not connected:

- **Dashboard.** The proxy answers `needs_auth`, and the gateway raises the
  Connect card for the driving person (section 7.3). The card reads
  "Connect your Slack account". Anyone who presses it connects their own
  account, so the card says "your", never the driver's name as an ask to
  someone else.
- **Channels.** The gateway's own line goes to the person whose message
  needed the connection. An unbound person first gets per-person §6's two
  steps (a link in a DM from the bot, then a dashboard sign-in as
  themselves). A turn with no single person gets: "Slack needs each
  person's own sign-in. Send your request yourself after you connect in
  Kiro Crew."
- **Bot wakes and cron runs.** No card can render, so the person the run is
  for gets one bell notice that links to their Connections card. The run
  reports the missing connection and does not retry until that person
  connects.

**Two people at once.** Slack's redirect is one fixed port (48106). Today
`SignIn._bind` binds it for one consent, so a second person's Connect fails
with `callback_port_busy`. The pre-registered listener becomes routed by
`state`, the way the DCR listener already is (`crew_oauth_dcr.listen`). One
port then serves several pending sign-ins. Each pending sign-in records the
person who started it, and the code it receives is stored under that
person. Mint rows are keyed by provider and person, so one person's Cancel
or Connect never ends another person's sign-in.

**Approvals.** A Connection call in a person's turn spends that person's
token, so only that person can approve it. That is per-person R8 ("Tool
approvals ... from a run-as runtime can be answered only by that
principal"), which lands in its phase 5. Today any Shared Crew teammate may
decide a session's approval card (shared-crew §5, first submit wins). For a
Connection call, this RFC applies R8 early, and only to that call: other
people see the card read-only. The shared-crew spec says so in the same PR.

**Under run-as.** When per-person run-as is on, the bridge runs as the
person's account and reaches the proxy over the gateway's socket. The proxy
then also requires the socket's peer uid to equal the session's run-as
account (R11), and that account to map to the principal whose token it
spends (§9 account map). A mismatch refuses the call (R1, fail closed). With
run-as off, every runtime runs as the gateway account, and the proxy reads
the principal from the turn's attribution alone.

**Open chats.** After a person connects, the `on_granted` hook (section 6)
marks only the live sessions that person drives.

**Who may start a sign-in and paste the address.** The mint route and the
relay route (`POST /api/mcp/oauth/relay`) are owner-only today
(`require_owner_dashboard_request`). Phase 5a admits every collaborator
(above), or with roles on every person whose role may Connect. It scopes
each call to the caller's own principal: a person can start, cancel, relay
into or disconnect only their own sign-in. The relay checks that the pending
sign-in it delivers to was started by the same person. A Crew API delegated
or embed credential ([rfc-crew-api.md](rfc-crew-api.md) Design §1, §2
"Three tiers") still cannot reach these routes: they stay Internal, outside
the Public tier, so an app acting for the user cannot start a sign-in for
anyone.

**A one-person Crew** has one principal, the owner. Nothing visible changes
there.

A sign-in and the post-grant check (section 6) are not turns. Neither
Shared Crew fair admission (shared-crew §4) nor the attended-turn cap
([rfc-attended-turn-cap.md](rfc-attended-turn-cap.md)) counts them.

## Migration plan

Each phase is one PR, in this order. Every PR keeps the preview flag
default-off and changes nothing for a user who never turns it on.

| Phase | PR | Exit criteria |
|---|---|---|
| 0 | Probe, no code: create a Slack app from the section 2 manifest on a throwaway workspace; re-read Google and Asana redirect rules. Verdict written into this RFC. | The RFC states whether Slack accepts `pkce_enabled` and the `localhost` redirect in one manifest, with the date and the error text if not. |
| 1 | Plain words (section 5) and the consent tab fixes (section 3): reproduce the lost tab in a Mac browser first, then the reclaim fix and the primary approve link. Frontend and locales only. | The waiting card shows the new strings in every locale. A test pins `window.open` before any `await` in `startMint`. A render during the mint never closes the held tab. A refused or lost tab shows **Open Slack to approve** as the primary button. A test pins no server-side browser opener under `connections/`. |
| 2 | One-click enable on the card (section 1), and live-session marks when the flag turns on. | With previews on and a bridged backend, one click on the Slack card shows **Connect** with no page change. With previews off or locked, the card shows the link and the reason. |
| 3 | Copy manifest (section 2): registry fields `app_manifest`, `redirect_urls_extra`; tip button; guide pictures. Blocked on Phase 0 for the redirect line. | The copied text parses as a Slack manifest. It carries the registry's scopes and no bot scopes. The guide's section 1 to 3 shrink to the manifest step plus the manual steps. |
| 4 | Remote mode (section 4.1, 4.2): the `/reachable` route on the sign-in listener, `gatewayHeadless` in the status answer, the warning and the open paste box. | On a gateway the browser cannot reach, the paste box is open before **Allow**. On a local gateway it is closed. The route answers 204 only while a sign-in is pending. |
| 5 | Verified Connected (section 6): the post-grant `tools/list`, "Checking…", `on_granted` on the card path, the open-chats line. | A grant whose `tools/list` fails never shows Connected. An open KAS chat can call a Slack tool after Connect with no reload. |
| 5a | Per-person tokens with run-as off (section 7.4): the vault entry keyed by `Principal.id` and the one-time rename of today's entry to the owner's, a `state`-routed pre-registered listener, mint rows per person, the proxy's lookup of the person the run is for, per-person card state and Disconnect, mint and relay open to collaborators and scoped to the caller, R8 applied to Connection approvals, the shared-crew spec updated in the same PR. Cron jobs, Bot wakes and channel turns get no token in a shared Crew until the per-person phases below. | Two collaborators in one Crew each connect Slack at the same time. A's turn calls Slack with A's token and B's turn with B's, and the SEL event names each person. B's Disconnect leaves A connected. A turn with no single person, a channel turn and a cron job all get `needs_auth`, never a token. An app token cannot start a sign-in. A one-person Crew keeps its grant with no new sign-in. |
| 5b | With per-person: roles gate Connect (`chat` plus `run_as: self`; viewer and guest refused) once per-person R0 ships; the peer-uid check on the proxy once per-person phase 0 and 1 ship; cron and Bot-wake tokens under R9 consent once per-person phase 3 ships; channel tokens through the §6 binding once per-person phase 5 ships. Each part is its own PR, after the per-person phase it needs. Blocked on open question 3 (the R5 and R9 amendments). | A viewer sees no **Connect**. A run-as bridge whose peer uid does not match the session's account is refused. A cron job edited by someone else spends no token without the owner's consent, and each use shows on the owner's "My account" page. A bound channel user's turn uses their token. |
| 6 | Chat card (section 7.1, 7.2): `connection.connect` reads the pressing person's grant, renders the shared waiting body, channel fallback line. | In a dashboard chat the agent's card connects Slack end to end, paste box included. On a channel the reply carries the gateway's line and no card. |
| 7 | Gateway-raised card and expiry (section 7.3). | A `needs_auth` refusal in a dashboard turn shows one card. A grant 3 days from its refresh lifetime shows the warning. |
| 8 | Optional: https return for pre-registered cards (section 4.3). Blocked on open question 6. | With an https `dashboard.url` and the checkbox set, Slack returns to the gateway and the card connects with no paste. |

Phase 1 needs nothing else. Phases 2 to 5 can go in any order after it.
Phase 5a needs Phase 5. Phase 5b's parts each need the per-person phase
they name. Phase 6 needs Phases 1, 4, 5 and 5a, because it
reuses their card body and reads the per-person grant. Phase 7 needs Phase 6.

## Backward compatibility

- A user with the flag off sees today's cards, plus the new wording.
- A Slack app made by hand keeps working: the redirect is the same.
- An existing grant stays Connected. The post-grant check runs only on a new
  sign-in. **Test** keeps its behaviour.
- **Today's token becomes the owner's.** Phase 5a renames each existing
  `CONNECTIONS_<SLUG>_OAUTH_TOKENS` entry once, to the owner principal's
  key, in one vault write. A one-person Crew sees no change and needs no new
  sign-in.
- **In a shared Crew, other people stop using the owner's token.** Today
  every person's chat calls Slack with the one grant. After Phase 5a a
  person who has not connected gets `needs_auth` and their own Connect card.
  This is the ruling, not a regression: no call may borrow another person's
  token. The card tells them what to do in one click.
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
- The token entry's name gains a person id. Readers: `crew_oauth.TokenStore`
  and `token_entry_name`, `holds_grant`, the proxy's token lookup, the
  refresher, Disconnect and revoke, the Google grant-group check
  (`_shares_upstream_grant`), the status collector, and the change-card
  state read. Each moves to the per-person key in Phase 5a, and the rename
  runs before any of them reads.

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
  behind the same gate. It cannot clear the preview master switch or an
  edition lock. It widens nothing beyond what the Settings row already
  allows.
- **The agent's card.** The agent's input is `{slug}`, validated against the
  registry pattern. It sees no client id, secret, code, verifier or token:
  the plan is built by the gateway, the consent URL reaches only the
  pressing person's browser, and the paste goes from that browser to the
  relay route, which delivers only into a sign-in the same person started.
  The agent cannot press Connect, and a channel or worker turn cannot raise
  the card at all. The channel fallback is a gateway-built line with a
  gateway-built link, so a model cannot plant a URL in it.
- **The gateway-raised card** is bounded to one open card per chat,
  provider and person, and only on a dashboard turn. A prompt-injected agent
  can at most cause the same card the person could already open from
  Settings.
- **Per-person tokens (section 7.4).** These build on the per-person RFC's
  rules and add none of their own.
  - **Custody.** The gateway vault holds each person's pair as a Crew-held
    grant (section 7.4, option b). No harness, sandbox, transcript or
    person's home gets any of them, as today. This is the one point that
    amends per-person R5, and only for grants issued to the Crew's own
    OAuth app.
  - **Identity.** The token is chosen by the Shared Crew `Principal` of the
    person the run is for, derived by the gateway (shared-crew §2
    Derivation, per-person R2). An agent, a harness header or a client
    field cannot choose it, so an agent in A's turn cannot spend B's token.
    A prompt injected into B's message drives only B's token, the same reach
    B already has. Under run-as, the socket's peer uid must also match
    (R11).
  - **No borrowing.** A run with no single human principal gets no token
    (R10). Nothing falls back to the owner or to the newest grant, which
    closes the borrowed-token path a single shared grant had. A cron job or
    Bot wake spends a person's token only under that person's R9 consent,
    and each such use is an SEL record on their "My account" page (§5).
  - **Rights.** With roles off, Connect is open to collaborators only
    (shared-crew §5). With roles on, it needs `chat` and `run_as: self`
    (per-person §7). Saving the OAuth app needs `settings.write`. Start,
    cancel, relay and Disconnect act on the caller's own principal, read
    from the request's identity, never from a body field. Opening the mint
    and relay routes to collaborators widens who may call them, not what a
    call can reach.
  - **Approvals.** A Connection approval is answered only by the person
    whose token it spends (R8, applied early to these calls only).
  - **Audit.** The SEL event of every forwarded call names the person whose
    token it used.
  - **Governance.** A role profile, or the LTS remote policy's `mcp` control
    ([rfc-crew-lts-edition.md](rfc-crew-lts-edition.md) §5, "Open
    controls"), can only narrow which Connection tools a person may call.
    Nothing here widens a profile.
- **The post-grant check** calls only the registry's `mcp_url`, with the
  same SSRF vetting and no redirects as the proxy. It sends `initialize` and
  `tools/list` only. It logs one SEL event, as the sign-in does.
- **The manifest** carries no secret. It asks for the same user scopes as
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
- **One shared token per Crew** (the owner's grant used by everyone).
  Rejected by the 2026-11-16 ruling: every person has equal rights, and a
  call made in Ben's turn must act as Ben, not as whoever connected first.
- **Fall back to the owner's token when the driver has none.** Rejected: it
  is the same borrowing, only rarer, and the person would never learn they
  need to connect. Per-person R10 rules it out.
- **Keep each person's token in their own home** (option a, section 7.4).
  Rejected: Slack's refresh needs the app's client secret, so either the
  gateway still sees every refreshed token or every run-as account holds the
  secret. And the token would sit inside the person's runtime uid, which the
  parent RFC's custody keeps it out of.
- **A new `connections.connect` right.** Rejected: per-person §7's rights
  list is closed and enforcer-owned, and Connect already follows from
  `chat` plus `run_as`. A role profile limits providers.
- **Always show the paste box.** Rejected as the default: a local user would
  see a box they never need. It stays one click away in local mode.
- **Detect remote from the dashboard host only.** Rejected: an ssh tunnel
  makes a remote gateway's dashboard read `localhost`. The probe catches it.
- **Turn the preview flag on by default.** Out of scope: the parent RFC ties
  it to a security sign-off.
- **A Slack "create app" link with the manifest prefilled.** Not documented
  by Slack. Left as open question 9.

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
   because the agent often cannot tell why the call failed.
3. **Shared Crew.** Ruled 2026-11-16: every person has equal rights and
   presses **Connect** for their own account. Tokens are per person, a call
   uses the token of the person the run is for, and no case borrows another
   person's. Section 7.4 builds this on
   [rfc-per-person-execution.md](rfc-per-person-execution.md). Three points
   need Ray's decision, because they amend or narrow that accepted RFC:
   - **R5 amendment.** R5 says the Crew never reads a person's
     credentials. Keeping Connection tokens Crew-held (option b) needs one
     added sentence: a grant issued to the Crew's own OAuth app for a person
     is gateway-held, used only in that person's runs, and never enters
     their runtime. Recommendation: accept it. Option a (token in the
     person's home) either shows the gateway every Slack refresh or puts
     the app's client secret in every run-as account.
   - **R9 for Bot wakes.** R9 covers cron jobs and channel answers. A Bot's
     own wake that spends a person's token needs the same consent rule, so
     R9 gains "and a Bot's own wake". Recommendation: accept it.
   - **"Everyone" with roles on.** With no `people_policy.json`, every
     collaborator may Connect, exactly as ruled. With roles on, `viewer`
     (`run_as: none`) and `guest` (R10, one shared guest account) cannot.
     Per-provider limits come from the role's profile, not from a new
     right. Recommendation: keep this carve-out; it needs no amendment.
   The amendments go into the per-person RFC's own document in a separate
   docs PR once Ray agrees, so that accepted document is not edited here.
4. **Expiry warning (section 7.3).** Warn 3 days before Slack's 30-day end,
   on the card and with one bell notice? Recommendation: yes, 3 days.
5. **Remote probe (section 4.1).** Is a fixed `/reachable` route on the
   sign-in listener acceptable? Recommendation: yes; it lives only while a
   sign-in is pending and returns nothing.
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
9. **Prefilled Slack link.** Slack documents no create link with a manifest
   in it. Keep **Copy manifest** only? Recommendation: yes.
10. **A person who leaves a shared Crew.** With roles off, nobody can
    disconnect someone else, so a departed person's token stays in the
    vault until it expires (30 days for Slack) or they disconnect. With
    roles on, the per-person RFC gives revocations to `people.manage`
    (§7 Rights). Recommendation: revoking a person (a Tailscale invite
    revoked, or a `people.manage` revocation) also deletes and revokes
    their Connection tokens. Until then the token is spent only in that
    person's own runs, so a token left behind is never used for anyone
    else.

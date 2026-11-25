---
title: Per-person Connection tokens in a shared Crew
status: draft
author: buluoray, with kirocrew agent session
created: 2026-11-18
last-audited: 2026-11-20
audited-at: c157748343
doc-pr: null
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Per-person Connection tokens in a shared Crew

- Status: draft. Nothing is built, and this document changes no code.
- Split out of the sign-in redesign RFC
  (`rfc-crew-oauth-sign-in-redesign.md`, proposed in
  [#38119](https://github.com/kirodotdev/KiroCrew/pull/38119)),
  where it was section 7.4 and Phases 5a and 5b. That RFC fixes how one person
  reaches and finishes a sign-in. This one changes whose token a call spends.
- Builds on [rfc-crew-owned-oauth.md](rfc-crew-owned-oauth.md) (the gateway
  runs OAuth and holds the tokens), on the accepted
  [rfc-per-person-execution.md](rfc-per-person-execution.md) (cited below as
  "per-person") and on Shared Crew
  ([shared-crew.md](../system-specs/modules/shared-crew.md)). It needs the
  per-person R5 and R9 amendments proposed in
  [#38142](https://github.com/kirodotdev/KiroCrew/pull/38142).
- Measured at `origin/main` `224959b4ade`; re-audited at `e80c677c04a`. Paths are under `src/kiro_crew/`
  unless they start with `docs/`.

## Summary

Today a Crew holds one token per provider. In a shared Crew every person's
chat spends that one token, so a call made in Ben's turn acts as whoever
connected first. This RFC gives each person their own Connection token:

- Each person presses **Connect** and signs in with their own account.
- The gateway vault keeps one token pair per person and provider, keyed by
  the person's Shared Crew `Principal.id`.
- A call spends the token of the person the run is for. With no single
  person, the call gets no token. Nothing falls back to the owner's.

## Motivation

### Current state

- **One grant per Crew.** The vault entry is
  `CONNECTIONS_<SLUG>_OAUTH_TOKENS` (`token_entry_name` and `holds_grant` in
  `connections/crew_oauth.py`). The proxy adds that one bearer to every
  bridged call, whoever drives the turn.
- **Teammates already pass the owner gate.** The Connection routes call
  `require_owner_dashboard_request` (`dashboard/handlers/connections.py`:
  relay, mint, test, cancel, premint and the OAuth client save).
  `is_owner_dashboard_request` (`dashboard/handlers/source_providers.py`)
  also admits a Shared Crew teammate through `teammate_passes_owner_gate`
  (`shared_crew/teammate_access.py`), since
  [#38209](https://github.com/kirodotdev/KiroCrew/pull/38209) and, for relay
  teammates, [#38437](https://github.com/kirodotdev/KiroCrew/pull/38437).
  `is_owner_only_for_teammates` does not list these routes. A teammate here
  is anyone `is_shared_crew_teammate` accepts, which includes a person who
  signed in through an SSM invite (`is_ssm_guest_request`). So on main a
  teammate can start a sign-in, paste the return address, cancel, and save
  the OAuth app. The grant that results then serves everyone.
- **Nothing records who made the grant.** A mint row and the vault entry
  name the provider, not the person who pressed Connect (`connections/mint.py`,
  `connections/crew_oauth.py`). So in a shared Crew, today's one grant may be
  the owner's or a teammate's, and the gateway cannot tell which.
- **One sign-in at a time.** Slack's redirect is one fixed port (48106).
  `SignIn._bind` binds it for one consent, so a second sign-in fails with
  `callback_port_busy`.

### Problems

1. A call in one person's turn spends another person's token. The vendor
   (and its audit log) sees the wrong person.
2. A teammate who connects replaces the grant everyone else uses.
3. Two people cannot sign in at once.

## Goals

- In a shared Crew, each person connects their own account.
- No call ever uses another person's token.
- Every person, the owner included, signs in again at most once, at the
  upgrade, through a plain "sign in again" prompt.

## Non-goals

- No change to the bridge, the proxy approval gate or the custody model of
  [rfc-crew-owned-oauth.md](rfc-crew-owned-oauth.md). The vault entry gains a
  person id; nothing else about the vault changes.
- No running a person's harness under their own OS account. That is
  per-person run-as.
- No who-can or who-can't limits for now (ruled 2026-11-17, open question 1).
- Nothing in the sign-in flow itself. That is
  the sign-in redesign RFC ([#38119](https://github.com/kirodotdev/KiroCrew/pull/38119)).

## Design

The repository owner ruled on 2026-11-16: in a shared Crew every person has
the same rights, so every person can press **Connect**. Each person signs in
with their own account, and the token is theirs. No person's call ever uses
another person's token. This design adds no identity, role or consent model
of its own.

### 1. Where the token lives: a Crew-held grant, per person (option b)

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
that are that person's (section 4), and never enters the person's runtime.
It is proposed in [#38142](https://github.com/kirodotdev/KiroCrew/pull/38142).

### 2. What is shared, what is per person

| Part | Scope |
|---|---|
| The OAuth app (Client ID and Client Secret on the card) | one per Crew, as today. Saving it keeps today's gate, which also admits teammates (open question 3). Later, not now: with roles on, it would need `settings.write` (per-person §7 Rights) |
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
`CONNECTIONS_<SLUG>_OAUTH_TOKENS__<Principal.id>`. Section 2a says what
happens to today's entry. Only a `human` principal
gets an entry. `machine`, `system` (apps, the internal caller, automation)
and `unknown` never do. The Google grant group (one grant shared by the
Google slugs) is kept per person the same way.

**2a. Today's entry goes to the owner only when the record says the owner
made it.** Nothing on main records who made it (Motivation). So Phase A0,
a small PR before Phase A, adds that record: each sign-in stores the
`Principal.id` of the person who pressed Connect next to the grant, read
from the request's identity as in section 2. At the Phase A upgrade:

| What the record says | Today's entry |
|---|---|
| The owner made it | moved once to the owner's key, in one vault write. No new sign-in. |
| A teammate made it | moved once to that teammate's key. It never serves the owner's calls. |
| No record (the grant is older than Phase A0) | never moved, in any Crew. It is revoked at the vendor where the registry allows it, then deleted. |

**No exception for a one-person Crew.** Nothing records who was ever
admitted: a revoked SSM invite row is deleted, a Tailscale revoke drops the
member, and relay membership lives on the relay. So a Crew whose teammate
connected and was later revoked looks like a one-person Crew. Moving its
grant to the owner could let a teammate's grant serve the owner's calls.
So every grant older than A0 is revoked, and the person signs in again
once.

**The sign-in-again prompt.** The person sees plain words, never a vault
key, a slug or a config name. The Slack card says "Sign in to Slack again
to keep using it" with one **Sign in again** button. A bell notice with the
same words links to the card. The prompt shows once per provider and
person, and goes away after the sign-in. A turn that needs the provider
before then gets the normal Connect card (section 4).

### 3. Who may Connect

"Equal rights" is per-person §7's state with no `people_policy.json`:
"every admitted person keeps the rights Shared Crew gives them today" (§7
Built-in roles, §12 decision 6). There, four kinds of person may Connect,
each for their own account only:

| Who | How the gateway knows | Token key (`Principal.id`) |
|---|---|---|
| The owner, on the machine or over the Kiro relay | `is_owner_dashboard_request` | the owner's own principal. A relayed owner already resolves to it ("the machine tab and the relay tab are one person", shared-crew §2 Derivation) |
| A collaborator | `collaborators.is_collaborator_request` (shared-crew §5): a `tailscale_serve` or `tailscale_listener` peer | the tailnet principal (provider `tailscale`, issuer `tailnet`, the lowercased login) |
| A relay teammate on a team Crew | `routes_identity._relay_teammate` (provenance `kiro_relay`) | the relay principal (provider `kiro`, issuer `kiro-relay`, the exact Kiro user ID, no case folding) |
| A person who signed in through an SSM invite | `teammate_access.is_ssm_guest_request` | the SSM principal (provider `ssm`, the invite id as subject; `shared_crew/identity.py`) |

A relay teammate is not a collaborator: `collaborators.is_collaborator_actor`
excludes every `kiro_relay` person (shared-crew §2). So the Connect check is
its own predicate, "owner, collaborator, relay teammate or SSM invite
guest", not `is_collaborator_request` alone, which would shut every relay
teammate out of a team Crew. It matches who passes the owner gate on these
routes today (`is_shared_crew_teammate`), so nobody loses Connect. The same predicate gates cancel, paste and Disconnect. A
`tailscale_direct` peer is attribution only (shared-crew §2) and cannot
Connect. Neither can an app token, a Slack-allowlist dashboard token, an
internal caller or Unknown.

**The relayed owner.** Its published actor keeps `kiro_relay` provenance,
which is never `local_operator` (shared-crew §2). So every gate that requires
the local operator refuses or narrows it. No Connection route uses such a
gate. Each one reads the caller's principal, and the relayed owner's
principal is the owner's. So the owner can Connect, paste and Disconnect over
the relay, and spends the same grant as at the machine. A test pins this.

**Start, paste, cancel: today and after Phase A.** Today the mint, relay and
cancel routes already admit teammates (Motivation). They act on the Crew's
one sign-in and its one grant. Phase A does not open them further. It
**narrows** each call to the caller's own principal: a person can start,
cancel, relay into or disconnect only their own sign-in. The relay checks
that the pending sign-in it delivers to was started by the same person. A
Crew API delegated or embed credential
([rfc-crew-api.md](rfc-crew-api.md) Design §1, §2 "Three tiers") still
cannot reach these routes: they stay Internal, outside the Public tier, so
an app acting for the user cannot start a sign-in for anyone.

**Role limits: later, not now.** On 2026-11-17 the repository owner ruled
that a Shared Crew has no who-can or who-can't limits for now. So every
admitted person in the table above may Connect, and nothing in this RFC
narrows that. The rest of this part records how Connect would follow roles
if per-person roles ship later. No phase here builds it now (Phase B marks
it the same way).

If the operator later turns roles on:

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

### 4. Whose token a call uses

The token belongs to the person the run is for. Per-person names that person
on each surface (§3 table, R7, R9, R10), and this table follows it. The
person is fixed when the turn starts and does not change mid-turn (R7). It
is never read from a name the agent wrote, a header the harness sent, or a
field a client supplied (R2).

| Run | Whose token | Rule |
|---|---|---|
| A dashboard turn | the sender (`attribution.message_author`) | per-person §3 "Dashboard turn", R7 |
| The next turn, sent by someone else | that person's, from their turn on | R7 (decision 2: the runtime is replaced on a change of person) |
| A subagent, fork, workflow step, or a Bot worker dispatched from a turn | the person who drove that turn | R7 "inherit the initiator's account" |
| A cron job | the person the job runs as: its owner, or a person who consented | R9 and §5: consent checked at save and at every fire, against everyone who shaped the job; each use for someone else is logged where that person sees it |
| A Bot's own wake (patrol, check-in) with no person sending | the person the Bot runs for, under the same R9 consent as a cron job | R9 as amended in [#38142](https://github.com/kirodotdev/KiroCrew/pull/38142), which names a Bot's own wake next to cron jobs |
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
  Connect card for the driving person (sign-in redesign section 7.3). The
  card reads "Connect your Slack account". Anyone who presses it connects
  their own account, so the card says "your", never the driver's name as an
  ask to someone else.
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

### 5. Two people at once

Slack's redirect is one fixed port (48106). The pre-registered listener
becomes routed by `state`, the way the DCR listener already is
(`crew_oauth_dcr.listen`). One port then serves several pending sign-ins.
Each pending sign-in records the person who started it, and the code it
receives is stored under that person. Mint rows are keyed by provider and
person, so one person's Cancel or Connect never ends another person's
sign-in.

### 6. Approvals

A Connection call in a person's turn spends that person's token, so only
that person can approve it. That is per-person R8 ("Tool approvals ... from
a run-as runtime can be answered only by that principal"), which lands in its
phase 5. Today any Shared Crew teammate may decide a session's approval card
(shared-crew §5, first submit wins). For a Connection call, this RFC applies
R8 early, and only to that call: other people see the card read-only. The
shared-crew spec says so in the same PR.

### 7. Under run-as

When per-person run-as is on, the bridge runs as the person's account and
reaches the proxy over the gateway's socket. The proxy then also requires the
socket's peer uid to equal the session's run-as account (R11), and that
account to map to the principal whose token it spends (§9 account map). A
mismatch refuses the call (R1, fail closed). With run-as off, every runtime
runs as the gateway account, and the proxy reads the principal from the
turn's attribution alone.

### 8. A person who leaves

Revoking a person also deletes and revokes their Connection tokens, in Phase
A. That covers a Tailscale invite revoked, a relay teammate removed from
the team Crew, and an SSM invite revoked
(`DELETE /api/shared-crew/ssm/invites/{invite_id}`, which already closes that
person's open sockets). It needs no roles. Without it, a departed person's token would
stay in the vault until it expired (30 days for Slack). Later, not now: with
roles on, a `people.manage` revocation (per-person §7 Rights) does the same.

### 9. The rest

**Open chats.** After a person connects, the `on_granted` hook (sign-in
redesign section 6) marks only the live sessions that person drives.

**A one-person Crew** has one principal, the owner. After the one sign-in
again at the upgrade (section 2a), nothing visible changes there.

A sign-in and the post-grant check are not turns. Neither Shared Crew fair
admission (shared-crew §4) nor the attended-turn cap
([rfc-attended-turn-cap.md](rfc-attended-turn-cap.md)) counts them.

## Migration plan

| Phase | PR | Exit criteria |
|---|---|---|
| A0 | Record who made each grant (section 2a): each sign-in stores the `Principal.id` of the person who pressed Connect next to the grant. No other change. | A teammate's sign-in and the owner's sign-in each record their own `Principal.id`. A request with no human principal records nothing and cannot sign in. |
| A | Per-person tokens with run-as off: the vault entry keyed by `Principal.id` and the one-time move of today's entry by its record (section 2a), a `state`-routed pre-registered listener, mint rows per person, the proxy's lookup of the person the run is for, per-person card state and Disconnect, Connect open to the four kinds of person in section 3, mint, relay and cancel scoped to the caller, token delete and revoke when a person is revoked (section 8, SSM invites included), R8 applied to Connection approvals, the shared-crew spec updated in the same PR. Cron jobs, Bot wakes and channel turns get no token in a shared Crew until Phase B. | A tailnet collaborator and a relay teammate in one Crew each connect Slack at the same time, and each token is keyed by that person's own principal. The owner connects over the relay and gets the same grant as at the machine. A `tailscale_direct` peer cannot Connect. A's turn calls Slack with A's token and B's turn with B's, and the SEL event names each person. B's Disconnect leaves A connected. Revoking B's invite deletes and revokes B's token, for a Tailscale invite and for an SSM invite. An SSM invite guest connects their own account. The upgrade gives a grant to the owner only when its A0 record names the owner, and to a teammate when it names them. Test `a pre-A0 grant serves no one after A0`: in a one-person Crew, in a Crew with a current teammate, and in a Crew whose teammate connected and was revoked before A0, a grant with no A0 record is revoked and deleted. No turn, cron job or Bot wake spends it, and its person sees the "Sign in to Slack again" prompt. A turn with no single person, a channel turn and a cron job all get `needs_auth`, never a token. An app token cannot start a sign-in. |
| B | With per-person: later, not now, roles gate Connect (`chat` plus `run_as: self`; viewer and guest refused) once per-person R0 ships and the repository owner lifts the 2026-11-17 no-limits ruling; the peer-uid check on the proxy once per-person phase 0 and 1 ship; cron and Bot-wake tokens under R9 consent once per-person phase 3 ships; channel tokens through the §6 binding once per-person phase 5 ships. Each part is its own PR, after the per-person phase it needs. | Later, not now: a viewer sees no **Connect**. A run-as bridge whose peer uid does not match the session's account is refused. A cron job edited by someone else spends no token without the owner's consent, and each use shows on the owner's "My account" page. A bound channel user's turn uses their token. |

Phase A needs Phase A0, sign-in redesign Phase 5 (the post-grant check and
`on_granted` on the card path) and [#38142](https://github.com/kirodotdev/KiroCrew/pull/38142)
merged. [#38142](https://github.com/kirodotdev/KiroCrew/pull/38142) cites this RFC for its R5 carve-out, so it merges with this
RFC or after it. Phase B's parts each need the per-person phase they name. After
Phase A, the sign-in redesign's chat card (its Phase 6) reads the pressing
person's grant in place of the Crew's one grant.

## Backward compatibility

- **Today's token.** Phase A moves each existing
  `CONNECTIONS_<SLUG>_OAUTH_TOKENS` entry once, by its Phase A0 record: to
  the owner when the owner made it, to a teammate when they did. A grant
  older than A0 has no record. It is revoked and deleted in every Crew,
  one-person Crews included, and its person sees one plain "Sign in to
  Slack again" prompt (section 2a).
- **In a shared Crew, other people stop using the owner's token.** Today
  every person's chat calls Slack with the one grant. After Phase A a
  person who has not connected gets `needs_auth` and their own Connect card.
  This is the ruling, not a regression: no call may borrow another person's
  token. The card tells them what to do in one click.
- **Teammates who start a sign-in today**, SSM invite guests included, keep
  that ability. Their sign-in
  now writes their own token, not the Crew's one grant.

Reader list for what this changes:

- Who may answer a Connection approval card. Today any teammate may
  (shared-crew §5, first submit wins). After Phase A only the person whose
  token the call spends may (section 6). Readers: the approval card, the
  approvals route, and the shared-crew spec, updated in the Phase A PR.

- The token entry's name gains a person id. Readers: `crew_oauth.TokenStore`
  and `token_entry_name`, `holds_grant`, the proxy's token lookup, the
  refresher, Disconnect and revoke, the Google grant-group check
  (`_shares_upstream_grant`), the status collector, and the change-card
  state read. Each moves to the per-person key in Phase A, and the rename
  runs before any of them reads.

## Security considerations

These build on the per-person RFC's rules and add none of their own.

- **Custody.** The gateway vault holds each person's pair as a Crew-held
  grant (section 1, option b). No harness, sandbox, transcript or person's
  home gets any of them, as today. This is the one point that amends
  per-person R5, and only for grants issued to the Crew's own OAuth app.
- **Identity.** The token is chosen by the Shared Crew `Principal` of the
  person the run is for, derived by the gateway (shared-crew §2 Derivation,
  per-person R2). An agent, a harness header or a client field cannot choose
  it, so an agent in A's turn cannot spend B's token. A prompt injected into
  B's message drives only B's token, the same reach B already has. Under
  run-as, the socket's peer uid must also match (R11).
- **No borrowing.** A run with no single human principal gets no token
  (R10). Nothing falls back to the owner or to the newest grant, which
  closes the borrowed-token path that the single shared grant has on main
  today. A cron job or Bot wake spends a person's token only under that
  person's R9 consent, and each such use is an SEL record on their "My
  account" page (per-person §5).
- **Rights.** With roles off, Connect is open to the owner, collaborators
  (shared-crew §5), relay teammates and SSM invite guests, never to a
  `tailscale_direct` peer, an app token or Unknown. Role limits are later, not now: if roles ship,
  Connect would need `chat` and `run_as: self` (per-person §7), and saving
  the OAuth app would need `settings.write`. Start, cancel, relay and
  Disconnect act on the caller's own principal, read from the request's
  identity, never from a body field. No Connection route requires
  `local_operator` provenance, so the relayed owner keeps the owner's
  rights, and nothing else gains them. The mint, relay and cancel routes
  already admit teammates on main. Phase A narrows what each call can reach
  to the caller's own sign-in; it does not widen who may call them.
- **Leaving.** A revoked person's tokens are deleted and revoked with the
  revocation (section 8), for Tailscale, relay and SSM invites, so none stays
  behind in the vault.
- **Upgrade.** A grant reaches the owner's key only when its A0 record
  names the owner. Every grant older than A0 is revoked, with no exception
  (section 2a), so the move never lets the owner spend a teammate's token.
  Nothing is left as an accepted risk.
- **Approvals.** A Connection approval is answered only by the person whose
  token it spends (R8, applied early to these calls only).
- **Audit.** The SEL event of every forwarded call names the person whose
  token it used.
- **Governance.** A role profile, or the LTS remote policy's `mcp` control
  ([rfc-crew-lts-edition.md](rfc-crew-lts-edition.md) §5, "Open controls"),
  can only narrow which Connection tools a person may call. Nothing here
  widens a profile.

## Alternatives considered

- **One shared token per Crew** (the owner's grant used by everyone).
  Rejected by the 2026-11-16 ruling: every person has equal rights, and a
  call made in Ben's turn must act as Ben, not as whoever connected first.
- **Fall back to the owner's token when the driver has none.** Rejected: it
  is the same borrowing, only rarer, and the person would never learn they
  need to connect. Per-person R10 rules it out.
- **Keep each person's token in their own home** (option a, section 1).
  Rejected: Slack's refresh needs the app's client secret, so either the
  gateway still sees every refreshed token or every run-as account holds the
  secret. And the token would sit inside the person's runtime uid, which the
  parent RFC's custody keeps it out of.
- **A new `connections.connect` right.** Rejected: per-person §7's rights
  list is closed and enforcer-owned, and Connect already follows from
  `chat` plus `run_as`. A role profile limits providers.

## Open questions

For the repository owner and the maintainers.

1. **Rulings to confirm.** The repository owner (buluoray) ruled on
   2026-11-16 and 2026-11-17. A maintainer confirms them in the sign-off,
   or names what they disagree with:
   - **Equal rights.** Every person presses **Connect** for their own
     account. Tokens are per person, a call uses the token of the person the
     run is for, and no case borrows another person's.
   - **R5 amendment: accepted.** A grant issued to the Crew's own OAuth app
     for a person is gateway-held, used only in that person's runs, and
     never enters their runtime (option b).
   - **R9 for Bot wakes: accepted.** A Bot's own wake that spends a
     person's token needs the same consent as a cron job.
   - **"Everyone" with roles on: accepted as a later step.** With no
     `people_policy.json`, the owner, every collaborator, every relay
     teammate and every SSM invite guest may Connect. If roles ship, `viewer` and `guest` would not,
     and per-provider limits would come from the role's profile.
   - **No limits for now (2026-11-17).** A Shared Crew has no who-can or
     who-can't limits for now. Every role-based limit in this RFC is later,
     not now.
   The two amendments are proposed in the per-person RFC's own document in
   [#38142](https://github.com/kirodotdev/KiroCrew/pull/38142), so that
   accepted document is not edited here.
2. **A stop-gap before Phase A.** Teammates can already start a sign-in for
   the Crew's one grant (Motivation). Should the mint, relay and client-save
   routes join the teammate owner-only set until Phase A lands? Open; the
   repository owner decides.
3. **Teammates and the OAuth app.** Saving the Client ID and Client Secret
   keeps today's gate, which admits teammates. Is it intended that a
   teammate can replace the Client Secret? Open; the repository owner
   decides.

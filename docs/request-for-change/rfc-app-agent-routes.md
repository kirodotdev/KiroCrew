---
title: "App agent routes: an installed app declares which of its routes agents may call"
status: draft
author: unstablebrainiac
created: 2026-10-13
last-audited: 2026-10-21
audited-at: 098bc0a17
doc-pr: 19676
implementation-prs: [17015]
tracking-issues: [16910]
supersedes: []
superseded-by: []
---

# RFC: App Agent Routes

> **Status:** `draft`. Acceptance is requested from a maintainer; the status
> flips to `accepted` when one records it in Open questions below. Nothing is on
> main. The baseline was re-verified at `098bc0a17` (2026-10-21): no source
> file under `src/` names `agentRoutes`, and no `app_request` tool exists.
> `token_auth` honours the internal secret only on the paths enumerated in
> `_STRICT_INTERNAL_API_PATHS` and `_MIXED_INTERNAL_API_PATHS`. Both sets are
> defined in `src/kiro_crew/dashboard/server_internal_paths.py` (moved out of
> `server.py` by #22867, which re-exports them) and enforced through
> `src/kiro_crew/dashboard/server_runtime/security_middleware.py`, which adds
> an edition's contributed paths to the mixed set. Four builtin apps are named
> there one route at a time: Issue Radar (`/investigation`, `/crew`,
> `/crew/work`), Ops Mission Control (ten routes), Dev Fleet (four `/pod/*`
> routes, strict) and Design Tweak (`/apps/design-tweak/api/thread`).
> `RouteRegistry.dispatch` in `src/kiro_crew/apps/route_registry.py` hands an
> app handler no caller session. A full implementation is open as
> [#17015](https://github.com/kirodotdev/KiroCrew/pull/17015). It was written
> before this document and is offered as the reference for it, not as a decision.

## Summary

An installed app lists, in `app.json`, the routes agents may call:
`"agentRoutes": ["POST /subscriptions", "DELETE /subscriptions/{id}"]`. An
agent calls them through one `kirocrew-core` tool, `app_request`. The gateway
admits that call only on a declared route of an enabled app, and the app's
handler receives the calling session as `request["kirocrew_agent_session"]`.
Core never names the app.

## Motivation

### Current state

An agent reaches the gateway through an MCP server that holds the internal
secret. `token_auth` accepts that secret only on the hardcoded path sets in
`dashboard/server_internal_paths.py`, enforced through `dashboard/server_runtime/security_middleware.py`.
Each app agents use today got its own core change: paths added to one of those
sets and a hand-written tool in `mcp_tools/apps.py` or `mcp_core.py` (Issue Radar's
investigation and crew routes, Ops Mission Control's API, Dev Fleet's four
`/pod/*` paths, Design Tweak's thread route).

An app can ship its own MCP server through `mcpServers`, and that server can
call the app's routes using the app-secret token exchange. That call carries the
app's credential, not the agent's, so the gateway attests nothing about which
session asked and the handler has no session to act on.

### Problems

1. **A registry app cannot offer agent tools that drive its backend.** Its
   routes answer `Token required` to every agent call, and it cannot add itself
   to a core path set.
2. **Each in-repo app costs a core patch.** The hardcoded sets grow by hand, and
   every new entry is a new place where admission can drift from dispatch.
3. **An app cannot tell who asked.** Even where admission works, the handler
   does not learn which session the request is for, so a per-session action
   (subscribe this session to a thread) has no trustworthy key.

The concrete first consumer is a registry app outside this repository that
polls Slack for the user and lets a session subscribe itself to a channel or a
thread. It needs an agent-callable route and the calling session.

## Goals

- An app opens a route to agents by declaring it, with no core change.
- Admission is decided once, from the route the router will actually run, so
  admission and dispatch cannot disagree.
- The handler learns the calling session, and a browser request never carries
  one.
- Disabling an app closes its agent routes on the next request.
- Existing apps, routes and dedicated tools behave exactly as on main.

## Non-goals

- Apps that run their own backend process, reverse-proxied at
  `/apps/<app>/api/`. Only in-gateway `hooks.routes` handlers are covered.
- Moving Issue Radar, Ops Mission Control or Dev Fleet onto this. They may move
  later, in their own PRs.
- Showing the declared routes on the install screen.
- Retry or idempotency machinery in `app_request`.
- Channel-agent sessions (Slack, Discord and other channel keys).
  `app_request` refuses them, like other tools that can act outside the chat.

## Design

### 1. Manifest field

`agentRoutes` is an optional list of `"METHOD /relative/path"` strings.
`{param}` segments are allowed. One parser, `parse_agent_route` in
`apps/manifest.py`, is the grammar. Install-time validation refuses:

- any other shape, an unknown method, a duplicate;
- a first segment that is a `{param}` or a core lifecycle name under
  `/api/apps/<app>/`;
- more than 32 entries, or an entry over 256 characters.

A non-empty list is part of `signing_payload()`. That alone protects nothing
under the stock open policy: `app_admission_denied` returns early unless
`app_admission.json` enforces something, and it checks a signature only when
`require_signature` is set. Gating `agentRoutes` on a verified signature does
not close that gap either. `trust_keys` is empty by default, so the gate would
turn the feature off on a stock install, the first consumer's included. And the
POC check is an HMAC whose secret sits in `app_admission.json`, a file a
shell-capable agent can read, so that agent could re-sign an edited manifest.
Phase 1 therefore honours an enabled app's `agentRoutes` as declared, with no
signature gate. The threat this leaves open is stated under Security
considerations, and open question 5 asks what should close it. Declarations are installed in the same
generation as the app's routes and are kept all or none: a malformed entry, an
over-cap list or a failed hook load gives the app no agent routes and logs one
warning. Deregistration clears them.

### 2. Admission in `token_auth`

`token_auth` holds no declaration knowledge. It asks one question, through
`hooks_integration.agent_route_arm`: did aiohttp select the RouteRegistry
catch-all for this request? A request takes the agent-route arm only when all
of these hold:

- it is local (unix socket or loopback);
- it presents `X-Internal-Secret`;
- its path is in none of the static internal sets;
- its resolved route is the catch-all.

On that arm the existing mixed-internal checks run unchanged
(`_verify_unix_peer`, the secret, app scope). A caller naming a dashboard slot
that has already closed is refused before app scope is derived. The request is
marked `request["app_agent_route"]`. A host-owned route at the same path, a core
lifecycle handler and any path outside `/api/apps/` are not the catch-all, so
they never arm.

`app_request` stamps every call with `X-KiroCrew-App-Request: 1`. A marked call
is refused on any static internal path (`app_request_static_route_refused`) and
anywhere the arm did not engage (`app_request_route_refused`), each with an
audit row. Static internal paths therefore stay reachable only through their
dedicated tools, even if an app's editable `app.json` declares one. Unmarked
traffic is unchanged.

### 3. Decision and identity in `RouteRegistry.dispatch`

`dispatch` resolves the route once (exact, then pattern). For an armed request
it refuses with 403 before any handler runs unless:

- `X-Session-Key` is present (`agent_session_required` otherwise), and
- the resolved route is a declared `(method, path)` of that app, and the request
  path instantiates it with no empty, `.` or `..` segment
  (`agent_route_not_declared` otherwise).

Only then does it set `request["kirocrew_agent_session"]`. Nothing else sets
it, so a cookie request never carries it, whatever headers it sends. The
dispatch audit row names the calling session as caller and
`app:<name> METHOD path` as the resource.

### 4. The agent tool

`app_request(app, method, path, body?)` is one `kirocrew-core` tool. It:

- resolves the caller through `require_strict_session_key` and refuses an
  unidentified caller or a channel agent;
- refuses a disabled app, a path carrying credential material, and a route the
  app does not declare, before anything is sent;
- sends the strict session key on the wire;
- reads at most 60,000 bytes of response and redacts the whole result before
  capping it;
- for POST, PUT, PATCH and DELETE, reports a failure after the request was sent
  as "outcome unknown", telling the agent to read state back rather than resend.

The local declared-route check only saves a round trip. The gateway is what
enforces it, because the tool reads `app.json` from a directory an agent can
write. The tool is listed in `tools/list` only while some installed app that is
not disabled declares `agentRoutes`. If that scan fails, the tool stays listed.

Apps teach agents their routes through the skills they already ship.

## Migration plan

| Phase | Scope | Exit criteria |
|---|---|---|
| 1 | Manifest field, admission arm, dispatch decision, `app_request`, docs ([#17015](https://github.com/kirodotdev/KiroCrew/pull/17015)) | An app that declares no `agentRoutes` behaves as on main. A declared route answers 200 with the caller's session in `kirocrew_agent_session`. An undeclared route, a missing `X-Session-Key` and a `..` path answer 403 before any handler runs. A marked call to a static internal path answers 403. A disabled app's routes answer 404. All of these hold over both the unix socket and TCP loopback. Phase 1 applies no signature gate to `agentRoutes`; the manifest-edit threat in Security considerations stays open until open question 5 is decided. |
| 2 (optional) | Move an in-repo app (Issue Radar first) off its static paths and dedicated tool | Its paths leave the static sets in `dashboard/server_internal_paths.py`, and its existing tests pass through `app_request`. Not required for Phase 1 to be useful. Blocked on a maintainer wanting it. |

Phase 1 was run end to end on an isolated gateway booted from the #17015 branch;
the results are in that PR's description.

## Backward compatibility

`agentRoutes` is absent from every existing manifest, and an app without it
behaves as today. No route, key, field or function is removed or renamed, and
the static internal sets and their handlers are unchanged.

One narrowing: a local request that presents `X-Internal-Secret` on an app route
outside the static sets is now judged on that secret, so a wrong secret is
refused instead of falling through to cookie auth. Browsers never send that
header.

## Security considerations

- **Attestation is transport-dependent.** `kirocrew_agent_session` is pinned to
  the kernel-identified peer only on the unix socket, and only when
  `_verify_unix_peer` resolves that peer's tenancy. Over TCP loopback, or when
  tenancy is unknown, it is the caller's own claim. An app must read it as the
  session a call is FOR, never as proof of who made it. The first consumer
  routes Slack messages by it, so this rule belongs in the app kit docs.
- **One decision point.** Admission is the route dispatch resolves, so a path
  that admission accepts and dispatch routes elsewhere cannot exist.
- **Editable manifests cannot reach core routes.** The marker refusal keeps
  `app_request` off every static internal path, whatever `app.json` says.
- **Signing.** A signature covers the declarations only when it is verified:
  under `require_signature`, or against a trust key the admission policy
  carries. Under the stock open policy nothing checks it, so a signed app is no
  better protected than an unsigned one. Where it is checked, the POC HMAC
  secret is readable by any process that can read `app_admission.json`, so it
  does not bind a shell-capable agent (Design §1, open question 5).
- **Audit.** `token_auth` writes an `app_agent_route` granted row only after
  every refusal has passed. `dispatch` writes a denied row for an undeclared
  route or a missing key.
- **Blast radius.** An agent can reach only routes an app it can already see
  chose to open, and only while that app is enabled.
- **Who writes the declaration.** `agentRoutes` is only as trustworthy as the
  `app.json` it comes from. A registry app's checkout under `app-sources` is on
  the write-protected path list, so agent file tools cannot edit it. That
  protection covers file tools only: `app-sources` has no disposition in the OS
  sandbox, so a shell-capable agent can still edit an app's `app.json`. That
  differs from `config.json`, which the OS sandbox mounts read-only
  (`_CREW_READONLY_LEAVES` in `sandbox.py`). No signature check stops it in
  Phase 1 (Design §1). This is
  the threat Phase 1 leaves open: such an agent can add routes to an app's
  declaration and gain that app's own routes after the next app reload, never a
  core route. The same agent can also edit the app's handler code in that
  checkout, which runs with the app's own authority after a reload, so a guard
  on the declaration alone does not close the threat. Closing it needs a guard
  over the app's whole checkout whose secret the agent cannot read (open
  question 5).
- **Governance ceiling.** `app_request` sits under POLICY ∩ PROFILE through two
  existing scopes and adds none. As a `kirocrew-core` MCP tool it passes Plane A's
  `gate_decision` under the `mcp` scope, so a policy or a surface profile can deny
  `@kirocrew-core/app_request` like any other tool. The `apps` scope governs
  activation: an app the ceiling denies never activates, `app_request` refuses
  an app that is not enabled, and the `token_auth` arm does not engage for it.
  The gap is granularity. The `mcp` rule matches the tool, not its `app`
  argument, so a profile that allows `app_request` allows it toward every
  enabled app that declares routes. Per-app control today means denying the
  app's activation. See open question 2.

## Alternatives considered

1. **Keep patching core per app.** This is the status quo. It does not help
   registry apps, and each patch widens the hardcoded sets by hand.
2. **Let the app's own MCP server call its routes with the app secret.** This
   works today but carries the app's credential, so the handler cannot learn
   which session asked.
3. **Generate one tool per app, or per declared route, from the manifest.**
   More discoverable to the model, but it grows every session's tool list with
   each installed app and needs a naming scheme. Left as an open question.
4. **Admit in `token_auth` by matching declarations there.** This puts a second
   copy of route matching beside dispatch's, and the two can disagree on
   patterns or shadowed routes. Rejected for the single decision in dispatch.

## Open questions

1. Field name and shape: `agentRoutes` with `"METHOD /path"` strings (proposed),
   or objects that also carry a description for the model.
2. One generic tool (proposed) or per-app generated tools (Alternative 3).
   Per-app tools would let an `mcp` scope rule allow or deny each app's routes
   separately. With one tool, that needs either a governance rule that matches
   the `app` argument or reliance on the `apps` activation scope.
3. Whether to ship before the first consumer is public. The consumer is a
   registry app outside this repository, so no app in this repository uses the
   field when Phase 1 merges.
4. Whether a security owner should sign off on the new `token_auth` arm before
   Phase 1 merges, as the reviewer of #17015 asked.
   **Ruled by bolichen97 on 2026-10-25 ([comment](https://github.com/kirodotdev/KiroCrew/pull/17015#issuecomment-6396479581)):**
   yes. This RFC merges first, then #17015 is re-checked against it and gets a
   human security review of the internal-secret admission arm in `token_auth`.
5. What should close the manifest-edit threat that Phase 1 leaves open. A
   shell-capable agent can edit any app's `app.json`, signed or not, and the
   POC HMAC trust key is readable by that agent, so a signature gate built on
   it binds nothing and, with empty default `trust_keys`, also disables the
   feature on a stock install. The same agent can rewrite the app's handler
   code too, so any guard must cover the whole checkout, not only `app.json`;
   a guard on the declaration alone leaves the handler path open. Candidates:
   an asymmetric publisher signature over the checkout
   verified against a public key pinned in the admission policy, so the agent
   holds nothing it can sign with; or owner confirmation of each app's routes
   through an owner-authenticated dashboard action, snapshotted in gateway
   state and re-confirmed when the declaration or the checkout changes. Either is a follow-up
   phase, not part of Phase 1. Proposed: closing this threat is not a
   condition of acceptance. Phase 1 gives a shell-capable agent nothing it
   lacks today, since that agent can already rewrite any app's handler code.
   Until a follow-up closes it, no document may describe `agentRoutes` as a
   boundary against such an agent.

Acceptance: _none recorded yet._

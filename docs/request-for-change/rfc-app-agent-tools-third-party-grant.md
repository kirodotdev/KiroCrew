---
title: "Third-party app agent tools: an owner grant on top of the prompted server"
status: accepted
author: pearcekieser, with kirocrew-worker
created: 2026-11-12
last-audited: 2026-11-12
audited-at: 029b6db029
doc-pr: 35966
implementation-prs: []
tracking-issues: [32727]
supersedes: []
superseded-by: []
---

# RFC: Third-Party App Agent Tools Grant

> **Status:** `accepted`. Maintainer bolichen97 signed the direction on
> 2026-11-12 in a review of
> [#35966](https://github.com/kirodotdev/KiroCrew/pull/35966), and answered the
> three open questions. Open questions below records the answers, and the
> design follows them. Nothing in this document is on main. The baseline was verified at `029b6db029`:
> `src/kiro_crew/apps/app_agent_tools.py` reads the `agentTools` block of a
> first-party app only (`_first_party_apps`), and no grant family named
> `agentTools` is registered in `src/kiro_crew/apps/point_grants.py`. The
> prompted `kirocrew-apps` server that this document builds on is open as
> [#17015](https://github.com/kirodotdev/KiroCrew/pull/17015). A maintainer
> approved its head `f4b502fd` on 2026-11-12, and a later push dismissed that
> approval. This document specifies the owner grant part of section 7 of
> [rfc-app-contribution-points.md](rfc-app-contribution-points.md) for
> third-party apps, as that section's last line asks. Phase 1 builds only
> that part.

## Summary

A third-party app's manifest `agentTools` reach an agent through the
`kirocrew-apps` server only when three things all hold. Security considerations names a direct path that
skips the second and the third:

1. The owner approved that tool for that app. This is a new staged grant
   family, `agentTools`, with the same widening rule as `permissions.api`.
2. The calling agent mounts the prompted `kirocrew-apps` server. This is the
   opt-in server from #17015.
3. The person approves the call. `kirocrew-apps` is never auto-approved, so
   kiro-cli asks before each call.

This is the effective-authority rule of section 7: app grant AND caller
authority AND per-call approval. #17015 gives the second and the third parts.
This document adds the first part. It also lets an app's own scoped agent reach
its own approved tools, which #17015 withholds.

## Motivation

### Current state

Since #34762, an app's secret is in a store that no sandbox reads. A backend
gets the secret at spawn in `KIROCREW_PROXY_SECRET`, and code in the gateway
process gets an app token from `ctx.app_token()` (#35533). An app's agent MCP
servers get neither. `docs/app-kit/api-reference.md` says so:
the stdio `mcpServers` that an app's agents declare, which kiro-cli starts in
the agent sandbox, "get neither a secret nor an app token, so they cannot call
the app's own routes."

Issue [#32727](https://github.com/kirodotdev/KiroCrew/issues/32727) asked how a
third-party app's `agentTools` can be listed. Its option (c) was "not at all,
so third-party apps keep shipping their own `mcpServers` entry." After #34762,
option (c) does not work: such a server has no credential to call its app.
Thus, on main, a third-party app has no supported path from its agent to its
own routes.

#17015 implements option (a) of #32727. It adds `kirocrew-apps`, an opt-in
managed server that lists the installed third-party apps' tools. The server has
no `autoApprove` and is never in `allowedTools`, so each call asks. Its route,
`POST /api/app-tools/prompted-call`, resolves third-party declarations only and
refuses a call from a session that another app owns (403 `cross_app`).

### Problems that remain after #17015

1. No owner grant. An agent that mounts `@kirocrew-apps` can call every tool of
   every installed third-party app. The owner never approves the tool set. An
   update can add a tool, or change a tool's route or schema, and the new
   declaration lists at once. Section 7 says: "Approving the app does not
   expose its tools," and a new tool or a looser schema "needs approval."
2. A scoped app agent has no channel. #17015 withholds `kirocrew-apps` whole
   from an app agent whose manifest declares `permissions.mcpTools`
   (`_OPEN_ENDED_MANAGED_SERVERS`). The reason is correct: the exclusion list is
   built at materialization, and the server's tool names depend on what is
   installed at call time. But an app that scopes its agent, which is the safer
   choice, thus loses the only path to its own routes.
3. The audit row of a call names the server and the tool, not the grant that
   allowed it. Section 7 asks for "caller → app → tool, with the grant that
   allowed it."

## Goals

- The owner approves a third-party app's agent tools, and a change to a tool
  needs a new approval.
- An app's own scoped agent can reach the app's own approved tools, and no
  other app's tools.
- Each call through `kirocrew-apps` still asks the person. Nothing in this
  document adds an auto-approval.
- Each call's audit row names the grant entry that allowed it.

## Non-goals

- Grants per crew, crewmate or session. Section 7 names this finer target. This
  document grants per app as the first step. See Alternatives.
- First-party apps. Their tools stay on `kirocrew-core`, with no grant family.
- A default mount of `kirocrew-apps` on any agent. #17015 keeps it opt-in, and
  this document does not change that.
- `strictSession` for third-party tools. #17015 lists no third-party tool that
  declares it, and this document keeps that rule.
- A credential for an app's own stdio `mcpServers`. See Alternatives.
- The `outputSchema` validation, the `effect` label and the schema drift rule
  of section 7 in `rfc-app-contribution-points.md`. A later RFC specifies them.

## Design

### 1. The `agentTools` grant family

Register one staged grant family, `agentTools`, through the registry in
`src/kiro_crew/apps/point_grants.py`. The family is in
`STAGED_GRANT_FAMILIES` (`apps/manager_records.py`) through
`point_grant_families()`, so every update and approval reads it with no new
code path. Each writer of a first approved set needs one new rule, which
section 2 describes.

One entry is one tool: `<tool name>@<digest>`. The digest is the first 12 hex
characters of SHA-256 over the canonical JSON of these fields: the block's
route, and the tool's `title`, `description`, input schema, `timeoutSeconds`,
`result`, `strictSession` and `maxResultChars`. A change to any of these gives
a new entry, so the update rule treats it as widening. A higher
`maxResultChars` lets more of the app's output reach the model, so it is in the
digest.

The digest includes `title` and `description`, as answer 3 in Open questions
decides. Changed display text can change what the model does with the tool, so
a changed description needs a new approval. The adopted first-party path also
compares the display text: `_within_wheel_copy` in `apps/app_agent_tools.py`
compares every field of the declaration.

The route is one value for the whole block (`AgentToolsConfig.route` in
`apps/manifest_agent_tools.py`), and every tool of the block is served from it.
An update that changes the route thus gives every tool of the app a new entry
and holds back every tool.

`grant_source` (`point_grants.py`) reads `permissions` and `contributes`.
`agentTools` is a top-level manifest key, so the family needs a third
registry kind that passes the whole manifest to its entry function.

The family is `explicit_only=True`, like the user-session reach family
(`USER_SESSION_REACH_GRANT_FAMILIES` in `point_grants.py`). With the rule in
section 2, a record whose stored set predates the family thus approves no
agent tool. The owner approves on the app detail page.

### 2. Install, update and approval

- Install: the install screen lists each declared tool by name and
  description, but the install approves no tool. The owner approves agent
  tools in a separate step after install. Answer 1 in Open questions decides
  this. It is a new install rule. On main, each writer of a first approved set
  stores every declared entry of each staged family, explicit-only families
  included. These writers are the install (`manager_install.py`),
  `_freeze_declared` (`manager_records.py`), the two first-manifest paths of
  an external registration (`manager_external.py`) and the built-in writer
  (`manager_builtins.py`). Phase 1 makes each of them store an empty
  `agentTools` set. The built-in writer is in the list because an adopted
  former built-in keeps its stored set, and the app counts as third-party if
  its pin trust ends. `_declared_grant_entries` still lists
  `agentTools`, because the update rule and `grants_consent` read the declared
  set from it.
- Update: `_approved_grants_after_manifest_change`
  (`manager_records.py`) keeps only the entries that the new manifest
  still declares. A new or changed tool is held back. The app stays enabled
  with its old tools.
- Approval: the owner approves held-back tools on the app detail page, through
  `enable_app(grants_consent=...)` (`apps/manager_activation.py`). That
  path requires an owner dashboard request, and it adds only entries that the
  owner was shown and the live manifest still declares.
- The detail page shows each held-back tool by name and description, not by
  its digest.

### 3. The reader and the listing

`third_party_tools` (from #17015) keeps a tool only when
`staged_app_grants(app, "agentTools", ...)` (`manager_records.py`)
holds its entry. This is the same read that `app_holds_worker_auto_approve`
(`apps/worker_auto_approve.py`) does for its family.

The gated reader runs in the gateway only. The `kirocrew-apps` server is a
stdio server that kiro-cli starts, and on the default launch path it runs in
the session sandbox. The sandbox masks `member-records/`, so a read there
finds an empty approved set (`apps/protected_app_state.py`). A server that ran
the gated reader itself would thus list no tool and refuse every call.

So the server never calls the reader. In #17015 the server also calls
`third_party_tool` for the argument check, the call timeout and the answer
shape, and this design removes those calls. Add a strict internal route,
`POST /api/app-tools/prompted-list`, for the `kirocrew-apps` server, with the
same session key as `/api/app-tools/prompted-call`. Like that route, it accepts
a caller that holds the gateway's internal secret. The route runs the gated
reader and returns the full declaration of each approved tool. The server uses
these declarations for `tools/list`, for the argument check before each call,
for the call timeout, and for the result shape and the `maxResultChars` cap of
the answer. The gateway does not cap the answer, so the route must return every
field that the server reads. The gateway reads the grant file at each request, so an
approval lists the tool at the next `tools/list` and a held-back tool stops
listing at once.

The call route reads the same gated reader, so a tool that is not approved gets
404 `unknown_tool` from `/api/app-tools/prompted-call` even if a stale listing
named it.

### 4. An app's own scoped agent

A scoped app agent of app X can name `@kirocrew-apps/<tool>` in
`permissions.mcpTools` for a tool that X's own manifest declares. For such an
agent, the materializer mounts `kirocrew-apps` with those tool refs only,
instead of withholding the server whole.

The open-ended problem in #17015 is a deny list built at materialization. This
design uses an allow list instead: the agent's refs name X's tools, and those
names are fixed in X's manifest. A tool that another app installs later is not
in the list, so the agent never gets it. Two checks at call time keep this
true:

- The listing route returns, for a session that app X owns, only X's approved
  tools.
- The route already refuses a call from a session that another app owns
  (`cross_app`). It also refuses, for a session that app X owns, a tool that X
  does not declare.

The exact mechanism for the allow list (per-tool refs in `tools` with
`honourToolRefs`, or a `managedToolPolicy` include list) is for the phase 2 PR
to choose, as answer 2 in Open questions decides.

### 5. Per-call approval

No change from #17015. `kirocrew-apps` stays in `PROMPT_ONLY_MCP_SERVERS`, and
`may_skip_gate` never skips the gate for a ref that names it. A grant in
section 1 makes a tool exist for the app. It never makes a call skip the
prompt.

### 6. Audit

The tool-call audit row of each `/api/app-tools/prompted-call` call adds the
grant entry (`<tool name>@<digest>`) that allowed it, next to the calling
session, the app and the tool.

## Migration plan

Each phase is one PR. #17015 must merge before phase 1 starts.

| Phase | Change | Exit criteria |
|---|---|---|
| 1 | The `agentTools` family, the gated reader, the listing route, the install and detail page display, and the audit entry. | A test installs a third-party app with two tools and finds none listed. A record that `_freeze_declared` fixes, both first-manifest paths of an external registration and the built-in writer also store no `agentTools` entry. `grants_consent` for both entries lists both. An update that adds a third tool and changes the input schema of the first lists only the second, and the result names the two held-back tools. `grants_consent` for the held-back entries lists all three. An update that changes the block's route holds back every tool. An update that raises only a tool's `maxResultChars` holds that tool back, and so does an update that changes only a tool's `description`. A record that predates the family lists nothing. A `kirocrew-apps` server that cannot read `member-records/` lists the approved tools and calls one of them through to the app, because it takes the declarations from the listing route. A call to a held-back tool gets 404 `unknown_tool`. The audit row of a call names its entry. |
| 2 | An app's own scoped agent reaches its own approved tools. The phase 2 PR chooses the allow-list mechanism. | A scoped app agent of X that names `@kirocrew-apps/x_ping` can call `x_ping` and gets a prompt. It cannot list or call a tool of app Y, including one that Y installs after the agent was materialized. A scoped agent that names a tool X does not declare gets no mount. |
| 3 | Optional: grants per crew or session, as section 7 describes. | Not specified here. It needs its own revision of this document. |

The App Kit seam level goes up by one in phase 1, in
`src/kiro_crew/apps/app_sdk_level.py` and `website/src/app-sdk/sdkLevel.ts`.

## Backward compatibility

- First-party tools do not change.
- No default agent changes. `kirocrew-apps` stays opt-in.
- A third-party app installed before phase 1 lists no agent tool until the
  owner approves its tools. This removes reach that #17015 gives. If phase 1
  ships in the same release as #17015, no released install loses a tool. If a
  release ships #17015 without phase 1, the phase 1 PR and its release note
  say that every third-party agent tool stops listing until the owner
  approves it.

## Security considerations

- For an agent call through `kirocrew-apps`, effective authority is the AND of
  three checks, and each one fails closed. No stored entry lists nothing, no
  mount reaches nothing, and kiro-cli runs no call that the person did not
  approve. The next bullet names a path that skips the second and the third,
  and also the own-app limit of section 4.
- A process that holds the gateway's internal secret can post the listing and
  call routes directly, with no approval prompt. The session sandbox can read
  that secret, because `.local_secret` and the `run/` folder are on the visible
  list in `sandbox_leaves.py`. The `run/` folder holds `gateway-<port>.secret`
  and `gateway-<port>-<address>.secret`. So any
  process in the sandbox, an agent shell command
  included, can skip the prompt this way. This direct path also skips the
  own-app limit of section 4. In #17015 the token middleware takes the
  calling app from the `X-Session-Key` header that the caller sends. A key
  that no app owns, an empty one included, skips the `cross_app` check. On this path, only the owner grant still holds. The
  gated reader still refuses a tool that has no stored entry. Main has the
  same gap for
  `/api/app-tools/call`, and this document does not close it.
- The grant file is in `member-records/app-grants/<app>.json`
  (`apps/protected_app_state.py`). The OS sandbox masks it and the agent file
  tools refuse it, so an app cannot approve its own tools.
- The digest entry makes a changed route, schema or description a new grant.
  Without it, an update could point an approved tool name at a different route.
- The listing route and the call route run the gated reader in the gateway. A
  process in the sandbox cannot read the grant file, so it cannot widen the
  listing by reading or writing it.
- The tool code runs in the app's own process and receives no core credential.
  #17015 removes core credentials from the request that an app handler gets.

## Alternatives considered

### A scoped app token for the app's own stdio `mcpServers`

At session start, mint an app token and put it in the environment of each
stdio server that the app's agent declares. The session MCP path for builtin
app servers (`kiro_control_plane_servers` in `src/kiro_crew/acp/session_mcp.py`)
already adds a per-session element.

Rejected, for three reasons:

- An app token reaches every path under `/apps/<name>/` and `/api/apps/<name>/`
  with every method, plus the approved `permissions.api` prefixes
  (`app_token_path_allowed`, `dashboard/token_auth_app_scope.py`). An
  `agentTools` call reaches one declared route with `POST` and arguments that
  match a schema.
- The token is in the environment of a process in the agent's process tree. If
  an injected prompt makes the agent read it, the agent can call those routes
  with no prompt.
- #34762 removed the secret from places that the agent sandbox reads. This
  alternative gives a credential back to the same sandbox.

### Tool name as the grant entry

Simpler to read, but an update could change an approved tool's route or schema
with no new approval. Rejected for the reason in Security considerations.

### List every declared tool, and gate only the call

The server keeps the ungated reader of #17015, and only
`/api/app-tools/prompted-call` checks the grant. This needs no listing route.
Rejected: the agent sees tools that the owner never approved, and a held-back
tool lists but each call to it fails with 404 `unknown_tool`.

### Grants per crew or session now

Section 7 describes this finer target. It needs a per-caller grant store and a
UI that does not exist yet. A per-app grant is the base that a per-caller grant
narrows, so phase 1 does not block it.

### A per-tool approval inside `kirocrew-core`

Option (b) of #32727. The default agent grants `@kirocrew-core` whole
(`src/kiro_crew/config/defaults.json`), and kiro-cli has no "all of a server
except these tools" form. Every core tool would need its own entry in the
default `allowedTools`. #17015 chose a separate server, and this document
keeps that choice.

## Open questions

Maintainer bolichen97 answered all three questions on 2026-11-12, in a review
of [#35966](https://github.com/kirodotdev/KiroCrew/pull/35966).

1. Is install-time approval correct for this family, or must the owner approve
   agent tools in a separate step after install? Answer: "approval is a
   separate step after install, not implied by installing, matching other
   explicit-only grants." Installing approves no tool, and section 2 follows
   this answer. On main, a first install approves the declared entries of the
   explicit-only `reachableByUserSessions` family too, so section 2 states
   this as a new install rule for `agentTools`.
2. Which mechanism gives a scoped app agent an allow list on `kirocrew-apps`:
   per-tool refs in `tools` with `honourToolRefs`, or a `managedToolPolicy`
   include list? Answer: the phase 2 PR chooses the mechanism. Phase 1 keeps
   to per-tool owner approval.
3. Must a tool's `title` and `description` be part of the digest? Changed
   display text can change what the model does with the tool, but it does not
   change what the tool can reach. Answer: yes. The digest includes both, so a
   changed description needs a new approval. Section 1 follows this answer.

Acceptance: bolichen97 signed the direction on 2026-11-12 in a review of
[#35966](https://github.com/kirodotdev/KiroCrew/pull/35966).

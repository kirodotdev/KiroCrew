---
title: Crew API — a stable, versioned HTTP API for clients of the Kiro Crew gateway
status: draft
author: headless-crew (crewmate), with kirocrew-worker
created: 2026-10-31
last-audited: 2026-10-31
audited-at: 4e605b0d2b
doc-pr:
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Crew API

- Status: draft. Nothing is built. This document changes no code.
- Machine-readable contract: the OpenAPI 3.1 drafts in [`assets/crew-api/`](assets/crew-api/crew-public.yaml):
  `crew-public.yaml` (the Public tier), `crew-owner.yaml` (the Owner tier) and `components.yaml`
  (shared schemas). Both `openapi-spec-validator` and `@redocly/cli lint` pass on them with the
  bundled `redocly.yaml`.
- Size: the drafts define 59 operations in full. 48 are Public and 11 are new Owner operations.
  One more Owner route wraps an existing route and is not counted in the 59. The count is a
  proposal: these drafts still have to be merged, operation by operation, with a second draft of
  this API, which is not in this repository, into one specification.

## Motivation

Kiro Crew needs a stable, versioned HTTP API that clients other than the built-in dashboard can rely on.

## Goals

1. One versioned API contract that apps can build on, compatible across gateway upgrades.
2. An app acts only within the scopes its credential carries, and only on what it created.
3. An app can never answer an approval. Approvals stay with the person.
4. An existing OpenAI Responses client works by changing its base URL.

## Non-goals

- Implementation. This RFC defines the contract only.
- How credentials are issued, refreshed or revoked, and how a request reaches the gateway. The
  contract only names the two issuers a delegated credential can have (see
  [Authentication](#1-authentication)).
- How the gateway decides when to rate limit, throttle or pause an app. The API only defines the
  error codes it returns.
- Freezing the dashboard's own `/api/*` routes. They stay Internal.

## Design

### 1. Authentication

Requests carry a bearer credential. Each credential carries two things:

- **Scopes**: what the user granted this app.
- **Entitlements**: what Kiro allows this app to ask for. An entitlement is a second gate beside
  the scope: some scopes can be requested only by an app that holds the matching entitlement
  (see [Scopes and entitlements](#32-scopes-and-entitlements)).

Three credential kinds appear in the contract:

| Credential | Represents | Can do |
|---|---|---|
| Delegated credential | An app acting for the user, under one **grant** | Only what its scopes allow, and only on resources created under its grant |
| The user's own credential | The user (a first-party sign-in) | Everything, including the Owner tier |
| Embed credential | The user, bound to one conversation, short-lived | One thing only: answer approvals in that conversation (see [Approvals](#6-approvals)) |

A delegated credential has two issuers, and both put the same claims in the token:

1. The Kiro authorization service, for an app that reaches the user's Kiro Crew from another
   machine.
2. **Local authorization**, for a client on the same machine: it runs authorization code + PKCE
   against the local gateway with a loopback redirect, and the gateway signs the token with a
   local key. This is an official phase 1 path, not a development shortcut. A grant from local
   authorization starts with only the scopes that need no entitlement.

### 2. Three tiers

| Tier | Caller | In the SDK | Promise |
|---|---|---|---|
| Public | Apps holding a delegated credential | Yes: `@kiro/crew`, `@kiro/crew/ui` | Versioned; inside `/v1` the server only adds and never refuses a client for an old minor version |
| Owner | The user's own clients: dashboard, control panel, official app, CLI | Yes: `@kiro/crew/owner` | Versioned; every endpoint is under `/v1/crew/owner/`; marked experimental, may change faster (see [Versioning](#9-versioning-and-compatibility)) |
| Internal | The gateway itself, MCP servers, agent harnesses, dashboard pages | No | None |

Rules:

1. **One tier per endpoint.** Existing `/api/*` routes default to Internal. A route moves up to Owner
   only with a written reason.
2. **A delegated credential or an embed credential calling any non-Public endpoint gets 403
   `owner_only`**, before the handler runs, whatever its scopes. It is kept apart from
   `insufficient_scope`: a missing scope is something the user can grant on the consent page, and
   Owner access is something a third-party app can never get.
3. **Check order.** Identify the credential kind, then the tier, then the binding (instance, grant
   and resource ownership), then the entitlement, then the scope. This runs before any handler's own
   owner check (`src/kiro_crew/dashboard/handlers/_shared_access.py`), so a delegated credential
   never reaches one.
4. **Internal never enters the SDK** and is not documented as a contract.
5. **The Owner tier is for the owner only.** A dashboard session that is not the owner's, such as a
   Shared Crew teammate's, gets 403 `owner_only` like a delegated credential does
   ([Q5](#open-questions)).
6. **A refusal on an Owner route is 403 `owner_only`, not 404.** The `/v1` route table is a public
   document, so a 404 would hide nothing and would only make "no permission" read as "wrong path".
7. **Every Owner endpoint is under `/v1/crew/owner/`**, so the path alone tells the tier. A resource
   has the same name in both tiers: a Bot is `bots` in Public and in Owner ([Q7](#open-questions)).

**SDK.** Two client packages, with proposed names:

| Package | Language | Published to |
|---|---|---|
| `@kiro/crew` | TypeScript | npm |
| `kirocrew-client` | Python | PyPI |

Both live in the same repository as Kiro Crew and are released separately from it, so a third
party installs the client without installing Kiro Crew. Their version follows the Crew API
version, not the version of the Kiro Crew package: when the Crew API is at 1.3, both clients are
at 1.3.x, and the third number is for the client's own fixes.

`@kiro/crew` has four entry points:

| Entry | Contents |
|---|---|
| `@kiro/crew` | Public client: responses, conversations, events (cursor resume), approvals (read only), the app's own grant, Bots, artifacts, schedules, workflows, notifications. Every POST carries an `Idempotency-Key` by default |
| `@kiro/crew/ui` | UI components: the conversation view, and the approval card |
| `@kiro/crew/owner` | Owner client: answering approvals, managing Bots, writing memory, grants, capability bundles, the instance |
| `@kiro/crew/manifest` | Helpers for an app's manifest: types, validation against the manifest's JSON Schema, and its hashes |

The permission boundary is on the server, so an app that imports `@kiro/crew/owner` only gets 403s.
Subpaths let bundlers drop the Owner code while keeping one version number.

### 3. Public API

Two parts with one version, one error shape and one permission check:

- **A Responses-compatible subset**: `/v1/responses`, `/v1/conversations`, `/v1/models`, `/v1/files`.
- **`/v1/crew/*` extensions** for what Responses has no word for: capabilities, the app's own grant,
  events, approvals, Bots, artifacts, notifications, schedules, workflows, memory and knowledge
  search, webhooks.

There are two streams, and each does one job. The stream of one turn is the Responses subset: the
SSE of `POST /v1/responses`. Everything outside a turn is `GET /v1/crew/events`: tool records, plan
updates, approvals, conversation changes, notices and catching up after a disconnect. Between the
gateway and its engine the protocol is ACP, and it stays there: a third party never speaks ACP.

| Object | What it is |
|---|---|
| `conversation` | A chat session in the user's Kiro Crew. Its id (`conv_…`) is external; internal keys never appear |
| `response` | One turn. A conversation runs at most one turn at a time |
| `model` | The agent that runs the turn: the app's own agent, or a Bot the grant allows |
| output item | Reply text, a tool call, an approval request, or a reasoning summary |
| event | A record of something that happened in the user's Kiro Crew, filtered by permission |

#### 3.1 The Public endpoints

There are 48 Public operations, counted by method and path. Each belongs to one phase:

1. **Phase 1a** (18 operations): what every integration needs. Turns, conversations, events,
   approvals, `/v1/models`, capabilities, instance information and the app's own grant.
2. **Phase 1b** (13 operations): Bots, artifacts and notifications. They are marked **preview**
   (`x-crew-stability: preview` in the OpenAPI): their shape may still change, with notice for the
   preview deprecation period first (see [Versioning](#9-versioning-and-compatibility)).
3. **Phase 2** (14 operations): files, helper Bots, schedules, workflows, memory recall and knowledge
   search.
4. **Phase 3** (3 operations): webhooks.

| Method | Path | Scope | Entitlement | Phase | Purpose |
|---|---|---|---|---|---|
| POST | `/v1/responses` | `responses:write` | — | 1a | Run a turn |
| GET | `/v1/responses/{id}` | `conversations:read` or `responses:write` | — | 1a | Read a turn; resume its stream. With only `responses:write`, only turns this grant created |
| POST | `/v1/responses/{id}/cancel` | `responses:write` | — | 1a | Cancel a running turn |
| GET | `/v1/responses/{id}/input_items` | `conversations:read` | — | 1a | The turn's input |
| POST | `/v1/files` | `responses:write` | — | 2 | Upload a file for `input_image` or `input_file` |
| DELETE | `/v1/files/{id}` | `responses:write` | — | 2 | Delete a file this grant uploaded |
| POST | `/v1/conversations` | `responses:write` | — | 1a | Create a conversation |
| GET | `/v1/conversations` | `conversations:read` | — | 1a | List the grant's conversations |
| GET | `/v1/conversations/{id}` | `conversations:read` | — | 1a | Read a conversation |
| POST | `/v1/conversations/{id}` | `responses:write` | — | 1a | Update its metadata |
| DELETE | `/v1/conversations/{id}` | `responses:write` | — | 1a | Delete a conversation the grant opened |
| GET | `/v1/conversations/{id}/items` | `conversations:read` | — | 1a | History, paged |
| GET | `/v1/models` | any valid credential | — | 1a | Agents and Bots the credential can use |
| GET | `/v1/crew/capabilities` | any valid credential | — | 1a | API version, features, limits, quota |
| GET | `/v1/crew/info` | any valid credential | — | 1a | Instance id, display name, time zone, API version |
| GET | `/v1/crew/grant` | any delegated credential | — | 1a | The app's own grant: scopes, Bot range, quota, status, pause reason |
| GET | `/v1/crew/events` | `conversations:read` or `instance:read` or `bots:wake` | — | 1a | Event stream, filtered by the scopes held |
| GET | `/v1/crew/approvals` | `conversations:read` | — | 1a | List approvals (read only) |
| GET | `/v1/crew/approvals/{id}` | `conversations:read` | — | 1a | Read one approval |
| POST | `/v1/crew/approvals/{id}/decision` | embed credential only | — | 1a | Answer an approval |
| GET | `/v1/crew/bots` | `bots:chat` or `bots:read` | — | 1b, preview | Bots in the grant's range, with status |
| POST | `/v1/crew/bots` | `bots:create` | `bots.create` | 2 | Create a helper Bot from a template in the app's manifest |
| GET | `/v1/crew/bots/{id}/goals` | `bots:read` | — | 1b, preview | A Bot's goals, key results and readings |
| GET | `/v1/crew/bots/{id}/reports` | `bots:read` | — | 1b, preview | A Bot's weekly and monthly reports |
| POST | `/v1/crew/bots/{id}/wake` | `bots:wake` | — | 1b, preview | Wake a Bot; the body carries only a reference number |
| GET | `/v1/crew/artifacts` | `artifacts:read` | — | 1b, preview | List the grant's artifacts |
| POST | `/v1/crew/artifacts` | `artifacts:write` | — | 1b, preview | Create an artifact |
| GET | `/v1/crew/artifacts/{id}` | `artifacts:read` | — | 1b, preview | Read one |
| PATCH | `/v1/crew/artifacts/{id}` | `artifacts:write` | — | 1b, preview | Update one |
| DELETE | `/v1/crew/artifacts/{id}` | `artifacts:write` | — | 1b, preview | Delete one the grant created |
| GET | `/v1/crew/artifacts/{id}/versions` | `artifacts:read` | — | 1b, preview | Its versions |
| GET | `/v1/crew/artifacts/{id}/comments` | `artifacts:read` | — | 1b, preview | Its comments |
| POST | `/v1/crew/artifacts/{id}/comments` | `artifacts:write` | — | 1b, preview | Post a comment |
| POST | `/v1/crew/notifications` | `notifications:send` | `notifications.send` | 1b, preview | Notify the user |
| GET | `/v1/crew/schedules` | `schedules:manage` | `schedules.manage` | 2 | List the grant's schedules |
| POST | `/v1/crew/schedules` | `schedules:manage` | `schedules.manage` | 2 | Create a schedule |
| PATCH | `/v1/crew/schedules/{id}` | `schedules:manage` | `schedules.manage` | 2 | Change, pause, resume |
| DELETE | `/v1/crew/schedules/{id}` | `schedules:manage` | `schedules.manage` | 2 | Delete |
| POST | `/v1/crew/schedules/{id}/run` | `schedules:manage` | `schedules.manage` | 2 | Run once now |
| GET | `/v1/crew/workflows/runs` | `workflows:run` | `workflows.run` | 2 | List the grant's workflow runs |
| POST | `/v1/crew/workflows/runs` | `workflows:run` | `workflows.run` | 2 | Start a run |
| GET | `/v1/crew/workflows/runs/{id}` | `workflows:run` | `workflows.run` | 2 | Status and result |
| POST | `/v1/crew/workflows/runs/{id}/cancel` | `workflows:run` | `workflows.run` | 2 | Cancel a run |
| POST | `/v1/crew/memory/recall` | `memory:read` | `memory.read`, `memory.global` | 2 | Recall from the user's Global memory |
| POST | `/v1/crew/knowledge/search` | `knowledge:read` | `knowledge.read` | 2 | Search the user's knowledge base |
| GET | `/v1/crew/webhooks` | `webhooks:manage` | — | 3 | List webhook subscriptions |
| POST | `/v1/crew/webhooks` | `webhooks:manage` | — | 3 | Subscribe to event types |
| DELETE | `/v1/crew/webhooks/{id}` | `webhooks:manage` | — | 3 | Delete a subscription |

`DELETE /v1/responses/{id}` and `POST /v1/conversations/{id}/items` return 405: a turn is part of a
conversation's history, and inserting items without running a turn would show the agent a
conversation that never happened. `/v1/chat/completions` is not Public: it is a frozen Owner
transition route (see [Versioning](#9-versioning-and-compatibility)).

Memory recall and knowledge search are POSTs with the query in the request body. The query is the
user's private content, and in a URL it would land in the access logs of everything between the
app and the gateway.

**Ownership.** A delegated credential sees and changes only what its own grant created: the
conversations it opened, the artifacts and files it saved, the schedules and workflow runs it
started. Anything else answers 404, so the app cannot learn it exists. `/v1/models` and
`/v1/crew/bots` list only what the credential can use. Workflows can be started only if the user
saved them and marked them "allow third parties"; schedules run only an agent message, never a
script or command.

#### 3.2 Scopes and entitlements

| Scope | Allows | Delegated credential may hold it | Default on the consent page |
|---|---|---|---|
| `responses:write` | Open conversations, run and cancel turns, delete its own conversations, read the turns it created | Yes | — |
| `conversations:read` | Read its own conversations, their events and approvals | Yes | Checked |
| `instance:read` | Receive instance-level notices sent to this grant that belong to no conversation | Yes | — |
| `bots:read` | Read the roster and status of the Bots in the grant's range, and their goals, key results, progress and reports. Never private chat, briefing or rules | Yes | Checked |
| `bots:chat` | Chat with the Bots in the grant's range. In such a conversation the Bot has none of its usual tools and reads no private memory | Yes | **Not checked by default** ([Q12](#open-questions)) |
| `bots:wake` | Wake a Bot in the grant's range, passing only a reference number | Yes | Not checked by default |
| `bots:create` | Create helper Bots from templates in the app's manifest | Yes | Not checked by default |
| `artifacts:read` | Read its own artifacts and comments | Yes | Checked |
| `artifacts:write` | Create, update and delete its own artifacts and comments | Yes | Checked |
| `notifications:send` | Notify the user, by default at most 10 per hour per grant (past that, 429 `notification_limited`) | Yes | Checked |
| `schedules:manage` | Its own schedules | Yes | Not checked by default |
| `workflows:run` | Start saved workflows marked "allow third parties" | Yes | Not checked by default |
| `memory:read` | Recall from the user's Global memory, never a Bot's private memory; works only when the grant's `memory_global` is `true` | Yes | Not checked by default, shown prominently |
| `knowledge:read` | Search the user's knowledge base | Yes | Not checked by default, shown prominently |
| `webhooks:manage` | Choose webhook event types | Yes | — |
| `approvals:answer` | Answer approvals | **No** | — |
| `bots:manage`, `memory:write`, `knowledge:write` | Change Bots, write memory and lessons, add documents | **No** | — |
| `grants:manage`, `instance:manage` | Manage grants and the instance | **No** | — |

A grant also carries a **Bot range**: `selected` (the Bots the user ticked), `app_created` (only the
Bots this app created) or `all`. A Bot outside the range is 404 to the app, and `model` cannot name
it. "—" in the last column means the default is not decided here.

There are eight entitlements. The gateway checks the entitlement before the scope: a missing
entitlement is 403 `entitlement_required`, and an entitlement without the scope is 403
`insufficient_scope`.

| Entitlement | What it lets the app do |
|---|---|
| `memory.read` | Request the scope `memory:read` |
| `memory.global` | Let the user turn on the grant's `memory_global`. A third party can read only Global memory, so recall needs both `memory.read` and `memory.global` |
| `knowledge.read` | Request the scope `knowledge:read` |
| `workflows.run` | Request the scope `workflows:run` |
| `schedules.manage` | Request the scope `schedules:manage` |
| `notifications.send` | Request the scope `notifications:send` |
| `bots.create` | Request the scope `bots:create`, and ship a capability bundle that creates a Bot with private memory |
| `quota.elevated` | Use the elevated quota tier |

In phase 1 the only entitlements are local-development ones, which an owner sets for apps they
registered on their own Kiro Crew. Review by Kiro starts in phase 2.

#### 3.3 The Responses subset

The rule has three parts:

1. Support what the user's Kiro Crew can do.
2. Refuse with 400 a field that would change what runs or what is stored: `tools` outside the
   manifest, `store: false`, an unsupported tool type, `prompt`.
3. Accept and ignore a field that only tunes generation or does bookkeeping, and list it in the
   response's `crew.ignored_parameters`. OpenAI SDKs and agent frameworks send more fields with each
   release; refusing them would break an app when it upgrades its SDK although Kiro Crew changed
   nothing.

| Field of `POST /v1/responses` | Handling |
|---|---|
| `model` | Required. For a delegated credential: the app's agent, or a Bot the grant allows. Anything else is 404 `model_not_found` |
| `input` | A string, or message items with roles `user`, `assistant`, `developer`, `system` and `input_text` content |
| `input_image`, `input_file` | Phase 2, per agent: reported in capabilities as `input_modalities`. An agent without the modality answers 422 `capability_unavailable`. `input_file` takes only a `file_id` from `POST /v1/files` |
| `function_call_output` | Supported when the agent has the `client_tools` capability |
| `mcp_approval_response` | Owner only: the user's own credential. A delegated credential and the embed credential both get 403 `approval_requires_first_party`; the embed credential answers only through the decision endpoint |
| `instructions` | Appended after the agent's own prompt; never replaces it |
| `conversation`, `previous_response_id` | Either one; both must point to the same conversation. Without either, a new conversation starts |
| `stream` | Server-sent events |
| `background` | Returns `queued` at once; read the result later or follow the event stream |
| `metadata` | At most 16 pairs, as in OpenAI |
| `tools` of type `function` or `mcp` | Must be a subset of the tools in the manifest the user consented to: same name, server URL, parameter schema and approval setting; `always` cannot become `never`. Any mismatch is 403 `tool_not_registered` |
| Other tool types | 400 `unsupported_tool_type` |
| `tool_choice` | `auto` or `none`; any other value is 400 |
| `store` | Only `true`; `false` is 400. Conversations always live in the user's Kiro Crew |
| `text.format` | `text` in phase 1; `json_schema` in phase 2, when the agent reports `structured_output`. A reply that does not match the schema fails the turn with `error.code` `structured_output_invalid` |
| `reasoning.summary` | Phase 2, off by default. Summaries only, never the raw reasoning |
| `prompt` | 400 `unsupported_parameter`. It points to a prompt template stored at OpenAI, which Kiro Crew cannot fetch |
| `include`, `truncation`, `service_tier`, `user`, `parallel_tool_calls`, `max_tool_calls`, `top_logprobs`, and other OpenAI fields the server does not know | Accepted and ignored; listed in `crew.ignored_parameters` |
| `temperature`, `top_p`, `max_output_tokens` | Accepted and ignored; listed in `crew.ignored_parameters`. The user's Kiro Crew picks the model and the quota |

The Response object carries `id`, `status` (`queued`, `in_progress`, `requires_action`, `completed`,
`failed`, `cancelled`, `incomplete`), `model`, `output`, `error`, `incomplete_details`,
`conversation`, `metadata` and `usage`. `requires_action` means the turn is waiting for the user to
answer an approval. Output items are `message`, `function_call`, `mcp_call` (arguments and output as
summaries only), `mcp_approval_request` and, in phase 2, `reasoning`. `usage` is filled when token
counts are known and is `null` otherwise, never a made-up zero.

The stream sends the standard `response.*` lifecycle, output item, content part, text delta,
function call argument and `response.mcp_call.*` events, plus `error`. Crew-only events use a
`crew.` prefix (for example `crew.notice`); clients skip the ones they do not know. Each event's
`sequence_number` counts within that turn only and feeds
`GET /v1/responses/{id}?stream=true&starting_after=N`.

Tool progress and plan updates are not in this stream. The only tool events in it are the ones
Responses already has, the start and end of an MCP call. The record of each tool call
(`tool.called`, `tool.completed`) and the agent's plan (`plan.updated`) are in the conversation's
event stream only (see [Event stream](#5-event-stream)). A screen that shows the plan while text
streams opens both connections. The reason: this stream stays to events an OpenAI client already
knows, and tool records and plans have to be replayable by cursor after the turn's stream closed.

### 4. Errors, request id, quota headers, idempotency

#### 4.1 Error shape and codes

One body for every error, shaped like OpenAI's:

```json
{"error": {"type": "permission_error", "code": "insufficient_scope",
           "message": "needs responses:write", "param": null, "request_id": "req_…"}}
```

Every refusal under `/v1/` uses this body, including refusals from middleware (credential checks,
tier checks, rate limits), so a client parses one format. The one exception is
`/v1/chat/completions`: it is frozen as it is, so the refusals it already has keep their shape and
status. The body can carry extra fields:
`entitlement`, `oldest_seq` and `latest_seq`, `retry_after`, `final`, and `limit` (which
concurrency or wake limit was hit).

| HTTP | `type` | `code` | When |
|---|---|---|---|
| 400 | `invalid_request_error` | `invalid_request` | Malformed field |
| 400 | `invalid_request_error` | `unsupported_parameter`, `unsupported_tool_type` | A refused field or tool type |
| 401 | `authentication_error` | `invalid_token` | Missing credential, or a bad signature or expiry. Refresh the token and retry once |
| 401 | `authentication_error` | `token_revoked`, `third_party_disabled` | The grant was revoked, or the user turned off third-party access |
| 401 | `authentication_error` | `stale_session_reauth` | Owner tier only: this first-party session signed in before an owner was configured; sign in again |
| 403 | `permission_error` | `entitlement_required` | The app lacks the entitlement this capability needs; the body's `entitlement` names it. Checked before the scope |
| 403 | `permission_error` | `insufficient_scope` | Missing scope. The user can grant it on the consent page |
| 403 | `permission_error` | `owner_only` | A delegated or embed credential called an Owner endpoint |
| 403 | `permission_error` | `approval_requires_first_party` | A delegated credential tried to answer an approval |
| 403 | `permission_error` | `tool_not_registered` | A tool in the request is not in the grant's manifest |
| 403 | `permission_error` | `connector_definition_changed` | The app's manifest changed its tools or permissions and the user has not consented again. The grant is paused with `pause.by: reconsent` and returns only this code |
| 403 | `permission_error` | `grant_paused` | The grant is paused by the user, by the gateway's monitor, or because the app lost an entitlement. It is 403 and not 429: waiting does not help, and SDKs retry 429 by themselves |
| 403 | `permission_error` | `use_responses_api` | A delegated credential called `/v1/chat/completions` |
| 404 | `not_found_error` | `not_found`, `model_not_found` | Missing, or the caller may not know it exists |
| 405 | `invalid_request_error` | `method_not_supported` | A method this API does not support |
| 409 | `conflict_error` | `response_in_progress` | The conversation already has a turn running |
| 409 | `conflict_error` | `approval_already_resolved` | Someone answered first; the body's `final` carries the result |
| 410 | `resync_required_error` | `resync_required` | The event cursor is stale or untrustworthy; the body carries `oldest_seq` and `latest_seq` |
| 413 | `invalid_request_error` | `payload_too_large` | Input too large |
| 422 | `invalid_request_error` | `idempotency_key_reused` | The same `Idempotency-Key` with a different body |
| 422 | `capability_error` | `capability_unavailable` | The agent or this Kiro Crew cannot do it |
| 429 | `rate_limit_error` | `rate_limited` | Too many requests; with `Retry-After` |
| 429 | `rate_limit_error` | `concurrency_limit` | A concurrency limit was hit; `limit` says which: `turns` (running and queued turns are full) or `event_streams` (too many event streams). With `Retry-After` |
| 429 | `rate_limit_error` | `quota_exhausted` | The grant's credit quota is used up; `Retry-After` runs until the quota resets |
| 429 | `rate_limit_error` | `notification_limited` | The grant's notification limit is reached; with `Retry-After` |
| 500 | `server_error` | `internal_error` | Gateway error. Retry with the same `Idempotency-Key` |
| 503 | `server_error` | `instance_unavailable` | The user's Kiro Crew is offline |
| 503 | `server_error` | `prune_in_progress` | Only on endpoints that read the Bot roster: the gateway is still tidying it after a start |

Both `type` and `code` are open sets: a client treats an unknown `code` by its HTTP status.

Every 401 carries the header `WWW-Authenticate: Bearer error="invalid_token"` (RFC 6750), so a
client can tell "get a new credential" from "no permission".

OpenAI SDKs retry 408, 409, 429 and 5xx by themselves. Three errors gain nothing from that, and a
client turns the retry off or handles them itself:

1. 409 `response_in_progress`: wait for the turn to end (`turn.completed` on the event stream).
2. 403 `grant_paused`: the user has to resume the grant. `GET /v1/crew/grant` gives the reason.
3. 429 `quota_exhausted`: `Retry-After` can be hours; wait for it, not for the SDK's own backoff.

Two results are not HTTP errors:

- `structured_output_invalid` is the reason on a turn's result. The request returns 200 and the
  Response object has `status: failed`.
- A tool an app installed into the user's Kiro Crew can be removed, revoked by the app's site or
  paused. The MCP proxy then answers the tool call with a tool result that has `isError: true` and
  the same `type` and `code` pair: `tools_removed`, `tools_revoked_by_app`, `grant_paused` or
  `connector_definition_changed`.

#### 4.2 Request id

Every request has an id. It is returned in `X-Request-Id`, written to the gateway's audit log, and
placed in the error body as `request_id`. An Owner client may supply its own. In the Public tier
the id is assigned in front of the gateway or generated by the gateway; an id a third party sends
is not used as the audit key.

#### 4.3 Quota headers

Every response to a delegated-credential request, success or failure, carries:

| Header | Meaning |
|---|---|
| `X-Crew-Quota-Limit` | The grant's daily credit ceiling |
| `X-Crew-Quota-Remaining` | What is left today, after reservations |
| `X-Crew-Quota-Reset` | When it resets, in Unix seconds |
| `Retry-After` | On a 429: seconds to wait |

The embed-only approval endpoint carries none of these. A turn that stops on a per-turn limit is not
an error: it ends `incomplete` with reason `turn_token_limit`, `tool_call_limit` or
`turn_time_limit`. `GET /v1/crew/capabilities` reports the grant's quota and usage, and
`GET /v1/crew/grant` reports the grant's scopes, status and pause reason.

#### 4.4 Idempotency-Key

**Every Public POST accepts an `Idempotency-Key` header**, not only the ones that spend credits.
Within one grant, a retry with the same key within 24 hours returns the first result (the same
object id), is not run a second time and is not charged again. The same key with a different body
is 422 `idempotency_key_reused`. The reason for "every": a retried request that creates a schedule
would create two schedules, and deciding POST by POST which ones are safe to repeat is easy to get
wrong.

### 5. Event stream

`GET /v1/crew/events` serves two kinds of stream with one cursor rule:

| Stream | Request | Contents |
|---|---|---|
| Conversation stream | `?conversation={id}&since={c}` | Every event of one conversation |
| Instance stream | `?since={c}` | Events that belong to no conversation (a new conversation, a revoked grant, a notice, a Bot's reply to a wake), plus a hint per conversation that it has new events |

```json
{"seq": 43, "event_id": "evt_…", "type": "approval.requested",
 "conversation": "conv_…", "response": "resp_…", "created_at": 1761822000,
 "data": {"approval_id": "apr_…", "tool": "docs.publish", "reason": "Publish the document"}}
```

Event types: `conversation.created`, `conversation.updated`, `turn.started`, `message.created`,
`tool.called`, `tool.completed`, `plan.updated`, `approval.requested`, `approval.decided`,
`turn.completed`, `grant.revoked`, `notice`, `bot.reply`, `bot.turn.completed`. The set is open.
Events carry ids, not message bodies; bodies are read from `/v1/conversations/{id}/items` under the
same permission check.

Tool and plan events use four status words, the same ones ACP uses: `pending`, `in_progress`,
`completed`, `failed`. `tool.called` and `tool.completed` share one `call_id`; `tool.completed`
carries the status and never the tool's returned content. `plan.updated` carries the agent's whole
task list each time, and the client replaces its copy.

| Rule | Meaning |
|---|---|
| `seq` | Strictly increasing within one stream. A conversation stream is numbered per conversation; the instance stream is numbered separately for each grant, so an app cannot read the user's activity elsewhere from the numbers. Numbers can still be skipped: a skipped number is an event this caller may not see. A client stores and sends back `seq` and does no arithmetic on it |
| `event_id` | Globally unique. Deduplicate across reconnects and replays by it |
| `since=c` | Every visible event with `seq > c`, in order |
| No `since` | Only events from now on |
| `c` older than retained, inside a truncated range, or beyond the newest `seq` | 410 `resync_required` with `oldest_seq` and `latest_seq` |
| Delivery | An event is stored before it is sent, so a client may see an event twice but never misses a visible event: one it is allowed to see. A skipped `seq` number is not a missed event |
| Slow client | Disconnected rather than dropping events; it reconnects with its cursor |
| Filtering | On the server only, by the scopes held: `conversations:read` gets the events of the grant's conversations, `instance:read` the instance-level notices sent to the grant, `bots:wake` the Bot events of the turns the grant woke |
| Retention | Stored responses, the event index, resolved approvals, uploaded files and monitor decisions are kept 30 days by default (a proposal); the owner can set 1 to 365 days. Past it, a cursor gets 410 and a response 404 |

**SSE.** With `Accept: text/event-stream`, each message's `id:` is the `seq` and `event:` is the
type, so the browser's `Last-Event-ID` on reconnect is the cursor. A comment line every 15 seconds
keeps an idle connection alive. Without SSE, the endpoint returns paged JSON
(`{"events": [...], "latest_seq": N, "has_more": bool}`, `limit` up to 1000, `limit=0` returns only
`latest_seq`) with optional long polling (`wait`, up to 30 seconds). One user, one Kiro Crew and one
grant may hold at most 10 stream connections in total, and the whole instance has a configurable
ceiling of 100 by default (both proposals); past either, 429 `concurrency_limit` with
`limit: event_streams`.

**Resume after a 410**, in this order so nothing is missed: take `S = latest_seq` from the 410 body,
read the current state (conversation items, `GET /v1/crew/approvals?state=pending`), replay from
`since=S`, and drop what was already seen by id.

### 6. Approvals

An approval is the user's Kiro Crew pausing before a tool call until a person says yes.

| Rule | Meaning |
|---|---|
| **A delegated credential can never answer an approval** | The app writes the prompt and chooses the tools; if it could approve, it would approve itself. Answering through `mcp_approval_response` or the decision endpoint returns 403 `approval_requires_first_party` |
| Public answer path | `POST /v1/crew/approvals/{id}/decision` with `{"decision": "allow" \| "deny"}`. Accepts only the embed credential, for its one conversation. This is the embed credential's only way to answer |
| Owner answer paths | The Owner approval endpoint with `allow`, `deny` or `allow_always`, and `mcp_approval_response` in `/v1/responses`. The reused route's `reject_once` is written in the contract as `deny` with `scope: once` |
| Visibility | Every caller that can read the conversation sees `mcp_approval_request` and `approval.requested`, so an app can tell the user why a turn is waiting. The turn's status is `requires_action` meanwhile |
| Approval page address | A proposal ([Q6](#open-questions)): the `mcp_approval_request` item and the Approval object carry `approval_url`, a page under the Kiro Crew host name that shows this one approval. An app that has only a backend cannot answer, but it can hand this address to the user |
| First answer wins | A later answer gets 409 `approval_already_resolved` with the final result |
| Id | `apr_` plus 128 random bits, unique across the instance; the gateway finds the conversation from the id |
| Durable | Stored before it is announced; survives a gateway restart |
| States | `pending`, `allowed`, `denied`, `expired`. When a turn is cancelled, its waiting approval becomes `expired` with reason `turn_cancelled` |
| Timeout | Unanswered after 4 minutes 30 seconds, the gateway answers `deny` and records `expired` with reason `timeout`. The limit sits under the 5 minutes after which the engine drops a pending request. The turn itself goes on |

### 7. Webhooks

Webhooks are phase 3. The callback address comes **only** from `callbacks.webhook_url` in the app's
manifest, the one the user consented to. The API only selects event types: `POST /v1/crew/webhooks`
takes `{"events": [...]}` and no URL. The first event types are `turn.completed`,
`approval.requested` and `grant.revoked`.
Callbacks carry ids, never content; the app reads content through the API. Subscriptions are
deleted with their grant and are not retried after that.

Each callback is a `POST` with a JSON body (`id`, `type`, `created_at`, `grant_id`, `conversation`,
`response`) and two headers:

| Header | Value |
|---|---|
| `X-Crew-Timestamp` | Send time in Unix seconds |
| `X-Crew-Signature` | `sha256=<hex>`, where `<hex>` is the lowercase hex HMAC-SHA256 of `timestamp + "." + raw_body` under the app's webhook secret; `timestamp` is the `X-Crew-Timestamp` value and `raw_body` the exact request body |

Receivers recompute the signature over the raw body, compare in constant time, and reject a
timestamp older than 5 minutes. Any non-2xx answer counts as one failed delivery. The gateway sends
only to public addresses and does not follow redirects. The webhook secret is issued once when the
app is registered and can be rotated; how it is issued is out of scope for this RFC.

### 8. Owner tier

Every Owner endpoint is under `/v1/crew/owner/`. The tier has two kinds of endpoint:

1. **New operations**, defined in full in `crew-owner.yaml`. There are 11: seven for grants and
   connectors, four for capability bundles.
2. **Wrappers**: an existing `/api/*` route gets a `/v1/crew/owner/*` path over the same handler and
   the same data. A wrapper is not a new operation and is not counted in the 11.

The 11 new operations all need `grants:manage`, which only the user's own credential holds:

| Method | Path | Phase | Purpose |
|---|---|---|---|
| GET | `/v1/crew/owner/grants` | 1a | List grants: which app, which scopes, when consented, last used, usage |
| DELETE | `/v1/crew/owner/grants/{id}` | 1a | Revoke a grant. The user can disconnect an app from the first day |
| PATCH | `/v1/crew/owner/grants/{id}` | 2 | Tighten only: lower the quota, remove Bots, lower limits, pause |
| POST | `/v1/crew/owner/grants/{id}/resume` | 2 | Resume a paused or throttled grant |
| GET | `/v1/crew/owner/grants/{id}/decisions` | 2 | Read the monitor's decisions for this grant |
| GET | `/v1/crew/owner/connectors` | 2 | List the app manifests the user consented to |
| GET | `/v1/crew/owner/connectors/{client_id}` | 2 | Read one manifest and its hash |
| GET | `/v1/crew/owner/bundles` | 2 | List capability bundles that came from apps |
| GET | `/v1/crew/owner/bundles/{id}` | 2 | Read one bundle, with the grants a removal would disconnect |
| PATCH | `/v1/crew/owner/bundles/{id}` | 2 | Change a bundle's settings: each tool's approval, and the plan approval switch |
| DELETE | `/v1/crew/owner/bundles/{id}` | 2 | Disconnect the app from this Kiro Crew: revokes every grant that references the bundle |

A **capability bundle** is what an app installs into the user's Kiro Crew when the user consents:
one agent, its MCP servers, skills and client tools. It has no Public endpoint; it follows the
consent and the grant.

Grant states are `active`, `throttled`, `paused` and `revoked`. A paused grant records `pause.by`:
`monitor` (the gateway paused it automatically), `user`, `reconsent` (it waits for the user to agree
to a changed manifest) or `entitlement` (the app lost an entitlement; the grant resumes by itself
when it is back). A grant also carries its Bot range, its wake limits, `notify_per_hour` (default
10) and `memory_global` (default `false`), which `memory:read` needs before
`/v1/crew/memory/recall` answers. Its `kind` is `remote` (issued by the Kiro authorization service)
or `local` (local authorization, issued by this gateway).

**The one wrapper so far** is the Bot roster. In the contract its path is `GET /v1/crew/owner/bots`,
and it wraps `GET /api/members`. Its code is in
[#34104](https://github.com/kirodotdev/KiroCrew/pull/34104), which is not merged, and there the
route is `GET /v1/crew/owner/members`: it keeps the name `members` today because it wraps the body
of the existing `/api/members` unchanged. Renaming it to `bots` is proposed
([Q7](#open-questions)). It is the Owner view of the same roster the Public `GET /v1/crew/bots`
projects:

| | Owner roster | Public `GET /v1/crew/bots` |
|---|---|---|
| Caller | The owner's own client | A third-party app with a delegated credential |
| Bots listed | Every global Bot | Only the grant's Bot range |
| Each row | Name, private chat binding and its last line, configuration, live state | Only id, name, description, avatar and status |
| Private chat, briefing, rules | The last line of the private chat is present | Never |

**Candidates for more wrappers.** The route inventory assigns 96 existing routes (distinct method
and path) to the Owner tier: conversations, approvals, events, models, Bots, memory, schedules,
artifacts, workflows, notifications, instances, sign-in, and `/v1/chat/completions`. They are
listed with their source in `x-crew-reused-routes` of `crew-owner.yaml` and marked experimental.
They are candidates, not a promise. A route is wrapped only when one of three callers really needs
it:

1. The official app: answering approvals, the Bot roster, grant management.
2. The command line: status, version, upgrade.
3. The multi-instance console: `/v1/crew/owner/instance/*`.

Chat is not in that list. The official app and the dashboard's chat page use the Public
`/v1/responses`, conversations and events, the same endpoints a third party gets.

**Routes outside the 59.** The gateway has more entry points than the 59 operations above. They are
listed here so the count is not read as "everything the gateway exposes":

| Routes | What they do | Who can call | Phase | Specification |
|---|---|---|---|---|
| `/v1/crew/owner/instance/*` | Control plane: health, configuration, upgrade and rollback, start and stop | The owner | 3 | No OpenAPI yet |
| The wrapped Owner route for the Bot roster | Wraps `GET /api/members` | The owner | 1b | In `crew-owner.yaml`; code in #34104, not merged |
| Local authorization: `GET /oauth/authorize`, `POST /oauth/token`, `POST /oauth/revoke`, `GET /.well-known/oauth-authorization-server` on the local gateway | A client on the same machine asks for a grant | Programs on the same machine; consent needs the owner in the dashboard | 1a | No OpenAPI; parameters follow OAuth 2.1 and RFC 8414 |
| Embed page: `GET /embed` and its sign-in routes | The embedded page where the user signs in and answers approvals; `/embed?approval=<id>` shows one approval | Reachable without a token; reads no conversation data | 2 | No OpenAPI yet |
| Site connection: `GET /connect/start`, `GET /connect/callback` | Connects the user's account at an app's site when the app's MCP server needs it | Reachable without a token; accepts only a one-time code and `state` | 2 | No OpenAPI yet |
| `POST /v1/chat/completions` | Frozen transition route | The owner and local apps | Exists today | Not specified here |
| Every other `/api/*` route | Internal | The gateway and the dashboard | — | None |

The routes that are reachable without a token need a specification as detailed as the `/v1`
operations before phase 2 starts ([Q8](#open-questions)).

### 9. Versioning and compatibility

| Level | Where | Changes when |
|---|---|---|
| Major | The path, `/v1/` | Only for a breaking change; `/v1/` is served for at least 24 months after `/v2/` is released (proposal, not final) |
| Minor | The `Crew-API-Version` request header; the response header of the same name carries the server's version | Each addition. The request header only declares which minor version the client was written for |

The minor version is the Crew API's own number and has nothing to do with the version of the Kiro
Crew package.

Rules inside `/v1`:

1. Within a major version the server only adds: endpoints, optional fields, event types, enum
   values, error codes.
2. It never removes or renames a field, changes a type or meaning, makes an optional field required,
   changes a default, or moves an error code to a different HTTP status.
3. The server never refuses a request because the declared minor version is old. A client written
   for 1.0 works against every 1.N server, and `/v1` has no "version too old" error.
4. A client that sends no `Crew-API-Version` header gets 1.0 behaviour. Existing OpenAI clients
   never send the header, and what they get must not move when the server upgrades.
5. When a client declares a minor version, the server may leave out optional fields that are newer
   than that version. Nothing else changes.
6. When a client declares a minor version higher than the server's, the server answers as usual and
   reports its own version in the response header.
7. Clients ignore unknown fields, skip unknown event types, treat unknown enum values as "other",
   and treat unknown error codes by HTTP status.

Deprecation periods are calendar time:

| What | Shortest time from deprecation to removal | Notice |
|---|---|---|
| Public | Nothing is removed inside `/v1`. A deprecated item keeps working until `/v1` as a whole shuts down | A `Deprecation` header on the response, and the replacement written in the contract |
| Public operations marked preview (phase 1b) | 12 months (proposal, not final) | Same |
| `/v1` as a whole | 24 months after `/v2` is released (proposal, not final) | Same, plus a `Sunset` header with the shutdown date |
| Owner | 3 months (proposal, not final; [Q4](#open-questions)) | Same; marked experimental in the SDK |

These periods are proposals without usage data behind them. Their one basis: third-party apps
release about once a quarter, so 12 months lets an app follow in two or three releases, and 24
months gives an app nobody maintains time to be taken down.

The server side of the Crew API is not one cloud service. Each user's Kiro Crew upgrades on its
own, so one app's backend faces many versions at once. The client side therefore has rules too:

1. A client probes features and does not compare version numbers. After connecting it calls
   `GET /v1/crew/capabilities` and decides from `features` and `agents`. `api_version.current` is
   for display and troubleshooting.
2. Using a feature the server lacks returns 422 `capability_unavailable` (the endpoint exists but
   this Kiro Crew cannot do it) or 404 `not_found` (this version has no such endpoint). Neither is
   retried.
3. A client at 1.N can connect to a Kiro Crew at any 1.M.

`GET /v1/crew/capabilities` reports the server's version, features, per-agent abilities, limits,
retention and the grant's quota. A feature reported as `false` answers 422
`capability_unavailable` instead of degrading silently. The gateway never reports a capability it
cannot confirm.

**`/v1/chat/completions` is frozen** as an Owner transition route for the user's own clients and
local apps. It stays as it is and gets no new features. A delegated credential calling it gets 403
`use_responses_api`.

## Security considerations

- **Fail closed on identity.** An unknown credential kind, a non-Public endpoint, a resource from
  another grant, a missing entitlement or a missing scope is refused before any handler runs.
- **No self-approval.** A delegated credential cannot answer approvals by construction.
- **No existence leaks.** Resources outside the grant answer 404, not 403. The instance stream is
  numbered per grant, so sequence numbers do not show activity elsewhere.
- **Content stays in the user's Kiro Crew.** Events and webhooks carry ids, never message bodies.
  Search queries travel in request bodies, not URLs.
- **Two gates on sensitive capabilities.** Memory, knowledge, schedules, workflows and
  notifications need an entitlement the app holds and a scope the user granted.
- **Residual risk.** What an app lets the agent read (memory, knowledge, tool output) can flow to
  that app's own tools. `memory:read` and `knowledge:read` therefore never reach a Bot's private
  memory, and no scope can write memory.

## Alternatives considered

| Question | Chosen | Not chosen | Why |
|---|---|---|---|
| Compatible surface | OpenAI Responses subset | Chat Completions; a new API only | Responses has conversations, tool calls and approval items; Chat Completions has none of them |
| Stream shape | The stream of one turn follows the Responses subset; everything else is Kiro Crew's own event stream, `/v1/crew/events` | An ACP-shaped stream to third parties; the shape of another vendor's model API; only our own event set | ACP assumes the other side is the user's own interface, which answers approvals and can read files; a third-party app can do neither. ACP's remote transport is still a proposal. The gateway already turns each engine's ACP into its own events, so sending ACP out would save no conversion. With the Responses shape, a language the SDK does not cover can use an existing OpenAI client as transport |
| Tool progress and plan updates | Only on the event stream | Extra `crew.*` events in the turn's stream | The turn's stream stays to events an OpenAI client knows; these records must be replayable by cursor after a disconnect |
| Unsupported fields | Refuse with 400 what changes what runs or what is stored; ignore what only tunes generation, and list it | Ignore all; refuse all | Ignoring silently hides that a field had no effect, so ignored fields are listed; refusing all breaks an app each time it upgrades its OpenAI SDK |
| A client on an old minor version | Never refused inside `/v1` | Compatible with the previous minor version only, then 400 | Each user's Kiro Crew upgrades on its own; a version floor would break live apps within weeks |
| Existing `/api/*` routes | Internal by default, promoted with a reason | Freeze them as the contract | The dashboard depends on them in many places; freezing would stop it changing |
| Where Owner endpoints live | All under `/v1/crew/owner/` | Mixed with Public under `/v1/crew/`, told apart by a per-endpoint list | The path tells the tier; middleware and anything in front of the gateway can refuse by prefix |
| SDK packaging | One package, four entry points | Two packages; one entry point | One version number; the boundary is on the server anyway |
| Capability bundles | No Public endpoint; they follow the consent and the grant, with settings in Owner | A separate install endpoint | Two life cycles would drift apart; a delegated credential stands for the app and must not install things by itself |
| Listing conversations | `GET /v1/conversations` | `/v1/crew/conversations` | No clash with OpenAI's paths; one fewer path to learn |
| Cursor | One increasing integer per stream; the instance stream numbered per grant | An opaque string; a timestamp; one numbering shared by all callers | Readable, comparable, and maps onto SSE `Last-Event-ID`. A shared numbering would show an app how active the user is elsewhere |
| Stale cursor | 410 `resync_required` | Silently replay from the oldest | A silent replay lets the client think nothing was missed |
| Slow client | Disconnect it | Drop events | The contract promises no missed visible event |
| Minor version | A request header | The path; a query parameter | URLs stay stable across minor versions |
| Webhook address | Only from the consented manifest | Set through the API | An API-set address would let an app redirect callbacks the user never agreed to |
| Approval answers in Public | Embed credential only | Delegated credentials too | An app could otherwise approve its own tool calls |

## Open questions

| # | Question | Leaning |
|---|---|---|
| Q1 | How a conversation stream's `seq` stays continuous when one conversation runs on several engine sessions in turn | The event layer keeps the mapping; the crew log format does not change. The contract stays the same either way: one increasing integer per stream |
| Q2 | How these drafts and a second draft of this API, which is not in this repository, become one specification | Merge operation by operation into one file kept in this repository; the count of 59 changes with it |
| Q3 | Minor version format | Integer, `1.N` |
| Q4 | The Owner tier's compatibility promise | Three months from deprecation to removal (proposal, not final); experimental in the SDK |
| Q5 | Whether a dashboard session that is not the owner's may use the Owner tier | No. What such a session needs goes through the Public tier with its own credential; until then it keeps using `/api/*` |
| Q6 | How a user answers an approval when the app has only a backend: the `approval_url` field, a reminder to the owner over the gateway's existing Web Push, and making the engine's 5-minute limit configurable | Do all three, in phase 2 |
| Q7 | Owner naming: everything under `/v1/crew/owner/`, the resource called `bots` in both tiers, renaming the wrapper in #34104 from `members`, and which existing routes are worth wrapping | As proposed; wrap only what the three callers in [Owner tier](#8-owner-tier) need |
| Q8 | The periods in [Versioning](#9-versioning-and-compatibility) (12 months for preview operations, 24 months for `/v1`), how old a Kiro Crew the contract tests cover (proposal: releases of the last 12 months), and who specifies the routes that are reachable without a token | All proposals without usage data |
| Q9 | Helper Bot templates and the per-grant cap | At most 16 helper Bots per grant (proposal) |
| Q10 | Whether the stream shape in [Public API](#3-public-api) stands: Responses subset for a turn, Kiro Crew's own event stream for the rest, no ACP-shaped stream to third parties | Yes |
| Q11 | Whether to declare compatibility with the Open Responses specification, which wants vendor extensions named with a `crew:` prefix where this RFC writes `crew.` | Change the prefix now, while no app depends on it; decide on the declaration after checking the whole specification |
| Q12 | When `bots:chat` becomes checked by default on the consent page | After the rules for a conversation that uses a Bot as its `model` are reviewed |
| Q13 | Who holds the names `@kiro/crew` and `kirocrew-client`, and whether they are published from this repository | The Kiro team holds both; this repository's CI publishes them |
| Q14 | Which real clients the compatibility check pins besides `openai-python` and `openai-node` | The two agent frameworks third parties use most |

## Appendix: the OpenAPI drafts

| File | Contents |
|---|---|
| [`crew-public.yaml`](assets/crew-api/crew-public.yaml) | The 48 Public operations: request and response schemas, examples, security and scope, entitlement, phase, error responses, SSE event lists, and the `webhooks` callback |
| [`crew-owner.yaml`](assets/crew-api/crew-owner.yaml) | The 11 new Owner operations in full, the one wrapped route, and the 96 candidate routes as a list |
| [`components.yaml`](assets/crew-api/components.yaml) | The error body, pagination, the Response object, stream and instance events, quota headers, `Idempotency-Key`, request id, webhook signature headers, tool errors |
| [`redocly.yaml`](assets/crew-api/redocly.yaml) | Lint configuration; run `npx @redocly/cli lint` in that directory |

Each operation carries `x-crew-tier`, `x-crew-scope`, `x-crew-phase`, `x-crew-reuses` (existing code
it can build on), `x-crew-coverage` and, where they apply, `x-crew-entitlement`,
`x-crew-stability` (`preview` for phase 1b) and `x-crew-depends-on` (an open question above).

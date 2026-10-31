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

## Motivation

Kiro Crew needs a stable, versioned HTTP API that clients other than the built-in dashboard can rely on.

## Goals

1. One versioned API contract that apps can build on, compatible across gateway upgrades.
2. An app acts only within the scopes its credential carries, and only on what it created.
3. An app can never answer an approval. Approvals stay with the person.
4. An existing OpenAI Responses client works by changing its base URL.

## Non-goals

- Implementation. This RFC defines the contract only.
- How credentials are issued, refreshed or revoked, and how a request reaches the gateway.
- How the gateway decides when to rate limit, throttle or pause an app. The API only defines the
  error codes it returns.
- Freezing the dashboard's own `/api/*` routes. They stay Internal.

## Design

### 1. Authentication

Requests carry a bearer credential; each credential carries a set of scopes; how credentials are
issued is out of scope for this RFC.

Two credential kinds appear in the contract:

| Credential | Represents | Can do |
|---|---|---|
| Delegated credential | An app acting for the user, under one **grant** | Only what its scopes allow, and only on resources created under its grant |
| The user's own credential | The user | Everything, including the Owner tier |

The approval endpoint also accepts an **embed credential**: a short-lived credential that
represents the user and is bound to one conversation. It can do one thing only, answer approvals in
that conversation (see [Approvals](#6-approvals)).

### 2. Three tiers

| Tier | Caller | In the SDK | Promise |
|---|---|---|---|
| Public | Apps holding a delegated credential | Yes: `@kiro/crew`, `@kiro/crew/ui` | Versioned, backward compatible (N-1) |
| Owner | The user's own clients: dashboard, control panel, CLI | Yes: `@kiro/crew/owner` | Versioned, marked experimental, may change faster |
| Internal | The gateway itself, MCP servers, agent harnesses, dashboard pages | No | None |

Rules:

1. **One tier per endpoint.** Existing `/api/*` routes default to Internal. A route moves up to Owner
   only with a written reason; the Owner list in `crew-owner.yaml` is that list.
2. **A delegated credential calling any non-Public endpoint gets 403 `insufficient_scope`**, before
   the handler runs, whatever its scopes.
3. **Check order.** Identify the credential kind, then the tier, then the binding (grant and
   resource ownership), then the scope. This runs before any handler's own owner check
   (`src/kiro_crew/dashboard/handlers/_shared_access.py`), so a delegated credential never reaches
   one.
4. **Internal never enters the SDK** and is not documented as a contract.

**SDK.** One package, three entry points:

| Entry | Contents |
|---|---|
| `@kiro/crew` | Public client: responses, conversations, events (cursor resume), approvals (read only), mates, artifacts, schedules, workflows, notifications, memory and knowledge search |
| `@kiro/crew/ui` | UI components: the conversation view, and the approval card |
| `@kiro/crew/owner` | Owner client: answering approvals, managing mates and grants |

The permission boundary is on the server, so an app that imports `@kiro/crew/owner` only gets 403s.
Subpaths let bundlers drop the Owner code while keeping one version number.

### 3. Public API

Two parts with one version, one error shape and one permission check:

- **A Responses-compatible subset**: `/v1/responses`, `/v1/conversations`, `/v1/models`.
- **`/v1/crew/*` extensions** for what Responses has no word for: capabilities, events, approvals,
  mates, artifacts, notifications, schedules, workflows, memory and knowledge search, webhooks.

| Object | What it is |
|---|---|
| `conversation` | A chat session in the user's crew. Its id (`conv_…`) is external; internal keys never appear |
| `response` | One turn. A conversation runs at most one turn at a time |
| `model` | The agent that runs the turn |
| output item | Reply text, a tool call, or an approval request |
| event | A record of something that happened in the crew, filtered by permission |

#### 3.1 The 42 Public endpoints

| Method | Path | Scope | Purpose |
|---|---|---|---|
| POST | `/v1/responses` | `responses:write` | Run a turn |
| GET | `/v1/responses/{id}` | `conversations:read` | Read a turn; resume its stream |
| POST | `/v1/responses/{id}/cancel` | `responses:write` | Cancel a running turn |
| GET | `/v1/responses/{id}/input_items` | `conversations:read` | The turn's input |
| POST | `/v1/conversations` | `responses:write` | Create a conversation |
| GET | `/v1/conversations` | `conversations:read` | List the grant's conversations |
| GET | `/v1/conversations/{id}` | `conversations:read` | Read a conversation |
| POST | `/v1/conversations/{id}` | `responses:write` | Update its metadata |
| DELETE | `/v1/conversations/{id}` | `responses:write` | Delete a conversation the grant opened |
| GET | `/v1/conversations/{id}/items` | `conversations:read` | History, paged |
| GET | `/v1/models` | any valid credential | Agents and mates the credential can use |
| GET | `/v1/crew/capabilities` | any valid credential | Version range, features, limits, quota |
| GET | `/v1/crew/info` | any valid credential | Instance id, display name, time zone, API version |
| GET | `/v1/crew/events` | `conversations:read` | Event stream |
| GET | `/v1/crew/approvals` | `conversations:read` | List approvals (read only) |
| GET | `/v1/crew/approvals/{id}` | `conversations:read` | Read one approval |
| POST | `/v1/crew/approvals/{id}/decision` | embed credential only | Answer an approval |
| GET | `/v1/crew/mates` | `mates:chat` | Mates the app may chat with |
| GET | `/v1/crew/mates/{id}/goals` | `mates:read` | A mate's goals, key results and readings |
| GET | `/v1/crew/mates/{id}/reports` | `mates:read` | A mate's weekly and monthly reports |
| GET, POST | `/v1/crew/artifacts` | `artifacts:read`, `artifacts:write` | List and create the grant's artifacts |
| GET, PATCH | `/v1/crew/artifacts/{id}` | `artifacts:read`, `artifacts:write` | Read and update one |
| GET | `/v1/crew/artifacts/{id}/versions` | `artifacts:read` | Its versions |
| GET, POST | `/v1/crew/artifacts/{id}/comments` | `artifacts:read`, `artifacts:write` | Its comments |
| POST | `/v1/crew/notifications` | `notifications:send` | Notify the user |
| GET, POST | `/v1/crew/schedules` | `schedules:manage` | List and create the grant's schedules |
| PATCH, DELETE | `/v1/crew/schedules/{id}` | `schedules:manage` | Change, pause, resume, delete |
| POST | `/v1/crew/schedules/{id}/run` | `schedules:manage` | Run once now |
| GET, POST | `/v1/crew/workflows/runs` | `workflows:run` | List and start the grant's workflow runs |
| GET | `/v1/crew/workflows/runs/{id}` | `workflows:run` | Status and result |
| POST | `/v1/crew/workflows/runs/{id}/cancel` | `workflows:run` | Cancel a run |
| GET | `/v1/crew/memory/recall` | `memory:read` | Recall from the user's Global memory |
| GET | `/v1/crew/knowledge/search` | `knowledge:read` | Search the user's knowledge base |
| GET, POST | `/v1/crew/webhooks` | `webhooks:manage` | List and create webhook subscriptions |
| DELETE | `/v1/crew/webhooks/{id}` | `webhooks:manage` | Delete a subscription |

Counted by method and path, that is 42. `DELETE /v1/responses/{id}` and
`POST /v1/conversations/{id}/items` return 405: a turn is part of a conversation's history, and
inserting items without running a turn would show the agent a conversation that never happened.

**Ownership.** A delegated credential sees and changes only what its own grant created: the
conversations it opened, the artifacts it saved, the schedules and workflow runs it started.
Anything else answers 404, so the app cannot learn it exists. `/v1/models` and `/v1/crew/mates`
list only what the credential can use. Workflows can be started only if the user saved them and
marked them "allow third parties"; schedules run only an agent message, never a script or command.

#### 3.2 Scopes

| Scope | Allows | Delegated credential may hold it |
|---|---|---|
| `responses:write` | Open conversations, run and cancel turns, delete its own conversations | Yes |
| `conversations:read` | Read its own conversations, events and approvals | Yes |
| `mates:chat` | List allowed mates and chat with them | Yes |
| `mates:read` | Read allowed mates' goals, key results, progress and reports | Yes |
| `artifacts:read` | Read its own artifacts and comments | Yes |
| `artifacts:write` | Create and update its own artifacts and comments | Yes |
| `notifications:send` | Notify the user, by default at most 10 per hour per grant (past that, 429 `notification_limited`) | Yes |
| `schedules:manage` | Its own schedules | Yes |
| `workflows:run` | Start saved workflows marked "allow third parties" | Yes |
| `memory:read` | Recall from the user's Global memory, never a crewmate's private memory; works only when the grant's `memory_global` is `true` | Yes |
| `knowledge:read` | Search the user's knowledge base | Yes |
| `webhooks:manage` | Choose webhook event types | Yes |
| `approvals:answer` | Answer approvals | **No** |
| `mates:manage`, `memory:write`, `knowledge:write` | Change mates, write memory and lessons, add documents | **No** |
| `grants:manage`, `instance:manage` | Manage grants and the instance | **No** |

A grant also lists the **mates** the user allows the app to use; with none, `mates:chat` lists
nothing and `model` cannot name a mate.

#### 3.3 The Responses subset

The rule: **support what the crew can do, refuse the rest with 400, never silently ignore a field.**
The exception is sampling: `temperature`, `top_p` and `max_output_tokens` are accepted and ignored,
because many clients send them by default and the user's crew picks the model. The response lists
them in `crew.ignored_parameters`.

| Field of `POST /v1/responses` | Handling |
|---|---|
| `model` | Required. For a delegated credential: the app's agent, or a mate the grant allows. Anything else is 404 `model_not_found` |
| `input` | A string, or message items with roles `user`, `assistant`, `developer`, `system` and `input_text` content |
| `input_image`, `input_file` | Open question ([Q2](#open-questions)); reported in capabilities |
| `function_call_output` | Supported when the agent has the `client_tools` capability |
| `mcp_approval_response` | Owner only: the user's own credential. A delegated credential and the embed credential both get 403 `approval_requires_first_party`; the embed credential answers only through the decision endpoint |
| `instructions` | Appended after the agent's own prompt; never replaces it |
| `conversation`, `previous_response_id` | Either one; both must point to the same conversation. Without either, a new conversation starts |
| `stream` | Server-sent events |
| `background` | Returns `queued` at once; read the result later or follow the event stream |
| `tools` of type `function` or `mcp` | Must be a subset of the tools recorded for the grant: same name, server URL, parameter schema and approval setting; `always` cannot become `never`. Any mismatch is 403 `tool_not_registered` |
| Other tool types | 400 `unsupported_tool_type` |
| `tool_choice` | `auto` or `none` |
| `store: false` | 400. Conversations always live in the user's crew |
| `reasoning`, `include`, `truncation`, `prompt`, `service_tier`, `user`, `parallel_tool_calls`, `max_tool_calls`, `top_logprobs` | 400 `unsupported_parameter` |

The Response object carries `id`, `status` (`queued`, `in_progress`, `completed`, `failed`,
`cancelled`, `incomplete`), `model`, `output`, `error`, `incomplete_details`, `conversation`,
`metadata` and `usage`. Output items are `message`, `function_call`, `mcp_call` (arguments and
output as summaries only) and `mcp_approval_request`. `usage` is filled when token counts are known
and is `null` otherwise, never a made-up zero.

The stream sends the standard `response.*` lifecycle, output item, content part, text delta,
function call argument and `response.mcp_call.*` events, plus `error`. Crew-only events use a
`crew.` prefix (for example `crew.notice`); clients skip the ones they do not know. Each event's
`sequence_number` counts within that turn only and feeds
`GET /v1/responses/{id}?stream=true&starting_after=N`.

### 4. Errors, request id, quota headers, idempotency

#### 4.1 Error shape and codes

One body for every error, shaped like OpenAI's:

```json
{"error": {"type": "permission_error", "code": "insufficient_scope",
           "message": "needs responses:write", "param": null, "request_id": "req_…"}}
```

| HTTP | `type` | `code` | When |
|---|---|---|---|
| 400 | `invalid_request_error` | `invalid_request` | Malformed field |
| 400 | `invalid_request_error` | `unsupported_parameter`, `unsupported_tool_type` | A refused field or tool type |
| 400 | `invalid_request_error` | `unsupported_api_version` | Declared minor version too old; the body carries the supported range |
| 401 | `authentication_error` | `invalid_token`, `token_revoked`, `third_party_disabled` | Missing or invalid credential, revoked grant, or the user turned off third-party access |
| 403 | `permission_error` | `insufficient_scope` | Missing scope, or a non-Public endpoint |
| 403 | `permission_error` | `approval_requires_first_party` | A delegated credential tried to answer an approval |
| 403 | `permission_error` | `tool_not_registered`, `connector_definition_changed` | A tool outside the grant, or the grant must be renewed |
| 403 | `permission_error` | `use_responses_api` | A delegated credential called `/v1/chat/completions` |
| 404 | `not_found_error` | `not_found`, `model_not_found` | Missing, or the caller may not know it exists |
| 405 | `invalid_request_error` | `method_not_supported` | A method this API does not support |
| 409 | `conflict_error` | `response_in_progress` | The conversation already has a turn running |
| 409 | `conflict_error` | `approval_already_resolved` | Someone answered first; the body carries the final result |
| 410 | `resync_required_error` | `resync_required` | The event cursor is stale or untrustworthy; the body carries `oldest_seq` and `latest_seq` |
| 413 | `invalid_request_error` | `payload_too_large` | Input too large |
| 422 | `invalid_request_error` | `idempotency_key_reused` | The same `Idempotency-Key` with a different body |
| 422 | `capability_error` | `capability_unavailable` | The agent or this crew cannot do it |
| 429 | `rate_limit_error` | `rate_limited` | Too many requests; with `Retry-After` |
| 429 | `rate_limit_error` | `concurrency_limit` | Too many turns running or queued for this grant or crew; with `Retry-After` |
| 429 | `rate_limit_error` | `quota_exhausted` | The grant's credit quota is used up; with `Retry-After` |
| 429 | `rate_limit_error` | `grant_paused` | The grant is paused until the user resumes it; no `Retry-After` |
| 429 | `rate_limit_error` | `notification_limited` | The grant's notification limit is reached; with `Retry-After` |
| 500 | `server_error` | `internal_error` | Gateway error |
| 503 | `server_error` | `instance_unavailable` | The crew is offline |

Both enums are open: a client treats an unknown `code` by its HTTP status.

#### 4.2 Request id

Every request has an id, supplied by the caller or generated by the gateway. It is returned in
`X-Request-Id`, written to the gateway's audit log, and placed in the error body as `request_id`.

#### 4.3 Quota headers

Every response to a delegated-credential request, success or failure, carries:

| Header | Meaning |
|---|---|
| `X-Crew-Quota-Limit` | The grant's daily credit ceiling |
| `X-Crew-Quota-Remaining` | What is left today, after reservations |
| `X-Crew-Quota-Reset` | When it resets, in Unix seconds |
| `Retry-After` | On a 429 other than `grant_paused`: seconds to wait |

The embed-only approval endpoint carries none of these. A turn that stops on a per-turn limit is not
an error: it ends `incomplete` with reason `turn_token_limit`, `tool_call_limit` or
`turn_time_limit`. `GET /v1/crew/capabilities` reports the grant's quota and usage.

#### 4.4 Idempotency-Key

Every POST that spends credits accepts an `Idempotency-Key` header: `/v1/responses` (including
background runs), `/v1/crew/workflows/runs` and `/v1/crew/schedules/{id}/run`, and any such POST
added later. Within one grant, a retry with the
same key within 24 hours returns the first result and is not charged again. The same key with a
different body is 422 `idempotency_key_reused`.

### 5. Event stream

`GET /v1/crew/events` serves two kinds of stream with one cursor rule:

| Stream | Request | Contents |
|---|---|---|
| Conversation stream | `?conversation={id}&since={c}` | Every event of one conversation |
| Instance stream | `?since={c}` | Events that belong to no conversation (a new conversation, a revoked grant, a notice), plus a hint per conversation that it has new events |

```json
{"seq": 43, "event_id": "evt_…", "type": "approval.requested",
 "conversation": "conv_…", "response": "resp_…", "created_at": 1761822000,
 "data": {"approval_id": "apr_…", "tool": "docs.publish", "reason": "Publish the document"}}
```

Event types: `conversation.created`, `conversation.updated`, `turn.started`, `message.created`,
`tool.called`, `approval.requested`, `approval.decided`, `turn.completed`, `grant.revoked`, `notice`.
Events carry ids, not message bodies; bodies are read from `/v1/conversations/{id}/items` under the
same permission check.

| Rule | Meaning |
|---|---|
| `seq` | Strictly increasing within one stream. Gaps are normal: a filtered event still takes a number |
| `event_id` | Globally unique. Deduplicate across reconnects and replays by it |
| `since=c` | Every visible event with `seq > c`, in order |
| No `since` | Only events from now on |
| `c` older than retained, inside a truncated range, or beyond the newest `seq` | 410 `resync_required` with `oldest_seq` and `latest_seq` |
| Delivery | An event is stored before it is sent, so a client may see a duplicate, never a gap |
| Slow client | Disconnected rather than dropping events; it reconnects with its cursor |
| Filtering | On the server only. A delegated credential sees only its grant's conversations |

**SSE.** With `Accept: text/event-stream`, each message's `id:` is the `seq` and `event:` is the
type, so the browser's `Last-Event-ID` on reconnect is the cursor. A comment line every 15 seconds
keeps an idle connection alive. Without SSE, the endpoint returns paged JSON
(`{"events": [...], "latest_seq": N, "has_more": bool}`, `limit` up to 1000, `limit=0` returns only
`latest_seq`) with optional long polling (`wait`, up to 30 seconds). Each credential may hold at
most 10 stream connections; past that, 429 `concurrency_limit`.

**Resume after a 410**, in this order so nothing is missed: take `S = latest_seq` from the 410 body,
read the current state (conversation items, `GET /v1/crew/approvals?state=pending`), replay from
`since=S`, and drop what was already seen by id.

### 6. Approvals

An approval is the crew pausing before a tool call until a person says yes.

| Rule | Meaning |
|---|---|
| **A delegated credential can never answer an approval** | The app writes the prompt and chooses the tools; if it could approve, it would approve itself. Answering through `mcp_approval_response` or the decision endpoint returns 403 `approval_requires_first_party` |
| Public answer path | `POST /v1/crew/approvals/{id}/decision` with `{"decision": "allow" \| "deny"}`. Accepts only the embed credential, for its one conversation. This is the embed credential's only way to answer |
| Owner answer paths | The Owner approval endpoint with `allow`, `deny` or `allow_always`, and `mcp_approval_response` in `/v1/responses`. The reused route's `reject_once` is written in the contract as `deny` with `scope: once` |
| Visibility | Every caller that can read the conversation sees `mcp_approval_request` and `approval.requested`, so an app can tell the user why a turn is waiting |
| First answer wins | A later answer gets 409 `approval_already_resolved` with the final result |
| Id | `apr_` plus 128 random bits, unique across the instance; the gateway finds the conversation from the id |
| Durable | Stored before it is announced; survives a gateway restart |
| States | `pending`, `allowed`, `denied`, `expired`. When a turn is cancelled, its waiting approval becomes `expired` with reason `turn_cancelled` |
| Timeout | Unanswered after 4 minutes 30 seconds, the gateway answers `deny` and records `expired` with reason `timeout` |

### 7. Webhooks

The callback address comes **only** from the `callbacks.webhook_url` recorded for the grant. The API
only selects event types: `POST /v1/crew/webhooks` takes `{"events": [...]}` and no URL. Callbacks
carry ids, never content; the app reads content through the API. Subscriptions are deleted with
their grant and are not retried after that.

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

The Owner tier starts with **96 existing routes** that the user's own clients need: conversations,
approvals, events, models, mates, memory, schedules, artifacts, workflows, notifications, instances,
sign-in, and `/v1/chat/completions`. They are listed with their source in `x-crew-reused-routes` of
`crew-owner.yaml`, marked experimental, and get `/v1/crew/owner/*` wrappers over time. When a group
is wrapped, the SDK switches to the wrapper and the old routes return to Internal.

Six Owner endpoints are new: `GET /v1/crew/grants`, `PATCH /v1/crew/grants/{id}` (tighten only:
lower the quota, remove mates, lower the notification limit), `DELETE /v1/crew/grants/{id}`,
`POST /v1/crew/grants/{id}/resume`, `GET /v1/crew/connectors` and
`GET /v1/crew/connectors/{client_id}`. Grant states are `active`, `throttled`, `paused` and
`revoked`; a paused grant records `pause.by` as `monitor` (the gateway paused it automatically),
`user` or `reconsent` (it waits for the user to agree to a changed grant). A grant also carries
`mates`, `notify_per_hour` (default 10) and `memory_global` (default `false`), which
`memory:read` needs before `/v1/crew/memory/recall` answers.

### 9. Versioning and compatibility

| Level | Where | Changes when |
|---|---|---|
| Major | The path, `/v1/` | Only for a breaking change; `/v2/` would run beside `/v1/` for at least one minor version |
| Minor | The `Crew-API-Version` request header, echoed by the server | Each addition |

Public rules (N-1):

1. A server at minor version N passes the full contract tests of a client that declares N-1.
2. Within a major version the server only adds: endpoints, optional fields, event types, enum
   values, error codes.
3. It never removes or renames a field, changes a type or meaning, makes an optional field required,
   changes a default, or moves an error code to a different HTTP status.
4. Clients ignore unknown fields, skip unknown event types, treat unknown enum values as "other",
   and treat unknown error codes by HTTP status.
5. A client that sends no version header is treated as the server's lowest supported minor version.
   One below that gets 400 `unsupported_api_version` with the supported range.
6. Anything deprecated carries a `Deprecation` header for at least one minor version first.

`GET /v1/crew/capabilities` reports the version range, features, per-agent abilities and limits. A
feature reported as `false` answers 422 `capability_unavailable` instead of degrading silently.

The **Owner** tier is versioned the same way but marked experimental: it may change faster
([Q5](#open-questions)).

**`/v1/chat/completions` is frozen** as an Owner transition route for the user's own clients and
local apps. It gets no new features. A delegated credential calling it gets 403
`use_responses_api`. An unanswered approval there ends the turn after the same 4 minutes 30 seconds,
with `finish_reason: "stop"` and a note that the tool did not run.

## Security considerations

- **Fail closed on identity.** An unknown credential kind, a non-Public endpoint, a resource from
  another grant or a missing scope is refused before any handler runs.
- **No self-approval.** A delegated credential cannot answer approvals by construction.
- **No existence leaks.** Resources outside the grant answer 404, not 403.
- **Content stays in the crew.** Events and webhooks carry ids, never message bodies.
- **Residual risk.** What an app lets the agent read (memory, knowledge, tool output) can flow to
  that app's own tools. `memory:read` and `knowledge:read` therefore never reach a crewmate's private
  memory, and no scope can write memory.

## Alternatives considered

| Question | Chosen | Not chosen | Why |
|---|---|---|---|
| Compatible surface | OpenAI Responses subset | Chat Completions; a new API only | Responses has conversations, tool calls and approval items; Chat Completions has none of them |
| Unsupported fields | Refuse with 400, except sampling | Ignore all; refuse all | Ignoring hides that a field had no effect; refusing sampling breaks common clients |
| Existing `/api/*` routes | Internal by default, promoted with a reason | Freeze them as the contract | The dashboard depends on them in many places; freezing would stop it changing |
| SDK packaging | One package, three entry points | Two packages; one entry point | One version number; the boundary is on the server anyway |
| Listing conversations | `GET /v1/conversations` | `/v1/crew/conversations` | No clash with OpenAI's paths; one fewer path to learn |
| Cursor | One increasing integer per stream | An opaque string; a timestamp | Readable, comparable, and maps onto SSE `Last-Event-ID` |
| Stale cursor | 410 `resync_required` | Silently replay from the oldest | A silent replay lets the client think nothing was missed |
| Slow client | Disconnect it | Drop events | The contract promises no gaps |
| Minor version | A request header | The path; a query parameter | URLs stay stable across minor versions |
| Webhook address | Only from the grant | Set through the API | An API-set address would let an app redirect callbacks the user never agreed to |
| Approval answers in Public | Embed credential only | Delegated credentials too | An app could otherwise approve its own tool calls |

## Open questions

| # | Question | Leaning |
|---|---|---|
| Q1 | How long events, turn streams and resolved approvals are retained | At least 7 days or the last 10,000 events per open conversation; resolved approvals 30 days. The contract only promises that `oldest_seq` says how far back a client can go |
| Q2 | Whether to accept `input_image` and `input_file` | Report per agent in capabilities; text only until settled |
| Q3 | Whether to support `text.format: json_schema` | Report per agent |
| Q4 | Whether to expose reasoning summaries | Not at first |
| Q5 | The Owner tier's compatibility promise | One minor version of deprecation notice; experimental in the SDK |
| Q6 | Minor version format | Integer, `1.N` |

## Appendix: the OpenAPI drafts

| File | Contents |
|---|---|
| [`crew-public.yaml`](assets/crew-api/crew-public.yaml) | The 42 Public operations: request and response schemas, examples, security and scope, error responses, SSE event lists, and the `webhooks` callback |
| [`crew-owner.yaml`](assets/crew-api/crew-owner.yaml) | The 6 new Owner operations in full, and the 96 reused routes as a list |
| [`components.yaml`](assets/crew-api/components.yaml) | The error body, pagination, the Response object, stream and instance events, quota headers, `Idempotency-Key`, request id, webhook signature headers |
| [`redocly.yaml`](assets/crew-api/redocly.yaml) | Lint configuration; run `npx @redocly/cli lint` in that directory |

Each operation carries `x-crew-tier`, `x-crew-scope`, `x-crew-reuses` (existing code it can build
on), `x-crew-coverage` and, where it waits on an open question above, `x-crew-depends-on`.

# Shared Crew

Owner: `config.sections_shared_crew` (the `dashboard.shared_crew` section). The
sections below are filled in as each part of Shared Crew lands.

## 1. Purpose and boundaries

Shared Crew lets several people who can already reach one Crew work in it together.
It attributes what each person does, charges execution to the person who asked for
it, keeps one person from exhausting the host, and shows collaborators to each other.

It is an attribution and fairness layer, not an access layer. The network
(Tailscale) and the existing dashboard auth in `dashboard.token_auth` decide who may
connect; Shared Crew never admits a request auth refused, and never refuses one auth
admitted because an identity lookup failed. Every authorized collaborator has the
same application rights. There are no roles and no per-session ACLs.

A Crew is a machine identity, not a group of people. Its synthetic principal is the
Crew acting as itself; the people who may work in it are individual grants, and
several Crews may have overlapping member lists.

Status: proof of concept. In this build only the configuration section exists, and
nothing reads it yet. With every switch at its default the gateway behaves as a
single-operator Crew.

## 2. Identity

### Principal

`contracts.Principal` is the identity work and records are attributed to. Its `id`
is `principal_id_for(provider, issuer, subject)`: a deterministic, opaque digest, so
one person maps to one id on every device and tab without a registry, and the id does
not reveal the login. Email and display name are never inputs; two providers asserting
the same email are two principals. `DisplaySnapshot` is a label captured when the
principal was seen. A rename changes it and leaves `id` alone. It carries no avatar
URL, because a remote URL in a shared record is an outbound fetch someone else chose.

`PrincipalKind` separates a person (`human`), a device with no personal identity
(`machine`, such as an ACL-tagged tailnet node, keyed by node because every tagged
node reports the same login), Crew automation (`system`, one bounded principal per
named component from `Principal.system`), and `unknown`.

### Derivation

`identity.publish_actor` derives the principal once per request, after
`token_auth_middleware` admitted it and before the handler, and publishes an
`ActorContext` under `request[ACTOR_REQUEST_KEY]`. Handlers read it with
`identity.actor_of` (`viewer_principal` for the principal); a request with no
published actor reads as Unknown, never as the operator. The WebSocket upgrade
passes through the same middleware, so a socket's actor is the upgrade request's.
The browser never names itself: a principal is never read from a body, query,
header or display name, and `from_dict` exists only to read records this server
wrote.

Evidence, strongest first (`identity.derive_principal`):

| Request | Principal | Provenance |
|---|---|---|
| An app identity (an app token, or an internal caller the middleware derived an app for) | `system`, component `app:<name>` | `internal` |
| The internal loopback secret (kiro-cli, MCP, doctor) | `system`, component `internal` | `internal` |
| Loopback peer forwarded by local `tailscale serve`, resolved and allowlisted by `tailnet.resolve_forwarded_peer` | `human` keyed by tailnet login; `machine` keyed by node for a tagged node | `tailscale_serve` |
| Tailnet peer reaching a widened bind on its tailnet address, no forwarding header, resolved by daemon `whois` of the socket address (`tailnet.resolve_direct_peer`) | `human` keyed by tailnet login; `machine` keyed by node | `tailscale_direct` |
| Loopback socket with no forwarding header at all | `human`, the local operator, keyed by the token subject | `local_operator` |
| Anything else auth admitted | `Principal.unknown()` | `none` |

A tailnet principal's subject is the lowercased daemon login under provider
`tailscale` and issuer `tailnet`; a tagged node's is `node:<name>`, because every
tagged node reports the login `tagged-devices`. The display snapshot is the login
(or node name) as the daemon reported it, so a rename never changes the id.

A person is keyed by their tailnet login on every transport, never by a display
name, so every node of one user and a re-created node map to one principal. The
display name is a label only. The device a person was seen from is the node key.

The serve and widened-bind rows require the existing identity-trust opt-in
(`dashboard.tailscale.trust_identity` with a non-empty `allowed_logins`, i.e.
`TailnetTrust.enforces_identity`). Without it no daemon lookup runs on those paths
and tailnet callers are Unknown. The serve row reuses the middleware's existing
resolution, allowlist deny and pin unchanged. The widened-bind row is attribution
only: its lookup runs after the auth decision, shares the whois cache and executor,
and is read by neither the pin (a direct peer stays `ip:`-pinned) nor the
allowlist, so it cannot admit or refuse a request.

Loopback alone is not proof of a trusted proxy: a loopback request carrying any
forwarding header (`origin.is_proxied_request`) is never the local operator. It is
a person only when the serve row resolved it, else Unknown.

Failure never changes access. Derivation runs only on an admitted request, and
an exception anywhere in it publishes Unknown. A request auth refused publishes
nothing.

`ActorContext.auth` is a copy of what the middleware decided, kept for audit. It is
not a second decision. `correlation_id` is the client's `X-Correlation-Id` when it
is a short plain token, else the server's `request_id`.

### Observed collaborators

`people` keeps a bounded in-memory record (`MAX_OBSERVED` principals, LRU) of the
human and machine principals seen on authorized requests, with first and last
sighting and a device count (a digest per node, at most `MAX_DEVICES`). Unknown
and system principals are not recorded. `people.people_view` builds the People
body: it merges that record with the creators recorded on persisted sessions. No
route serves it yet. Each row names its `sources`
(`observed`, `session_creator`), so an observation is never presented as
membership; `directory.state` is `unavailable` because this build has no
authoritative assigned-user directory. Per-person `usage` is `null`, and
`usage_available` false, until the admission layer registers a reader
(`people.register_usage_source`).

### People and setup modes

`crew_mode` puts every Crew in exactly one mode, served by `GET
/api/shared-crew/mode` (dashboard user only, 403 otherwise). The People surface,
setup guidance and session list follow it, and the modes never mix:

| Mode | When | People shows | Session list |
|---|---|---|---|
| `personal` | no setup path is available or degraded, and no principal but the local operator viewing has been seen (the operator's bootstrap subjects `local-app`, `local-startup` and the configured owner are the same person; session creators are matched by id, never by a stored provenance) | setup guidance with each adapter's honest status, personal Remote Control | no creator bylines, no All/Mine control |
| `network` | a Tailscale or Systems Manager path is on, or a network person or another operator has been seen | who has been seen (labelled as seen), each adapter read-only, personal Remote Control | bylines, filter |
| `invalid` | the tailnet login allowlist could not be read | one error state with the reason code | bylines, filter |

A team configuration is refused (`crew_mode.team_config_problem`) with reason
`network_identity_config_unreadable` while the tailnet login allowlist could not be
read (`config.resolution.tailnet_identity_unknown`, recorded by `access.note_tailnet`).
The gateway logs the refusal at start (`crew_mode.validate_at_startup`, from
`wiring.install`) and `lifecycle.team_config_middleware` refuses every tailnet
person with 403 `{error, code: team_config_invalid, reason}` while it holds. The
local operator keeps working and sees the error in People. An invalid team setup
never falls back to personal mode, and the browser treats an unreadable mode route
as an error, not as personal.

The mode body: `mode`, `crew_id` (the gateway id; it keys the browser's remembered
list filter), `viewer`, `team_config` (`state` `not_configured` | `invalid`, with
`reason`, `capability`, `cause`), `console` (always `not_offered`), `adapters` (the
setup paths), `systems_manager_attribution` (always `unavailable`) and
`workload_identity_ref` (always `unavailable`). The Tailscale and Session Manager
links are fixed public documentation pages.

AWS Systems Manager governs its path through IAM and Session Manager. A port-forwarded
Session Manager connection arrives on loopback with no forwarding header, so
`derive_principal` cannot tell it from the operator's own browser: with a dashboard
token it is the local operator, and its work is attributed to whoever runs the Crew.
No verified connection-to-principal binding exists for Session Manager in this build,
so Session Manager access to the host is the operator's own identity; People says so.
Identity is never inferred from the OS user or a typed label, and loopback alone
yields the local operator only together with a dashboard token.

People has no role control in any mode. Its one invite and revoke control is the
Tailscale invite card below, shown in `personal` and `network` mode to the owner only.
Inviting adds a person to the tailnet, which is the sharing step. The access card
(waiting list, Approve, Remove) is shown only with `local_authorization` on.

### Access set

### Tailscale invites

## 3. Session attribution and records

`SessionAttribution` is stored under the metadata-line key
`SESSION_ATTRIBUTION_META_KEY` (`attribution`). It is not `created_by`, which already
names the creating slot key. It is write-once: `keep_creator` returns the stored value
whenever one exists, so a later message by someone else never changes the creator.

The slot holds it on `_attribution` (`attribution.slot_attribution`).
`attribution.stamp_creator` records the creating actor where a session is
created; `stamp_creator_if_fresh` records the sender of the first accepted user
message of a slot no creation path stamped, and only when the slot holds no user
turn yet. The metadata codec's `attribution` row writes it on both save forms and
reads it back on every hydration purpose. No writer lists the key as owned, so a
save that does not write it carries the stored value through
`history.carry_unowned_metadata`, and the whole line is replaced atomically by the
existing full save.

Missing human attribution is displayed as Unattributed on every surface (session
list, filters, bylines, People, decisions and presence). Storage keeps the
`unknown` principal kind and the `p_unknown` id; nothing is rewritten.

A session with no `attribution` key reads as `legacy_missing` with an Unknown creator.
It stays legacy: the codec never writes a legacy value back, no stamp replaces it,
and nothing back-fills it, least of all from the current viewer. Attribution was on
but no principal could be established reads as `unattributed`. A damaged attribution
object, or a torn metadata line, reads as legacy, because authorization never reads
it and a damaged record may only lose its label. Keys inside the attribution object
this build does not know are kept (`extra`) and written back.

The metadata line is a file an agent's own tools can write, like `created_by`.
The creator is therefore a label for discovery, never an input to an access
decision.

Browser-bound copies use `attribution.public_attribution`: availability, creation
time and `Principal.to_public_dict` (id, kind, display; no issuer or subject).
The slot payload (`slot_projection`) and every session catalog row
(`history_search.SessionCatalogProjection.list_sessions`) carry it as
`attribution`.

A message's author rides in the message `meta` under `MESSAGE_AUTHOR_META_KEY`
(`author`), in the public principal shape, stamped by the server when it accepts
the message (`attribution.stamp_message_author`). A client-supplied `author` key is
discarded at ingress and overwritten at the stamp. Every other live user row an
authorized request's handler appends (regenerate, rewind, task-runner hand-offs, the
OpenAI shim) names that request's actor (`attribution.with_request_author`, bound by
`identity.publish_actor` to the request's own task, so a turn the handler starts never
inherits it). Channel rows name the bounded Unknown principal and automation rows the
automation principal (`stamp_principal_author`). A message written before authors
reads as Unknown (`attribution.message_author`).

Sender in the prompt. The agent is told who sent a message with one line before
its text, `[from: <display name> (<login>)]` (`attribution.sender_line`, added in
`prompt_assembly._turn_build_prompt`). It is added when the turn's user row is by a
person other than this machine's owner (the local operator's subjects and the
owner's tailnet login), or when more than one person has written in the session,
in which case the owner's rows read `[from: owner (<owner login>)]`. It reads the
server-stamped author already on the row; a merged queue row whose entries name
different people names no one and gets no line. Brackets, parentheses and control
characters are stripped from the label. Only the prompt carries it: the stored row,
the transcript the UI shows, the channel mirror and the system prompt are unchanged.

Trigger attribution. A session a schedule, a webhook or the Crew started names
that trigger instead of a person (`trigger.trigger_of`): `created_by` of
`cron:<job id>` (stamped only for a transport-attested cron caller), a key in the
`cron` or `hook` namespace in either separator spelling, or slot origin `system`.
The public attribution then carries `trigger` `{kind: scheduled | webhook |
system, ref, name}`, and never for a human creator. The UI reads
`Scheduled: <job name>`, `Webhook: <name>` or `Crew automation`. The job name
comes from the cron service's in-memory snapshot (`wiring.cron_job_namer`), so a
label costs no I/O; without it the label omits the name. A trigger is a label derived from editable records and grants
nothing.

External attribution output is suspended pending product reconciliation: no
tool call, outbound post or other downstream system receives "Requested by
<person>" text. The human stays in Crew records; a downstream system sees only
the Crew. `test_no_external_requested_by_attribution_is_emitted` pins it.

### Session list query

### Audit

### Memory changes

## 4. Admission and accepted work

### Operation state machine

### Capacity accounting

### Requests and idempotency

### Responses

### Fairness

### Accepted work, revalidation and recovery

### Containment

### Defaults and API

`dashboard.shared_crew` (`config.sections_shared_crew`), bounded at parse
(`shared_crew_config_from`) and resolved by `effective_limits`:

| key | default | meaning |
|---|---|---|
| `admission_enabled` | false | fair admission; off is the single-operator default |
| `principal_concurrency` | 4 | root executions one person runs at once (1 to 256) |
| `crew_concurrency` | 0 | root executions Crew-wide; 0 is the session start gate's automatic width (0 to 1024) |
| `containment_enabled` | true | per-person slices, applied only while admission is on |
| `hard_limits` | false | refuse work when the slices cannot be enforced |
| `tailnet_direct` | false | also listen on this machine's Tailscale addresses; only a literal `true` turns it on |
| `local_authorization` | false | Crew, not Tailscale, decides who gets in; a missing key or a literal `false` is off; any other non-boolean reads as on |
| `typing_idle_seconds` | 10 | idle seconds before another person's composer unlocks (3 to 60) |

An out-of-range integer is clamped and a wrong-typed value reads as its default, so an
edit can narrow or widen a limit but never remove one. The other limits are module
constants in `config.sections_shared_crew`: 8 subagents per person (twice the Crew
width Crew-wide), queue depth 16 per person and 64 Crew-wide, queue expiry 300 s,
deadlines 1800 s (root) and 600 s (child), 4 MiB per request, 30 session creates per
minute, 2048 MB accounted storage per person, and slice limits of 800% CPU, 24576 MB
memory and 8192 tasks per person and 2400% CPU Crew-wide. These are proof-of-concept
test targets measured on one host, not product commitments.

## 5. Collaboration

### Typing lease

### First submit wins

## 6. Wire shapes

### HTTP

### Session list filter

### WebSocket

## 7. UI states

## 8. Capabilities

### Aggregation

### Not in this build

## 9. Records and migration

Every record carries `schema_version` (`codec.SCHEMA_VERSION`). It is the Shared Crew
record version, unrelated to the platform `CONTRACT_VERSION`. Rules:

- An added optional field keeps the version. A changed meaning bumps it, and the
  reader handles every older version.
- A reader preserves keys it does not own (`codec.split_extra` into `extra`, written
  back by `merge_extra`), so an older gateway's rewrite never drops a newer field.
  Session metadata also passes through `history.carry_unowned_metadata`.
- An enum value a newer writer added reads as the field's explicit fallback
  (`enum_or`), not as a refusal of the whole record.
- No migration rewrites history to add attribution. Legacy stays legacy.

The contracts are pinned by `test/test_shared_crew_contracts.py`. Changing a field
or an enum value changes that test in the same change.

### Operations

# Shared Crew

Owner: `config.sections_shared_crew` (the `dashboard.shared_crew` section). The
sections below are filled in as each part of Shared Crew lands.

## 1. Purpose and boundaries

Shared Crew lets several people who can already reach one Crew work in it together.
It attributes what each person does, charges execution to the person who asked for
it, keeps one person from exhausting the host, and shows collaborators to each other.

It is an attribution and fairness layer, not an access layer. The network (Tailscale)
and the existing dashboard auth in `dashboard.token_auth`, including the direct
tailnet listener's `whois` and the derived access set (section 2, Access set), decide who may connect;
Shared Crew never admits a request auth refused, and never refuses one auth admitted
because an identity lookup failed. Every authorized collaborator has the same
application rights. There are no roles and no per-session ACLs. The in-Crew
membership actions are the owner's Tailscale invite (§2 Tailscale invites), which
shares this machine through Tailscale and admits the login that accepted, and the
owner's Approve on a same-tailnet person who is waiting (§2 Access set), both in
Tailscale mode only. Tailscale is the only access authority: nobody types the set of people
who get in.

No permission scoping in this release: every collaborator a Crew admits has the same
rights, and no code path reads a role or an ACL.

A Crew is a machine identity, not a group of people. Its synthetic principal is the
Crew acting as itself; the people who may work in it are individual grants, and
several Crews may have overlapping member lists.

Status: proof of concept. `shared_crew.wiring` and `shared_crew.lifecycle` connect
the sections below at gateway start (`dashboard.server`). With every switch at its default the gateway
behaves as a single-operator Crew, and the direct tailnet listener binds nothing.

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
| Tailnet peer on the direct tailnet listener, admitted by daemon `whois` of its own TCP `ip:port` and the derived access set (`tailnet_direct.DirectGate`) | `human` keyed by tailnet login (the stable user id is an alias); `machine` keyed by node for a tagged node | `tailscale_listener` |
| Tailnet peer reaching a widened bind on its tailnet address, no forwarding header, resolved by daemon `whois` of the socket address (`tailnet.resolve_direct_peer`) | `human` keyed by tailnet login; `machine` keyed by node | `tailscale_direct` |
| Loopback socket with no forwarding header at all | `human`, the local operator, keyed by the token subject | `local_operator` |
| Anything else auth admitted | `Principal.unknown()` | `none` |

A tailnet principal's subject is the lowercased daemon login under provider
`tailscale` and issuer `tailnet`; a tagged node's is `node:<name>`, because every
tagged node reports the login `tagged-devices`. The display snapshot is the login
(or node name) as the daemon reported it, so a rename never changes the id.

A person is keyed by their tailnet login on every transport, never by a display
name. `tailscale serve` names only the login, while the direct listener's `whois`
also names the stable user id (`UserProfile.ID`); the user id is an alias of the
login, never the key, so one person reaching the Crew through Serve and through the
listener is one principal with one admission budget. Every node of one user, a
re-created node and a node shared in from another tailnet (`Node.Sharer` set; the
principal is the node's owner, never the sharer) map to that principal. Access
approvals still match the stable user id. The display name is a label only. The
device a person was seen from is the node key.

#### Direct tailnet listener

With `dashboard.shared_crew.tailnet_direct` on (it is off by default; a value that
is not a boolean reads as off) and Tailscale running, the gateway also listens on this
machine's own tailnet addresses (`100.64.0.0/10` and `fd7a:115c:a1e0::/48`, from
the daemon's `status`) on the dashboard port (`dashboard/tailnet_direct_listener.py`).
It never binds a wildcard, a LAN address or any non-tailnet interface, and it does
not start beside a widened bind (`KIROCREW_BIND`), under a governance ceiling that
pins `capabilities.tailnet_origin` off, or when the daemon names no address or no
owner login. Loopback is unchanged. Off keeps the loopback-only listener; a change
applies at the next gateway start. The bind is fixed at start; a Tailscale that starts later, or
an address change, needs a gateway restart.

A request is on the direct listener when the local end of its TCP connection is one
of those addresses (`DirectGate.owns`). `token_auth_middleware` decides it before
any token is read:

| Check | Refusal (403, `code: tailnet_direct_refused`) |
|---|---|
| Socket peer is a tailnet address | `not_tailnet_peer` |
| Socket peer is not one of this machine's own tailnet addresses (a local process uses loopback) | `self_connection` |
| No `Forwarded`, `X-Forwarded-*` or `X-Real-IP` header | `forwarded_header` |
| Daemon `whois` of the peer `ip:port` names a node key and a user with a stable id | `whois_failed` |
| Same node key as the connection's earlier answer | `node_changed` |
| Tagged node: one of its tags is a `tag:<name>` entry the access set carried over (both modes) | `tagged_node_not_allowed` |
| Person, `local_authorization` off (the default): any person the `whois` names, untagged with a stable user id | none; Tailscale already decided |
| Person, `local_authorization` on: access set readable (the owner is still admitted) | `allowlist_unreadable` |
| Person, `local_authorization` on: the Crew owner (the login and user id of this machine's own node), a login of an accepted Crew invite or of the migration seed, or a stable user id the owner approved (`access_set`, published through `tailnet_live_allowlist`) | `login_not_allowed`, or `awaiting_approval` once the person is listed as waiting |

Only the `whois` answer is cached; the admission rule runs on every request, so an
accepted invite or an approval admits on the next request and a removal is refused
on the next request, with no restart. An admitted request needs no dashboard token.
The owner (stable user id and login both match this machine's node,
`tailnet_direct.is_owner`) carries the owner's dashboard subject (`owner_id`, else
the bootstrap `local-app`) and `auth_owner_whois`, so every owner-gated route
answers it (`dashboard.tailnet_owner`). Two irreversible actions, `DELETE
/api/shared-crew/tailscale/credential` and `DELETE /api/sessions`, also need
`X-Kirocrew-Confirm: irreversible`, which the dashboard sends only after its own
confirm dialog; without it the answer is 428 `confirm_required`. Anyone else is the
dashboard user `tailnet:uid:<id>` (a tagged node `tailnet:node:<name>`), which never
matches the owner subject, so owner-gated routes refuse it. A token or cookie it carries is ignored, except an app token,
which runs the ordinary token flow so the app keeps its scope. Strict internal
paths and the internal secret are refused on this listener. It cannot mint a
bearer link (`_caller_bounds` lends no lifetime), and `/api/auth/me` reports
`auth_method: tailnet_direct`. A `Tailscale-User-*` header is never read here.
Browsers get the refusal as a page, everything else as JSON.

`whois` is not run per HTTP request. It runs once per connection and is cached per
`ip:port` for `WHOIS_TTL_SECS` (30 s; a failure for 2 s; at most
`WHOIS_CACHE_MAX_ENTRIES` entries). A WebSocket upgrade always asks again, and an
open WebSocket is re-verified every `WEBSOCKET_REVERIFY_SECS` and closed when its
peer stops verifying. Any `whois` failure rejects the request; there is no
Unattributed fallback on this listener.

The daemon is reached with `tailnet_localapi`, LocalAPI first, then the CLI. Every
location is one the gateway user cannot write: a socket or token file must be
root-owned in a root-owned directory, and a CLI found on `PATH` is used only when
it resolves to a vetted install location (`tailnet._CLI_CANDIDATE_PATHS`). Nothing
is installed. The method used is reported in the capabilities
(`tailnet_direct.method`).

| Platform | Order | Status |
|---|---|---|
| Linux | LocalAPI unix socket (`/var/run/tailscale/tailscaled.sock`, `/run/tailscale/tailscaled.sock`), then the CLI | code path and tests; live check on a host with Tailscale pending |
| macOS | open-source daemon socket (`/var/run/tailscaled.socket`), standalone app LocalAPI (`/Library/Tailscale/ipnport` and `sameuserproof-<port>`), CLI on `PATH`, CLI in `/Applications/Tailscale.app/Contents/MacOS/Tailscale` | code path and tests with fakes; unverified on macOS |
| Windows | CLI in `C:\Program Files\Tailscale` | code path and tests with fakes; unverified on Windows |

`whois` names the user who owns the Tailscale node, not the operating-system user
at that machine. Several people sharing one node are one principal; each of them
needs the token path to be told apart. People says so.

Supported topologies:

| Topology | Names people | Admission |
|---|---|---|
| Direct tailnet listener (off by default), Linux | yes, by stable user id | `whois` plus access set, no token |
| Direct tailnet listener, macOS and Windows | yes (code path, unverified on those systems) | same |
| `tailscale serve` to loopback, Linux with `tailscaled` as root | yes, by login | token plus access set |
| `tailscale serve` to loopback, macOS, Windows, Linux with a non-root `tailscaled` | no, Unattributed | token plus access set |
| Widened bind reached on a tailnet address | yes, by login, attribution only | token |

The serve and widened-bind rows require the existing identity-trust opt-in
(`dashboard.tailscale.trust_identity` with a non-empty `allowed_logins`, i.e.
`TailnetTrust.enforces_identity`). Without it no daemon lookup runs on those paths
and tailnet callers are Unknown. The serve row reuses the middleware's existing
resolution, allowlist deny and pin unchanged. The widened-bind row is attribution
only: its lookup runs after the auth decision, shares the whois cache and executor,
and is read by neither the pin (a direct peer stays `ip:`-pinned) nor the
allowlist, so it cannot admit or refuse a request.

Loopback alone is not proof of a trusted proxy: a loopback request carrying any
forwarding header (`origin.is_proxied_request`) is never the local operator, and
its forwarded address is attributed only through the daemon, and only after the kernel
proves the client end of the loopback socket belongs to `tailscaled`
(`tailnet_proxy_proof.loopback_client_is_daemon`, Linux only; elsewhere the serve row
never resolves and such a caller is Unknown). Only a `tailscaled` running as root is
that proof (any user can name a process `tailscaled`), so a local token holder adding `X-Forwarded-For` is Unknown,
not the person it names. Without the proof the allowlist still decides admission
(`tailnet.forwarded_peer_refusal`): a forwarded tailnet request is admitted, as
Unknown, only when the daemon's `whois` of its single forwarded address names an
allowlisted login, and refused otherwise, so access never widens where names cannot
be proven.

Supported matrix for the serve row: Linux with `tailscaled` running as root names
people; Linux with a non-root `tailscaled`, macOS and
Windows admit allowlisted people as Unknown. macOS gives an unprivileged process no
owner for another user's loopback socket, Windows has no such check in this build,
and `tailscale serve` does not register its loopback connections for a `whois` by
port. The direct tailnet listener names people on every platform. When it is active,
`attribution` is `available` and the capabilities carry `attribution_via:
tailscale_direct` and `tailnet_direct` (`state`, `reason`, `method`, `urls`,
`owner_login`, `addresses`). Otherwise, when identity trust is on and
the gateway listens on loopback only, `host_power.probe_after_start` runs
`tailnet_proxy_proof.proof_support()` in a background task after the listener
accepts requests and records it (`access.note_proof_gap`); `attribution` reports
`degraded` (`not_checked`) until then, and afterwards
`unavailable` (`platform_unprovable`, `daemon_same_user`) or `degraded`
(`daemon_not_found`). A
`Tailscale-User-Login` header that disagrees with the daemon drops the
attribution. A non-loopback peer that sends `X-Forwarded-For` is a proxy on
another node and is Unknown. Funnel, other reverse proxies and unverifiable
forwarding headers resolve to Unknown.

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

**Network trust (default).** `dashboard.shared_crew.local_authorization` (bool,
default `false`; any value but a literal `false` reads as on; read at gateway start)
decides whether Crew adds its own approval on top of Tailscale. Off, Tailscale alone
decides: whoever Tailscale lets reach this machine gets in. `access_set.install`
publishes the set with `network_trust` on, so the direct listener admits every
`whois`-verified untagged person and nobody is listed as waiting. Network trust
widens only the direct listener: `tailnet.login_allowed` (the `tailscale serve`
path and mobile setup) still admits only the owner, accepted invites, approvals
and the seeded `allowed_logins`. Identity still comes from `whois` and is still used for
attribution and owner rights. Tagged nodes keep the carried-over `tag:` rule. The
`not_tailnet_peer`, `forwarded_header`, `whois_failed` and `node_changed` refusals
are unchanged. Approve, Dismiss and Remove answer 409 `local_authorization_off`,
`GET /api/shared-crew/access` reports `local_authorization: false` and People hides
the access card. Revoking an invite still deletes it at Tailscale, which ends the
share; Crew closes no socket and stops no work itself. Removing a person is done by
removing them from the tailnet. Everything below in this section, and "Removal
stops work now", applies only with `local_authorization` on.

With `local_authorization` on, who gets in is computed
(`shared_crew.access_set`), never typed:

| Member | Matched by | Source |
|---|---|---|
| the owner | stable user id and login of this machine's node | daemon `status` at start |
| a person who accepted a Crew invite | the accepting login (`acceptedBy.loginName`) | `tailscale_invites` members |
| a person the owner approved | stable Tailscale user id (`whois` `UserProfile.ID`); another account with the same login is not admitted | approvals |
| the migration seed | login, or `tag:<name>` for an ACL-tagged node | `dashboard.tailscale.allowed_logins`, read at the first start, only shrinks |

Before the dashboard binds, the gateway publishes an owner-only placeholder
(`access_set.install_placeholder`, no I/O); `tailnet_direct_listener.start` reads
the set after the bind. At the first gateway start with a readable tailnet section, `access_set.install`
copies `allowed_logins` (logins and `tag:` entries) into the set as its seed and
marks it seeded. After that the seed only shrinks: at each start an entry no
longer in `allowed_logins` leaves the seed, and an entry added later admits
nobody. People lists the carried-over entries. With the set installed,
`tailnet.login_allowed` reads the set and ignores the list it is handed, so the
`tailscale serve` path and mobile setup follow the same set. The invite flow still
mirrors an accepted login into `allowed_logins`, because `trust_identity` needs a
non-empty list for the serve path to resolve identities at all. The dashboard has
no control that edits the list.

A same-tailnet person the Tailscale ACL lets reach this machine is not admitted by
that alone. The direct listener refuses them (`awaiting_approval`, a "waiting for
the owner to approve you" page for a browser) and `access_set.note_waiting` lists
them: stable user id, login, display name, the `whois` `ProfilePicURL` when it is a
plain `https` URL (display only, loaded with no referrer), node, first and last
sighting. The list holds `MAX_WAITING` (50) people, oldest dropped; a person's
sighting is refreshed at most once a minute; at most `NEW_WAITING_PER_MINUTE` (10)
new people join per minute and the rest are refused unlisted. A dismissed person is
not listed again for a day. Each new listing, approve, dismiss and removal writes
one SEL `shared_crew.access.<op>` record with the actor and a login digest.

Owner routes (`shared_crew.routes_access`, the invite routes' owner gate):

| Route | Does |
|---|---|
| `GET /api/shared-crew/access` | waiting, approved, carried-over entries, `first_share` |
| `POST /api/shared-crew/access/waiting/{user_id}/approve` | admit by stable user id |
| `POST /api/shared-crew/access/waiting/{user_id}/dismiss` | drop from the list |
| `DELETE /api/shared-crew/access/approved/{user_id}` | remove an approval and stop the person's work |
| `POST /api/shared-crew/access/first-share/ack` | record the first-share acknowledgment |

**Removal stops work now.** Removing an approval or revoking an accepted invite
takes the person out of the set (a live revoke by login and by user id, so the
next request is refused), closes every dashboard socket whose verified actor is
that person (`routes_access.close_sockets`, by principal id or verified login), and
calls `admission_revoke.stop_principals` with the person's principal id (keyed by
login on every transport, `access_set.principal_ids_for`). Each non-terminal root work item charged to them is cancelled through
`AdmissionService.cancel` with reason `access_removed`: queued and admitted work
ends `cancelled` at once; running work moves to `cancelling`, its turn is
cancelled, and its slot is released only when the runtime has stopped and the
work settles. Finished work is kept. A parked message of theirs is dropped from
its session's queue before any merge: the drain's per-entry check
(`AdmissionService._cancelled_parked`) refuses work ended by `cancelled_by_user`
or `access_removed`, with that reason, so their text never runs merged into
someone else's turn. Work of theirs that reaches the front of a
queue later is refused at revalidation (`AdmissionService._revalidate` consults
`access_set.principal_removed`). With fair admission off (the default)
no work record exists, so `access_set.slot_work_stopper`
(`routes_access.stop_slot_work`) drops the queued messages the person sent first
and then stops each turn they started through `stop_slot_turn`: a turn running
outside admitted work records its initiator per slot
(`turn_gate.unadmitted_slots`; a chat send passes its sender with
`initiated_by`), and `turn_gate.gate_turn` refuses a removed person's drained
turn with an `access_removed` note. A thread or side reply records its
initiator under its session key the same way and is cancelled
(`turn_gate.cancel_unadmitted`). The audit detail reads
`stopped: access removed`. Removing an approval also marks that person's
accepted-invite member record `removed`, so the set rebuilt after a restart
leaves them out and `tailscale_invites.sync` re-admits them only through a new
invite.

Revoking an invite and `tailscale_invites.sync` commit one at a time
(`_commit_lock`): `sync` reads the invite state again under the lock, and an
invite Crew made that was revoked after the list was read admits nobody.

**First share.** While nobody but the owner is in the set and the owner has not
acknowledged it, the first invite (with a key connected) and the first approve
answer 409 `first_share_ack_required`. People shows a confirm first: they will see
all N sessions and Crew memory (M items), counted from the session catalog and the
global memory store; on a laptop host it adds that teammates lose access while the
host sleeps. The acknowledgment is stored once per Crew.

**Keep awake.** `host_power.detect_laptop` reports a battery from
`/sys/class/power_supply` (a `Battery` that is not a peripheral's `Device` scope),
`/usr/bin/pmset -g batt` on macOS, or `GetSystemPowerStatus` on Windows; anything
else is unknown, never a laptop. The gateway's prevent-sleep poll has a third
reason, `host_power.shared_use_wants_awake`: a collaborator (a Tailscale person who
is not the owner) has a dashboard socket open, or work charged to a member of the
set is queued or running. The existing `power.SleepInhibitor` holds the host awake
(`caffeinate -i -w <gateway pid>`, `systemd-inhibit --what=idle:sleep`,
`SetThreadExecutionState`), each helper resolved from a fixed system path, and
releases it when that stops. Nothing is installed.

**Storage.** `<data_home>/.vault/shared-crew-tailscale/access.json` (0600), beside
the invite records, masked from every sandbox mode. A file that cannot be parsed
leaves the set unreadable: only the owner is admitted, and the access card says so. A
failed write drops the in-memory copy, so the next read is the file again.

### Tailscale invites

The owner invites a teammate in one step: emails, or a link, with an optional alias
and avatar. Crew calls Tailscale's device-invite API, and when the teammate accepts it
learns their real Tailscale login and admits it. This replaces a separate member list
and a hand-edited trust list (`shared_crew.tailscale_invites`,
`shared_crew.routes_invites`).

**Credential.** A user-owned Tailscale API access key (`tskey-api-...`), made by an
Owner, Admin or IT admin. An OAuth-client token cannot create or resend device
invites (the share belongs to a user), so no OAuth path exists. The key is entered
once through `POST /api/shared-crew/tailscale/credential` and stored in a dedicated
`SecretVault` at `<data_home>/.vault/shared-crew-tailscale/`
(`shared_crew.tailscale_invite_store`); never in `config.json`, never logged, never in
a response. The status read reports `connected`, the tailnet name, the key's
`expires` (from `GET /tailnet/-/keys/{keyId}`) and `key_expires_soon` (within 7
days) or `key_expired`. `DELETE .../credential` removes it.

**This machine's device.** `connect` matches the local daemon's `Self.PublicKey`
against `devices[].nodeKey`, then `Self.TailscaleIPs` against `devices[].addresses`,
and keeps the `nodeId`. Never by name; no match refuses `device_not_found`.

**API calls.** Only to `https://api.tailscale.com/api/v2`, basic auth with the key,
redirects refused (`shared_crew.tailscale_api`):

| Call | Use |
|---|---|
| `POST /device/{id}/device-invites` | body a JSON array of `{email?, multiUse, allowExitNode}`; `allowExitNode` is always `false`, `multiUse` is `false` unless the owner turns on a reusable link (email invites are always single-use) |
| `GET /device/{id}/device-invites` | read back each invite's `accepted` and `acceptedBy {id, loginName, profilePicUrl}` |
| `POST /device-invites/{id}/resend` | Resend; Tailscale allows one per minute |
| `DELETE /device-invites/{id}` | Revoke; a 404 counts as already gone |

**Accept.** `sync` runs when the owner opens People (at most every 15 s) and from a
gateway poller while any invite is pending (60 s, doubling on failure to 15 min, never
sooner than a 429's `Retry-After`; a backed-off sync makes no call). Each newly
accepted invite binds `acceptedBy.id` (the stable Tailscale user id) to `loginName`
and the invite's alias and avatar, adds the login to
`dashboard.tailscale.allowed_logins` with `enabled` and `trust_identity` on and the
machine owner's own login kept (`shared_crew.tailscale_allowlist`), and grants it
live (`dashboard.tailnet_live_allowlist`, read by `tailnet.login_allowed`), so no
restart is needed once identity trust was already on; the first accept that turns it
on reports `restart_required`. The accepting login is used even when it differs from
the invited email: Tailscale lets an invite be accepted from any account. An invite
made in the admin console that Tailscale reports accepted is admitted the same way. An
unaccepted invite Tailscale no longer lists is dropped. Live access is granted only
after the membership write is durable, so a revoke always finds the person. A reusable link stays due for a
sync after its first accept, so each later person who accepts it is admitted.

**Display only.** Alias (printable, 64 characters) and avatar (PNG, JPEG, GIF or
WebP by magic bytes, at most 256 KiB, named by digest, served same-origin with
`nosniff` and a sandbox CSP) never decide access. People rows whose provenance is a
verified tailnet peer show the alias and avatar bound to that login; a session
creator read back from stored attribution never borrows one.

**Revoke.** Delete the invite at Tailscale; for every person it admitted (each one
who accepted a reusable link), take the login out of the access set and
the config mirror (never the machine owner's own), refuse it live, close that
person's dashboard sockets (`_shared_crew_actor` on each socket) and stop their
queued and running work (Access set, Removal stops work now). Revoking an accepted invite is assumed
to end the share (Tailscale's sharing guide says so; the API schema does not); Crew's
own refusal holds either way.

**Owner gate.** Every route but the avatar read requires a dashboard user, a
non-restricted session, `is_owner_dashboard_request`, and a viewer who is the local
operator, the owner proved by the direct listener's `whois` (`auth_owner_whois`), or
this machine's own tailnet login, so an admitted teammate cannot invite, approve or
revoke. An upstream Tailscale failure answers 502 `tailscale_<code>` (429
`tailscale_rate_limited` with `Retry-After`), never 401.

**Without a key.** The card explains the manual step and links the admin console's
Machines page; nothing else changes.

**Audit.** Every connect, create, resend, accept and revoke writes one SEL
`shared_crew.invite.<op>` record with the actor, `email_sha256:<16 hex>` of the
invited email (never the address) and the outcome.

Share link. While the direct tailnet listener is active, People (`network` mode) and
the phone dialog show its plain URL, `http://<magicdns-name>:<port>/` first and each
tailnet address as a fallback, with a copy button and the note that only people on
the owner and people they invited or approved can open it (`TailnetDirectLink`). The URL carries no token.
The phone dialog keeps its token-carrying QR and sign-in link under an Advanced fold
for devices without Tailscale. People adds the device-owner caveat under the list,
and the macOS empty state (`platform_unprovable`) points at the direct link.

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

`session_query.query_sessions` runs over the whole persisted catalog: machine
transcripts are dropped, then the search (`q`, case-insensitive over title and
key) and the creator filter, then the page is cut. Order is newest `modified`
first with a key digest as the tiebreak. The cursor is a keyset position, so one
walk never returns a row twice; a session modified during a walk moves to the top
and appears on the next walk. `total_matching` counts every session passing the
search and the filter. `hidden_by_filter` counts the sessions passing the search
that the creator filter excluded. The list covers persisted sessions; a slot
that has never been saved is not in it. The route answers only the dashboard
user, and refuses an app token or an internal caller with 403, so it reaches no
session the existing list does not.

The `unattributed` filter (the older `unknown` value is accepted and echoed as
`unattributed`) lists sessions with no human creator and no named trigger. A
triggered session lists under All with its trigger. Filters change visibility
only; every session stays reachable with All.

The browser shows All by default, Mine, each person seen and Unattributed, labels
the verified viewer You, and remembers the choice per Crew in local storage keyed
by the mode route's `crew_id` (`kc.shared-crew.creator-filter.<crew id>`). A
personal Crew, or one that cannot attribute, hides the control and the bylines
(`useSharedCrewListView`, `SharedCrewBylineContext`).

### Audit

`ActionRecord` audits a consequential shared action: session create, message,
approval, question answer, steer, cancel, retry, control actions and memory changes. It names the
initiator, the responder where one exists, the runtime reference, the outcome and a
causal link. It carries ids, never prompt text or credentials.

`audit.record_action` writes it to the existing tamper-evident security event log
(`kiro_crew.sel`) as one `shared_crew_action` event: `caller_identity` is the
initiator's principal id, `operation` is `shared_crew.<action_type>`, `resources`
is the target, `request_id` the correlation id, and `metadata` holds the action
ids and a reduced principal (id, kind, provenance, login). That log's HMAC chain,
size rotation and age retention bound it; there is no second audit store.

Every record also carries, in `ActionRecord.extra` and the event metadata,
`workload_identity_ref`, always `unavailable` in this build, so the human and the
Crew workload are named separately. Work no person started (a
system or Unknown initiator in a `cron` or `hook` session) adds `trigger` `{kind,
ref, name}`; a human initiator never gets one. The causal join is the record's
`target` (the work id for work actions), `session_key`, `turn_id`,
`causal_action_id` and initiator id, and the initiator stays distinct from the
work's owner (`budget_key`) when a collaborator acts on someone else's work.

The write is the log's fail-closed `critical` mode, off the event loop, bounded by
`AUDIT_TIMEOUT_SECONDS`. `record_action` returns whether the record is confirmed.
When it is not, the action still applies and its response says it was not
audited (`audited: false`). A refused audit must not wedge a turn, and it must not
be reported as recorded. An action whose own durable row already names its actor
(a message row carries its author) is audited with `schedule_action_audit`,
which does not make the caller wait and makes no audit claim in the response; a
failed write is logged.

Audited sites, all through `schedule_action_audit`:

| Action | Site | Initiator | Responder | Outcome |
|---|---|---|---|---|
| `session_create` | `chat_api.slot_create` | request actor | | `accepted` |
| `message` | `chat_api.chat_send` | request actor | | `accepted` |
| `cancel` | `routes_admission.api_work_cancel`, target the work id | request actor | | `applied`, or `conflict` on a stale or illegal revision |
| `approval`, `question_answer` | `DecisionRegistry.resolve` (`decisions.audit_decision_response`), target the decision id | the decision's initiator | the verified responder | `applied` for the winner, `conflict` for a different response that lost, including one that reaches the settled decision after its wait ended (`DecisionRegistry.classify_late`, from the question answer and approval routes); a replay of the winner and a response to nothing pending add no record |
| `memory_change` | `cron.api_lessons_create` (`memory_changes.announce_replacement`), target `lesson:<category>` | the lesson's writer | | `applied`; only for a write that replaced stored lessons |

### Memory changes

Crew memory is fully shared. A lesson, correction or preference any admitted
person teaches (the `learn_add` tool, the dashboard, the CLI) is written to the
Crew's one store and applies to everyone, the owner included. The owner gets no
priority. `memory_changes` (`shared_crew/memory_changes.py`) owns the three things
around that write.

Writer. `writer_principal` derives who the write is attributed to, never from the
request body: the verified actor of a person's own dashboard request; for an agent
write (`learn_add` posts through the internal caller), the author of the latest
user turn in the calling session (`decisions.turn_initiator`); else Unknown. A
known writer is stored on the row as `written_by` `{id, kind, name}` in both lesson
stores. A row without one (every lesson written before this) loads unchanged.

Newest wins. The lesson stores decide what conflicts. The vector store (V1) replaces
an older lesson on topic overlap (shared keywords at least half the larger set) or
cosine similarity above 0.85, whoever wrote either, since every route write is
`user_explicit`. The JSONL store replaces a stored `preference` that overlaps a new
explicit `preference` the same way. A member's private V2 store deletes only on an
exact key. Two contradicting rules are never both kept, and the route reports what
it replaced in `superseded`.

Everyone is told. When `superseded` is not empty, `announce_replacement`:

1. Schedules one `memory_change` audit record: initiator the writer, target
   `lesson:<category>`, and in `extra` / the event metadata `memory_change`
   `{category, old: [summary], new: summary}`. Summaries are redacted
   (`_redact_memory_field`) and clipped to one line of `SUMMARY_MAX` (120)
   characters, at most `OLD_MAX` (5) old ones. No full lesson body, prompt text or
   secret.
2. In a shared Crew only (`crew_is_shared`: the writer came over a network path, a
   sharing path is on, or anyone but the operator has been seen), appends a system
   notice to the session the write came from and sends a `shared_crew.memory_change`
   frame `{type, schema_version, change}` to the owner and every collaborator
   (`broadcast_to_collaborators`). The notice is an assistant row with
   `meta.kind = "memory_change"` (a `SYSTEM_NOTICE_KINDS` member on both sides) and
   `meta.memory_change` = the change payload `{actor: {id, kind, name}, session_key,
   category, new, old}`. Its text is English ("Bob changed a Crew preference: ...");
   the dashboard renders the localized `sharedCrew.memory_change_*` line from the
   payload.

A personal Crew has one person: the newer lesson still replaces the older one and
the audit record is still written, but no "changed by" notice or frame is sent. A
notice or audit failure never fails the write.

## 4. Admission and accepted work

### Operation state machine

`admission.TRANSITIONS` is the one state machine every entry point reports through.

```
submitted -> queued | admitted | rejected
queued -> admitted | cancelled | expired | reconciling
admitted -> running | queued | cancelled | expired | reconciling | failed
running -> cancelling | completed | failed | reconciling
cancelling -> cancelled | completed | failed | reconciling
reconciling -> queued | running | completed | failed | cancelled | unknown_outcome
```

`submitted` exists only in the client. `rejected` is reachable only from it, and
`AcceptedWork` refuses both: a rejected request was never accepted. `admitted ->
queued` is a start-time revalidation that found capacity gone; `admitted ->
cancelled` is one that found the grant or policy revoked, so a queue record is never
standing permission. `reconciling -> queued` requires proof the work never started.
Ambiguous execution after a crash ends in `unknown_outcome` and is surfaced, never
replayed.

`TASKQ_STATE_MAP` maps every `kiro_crew.taskq.model` state onto this machine; the
durable queue stays the store of record and this is its projection.

### Capacity accounting

Work counts against active capacity in `CAPACITY_HOLDING` and against queue depth
while `queued`. `AdmissionService._maybe_release` returns capacity only for terminal
work whose runtime is proven stopped and whose children have all settled.
Restart, disconnect or lease expiry therefore never frees a slot while a child lives.

`RuntimeOwnershipRef` points into the existing ownership registry
(`runtime_ownership` lease, `RuntimeTenancy` epoch, taskq generation, systemd scope
unit). It is not a second registry. Its `liveness` is what the reconciler proved,
never what a client reported.

### Requests and idempotency

`AdmissionRequest.budget_key` is the initiator's principal id. It is never the session
creator and never a per-device token, so tabs, devices, reconnects and child tasks of
one person share one budget. Work in a shared session is charged to whoever started
it.

The client sends an `Idempotency-Key` header per logical send and reuses it on retry.
The deduplication scope is `idempotency_scope(budget_key, session_key, key)`, retained
for `IDEMPOTENCY_TTL_SECONDS`. A replay with the same `request_digest` returns the
existing `AcceptedWork`; a different digest is `idempotency_conflict`. A retry never
runs a prompt twice.

A chat send asks `admission_entry.chat_replay` before it writes anything, so a retry
under a key already accepted, whether the first send started a turn, queued behind a
busy session or already finished, answers JSON with `replayed: true`, the recorded
`admission` and, for a queued message, its `queue_id`. It appends no row, queues
nothing, starts no turn and opens no stream. Only a client-supplied key can match;
every chat path records the same digest (`admission_entry.chat_digest`), so a retry
matches whichever path the first send took. A replayed `Admission` handle closes its
coroutine unrun.

### Responses

A lost send race is settled before admission and is not an admission refusal:
it answers 409 `send_race` with the winner (section 5, First submit wins) and
creates no `AcceptedWork`. The race-versus-queue boundary is open; the POC
settles races by revision compare-and-set and leaves capacity to admission.

A refusal or conflict answers `admission.error_body`: the dashboard's
`{"error", "code"}` envelope, plus `capacity` on a limit and `current` (the public
`AcceptedWork`) on a conflict, so a stale UI action reconciles against server truth.
The status comes from `REASON_HTTP_STATUS`, and a refusal with a retry hint sends
`Retry-After` (`retry_after_header`). `CapacityResponse` reports both scopes' limits
and usage and the effective `EnforcementLevel`. It has no wait-time field, because the
scheduler cannot back one.

### Fairness

Owner: `shared_crew.scheduler` (pure; one lock, so a race at the limit admits exactly
as many as there are slots) behind `shared_crew.admission_gate.AdmissionService`.

Two work classes. A ROOT (chat turn, task run, workflow run, cron run) is admitted
while its principal holds fewer than `principal_active` and the Crew fewer than
`crew_active` root slots; otherwise it waits in its principal's FIFO, bounded by
`principal_queue` and `crew_queue`, and a submission past either is refused
`principal_queue_full` / `crew_queue_full`. A CHILD (subagent) never queues here:
the durable task queue is its queue, so the gate bounds what a principal and the
Crew have outstanding (`principal_children`, `crew_children`) and refuses past it.
Background runs (task runner, workflows) cannot wait in a request, so they start
now or are refused with the active-limit reason.

A message sent to a session whose turn is running (or held behind its running
subagents) waits for that turn, not for capacity. It is accepted as parked work
(`admit(park=True)`, `FairScheduler.park`): durably `queued`, charged to its sender,
bounded by `principal_queue` and `crew_queue` and `max_request_bytes` like any root,
and counted in `queued` capacity, but never dispatched. The queue entry carries its
work id (`admission_entry.QUEUED_WORK_META_KEY`); the session's drain runs the turn
as that work (`turn_gate.queued_work`, `AdmissionService.start_parked`), which takes
its place in the fair queue then. Messages a drain merges into one turn end with that
turn's outcome (`extra.merged_into`). A parked message's expiry is a check-in: while
its queue card exists it waits on; once the card is gone it ends `cancelled`
(`queue_entry_gone`). A card cancelled through the work API, or whose sender was
removed, does not run.

Capacity lives in memory (the scheduler and the live table); the store is the
durable record. When any admission write fails (promotion, the pre-start
`running` write, settle, cancel, expiry, attach; the task store is locked past its
timeout), `AdmissionService._fail_closed` ends the work in memory at once: it
stops the child (`_stop_child`), wakes every waiter and logs a warning. The work
keeps its slot and stays tracked with its last durable record
(`_Live.unpersisted`); every ticker pass retries the terminal write
(`admission_settle.persist_pending`) and frees the slot only once the store
records it, so capacity is never released ahead of the durable transition. A
merged queued message whose settlement write fails is kept the same way. A
deadline whose `cancelling` write fails still stops the
child. A caller cancelled before its work starts (a queued chat closed while it
waits) ends that work `cancelled` (`admission_parked.end_unstarted`); any other
error before the start fails it closed, so its ticket holds a slot only until
its terminal write lands.

A background run admitted now can be cancelled while its start is still in flight.
`_end` remembers the ended, not-yet-attached work with its operation's cancel hook,
and `attach_child` then finds no live record and stops the new execution through
that hook, so cancelled work never keeps running outside admission.

Across principals the scheduler is deficit round robin. Principals with queued roots
sit in a ring; a visit adds the quantum (1) to the principal's deficit and admits
while the deficit covers the head ticket's weight (1) and both scopes have room. A
principal blocked by its own limit is skipped with its deficit reset, so no credit
is banked while ineligible. Within one principal the queue is FIFO, so order within
a session is kept. The subagent pump's existing lane round-robin becomes per person
too: `taskq.lanes.set_lane_resolver` maps a root session to `principal:<id>` while
admission is on.

Starvation bound: with `P` principals holding eligible queued work, a continuously
eligible principal is admitted within `(P - 1) * (quantum + max_weight - 1)`
admissions of others, which is `P - 1` with the defaults. Every admitted root ends
within its run deadline, so the wall-clock wait is at most
`ceil(P / crew_active)` root deadlines; work that would wait past `queue_expiry`
expires `queue_expired`, reported, never hidden.

Who is charged. The initiator, read from `request[ACTOR_REQUEST_KEY]`
(`admission_gate.actor_budget`); never the session creator. Every caller without a
principal is `p_unknown`, one shared bucket. A child inherits the budget and root of
the work running in its parent session (`budget_for_session`), so a collaborator
steering or spawning inside someone else's turn charges that turn's initiator. Cron
and loopback automation with no recorded initiator charge `Principal.system("automation")`,
which has the same limits as anyone. Work an agent starts from inside a turn reaches
the gateway on the internal transport naming its calling session (`X-Session-Key`,
set by the in-process MCP broker). The header alone is the caller's word, so the
session counts only once proven (`admission_gate.attest_internal_session`): the
auth layer kernel-attested the declared key (`peer_verified`), or a per-session token
the gateway signed (`X-Session-Token`, `session_token_sig`) names it. The work is
charged to the principal admission bound to that session server-side
(`internal_initiator`, `budget_for_session`). An internal request whose session is
unproven or holds no work is charged to the bounded Unknown bucket, never to
automation, so a person cannot gain a second budget through automation.

Native Slack's capacity entry uses its dispatcher's `_is_control_message`
predicate, so bang commands such as `!yolo off` and modifier-only messages reach
their existing sender and channel-policy checks without waiting for model
capacity. Mention and privacy-token parsing are shared with dispatch; a privacy
modifier with a remaining prompt still faces admission. Model messages re-enter
the full handler after admission, preserving its start-time checks. Telegram and
`ChannelTurns` callers dispatch commands before entering their turn gates.

A turn started from another turn's end (the chat queue drain, a Side Chat follow-up, a channel wake or held retry) starts outside its predecessor's admission (`turn_gate.independent_turn`) and is admitted on its own; work nested inside a turn stays in that turn's admission.

### Accepted work, revalidation and recovery

`shared_crew.work_store` keeps each `AcceptedWork` as one `accepted_work` row in
`tasks.db` (`taskq.store_work`, created by `taskq.migrate`; additive, so the schema
version stays and an older build leaves the table in place). The row is committed
before the caller hears `admitted` or `queued`. Idempotency is the unique `scope`
column; a replay returns the stored record and a replayed handle never runs twice.
Terminal rows are purged after `IDEMPOTENCY_TTL_SECONDS`.

`Admission.run` waits for a slot, then revalidates right before starting: queue
expiry, `policy_version` (a change to `admission_enabled`, `hard_limits` or
`containment_enabled` since acceptance is `policy_changed`), the hard-limit probe,
and the entry point's own check (a chat slot must still be live). `running` is
written before the work launches, so `admitted` proves it never started. A queued
chat turn holds the slot reservation; while it waits a later send on that slot
cannot steer into it (`admission_entry.admission_pending`) and queues instead, so a
queued prompt is never delivered as a steer.

Disabling admission affects new submissions immediately. Maintenance keeps running
until accepted live work settles: queued work (including parked messages) ends
`cancelled` with `policy_changed`, waking its waiters without running its coroutine;
attached executions keep their reservations until their owner reports them terminal.
Messages merged into a running turn keep that turn's outcome.
The ticker stops once disabled admission has no live work left.

The gateway's own stop (a signal or an in-app restart, read through
`host_hooks.shutdown_probe`) never records `completed` for work it cut: a running
turn that returns once the stop began ends `cancelled` with
`extra.reconcile_detail = host_stopped_while_running`, and admitted or queued work
that would start then ends `cancelled` with `host_stopped_before_start`, unrun.

Deadlines: `root_deadline_seconds` for roots, `child_deadline_seconds` for children
and background runs, enforced by `AdmissionService.tick` (once a second): it cancels
the work and records `cancelled` with `run_deadline`. A root's slot is released only
after its own task ended and every child it started settled; a child's slot only
after its owner (subagent manager, task runner, workflow registry) reports it
terminal.

Both server entry points (the dashboard and the API-only server) register the same
startup lifecycle. Before the socket binds, `startup_check` runs only the bounded
containment probe and publishes the hard-mode refusal; reconciliation and the stale
slice sweep run as a maintenance task after the socket is ready, and the shutdown
cancels it. Before the first admission or capacity read of a process, `ensure_ready`
reconciles every non-terminal row:

| Found | Becomes | Capacity |
|---|---|---|
| `queued`, `admitted` | `reconciling`, then `queued` and relaunched when a relauncher is registered for its operation and payload, else `cancelled` with `extra.reconcile_detail = host_restarted_before_start` | held only if requeued |
| `running`, `cancelling` with no unit of its own | `reconciling` -> `unknown_outcome` (`cancelled` if a cancel was asked) | released once proven stopped |
| `running` with a live scope unit, or a principal slice holding a populated `kirocrew-rt-*.scope` | `reconciling`; the scopes found are stopped (`extra.orphans_reaped`), and the row ends `unknown_outcome` once `systemctl is-active` says each stopped | held |
| child or probed background run | `reconciling`; its owner reconciles the run and the probe settles the row | held |

Nothing that may have run is replayed. No relauncher is registered for chat turns in
this build, so a chat turn queued across a restart ends `cancelled`, and the session
says so in an error row (`host_hooks.reconcile_note`, also for `unknown_outcome`). A
parked message is the exception: its queue entry is saved with the session and drains
again after the restart. A session that has never been saved gets its metadata line
before its first work is accepted (`host_hooks.ensure_slot_recorded`), so a crash
right after the accept cannot leave a work record for a session that is gone.
A false persistence result refuses the chat send before admission; child cancellation
uses the manager's off-loop queued stop under its refill hold, and a finished
`user_stopped` child settles as `cancelled` rather than `completed`.

A chat turn's work carries its accepted prompt and author as its payload
(`admission_entry.CHAT_PROMPT_PAYLOAD_KEY`, `CHAT_AUTHOR_PAYLOAD_KEY`) until it ends;
reconciliation reads it before the terminal write clears it and hands it to the note.
Notes wait until the startup restore has built its slots
(`host_hooks.mark_sessions_restored`). A session no slot holds is opened from disk,
so a session the crash left without an open-tab record can be opened; when it never
saved the accepted prompt, the prompt is put back as its user row ahead of the note.
The prompt is only shown, never run again.

### Containment

Owner: `shared_crew.containment`, reusing `sandbox_cgroup` and `sandbox_slice`.
Each principal's runtimes start in `kirocrew-agents-<instance>-p<sha256(id)[:12]>.slice`,
a child of the instance slice, sized by `systemctl --user set-property --runtime`
(`principal_cpu_quota_percent`, `principal_memory_max_mb`, `principal_tasks_max`); the
instance slice gets `crew_cpu_quota_percent`. `sandbox_slice.cgroup_scope_argv` picks
the principal slice from the `principal_scope` context the gate sets around admitted
work and around subagent run tasks. A principal slice nothing of its principal has
used for `SLICE_IDLE_SECONDS` (600) is stopped and its runtime drop-ins removed
(`systemctl --user stop` then `revert`), only while no runtime scope in it holds a
process; the gateway's stop releases every one at once, including the slice of a turn the
stop interrupted, repeating the sweep a bounded number of times
(`SHUTDOWN_SWEEP_ROUNDS`) while a runtime scope it ended still empties, then the
instance slice once its own control group is empty (`release_instance_slice`). The
gateway's hard-exit path runs the same release after it reaps orphaned sessions, so a
graceful shutdown that ran out of time does not skip it. Its start releases those an
earlier process left (`AdmissionService.sweep_slices`, `shutdown_cleanup`,
`release_stale_slices`). The slice is forgotten before it is stopped, so a spawn
racing the release lands in the instance slice. A shared session's runtime is one process, so it
stays in the slice of the initiator whose turn started it; later turns by others are
bounded by admission counts, not that slice.

The guarantee is `best_effort` and is reported that way: the kernel enforces the
CPU, memory and pids ceilings for cooperative or buggy work, but every runtime runs as
the gateway's uid, which can reach the user bus and the delegated subtree, so
adversarial escape is not closed (`adversarial_escape_closed: false`). IO is not
delegated on the supported profile and is `unavailable`. Disk is `app_accounting`:
`shared_crew.accounting` charges accepted request bytes per principal to the durable
`principal_storage` ledger and refuses past `principal_storage_bytes` with
`storage_limit`; it is not a filesystem quota.

`hard_limits` turns any missing prerequisite (`not_linux`, `systemd_run_untrusted`,
`systemctl_untrusted`, `no_user_session`, `user_bus_unreachable`, `not_cgroup_v2`,
`controllers_not_delegated`, `controllers_unreadable`, or `containment_probe_failed`
when the probe or the startup check raised) into `containment_unavailable`
(503) at admission. In hard mode slices must be enabled and both the Crew slice and
the principal slice must apply; an accepted turn whose slice setup fails settles
without running instead of starting in the instance slice. In either mode, an
unreadable or malformed `config.json` keeps the limits already in force, and a
process with no valid limits yet fails closed. With a prerequisite missing, `runtime_start` refuses every runtime spawn and `session/new`
with `ContainmentUnavailable` (the backstop for entry points that do not reach the
gate). With it off the gate admits and the capacity answer says what holds.
`hard_limits` needs fair admission, which applies the limits: with
`admission_enabled` off and `hard_limits` on, nothing would apply them, so the
gateway fails closed with `admission_disabled` (every admission and every runtime
start refused, logged at ERROR at start) rather than running unlimited.

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

Presence (`collab.PresenceEntry`) is telemetry. It is bound to the connection's
verified actor, folds every tab of one principal into one entry, expires after
`PRESENCE_TTL_SECONDS` without a refresh, is rate-limited to one activity frame per
`ACTIVITY_MIN_INTERVAL_SECONDS` per connection (the one step down from a reported `typing` in the same session is exempt and goes at once, so a send ends typing for everyone), and is never persisted. It never
grants access or frees a slot. Draft text is never sent.

`presence.PresenceRegistry` implements it in memory. `dashboard.ws` reads the
connection's principal once, from `request[ACTOR_REQUEST_KEY]` on the upgrade
request (Unknown when none was published), and hands each `shared_crew.activity`
frame to `presence.handle_activity_frame`, which reads only `session_key` and
`activity`, admits dashboard-user sockets only, and only for a session the gateway
has open. A refused frame (too soon, malformed, registry full) changes nothing and
is never answered with an eviction of someone else's entry. Disconnect and expiry
push a fresh `shared_crew.presence` frame for every session they changed; the
socket's status tick sweeps stale entries.

In the browser, typing is reported from the composer's user-edit handler
(`typingSignal.useComposerEditWithTyping`, which ChatInput calls only for the
user's own edits), not from the draft store: the store keeps one identity and a
session switch writes it too. The collab strip's `usePresenceReporter` listens to
that content-free signal and sends `typing`, then `viewing` after
`TYPING_HOLD_MS` without a keystroke.

A shared decision (`collab.SharedDecision`) is a tool approval or agent question that
every collaborator sees and any one may answer. `classify_response` decides a response
under the resolver's lock: the first response to see `pending` wins, a retry with the
winning `response_id` replays the same outcome, and any other response receives the
resolved decision with `already_resolved`. A resolved decision always names its
responder.

`decisions.DecisionRegistry` is that resolver: one in-memory, bounded record per
decision, with the classification, the side effect that settles the waiting future,
and the state write under one lock. A decision id is derived from what the card
already shows (`approval:<len>:<slot>:<request id>`, `approval-state:<id>`,
`question:<ask id>`), so every tab computes it without a lookup. A decision opens
the moment its card shows: a slot permission prompt in `chat_runner` (unless it was
declined at its append), a background approval in `ApprovalCoordinator.request`, and
a blocking or agent question in `QuestionCoordinator`. The initiator is the
author of the turn that asked (the latest user message's `author` stamp) or, for a
background approval, the `Principal.system` of its source; the responder is the
request's verified actor. Every existing gate on the approve and answer routes runs
before the registry sees a response. A client that sends `response_id` gets 409
`already_resolved` with the decision on a lost race; a client that sends none keeps
the existing 404. A wait that ends through a path with no verified responder closes
the decision with an Unknown responder, and an unanswered one closes as `expired` or
`withdrawn`. Approval decision frames ride `broadcast_ws` like approval frames.

An agent question belongs to every collaborator. `collaborators.is_collaborator_request`
admits the owner (`is_owner_dashboard_request`) and a dashboard-user session whose
verified actor is a person a membership boundary admitted: provenance
`tailscale_serve` (allowlisted and pinned by the middleware), `tailscale_listener`
(admitted by the direct tailnet listener's access set). A
`tailscale_direct` peer (attribution only, on a widened bind), a Slack-allowlist
dashboard token, an app token, an internal caller and Unknown are not
collaborators and keep the owner gate's 403.
`GET /api/ask-question/pending`, `POST /api/ask-question/{id}/answer` and
`POST /api/ask-question/dismiss` accept every collaborator; opening a question
(`POST /api/ask-question`) stays owner-only. The card frames (`question_card`,
`question_card_resolved`) and the question decision frame go to the owner sockets
and to every collaborator socket (`collaborators.broadcast_to_collaborators`, which
reads the socket's `_shared_crew_actor`), the same audience the pending read has.
Nothing else the owner set receives reaches a collaborator socket. `GET /api/shared-crew/presence` and `GET /api/shared-crew/decisions` (in
`routes_collab`) answer the same state for a reconnecting tab.

Message attribution. The session's creator is its default owner, shown on the
session row in the list (`SessionCreatorByline`). In the transcript a user message
written by the creator carries no extra label; a message any other participant
wrote carries the sender's name (`MessageByline`, `sharedCrewUi.bylineFor`). A
session with no recorded creator (legacy) treats the viewer as the default owner, so
a single-operator transcript is unchanged. An Unknown author reads as Unknown.

### Typing lease

Product owner's decision (2026-10-18), replacing the earlier "no input lock"
non-goal: while one collaborator types in a shared session, every other
collaborator's composer is read-only, the way a shared document shows who is
editing a line. Owner: `shared_crew.typing_lease`.

Rules:

1. One lease per session (keyed by slot), held by one person: the verified
   connection principal the socket handler stamps, never a value from a frame. A
   person's tabs fold into one holder (`typing_lease.same_person`: the same
   principal id, or the local operator's machine-local subjects from
   `crew_mode.operator_ids`), so their other tabs stay unlocked.
2. The first collaborator whose tab sends an accepted `typing` report with a
   non-empty draft acquires a free lease. Each later accepted `typing` report from
   the holder renews it. A typing report never takes a held lease away. Unknown and
   system principals never hold one.
3. The lease ends when the holder's send is accepted (`send_race.commit_request`
   calls `note_sent`), when a holder tab reports its draft empty
   (`"draft": "empty"`), when no holder tab has reported `typing` for the idle
   window, or when every holder tab that was typing closes its socket (tab close
   or disconnect). A plain step down to `viewing` (the tab's typing hold ran out)
   keeps it: the draft is still there and the idle window decides.
4. The idle window is `dashboard.shared_crew.typing_idle_seconds`, default 10,
   clamped to 3..60 (`SHARED_CREW_BOUNDS`). A timer armed on the event loop at the
   earliest lease's idle end (`arm_idle_timer`) ends it on time; the socket status
   tick's presence sweep also sweeps leases.
5. The host enforces it. `chat_send._composer_locked_answer` runs before the
   send race: a dashboard user's send while someone else holds the lease answers
   409 `{"error", "code": "composer_locked", session_key, holder (public), lease}`
   and nothing is accepted. A steer into a running turn passes, and Stop is a
   separate route, so a locked collaborator can still steer or stop the turn that
   is running. An app or automation send has no composer and is not held.
   First-submit-wins (below) stays the backstop for the moment a lease ends.
6. A personal Crew never locks: the only person is the operator, who is always
   the holder's own person. The session creator has no priority; there are no
   roles.

The lease rides the presence channel and its rate limit: a `typing` report renews
only when `PresenceRegistry` accepted it, and an empty-draft report only releases,
so it applies even inside the spacing. Every change (acquired, ended) pushes one
whole-value `shared_crew.typing_lease` frame to every dashboard socket;
`GET /api/shared-crew/typing-lease?session_key=` answers the same body for a
reconnecting tab: `{schema_version, session_key, holder (public) | null,
holder_ids, expires_at, idle_seconds, ended}`. `holder_ids` lists every id that
counts as the holder, so a tab tells its own lease from someone else's without a
second read; `ended` names why a lease just ended (`sent`, `cleared`, `idle`,
`disconnected`). Nothing is persisted: no draft text and no keystroke, only who
holds the lease, which of their connections are drafting, and when each last
reported.

In the browser (`usePresenceReporter`), a keystroke repeats `typing` once per
spacing while keystrokes continue, so a trailing report lands after the last one;
an edit that leaves the draft empty, and a send, report `draft: 'empty'` at once.
`useComposerTypingLock` reads the lease and the viewer; when someone else holds
it, ChatInput's `typingLock` turns the box read-only (not disabled: the draft
stays as it was, and so does focus), replaces the button row with
`TypingLockBanner` (initials and "<name> is typing…"), and keeps only Stop and
Steer for a running turn. When the lease ends the box unlocks in place. A send
refused `composer_locked` restores the draft and records the refusal's lease
(`api/typingLease.noteComposerLocked`), so the lock shows even before the frame
lands.

### First submit wins

POC choice, not an approved product contract: revision-based compare-and-set
(`shared_crew.send_race`). With the typing lease in place it settles the sends the
lease does not hold: a send at the moment a lease ends, an app or automation send,
and a steer into a running turn. The boundary between a refused race and accepted work
waiting for capacity stays open; this is the shape the POC ships to exercise it.

Each session (keyed by slot) has a send revision, the count of accepted sends.
`GET /api/shared-crew/send-state?session_key=` answers `{schema_version,
session_key, revision, last}` and every accepted send pushes the same body as a
`shared_crew.send_state` frame. A send carries the revision its tab last saw as
`base_revision` and a per-send `Idempotency-Key`. A send's base is the revision its tab knew at the press, for a steer into a
running turn too, so a frame that lands after the press never raises it. A tab reads
the send state for every open session whatever the capabilities, and a send whose
revision was unknown at the press joins that read (`ensureSendRevision`, up to 1.5 s), then goes without a
base. A session the send itself creates starts at revision 0. A frame or a read
only raises the tab's revision; a `send_race` conflict answer replaces it with the
host's `current_revision`, even when lower, so a tab open across a host restart
(the host counts from zero again) recovers on its first conflict and Send now goes
at the host's revision.

`chat_send` settles the race (`_settle_send_race`) after the empty-message guard
and before the responsive, steer, busy-queue, sub-agent-hold and new-turn branches,
so a losing send reaches none of them. `SendRaceRegistry.claim` decides, in order:

1. while another claim on the session is pending, wait for it to settle (bounded by
   `SETTLE_WAIT_SECONDS`; past it the pending sender wins);
2. a key this principal already won with is a replay: the admission replay answers
   when it has one, else `{ok, replayed, send_revision, message_id}`; nothing is
   accepted again, so a retry after a lost winner response never runs twice;
3. no `base_revision` (app token, script, old client): not compared;
4. a session with no accepted send since process start accepts any base, and its
   next revision continues above that base;
5. otherwise the earliest send accepted after the base by a DIFFERENT principal
   makes this send lose. One person's own tabs never race each other, so a
   single-operator session behaves as before.

A granted claim is pending until the branch that records the send commits it (the
user row, the queue entry, the steer or the responsive accept); `api_chat` releases
an uncommitted claim in a `finally`, so a send refused for capacity, a closed
session or any other reason never makes a collaborator lose to nothing. Commit bumps
the revision by one and records `{revision, principal, message_id, accepted_at}`.
`message_id` is the user row's `mid`, else the queue id, else the client `sendId`.

A loser answers 409 `{"error", "code": "send_race", session_key, current_revision,
winner: {revision, principal (public), message_id, accepted_at}}`. Nothing was
accepted: no row, no queue entry, no steer, no turn.

Send now is a new send with `send_now: true` and `base_revision` set to the
conflict's `current_revision`. `send_now` forces `steer` off, so it goes through
normal fair admission and may queue behind the running turn or a capacity limit;
it can lose again if yet another send landed first.

Everything is in memory and bounded (`MAX_SESSIONS`, `HISTORY_PER_SESSION`). A tab
that never learned a revision sends none and is not compared.

## 6. Wire shapes

### HTTP

| Route | Answer |
|---|---|
| `GET /api/shared-crew/capabilities` | `Capabilities` |
| `GET /api/auth/me` | gains `principal` (`Principal.to_public_dict`) |
| `GET /api/shared-crew/sessions?creator=&q=&cursor=&limit=` | `SessionPage`; each item carries `attribution` |
| `GET /api/shared-crew/people` | observed collaborators with usage, and the directory state |
| `GET /api/shared-crew/capacity` | `CapacityResponse` for the viewer |
| `GET /api/shared-crew/work?session_key=` | public `AcceptedWork` list |
| `POST /api/shared-crew/work/{work_id}/cancel` `{expected_revision}` | the next `AcceptedWork`, or 409 with `current`; a planned task or schedule run is owner only (403 `owner_only`), as on its own cancel route |
| a chat send | 200 when admitted, 202 with `work` when queued, 429 / 413 / 503 with `error_body` |
| an approval or answer, with `response_id` | the `SharedDecision`; 409 `already_resolved` with it on a lost race |
| `GET /api/shared-crew/presence?session_key=` | the session's `PresenceEntry` list (dashboard users only) |
| `GET /api/shared-crew/decisions?session_key=` | the session's `SharedDecision` list (dashboard users only) |
| `GET /api/shared-crew/send-state?session_key=` | the session's send revision and last accepted send (dashboard users only) |
| a chat send with `base_revision` | 409 `send_race` with `winner` and `current_revision` when another principal's send was accepted after it |
| `GET /api/shared-crew/typing-lease?session_key=` | who holds the session's typing lease (dashboard users only) |
| a chat send while someone else types | 409 `composer_locked` with `holder` and `lease`; a steer into a running turn is not held |
| `GET /api/shared-crew/tailscale/invites` | owner: `connected`, `tailnet`, key expiry, `invites`, `members`, `sync` (§2 Tailscale invites) |
| `POST /api/shared-crew/tailscale/invites` `{emails, multi_use, alias, avatar_id}` | owner: the created `invites` |
| `POST /api/shared-crew/tailscale/invites/{id}/resend` | owner: `{resent: true}` |
| `DELETE /api/shared-crew/tailscale/invites/{id}` | owner: `{revoked, member_removed, closed_connections}` |
| `POST` / `DELETE /api/shared-crew/tailscale/credential` | owner: connect `{key}` or remove the key; the status body, never the key |
| `POST /api/shared-crew/tailscale/avatars` `{data_base64}` | owner: `{avatar_id}` |
| `GET /api/shared-crew/tailscale/avatars/{avatar_id}` | any dashboard user: the image |

Browser-bound principals use `to_public_dict` (no issuer or subject); browser-bound
work uses `AcceptedWork.to_public_dict` (no idempotency scope, lease or fencing ids).

### Session list filter

`SessionQuery.from_query` parses `creator` (`all`, `mine`, `unattributed`, its older
spelling `unknown`, or a principal id), `q`, an opaque `cursor` and `limit` (clamped
to `SESSION_PAGE_MAX`). The filter
runs on the server before pagination. `mine` resolves against the authenticated
viewer, and never matches for an Unknown viewer. The filter is discovery, not access
control. `SessionPage.total_matching` counts every session the filter passes across
all pages; `hidden_by_filter` counts the sessions it excluded.

### WebSocket

The server pushes whole-value frames: `shared_crew.presence`, `shared_crew.work`,
`shared_crew.decision`, `shared_crew.capacity`, `shared_crew.send_state`,
`shared_crew.typing_lease` and `shared_crew.memory_change` (`collab.SERVER_FRAMES`). The
client sends `shared_crew.activity` with a session, an `Activity` and, when the
tab's draft is empty, `draft: "empty"`. The activity frame carries no principal and
no text; the server stamps the connection's actor and ignores anything else. On reconnect the client refetches the HTTP state and treats the next frames as
authoritative. It never resubmits a send on its own.

## 7. UI states

## 8. Capabilities

`Capabilities` reports what this Crew can back. Every field defaults to
`unavailable`, so a build that wires nothing claims nothing. `containment_level`
states the effective guarantee (`hard`, `best_effort`, `none`), never the configured
intent. `membership_link` is an id from the closed `MEMBERSHIP_LINKS` set, never a
URL taken from config or a peer.

### Aggregation

`capabilities.register_capability_source(name, fields, source)` is how a behaviour
module speaks for a field; the gateway wires the registrations at startup.
`aggregate_capabilities` combines them conservatively:

- A source answers only for the fields it declared. A name registers once.
- A source that raises, or returns anything but a `CapabilityContribution`, makes
  each declared field `unavailable` with reason `source_failed`.
- Two sources on one field: the weaker state wins (`unavailable` < `disabled` <
  `degraded` < `available`), and the weaker `containment_level` wins.
- `containment_level` is `none` unless `containment` is `available` or `degraded`.
- Two different membership links leave the link empty.

A source answers from state it already holds; it does no I/O on the request path.

`GET /api/shared-crew/capabilities` (`routes_capabilities`) answers
`Capabilities.to_dict()` plus these additive keys, states and reasons only, sent
`no-store`:

- `reasons`: field to `no_source` or `source_failed`, or for `attribution` the
  forwarded-identity gap (`platform_unprovable`, `daemon_same_user`,
  `daemon_not_found`, see Derivation). A source names a reason in
  `CapabilityContribution.reasons`; the weakest answer's reason is reported;
- `setup_paths`: each shared-access setup path (`access.SETUP_PATHS`: `tailscale`,
  `kiro_login`, `systems_manager`) to `{state, reason}`. Tailscale reports what the
  gateway resolved at start (`access.note_tailnet`): `available` once Tailscale access
  is on and the tailnet name resolved, or once identity trust with an allowlist is on
  (`tailscale serve` set up by hand), `degraded` (`tailnet_unreachable`) when it is
  on without a name, else `not_configured`. The other paths report
  `not_in_this_build`;
- `keep_awake`: `state` (`available` | `unavailable`), `reason` (`no_backend`,
  `unsupported_platform`, `not_checked` until the post-start probe has run),
  `backend`, `laptop` and `laptop_reason` (`battery_present`, `no_battery`,
  `unknown`), from `host_power.note_host`, which `host_power.probe_after_start`
  runs in a background task after the listener is up (see Access set, Keep awake).

The People surface (Settings > People) shows one setup empty state while no setup
path is `available` and no one but the viewer has been seen: it names the three paths, each with the availability its
capability reports and a link to existing docs only. It offers no button that does
nothing.

### Not in this build

Enterprise sign-in, per-person grants, revocation feeds, workload identity, Crew
registration and inference budgets are not part of this build; their seams are
described in the design doc. Every admitted person can use everything the Crew
can: there is no per-task authorization layer.

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

Prerequisites. One gateway on a host the operator controls. Collaborators reach it
through `tailscale serve` (preferred) or the tailnet address directly (only when
`KIROCREW_BIND` widens the bind), with
`dashboard.tailscale.trust_identity` on and `dashboard.tailscale.allowed_logins`
listing each person (section 2). Without that opt-in every remote caller is Unknown.
Per-person containment needs Linux with cgroup v2, a `systemd --user` session and the
cpu, memory and pids controllers delegated to the user slice; anything else runs with
admission counts only and reports it.

Supported profiles.

| Profile | Admission | Containment | `hard_limits` |
|---|---|---|---|
| macOS, Windows, Linux without delegation | counts and queues | `unavailable` | refuses all work, by design |
| Linux, cgroup v2, delegated user slice | counts and queues | `best_effort` (CPU, memory, pids) | allowed |
| Unrestricted host root | counts and queues | `best_effort` | not adversarial containment |

Start, stop and configure. Start with `kirocrew gateway` (or the installed service)
and stop with `kirocrew stop`. Limits live under `dashboard.shared_crew` (section 4)
and are re-read within seconds, without a restart (every turn gate call refreshes
the gateway's service, so enabling admission applies to the next turn):

```bash
kirocrew config set dashboard.shared_crew.admission_enabled true
kirocrew config set dashboard.shared_crew.principal_concurrency 2
kirocrew config set dashboard.shared_crew.hard_limits true   # only on a delegated Linux host
```

Check what holds with `GET /api/shared-crew/capabilities` (what is enforced) and
`GET /api/shared-crew/capacity` (the effective limits, the containment report and the
viewer's usage). A `hard_limits` host that lacks a prerequisite logs
`hard-limit mode is on and containment is unavailable (<reason>)` at start and refuses
every admission and runtime start until it is fixed.

Migration. Nothing is rewritten. Sessions created before Shared Crew read as
`legacy_missing` with an Unknown creator; their messages have no author. New sessions
and messages carry `attribution` and `author` stamps. No creator is backfilled and no
access is inferred from an author.

Rollback. Drain first: list `GET /api/shared-crew/work`, cancel what should not run
(`POST /api/shared-crew/work/{id}/cancel`), and wait until `capacity` shows nothing
active or queued. Then set `admission_enabled` false; new work passes through at once.
Accepted-work records stay in the task store (`tasks.db`), and attribution stays in
session metadata, which older readers carry as unowned keys. Never delete sessions to
roll back.

Diagnosing Unknown attribution. `GET /api/auth/me` answers the caller's `principal`.
`kind: unknown` with provenance `none` means the request did not come through a
verified tailnet path: check `trust_identity`, the caller's login in `allowed_logins`,
that `tailscale serve` (not Funnel or another proxy) fronts the port, and the gateway
log for whois failures. A channel sender is always Unknown in this build.

Quota ownership. `GET /api/shared-crew/capacity` names the limiting scope (`principal`
or `crew`) and the viewer's active and queued counts. `GET /api/shared-crew/people`
shows each observed person's usage when admission is on. Work records carry
`budget_key`, the opaque principal id charged.

Cancellation and recovery. Cancel a queued chat turn from its "Queued" line in the
chat, or any queued or admitted item by the cancel route with its `expected_revision`; a stale revision answers
409 with the current record. A restart reconciles accepted work before admitting new
work (section 4): nothing that may have run is replayed, and a chat turn queued across
a restart ends `cancelled` with its user row in the transcript to resend.

Cleanup. The admission service purges terminal work records from the task store
24 hours after they ended (`IDEMPOTENCY_TTL_SECONDS`), while admission is on.
Presence, observed people and shared decisions are in memory and reset on restart.
Principal slices are runtime-only systemd units and disappear with the user session.

Known limits. Containment is `best_effort`: runtimes share the gateway's uid. IO is not
limited, and disk is accounting of accepted request bytes, not a filesystem quota. A
shared session's runtime stays in its first initiator's slice. Channel senders are
Unknown. The internal one-shot helpers and the CLI's own processes in the table in
section 1 bypass fair admission.

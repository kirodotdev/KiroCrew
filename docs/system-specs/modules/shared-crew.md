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

### Derivation

### Observed collaborators

### People and setup modes

### Access set

### Tailscale invites

## 3. Session attribution and records

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

### Operations

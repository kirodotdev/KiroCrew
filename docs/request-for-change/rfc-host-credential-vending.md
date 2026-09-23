---
title: Host Credential Vending for Enforced Adapters
status: draft
author: zejiangg
created: 2026-09-23
last-audited: 2026-09-23
audited-at: 18f9984b04
doc-pr:
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Host Credential Vending for Enforced Adapters

An enforced adapter's child cannot resolve AWS credentials, because the mask that
confines it hides the files the SDK chain reads. Crew is outside that mask and can
read them. So Crew resolves the credential and vends it to the child at request
time, instead of the child going looking.

---

## Problem

Every turn on the `codex` backend can fail with:

```
stream disconnected before completion: failed to load AWS credentials:
an error occurred while loading credentials
```

`codex` is an enforced adapter, so its spawn carries a credential mask on top of
the sandbox tier. `adapter_hidden_credential_dirs` in `agent_sdk/tool_gate.py`
builds that mask by projecting the whole read-gate floor, `_SENSITIVE_HOME_DIRS`
in `security/paths.py`, which covers `~/.aws` and `~/.ssh`. One file comes back,
read-only: `ADAPTER_EXPOSED_CREDENTIAL_LEAVES` re-exposes `~/.aws/config`.

That leaves the child holding a config file and nothing the config points at:

| what the SDK chain wants | codex child sees |
|---|---|
| `~/.aws/config` | read-only copy |
| `~/.aws/credentials` | hidden |
| `~/.aws/sso` cache | hidden |
| `~/.aws` writable, for a `credential_process` cache | not writable |
| `~/.ssh`, for a certificate-backed helper | hidden |
| `SSH_AUTH_SOCK` | scrubbed unless the owner grants it |

Unenforced backends do not hit this. They carry the tier mask only, and no tier
list contains `.ssh` — see `_STRICT_DIRS`, `_CC_DIRS` and `_STANDARD_DIRS` in
`sandbox.py`.

## Why it matters

The failure is per user and permanent. A `credential_process` line that needs to
write a cache fails on a read-only directory every time, so restarting changes
nothing. There is no setting a user can flip inside the product. The two remedies
that exist today are both a step the user has to know about: point
`AWS_CONFIG_FILE` at a writable directory, which the `CodexHarness.apply_spawn_env`
docstring already names, or write `{"enabled": true}` into the leaf
`ssh_auth_sock_consent.py` reads. Neither is discoverable from the error.

A per-user step is not a fix. Every codex user must work with no setup.

## Design principle

**The child stops resolving credentials. Crew resolves and vends them.**

Crew already runs outside the mask and already reads these files. The mask stays
exactly as tight as it is. Nothing about `~/.aws`, `~/.ssh` or the ssh-agent
changes for the child.

## How it works

Four pieces, each following a shape this repo already has.

**A credential-free leaf.** Crew writes a `0700` directory under the data home
and grants it to this backend only, the way `PI_GATE_ARTIFACT_LEAF` is granted to
`pi` inside `adapter_hidden_credential_dirs`. The leaf is on the read-gate floor
`_CREW_SECRET_LEAVES`, so the agent's own file tools cannot open it while Crew's
writers can.

**A config file, in that leaf.** Its only content is a `credential_process` line
naming the helper below. Crew sets `AWS_CONFIG_FILE` and
`AWS_SHARED_CREDENTIALS_FILE` at it. Neither name matches
`_SPAWN_SCRUB_ENV_PREFIXES`, so both survive the spawn scrub outside a pod;
inside a pod `_apply_pod_home_remap` pops them, so the pod path keeps today's
behaviour and is out of scope here.

**A helper, in that leaf.** A `0700` script Crew writes the way
`_ensure_pi_gate_launcher` writes the pi launcher. The AWS SDK execs it and reads
one JSON object on stdout. The helper asks the gateway and prints the answer.

**A bearer, in the child's env.** A DISTINCT, vend-only bearer — not the session's
stub token. It is minted the same WAY (`secrets.token_hex(32)`, as
`STUB_SESSION_TOKEN_ENV` is) and delivered the same way
(`attach_stub_session_token`'s pattern, an env entry), but it is its own
credential and it authorizes the vend route and nothing else. Reusing the stub
token would make one leaked string both vend credentials and drive every MCP
route the gateway serves. The socket the helper dials is `gateway.sock` under the
directory `runtime_dir` resolves in `mcp_gateway/rewriter.py`, which no mask
covers.

```
codex child  ->  helper (in the granted leaf)  ->  gateway  ->  host credential chain
                     vend-only bearer               outside the mask
```

## What the child can and cannot do

Stated plainly, because the point of the mask is to bound this.

**Cannot:** read `~/.aws`, read `~/.ssh`, or use the operator's ssh-agent. None of
those change.

**Can:** read the vend bearer out of its own environment and ask the gateway for a
credential. So an agent that wants AWS credentials can obtain them.

**And the credential is only as bounded as the host chain makes it.** The gateway
returns what the chain returns. On a host whose chain ends at an SSO or
certificate-backed provider that is a short session credential. On a host with a
static access key in `~/.aws/credentials` or in the environment it is a
non-expiring key, and it stays usable after the session ends. This design does not
bound it, and must not claim to — see Open questions.

| shape | what the child holds |
|---|---|
| un-hide `~/.ssh` | the operator's private keys |
| forward the ssh-agent | use of those keys, whole session |
| static credentials file at spawn | one credential, stale once its TTL passes |
| vending (this) | a freshly resolved credential per request, whatever the chain returns |

The property vending buys is FRESHNESS, not a shorter life: the credential is
resolved at the moment of the request, so it is never the stale one. It is also
the only one of the four where Crew sees each request and can refuse, scope or
record it.

## Alternatives rejected

**Widen the expose list to `~/.ssh`.** Makes codex match the cc tier and needs no
new machinery. Rejected: it hands the child private keys, and the module docstring
of `acp/harness/codex.py` records that trade being made the other way on purpose.

**Forward the ssh-agent by default.** The mechanism exists and is owner-gated for
a reason: the socket grants use of the operator's keys for the whole session, as
`ssh_auth_sock_consent.py` states. A default-on authorization is not an
authorization.

**Write a credentials file outside the mask at spawn.** Simplest, and it is the
remedy the harness docstring already names. Rejected as a default: the credential
expires while the session runs, so the same error returns an hour in — which is
the reported symptom.

**Vend over the ACP connection, as KAS does.** `answer_get_access_token` in
`acp/kas_host_auth.py` already vends a token over the JSON-RPC link. Not
available: `CodexHarness.host_answered_methods` is empty and its `answer_request`
raises.

## Scope

In: the enforced-adapter spawn path, the granted leaf, the helper, the gateway
route, and the env wiring. Keyed on the adapter's ROUTING, not on the name
`codex`, so a later enforced harness inherits it.

Out: the pod path, which scrubs the two pointer variables by design. Out: any
change to the mask, the tier lists, or the ssh-agent consent. Out: anything
naming a specific credential provider — the gateway resolves through the host's
own chain, whatever that is.

## Phases

| phase | ships | exit criteria |
|---|---|---|
| 1 | the granted leaf and its seals, no helper yet | the leaf is spared for this backend, covered for every other, and refused to the agent's file tools |
| 2 | the gateway route, the vend-only bearer, the audit record | the route refuses a missing, wrong, expired and stub-token bearer, and every vend leaves a record |
| 3 | the helper, the config file, the env wiring | a codex session resolves credentials with no operator step, and the pod path is unchanged |

## Open questions

1. **Should the gateway bound what it vends?** Re-minting a shorter-lived
   credential before returning it would make the "short-lived" property true
   instead of incidental. It needs a mint the operator may not permit, and a host
   whose chain is a static key cannot be bounded at all without one. Decide
   before phase 2, because it shapes the route's response.
2. **The route's request and response contract.** Undecided: whether one request
   carries a profile name, and what the refusal shape is.
3. **The audit record's format.** Undecided: where a vend is written and what it
   names, given the record must not carry the credential.

## Risks

**The bearer is in the child's env.** Bounded by the session's lifetime and by the
route's own authority — it is vend-only, so a leaked bearer reaches nothing else.
It does NOT bound the credential that route returns; that is Open question 1.

**A new listener route is a new attack surface.** It is the existing socket, not a
new listener, and the route answers only a request carrying a live vend bearer.

**The helper is an executable the child runs.** Same seals `PI_GATE_ARTIFACT_LEAF`
already carries: membership in `_CREW_PRECREATE_READONLY_DIR_LEAVES` for the
pre-created read-only directory, a no-follow name check, and a tamper reason in
`_DELEGATED_OVERLAP_LEAF_REASONS`.

## Tests

- The granted leaf is spared by the mask for this backend and covered for every other one.
- The agent's file tools are refused on the leaf.
- The two pointer variables reach the child outside a pod, and are absent inside one.
- The route refuses a missing, wrong, expired, and stub-token bearer.
- The helper prints the SDK's expected JSON shape, and a non-zero exit on refusal.

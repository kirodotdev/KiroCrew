---
title: Outbound instance transport — reach a remote crew without SSH or SSM
status: draft
kind: framework
author: Koen Vincent (koenvincent)
created: 2026-09-23
last-audited: 2026-09-23
audited-at: 18f9984b0
doc-pr:
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Outbound instance transport

## Summary

Add a fourth `connection_method` — **`outbound`** — in which the remote crew
dials **out** to a rendezvous service the owner runs, and the local gateway dials
out to the same service. Neither side accepts an inbound connection, neither side
holds a credential for the other's host, and no port-forward child process is
spawned.

Every transport that exists today reaches a peer the same way: spawn a child that
holds open a **local port-forward**, then bootstrap the peer's dashboard token by
**executing a command on the peer's host** over that same credential. That is
true of all three methods on main at `18f9984b0` — `("ssh", "ssm", "fargate")`
(`src/kiro_crew/instances/registry.py`). The consequence is that reachability
is welded to host-level access: to see a peer's dashboard you must also be able
to run a command as a user on its host, and someone must hold the SSH key or the
IAM permission that allows it.

This RFC does not propose a new product surface. It proposes a transport **below
an existing choke point**. Every peer interaction in the tree already funnels
through one method — `SshTunnelManager.proxy_request`
(`src/kiro_crew/instances/ssh_tunnel_manager.py`) — so the dashboard proxy
route, the remote-turn mirror, slot adoption and the capability probe do not need
to know how bytes reach the peer. What a new transport owes them is a small
contract: a reachable local origin, a credential, a status, and a teardown.

It also has a reader for whom this is not an improvement but the only option at
all: an owner whose policy forbids SSH *and* the interactive cloud-session API has
no usable method on main, because `fargate`'s reach is itself an SSM port-forward.
§5 is written for that case and carries the parts a security review asks for — a
fail-closed method allowlist, a pinned rendezvous allowlist, managed-network
egress, provider-side conformity, and an honest account of the recovery path that
removing SSM costs.

## 1. Problem

### 1.1 Reachability and host access are the same permission

An `ssh` instance needs a key that logs a user into the host. An `ssm` instance
needs `ssm:StartSession` plus `ssm:SendCommand`, because the dashboard token is
minted by running `kirocrew token` on the peer through `sudo -u <user> -i`
(`_mint_for`, `src/kiro_crew/instances/ssh_tunnel_manager.py`). In both
cases the permission that makes the pane load is a permission to **execute
code** on someone else's machine. An owner who only wants to see a crew's
dashboard cannot be granted less.

This is also why the transport fails in ways that have nothing to do with Kiro
Crew. `StartSession` is treated by AWS as an interactive human action; a fleet
whose operator profile can read every instance can still be unable to open a
single tunnel, and the failure surfaces as an opaque proxy error rather than as
"you lack one IAM action".

### 1.2 The forwarder is an external child the gateway has to police

Because the forward is a spawned process, a gateway hard-kill can orphan it while
it still holds a loopback port. Recovering from that safely needed its own
mechanism on the `Instance` record — `forwarder_pid`, `forwarder_start` and an
HMAC `forwarder_sig`, so a reclaim is the gateway's own signed claim rather than
a guess from the process table. That machinery is correct, and it exists only
because the transport lives outside the process that depends on it.

### 1.3 A fourth transport by copy-paste would be the wrong shape

`fargate` landed as the third method, and it is honest about being a variant of
the second: its child *is* the SSM port-forward, aimed at an ECS task, and it
answers "no token" where the others mint one. The result is that
`connection_method` is now branched on in `_resolve_transport`
(`src/kiro_crew/instances/ssh_tunnel_manager.py`), in the mint, in connect,
in both self-heal tiers, in diagnostics and in restart. Adding a fourth arm to
each of those chains is how a seam becomes unmaintainable. The refactor in §3.1
is therefore part of this proposal, not a follow-up to it.

### 1.4 What the owner actually asked for

Three properties, in the order they matter:

1. no inbound listener on the remote host, and no SSH key anywhere;
2. no dependency on a cloud provider's interactive-session API for ordinary use;
3. reach that does not imply the right to execute arbitrary code on the peer.

`ssm` already delivers (1). Nothing on main delivers (2) or (3).

## 2. What deliberately does not change

This RFC is small because it changes one layer and leaves four contracts alone.

- **`proxy_request` stays the only way to talk to a peer.** Its callers
  (`api_instances_proxy`, `src/kiro_crew/dashboard/handlers_instances.py`;
  the remote-turn mirror in `src/kiro_crew/dashboard/remote_relay.py`; slot
  adoption in `src/kiro_crew/dashboard/remote_adopt.py`) are not touched.
- **The peer's own token auth stays the authorisation boundary.** An outbound
  transport does not introduce a second way to be authorised to a dashboard; it
  changes only how the token is obtained.
- **The pane keeps loading a loopback origin.** `resolveTunnelOrigin`
  (`website/src/lib/tunnelOrigin.ts`) admits exactly
  `http://127.0.0.1:<port>` for a port with a warm tunnel. Moving the pane to a
  same-origin route is a separate change with its own cookie, sandbox and
  token-pinning consequences; see §6.
- **The registry file format stays additive.** New fields default to empty, and a
  record written before this change loads unchanged.

## 3. Design

### 3.1 Extract the transport seam first (behaviour unchanged but for §3.9)

Introduce a `PeerTransport` protocol in a new module
`src/kiro_crew/instances/transports/` with one implementation per method, and
make `SshTunnelManager` hold a resolved transport instead of re-branching on a
string. The protocol is exactly what the **eighteen** existing branch sites need.
Counted at `18f9984b0` as every comparison of the method string (`==`, `!=`, `in`)
on the gateway side: fifteen in `src/kiro_crew/instances/ssh_tunnel_manager.py`
and three in `src/kiro_crew/instances/registry.py`, spread over record
validation, `_resolve_transport`, the mint, connect, both self-heal tiers,
diagnostics and restart. An earlier revision of this document said eight, which
does not match the code at the revision it claims to audit; the figure above is
measured and re-derivable from that definition.

Those eighteen sites collapse onto six protocol members:

| Member | Answers |
|---|---|
| `validate(inst)` | the per-method field validation, which today is split across `Instance.validate` (`src/kiro_crew/instances/registry.py`, the `ssh` and `fargate` arms plus the method-membership check) and `_resolve_transport` |
| `open(inst, local_port)` | bring up reach; return a handle the manager can poll and kill |
| `mint(inst)` | return a dashboard token, or declare that this method has none (the `fargate` answer) |
| `describe_target()` | the human-facing target string used in messages |
| `diagnose(inst)` | the per-method diagnosis already dispatched per branch |
| `restart(inst)` | remote gateway restart, or a refusal naming why the method cannot |

On the gateway side `ssh`, `ssm` and `fargate` move behind it with no behaviour
change, asserted by the existing tests. The dashboard branches on the same string
independently; that half of the seam is §3.9, and it carries two deliberate
corrections. This lands as its own PR and is separately revertible.

### 3.2 The outbound transport

A new method `outbound` whose reach is a **persistent WSS connection the peer
opens to a rendezvous service** and which the local gateway also connects to.
The service matches the two by instance identity and multiplexes framed streams
between them. It is a rendezvous, not a proxy of arbitrary traffic: it moves
frames belonging to one enrolled pair and can carry no other destination.

New `Instance` fields, all empty by default:

```
hub_url          wss:// endpoint of the rendezvous service
peer_handle      the peer's registered identity at that service
enrolment_ref    opaque reference to the local secret used to authenticate
```

Deliberately **not** on the record: any secret. The record is agent-writable, so
it names a reference and the value lives in the existing secret store.

The word *relay* is avoided in code and field names: `dashboard/remote_relay.py`
already means something else (mirroring a peer's turn into a local transcript),
and reusing the word would make two unrelated concepts grep the same.

### 3.3 Reach: an in-process loopback listener

The local side binds `127.0.0.1:<local_port>` **inside the gateway process** and
tunnels each HTTP request over the outbound channel as frames. The pane origin,
the port allocator and every `proxy_request` caller therefore work unchanged.

This is better than the spawned forwarder in one specific way: the listener dies
with the gateway. There is no orphaned child to reclaim, so an `outbound`
instance never populates `forwarder_pid` / `forwarder_sig`, and the reclaim path
is not extended to a fourth method — it becomes not-applicable for this one.

The listener is loopback-only, and that is not a weakening of the goal. The
objection this RFC answers is the SSH key, the inbound port and the interactive
cloud-session dependency, none of which a same-process loopback socket
reintroduces.

### 3.4 Credential: the mint becomes a channel RPC, not a remote exec

This is the substantive inversion. Today the hub obtains the peer's token by
running a command on the peer. Instead:

1. the local gateway sends a `MINT` frame on the authenticated channel;
2. the peer's own agent calls its **local** mint directly — the same code path
   `kirocrew token` uses — and returns the token over the channel;
3. the local gateway stores it exactly as it stores an SSH- or SSM-minted token,
   and the existing refresh timer keeps working.

No shell, no `sudo -u`, no command string crossing the channel. The peer-side
agent acts on exactly three verbs — *forward an HTTP request to my own loopback
dashboard port*, *mint my own dashboard token*, and *restart my own gateway unit*
— and refuses everything else. None of the three takes a command, a path, a user
or any argument, so there is nothing for a caller to widen.

That refusal has to be stated with its bound, because a reviewer who finds the
bound themselves will discount the whole document. What is removed is
**host-level execution outside Crew's own authorisation**: no shell, no
`SendCommand`, no ability to run something on the peer that Crew itself would not
run. What is *not* removed is the authority a dashboard token already carries — a
token still lets its holder start turns with tool access on that peer, exactly as
it does over `ssh` and `ssm`. The claim is therefore "reach no longer implies a
shell", not "reach implies no capability". Narrowing what a token itself may do is
a different problem and not this RFC's.

### 3.5 Frame protocol

Nine frame types, each carrying instance identity, a correlation id, an expiry and
a verified sender:

| Frame | Direction | Meaning |
|---|---|---|
| `REGISTER` | peer → service | I am this handle, here is my proof, these are my capabilities |
| `HEARTBEAT` | both | liveness plus the value that drives tunnel status |
| `OPEN` / `DATA` / `CLOSE` | both | one proxied HTTP request-response, streamed |
| `MINT` | local → peer | give me a dashboard token for my owner |
| `RESTART` | local → peer | restart your own gateway unit — no arguments of any kind |
| `EVENT` | peer → local | out-of-band peer state the status surface reads |
| `CANCEL` | local → peer | abandon an in-flight stream |

Count the two things separately, because only one of them is a security property.
The **codec** knows nine frame types. The **peer** acts on three verbs:
forward-to-my-own-loopback (`OPEN` / `DATA` / `CLOSE` / `CANCEL`), mint-my-own-token
(`MINT`) and restart-my-own-gateway (`RESTART`). `REGISTER`, `HEARTBEAT` and `EVENT`
are the peer's own outbound bookkeeping and accept nothing inbound. The test in §9
therefore asserts the peer's **inbound dispatch table is exactly those three
verbs** — a codec grows a frame type harmlessly, a peer growing a verb is the
regression worth failing a build over.

There is no generic "run this" frame, and no frame carries a command string, a
filesystem path, a user name or a shell argument. Adding one must be its own RFC.

`RESTART` exists because removing SSM removes the owner's only remote recovery
path, and a transport that can reach a peer but never recover it is not a
replacement (see §5.4). It is safe to add to a "no remote exec" design precisely
because it is parameterless and self-scoped: the peer restarts its own gateway
unit or refuses, and the frame cannot express anything else. Contrast today's
`restart_remote` (`src/kiro_crew/instances/ssh_tunnel_manager.py`), which builds
and dispatches a real command line over the transport's own credential.

### 3.6 Enrolment and identity

An enrolment design that requires **pasting** a code into the peer is circular for
the owner this transport is for: pasting needs a shell on the peer, and a shell on
the peer is the thing the transport exists to remove. So the paste path cannot be
the primary one.

**Primary — credential the peer already carries.** At first start the peer reads
its enrolment secret from its platform's own secret store, authenticating with the
identity the machine or task already has (an instance role, a workload identity, a
managed identity). No operator is on the box, no interactive session is opened, and
nothing is typed. This is the standard first-boot provisioning shape on every cloud
that offers it, and it is the only enrolment path available at all to an owner whose
policy forbids both SSH and the interactive session API.

**Fallback — single-use code.** For a peer the owner can already type on (a laptop,
a container they start by hand) the owner creates a single-use enrolment code
locally and the peer exchanges it once for a long-lived key it stores itself. The
code is then dead: single use, short expiry, invalidated on first exchange.

Either way the peer ends up holding **its own** long-lived key, and revoking one
peer's key must disable exactly that peer and no other.

Where a cloud workload identity already exists, that is the better credential and
this RFC does not re-invent it: it defers to
[rfc-agentcore-identity-gateway.md](rfc-agentcore-identity-gateway.md) and its
`workload` posture. What this RFC owes that document is the enrolment *seam* — the
peer asks a resolver for its credential — rather than a second identity plan.

### 3.7 What the rendezvous service is allowed to be

The service is **bring-your-own**, and the protocol above is the contract. This
RFC specifies the client side that ships in Crew plus a reference deployment in
the guide; it does not propose that the project operate a service for users.

The earlier draft of this section said the service never sees a plaintext token
"where that is avoidable". That hedge does not survive a security review, and it
should not: without sealed payloads the service sees every dashboard request and
the minted token itself, which replaces one tunnel with one centralised
eavesdropping point. There are exactly two honest postures, and an install must
pick one:

**Sealed (default).** Frame payloads are closed between owner and peer. The service
routes on an envelope only — peer handle, correlation id, length, expiry — and
cannot read a body. A dashboard token and a session cookie are therefore never
legible to it, whoever runs it. This is the posture the client ships with.

**In-trust.** An install may decide the service is inside its own trust boundary,
in which case it must run in the owner's own account under the same controls as a
gateway, and the guide must say so in those words. What it may not be is a
third-party host that is trusted implicitly because the document was vague.

Both postures share one absolute rule: the service can never **originate** a
frame. It forwards frames between two enrolled endpoints and is otherwise inert.
A compromised service must be able to deny service and to observe timing, sizes
and peer handles — and must not be able to impersonate either side, read a
payload, mint a token or trigger a restart.

### 3.8 Audit

A transport that carries a credential mint and a restart owes an audit trail, and
a trail that records only successes is not one: it cannot tell "nothing happened"
apart from "we were refused". So the peer appends one **content-free** line per
channel open, channel close, `MINT` and `RESTART`, and also per failure route —
refused `REGISTER`, expired or replayed enrolment, sealing failure, verb refusal.
Each line carries peer handle, correlation id, verb, outcome and timestamp.

It carries no payload, no token, no header and **no request path**: a path would
put the owner's work in an operational log. The trail answers "who reached this
peer, when, and what did they ask for", which is the question a review asks, and
nothing else.

### 3.9 Surfaces that branch on the method string

§3.1 extracts the seam the *gateway* branches on. The dashboard branches on the
same string independently, and it does so with non-exhaustive conditionals whose
final arm is `ssh`. `connectionTypeLabel` and `connectionTypeHint`
(`website/src/pages/settings/RemoteCrewPanel.tsx`) end in the SSH label and the
SSH hint; the card's target line reads `ssm_target` for an SSM-family method and
`ssh_host` otherwise; and the diagnostics handoff passes a `transport` of `ssm`
or `ssh` into `reportInstanceFailure`
(`website/src/utils/instanceFailureReport.ts`), which prints it and selects the
repair steps from it.

An unmapped method is therefore not rendered as unknown. It is rendered as SSH,
with an SSH badge, an SSH explanation, an empty target and an error report
recommending SSH repairs — for a transport whose entire purpose is that no SSH
exists. That is worse than a missing option, because it reads as correct.

This is not hypothetical, and it is not a risk this RFC introduces. The same
pattern already mislabels the transport that landed before this one: the handoff
in `InstancesViewport` compares against `'ssm'` inline instead of using the
shared `usesSsmTransport` helper (`website/src/utils/remoteCrew.ts`), so a
`fargate` crew's failure report says `transport: ssh` on main today. One
non-exhaustive conditional per surface is how a method string acquires four
silent defaults.

So the requirement is a mapping property, not a new label: each of these sites
resolves the method through one exhaustive mapping, and a method with no entry
renders **visibly unmapped** rather than falling through to a sibling's identity.
Gaining a method must then either be a deliberate mapping entry or a visible
gap — never an inherited SSH badge. This belongs in §3.1's refactor, before any
new method exists, because it is the same seam and because fixing it afterwards
means shipping the window first.

It carries **two** intended behaviour changes, both stated rather than absorbed;
everything else in §3.1 is assert-unchanged. The first is the `fargate`
correction above. The second is heavier and was missed by an earlier revision of
this section: `InstanceFormFields.tsx` collapses an unrecognised
`connection_method` to `'ssh'` when it loads a record into the edit form
(`inst.connection_method === 'fargate' ? 'fargate' : inst.connection_method ===
'ssm' ? 'ssm' : 'ssh'`), so opening an unmapped crew and saving it **rewrites the
stored method**. That is not a label falling through to a sibling, it is the
record losing its transport, and it is the reason the mapping has to reach the
form and not only the card.

A mapping entry is not complete until it has strings in every shipped catalog, and
the precedent `fargate` set is **three** strings per catalog, not two. Two of them
follow one per-method family under `pages.settings.remoteCrewPanel`: the badge
label `type_<method>` and the card hint `transport_hint_<method>`, both complete
for `("ssh", "ssm", "fargate")` in all thirteen shipped locale catalogs
(`website/src/i18n/locales/`, which is fourteen files less the `en.manual.json`
overlay). The third does not follow it: the add-and-edit form's method hint
`pages.settings.instancesPanel.fargate_method_hint`
(`website/src/pages/settings/InstanceFormFields.tsx`) sits in a different
namespace under a different naming scheme, and at `18f9984b0` it exists for
`fargate` alone — there is no `ssh_method_hint` and no `ssm_method_hint` anywhere
in the tree, because that selector's other two arms print content-derived keys
(`tunnels_via_aws_ssm_start_session_no_inbound_ssh` and
`opens_ssh_n_l_to_the_host_requires_non_interacti`) instead. There is, in other
words, no `<method>_method_hint` family to extend: `fargate` is the only arm named
after its method. A fourth method therefore needs all three strings, and the form
hint is precisely the one an author following the two-key reading of this
precedent will ship without. A method mapped in code but absent
from twelve catalogs renders raw or falls back to English for the users who
selected those languages, which is the same class of defect one layer down. So
locale parity is a delivery requirement of the method, in the same change that
registers it — not a later translation pass.

## 4. Security model

| Threat | Answer |
|---|---|
| Rendezvous service compromised | It forwards only; it cannot originate frames and cannot mint. Frames are authenticated end to end, so it cannot impersonate the owner to the peer |
| Rendezvous reads a token or a request body | Payloads are sealed between owner and peer and the service routes on the envelope only (§3.7). An install that declines sealing must instead declare the service in-trust and run it itself |
| Peer impersonation | `REGISTER` proof is a per-peer key or workload identity; one revocation disables one peer |
| Channel as a remote-exec surface | The peer acts on three parameterless verbs and no command execution. Asserted by a test that the peer's inbound dispatch table is exactly those three |
| `RESTART` widened into an exec surface | The frame takes no arguments and is scoped to the peer's own gateway unit. No command, path, user or shell argument exists anywhere in the protocol to widen |
| Channel redirected to an attacker's service | `hub_url` lives on an agent-writable record, so the record is not the control: the dial is pinned against a configured allowlist and a record naming an unlisted host fails validation (§5.3) |
| A forbidden transport reintroduced after rollout | `allowed_methods` rejects it at record validation, not at connect time, so a non-compliant instance cannot be created or loaded (§5.2) |
| Token exfiltration | The token is minted by the peer, travels once sealed, and is stored by the local gateway the same way an SSM-minted token is. It is never written to the registry record |
| Enrolment code replay | Single use, short expiry, invalidated on first exchange. The primary path has no code to replay at all (§3.6) |
| Token IP pinning is inert behind a tunnel | Already true of every transport today and correctly diagnosed in [rfc-tailnet-dashboard-access.md](rfc-tailnet-dashboard-access.md); this transport does not make it worse and does not claim to fix it |
| Reach mistaken for zero capability | Explicitly bounded in §3.4: a dashboard token still authorises turns with tool access. What is removed is host-level execution outside Crew's own authorisation |

## 5. Deployment posture where SSH and SSM are forbidden

This is not a hypothetical reader. Some owners are barred by organisational policy
from both SSH and their cloud's interactive-session API. For them **all three
methods on main are unavailable** — including `fargate`, whose reach *is* an SSM
port-forward aimed at a task — so `outbound` is not a preference, it is the only
transport they can run at all.

Two consequences follow, and both change the shape of this proposal rather than
just its wording. Open question 1 is **blocking** for such an owner: a
protocol-only RFC leaves them with nothing to connect to, so "guide-only" is not
an answer they can accept. And the transport must be able to **exclude** the other
methods, not merely coexist with them — a compliant choice that any operator can
undo next week is a statement of intent, not a control.

### 5.1 What this section is for

Everything below is the difference between "we chose the compliant transport" and
"the others cannot be used here". A reviewer under such a policy will ask for the
second. None of it changes the default install: every control here defaults to
today's behaviour and only bites when an owner turns it on.

### 5.2 Fail-closed method allowlist

`InstancesConfig` gains `allowed_methods`, defaulting to every registered method.
`Instance` validation (`__post_init__`, `src/kiro_crew/instances/registry.py`)
rejects a method outside it, so a non-compliant record can be neither created via
the API nor loaded into a live registry — the refusal is at validation, not at
connect, because a record that merely fails to connect still sits in the fleet
looking legitimate. The add-instance form offers only allowed methods, and
`kirocrew doctor` reports the effective posture so an owner can evidence it.

An owner under policy sets `["outbound"]`. The other three then fail closed rather
than sitting unused, and the posture is a config fact a reviewer can read.

**Why this control has no dashboard field, deliberately.** A compliance switch
writable through the same surface it constrains is not a control. The two files
are already in different protection classes, and the asymmetry is what makes the
placement work rather than a preference:
`~/.kiro/crew/config.json` is listed in the write-protected home paths
(`src/kiro_crew/security/paths.py`), so the agent's file-edit tool cannot modify
it; `~/.kiro/crew/instances.json`, which holds the records and therefore
`hub_url`, appears in no security path at all and is agent-writable by design —
registering a crew is ordinary agent work. Putting `allowed_methods` and the hub
allowlist in the config keeps the authorising fact on the protected side of a
boundary that already exists, and needs no new mechanism to hold.

Two honest limits. That protection covers the file-edit tool when the call
declares its edit kind; it does not cover shell writes, so it raises the cost of
a change and does not make one impossible — the audit trail and the posture row
(§5.7) are what make a change *visible*. And an owner editing config by hand is a
worse first-run experience than a form. That is accepted: this control is set once
at fleet setup by the person who owns the policy, not tuned per instance, and a
form for it would move it back to the surface an agent reaches.

### 5.3 Pinned hub allowlist

`hub_url` sits on a record that agents may write. Left unconstrained, an agent
could point a peer's channel at a host of its choosing, which is a worse hole than
the one this RFC closes. So the dial is pinned: config carries the allowlist of
acceptable rendezvous hosts, and a record naming an unlisted host fails validation
in the same place as the method check. The record addresses; the config authorises.

### 5.4 Egress from a managed network

An outbound transport is only compliant if it can actually dial out from the
network such an owner sits in. In practice this, not the protocol, is where
approval is won or lost:

- the WSS dial and its TLS handshake honour the standard HTTP `CONNECT` proxy
  environment, rather than assuming a direct route;
- a corporate CA bundle is accepted **explicitly** by configuration, because
  relying on the ambient trust store is what makes a TLS-inspecting proxy look
  like a protocol failure;
- failure to reach the service is diagnosed as *egress blocked* and distinguished
  from *service down* and *enrolment rejected*, since the three have different
  owners and an opaque proxy error sends the owner to the wrong one.

### 5.5 Provider-side conformity is the owner's, and must be stated

Crew cannot assert that a peer's platform is SSH-free and SSM-free; that is the
owner's infrastructure. What this RFC owes them is the checklist to run in their
own repository, so the claim is measured rather than assumed:

- no key-pair resource in the instance definition, and no public key provisioned
  at first boot;
- no `ssm:StartSession` and no `ssm:SendCommand` on the instance profile or on the
  operator role;
- no inbound security-group rule to the gateway port from anywhere;
- the enrolment secret readable only by the peer's own identity.

These belong in the guide as a conformity checklist with a sample assertion, not
in a Crew unit test that would be asserting facts about someone else's account.

### 5.6 Break-glass: what removing SSM actually costs

SSM is not only a transport, it is also the owner's out-of-band path when the
gateway is broken, and this RFC should not pretend otherwise. `RESTART` over the
channel cannot help when the channel itself is what is broken.

The mitigation is a deployment requirement, not a protocol one: the peer-side
agent must be **supervised independently of the gateway**, as its own unit, so a
crashed, misconfigured or token-broken gateway still leaves the channel up and
`RESTART` reachable. That covers the common failure.

What remains genuinely unrecoverable is a peer whose *agent* fails. For a cloud
peer the honest answer is to replace the instance rather than reach into it, which
is the shape a policy forbidding interactive access is asking for anyway; for a
laptop peer the owner is physically at the machine. This is a real cost of the
policy, it belongs in the guide, and an owner should accept it deliberately rather
than discover it during an incident.

### 5.7 The posture must be visible where a reviewer looks

`kirocrew doctor` proving the stance is necessary and not sufficient. A CLI
invocation is something the owner runs and then retells; a review asks to see the
control. The dashboard already has the surface for exactly this shape of fact:
Settings → Security renders a row per control from `GET /api/security/posture`,
where a count expands inline into the concrete list behind it, and a control that
could not be resolved renders as an explicit warning instead of as `0` — so an
operator is never told a control covers nothing when it may well be active.

The effective transport posture is that shape of fact, so it is registered as a
control rather than written as prose: an entry in `_CONTROLS`
(`src/kiro_crew/security_posture.py`) whose items are the methods
`allowed_methods` currently permits. `SecurityPanel` already states the dividing
line this follows — every control whose posture is a count belongs in the
registry, and the panel itself carries only qualitative descriptions.

Two properties make this cheap enough to ship with the enforcement rather than
after it. It is **backend-only**: the panel's icon map falls back to a generic
shield for a key it does not know, precisely so a new backend control is never
silently dropped for want of a frontend change. And it needs no new copy
decisions, because label, unit and summary come from the server with the control.

The one thing to get right is the unresolvable case. The count must be the
*effective* posture, and when it cannot be resolved it must be reported as
unresolved rather than as the permissive default — a posture row that answers
"all four methods" because it failed to read the config would be worse than no row
at all.

## 6. Non-goals

Named explicitly, because each one is live work by someone else and this document
must not read as a competing proposal:

- **Remote subagent placement.** `executor="remote"` and its shadow records are
  [#12821](https://github.com/kirodotdev/KiroCrew/pull/12821). That PR is about
  *where a run executes*; this RFC is about *how the peer is reached*. It is a
  consumer of this transport, not part of it.
- **A compute backend.** [rfc-remote-instance-on-fargate.md](rfc-remote-instance-on-fargate.md)
  changes what sits at the far end. Its own §Summary keeps the tunnel surface as
  it is; an `outbound` task is a later composition of the two, not a claim here.
- **Private-network access.** VPC peering, PrivateLink and tailnet routes are the
  other way to remove a forward, and the tailnet case is already documented in
  [rfc-tailnet-dashboard-access.md](rfc-tailnet-dashboard-access.md). They need a
  network the owner controls on both ends; this transport does not.
- **A same-origin pane.** Serving a peer's dashboard from the gateway's own
  origin is adjacent to
  [#12229](https://github.com/kirodotdev/KiroCrew/pull/12229) and would let the
  loopback listener be deleted. Out of scope; §3.3 is written so that change
  stays possible.

## 7. Alternatives considered

**Direct HTTPS with mTLS per crew.** Smallest code change — a `base_url` instead
of a forward — but it requires every crew to be publicly resolvable and to
terminate TLS, which puts an inbound listener back on the remote host. That is
the property the owner most wanted gone.

**Private network route.** Correct where it is available, and it needs no Crew
change at all beyond documentation. Rejected as the primary answer because it
requires controlling the network on both ends, which a laptop plus a rented
instance does not.

**Central control-plane API with an event bus.** Architecturally the cleanest
end state and the largest product change: no peer dashboards at all, crews
publishing status and consuming commands. Rejected as a first step because it
cannot be delivered incrementally behind an existing choke point, and because it
would obsolete the pane rather than serve it.

**A fourth arm on each existing branch.** Rejected in §1.3.

## 8. Rollout

Six PRs, each independently revertible. The full task list is
[plans/2026-09-23-outbound-instance-transport.md](plans/2026-09-23-outbound-instance-transport.md).

1. **This document.**
2. **Seam extraction** (§3.1) — refactor; existing tests unchanged, plus the
   exhaustive surface mapping of §3.9 and its two intended corrections (the
   `fargate` diagnostics label, and the edit form no longer rewriting an unmapped
   method to `ssh`).
3. **Outbound client** (§3.2–3.5) — registry fields, config section, listener,
   frames, status, mint RPC, teardown. Carries the posture controls with it: the
   `allowed_methods` gate (§5.2), the pinned hub allowlist (§5.3), proxy plus
   CA-bundle egress (§5.4) and the posture row (§5.7), because a transport that
   ships before its controls invites exactly the record it is meant to refuse. It
   also carries the method's locale strings, so a record that can exist can be
   labelled. Default off; no add-instance option yet.
4. **Peer-side agent** (§3.4) — the three-verb dispatch and its refusal test, the
   `RESTART` verb, the audit trail (§3.8), and independent supervision (§5.6).
5. **Reference service and guide** (§3.7) — **not optional** for an owner under a
   no-SSH, no-SSM policy: without a service they have no transport at all. The
   guide carries the enrolment, revocation, conformity checklist (§5.5) and the
   break-glass cost (§5.6).
6. **Owner-facing entry and diagnosis** — the add-instance option and its
   method-filtered form, `kirocrew doctor` checks, and per-method diagnosis. What
   is deliberately *not* here: nothing a phase-3 record needs in order to render
   correctly, which is why §3.9, the locale strings and §5.7 sit earlier.

Reversal for 3–6 is the config default: with the method unused, the transport
registry holds one more entry and nothing else changes.

## 9. Acceptance criteria

A phase is done when these are measured, not argued:

- the remote host has zero inbound security-group rules and no SSH key;
- no `ssh`, `session-manager-plugin` or `aws ssm` process is spawned for an
  `outbound` instance, asserted by a test on the spawn surface;
- the pane loads, a turn runs and its result streams back, over the same
  `proxy_request` path the other methods use;
- reconnect survives a peer restart and a network drop without owner action;
- revoking one peer's key disables exactly that peer;
- the peer's inbound dispatch table is exactly the three verbs of §3.5, asserted
  by a test, and no frame in the codec carries a command, path, user or argument;
- a method outside `allowed_methods` is refused at `Instance` validation and by
  the add-instance API, asserted by a test — not merely absent from the UI;
- a `hub_url` outside the configured allowlist fails validation, asserted by a
  test, so an agent-written record cannot redirect a channel;
- the channel dials out through a configured `CONNECT` proxy and trusts a supplied
  CA bundle, asserted by a test that neither is silently ignored;
- egress-blocked, service-down and enrolment-rejected are three distinct
  diagnoses, asserted by a test on the diagnosis output;
- every channel open, channel close, `MINT` and `RESTART` writes exactly one audit
  line, and so does each refusal route, asserted by a test that the trail is
  content-free (no token, no header, no request path);
- the peer-side agent answers `RESTART` while its gateway is stopped, proving the
  supervision requirement of §5.6;
- an enrolment completes with no shell, no interactive session and no keystroke on
  the peer, using only the identity the peer already carries (§3.6);
- every method-dependent dashboard surface of §3.9 resolves through one exhaustive
  mapping, asserted by a test that an unmapped method renders as unmapped and
  acquires no sibling's badge, hint, target field or repair steps — with the
  `fargate` diagnostics report naming `fargate` as the regression test of record;
- the badge label and hint of every registered method exist in every shipped
  locale catalog, asserted by a test rather than by review;
- the effective method posture appears as a row in the security-posture registry
  whose items are the permitted methods, and reports itself unresolved rather than
  permissive when it cannot be determined (§5.7);
- `ssh`, `ssm` and `fargate` behaviour is byte-identical after §3.1, asserted by
  the pre-existing tests with no edits, the single exception being the `fargate`
  diagnostics label corrected in §3.9.

## 10. Open questions

1. **Does the reference service ship in-tree?** A client-only RFC with a documented
   protocol is honest for an owner who can fall back to `ssm`. It is not an option
   for an owner forbidden both SSH and SSM (§5): they would have no transport at
   all, so for them phase 5 is mandatory and "guide-only" is not on the table.
   Remaining options: in-tree reference, or an app.
2. **Heartbeat to status mapping.** The existing `TunnelStatus` vocabulary was
   written for a process that either runs or exits. A channel can be connected
   but stale, and it is not yet decided whether that is a new state or an error.
3. **Which sealing primitive.** §3.7 requires payloads sealed between owner and
   peer but deliberately does not invent the construction. The candidates are
   reusing the enrolment key for an authenticated channel under it, or deferring to
   the identity plane in
   [rfc-agentcore-identity-gateway.md](rfc-agentcore-identity-gateway.md). This
   needs a reviewer with a cryptography opinion, not a decision taken here.
4. **Multiple owners per peer.** Out of scope as written (one enrolment, one
   owner). Whether that stays true affects the `REGISTER` proof.

Decided since the first draft, recorded so it is not reopened: `outbound` needs no
`remote_bin`. The mint is an RPC and the restart is a parameterless frame, so no
path to a remote binary ever crosses the channel — which is also why that field
stays method-specific rather than becoming shared.

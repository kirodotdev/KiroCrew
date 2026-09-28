---
title: Host Credential Vending for Masked Credentials
status: draft
author: zejiangg
created: 2026-09-23
last-audited: 2026-09-24
audited-at: 3d2bfa8f45
doc-pr:
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Host Credential Vending for Masked Credentials

A sandboxed child cannot use a credential the mask hides from it. Crew is outside
the mask and can. So Crew resolves the credential and vends it to the child at
request time, instead of the child going looking — and instead of the user going
around the mask.

Two credentials are vended: AWS credentials, through a `credential_process`
helper, and SSH signatures, through an ssh-agent proxy. The grant is keyed on
**what the child's mask hides**, not on which harness the child runs.

---

## Problem

A session whose mask hides `~/.aws` fails every turn with:

```
stream disconnected before completion: failed to load AWS credentials:
an error occurred while loading credentials
```

A session whose mask hides `~/.ssh` cannot `git fetch` over SSH.

Two masks produce those holes, and they reach different children:

| child | tier | `~/.aws` | `~/.ssh` |
|---|---|---|---|
| any backend | `standard` | visible | visible |
| any backend | `cc` | hidden, `config` re-exposed (Linux); visible (macOS) | visible |
| any backend | `strict` | hidden | hidden |
| `codex` | every tier | hidden, `config` re-exposed | hidden |
| `opencode`, `goose`, `pi`, `deepseek` | every tier | hidden | hidden |

The tier rows come from `_STANDARD_DIRS`, `_CC_DIRS` and `_STRICT_DIRS` in
`sandbox.py`: both of the tighter lists carry `.aws`. `~/.ssh` is hidden by a
separate `hide_ssh` switch in the launcher builders, true only for `strict`, which
leaves `known_hosts` readable. The `cc` cell is split because
`_build_seatbelt_profile` drops `.aws` from the `cc` list on purpose — Seatbelt
cannot do the partial exposure the Linux bind does — so a macOS `cc` child has
no `.aws` hole and gets no grant. The last row is the adapter mask. A harness whose
routing is in `ENFORCED_ROUTINGS` (`agent_sdk/tool_gate.py`) is one whose passive
reads never reach the tool gate, so `adapter_hidden_credential_dirs` projects the
whole `_SENSITIVE_HOME_DIRS` floor onto its child. `ACP_BACKEND_ROUTING` in
`agent_sdk/backends.py` puts five harnesses there today. One file comes back for
`codex`, read-only: `ADAPTER_EXPOSED_CREDENTIAL_LEAVES` re-exposes `~/.aws/config`.

That leaves the child holding a config file and nothing the config points at:

| what the credential chain wants | masked child sees |
|---|---|
| `~/.aws/config` | read-only copy (`cc`, `codex`) or hidden (`strict`) |
| `~/.aws/credentials` | hidden |
| `~/.aws/sso` cache | hidden |
| `~/.aws` writable, for a `credential_process` cache | not writable |
| `~/.ssh`, for a key or a certificate-backed helper | hidden |
| `SSH_AUTH_SOCK` | scrubbed unless the owner grants it |

## Why it matters

The failure is per user and permanent. A `credential_process` line that needs a
writable cache fails on a read-only directory every time. There is no setting a
user can flip inside the product. The two remedies that exist today are both a step
the user has to know about: point `AWS_CONFIG_FILE` at a writable directory, which
the `CodexHarness.apply_spawn_env` docstring names, or write `{"enabled": true}`
into the leaf `ssh_auth_sock_consent.py` reads. Neither is discoverable from the
error.

**And a mask that only hides files does not stop credential use. It moves it.** A
user hit the `~/.ssh` hole and routed around it: a `GIT_SSH_COMMAND` wrapper,
stored in the workspace, that mints an SSH ticket from a service and hands it to
`ssh`. The mask held. The credential moved from a directory the agent cannot read
or write to a script the agent can read and write, with no record of any use.
That is the posture the mask exists to prevent, reached because the mask offered
no sanctioned path.

A per-user step is not a fix. Every user whose mask hides a credential must work
with no setup — or they will build the shadow path above. If Decisions needed 1
rules opt-in anyway, the failure itself must name the switch: the
`failed to load AWS credentials` and SSH auth errors a masked child surfaces are
the only place a user looks, so the sanctioned path has to be printed there.

## Design principle

**The child stops resolving credentials. Crew resolves and vends them, and records
each vend.**

Crew already runs outside the mask. The mask stays exactly as tight as it is.
Nothing about `~/.aws`, `~/.ssh` or the operator's ssh-agent changes for the child.
What changes is that the child has one sanctioned place to ask.

## How it works

Five pieces, each following a shape this repo already has.

**A credential-free leaf, one subdirectory per grant.** A grant is one session,
or one runtime on `codex`, where every session shares a process (see The bearer).
Crew writes a `0700`
directory under the data home and, beneath it, one `0700` subdirectory per
session. The PARENT stays masked for every child; a child whose mask hides at
least one vended credential gets ITS OWN subdirectory opened, and only that one.
The primitive is `extra_private_dirs`, the per-spawn window the launcher already
opens inside a bind-masked parent (`PRIVATE_DIRS` in the Linux launcher, passed
on this spawn path for the scratch root). It is NOT the shape of
`ADAPTER_EXPOSED_CREDENTIAL_LEAVES`, which restores a pre-read `0444` copy of one
file and cannot yield a directory or a socket, and NOT the gate-artifact spare,
which `sandbox_credential_targets` can only apply to a whole leaf. The grant
predicate follows the way `PI_GATE_ARTIFACT_LEAF` is granted — per ROUTING and
per effective mask, never per backend id. Because the parent is bind-masked, a
sibling grant's child cannot list it, so it cannot find another grant's
socket or helper even though both run as the same uid; that isolation comes
from the mask, not from file mode or a secret name. `extra_private_dirs` is a
primitive of both POSIX builders: the Linux launcher binds the window, and
`_build_seatbelt_profile` carves it out of every masked tree through
`_private_window_spellings`. Windows has no bind mask at all — see Scope.
The leaf is on the read-gate floor `_CREW_SECRET_LEAVES`, so the agent's own file
tools cannot open it while Crew's writers can. The grant predicate reads the
child's EFFECTIVE mask — the tier list plus the adapter mask, minus what
`adapter_expose_files` hands back from `ADAPTER_EXPOSED_CREDENTIAL_LEAVES` — and
asks whether `.aws` or `.ssh` is in it.
Every harness in `ENFORCED_ROUTINGS` therefore qualifies by construction, on every
tier, and so does every harness on `strict`, and on `cc` where the launcher hides
`.aws` (Linux, not macOS) — with one exception the predicate must honour. The mask it reads is the one the launcher ACTUALLY
applied, not the tier constants: a delegated `kiro-cli` spawn (`delegate_to_kiro`
in `sandbox.py`, unconditional on Windows for a positively classified kiro
backend and on macOS when `kiro_internal_sandbox_enabled()` is true) goes to
`_delegate_to_kiro_internal_sandbox`, which applies the env scrub and no tier
directory mask, so that child reads the real `~/.aws` and `~/.ssh` and must get
no grant. That is the intent: the grant follows the hole, and only the hole.

Two sockets carry the two vends, and they are shaped differently because their
callers are: the AWS helper can present a bearer, `ssh` cannot.

**An AWS vend listener, shared, bearer-authenticated.** A NEW Crew-owned listener
under the directory `runtime_dir` resolves in `mcp_gateway/rewriter.py`, which no
mask covers: an `AF_UNIX` socket, started with the gateway whether or not anything
else is. The socket file is `0600` inside a `0700` directory. There is no Windows
listener, because no masked child exists there (see Scope). It is not
`gateway.sock`: that socket is only bound when `stub_servers` is non-empty (the
`if not stubs: return` in `_init_mcp_gateway` precedes the sole `GatewayManager`
construction, and `stub_servers` defaults to `[]`), so on a default install there
is no existing socket to reuse. This listener serves the AWS route and nothing
else. A request carrying a live vend bearer gets back one JSON object in the
`credential_process` shape, resolved through the host's own credential chain —
whatever provider that chain names. Every session reaches the same listener; the
bearer says which session is asking.

**An ssh-agent proxy socket, per grant, inside the granted leaf.** Not on the
shared listener. Crew binds one proxy socket per grant inside that grant's
own subdirectory of the leaf, so the mask that isolates the leaf isolates the
socket. It speaks the ssh-agent protocol and answers `REQUEST_IDENTITIES` and
`SIGN_REQUEST` by forwarding them
to the operator's own agent — the `SSH_AUTH_SOCK` Crew's process holds — and
refuses every other message: no add, no remove, no lock, no extension. The private
key never leaves the operator's agent; the child gets signatures, one at a time,
each one recorded. When Crew holds no agent socket the proxy answers an empty
identity list, and the child's `ssh` fails exactly as it would on a host with no
agent.

The ssh-agent protocol has no field for a bearer, and `ssh` would not send one, so
the only credential the proxy leg can carry is the socket PATH — which is why the
proxy is not on the shared listener. The socket is created at spawn and removed at
teardown; only that child's env names it, and only that child's mask exposes the
directory it sits in. State the bound plainly: file mode is not one. The child
runs as Crew's uid, so `0600` excludes other users and nothing else — the same
reason `_STRICT_DIRS` bind-masks `.vault` instead of trusting its mode. What
bounds the proxy is the mask over the leaf's parent (a sibling grant's child
cannot reach or list another grant's socket), that the socket is short-lived,
that it answers read and sign only, and that every signature is recorded against
the session whose directory it came through. That is what ssh-agent's own socket
offers today, minus mutation, plus a record, per grant. It is also default-on
where the raw socket is consent-gated, which is Decisions needed 1. The bearer
below belongs to the AWS route only.

**A helper and two env entries, for the child.** A `0700` helper script in a
Crew-owned read-only directory beside `pi-gate`, written the way
`_ensure_pi_gate_launcher` writes the pi launcher, that dials the AWS route and
prints the answer; a config file in the granted leaf whose only content is a
`credential_process` line naming the helper; and in the child's
process env: `AWS_CONFIG_FILE` and `AWS_SHARED_CREDENTIALS_FILE` pointing at that
config, `SSH_AUTH_SOCK` pointing at the proxy socket, and the vend bearer.

Two rules about that env, because each reverses a documented default if stated
carelessly:

- `AWS_CONFIG_FILE` / `AWS_SHARED_CREDENTIALS_FILE` are set **only when the
  operator has not set them**. `CodexHarness.apply_spawn_env` leaves both "exactly
  as the operator set them" on purpose, and this design keeps that: an
  operator-set value wins, and the helper is the default beneath it.
  `AWS_PROFILE` is neither scrubbed (the only `AWS_` prefixes in
  `_SPAWN_SCRUB_ENV_PREFIXES` are `AWS_SECRET` and `AWS_SESSION`) nor a pointer
  variable, so an operator-exported
  `AWS_PROFILE=work` reaches the child and selects a section the generated
  config would not have. The wiring therefore writes the `credential_process`
  line under `[default]` AND under `[profile <name>]` for the `AWS_PROFILE` value
  it sees, so the operator's profile selection keeps resolving.
- `SSH_AUTH_SOCK` is scrubbed TWICE today, and both scrubs must let the proxy
  path through. Both take the one boolean `_forward_ssh_auth_sock` resolves. The
  parent-side scrub (`scrub_agent_subprocess_env`) lets `scrub_env` drop the key
  unconditionally and re-adds the value only when the boolean is true; the
  launcher-side scrub — the Linux launcher's in-namespace unset loop and the
  macOS seatbelt's `env -u` list — drops `"SSH_AUTH_SOCK"` from its prefix list
  through `_agent_scrub_prefixes` only when it is true. The proxy therefore needs that boolean (or a
  sibling "proxy active" flag feeding `_agent_scrub_prefixes`) to be true
  whenever the proxy is set, with the VALUE replaced by the proxy path before
  the launcher runs; setting the variable after the parent scrub alone leaves
  the launcher to delete it inside the sandbox. When the owner has written the
  `ssh_auth_sock_consent` leaf, the raw operator socket is kept as today and
  the proxy is not set: an explicit owner grant outranks the default.
- Windows: Win32 OpenSSH's agent is a fixed named pipe and `ssh` there does not
  read `SSH_AUTH_SOCK`, so the proxy cannot be selected by env. SSH vending is
  POSIX-only in phase 3; Windows is Open question 4.

Neither pointer variable matches `_SPAWN_SCRUB_ENV_PREFIXES`, so both survive the
spawn scrub. Inside a pod one path does pop them: `_apply_pod_home_remap` removes
every `CREDENTIAL_POINTER_ENV_VARS` entry for a harness in
`ACP_BACKENDS_POD_HOME_REMAP` — today only `kiro`, which is masked on `cc` and
`strict` and so qualifies here. The vend wiring is therefore installed AFTER that
remap runs, never before it, so the pointers it sets are the ones the child sees.
That REVERSES, on purpose, the cost `_apply_pod_home_remap` records for a pod
`kiro` child ("an ACP agent turn inside a pod has no inherited AWS credentials
on any path"): the child gets credentials again, but vended and recorded, not
inherited. The leak that pin was removed for does not return, and not because
of where the file sits — the grant leaf is fenced too, so the exported pointer is
again an alias for a refused path. It does not return because of what the file
HOLDS: the pinned paths were the operator's own credential files, whose contents
are the secret; the vend config holds one `credential_process` line naming a
helper the child may already run, and the bearer that helper presents is already
in the child's own env. Reading the file through the alias grants nothing the
child does not hold. Phase 3 rewrites that docstring to say so.
A pod gateway has its own `runtime_dir`, so it vends from its own listener.

**The bearer, for the AWS route.** A DISTINCT, vend-only bearer — not the session's stub token. It is
minted the same way (`secrets.token_hex(32)`, as `STUB_SESSION_TOKEN_ENV` is) and
placed in the child's own process env, because the helper the SDK execs is the
reader. One consequence must be stated: the `codex` adapter multiplexes every
session onto ONE process (`ACP_BACKENDS_ACP_RUNTIME`), and a process has one env.
So on `codex` the bearer, the leaf subdirectory and the proxy socket are per
RUNTIME, shared by every session on it, and the vend record names the runtime.
On the `AcpClient`-served harnesses (`opencode`, `goose`, `pi`, `deepseek`) each
session is its own process and the grant is per session as described. Where the stub token lives depends on the harness: for `codex`
(`ACP_BACKENDS_ACP_RUNTIME`) `attach_stub_session_token` writes it into a
per-session MCP element's `env` array that only that MCP server sees; for the
`AcpClient`-served masked harnesses (`opencode`, `goose`, `pi`, `deepseek`)
`_apply_session_identity_env` puts it in the child's process env already. So on
four of the five masked harnesses one env read already yields a token that drives
every MCP route the gateway serves. That is a further reason the vend bearer must
be its own string: it authorizes the AWS route and nothing else, and a leak of
either token must not widen into the other's authority.

```
masked child ─┬─ AWS SDK ─> helper (read-only dir) ──bearer──> AWS vend listener ──> host credential chain
              │            config (grant leaf)                   (runtime_dir, shared)
              └─ ssh ─────> SSH_AUTH_SOCK = proxy socket (grant leaf) ──────────> operator's ssh-agent
                              (grant leaf = one per session; one per runtime on codex;
                               masked from every other grant)      every vend recorded
```

## What the child can and cannot do

Stated plainly, because the point of the mask is to bound this.

**Cannot:** read `~/.aws`, read `~/.ssh`, add or remove keys in the operator's
agent, or hold a private key. None of those change.

**Can:** read the vend bearer out of its own environment, ask for an AWS credential,
and ask for an SSH signature. So an agent that wants either can obtain it — through
a path Crew sees, can refuse, and records.

**And the AWS credential is only as bounded as the host chain makes it.** The route
returns what the chain returns. On a host whose chain ends at an SSO or
certificate-backed provider that is a short session credential. On a host with a
static access key in `~/.aws/credentials` or in the environment it is a
non-expiring key, and it stays usable after the session ends. This design does not
bound it, and must not claim to — see Decisions needed.

| shape | what the child holds |
|---|---|
| un-hide `~/.ssh` | the operator's private keys |
| forward the raw ssh-agent | use of every key, every operation, whole session, no record |
| static credentials file at spawn | one credential, stale once its TTL passes |
| a user's own workaround | a minting script the agent can read and edit, no record |
| vending (this) | a freshly resolved credential or one signature per request, whatever the chain returns, recorded |

The property vending buys is FRESHNESS and a RECORD, not a shorter life: the
credential is resolved at the moment of the request, so it is never the stale one,
and Crew sees each request. Compared with the fourth row — which is what happens
today when the mask blocks a user — every other row is an improvement.

## Alternatives rejected

**Widen the expose list to `~/.ssh`.** Needs no new machinery. Rejected: it hands
the child private keys, and the `apply_spawn_env` docstring in
`acp/harness/codex.py` refuses the same widening for `~/.aws` on purpose: un-hiding
the real credential tree weakens the mask for every session.

**Forward the raw ssh-agent by default.** The mechanism exists and is owner-gated
for a reason: the socket grants use of every key for every operation for the whole
session, as `ssh_auth_sock_consent.py` states, and nothing records a signature. A
default-on authorization is not an authorization. The proxy keeps the owner grant
as the raw path and offers a narrower, recorded default beneath it.

**Write a credentials file outside the mask at spawn, refreshed before TTL.**
Simplest, and it is the remedy the harness docstring names. A Crew-side refresh
before expiry removes the staleness objection. Rejected anyway: it does nothing
for SSH, it leaves a credential at rest on disk for the whole session, and Crew
never sees a use, so it cannot refuse or record one. Per-request refusal and audit
is the property this design is for; a refreshed file has neither.

**Vend over the ACP connection, as KAS does.** `answer_get_access_token` in
`acp/kas_host_auth.py` already vends a token over the JSON-RPC link. Not
available: `CodexHarness.host_answered_methods` is empty and its `answer_request`
raises, and the `ssh` binary the child runs speaks no ACP.

**Block the workaround.** Refuse `GIT_SSH_COMMAND` or scan the workspace for
minting scripts. Rejected: an enforced harness's exec is exactly what Crew cannot
observe, so the block cannot be enforced where it matters, and a blocked
workaround without a sanctioned path produces the next workaround.

## Scope

In: the masked-child spawn path, the grant predicate over the mask the launcher
actually applied, the granted leaf, the shared AWS vend listener, the
per-session (per-runtime on `codex`) ssh-agent proxy socket, the helper, the env
wiring, and the vend record. Pods included — the wiring is installed after the
pod remap. Platforms: Linux and macOS for both routes — `_build_seatbelt_profile`
carves the per-spawn window the leaf needs. Windows for neither: `detect_backend`
answers `"none"` there, so `credential_mask_applies` is false, an enforced adapter
is refused and a kiro spawn is delegated unmasked; no masked child exists to grant.

Out: any change to the mask, the tier lists, the expose list, or the raw
`ssh_auth_sock_consent` grant. Out: anything naming a specific credential provider
— the AWS route resolves through the host's own chain and the proxy forwards to
the host's own agent, whatever either is. Out: loading key files from `~/.ssh` into
the proxy when the operator runs no agent — see Decisions needed.

## Phases

| phase | ships | exit criteria |
|---|---|---|
| 1 | the grant predicate, the granted leaf (one per session, one per runtime on `codex`) and its seals, no helper yet | a child whose effective mask hides `.aws` or `.ssh` sees its own subdirectory and not the parent or any other grant's; every other child sees none of it; the agent's file tools are refused on all of it |
| 2 | the vend listener, the AWS route, the ssh-agent proxy, the vend-only bearer, the vend record | the listener binds on a default install; the AWS route refuses a missing, wrong, expired and stub-token bearer; the proxy refuses every non-read, non-sign message and its socket does not outlive the session; every vend leaves a record |
| 3 | the helper, the config file, the env wiring, the `_apply_pod_home_remap` docstring | a masked session resolves AWS credentials and signs over SSH on Linux and macOS, with no operator step; the proxy path survives both scrubs; an operator-set `AWS_CONFIG_FILE` still wins; an operator-set `AWS_PROFILE` still resolves; a granted consent leaf still forwards the raw socket; a delegated `kiro-cli` spawn gets no grant |

## Decisions needed

Each is a posture ruling, not a code shape. Phase 2 waits on all four.

1. **Default-on vending.** The mask exists so a third-party binary cannot reach the
   operator's credential homes, and this design vends to that binary with no
   operator step. The evidence for default-on is the workaround above: with no
   sanctioned path the credential ends up somewhere worse. An opt-in reintroduces
   the per-user step this document exists to remove. Needs the security owner's
   recorded acceptance, or a ruling that this ships opt-in and the step stays.
2. **A static long-lived key.** When the host chain resolves to a non-expiring
   key, vending it is equivalent to handing over the credentials file. The AWS
   route must do one of: vend unchanged, vend with a warning in the record, or
   refuse. Re-minting a shorter credential needs a mint the operator may not have.
3. **No agent, keys on disk.** When Crew holds no `SSH_AUTH_SOCK`, should the proxy
   load the operator's `~/.ssh` keys itself? It would make Crew, not the agent,
   the holder of key material — a wider role than forwarding. Default in this
   document: no; the proxy answers an empty list.
4. **Is vending governed by the ceiling?** Both vend paths sit beside the
   PreToolUse gate, so the enterprise tightest-wins ceiling that governs every
   other agent capability has no hook over them as drafted. Either vending is a
   governed scope the ceiling can deny, or it is deliberately keystone-only like
   `computer_use.json`, with the consent leaf as the sole off switch. Decide
   before phase 2; a governed scope changes the route's refusal path.

## Open questions

1. **The AWS route's request and response contract.** Whether one request carries
   a profile name, and what the refusal shape is.
2. **The vend record's format.** Where a vend is written and what it names, given
   the record must not carry the credential or the signature.
3. **Proxy scope.** Whether the proxy can restrict signing to keys whose comment
   or certificate principal names a git host, and whether that is worth the
   false refusals.
4. **SSH on Windows.** Moot until a masked child exists there (Scope). If one does,
   Win32 OpenSSH selects its agent by a fixed named pipe, not `SSH_AUTH_SOCK`, and
   whether Crew can stand in for that pipe per session is undecided.

## Risks

**The bearer is in the child's env.** Bounded by the grant's lifetime (the runtime's,
on `codex`) and by the
listener's own authority — it is vend-only, so a leaked bearer reaches the AWS
route and nothing else. It does NOT bound the credential the AWS route returns;
that is Decisions needed 2.

**A new listener is new attack surface.** It is new, and this document says so
rather than claiming reuse. The shared AWS listener is bounded by ordinary
permissions against other users (`0600` in a `0700` directory) and by a bearer on
every request against a same-uid caller;
it serves one route. The per-grant proxy socket is bounded by the mask over the
leaf's parent — the only thing that isolates one grant's socket from another's
— and by refusing every mutating message.

**The helper is an executable the child runs.** It lives in its own pre-created
read-only directory, not in the granted leaf: the leaf is the child's read-write
window, and a read-only seal cannot sit inside it. The directory takes the seals
`PI_GATE_ARTIFACT_LEAF` already carries: membership in
`_CREW_PRECREATE_READONLY_DIR_LEAVES`, a no-follow name check, and a tamper reason
in `_DELEGATED_OVERLAP_LEAF_REASONS`. Only the config file, which names the helper
by absolute path, sits in the leaf.

**The proxy signs on the child's behalf.** A prompt-injected child can request a
signature for any host the operator's agent can sign for. That is the same power
the raw-socket consent grants today, narrowed to read-and-sign and recorded. Open
question 3 is the lever to narrow it further.

## Tests

- The grant predicate re-exposes a child's own subdirectory when its effective
  mask hides `.aws` or `.ssh`, on every tier and for each of the five harnesses in
  `ENFORCED_ROUTINGS` (`codex`, `opencode`, `goose`, `pi`, `deepseek`), keeps the
  parent and every sibling subdirectory masked, and exposes nothing to a
  `standard`-tier unenforced child.
- With two masked sessions live on separate processes, each child can open its
  own socket and cannot list or open the other's; on `codex`, where both share
  one runtime, they share one grant and the test below applies instead.
- The agent's file tools are refused on the leaf.
- The listener binds on a default install with `stub_servers` empty.
- The AWS route refuses a missing, wrong, expired, and stub-token bearer.
- The proxy forwards `REQUEST_IDENTITIES` and `SIGN_REQUEST`, refuses every other
  message type, answers an empty list when Crew holds no agent socket, and its
  socket is gone after teardown.
- The pointer variables reach the child outside a pod and inside one — including a
  `kiro` child, whose pod remap runs first; an operator-set `AWS_CONFIG_FILE` is
  left untouched.
- With the consent leaf granted, the child's `SSH_AUTH_SOCK` is the raw operator
  socket; without it, the proxy — read INSIDE the sandbox, so the launcher-side
  scrub is exercised, not only the parent-side one.
- A delegated `kiro-cli` spawn (`delegate_to_kiro` true) receives no leaf, no
  bearer and no proxy.
- On `codex`, two sessions on one runtime see the same bearer and socket, and the
  vend record names the runtime.
- The helper prints the SDK's expected JSON shape, and a non-zero exit on refusal.
- Every vend writes a record that carries neither the credential nor the signature.

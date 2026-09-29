# Runtime Ownership — Design Document

## Overview

One agent runtime is one operating-system process, and one process can serve
several sessions. Everything in this spec follows from that sentence. A pid names
a process, never a session, so any code that reads a pid to mean "this session's
runtime" is making a statement about the wrong subject as soon as a sub-agent's
session sits beside its principal's on one process: a teardown ends a neighbour's
live process, a retry budget is charged to a session that did nothing wrong, a
stall verdict is derived from a co-tenant's activity.

The model is three rules:

1. **A pool owns the process.** `runtime_ownership.RuntimeOwnership` places a
   session on a runtime and hands back a lease. A session never names a pid.
2. **Anyone who may end a runtime holds a lease; anyone who is merely using one
   holds a tenancy.** `runtime_ownership.RuntimeTenancy` is the second table,
   because the two claims are held by different parties and answer different
   questions.
3. **Exactly one function decides whether a runtime may die.**
   `runtime_ownership.authorize_runtime_kill` asks both tables and records who
   fired. Every other path asks it first.

What this spec owns: the two ownership tables, the kill gate, death
classification, the kernel-versus-registry reconciler, the pid-shaped guards, and
the asymmetries between platforms. What it does not own: the session registry and
the sweep that drives the cleanup tick ([session.md](session.md)), the eight-step
peer check on a shared pid ([dashboard-token-auth.md](dashboard-token-auth.md) §
*Unix-socket transport: kernel-attested `X-Session-Key`*), and the threat model
([security.md](security.md)).

`runtime_ownership` imports nothing from `kiro_crew`. It is a leaf on purpose:
`session_pid` is the pid-bookkeeping leaf and calls the gate, so an import in the
other direction would close `session_pid → acp → acp.runtime → session_pid`.

### What the model is made of

| Mechanism | Where it lives |
|---|---|
| The lease table, and a lease taken where a chat session joins the registry | `runtime_ownership.RuntimeOwnership`, `acquire_session_lease` |
| The tenancy table, so a party that cannot hold a lease is still defended | `runtime_ownership.RuntimeTenancy`, `claim_runtime_tenancy` |
| The one gate, and the release-before-signal rule every kill path obeys | `runtime_ownership.authorize_runtime_kill`, `process_identity.release_teardown_lease` |
| One death classified once, read by every tenant of the process | `runtime_death.announce`, `caused_by_this_session` |
| The kernel compared against the registry, in both directions | `runtime_reconcile.RuntimeReconciler`, `session_scope_reap.instance_slice_pids` |
| One client-side identity ladder, and a pid mapping that records its tenants | `mcp_caller.resolve_own_identity`, `session_pid_sig.read_session_pid_mapping` |
| Liveness attributed per probe rather than claimed per module | `acp.liveness.LivenessOracle` |
| A cgroup scope named after the runtime that holds it | `sandbox.scope_unit_name`, `name_scope_unit` |
| Two counts of the pattern being removed, each allowed to fall only | `test/test_kill_chokepoint_ratchet.py`, `test/test_pid_reader_ratchet.py` |

## The model

### Leases

`RuntimeOwnership.acquire(key, session_key, spawn, cap=...)` returns an
`Acquisition`. Placement prefers the runtime this session key last landed on while
it is alive and has room, then any compatible runtime with room, then `spawn()`.
Room means fewer than `cap` leases. A runtime lives while at least one lease is
outstanding; the **last** release hands it back for teardown and every earlier
release only drops a reference.

`cap` is `runtime_ownership.CHAT_RUNTIME_CAP`, and it is **1** today. At that cap
no entry holding a lease has room, so every acquisition spawns and every release
is a last release — byte-for-byte the unpooled behaviour, with the claim recorded.
Raising it is a behaviour change that needs the eligibility rules deciding which
sessions may share a process, and those do not live here.

On the **chat** registration path a lease means the same thing as registry
membership, which is why it is taken there rather than at provider start:
`session_allocation.SessionAllocationService._get_or_create_impl` calls
`acquire_session_lease` immediately after the session enters `self._sessions`.
Before that line no tenant exists, which is why every earlier cleanup path in that
file may kill unconditionally.

That equivalence holds for chat and nowhere else, and the difference matters to
anyone reading a lease count as a tenancy count. `acquire_session_lease` is reached
from the chat registration path alone: `open_task_session` registers a session
without one, the task-run path forces `_owns_runtime = False` on the provider it
hands the runtime to, and a companion runtime is spawned bare. So **no shared
runtime holds a lease**, `outstanding_leases` reads 0 for a perfectly healthy one,
and what defends it is a tenancy. A mechanism keyed on lease absence would end a
task run's runtime at the close of its first step.

### Tenancies

A lease is a claim by a party that MAY end the runtime. A tenancy is a claim by a
party that is using a process it may NOT end: a session-sharing sub-agent mid-turn
on its principal's runtime (`acp.session_provider.AcpSessionProvider._claim_shared_turn`
/ `_end_shared_turn`), and the OAuth mint child mid-exchange
(`connections.mint`). At `cap=1` a tenancy is the only way such a party is
represented at all, because an acquisition cannot join an occupied runtime.

The two tables are separate for a reason a single refcount cannot express: a
tenancy has to outlive the lease. A principal's last release forgets the lease
entry, and that is the exact moment a co-tenant becomes undefended.

The scope of a sub-agent's tenancy is the **turn**, not the object's lifetime. A
turn is the interval in which ending the process destroys work that is never
retried; an idle sub-agent holding a claim for its whole life would pin a runtime
nobody is using.

A self-shield is not a tenancy. Every `AcpRuntime` shields its own pid as it spawns
and drops the shield only once its kill has succeeded, so
`session_pid._PROTECTED_PIDS` is held for the whole life of the very process its
owner is entitled to end. The gate therefore **never** reads it: doing so would
refuse every ordinary teardown and leak the process it declined to signal. A
tenancy is taken by a party that is not the owner, is named, and is released when
that party is done — which is what lets the gate refuse on its behalf without ever
refusing an owner.

### The shape in one picture

```
   session A ──lease──┐                          ┌── RuntimeTenancy ────────────┐
   session B ──lease──┼──► RuntimeOwnership      │ sub-agent turn  ─► claim     │
   session C ──lease──┘    (lease table,         │ OAuth mint      ─► claim     │
                            cap = 1 today)       │ kill owed ──► hand-back      │
                                 │               └──────────────┬───────────────┘
                                 │  placement / last release    │
                                 ▼                              │
                         ┌───────────────┐                      │
                         │ runtime pool  │  one runtime = one process
                         │  AcpRuntime   │  = one cgroup scope kirocrew-rt-<token>
                         └───────┬───────┘                      │
      every kill path            │                              │
   ─────────────────────────────►│◄─────────────────────────────┘
   session teardown              ▼
   dashboard reset-all   authorize_runtime_kill(target, reason=, caller=)
   cron run reaper         1. leases outstanding?  → REFUSED (release first)
   sub-agent reset ladder  2. tenancies outstanding? → REFUSED (debt recorded)
   scope reaper            3. note_runtime_kill: WARNING with caller + reason
   cleanup sweep                 │ allow
   reconciler                    ▼
                          kill seam: identity re-checked, then the signal
                                    │
                                    ▼  death detected once
                          runtime_death.announce ──► every tenant reads ONE record

   reconciler (every cleanup tick): kernel cgroup.procs  vs  the registry
        record with no process ──► retract, notify the holder once
        process with no record ──► count; kill only when all conditions hold
```

## The one kill gate

`authorize_runtime_kill(target, *, reason, caller) -> bool` returns a verdict and
does not kill. Ownership ("does anyone still need this runtime?") and identity
("is this pid still the process I recorded?", `process_identity`) are different
questions, and a caller that owns a careful escalation — a SIGTERM grace before
SIGKILL, a process group proved by a vouching member, a Windows tree pinned by
handle — keeps that escalation and asks the gate once, at the top. Folding them
together would mean re-resolving a process group from a pid at signal time, which
is the identity defect.

False means REFUSED, on either of two grounds:

- **Leases outstanding.** The caller holds a pid it does not own. A caller that
  legitimately ends this runtime releases its lease first and is then authorized;
  one that signals without releasing is refusing its own teardown. This is why the
  gate and the release sites had to be one change: a gate switched on alone
  declines every force-kill path and leaks the process it declines to signal.
- **Tenancies outstanding.** Somebody who does not own the process is mid-flight
  on it. This refusal is not the caller's mistake — the owner may have released
  correctly — and the remedy is to let the tenant finish.

Both outcomes log at WARNING. The allow path logs through
`note_runtime_kill`, so there is one attribution implementation and the refusal is
the only thing the gate adds:

```
runtime_ownership REFUSED kill pid=<pid> leases=<n> caller=<caller> reason=<reason>:
  the process is still leased, so this caller must release before it signals
runtime_ownership REFUSED kill pid=<pid> tenants=<n> holders=<a,b> caller=<caller> reason=<reason>:
  a party that does not own this process is still using it
```

Two call sites are pinned by test, and the rest hold by convention. That
difference is load-bearing for anyone adding a kill path, so it is stated rather
than blurred:

| Call site | Enforcement |
|---|---|
| `session_pid._sync_kill_provider` — the funnel the session layer's hard-kill sites reach | `test_kill_chokepoint_ratchet.py`'s `ATTRIBUTED_KILL_PATHS`: must call the gate, must pass `reason` and `caller`, must act on the verdict |
| `acp.runtime.AcpRuntime.kill` | same |
| `cron._sigkill_session` | convention |
| `session_cleanup._kill_authorized` | convention |
| `session_scope_reap._stop_and_signal_members` | convention |
| `subagent_manager.monitoring._reconcile_orphans_impl` | convention |
| `subagent_manager.terminal._sigkill_session_impl` | convention |
| `runtime_reconcile._default_authorize` | convention |

A convention site that drops its gate call is not caught: the primitive call it
still makes stays counted, so `BYPASS_BASELINE` does not move and the suite stays
green. Adding a site to `ATTRIBUTED_KILL_PATHS` is what promotes it.

Releasing before the signal is shared rather than re-implemented:
`process_identity.release_teardown_lease` releases the lease held by the teardown's
OWN subject — matched by the handle's pid and start identity, from the manager's
torn-down table and from the caller's captured pop — and touches no tenancy and no
other session's lease. It is best effort by design: a release that did not happen
costs an honest refusal, while an exception raised out of it would cost a wedged
process its only remaining kill.

### Kill owed, and the hand-back

A refusal on tenancy grounds would otherwise lose the teardown: the owner has
released, nobody will ask again, and the process outlives every record of it. So
the refusal records a **debt** on the claims it refused for
(`RuntimeTenancy.refuse_for_tenants`), and the last tenant out of a runtime that
owes one is handed the runtime back:

```
runtime_tenancy outcome=kill_owed_handback pid=<pid> holder=<holder>: ...
```

The count and the debt are one call, not a read followed by a write, because the
two can straddle the last tenant's departure and leave a debt owed by a process
that has no tenants — to be spent on whichever runtime holds that pid later. The
debt is keyed so it cannot migrate to a process that reuses the pid
(`test_runtime_ownership.py::test_refusal_debt_does_not_migrate_to_a_later_process_reusing_the_pid`).

A verdict is a statement about the past, so an authorized kill also takes a
barrier before it signals: `tenancy_epoch` is read right after the allow and
`commit_runtime_teardown` is called immediately before the first signal. A claim
that lands in between makes the commit fail and the kill is **abandoned**, because
the signal would land on a live turn that is never retried. A claim arriving after
the commit is refused with `RuntimeTeardownCommitted` rather than granted. The
barrier is dropped on every exit from the signal path, including a raise — a pid
left committed is one no tenant can claim for the life of the gateway.

## A death is a process event

`runtime_death.announce` is called ONCE, where the death is detected, and records
what the process was carrying at that instant. Every tenant then reads the same
record instead of re-deriving one, and the question a retry budget or a circuit
breaker asks is `runtime_death.caused_by_this_session(target)` — by handle, never
by pid, because a session that can name a pid can signal it.

One record is what keeps a single process death from costing N resets, N budget
charges and N unrelated accounts of why. A death on a shared runtime takes every
session on the process with it — a measured one carried 15, 8 of them mid-prompt —
and a tenant deciding alone has no way to tell that loss from its own failure.

The record carries two independent readings of co-tenancy, and both are needed.
`leases` is what the lease table holds; `acp_sessions` is how many ACP sessions the
process was actually multiplexing. A sub-agent runs on its parent's runtime and
holds no lease, so the registry alone would call that process single-tenant and
hand the parent the bill. The runtime alone would miss a tenant that has a lease
but has not opened its session yet. The lease reading is also not available on
every path, and the contract says so: on a REAPED death the return code is already
set, so the registry considers the runtime dead and the lease count is 0 whatever
it holds.

Tenants PULL the record rather than being pushed it. A tenant learns of the death
the way it already does — its poisoned session queue raises into whatever turn it
had — and reads the record while handling it. A tenant with no turn in flight has
nothing to recover and re-acquires on its next turn, which drops a dead runtime on
the way past. There is deliberately no observer registry: a notification channel
with no subscriber is a mechanism whose first real consumer would have to redesign
it.

A shared death still has to bound recovery, so it is counted per session
(`note_shared_death` / `clear_shared_deaths`) as the substitute ceiling for the
per-session streak it is exempt from. The rule that came out of building it: a
substitute ceiling must share the key and the clearing point of the counter it
replaces, or it never trips.

Pinned by `test_runtime_death_is_a_process_event.py` —
`test_a_shared_runtimes_death_is_nobodys_single_failure`,
`test_co_tenancy_is_read_before_the_dead_flag_flips`,
`test_the_death_is_announced_exactly_once`,
`test_a_tenant_asks_by_handle_never_by_pid`.

## Reconciliation: the kernel against the registry

Every other sweep in the gateway asks "is this pid one I still need?" and asks it
of a record. `runtime_reconcile.RuntimeReconciler` asks the kernel instead, and
compares both answers in **both** directions, because each direction is a
different leak:

| Direction | What it means | Action |
|---|---|---|
| `owned_dead` | A record names a pid that is gone, or one the kernel gave to an unrelated process. | Retract the record; tell the holder once. The stranger is never signalled. |
| `unowned_alive` | A process runs in this install's own agent slice and no record claims it. | Count it. Kill only when every condition below holds. |
| `owned_alive` | The healthy population. | Reported as the denominator. |

`unowned_alive` and `owned_dead` are the SLI, published so an operator sees a leak
while it is small; the actions are what makes a non-zero reading temporary rather
than permanent. `test/e2e/process_inventory.py` uses the same three names for the
same reconciliation read from outside the process, so a published reading and an
external inventory describe one host state in one vocabulary.

A recycled pid belongs in `owned_dead` and not in the healthy population, which is
what makes the reading a usable safety number: a record whose pid now names a
stranger is the exact input that turns a sweep signalling by pid into a kill of
somebody else's process.

Mounted on the cleanup tick at
`session_cleanup.SessionCleanup._reconcile_runtimes_hook`, built once and
retained across ticks because the two-pass confirmation IS its state. Two whole-pass
refusals come first, because a half-read pass is the dangerous one: a kernel
reading that cannot be taken, and a registry that cannot be read, each refuse the
entire pass rather than acting on the half that answered. An incomplete active-pid
union does the same.

### Why an unowned process is counted before it is killed

Absence from the record is evidence that something is unclaimed, not evidence that
it is abandoned. The unowned population on a healthy host is dominated by processes
with perfectly good owners that no *session* record describes: a Playwright
chromium tree owned by a browser panel, `mcp start-server` processes owned by a
stub connection, sandbox shim wrappers owned by a spawn in progress. Killing on
first sight would take a user's live browser out from under them and call it a leak
fixed. Measured at rest on a live instance: 20 such processes; under load, 57 to
97.

So a kill needs all of these, and any one missing leaves the process alone and
merely counted (`RuntimeReconciler._why_not_yet`, `_reconcile_unowned`):

1. the per-pass kill budget is not spent — see `runtime_reconcile.DEFAULT_MAX_KILLS`
   and the reconcile configuration for the value, so a reconciler that is wrong
   about a whole population is wrong slowly enough to be noticed;
2. it was unowned on the PREVIOUS pass too, keyed on pid **and** process identity
   so a recycled pid cannot inherit the confirmation;
3. it carries this install's own spawn marker (`KIROCREW_SPAWNED`, via
   `runtime_reconcile.process_is_ours`), so it is ours to end;
4. it is older than `runtime_reconcile.DEFAULT_MIN_AGE_SECS` — the same
   registration window seen from the other side;
5. neither table claims the pid, and an unreadable table counts as claimed;
6. its process identity has not changed since classification, re-read in the
   instant before the signal;
7. the gate allows it — asked LAST, because the allow path writes the kill
   attribution, so asking earlier would record a kill that never happened;
8. the teardown barrier commits, so a tenancy claimed after the verdict abandons
   the kill.

Two counting rules keep the SLI honest. A call the kill seam answers by signalling
NOTHING (its own managed-agent check refused) is not a kill: counting it would
spend the whole budget on the same lowest pids every pass, forever, while the
reading reported kills and nothing changed. And a withheld pid is audited on a
CHANGE of reason, not every pass — most of the unowned population is permanently
withheld, and one row per pid per pass would evict the history an operator needs.

The dead direction acts immediately and needs no second pass: forgetting a dead
pid harms nothing and the lease holder learns its runtime died instead of waiting
out a timeout. A single liveness probe saying DEAD is still not enough on its own —
an unsignalable pid is an unknown, not an absence.

Retraction is three-valued (`retracted` / `failed` / `not-mine`) rather than a
bool. A row owned by a concurrent CLI or a predecessor gateway is a real dead
record this pass cannot remove, and a pid known only to the MCP backend pidfile or
the manager's in-memory union has no row in either tracking file. Folding those
into the same `False` a real failure gets would publish one WARNING per stale pid
per tick for the gateway's life — a steady state reported as a fault.

### Platform scope

The kernel side of the unowned direction is Linux-only. It reads
`session_scope_reap.instance_slice_pids`, which enumerates `cgroup.procs` of this
install's per-instance child slice and each transient scope beneath it; an
unresolvable slice returns the EMPTY set and the pass then has nothing to compare.
Scoping to the per-instance child slice, not the shared parent, is what stops a
co-resident gateway's process reading here as one of ours with no record.

`session_scope_reap.reap_abandoned_agent_scopes` likewise reports
`supported=False` off Linux, without cgroup-v2 delegation, or with no
per-instance slice directory. The scope naming degrades the same way:
`sandbox.name_scope_unit` returns the argv unchanged wherever
`cgroup_scope_argv` hands back the bare command, and the runtime logs the unit
systemd actually received rather than one re-derived from the token, so a host
with no scope never announces one.

The `owned_dead` direction is portable: it compares records against liveness and
process identity and needs no cgroup.

## Identity: one ladder, and a pid that records its tenants

A pid answers per PROCESS, and one kiro-cli process hosts many ACP sessions, so on
a shared runtime a pid-keyed identity lookup succeeds with whichever session
published last. A wrong name is worse than no name, because the caller cannot tell
it is wrong.

**Client side** — `mcp_caller.resolve_own_identity` is the single ladder every
client-side consumer shares, so the rungs cannot drift between files. Strongest
first: (1) the protected member binding for this process, where an EMPTY string is
a refusal that must never fall through; (2) the signed per-SESSION token on this
process's own element, above the env var because a warm-pool rekey makes the env
stale; (3) `KIROCREW_SESSION_KEY`; (4) the `session_pid_<pid>.txt` mapping, by the
launcher-exported `KIROCREW_HOST_PID` and then by ancestor walk. Rung 4 is the one
that cannot express the tree it reads, so it REFUSES on a shared pid rather than
answering, and every caller that reaches it at all is warned. It never raises: an
identity source that can raise turns a resolvable session into a crashed tool call.

**The mapping** — `session_pid_sig` publishes `session_pid_<pid>.txt` with a
`session_pid_<pid>.sig` MAC sidecar keyed by the agent-unreadable SEL trust root
with the pid bound into the MAC. The `.txt` carries a `tenants=<n>` line and one
`tenant=<key>` line per member.

Read it through the mapping readers, not the string ones. `read_session_pid_mapping`
(lenient) and `verify_session_pid_mapping` (MAC-verified) return a
`session_pid_sig.PidMapping` whose `session_key` is non-empty only when the pid
names exactly one session; otherwise `tenants` / `tenant_count` carry what is known
and `refusal` says which kind it is (`REFUSAL_ABSENT`, `REFUSAL_MALFORMED`,
`REFUSAL_RECYCLED`, `REFUSAL_CO_TENANT`). `read_session_pid_txt` and
`verify_session_pid` are the thin `str` views of those two, kept for the historical
callers: they answer `""` for every refusal, so a caller written against them
cannot tell a co-tenanted pid from an absent file — which is the misattribution
this section exists to prevent. The refusal kind matters at the auth boundary
because a recycle is reported only after the signature verifies, so it is the
publisher's attested statement rather than ambiguity about who is calling.

**Server side** — the `.txt` is agent-readable, so membership in the roster is
necessary but never sufficient: a co-tenant can read a sibling's key out of it and
declare it. On a shared pid the caller must additionally present an
`X-Session-Token` that `session_token_sig.verify_session_token` resolves to
exactly the declared key. The full eight-step check —
`socketsec.check_peer_is_self`, the peer pid,
`peer_resolve.resolve_peer_identity(..., signed_only=True)`, the mismatch denial,
the shared-pid token demand, the unverifiable-MAC arm, the live-manager arm, and
the unresolved arm that proceeds under today's semantics — is specified in
[dashboard-token-auth.md](dashboard-token-auth.md) § *Unix-socket transport:
kernel-attested `X-Session-Key` (POSIX)* and is not repeated here.

The sidecar publication contract itself lives in [session.md](session.md) §
*session_pid sidecar contract*. Topology is pinned by
`test_identity_topology.py`.

**Liveness attribution** — `acp.liveness` states per probe whether it survives a
shared process tree, rather than claiming it for the module. The cmdline match keys
shell evidence to this session's in-flight command and attributes. The movement and
socket probes read the WHOLE tree, so a co-tenant's build reads as this session's
progress and a co-tenant's backend connection suppresses this session's wedge
signature — both err toward forbearance, which costs detection latency and no work.
The one reading acted on at once, the model-wait DEAD, is gated on a declared
`tenancy` callable: with co-tenants it degrades to UNKNOWN tagged
`liveness.EVIDENCE_SHARED_TREE`. Where attribution is impossible the verdict
degrades to UNKNOWN, never to a kill.

## Guards

### Two ratchets

Both are counts of a pattern the refactor is removing. Each may fall in any change
and may never rise, and a change that lowers one records the new number in the same
commit.

- `test/test_kill_chokepoint_ratchet.py` — `BYPASS_BASELINE` counts src sites that
  call a kill primitive without going through the gate. It also pins what a falling
  count has to MEAN: `ATTRIBUTED_KILL_PATHS` must call the gate
  (`test_attributed_kill_paths_consult_the_gate`), pass both `reason` and `caller`
  (`test_the_gate_call_names_a_caller_and_a_reason`), and act on the verdict
  (`test_the_gate_verdict_is_acted_on`) — a count driven to zero by renaming the
  primitives, with nothing logged, would satisfy a bare ceiling and leave the field
  as undiagnosable as before. A positive control
  (`test_the_needles_match_a_known_call_site`) stops an empty scan reading as total
  success.
- `test/test_pid_reader_ratchet.py` — `_BASELINE_SITES` counts src sites that read
  a runtime's pid, by AST rather than grep so reflective reads survive nesting and
  wrapping. A site whose reading genuinely belongs to the module that owns the pid
  carries an owner marker and is counted apart under `_BASELINE_OWNER_MARKED`, so
  marking a site is a visible act rather than a silent exemption. The exact-match
  test refuses a stale baseline in either direction: a baseline left above the real
  count leaves room for a future reader to fill silently.

The baseline is an invariant of the tree, not of a branch. Whoever merges after
someone else re-measures on their own tree.

### The leak and chaos gates

`test/e2e/test_process_leak_invariant.py` boots one real gateway on a throwaway
data home, drives several dashboard sessions through a full turn each, closes every
one, and then asks the kernel and the registry what survived. Four populations must
be empty: live pids in the instance's own agent slice that any session created
(with the gateway's own background runtimes as the one documented residue, bounded
by count and identified by the agent they run), managed MCP stub processes,
registry entries naming a process that is gone, and crew-log write handles held by
the gateway.

Two design points carry the gate. It runs a real gateway SUBPROCESS because two of
the four assertions are about the gateway process itself — `/proc/<pid>/fd` is only
the gateway's fd table when the gateway is its own process, and an agent slice is
only this instance's when the instance has its own data home. And the live PEAK is
asserted BEFORE the teardown, because "nothing survived" is also what a run
produces when nothing ever started.

Two switches, for the same reason: `KIROCREW_E2E` lifts the module skip, and
`KIROCREW_E2E_REQUIRE` turns an unmet precondition from a skip into a failure,
because pytest counts a skip as a pass. The nightly job asserts the suite reported
its expected passing count rather than trusting a zero-failure exit.

`test/e2e/test_process_chaos.py` covers the external-death direction: kill a
runtime root from outside and the registry must forget it and the lease holder must
recover.

## Known gaps

- **A session token is not bound to a lease or a claim.** `X-Session-Token` is a
  bearer: a same-uid process that can read `/proc/<pid>/environ` can present a
  neighbour's token. The reachability is narrow — such a process already has
  `PTRACE_MODE_READ`, and at `cap=1` a chat session does not reach the shared arm —
  but the fix is to bind the token to a tenancy claim with a per-process binding
  the server checks, not to harden the bearer. Tracked in issue #14646 together
  with the recycled-ancestor case, where the lenient MCP identity refuses every
  call for an identity-less caller.
- **The `tenancy` seam in `acp.liveness.LivenessOracle` has no production
  declarer.** All four construction sites take the single-tenant reading, so the
  model-wait DEAD verdict is not degraded on a shared runtime. Wiring it means
  passing a lease-plus-claim count from each site.
- **A shared runtime dying during session start advances every auto-nudge loop's
  start-failure streak on that process** (issue #14657). The start path raises
  without carrying the runtime's identity, so the death cannot be attributed.
- **`providers/base.py` still exposes process-shaped members**, and
  `runtime_info()` hands a caller a pid outright. They are the bulk of what the pid
  reader ratchet still counts; each needs classifying as a session-level question,
  a runtime handle, or a reading whose owner is the module that holds the pid.
- **`cap` is not open.** Raising `CHAT_RUNTIME_CAP` needs eligibility rules that
  decide which sessions may share a process, and a flag to stage it. Neither
  exists, so every runtime serves one session.
- **The gate's allow line names a pid the seam may never signal.** The
  managed-argv check lives inside the kill seam, after the verdict, so a
  reconciler pass records an allow for a process it then leaves alone. The reading
  is conservative in the safe direction but makes the allow lines a poor census of
  real kills.
- **[session.md](session.md)'s sweep contract does not describe the gate**
  (issue #14726). The reconciler is specified here; the sentences in that spec that
  still describe an ungated sweep are corrected by the change that owns it.

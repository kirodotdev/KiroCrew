---
title: Crew Agent SDK Boundary — isolate the codebase from ACP, and name the host contract
status: partially-implemented
revision: v4
author: zejiangg, with Kiro
created: 2026-08-28
last-audited: 2026-09-05
audited-at: 73d60a83d
doc-pr:
implementation-prs:
  - "PR 1 — the boundary gate and its baseline: scripts/check_agent_sdk_boundary.py,
    .github/agent-sdk-boundary-baseline.txt (seeded at 58 files / 107 edges),
    src/kiro_crew/agent_sdk/, and the ci.yml wiring at :440-441"
tracking-issues: []
supersedes: []
superseded-by: []
---
# RFC: Crew Agent SDK Boundary — isolate the codebase from ACP, and name the host contract

- Status: partially implemented. **PR 1 has landed**: the shrink-only import
  gate (`scripts/check_agent_sdk_boundary.py`), its baseline
  (`.github/agent-sdk-boundary-baseline.txt`, seeded at **58 files / 107
  edges**), the `ci.yml` wiring at `:440-441`, and the `src/kiro_crew/agent_sdk/`
  package — which already carries more than the layer docstring PR 1 proposed:
  `drivers/acp.py`, `backend_install.py`, `backend_identity.py`,
  `provider_identity.py` and `native_commands.py`. **Part of PR 4 has landed**
  too, on this branch: the import cycle §2.4 exists to break is closed, and the
  cycle v3 named was the wrong one. PRs 2, 3, 5 and 6 remain unstarted.
  The migration is additive: the boundary package sits beside the current
  provider layer and consumers move behind it one wave at a time under the
  ratchet. Every question in §12 carries a disposition: the two that gated PR 2
  and PR 4 are decided, and the rest record a conservative default plus the
  condition that reopens it.
- Author: zejiangg, with Kiro
- Created: 2026-08-28
- Audited against: `73d60a83d`
- Related: `../system-specs/features/agent-host-contract.md` (the host contract
  this document's §6 summarises),
  `../system-specs/features/claude-code-provider.md`,
  `../system-specs/modules/acp-client.md`,
  `../system-specs/modules/providers.md`,
  `../system-specs/modules/session.md`,
  `../system-specs/modules/subagent.md`,
  and `rfc-pluggable-model-providers.md`
- Related unmerged work: PR
  [#6307](https://github.com/kirodotdev/KiroCrew/pull/6307) (`feat: add staged
  acp adapter admission`, head `7e3e27395`) adds an adapter descriptor and
  registry **inside** the ACP layer. It is orthogonal to this RFC and needs no
  change to land — see §11.2.

## 1. Summary

Introduce `kiro_crew.agent_sdk` as the **only** import surface through which the
rest of the codebase talks to an agent backend, and make `kiro_crew.acp` private
to a single driver behind it.

Today there is a package named `kiro_crew.providers` that looks like this
boundary and is not one. It re-exports ACP symbols rather than translating them,
its "provider-agnostic" event type is the ACP event class under an alias
(`src/kiro_crew/providers/base.py:30`), and most modules outside it import
`kiro_crew.acp` directly rather than going through it. PR 1's baseline seeded the
census at **58 files / 107 edges** across the two forbidden roots; the live split
is printed by the gate on every run, and
`.github/agent-sdk-boundary-baseline.txt` is the per-file record. Do not read a
count out of this document — §PR 5 says why. The consequence is that switching
agent backends is not a driver swap; it is an edit across the whole tree.

This RFC proposes three inversions, in order: the SDK owns the **types** that
cross the boundary, the SDK owns the **process and session lifecycle**, and a
shrink-only **import ratchet** makes the boundary enforceable instead of
aspirational.

It also separates a second body of coupling that the SDK does **not** address, and
that conflating with the first is how a provider migration fails halfway. Much of
what looks like backend coupling is coupling to a **host**: an agent-definition
layout, a session replay store, an identity store, a sandbox posture, an MCP
delivery channel, a billing surface, a permission engine, and the auxiliary
runtimes a host cannot discover for itself. Those are provider-scoped, and §6
summarises them against the full contract in
[`../system-specs/features/agent-host-contract.md`](../system-specs/features/agent-host-contract.md).

The evidence for that contract is not hypothetical. Claude Code is a real,
previously-exercised foreign host: the public core carries its protocol layer and
leaves the host glue to an internal companion, and the companion supplies a
complete answer to every bucket. Its profile is what tells us what a future
non-Kiro provider actually costs — and, read adversarially, it is also what
exposed a design flaw in an earlier draft of §5 (§5.3).

## 2. Motivation and current state

Re-verified at `73d60a83d` on 2026-09-05. Counts below are from
`src/kiro_crew`, excluding `src/kiro_crew/acp/` and `src/kiro_crew/providers/`
themselves, and excluding `test/` unless stated. For import edges the
authoritative count is no longer an ad-hoc scan: it is
`.github/agent-sdk-boundary-baseline.txt`, and the gate prints the live per-root
split on every run.

### 2.1 The existing seam is an alias, not a translation

`src/kiro_crew/providers/base.py:30`:

```python
from kiro_crew.acp.types import AcpEvent as LLMEvent  # noqa: F401
```

Every consumer that touches a turn reads ACP's own dataclass. `base.py` further
re-exports 13 `EVENT_*` constants from `kiro_crew.acp.types` unchanged, so the
event vocabulary is ACP's vocabulary with a different import path. There are
**466** `EVENT_*` / `STOP_REASON_*` usages outside the ACP package.

`AcpEvent` (`src/kiro_crew/acp/types.py:456`) carries 30 fields. Roughly a third
are not domain facts:

| Field | Why it should not cross a boundary |
|---|---|
| `request_id: str \| int` | Raw JSON-RPC id |
| `options: list[dict[str, str]]` | Raw ACP permission `optionId` dicts |
| `raw_tool_params: dict \| None` | Pre-conversion ACP params |
| `tool_final: bool` | ACP `status=completed` marker |
| `tool_kind: str` | Raw ACP kind vocabulary |
| `runtime_global: bool`, `sub_session_id: str` | Runtime-multiplexing artifacts |
| `raw_params_trusted`, `shell_classified`, `mcp_identity_trusted`, `mcp_identity_ambiguous` | Driver-internal provenance/cache flags |

The `request_id` leak is the sharpest instance. `approve_tool(request_id)` /
`reject_tool(request_id)` take the wire id straight through; `chat_runner.py:7300`
keys `slot._approval_futures` on `str(event.request_id)`, and
`chat_runner.py:7487` ships `{"id": str(event.request_id)}` to the browser. A raw
JSON-RPC id is part of the frontend contract.

The first domain vocabulary now owned by `kiro_crew.agent_sdk` is the minimal
completed-turn contract used by structured monitors: provider-neutral input and
output token dimensions plus terminal stop reasons. Monitor accounting consumes
that SDK surface instead of importing ACP's `TurnUsage` and constants directly.

### 2.2 The boundary is bypassed

**58 files / 107 edges**, as seeded into
`.github/agent-sdk-boundary-baseline.txt` by PR 1 across both forbidden roots
(`kiro_crew.acp` and `kiro_crew.providers`).

An earlier revision printed a per-file table here. It is deleted rather than
refreshed, for the reason §PR 5 gives about counts: every row was already wrong
within days — `session.py` had grown from 10 to 12, `subagent.py` from 5 to 8,
and `dashboard/handlers/core.py` and `handlers/agents.py` were no longer among
the heaviest at all. Worse, the figure it opened with was the "68 edges across 42
files" that §7's PR 1 paragraph explicitly disowns as a regex artifact, so the
document contradicted itself two ways. The baseline is the per-file record and
the gate prints the live per-root split; read either, not this paragraph.

Several files reach past the public surface entirely: `session.py` imports
`acp.session_handle._load_watchdog_settings`, `dashboard/session_memory.py`
imports `acp.runtime._get_rss_tree_mb` and `_iter_descendant_pids`,
`dashboard/stall_enrichment.py` imports `acp.liveness.socket_inodes` (a `/proc`
primitive), and `dashboard/steer_settle.py` imports `acp._dispatch.redact_text`.

`src/kiro_crew/mcp_tools/spawn.py` has zero ACP references — it consumes
everything through `subagent.py`. It is the only already-clean consumer and it is
the shape the rest should have.

### 2.3 Backend identity is a string that everyone compares

**This subsection was structurally wrong in v3 and is rewritten.** It cited
`acp/types.py:109-113` as the home of the backend constants and "seven opt-in
frozensets in the same file". `acp/types.py` now holds **zero** of either. Every
constant and every capability set lives in the leaf
`src/kiro_crew/acp_backends.py` — `ACP_BACKEND_CLAUDE` (`:41`),
`ACP_BACKEND_KAS` (`:42`), `ACP_BACKEND_CODEX` (`:47`), `ACP_BACKEND_KIRO`
(`:50`) — which `acp/types.py` re-exports, so existing call sites kept their
import path while the definitions moved.

That leaf has outgrown the job v3 credited it with. v3 called it a deliberate
leaf owning the selectable-backend list, three drifted literals collapsed into
one place, and said what it did *not* cover was the comparison behaviour. It
covers that too now. At ~580 lines it holds:

- **15** `ACP_BACKENDS_*` capability frozensets, not seven. Beyond the five v3
  named: `_MEMBER_DISPATCH` (`:331`), `_COMPACT` (`:351`),
  `_MODEL_VIA_CONFIG_OPTION` (`:414`), `_EFFORT_VIA_CONFIG_OPTION` (`:423`),
  `_ADVERTISED_MODEL_SELECTION` (`:435`), `_SEED_LOCAL_SETTINGS` (`:445`),
  `_KIRO_SLASH_COMMANDS` (`:479`), `_MCP_CONFIG_HOT_RELOAD` (`:496`) and
  `_SESSION_MCP_ARRAY` (`:79`). Count them at the file; a number written here
  rots, and this one already did.
- A `Routing` enum (`:509`) naming the *mechanism* by which a harness is made to
  ask before it runs a tool, with `UNVERIFIED` as an explicit "we do not know"
  member so an unestablished harness cannot fall through to a permissive branch.
- Two identity-keyed dispatch tables — `ACP_BACKEND_ROUTING` (`:548`) and
  `ACP_BACKEND_PERMISSION_CONFIG` (`:568`) — read through `routing_for()`
  (`:573`) and `permission_config_for()` (`:578`), both of which fail closed on
  an id the table does not name.

A second top-level policy module has joined it: `src/kiro_crew/acp_tool_gate.py`
(383 lines), which decides whether a harness's routing counts as *enforced*
(`ENFORCED_ROUTINGS`, `:56`) and derives the credential directories an adapter's
child must not be able to read. Like the leaf, it imports nothing from `acp/`;
like the leaf, it sits outside `agent_sdk`.

Neither module is a boundary violation, and the architecture test pins that: a
prefix-match on `kiro_crew.acp` would flag `acp_backends`, which is why the gate
matches on module boundaries instead.

So the defect is narrower and more precise than v3 stated, and PR 3's job changes
accordingly (§7). Two things remain wrong:

1. **Consumers ask set membership, not a semantic capability.** A caller that
   wants to know "can this session be steered?" still asks "is this backend in
   `ACP_BACKENDS_STEER`?". The vocabulary has an owner; the question is still
   spelled as identity, and §5.2 is the list of places it is asked.
2. **Routing and permission dispatch are keyed on backend id.** `routing_for()`
   and `permission_config_for()` are accessors over `dict[str, …]`, so adding a
   harness is a table row plus whatever reads it. That is much better than an
   `if/elif` chain — §2.7 credits it as the right shape arrived at independently
   — but it is still an id lookup rather than a capability a driver declares.

The consequence for scope: PR 3 no longer *builds* the capability mechanism. It
consolidates this one into `agent_sdk` and closes those two gaps.

### 2.4 ACP lifecycle state lives outside the ACP package

**Three of the four concerns v3 listed have since moved**, and `session.py` only
forwards to them. The state is no longer where this subsection said it was:

- `_warm_pool` is a forwarding property at `session.py:1435` onto
  `session_pool.py`, which owns the pre-spawned process pool.
- `_bg_runtime` and `_bg_runtime_lock` forward from `session.py:1467` / `:1475`
  onto `session_background.py`.
- `_subagent_runtimes` forwards from `session.py:1329` onto
  `session_allocation.py`.
- `_rss_max_mb` forwards from `session.py:1190` onto `session_cleanup.py`. Its
  settings loader is still imported from *inside* the ACP package.

Those four are now a boundary question about where the SDK reads them, not a
"lifecycle state lives in `SessionManager`" question. Only `session_pid.py`, which
owns the whole PID lifecycle for agent processes, was genuinely ACP-owned — and
the cycle v3 named there was the wrong cycle.

**The cycle, corrected.** v3 asserted `session_pid → acp.client → session →
session_pid`, evidenced by two lazy `from kiro_crew.acp.client import
_get_child_pids` calls. Those calls are real — `session_pid.py:323` and `:2504`
today, not the `:311-312` / `:1888-1889` v3 cited — but a lazy in-function import
runs after both modules are initialized, so it cannot close a loop at import time.
The operative cycle ran through a **module-scope** import: `session_pid.py:28`
imported `kiro_crew.providers.base` for one parameter annotation, giving

```
session_pid → providers.base → acp.types → acp/__init__ → acp.runtime → session_pid
```

and that one was fatal rather than cosmetic. `import kiro_crew.session_pid` as the
first `kiro_crew` module raised `ImportError: cannot import name '_track_pid' from
partially initialized module`. It also explains a pattern elsewhere: leaf modules
carried `LLMProvider = Any` runtime stubs to stay out of it.

**It is closed, on this branch** — the cheap half of PR 4, landed rather than
proposed. The `providers.base` import at `session_pid.py:28` is **gone, not
deferred.** Making it `TYPE_CHECKING`-only was the first attempt and the boundary
gate refused it, correctly: a type-only import is still boundary knowledge by that
gate's explicit design, and it offers no opt-out marker precisely so that "this
consumer legitimately needs the layer" cannot be asserted. The refusal exposed a
better answer. `_sync_kill_provider` reads three PRIVATE attributes through
`getattr(..., None)` — `_client`, `_proc`, `_active_proc` — none of which the
provider ABC declares, so `LLMProvider` never described that parameter. It is
`object` now, the edge is deleted rather than exempted, and the baseline shrank
from 107 edges to 106. `session_compaction.py`'s `LLMProvider = Any` runtime stub
is deleted with it; and
`test/test_agent_lifecycle_cycle.py` pins both halves in a fresh interpreter —
`import kiro_crew.session_pid` standing alone, plus an assertion that the import
leaks no ACP module into `sys.modules`. `session_allocation.py` keeps its stub,
because its line 37 `ProviderFactory = Callable[..., LLMProvider]` is a runtime
subscript rather than an annotation, so deferral does not reach it.

`acp/worker_pool.py:49`'s `try/except` around `register_protected_pid` /
`unregister_protected_pid` was never part of any cycle. The edge is one-way, the
guard's own comment says it exists so the engine stays importable standalone, and
it catches `Exception` rather than `ImportError` — so no exit criterion should be
written against it, and deleting it is tidy-up.

**What PR 4 still owes is ownership, not the import.** The two lazy
`_get_child_pids` calls, `_kill_pid_tree`, and the `_MANAGED_AGENT_MARKERS`
vocabulary are all consulted from the agent half *and* the work-class half, so the
split has to place them rather than assume they travel with one side. The
non-agent sweeps gate their kill sets *negatively* on that marker vocabulary and
consume the protected set through `_collect_active_pids`, so a careless split
makes the work-class sweep less safe, not merely less tidy.

So the process supervision decision is still made in two places at once. The
import cycle that made it urgent is gone; the ownership question is not.

### 2.5 What this costs, measured on KAS

Two more backends already exist. KAS is the instructive one for this section:
adding it did not require a driver — it required branches. `runtime.py` and
`session_handle.py` carry explicit KAS arms, `session_handle.py` has five
`_handle_kas_*` methods, and the dashboard, config loader and doctor each learned
the new id. The fourth backend, Codex, landed after this census and did **not**
pay that price in the same way; §2.7 records what it did instead, and which part
of the cost survived.

### 2.6 What this costs, measured on a foreign host

KAS understates the cost, because KAS *is* kiro-cli (`kiro-cli acp
--agent-engine v3 --auth-method cli`) and therefore shares Kiro's identity store,
runtime, steer extension and model vocabulary. Claude Code is the only genuinely
foreign host this repository has ever carried, and its price is visible today as
**permanently dormant conditional surface** in the public core.

The registration seam is coherent: `ProviderRegistry.register_acp_backends` /
`create_factory` (`platform/interfaces.py:66-90`), a documented no-op default
(`platform/defaults.py:41-48`), one wiring site (`platform/bootstrap.py:220-229`),
and an explicit rule that the core never imports the companion. Everything below
it is not — the *behaviour* the companion must supply is delivered through three
kinds of undeclared hole:

| Kind | Count | Failure mode when the companion omits it |
|---|---|---|
| `getattr`-by-name seams whose target the core never defines — `getattr(self, "_write_claude_local_settings", None)` (`acp/client.py:2742`, `:3351`) | 2 | Silent: no permission mode, and the context window collapses from 1M to 200K |
| Methods returning a neutral value purely so a companion can override them — `_session_mcp_servers() -> []` (`acp/client.py:2335-2346`) is the type case | 6 | Silent: a CC session gets **zero MCP tools**, as the docstring itself states |
| `ClaudeCodeProvider is not None and isinstance(...)` guards against a name hard-coded to `None` (`session.py:170`, `subagent.py:131`) | 11 sites | Statically unreachable; nine `session.py` branches and two `subagent.py` branches are dead-but-maintained |
| Defensive attribute probes across the provider boundary (`session_pid.py` (`_collect_active_pids`) probes `_proc` and `_active_proc`, `chat_runner.py:867`, `knowledge/llm_pool.py:325`) | 4 | Duck typing in place of a type |
| Comment clusters naming the companion or a deleted module as the supplier of behaviour | 19 | The seam's real contract lives in prose |
| Refusal / downgrade mechanisms, including the degrade log in `acp_backends.resolve_selected_backend()` and five capability non-memberships | 9 | — |
| Live `_is_claude` branches inside `acp/` | 13 | — |
| CC-symbol lines in `src/kiro_crew` | 146 (352 with `test/`) | — |

None of the three hole kinds is declared in a Protocol, none is type-checked, and
none fails loudly when forgotten. That is the concrete cost this RFC's driver
contract is meant to replace, and it is why §6 exists as a contract rather than as
a list of observations.

The census above is the state that motivated this RFC. Two of its rows have since
been **closed in the core** rather than by a companion, because CC became
selectable in a public build and a silent hole is not something a selectable
provider can carry: the core defines `_write_claude_local_settings` itself
(permission mode, the `availableModels` allowlist that keeps a 1M-window id from
collapsing back to 200K, and the resolved model), and
`_session_mcp_servers()` returns a real array translated from the
materialized kiro agent spec (`acp/session_mcp.py`). The shape argument is
untouched: both are still undeclared overridable methods rather than a typed
extension point, which is what PR 3 lands.

### 2.7 A fourth backend landed after this RFC was drafted

§2.5 and §2.6 measure what a new backend costs. Codex is the third measurement in
that series and the most informative, because it is the first backend added
*after* the census above — so it is direct evidence on whether §2.3's and §2.5's
cost claims still hold. They half hold.

`ACP_BACKEND_CODEX = "codex"` (`acp_backends.py:48`) shipped in `d3e67b7e9`
(*wire Codex in behind an enforced tool-permission route*, #7963) and is in both
`ACP_BACKENDS_KNOWN` and `BASELINE_SELECTABLE_BACKENDS`, so a public build offers
it and serves sessions on it today. Dashboard copy saying what a Codex install
still cannot do followed in `6d1b51704` (#8684).

**What it did right, and what that costs §2.5.** There is no `if/elif` chain. It
added the `Routing` enum plus two identity→capability tables read through
accessors that fail closed on an unknown id, and — the part worth copying —
enforcement dispatches on the **mechanism**, not on the harness:
`acp_tool_gate.ENFORCED_ROUTINGS` (`:56`) holds `Routing` members, so
`routing_for(backend) in ENFORCED_ROUTINGS` (`:276`) is the entire test and a
fifth harness inherits the decision by declaring a mechanism. That is the shape
this RFC argues for, reached independently and before it. §2.5's "a third backend
pays the same price again, in the same places" is therefore too pessimistic about
the permission path — and §2.3's residual defect is exactly what is left over:
the tables are keyed on id, and consumers still read set membership.

**What it shows is still missing.** None of it is protocol coupling. All of it is
host contract and declaration.

- **It appeared in no bucket of the host contract.** Until this PR,
  `docs/system-specs/features/agent-host-contract.md` contained zero references to
  codex. That spec's own new-provider checklist says *"Silence is not an answer"*
  — and nothing enforced it. An undeclared host is precisely the failure §6 exists
  to make impossible, and it happened while this RFC sat open, which is the
  strongest argument in the document for §12.4 becoming code. The column and its
  ratchet land with this revision; the ratchet is the part that generalises.
- **Its capability gaps are frontend prose.** What a Codex install cannot do is
  said in dashboard copy behind a plain `if (value === CODEX)` chain, with nothing
  on the wire for the frontend to read. A capability the backend knows and the UI
  re-derives by string compare is the §2.3 defect one layer further out, where no
  Python-side gate can see it.
- **Its sessions get an empty MCP array.** `_codex_session_mcp_servers()`
  (`acp/client.py:3114`) returns `[]`, so **nothing is projected** onto a codex
  session — not a reduced set, and not Crew's own control plane. The only entries
  it can carry are the shared MCP gateway's broker stubs, appended for every
  backend alike and empty when that gateway is off, so with the gateway off the
  session has no tools at all.
  `providers/mirrors/`'s `NO_MIRROR` entry states this outright and calls it "a
  real user-visible state". It is §2.6's `_session_mcp_servers() -> []` row
  returning on a *selectable* provider, after the core had closed that hole for
  CC — which is the evidence that a neutral-return override is a hole in the
  contract and not a one-off.

The enforcement that was missing is landing with this revision: the host-contract
spec gains a Codex column and a parity test in the same PR as this document. The
mechanism is the point, not the column — a bucket left silent should fail a test.

Four places asserted the opposite of all this — that codex was a dormant seam no
build offered — and each used that as the *justification* for supplying nothing:
`acp_backends.py`'s comment on the id, `AcpClient._codex_session_mcp_servers`'s
docstring, the "dormant seam" comment on the `_is_codex` spawn branch, and the
`NO_MIRROR` rationale string in `providers/mirrors/registry.py`. All four are
corrected in this same PR, because a reader who believes them concludes the empty
MCP array costs nobody anything. `docs/system-specs/modules/providers.md` carried
the same retracted claim about the CC branch and is corrected with them.

## 3. Goals

1. Exactly one import path from application code to an agent backend:
   `kiro_crew.agent_sdk`. Enforced mechanically, with a baseline that can only
   shrink.
2. No ACP protocol shape crosses that boundary — no JSON-RPC ids, no raw ACP
   option dicts, no raw tool params, no multiplexing artifacts.
3. No consumer branches on a backend id. Consumers ask semantic capability
   questions, and where a backend *lacks* an operation they test for a protocol
   rather than reading a boolean (§5.3).
4. Agent process and session lifecycle state has a single owner, and
   `session_pid.py` imports no `kiro_crew.acp` or `kiro_crew.providers` name at
   module scope. There is no `session_pid` ↔ `worker_pool` cycle and never was
   (§2.4); the real one ran `session_pid.py:28 → providers.base → acp.types →
   acp/__init__ → acp.runtime → session_pid`, and it is closed. What remains of
   this goal is ownership: the kill primitives shared by the agent and work-class
   sweeps need a home.
5. The host contract is written down, per provider, with "not supported" as a
   valid declaration that degrades a Crew surface rather than being assumed away.
6. Adding a driver becomes: implement the protocols it can honour, declare a host
   profile, add no consumer edits.

## 4. Non-goals

1. **Not** making the adapters in #6307 work, or turning its descriptor into a
   behavioural interface. That is driver-internal and below this boundary.
2. **Not** building the host-contract seams. §6 and the host-contract spec record
   the contract; converting agent-spec writing, session replay, sandbox
   delegation or credit accounting into abstractions is separate work and out of
   scope — with two exceptions promoted into PR 3 because the CC review showed
   the boundary cannot be drawn without them (§7, PR 3).
3. **Not** adding a provider, and **not** re-adding a provider selector.
   `docs/system-specs/features/claude-code-provider.md` carries a standing rule —
   *"Do not re-add the registration glue or a provider selector"* — and `AGENTS.md`
   lists other providers under *Never re-add*. This RFC honours both: Claude Code
   appears here **only as evidence** of what a foreign host requires. Whether
   `agent.provider` ever becomes selectable is a question for
   `rfc-pluggable-model-providers.md`.
4. **Not** changing ACP wire behaviour, event kind string values, or the browser
   payload shape.
5. **Not** a rename-only change. A boundary that re-exports is what we already
   have.

## 5. Design

### 5.1 Layering

```
consumers        dashboard/  slack/  discord/  telegram/  messaging/
                 session.py  subagent.py  apps/  cli_*.py  workflows/
                        |
                        |  may import ONLY kiro_crew.agent_sdk
                        v
                 kiro_crew.agent_sdk          domain types, role protocols,
                                              capabilities, supervisor
                        |
                        |  resolves drivers through a registry
                        v
                 kiro_crew.agent_sdk.drivers.acp
                                              the ONLY module permitted to
                                              import kiro_crew.acp
                        v
                 kiro_crew.acp   (private)    wire, dialects, adapters,
                                              session handles, worker pool
```

If this goes red you introduced a boundary violation; fix the import direction,
do not relax the rule.

`kiro_crew.providers` becomes a thin deprecated shim during migration (§9) and
its **shim surface** is deleted at the end. Not the whole package: since v3,
`src/kiro_crew/providers/mirrors/` has become a real and growing layer —
`base.py`'s `AgentConfigMirror` with `Concern` / `Disposition` / `Ruling`,
`registry.py`'s `MIRRORS` / `NO_MIRROR`, `claude_code.py`, and a `README.md` —
with claude the only mirror and kiro, KAS and codex carrying explicit `NO_MIRROR`
reasons. That is load-bearing code, not an alias, and deleting `kiro_crew.providers`
wholesale would delete it. Where it lives after the boundary is drawn is an open
question (§12.5), and PR 6's deletion is scoped accordingly.

### 5.2 The SDK owns the types

**`AgentEvent`.** A new dataclass in the SDK, built by the driver from
`AcpEvent`. Field disposition:

| Disposition | Fields |
|---|---|
| Keep as-is | `kind`, `text`, `tool_call_id`, `title`, `tool_purpose`, `context_usage_pct`, `stop_reason`, `tool_input`, `tool_input_redacted`, `tool_output`, `usage`, `server_name`, `oauth_url`, `subagents`, `todo`, `is_shell`, `tool_name`, `mcp_server_name`, `diff_old_text`, `diff_path` |
| Replace | `request_id` → `approval: ApprovalToken \| None`; `options` → `choices: tuple[ApprovalChoice, ...]`; `tool_final` → `status: ToolStatus`; `tool_kind` → a domain enum |
| Do not cross **as fields** (consumed by the driver to compute the four derived members below) | `raw_tool_params`, `raw_params_trusted`, `shell_classified`, `mcp_identity_trusted` |
| Collapse | `runtime_global`, `sub_session_id`, `mcp_identity_ambiguous` → one `attribution: ChildAttribution \| None` value object, non-`None` only on subagent-related events (decided, §12.2) |

`AcpEvent` is not only fields. It also carries four derived `@property` members —
`shell_command` (`acp/types.py:586`), `child_low_fidelity` (`:640`),
`child_mcp_identity_trusted` (`:668`) and `child_unconditional_grant_eligible`
(`:711`) — every one of which is read outside `acp/` on the permission path, and
every one of which is computed from fields in the *Do not cross* row. They are
therefore **first-class SDK members** on `AgentEvent`, computed by the driver from
the ACP event before the raw fields are discarded, carrying their fail-closed
semantics unchanged (`child_low_fidelity` returns `True` on any missing
provenance, in the body at `types.py:640`; `hooks.py` denies a shell tool whose
command could not be recovered). Both citations shifted with the `types.py`
reflow — re-derive them rather than trusting a line number here.

That is why the fourth row reads *do not cross **as fields***. It does not mean
the values are unused: `raw_tool_params` is passed as a security-decision argument
at 20 sites in 12 non-ACP files — `hooks.on_tool_call(raw_params=…)`
(`chat_runner.py:6637`), `approval_command(raw_tool_params=…)`
(`:7235`, `:7242`), and so on — so it crosses as an opaque `Mapping | None` for
the duration of PRs 2-5 and is deleted per-file by PR 5 alongside its last
consumer. The other three are consumed by the driver only; their read count
outside `acp/` is zero, which is exactly why an earlier draft missed that four
properties depend on them.

Event **kind string values stay byte-identical** (`"text_chunk"`,
`"tool_call_update"`, `"end_turn"`, …). They are persisted and serialized; only
the Python symbol's home moves. The SDK re-declares them as its own constants and
the driver asserts equality with the ACP ones in a parity test.

**`ApprovalToken`.** An opaque, stable-serializable string minted by the SDK when
it emits a permission event, valid only for the live turn on the session that
minted it. Chosen over an integer handle or a structured object specifically so
`chat_runner.py:7487`'s `{"id": "..."}` payload to the browser does not change
shape. The driver keeps the private token → JSON-RPC id map. Consumers never see
a wire id again.

**Error taxonomy.** SDK-owned exceptions replacing the eight `Acp*` classes that
currently appear in 14 non-ACP modules: `AgentError`, `AgentAuthRequired`,
`AgentProcessDied`, `AgentTimeout`, `AgentBusy`, `AgentModelUnavailable`,
`AgentRuntimeDead`, `AgentRequestTimeout`, plus one addition the CC review
required — **`AgentRuntimeMissing`**, raised by `AgentSupervisor.preflight()` when
a declared auxiliary runtime cannot be resolved. Today a missing
`CLAUDE_CODE_EXECUTABLE` produces a warning log and then death at `session/new`
(`acp/client.py:2807-2820`); a declared requirement plus a preflight turns that
into a diagnosable refusal. `AgentAuthRequired` must remain distinguishable
because the readiness gate depends on it (§10.4).

**`SessionCapabilities`.** A frozen value read off the session. Named
`SessionCapabilities`, not `AgentCapabilities`, because the internal companion
already ships an unrelated module of that name — one that installs MCP servers,
skills and agent packages — and the collision would be genuinely ambiguous.

Each question is semantic, and each replaces a place that asks backend identity
today:

Every `acp/types.py:NNN` citation this table carried in v3 is dead: the sets
moved to `acp_backends.py` (§2.3), and the table was also short — it named five of
what are now 15 sets, so ten capability questions had no row. Both are fixed
below. All `:NNN` references in the middle column are `acp_backends.py`.

| Question | Asked today as |
|---|---|
| `can_steer` | `ACP_BACKENDS_STEER` membership (`:336`) |
| `multiplexes_sessions` | `ACP_BACKENDS_ACP_RUNTIME` (`:387`) — **post-session only**; see the pre-session query below |
| `shares_subagent_session` | `ACP_BACKENDS_SESSION_SHARING` (`:317`) — a *subset* of the above, which one boolean cannot express |
| `self_sandboxes` | `ACP_BACKENDS_INTERNAL_SANDBOX` (`:371`) |
| `recyclable_on_host_logout` | `ACP_BACKENDS_KIRO_IDENTITY_STORE` (`:407`) |
| `dispatches_members` | `ACP_BACKENDS_MEMBER_DISPATCH` (`:331`) |
| `can_compact` | `ACP_BACKENDS_COMPACT` (`:351`) |
| `model_via_config_option` | `ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION` (`:414`) |
| `effort_via_config_option` | `ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION` (`:423`) |
| `advertises_model_selection` | `ACP_BACKENDS_ADVERTISED_MODEL_SELECTION` (`:435`) |
| `seeds_local_settings` | `ACP_BACKENDS_SEED_LOCAL_SETTINGS` (`:445`) |
| `native_slash_commands` (host half) | `ACP_BACKENDS_KIRO_SLASH_COMMANDS` (`:479`) |
| `mcp_config_hot_reload` | `ACP_BACKENDS_MCP_CONFIG_HOT_RELOAD` (`:496`) |
| `injects_mcp_per_session` (wire half) | `ACP_BACKENDS_SESSION_MCP_ARRAY` (`:79`) |
| `tool_routing_mechanism` | `routing_for()` (`:573`) over `ACP_BACKEND_ROUTING` (`:548`) — an id lookup, not a session value |
| `permission_config_required` | `permission_config_for()` (`:578`) over `ACP_BACKEND_PERMISSION_CONFIG` (`:568`) |
| `bills_host_credits` | `bills_kiro_credits` membership |
| `reports_subagent_progress` | descriptor level |
| `activates_agent_by_mode` | `acp/client.py:3521` `if self._is_kiro:` |
| `native_slash_commands` | `providers/acp.py:1263` `if self.is_claude_backend:` |
| `reports_compaction_status` | a comment at `providers/acp.py:1297-1306` |
| `resume_needs_local_transcript` | `acp/client.py:3408` `if self._is_claude:` |
| `injects_mcp_per_session` | `acp/client.py:3313` / `:3434` |
| `advertised_ids_comparable` | `acp/client.py:2438` `if self._is_kiro and self._model_is_unusable(...)` |
| `substitutes_models_at_session_new` | `acp/client.py:3323` |
| `can_reset_config_default` | `providers/acp.py:1139` |
| `effort_applied_at_spawn` | `providers/acp.py:957` |
| `permission_mode_is_spawn_scoped` | the companion’s provider, itself `getattr(client, "_is_claude", False)` |
| `writes_own_transcripts` | `acp/client.py:3505` `if self._session_id and self._is_kiro:` |

Two of these deserve a note because an earlier draft got them wrong.
`recyclable_on_host_logout` was drafted as `own_identity_store`, which **inverts**
the meaning: the set records an *authorization* — that a `kiro-cli logout` may
retire this backend's live child — not ownership of a store. A CC session must
never be recycled on a Kiro logout, so the polarity matters. And
`shares_process` was one boolean over two sets that the comment above
`ACP_BACKENDS_ACP_RUNTIME` (`acp_backends.py:387`, formerly cited as
`acp/types.py:177-181`) documents as a superset relation; it is split above.

The last two rows are not session values and cannot become ones without a
decision. `Routing` and the two dispatch tables are *mechanism* facts keyed on
backend id, and `acp_tool_gate.py` reads them from outside `agent_sdk`. Where they
land is PR 3's to settle (§7).

**Not every backend question is a session question, and one of these is not.**
`SessionCapabilities` is read off a live session, which makes it the wrong home for
a question asked in order to decide *how to build* that session.
`ACP_BACKENDS_ACP_RUNTIME` is exactly that case: a helper in `session.py`
(spelled `_bg_runtime_backends()` at `:511` today) computes
`ACP_BACKENDS_ACP_RUNTIME & selectable_backends()` per call, and its callers
consult the result against the configured `agent.acp_backend` **string** to choose
between the multiplexed-runtime path and the provider-backed `_Session` path
serialized by `Semaphore(1)`. v3 cited three fixed line numbers here
(`session.py:399-400`, `:1398`, `:1610`); `#6921` and later splits moved them, and
some reads now sit in `session_allocation.py` / `session_pool.py`, so PR 3 must
re-derive the call sites rather than work from that list. No value read off the session can answer it, because
the session does not exist yet.

It is also not a boolean. The second operand, `selectable_backends()`
(`acp_backends.py:200`), is a **deployment** fact resolved after module import when
an edition registers backends during boot — deliberately not frozen at import
time. The intersection is the answer; collapsing it to one boolean reintroduces the
"silently hands the capability to a third backend" failure the opt-in frozensets
exist to prevent.

So the pre-session half lives on the **driver registry**, not on the session:

```python
def spawnable_multiplexed_selections() -> frozenset[str]:
    """Backend selections the SDK can serve with a multiplexed runtime.

    Runtime-capable AND operator-selectable. Computed per call, never frozen at
    import: selectability is a deployment fact an edition adds during boot.
    """
```

`multiplexes_sessions` stays on `SessionCapabilities` for the post-session
question (the existing spelling, `providers/acp.py:466-476`, needs a started
process, which is what makes it post-session). A registry function was chosen over
an `AgentSupervisor.can_multiplex(selection) -> bool` method for two reasons: the
answer is genuinely set-valued, so a boolean accessor throws away the shape at
every call site; and the fact being reported is about the deployment, not about any
supervisor instance, so hanging it off the supervisor would imply per-instance
variation that does not exist.

`cli_doctor.py` compares `== ACP_BACKEND_KAS` against a config load with no
session, and is the same pre-session shape. Both it and `session.py`'s reads route
through this function; §7 records which phase pays each off.

### 5.3 The SDK surface: presence-tested role protocols, not a flat interface

`LLMProvider` has 33 members; `AcpSessionProvider` implements roughly 60 plus 11
underscore-prefixed AcpClient-parity shims (`_model`, `_work_dir`, `_pid`,
`_child_pids`, `_start_time`, `_drain_post_compaction_metadata`, …). The SDK
surface must be **smaller** than that, and it must not be flat.

An earlier draft of this section proposed four mandatory protocols with
capabilities as booleans beside them. Checking that draft against Claude Code
found the flaw: **capabilities gate behaviour, but not method presence.** A
foreign host's profile is not "kiro-cli minus a few flags" — it is a set of
absences that change control-flow *shape*. No `set_mode` removes the home of a
fail-closed privilege check (`acp/client.py:3513-3536`). No compaction
notification inverts a two-call API into one. No `commands/execute` deletes a
method the draft never drew at all, though `acp/client.py:4860-4920` is real
surface with a real degradation branch at `providers/acp.py:1263-1268`. Under a
flat mandatory interface every absence becomes an implement-and-raise stub, and
the `not is_claude` inference the frozensets were built to kill reappears at the
SDK boundary where consumers can no longer see it.

So: **the mandatory core is small, every optional operation is its own
`runtime_checkable` protocol, and a consumer tests for the protocol rather than
reading a flag.** Each capability question in §5.2 that corresponds to an
operation gates a protocol, not a branch.

Mandatory:

| Protocol | Members |
|---|---|
| `AgentSession` | `submit(message) -> AsyncIterator[AgentEvent]`, `cancel`, `approve(token, *, always=False)`, `reject(token)`, `has_active_turn`, `wait_turn_done`, `is_process_alive`, `new_conversation` |
| `AgentSessionInfo` | `session_id`, `served_model`, `available_models`, `effort_levels`, `context_usage`, `capabilities` |

Optional, presence-tested:

| Protocol | Members | Gated by |
|---|---|---|
| `AgentSteerable` | `steer`, `last_steer_monotonic` | `can_steer` |
| `AgentCompactable` | `compact() -> CompactionResult` | — |
| `AgentCompactionReporting` | `wait_for_compaction` | `reports_compaction_status` |
| `AgentCommandable` | `send_command`, `stream_command` | `native_slash_commands` |
| `AgentModeSwitchable` | `set_mode` | `activates_agent_by_mode` |
| `AgentSessionConfig` | `set_model`, `set_config_option`, `supports_config_option`, `reset_config_option() -> bool` | `can_reset_config_default` |

`compact()` returns a `CompactionResult` rather than being a two-call
`compact` + `wait_for_compaction` pair, because the two-call form encodes Kiro's
asynchronous model. On a host that compacts synchronously inside `session/prompt`
no status notification ever arrives, `providers/acp.py:1378-1386` leaves the
result unset, and the wait can only time out. A driver that *does* report status
additionally implements `AgentCompactionReporting`.

`set_mode` is deliberately **not** a live setter in `AgentSessionConfig`. It is
step 4 of session initialization and it carries a fail-closed privilege check
(`acp/client.py:3513-3536`), so agent activation belongs to `create_session`;
`AgentModeSwitchable` exists only for hosts that can also switch mid-session. A
driver whose host cannot activate an agent by mode must declare
`activates_agent_by_mode = False`, and the SDK must refuse to *silently* widen
privilege when it is absent.

`AgentSupervisor` (mandatory for a driver, not per session):

| Member | Note |
|---|---|
| `preflight() -> None` | Resolves the entry point and every declared auxiliary runtime; raises `AgentRuntimeMissing`. Runs before a session is attempted. |
| `create_session(request) -> AgentSession` | May return a session whose served model differs from the requested one; the substitution is reported as an event (`acp/client.py:3313-3332`). |
| `destroy_session`, `cleanup_session` | `cleanup_session` is a real member, not a shim: on a host that writes its own transcripts it must delete them (the companion does exactly this today). |
| `adopt_session` | Ownership transfer of a live session. |
| `persist_permission_mode(mode)` | Spawn-scoped on hosts where auto mode is a file consumed at the *next* spawn, hence a supervisor concern rather than a session setter. |
| `health`, pool operations | §5.4. |

`SessionRequest` is one frozen record, replacing the 19-kwarg
`AcpProvider.__init__` and the two `_acp` closures in `config/loader.py`. Its
field list is taken from the only factory that has been exercised against a
foreign vendor (the companion’s session factory) rather than
invented: `session_key`, `agent`, `channel_id`, `cwd`, `extra_env`, plus a
base-versus-override distinction the flat draft lost —
`model` / `model_override`, `effort_per_model` (a mapping, not a scalar) /
`reasoning_effort_override`, and
`permission_mode` / `permission_mode_override`. Two fields the CC review found
missing: `resume_session_id`, and a declared per-session `mcp_servers` extension
point so injecting servers on the wire is a contract rather than a `getattr`
override.

The 11 private shims stay out. Exactly one of them
(`_drain_post_compaction_metadata`, reached by `getattr` at
`providers/acp.py:1474`) has a cross-package caller today, and it is inside the
would-be driver — so nothing outside loses access.

### 5.4 The supervisor owns process and session lifecycle

Moved into the SDK: the warm pool (`session.py:1108`), the shared background
runtime (`:1136`), the per-parent subagent runtime map (`:1153`), the RSS
watchdog threshold and its settings loader (`:1162`), provider adoption
(`session.py`), and agent-process PID tracking, sweeping and reaping (currently
`session_pid.py`).

This is the step that makes the boundary real. Without it `SessionManager` still
holds ACP's guts and the SDK is decoration. It also settles §2.4: **the
supervisor owns process supervision**, and `session_pid.py`'s agent-process half
moves in.

What that has to accomplish is the cycle `session_pid → acp.client → session →
session_pid`, whose two live sites are `session_pid.py:311` and `:1888` (both a
lazy `from kiro_crew.acp.client import _get_child_pids`). Deleting the
`worker_pool.py:49` guard is a *consequence* of the move, not the proof it worked —
that edge is one-way and the guard exists for standalone importability (§2.4). The
move must therefore place two pieces of shared surface: `_get_child_pids`, or a
PID-enumeration primitive it can be replaced by, so `session_pid.py` retains no
`kiro_crew.acp` import at any scope; and `_kill_pid_tree` (`session_pid.py:300`)
plus the `_MANAGED_AGENT_MARKERS` vocabulary (`:229`), both of which are consulted
from the agent half and the work half. The non-agent sweeps gate their kill sets
*negatively* on that marker vocabulary (`:1454`, `:1561`) and consume the protected
set through `_collect_active_pids` (`:267`), so a careless split makes the
work-class sweep less safe, not merely less tidy.

Deliberately *not* moved: `SessionManager`'s slot/transcript/channel
responsibilities. The supervisor takes the agent-process concerns only.

### 5.5 What stays inside the driver

Wire dialect, argv resolution, adapter descriptors and admission gating, model-id
translation and downgrade, per-adapter quirks, the permission **option**
vocabulary and its per-request `optionId` echo, protocol-version selection,
credential scrubbing on spawn, and the KAS-vs-kiro-cli branches now in
`runtime.py` / `session_handle.py`. #6307's registry and `BackendDescriptor` live
here untouched.

## 6. The host contract

The coupling counted in §2.1–2.4 is ACP-protocol coupling. The coupling counted in
§2.6 is something else: `kiro-cli` appears in 193 files, and most of those
references are not about the protocol. They are about a **host** — its filesystem
layout, agent format, session store, credential store, sandbox posture, MCP
delivery channel, billing surface, permission engine, and the extra runtimes it
cannot find for itself.

The full contract, with all four backends side by side and every "must declare"
line, is
[`../system-specs/features/agent-host-contract.md`](../system-specs/features/agent-host-contract.md).
This section states only its shape and the two conclusions that bind this RFC.

That spec was written against three backends and Codex is absent from every one of
its buckets (§2.7). It gains a Codex column and a parity test in the same PR as
this revision — the test being the part that matters, since the spec's own rule is
that silence is not an answer and nothing was enforcing it.

### 6.1 Eight of the nine buckets, and who proves each one is provider-scoped

| Bucket | The divergence that proves it is not universal | Proven by |
|---|---|---|
| 1 Agent definition and layout | Markdown-with-frontmatter in a different directory, no `--agent`, **no `set_mode` at all**. Codex: no spec projection at all (`NO_MIRROR`), and model plus effort arrive as ACP session config options (`ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION`, `_EFFORT_VIA_CONFIG_OPTION`) rather than in a definition | CC, Codex |
| 2 Session persistence | A foreign transcript store keyed by an encoded `realpath(cwd)`, a path-less `session/load`, in-band synchronous `/compact`, one session per process | CC |
| 3 Identity and auth | Its own sign-in and its own credential command; a host logout must **not** retire its children | CC |
| 4 Sandbox | No internal sandbox, so Crew's own wrap must stay — the one membership set that fails *open* | CC |
| 5 MCP server injection | Reads no file; servers must ride `session/new` **and** `session/load`, in a different shape. Codex: nothing is projected — `_codex_session_mcp_servers()` returns `[]`, so a session mounts zero tools (§2.7) | CC, Codex |
| 6 Usage, billing, credits | Dollars per token instead of host credits | CC |
| 7 Security and permission parity | A native permission engine upstream of and invisible to the host gate; a different option vocabulary with a real `reject`; auto mode as a per-session file. Codex: asks only under an applied `("mode", "read-only")` config option, with a residual read gap ACP v1 cannot close | CC and KAS, Codex |
| 8 Auxiliary runtimes | A second native binary the adapter's own SDK will not find | CC |

The table covers eight buckets, not the contract's nine: bucket 9 (tool-result
marker fidelity) is absent because it is not proven by a *divergence* — it is the
one bucket already enforced by a ratchet, so it needs no argument here.

Codex's remaining buckets — 2, 3, 4, 6, 8 and 9 — are **unaudited**, and the
contract's Codex column says so in each rather than implying the blanks are "same
as Kiro". Filling them is the companion change named above.

KAS diverges on agent projection, permission vocabulary, prompt resolution and
MCP projection, but it is Kiro's own service and therefore shares the identity
store, the runtime, steer and the model vocabulary. **CC is the column that
matters**, and it is the reason this RFC treats the host contract as a first-class
artifact rather than a footnote. Codex is the column that proves the contract
needs a *test*: it was added, shipped and made selectable without a single bucket
being answered.

### 6.2 The parity rule

When a foreign host lacks an enforcement **mode** rather than a rule, parity
cannot be reached by translation. kiro-cli's 42 "suspicious bash" patterns are
audit-only; CC has no audit-only mode, so they are deliberately not translated and
the gap is recorded as a known security gap
(the companion records it as such). The honest contract is therefore
**a declared capability plus a documented gap**, never a silent downgrade. §5.3's
presence-tested protocols exist so that a declaration of absence is visible in the
type system instead of arriving as a no-op.

### 6.3 Seam maturity, and what that means for scope

The buckets differ by orders of magnitude. Usage/billing is already a boolean
flag read by consumers. Permission vocabulary has a genuine shared seam
(`acp/kas_permissions.py`, used by both the wire projection and the on-disk writer
so they cannot drift). Agent definition is half-sealed: `acp/kas_agents.py` is a
real projection, but the *writer* has none. Session persistence, MCP injection,
regex-engine parity and auxiliary runtimes have **no seam at all**.

Those last four are also, precisely, CC's hardest requirements. §4.2 keeps
host-contract seam-building out of scope, with two exceptions promoted into PR 3
because the CC review showed the boundary cannot honestly be called drawn without
them: **per-session MCP injection** and **transcript ownership**. The rest stay
documented-only, and PR 6's exit criterion is worded so that "sealed" means the
import boundary, not the host contract (§7).

## 7. Migration plan: six stacked PRs

Each phase is independently shippable and independently abandonable.

### PR 1 — declare the boundary and ratchet the inventory — **LANDED**

Shipped as described, with one deviation worth recording. `ci.yml:440-441` runs
`--test` and then the gate; `.github/agent-sdk-boundary-baseline.txt` was seeded
at **58 files / 107 edges** across both roots; the architecture tests landed as
four `test/test_agent_sdk_*.py` files.

The deviation: PR 1 proposed `src/kiro_crew/agent_sdk/` with "the layer docstring
and nothing else" and "no code moves". The package now also carries
`drivers/acp.py`, `backend_install.py`, `backend_identity.py`,
`provider_identity.py` and `native_commands.py`. That is not drift to be corrected
— PR 2's work has partly started inside a package the gate already exempts — but
it does mean the boundary's contents are no longer described by this plan, and
PR 2's first job is to reconcile what is there with §5.2.

**The gate watches two roots, not one.** `kiro_crew.acp` is the obvious one;
`kiro_crew.providers` is the one that makes the number honest. `providers/base.py`
re-exports `LLMEvent` and the `EVENT_*` constants, so a consumer can depend on
ACP shapes without naming ACP. A gate watching only `acp` would let a file look
migrated while nothing decoupled, and the baseline would fall for free.

**The baseline is derived, never transcribed.** `--seed-baseline` writes the first
file from a live scan and refuses to overwrite an existing one, because re-seeding
is exactly the "regenerate and absorb every new offender" move the missing-file
error exists to prevent. `--update-baseline` only lowers counts and deletes lines.
An earlier draft of this section hardcoded "68 edges across 42 files"; that number
came from a regex whose `kiro_crew\.acp` prefix also matched
`kiro_crew.acp_backends`, a sibling leaf module that imports no ACP at all. The
AST scan is authoritative, which is why §2.2 now points at the baseline instead of
reprinting a table — v3 disowned the figure here and printed it there.

Exit criteria, as verified on the merged tree:

- Verified: `--test` plants one probe per rule family (plain, module, relative,
  multi-line, `TYPE_CHECKING`-only, dynamic `import_module`, `__import__`, and the
  `providers` re-export channel) plus clean probes for the SDK, an unrelated
  `kiro_crew` module, and the `acp_backends` prefix neighbour — and runs first in
  the same CI step (`ci.yml:440`).
- Verified: the gate exits 0 on the seeded tree, and non-zero when an import of
  either forbidden root is added to any file under `src/`.
- Verified: the seeded baseline records what the scan found — 58 files, 107 edges
  — and the gate prints the split per root, so both halves of the migration are
  visible on every run.
- Verified: `--seed-baseline` refuses when the baseline already exists.
- Verified: the architecture test pins the exempt set to exactly the three
  boundary trees, fails when a baseline entry has been paid off but not pruned,
  and asserts the scan visited a non-trivial number of consumer files so a broken
  walk cannot read as a clean tree.
- Verified: `./scripts/docs-lint.sh` passes and the host-contract spec is
  reachable from `docs/system-specs/features/README.md`.

### PR 2 — the SDK owns the types

`AgentEvent`, `ApprovalToken`, `ToolStatus`, `CompactionResult`,
`SessionCapabilities`, the error taxonomy including `AgentRuntimeMissing`, and the
driver translation. `providers/base.py` stops aliasing `AcpEvent`.

**PR 2 is additive only if `AgentEvent` carries deprecation shims, and it must.**
An earlier draft claimed consumers "keep working through the deprecated shim"
while also removing `request_id`, `options`, `raw_tool_params`, `tool_final` and
`tool_kind` from the event. Those two cannot both be true: a shim can re-export a
*name*, it cannot keep a *deleted field* readable. Measured on `d7b7d65c3`,
outside `acp/` and `providers/`:

| What PR 2 changes | Consumer reads today |
|---|---|
| `request_id` → `approval: ApprovalToken` | **131** event-shaped reads across **17 files** |
| `approve_tool` / `reject_tool` signatures | **87** call sites |
| `tool_kind` → a domain enum | 52 |
| `raw_tool_params` → does not cross | **20** call-argument sites in **12** files |
| `options` → `choices` | 6 |
| `tool_final` → `status` | 2 |

The 17 files include `dashboard/chat_runner.py`, `subagent.py`,
`slack/handler.py`, `slack/gateway.py`, `messaging/driver.py`,
`messaging/renderer.py`, `channel.py`, `task_executor.py`, `task_planner.py`,
`llm_helpers.py`, `cli_chat.py`, `eval/runner.py` and `eval/judge.py`. Landing the
removal in PR 2 would make it a 17-file breaking change, which contradicts the
four-wave consumer migration this plan puts in PR 5.

So PR 2 ships `AgentEvent` with a deprecated read-only member for each field it
removes, each emitting a `DeprecationWarning`. Two mechanisms, not one, because the
disposition table has two kinds of removal:

- A **Replace** field has an SDK successor, so its shim is *derived*: `request_id`
  from `approval`, `tool_kind` from the domain enum, `status` from `tool_final`.
- A **Do not cross** field has no successor — that is what the row means — so a
  derived property is impossible. `raw_tool_params` therefore ships as a
  carried-through field, deprecated by warning only, and is deleted in PR 5
  alongside the consumer that reads it. The other three (`raw_params_trusted`,
  `shell_classified`, `mcp_identity_trusted`) have zero consumer readers and are
  consumed by the driver to compute the four derived members of §5.2, so they do
  not need a shim at all.

The four derived members are not shims. `shell_command`, `child_low_fidelity`,
`child_mcp_identity_trusted` and `child_unconditional_grant_eligible` land as
first-class SDK members computed by the driver (§5.2), because they are read on the
permission path and losing them is not a deprecation — it is a security regression.
Dropping `raw_tool_params` without them makes `shell_command` return `None` on
`tool_call` events, every such shell call then hits `hooks.py:539`'s
deny-by-default, and the `use_aws` regression `types.py:596-605` exists to prevent
comes back.

PR 5's waves delete the shims per file as each consumer moves, and PR 6's exit
asserts none remain. The "no `request_id` on `AgentEvent`" criterion therefore
belongs to PR 6, not here.

- Exit: `grep -rn "AcpEvent" src/kiro_crew` outside `acp/` and the driver returns
  zero hits.
- Exit: a parity test asserts every SDK event-kind and stop-reason string equals
  its ACP counterpart.
- Exit: `approve`/`reject` accept an `ApprovalToken`, and the browser payload at
  `chat_runner.py` is byte-identical before and after.
- Exit: every deprecated member is covered by a test asserting both the value it
  derives (or carries) and the `DeprecationWarning` it raises, so PR 5 can delete
  each one against a known contract.
- Exit: a test asserts each of the four derived members returns the same value on
  `AgentEvent` as on the source `AcpEvent`, **including the fail-closed branches**:
  `raw_params_trusted` False, `shell_classified` False, and `is_shell` with an
  unrecoverable command. A parity test over fields only would pass while these
  four silently invert.
- Exit: no consumer file changes in this PR. If one has to, the shim set is
  incomplete.
- Exit: a test asserts no name exported from `agent_sdk` **is** its ACP
  counterpart object. The import gate exempts `agent_sdk/` wholly, so the one
  channel it cannot see is a verbatim re-export *inside* the SDK — precisely the
  `providers/base.py` aliasing (`LLMEvent = AcpEvent`) that made two forbidden
  roots necessary in the first place. A consumer importing `agent_sdk` would read
  as migrated while holding the ACP object, and the baseline would shrink for
  free. Identity (`is`), not equality: a parity test over field values passes on
  the same object. PR 1 cannot carry this test, because the package it guards is
  empty until this PR populates it.
- Blocked on: nothing. §12.2 settled the attribution shape (`ChildAttribution`
  value object).

### PR 3 — consolidate the capability mechanism into the SDK, and close its gaps

**This phase was scoped as "build the capability mechanism". It is not that any
more.** The mechanism exists (§2.3): `acp_backends.py` owns 15 `ACP_BACKENDS_*`
frozensets, the `Routing` enum, `ACP_BACKEND_ROUTING`,
`ACP_BACKEND_PERMISSION_CONFIG`, `routing_for()` and `permission_config_for()`;
`acp_tool_gate.py` owns `ENFORCED_ROUTINGS` and the adapter credential mask. Both
are leaves that import no ACP, and both sit **outside** `agent_sdk`. PR 3's job is
to consolidate them behind the boundary and close the two gaps §2.3 names.

So this PR lands `SessionCapabilities` on every session and the presence-tested
role protocols of §5.3 as `runtime_checkable` — but as a *translation* of an
existing vocabulary, not a new one. Each of the 15 sets is either mapped to a
semantic question on `SessionCapabilities`, mapped to a pre-session registry query
(the `ACP_BACKENDS_ACP_RUNTIME` shape, below), or explicitly declared
driver-internal. A set left unmapped is an unanswered question, not an omission to
discover in PR 5.

`Routing`, the two dispatch tables and `acp_tool_gate.py` need a decision this PR
cannot dodge, because they are the part that is keyed on backend id. Three options,
in the order this RFC prefers them: move the tables into `agent_sdk` and have each
driver declare its own routing mechanism; keep them where they are and have the SDK
re-export the accessors, which leaves the id key intact; or leave `acp_tool_gate.py`
out of the boundary entirely as a policy module above it. Pick one and record it —
the current state, where a policy module reads a leaf's id tables and neither is
inside the boundary, is not a resting place.

It also lands the two promoted host-contract contracts: a declared per-session
`mcp_servers` extension point on `SessionRequest`, replacing the
`_session_mcp_servers()` override hole (the core now implements that method for
CC, so what PR 3 removes is the untyped override seam, not the behaviour — and
`_codex_session_mcp_servers()` returning `[]` is the same hole still open, §2.7),
and `writes_own_transcripts` + `AgentSupervisor.cleanup_session` as the declared
home of transcript ownership.

`ACP_BACKENDS_ACP_RUNTIME` is asked **before a session exists**, against a config
string, to choose which path builds the session — so no `SessionCapabilities` value
can answer it. §5.2 puts the pre-session half on the driver registry as
`spawnable_multiplexed_selections() -> frozenset[str]`, and this PR routes the
`session.py`-side reads and `cli_doctor.py`'s through it. Those call sites moved
with `#6921` and later splits, so PR 3 re-derives them rather than working from a
line list. Rewriting them to stop *dispatching* on the answer is PR 4's move; PR 3
only changes where the answer comes from, which is why the exits record those files
as carried rather than clean.

- Exit: `ACP_BACKEND_*` constants and `ACP_BACKENDS_*` sets are read only inside
  `agent_sdk/` and whichever module PR 3's decision leaves owning the tables.
  **The v3 exit — "zero `ACP_BACKEND_*` imports outside `acp/` and the driver" —
  is deleted as vacuous:** the constants left `acp/` entirely, so a consumer
  importing them from `acp_backends.py` satisfies it while changing nothing.
- Exit: the six surviving identity checks outside the boundary are gone, each
  replaced by a capability question, not relocated:
  `config/loader.py:3896` (`== ACP_BACKEND_CLAUDE` off a config load),
  `dashboard/chat_handlers.py:5662` (`provider.is_claude_backend`),
  `dashboard/chat_runner.py:972` (`getattr(client, "is_claude_backend", False)`),
  `:9281` and `:9701` (`is_claude_backend(client)`), and
  `knowledge/llm_pool.py:332` — which reads `AcpClient`'s **private** `_is_claude`
  from outside the package and is the sharpest of the six.
- Exit: every one of the 15 `ACP_BACKENDS_*` sets has a recorded disposition —
  semantic question, pre-session registry query, or driver-internal — and a test
  fails when a new set is added without one.
- Exit: `Routing` / `ACP_BACKEND_ROUTING` / `ACP_BACKEND_PERMISSION_CONFIG` have a
  named home, and `acp_tool_gate.py`'s relationship to the boundary is stated
  rather than incidental.
- Exit: Codex is covered. `routing_for()` and `permission_config_for()` fail closed
  today, so the capability translation must preserve that: an id the tables do not
  name resolves to refuse, never to a neighbour's mechanism.
- Exit: the pre-session multiplex query is reached only through
  `spawnable_multiplexed_selections()`, and no consumer computes
  `ACP_BACKENDS_ACP_RUNTIME & selectable_backends()` itself.
- Exit: every optional operation is reached through an `isinstance` protocol test,
  and no consumer calls a method that a driver implements only to raise.
- Exit: a test asserts each capability question in §5.2 has exactly one consumer
  spelling, so a second `not is_claude`-shaped inference cannot reappear.
- Blocked on: PR 2.

### PR 4 — the supervisor takes the lifecycle — **import cycle half LANDED**

**The cheap half is done and this section is narrowed.** v3 scoped PR 4 as moving
the warm pool, background runtime, per-parent runtime map, RSS watchdog, adoption
and agent-process PID tracking into `agent_sdk`, and named the cycle it was
breaking as `session_pid` ↔ `worker_pool`. Both were wrong (§2.4): three of the
four lifecycle concerns already sit in neutral modules that `session.py` merely
forwards to, and there is no `session_pid` ↔ `worker_pool` cycle. The real cycle
was `session_pid.py:28 → providers.base → acp.types → acp/__init__ → acp.runtime
→ session_pid`, it was a hard `ImportError` rather than a comment, and it is
closed: that import is deleted outright — `_sync_kill_provider`'s parameter is
`object`, since every read it makes is a `getattr` against a private attribute the
ABC does not declare — `session_compaction.py`'s `LLMProvider = Any` stub is
deleted, and `test/test_agent_lifecycle_cycle.py` pins it in a fresh interpreter.
The baseline shrank 107 → 106 as a result, so this is recorded progress rather
than an exemption.

What is left for PR 4 is ownership, which the cheap fix does not settle:
`session_pid.py`'s agent half moves behind the supervisor, `preflight()` lands
here, and `session.py` stops dispatching on the pre-session multiplex answer at
all because the construction choice moves with the pool. The move must place
`_get_child_pids` — or replace it with a PID-enumeration primitive the SDK owns —
and place `_kill_pid_tree` plus `_MANAGED_AGENT_MARKERS`, both consulted by the
work-class sweep as *safety* input, not merely as shared code.

- Exit: `session.py` declares no `AcpRuntime`-typed attribute, and the forwarding
  properties onto `session_pool.py` / `session_background.py` /
  `session_allocation.py` / `session_cleanup.py` either move with the pool or are
  stated as staying, deliberately.
- Exit: **`session_pid.py` imports no `kiro_crew.acp` or `kiro_crew.providers`
  name at module scope, proven by `test/test_agent_lifecycle_cycle.py` — a
  standalone import in a fresh interpreter plus a zero-leaked-ACP-modules
  assertion — not by a grep.** This criterion is already met, and it replaces v3's,
  which was factually wrong: deleting the two lazy `acp.client` imports would not
  have closed the real cycle, because a lazy in-function import runs after both
  modules are initialized.
- Exit (tidy-up, not a cycle proof): `acp/worker_pool.py` contains no
  `from kiro_crew.session_pid import`. v3 listed this as cycle evidence; the edge
  is one-way and the guard exists for standalone importability (§2.4), so it
  certifies nothing about the cycle and should not be read as doing so.
- Exit: the remaining lazy `from kiro_crew.acp.client import _get_child_pids`
  calls (`session_pid.py:323`, `:2504`) are gone — this is a *coupling* criterion
  now, not the cycle criterion.
- Exit: `dashboard/session_memory.py` imports no underscore-prefixed ACP names
  (`_get_rss_tree_mb`, `_iter_descendant_pids` today).
- Exit: `dashboard/stall_enrichment.py` imports no `kiro_crew.acp` name at all.
  The underscore form of this criterion passes today — its only ACP import is
  `acp.liveness.socket_inodes` — so it certifies nothing; the `/proc` primitive has
  to move behind the SDK or into a shared module for this to go green.
- Blocked on: PRs 2-3 for the ownership move. §12.1 settled PID ownership on the
  supervisor, so this phase is design-unblocked, and its import-cycle half has
  already shipped independently of them.

### PR 5 — consumer migration waves

Four waves, each driving the ratchet down. The waves are defined by the baseline,
not by category, because a category list is how a consumer ends up owned by nobody:

1. **dashboard** — `dashboard/` and its handlers.
2. **messaging and the channels** — `messaging/`, `slack/`, `discord/`,
   `telegram/`, `teams/`, `webex/`, `wecom/`, `whatsapp/`, `imessage/`.
3. **apps, workflows, knowledge** — `apps/`, `workflows/`, `knowledge/`.
4. **CLI, config, platform, and the top level** — `cli_*.py`, `config/`,
   `platform/`, `eval/`, `connections/`, and the top-level modules. This wave is
   the named catch-all and the largest: `session.py` and the five modules
   `#6921` split out of it (`session_allocation.py`, `session_background.py`,
   `session_cleanup.py`, `session_compaction.py`, `session_pool.py`),
   `session_pid.py`, `subagent.py`, `channel.py`, `llm_helpers.py`,
   `task_executor.py`, `task_planner.py`, `cli_doctor.py` and the rest of the
   top-level files. `channel.py` is wave 4, not wave 2 — it is a top-level
   module, and the "seven channels" of wave 2 are the transport packages.

Deliberately no edge counts here. The gate prints the live per-root split on
every run and the baseline is the per-file record, so a count written into this
document is a second source of truth that goes stale on the next upstream
refactor — `#6921` moved eight ACP edges out of `session.py` into five new files
between this RFC being merged and PR 1 being raised, which is exactly how.

Every baselined path must fall in exactly one wave; a path that
falls in none is a defect in this list, not a file to be improvised over.

Each wave is its own commit and can ship alone.

Each wave does two things per file, not one: route the dependency through
`agent_sdk`, and delete the `AgentEvent` deprecation shims that file was the last
reader of (PR 2). A wave that moves the import but leaves a shim alive has not
finished — the shim is what let PR 2 be additive, and it is dead weight from the
moment its last consumer is gone.

Both halves of the baseline move here. The `kiro_crew.providers` edges are not a
PR 6 problem: roughly two fifths of the recorded edges reach the shim rather than
the ACP package, spread across more files than the `acp` half. If the waves only
chase `kiro_crew.acp`, PR 6 arrives with that whole half still pointing at a
package it is supposed to delete. The gate prints the live split on every run —
read it there rather than from this paragraph.

One spelling deserves naming because it is easy to miss: `config/loader.py`
constructs `AcpProvider` directly, and that construction **is**
`create_provider_factory`. Other files import the name without constructing it,
so "route it through the factory" is already true — what PR 5 has to change is
which module they import the name *from*, not how they build it.

- Exit: after each wave the baseline shrinks and never grows.
- Exit: each wave's commit reduces the per-root split the gate prints, and by the
  end of wave four the `kiro_crew.providers` half is at zero.
- Exit: `mcp_tools/spawn.py` remains at zero, unmodified.
- Blocked on: PRs 2-4.

### PR 6 — seal the import boundary

The baseline reaches zero and is empty. **The driver never appears in it.** The
gate exempts by directory prefix and `_scan` skips exempt files, so
`agent_sdk/drivers/acp.py` is carried by the exempt set, not by a baseline
count — and PR 1's own test already pins that
(`assert gate._is_exempt("src/kiro_crew/agent_sdk/drivers/acp.py")`). A
hand-written baseline entry for it would fail
`test_every_recorded_violation_still_exists` and be erased by
`--update-baseline`.

`kiro_crew.providers`'s **shim surface** is deleted — `base.py`'s aliases and
`EVENT_*` re-exports, `acp.py`'s provider class and its `is_claude_backend` /
`provider_label` helpers — and with it the `providers/` entry in the gate's exempt
set. The exemption was always temporary and the architecture test pins the set, so
forgetting to remove it fails a test rather than silently widening the boundary.

**`providers/mirrors/` is not part of that deletion and PR 6 cannot start until it
has a home.** It is a live agent-config projection layer, not a shim (§5.1), so
there are only two honest orders: relocate it first — `agent_sdk/mirrors/` is the
obvious candidate, since a mirror projects a spec for one host and that is driver
work — or delete only the shim modules and leave `providers/` as a package whose
sole remaining contents are mirrors, in which case the exempt set keeps a
`providers/mirrors/` prefix and this PR's "reduced to two trees" claim is wrong as
written. Decide in §12.5; do not let PR 6 discover it.

The exempt set is reduced to **two** trees, `agent_sdk/` and `acp/`, not to one
module. `acp/` stays exempt because 30 of its own internal imports across 8 files
would otherwise become unbaselined violations with no legal remedy — the baseline
header forbids adding a line to make a red gate green. A prefix naming the driver
file directly also fails `test_every_exempt_prefix_points_at_a_real_tree`, which
asserts `.is_dir()`.

Specs updated in the same commit per `docs/README.md`.

"Sealed" here means the **import** boundary. The host contract is not sealed by
this PR and must not be described as such: four of its nine buckets still have no
seam, and the spec doc names them.

- Exit: `.github/agent-sdk-boundary-baseline.txt` is empty (header only), and the
  gate's exempt set is exactly the trees §12.5's mirrors decision leaves standing
  — `agent_sdk/` and `acp/` if mirrors relocated, plus `providers/mirrors/` if it
  did not — with the pinned exempt-set test updated in the same commit.
- Exit: `providers/mirrors/` has a stated home and no code in it was deleted by
  accident along with the shim.
- Exit: no `request_id`, `options`, `raw_tool_params`, `tool_final`, `tool_kind` or
  `runtime_global` attribute survives on `AgentEvent`, and no deprecated property
  from PR 2 remains.
- Exit: `docs/system-specs/modules/providers.md` and `acp-client.md` describe the
  boundary as built.
- Exit: the host-contract spec's seam-status table is re-audited in the same
  commit, and every bucket still lacking a seam is stated as open.
- Blocked on: PR 5.

### Deferred, tracked separately

Host-contract seams for session persistence, regex-engine parity and auxiliary
runtimes. A second driver. Whether `agent.provider` becomes selectable
(`rfc-pluggable-model-providers.md`).

## 8. Enforcement and testing strategy

### 8.1 The ratchet reuses an established pattern

Model on `scripts/check_subprocess_encoding.py` with
`.github/subprocess-encoding-baseline.txt`, not on `error-code-baseline.json` and
not on `config-baseline.json`. It is the only existing mechanism that matches an
import rule on all four properties we need:

1. Per-file `<count> <path>` lines, shrink-only: a file absent from the baseline
   must be clean, a baselined file may not grow, and a file whose count has
   shrunk must be pruned.
2. `--test` plants one probe per rule family and runs first in the same CI step.
   Per `docs/ci/harness-parity-gate.md`: a gate that has silently stopped
   matching reads as a green signal, which is worse than no gate.
3. `--update-baseline` only deletes lines, and a missing baseline is a hard error
   rather than a regeneration. Without this the boundary can be laundered in one
   commit.
4. Pure stdlib over `src/`, no CI install step.

`config-baseline.json` is rejected because regenerating it is expected, so it
does not ratchet. `lint:theme-colors` is rejected because it exits 0 by design.

The gate ships as `scripts/check_agent_sdk_boundary.py` with
`.github/agent-sdk-boundary-baseline.txt`, and three of its decisions are load
bearing rather than incidental:

**It watches two roots, not one.** `kiro_crew.acp` alone would be a hole.
`providers/base.py` re-exports `LLMEvent` and the `EVENT_*` constants from ACP
verbatim, so a consumer that swapped `from kiro_crew.acp import
LLMEvent` for `from kiro_crew.providers import LLMEvent` would read as migrated
while still depending on ACP event shapes — and the baseline would shrink for
free. The forbidden roots are therefore `kiro_crew.acp` **and**
`kiro_crew.providers`, the exempt prefixes are `agent_sdk/`, `acp/` and
`providers/` themselves, and the gate prints the per-root split on every run, so a
wave that moves one half and stalls on the other is visible rather than merely
smaller.

**It matches bound names, not just the from-target.** `from kiro_crew import acp`
has `node.module == "kiro_crew"`, which matches no forbidden root — the forbidden
package is the *name being bound*. A gate that inspects only the from-target lets
that spelling, plus `from kiro_crew import providers` and `from .. import acp`,
through untouched. Since there is no inline opt-out marker, that would not have
been an obscure edge case: it would have become the standard way around the rule,
with the baseline still shrinking and dependence unchanged. So each bound name is
resolved against the from-target and checked too, with a self-test probe per
spelling. `*` is skipped, because a star-import of an ancestor names no forbidden
root. Two reviewers found this independently on PR 1; it is recorded here because
the fix is a property of the rule, not an implementation detail.

The same completeness rule applies to the dynamic branch, and there it forced a
change of shape rather than another patch. `import_module` and `__import__` each
carry the module in more than one argument position: `name` may be a keyword,
`__import__`'s real target lives in `fromlist` when `name` is only the package, a
leading-dot `name` with `package=` is relative, `level > 0` is relative to the
calling module, and the importer itself can be renamed
(`from importlib import import_module as _im`) or bound by assignment. Each of
those is a distinct spelling and all of them are checked.

`fromlist` is different in kind, and it is worth recording why. Three review
rounds each closed one container shape and revealed the next — tuple and list,
then set and dict keys, then starred unpacking, dict-unpack and literal
concatenation. That sequence does not converge, because `('acp',) * 1`,
`('a' + 'cp',)`, `frozenset({'acp'})` and a comprehension all import the same
package: enumerating the shapes a reader happens to recognise is an open-ended
game against Python's expression grammar, and every round of it ships a gate that
looks complete and is not.

So the question is posed the other way round. When `name` is an **ancestor** of a
forbidden root, `fromlist` alone decides whether the call crosses the boundary —
and the gate reports it unless it can **prove** the `fromlist` names nothing
forbidden. A fully-readable literal is decided exactly; anything the scanner
cannot read completely is reported. That closes the whole class at once, including
the shapes an earlier draft had classified as "not statically decidable, therefore
out of scope" — an argument that only held while the alternative was believed to
be silence.

Two properties keep this from being merely conservative. Ancestry is required, so
`__import__("kiro_crew.sandbox", fromlist=[...])` — which this tree really does
call — is untouched, because no forbidden root lives under `kiro_crew.sandbox`. And
a bare string is decided rather than feared: iterating `"acp"` yields single
characters, so it can only ever name one-letter submodules.

One argument-parsing bug is worth recording separately, because it was not a
container shape and no amount of enumeration would have found it. `__import__("")`
with a `level` is how a purely relative import spells itself, and
`__import__("", globals(), locals(), ("acp",), 2)` from two levels under
`kiro_crew` really does import `kiro_crew.acp`. The scanner read the module name as
`positional or keyword`, and `""` is falsy — so a legal name was treated as "no
name given" and the whole relative branch never ran. The rule is to test for
absence, not for truth. Its mirror is equally load-bearing:
`import_module("", package=...)` raises `ValueError: Empty module name` and imports
nothing, so it stays clean — both directions were checked against the interpreter
rather than reasoned about.

One gap remains open on purpose and is recorded as a clean probe: a non-literal
*module* target (`mod = cfg.name; import_module(mod)`). Unlike `fromlist`, there is
no ancestry signal to key a conservative rule on — the module name is the whole
input — so reporting every dynamic import would flag the app loader and the plugin
registry, and the gate would be turned off within a week.

**Every verdict is scoped to the files the change touched.** The gate resolves
the changed set through the same resolver the black gate uses, and *all four*
verdicts — new offender, grown count, edge on an added line, entry to prune — are
gated on it. One of them, `grown`, was written without that guard, and the
asymmetry is worth recording because it turns a per-PR check into a shared
tripwire: an edge landing anywhere in the tree fails the *next* unrelated PR, whose
author has no fix available inside their own diff, and the only way out is to
touch a file they had no reason to open. Whoever grew the file still gets the
error, because the file is in *their* scope. Full-tree runs (no change scope) keep
reporting everything, so the unscoped view is still available on demand.

**Re-seeding is legitimate exactly once, inside PR 1.** The refuse-if-exists guard
is what stops the baseline being laundered, but it also blocks the one honest
reason to regenerate: an upstream refactor that moves edges between paths while
PR 1 is still open. That happened here — `#6921` split `SessionManager` into five
new modules between this RFC merging and PR 1 being raised, moving eight ACP edges
out of `session.py` and adding one. `--update-baseline` can only lower and prune,
so it cannot record the new paths, and leaving them unrecorded would fail the next
person to touch them for a leak they did not introduce. The resolution is narrow
and worth stating so it is not mistaken for a precedent: while PR 1 is unmerged the
baseline has no history to protect, so deleting and re-seeding it is a correction.
After PR 1 merges it does have history, and the guard is absolute — a path that
appears later is a violation to route through `agent_sdk`, not a line to add.

**That window is closed.** PR 1 landed with the baseline at 58 files / 107 edges,
so the guard is now absolute with no exception, and the paragraph above is history
rather than live guidance.

**Seeding and updating are separate verbs.** `--update-baseline` can only lower
counts and delete lines; it refuses to introduce a path. Creating the file at all
requires `--seed-baseline`, which **refuses to run when the baseline already
exists**. Without that split, laundering the boundary is one command: delete the
file, re-run the updater, and every new violation is absorbed as pre-existing.

**There is no inline opt-out marker.** The sibling gates allow a per-line comment
to exempt a call site; this one deliberately does not. "This consumer legitimately
needs ACP" is precisely the claim the boundary exists to refuse, so a marker would
not be an escape hatch for edge cases — it would be the standard way to defeat the
rule, one line at a time, with no review signal. The baseline is the only
exemption mechanism, and it only ever shrinks.

`test/` is out of scope: a test that exercises the driver must import the
driver's dependencies, so gating test files would make the boundary untestable.
A file that fails to parse is a hard error, never "clean" — otherwise a
shrink-only prune could silently delete a real entry.

### 8.2 The architecture test

An `ast`-based test in house style, modelled on
`test/test_messaging_import_purity.py` and `test/test_workflows_architecture.py`:

- Forbidden set **derived**, not hand-listed — a hand-kept list fails open, which
  is exactly how the messaging test previously missed two channels.
- The exempt set is a **ratchet, not a comment**: the test asserts the gate's
  exempt prefixes equal an expected tuple, and that each one names a directory
  that exists. Widening the boundary — or leaving `providers/` exempt after PR 6
  deletes it — fails a test instead of passing quietly. A
  "every module is classified" assertion was considered and rejected: every path
  under `src/` begins with `src/kiro_crew/`, so such a test can never fail and
  reads as coverage it does not provide.
- A neighbouring-name probe: `kiro_crew.acp_backends` is not `kiro_crew.acp`, and
  the test pins that a prefix-match regression does not start flagging it.
- `test_every_recorded_violation_still_exists` — a stale exemption fails.
- Negative probes: a violation outside the table is still caught; a
  `TYPE_CHECKING`-only import is still refused; `importlib.import_module` and
  `__import__` do not escape the scan.
- A `scanned` counter so an empty scan cannot pass green.

### 8.3 Behavioural parity

- Event-kind and stop-reason string equality between SDK and ACP constants.
- A translation test per `AcpEvent` field: kept fields round-trip, dropped fields
  have no SDK attribute, replaced fields map correctly. **Per property as well as
  per field** — `AcpEvent` carries four derived members (§5.2) and a field-only
  test passes while all four silently invert, which is why this list previously
  read as complete and was not.
- An approval test proving a token from turn N is refused on turn N+1 and on a
  different session.
- A protocol-conformance test per driver: for every optional protocol, the driver
  either satisfies it or the corresponding capability question is False — never
  both, and never neither. This is what stops an implement-and-raise stub.
- The existing dialect-parity harness continues to run against the driver
  unchanged.

## 9. Backward compatibility

- **Browser wire unchanged.** `ApprovalToken` is a string and serializes into
  today's `{"id": "..."}` payload. No frontend change in any phase.
- **Event kind values unchanged.** Persisted transcripts and channel payloads
  keep working; only the Python import path moves.
- **`LLMProvider` survives migration, and `AgentEvent` ships deprecation shims.**
  `kiro_crew.providers.base` becomes a deprecation shim re-exporting the SDK role
  protocols, so PR 2 does not have to land with every consumer file. That alone
  is not enough to make PR 2 additive: the five ACP-shaped fields PR 2 drops from
  the event object are read at a large majority of the consumer files, led by
  `event.request_id`. The figures v3 carried here — over 200 sites across 17
  files, 131 `request_id` reads — were measured on `d7b7d65c3`, and the baseline
  has grown from 68 to 107 edges since, so treat them as unverified and re-measure
  inside PR 2. §PR 5's rule applies to this paragraph as much as to that one: the
  scan is the source, not the prose. PR 2 therefore lands each
  dropped field as a read-only derived property on `AgentEvent` that raises
  `DeprecationWarning` — so every existing reader keeps working on the day PR 2
  merges. PR 5's waves delete each shim as they migrate its last reader, and PR 6
  asserts none survive. Both shim families are deleted in PR 6, not before.
- **Config keys unchanged.** `agent.acp_backend` and
  `agent.acp_backend_allow_ungated_tools` keep their names and values; the SDK
  reads them through the driver. Renaming them is a separate change with its own
  migration.
- **The dormant CC seam keeps working.** The companion's registration path
  (`ProviderRegistry.register_acp_backends` / `create_factory`) is unchanged by
  every phase. What changes is that the three kinds of undeclared hole in §2.6
  gain typed replacements — a driver may adopt them incrementally, and until it
  does the existing overrides continue to function.
- **ACP behaviour unchanged.** No phase alters wire traffic, spawn argv, or
  permission routing.

## 10. Security considerations

1. **The permission path is the security boundary.** Today `approve_tool` accepts
   any `str | int` and matches it against a pending-request map. An
   `ApprovalToken` must be minted by the SDK, bound to one turn on one session,
   single-use, and rejected otherwise — so a stale or forged id from a
   long-running browser tab cannot approve a later tool call. This is a
   strengthening, and §8.3 asserts it.
2. **Deny-rule parity is host contract, not SDK.** The 137 built-in patterns are
   enforced at Crew's own PreToolUse gate and deliberately not delegated to the
   provider (`security.py:50-58`). The boundary must not create the impression
   that a driver can take over command denial; §6 records the engine-class
   dependency instead.
3. **An absent enforcement mode must not read as an enforced one.** §6.2's rule
   is a security requirement, not a documentation preference: a foreign host that
   cannot express audit-only must declare the gap. A presence-tested protocol
   makes the absence type-visible; a boolean beside a mandatory method does not.
4. **Skipping agent activation must not widen privilege.** `set_mode` carries a
   fail-closed check (`acp/client.py:3513-3536`). A driver declaring
   `activates_agent_by_mode = False` must cause the SDK to refuse, not to proceed
   with an unactivated agent.
5. **Credential scrub stays in the driver.** `scrub_agent_denied_env` and
   `scrub_agent_subprocess_env` exist because ACP spawn paths copy raw
   `os.environ`. They must move with the spawn code, not be re-derived above the
   boundary where the env is already assembled.
6. **Auth failure must stay legible.** `AcpAuthRequired` currently reaches
   `dashboard/kiro_readiness.py`, which lets ordinary sends run ungated and blocks
   pre-turn and destructive endpoints. A collapsed error taxonomy that folded it
   into a generic `AgentError` would silently un-gate those endpoints.
7. **Logout authorization must not be inverted.** `recyclable_on_host_logout`
   replaces a set whose name suggests ownership but whose meaning is
   authorization — `ACP_BACKENDS_KIRO_IDENTITY_STORE`, at
   `acp_backends.py:407` since the constants left `acp/types.py` (§2.3). Getting
   the polarity wrong would let a host logout retire a foreign backend's live
   child.
8. **Sandbox delegation must stay fail-closed.** Every detection failure in
   `sandbox.py` resolves toward Crew's own sandbox. `self_sandboxes` must default
   to `False` for an unknown provider, matching `bills_kiro_credits`'s existing
   fail-safe treatment of unknown ids.
9. **The ratchet is a security control.** It is what prevents a future PR from
   reintroducing a raw wire id into a browser payload. Its `--test` self-probe
   and refuse-to-regenerate property are the reasons it is trustworthy.

## 11. Alternatives considered

### 11.1 Evolve `kiro_crew.providers` in place

Rejected. It is already positioned as the boundary and has not become one in
practice: it re-exports rather than translates, and most consumer modules bypass
it — the baseline is the count. Fixing
it in place means the same three inversions plus keeping a name whose current
meaning is "ACP with an alias". A new package makes the rule statable — *this
directory may import ACP, that one may not* — which is what the ratchet needs.

### 11.2 Turn #6307's descriptor into a behavioural interface first

Rejected as the *first* step, not on merit. #6307's `BackendDescriptor` is a
frozen data record with no adapter ABC, and the work actually done for a backend
still lives in id-keyed `if/elif` chains — so adding an adapter means a
descriptor row plus edits at several dispatch sites. Making that a real Protocol
is worthwhile, but it is entirely **below** this boundary: it improves how the
driver is organised internally and leaves the whole event vocabulary and every
baselined import edge untouched. #6307 should land on its own merits; this RFC's Phase 1
does not touch it.

### 11.3 Adopt `import-linter`

Rejected. It is not in the version set, and
`test/test_workflows_architecture.py` already establishes the house alternative:
a pure-stdlib `ast` scan with a per-module allowlist and a
coverage-of-the-contract test. Matching that costs less than adding a dependency
and keeps the gate runnable without an install step.

### 11.4 One big-bang boundary commit

Rejected. It would touch every baselined file across every subsystem in one
unreviewable diff — 58 of them as PR 1 seeded it — and the two hardest decisions
(§12.1, §12.2) would be settled implicitly inside it rather than answered first.

### 11.5 Abstract the whole host contract now

Rejected for this RFC, on scope — with two exceptions. §6.3 covers eight of the nine buckets,
four of which have no seam whatsoever. Bundling all of it would make the SDK
boundary hostage to decisions about transcript formats and regex engines.
Documenting the contract is what lets that work start independently, and is what
stops us believing after PR 6 that a provider swap is finished. The two
exceptions — per-session MCP injection and transcript ownership — are promoted
into PR 3 because the CC review showed they are not optional to the boundary
itself: today they *are* the boundary, in the form of an override hole and an
`isinstance` guard.

### 11.6 Adopt the internal companion's provider abstraction

Rejected, but instructive. The internal companion ships two real backends and has
a module whose name suggests a capability model and another whose name suggests a
provider registry, so it looked like a working seam worth building on. It is not
one. Its provider registry is a little over a hundred lines whose
`register_acp_backends()` is an explicit no-op and whose factory dispatches on a
string compare against an env var, with no ABC or Protocol anywhere. The second
provider is implemented by subclassing the first and swapping `__class__` on a
live client, to avoid re-implementing the core client's ~110-line `__init__`.
The capability-sounding module is a name collision: it installs MCP servers,
skills and agent packages, and holds no per-provider feature table at all. And
`grep AcpEvent` across that package returns nothing — it has no event type of its
own, because it inherits the core's stream and uses ACP constants as its dispatch
vocabulary.

Adoption is impossible anyway: that registry is a *consumer* of an OSS-side
Protocol, so the seam this RFC must define is upstream of it by construction.
What it contributes is evidence and one artifact: its factory's keyword surface is
the only session-construction contract that has been exercised against a foreign
vendor, and §5.3 takes `SessionRequest` from it. Its `supports_permission_mode()`
returning `getattr(client, "_is_claude", False)` is a shipped instance of exactly
the defect §5.2 removes.

### 11.7 Keep capabilities as booleans beside a flat interface

Rejected, and this is the alternative the CC review killed. It was the earlier
draft of §5.3. Booleans gate behaviour but not method presence, so a driver for a
host that lacks steer, mode switching, slash commands, asynchronous compaction and
config reset must implement five methods purely to raise — and consumers, unable
to see that, either call them or reconstruct the very identity inference the
frozensets were introduced to remove. Presence-tested protocols cost one
`isinstance` at each optional call site and make the absence type-checkable.

The codebase already argues this against itself. The comment introducing
`ACP_BACKENDS_ACP_RUNTIME` (`acp_backends.py:387`, cited in v3 at its old
`acp/types.py` address) says the four sites meaning
"kiro or kas" say so positively "rather than as `not is_claude_backend` — an
inference that silently captures every harness added later", and in the same
breath records that the set is a **superset** of `ACP_BACKENDS_SESSION_SHARING`.
That is both halves of this rejection written by the code it describes: the
inference is the hazard, and one boolean cannot carry two nested facts.

## 12. Open questions

All questions carry a disposition as of 2026-09-05. The two blockers are decided,
so §12.1 no longer gates PR 4 and §12.2 no longer gates PR 2. The others were
never blocking; each records a conservative default and the condition that
reopens it.

1. **Who owns agent-process supervision? — DECIDED: the supervisor.**
   `session_pid.py`'s agent-process half moves into `agent_sdk`; its non-agent
   PID duties (MCP probes, cron scripts) stay where they are. The decision stands;
   **its justification named the wrong cycle and is corrected here.** v3 said the
   cycle being settled was `session_pid → acp.client → session → session_pid`,
   evidenced by two lazy `_get_child_pids` imports. A lazy in-function import
   cannot close a loop at initialization time. The real cycle ran
   `session_pid.py:28 → providers.base → acp.types → acp/__init__ → acp.runtime →
   session_pid`, raised a hard `ImportError` on a standalone
   `import kiro_crew.session_pid`, and **is already closed** by deleting that one
   annotation-only import rather than deferring it (§2.4). So the cycle is no longer
   an argument for this decision — the ownership question is the whole argument,
   and the move still has to relocate `_get_child_pids` and place
   `_kill_pid_tree` + `_MANAGED_AGENT_MARKERS`, which both halves use.
   The `acp/worker_pool.py:49` guard is still in place and PR 4 will delete it as
   a consequence of the move, not as a proof of the cycle: it is a one-way
   standalone-importability fallback and it catches `Exception` rather than
   `ImportError`. That clarification was right in v3 and still is.
   *Rationale:* the warm pool and background runtime move in PR 4 regardless,
   and kill authority has to travel with the pool it kills. The rejected
   alternative — SDK depends on `session_pid` — preserves the cycle and only
   renames it.
   *Revisit if:* a non-agent consumer turns out to depend on the agent-process
   tracking file format, in which case the file stays put and the supervisor
   writes through a narrow interface instead of owning it. **Note this condition
   is already partly met and PR 4 must plan for it:** the work-class sweeps gate
   their kill sets negatively on `_MANAGED_AGENT_MARKERS` and the MCP sweep
   consumes the protected set through `_collect_active_pids`. That is shared
   *safety* input, not file format, so the decision stands — but the split must
   preserve both, not discover them. (Line numbers in `session_pid.py` have moved
   twice since v3 recorded them; re-derive.)
   **PR 4 is unblocked, and its import-cycle half has landed.**

2. **Does `AgentEvent` carry child attribution as an object, or flatten it? —
   DECIDED: a `ChildAttribution | None` value object,** non-`None` only on
   subagent-related events.
   Measured rather than assumed. Outside `acp/` and `providers/`, the three
   fields have **10 references across 4 files**: `dashboard/chat_runner.py:6624`,
   `:6743`, `:7820`; `subagent.py:1719`, `:1744`, `:6011`, `:6060`;
   `messaging/driver.py:468`; plus two comment lines. `mcp_identity_ambiguous`
   has **zero** external readers. The 466-usage figure in §2.1 counts the whole
   `EVENT_*` / `STOP_REASON_*` vocabulary; almost none of it reads attribution.
   *Rationale:* at 10 sites the cleaner shape costs nothing, and
   `runtime_global`'s meaning — a fanout frame with no owning session — is only
   legible beside `sub_session_id`.
   *Revisit if:* PR 2 finds a reader that needs attribution on a
   non-subagent event. That would mean the field is not attribution.
   **PR 2 is unblocked.**

3. **Is runtime multiplexing part of the SDK contract or driver-private? —
   DISPOSITION: driver-private, provisionally, and the question is asked in two
   places rather than one.** `AgentSupervisor` exposes "give me a session", never
   "give me a runtime". The capability split introduced in §5.2
   (`multiplexes_sessions` versus `shares_subagent_session`) answers the
   consumer-facing question without exposing a runtime object.
   What an earlier draft of this item missed is that the *pre-session* half is not
   a session question at all: `session.py`-side callers ask it off a config string
   to choose which path builds the session — v3 named `:1398` and `:1610`, both
   stale after the `#6921` splits, with some reads now in
   `session_allocation.py` / `session_pool.py` — and the answer is
   `ACP_BACKENDS_ACP_RUNTIME & selectable_backends()` — a set, whose second
   operand is a deployment fact resolved during boot. §5.2 puts that on the driver
   registry as `spawnable_multiplexed_selections()`; a `SessionCapabilities` value
   could not express it, and a boolean would discard the intersection that keeps a
   runtime-capable-but-unregistered backend out.
   *Revisit if:* PR 4 cannot express warm-pool or background-runtime behaviour
   without a runtime-shaped argument crossing the boundary. Not blocking — PR 4
   produces the answer as a side effect of doing the move.

4. **Should host-contract declarations become code? — DISPOSITION: yes,
   eventually; not in this RFC. The condition that reopens it has now fired
   once, and it was missed.** The earlier draft deferred this on the grounds
   that "with one provider it would be a table with one row." That rationale was
   **wrong**: there are four backends, and the Claude Code row is complete. The
   trigger it named — a second driver being proposed — has effectively already
   fired, in another repository.
   Then Codex landed, shipped, and became selectable **without answering a single
   bucket** (§2.7). A prose checklist that says "silence is not an answer" and has
   nothing enforcing it is the failure mode this question exists to prevent, and it
   has now happened in-repo rather than hypothetically. That is evidence for "yes",
   not against.
   What changes now: the checklist is written against four columns rather than
   one, the host-contract spec gains a Codex column and a parity test alongside
   this revision, and PR 3 lands the two declarations the boundary cannot do
   without (per-session MCP injection, transcript ownership). A full `HostProfile` type
   covering all nine buckets still waits, because four of them have no seam to
   type against yet (§6.3) — typing an unsealed bucket would freeze the wrong
   shape.
   *Revisit if:* a driver is proposed inside this repository, at which point an
   omitted declaration should fail at import rather than at runtime. Given Codex,
   read that condition as met in spirit: the parity test is the interim
   enforcement, and a `HostProfile` type is the eventual one.

5. **Does the deprecated `providers` shim need a release window, and where does
   `providers/mirrors/` live? — DISPOSITION: three halves; the third is a
   relocation decision, not a release-window one, and it gates PR 6.** The
   question is not only about external consumers. `from kiro_crew.providers` is a substantial minority of the
   baseline — the gate prints the exact split on every run — and it pulls the role
   protocols (`LLMProvider`), the provider class (`AcpProvider`), the event type
   and `EVENT_*` constants that `providers/base.py` re-exports from ACP, plus
   `provider_label` and `is_claude_backend`, which `providers/acp.py` defines
   rather than re-exports. An earlier draft of this item carried per-symbol
   counts; they were wrong on most entries and stale on the rest within two days,
   so they are gone. The distinction that survives is the one that matters:
   `LLMEvent` and the `EVENT_*` names are ACP shapes wearing a `providers` label,
   so an edge that pulls them is not more migrated than a direct `acp` import —
   which is why the gate watches both roots.
   So the in-repo half is PR 5's work, not a release-window question, and PR 6
   must not arrive with it outstanding.
   The second half, genuinely open, is *external*: before PR 6 deletes the shim
   surface, grep the internal companion and the app catalogue for
   `kiro_crew.providers.base`. Zero hits → delete in PR 6. Any hit → one release
   window, and the shim carries its deprecation warning from PR 2 rather than
   PR 6.
   **The third half is new since v3 and is not a release-window question at all.**
   `src/kiro_crew/providers/mirrors/` did not exist when this item was written. It
   is now a real agent-config projection layer — `base.py`'s `AgentConfigMirror`
   with `Concern` / `Disposition` / `Ruling`, `registry.py`'s `MIRRORS` /
   `NO_MIRROR`, `claude_code.py`, `README.md` — with claude the only mirror and
   kiro, KAS and codex carrying explicit `NO_MIRROR` reasons, and KAS's own entry
   says its projection is next in the stack. So the layer is *growing*, and
   "delete `kiro_crew.providers`" as v3 wrote it would delete live code. It needs a
   home named before PR 6 starts, and the choice is a real one: a mirror projects
   an agent spec for one host, which is driver work and argues for
   `agent_sdk/mirrors/`; but the mirrors also encode per-host *decisions* a driver
   should not be free to restate, which argues for leaving them above the boundary
   in a package of their own. Either answer is defensible; discovering the question
   during PR 6 is not.
   *Blocking for PR 6, not before.* The in-repo shim count is already tracked by
   the gate's per-root split, the external check is cheap, and the mirrors decision
   only has to be made by the time the deletion lands.

6. **Is protocol-presence testing the right consumer ergonomic? —
   DISPOSITION: yes for optional *operations*, no for optional *facts*.**
   §5.3 uses `isinstance(session, AgentCompactable)` for operations and keeps
   §5.2's questions as values for facts (`recyclable_on_host_logout`,
   `bills_host_credits`, `self_sandboxes`), because those gate policy rather than
   a call. The risk is a consumer writing a long `isinstance` ladder where a
   single question would do.
   *Revisit if:* PR 3 produces a call site testing three or more protocols to
   make one decision — that is the signal the split is drawn in the wrong place.

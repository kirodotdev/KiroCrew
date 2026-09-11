# Onboarding a new ACP harness

[harness-parity.md](harness-parity.md) says what a new harness may **not** do to
the Kiro path. This file says what it **must** do to land at all, in the order
the work actually falls out.

The sequence below is derived from onboarding Codex (`ACP_BACKEND_CODEX`), not
reconstructed from KAS and Claude Code after the fact. That matters, because two
of the stages here did not exist as stages until a third harness needed them:
Stage 2 had five capability sets and no tuning channels at all, and Stage 6
was invisible while every known id happened to be selectable. A harness
that walks this list will find gaps the list does not predict; when it does, the
gap belongs here in the same change, as a stage — not in the harness's own
module as a special case.

## The two landing states

A harness lands in one of two states, and choosing between them is Stage 7, not
Stage 1:

- **Dormant.** The core can *spell* the id — it is in `ACP_BACKENDS_KNOWN`, it
  has a provider label, a policy name, and a decided membership in every
  capability set — but no operator can *choose* it. A dormant harness is not a
  stub: its spawn path can be complete. It is dormant because something a real
  session depends on cannot yet answer for it.
- **Selectable.** The id is in `BASELINE_SELECTABLE_BACKENDS`, or an edition
  called `register_selectable_backend`, so it renders in the dashboard switch
  and survives a config load.

Dormant is a legitimate destination, and shipping there deliberately is cheaper
than a long-lived branch. But it must be *named* as an exception (Stage 7), or
the narrowing check fails.

## Stage 1 — the vocabulary, in the leaf

Everything a consumer needs to *name* your harness goes in
`src/kiro_crew/acp_backends.py`, which imports no ACP and therefore may be
imported by anything:

| Add | Why there |
|---|---|
| `ACP_BACKEND_<NAME>` | The id. Never a bare literal at a call site (H5). |
| membership in `ACP_BACKENDS_KNOWN` | `AcpProvider.__init__` rejects anything outside it (H8), and every capability set is asserted a subset of it. |
| `PROVIDER_LABEL_<NAME>` in `acp/types.py` | A closed mapping; an absent label means Kiro, so a harness without one persists as a Kiro session and has its transcript pruned for want of a Kiro session file (H11). |
| an entry in `POLICY_ID_BY_BACKEND` | A governance rule is written by a human as an identifier. The mapping is what makes the id nameable in a deny rule **before** anything registers it — so this is required even for a dormant harness. |

`acp/types.py` re-exports the vocabulary, so existing callers keep their import
site. Do not define the constants there: it is a forbidden root for the SDK
boundary gate, and a definition there is a definition consumers cannot reach
without crossing it.

## Stage 2 — an explicit decision for every capability set

Every capability set needs a decision. **"Inherited the default" is not a decision** — a
capability is granted by opt-in membership, never by negation (H6), so a set you
do not think about is a set you have silently opted out of. That is usually
right, and it must still be deliberate, because the review lane and the tests
both read the membership as a claim. `ACP_BACKENDS_KNOWN` is not one of them: it
is the membership floor, not a capability. Neither is
`backends_retired_by_host_logout()`: whether a host logout may retire your running
child is a fact about how you sign in, so it is declared in Stage 5 and projected
from there rather than decided here. It is deliberately not an `ACP_BACKENDS_*` set
— that naming is vocabulary this module owns, and a derived answer is not
vocabulary.

| Set | Grants |
|---|---|
| `ACP_BACKENDS_SESSION_SHARING` | One process may serve several sessions. Wrong membership hands a second session to a process that cannot hold it. |
| `ACP_BACKENDS_STEER` | The `_session/steer` extension. A steer sent to a non-implementer answers `-32601`. |
| `ACP_BACKENDS_INTERNAL_SANDBOX` | The harness sandboxes itself, so Kiro Crew's own wrapper stands down. Security-relevant: wrong membership hands isolation to a layer that never starts (H7). |
| `ACP_BACKENDS_ACP_RUNTIME` | Driven through `AcpRuntime` rather than its own spawn branch. |
| `ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION` | Model switching lands as a config option rather than a protocol call. |
| `ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION` | Reasoning-effort push, same channel shape. |
| `ACP_BACKENDS_KIRO_SLASH_COMMANDS` | Receives `_kiro.dev/commands/execute`, **and** gets the workspace `cli.json` overlay written for it. Membership decides both, so a non-member must not collect an overlay it never reads and the membership-gated clear can never remove. |
| `ACP_BACKENDS_SESSION_MCP_ARRAY` | The harness reads its MCP surface from the `session/new` array rather than from Crew's agent spec. A non-member that is added here gets an empty array and works with every Crew tool silently absent. |
| `ACP_BACKENDS_MEMBER_DISPATCH` | Crew's member-dispatch tools are mounted into a channel-member session, with the auto-approve grant that goes with them. A harness with no per-session mount to ride is excluded, which withholds only the extra grant. |
| `ACP_BACKENDS_PRIVATE_MEMORY_MCP` | Direct private member MCP tools execute inside the member's OS sandbox. Kiro, Claude Code and KAS are members. Codex and unknown or merely selectable backends fail before private runtime creation. Membership does not waive the separate OS sandbox checks. |
| `ACP_BACKENDS_COMPACT` | The manual `/compact` entry points are offered. A non-member refuses the manual command up front rather than stranding the status waiter on a harness that emits no compaction status of its own. |
| `ACP_BACKENDS_ADVERTISED_MODEL_SELECTION` | Membership buys two things, and a harness can need only one. First the CAPTURE: the list the harness advertises at `session/new` is written to the cross-session provider-model cache under the harness's own namespace, which is what `GET /api/models` reads back. Second the FOLD: a stored id is rewritten to the served spelling, at spawn and on a warm-pool `set_model`. A harness whose wire ids are already exact gets a no-op fold, so it joins for the capture alone — which is the whole point when its advertised select is the only source of ids it accepts back (codex). claude joins for both. |
| `ACP_BACKENDS_SEED_LOCAL_SETTINGS` | A local settings file is seeded at spawn **and re-seeded on `set_model`**, so a warm-pool claim does not leave a stale model or allowlist behind. A harness with no such file is not a member. |
| `ACP_BACKENDS_MCP_CONFIG_HOT_RELOAD` | The dashboard's MCP sync leaves running sessions alone after a config write, because the harness reconciles the agent file itself. Membership is version-gated per process by `mcp_hot_reload_supported`, not granted by the harness name alone. |
| `ACP_BACKENDS_STRUCTURED_REFUSAL` | The harness reports a model-side refusal with a **reason** — on the Kiro path a `_kiro.dev/metadata` frame with `stopReason: CONTENT_FILTERED` and a `refusal {category, explanation, recommendedModel}` object — and `acp/_dispatch.parse_refusal` is consulted on that frame. Every harness still lands on the same `RefusalInfo` and the same dashboard card; a non-member's card just has no category line. A harness whose refusal wire carries a reason in a different shape adds a parser and joins here — it must not widen the metadata reader to guess. |

The tuning channels are one set each rather than one "tuning" set, because a
harness can implement one and not another. If your harness needs a tuning
channel none of them describes, add a set — do not widen an existing one.

## Stage 3 — the spawn path

This is the irreducible new code, and on the harnesses measured so far it is the
largest single piece: `acp/client.py` grew between +194 and +806 lines per
harness. It is not reducible by refactoring, because it is the part that is
genuinely different.

What a harness needs, using the Codex adapter as the shape:

- **The adapter, and whether one is needed at all.** `codex-acp` exists because
  the `codex` CLI does not serve ACP — it reads `acp` as a prompt. The adapter is
  the transport, not an optimization. Establish this before anything else; a
  harness that speaks ACP natively skips most of this stage.
- **Binary and package constants** (`CODEX_ACP_BIN`, `CODEX_ACP_NPM_PKG`) and
  the package entry path.
- **A hoisted-dependency marker.** An adapter whose own dependencies are missing
  dies at ESM import time — *after* the child is spawned, which is the worst
  place to find out.
- **An explicit env override** (`CODEX_ACP_BIN`), spelled the way the adapter's
  own documentation spells it.
- **Resolution order**: project-local `node_modules` first, then global/PATH.
  Share the root discovery (`_vendored_acp_roots`) and join your own package
  path onto it. Generalizing that helper is allowed; it is harness-neutral and
  belongs to no harness. Adding a branch to the Kiro path is not (H13).

Constants an adapter reads *itself* from the ambient environment do not get a
constant here. Naming one implies a forwarding that does not exist — the Codex
seam documents exactly this asymmetry against its Claude counterpart, which *is*
explicitly forwarded.

## Stage 4 — the handshake, as your own literal

Protocol version and client capabilities stay per-harness literals (H10). Give
your harness its own `PROTOCOL_VERSION_<NAME>` **even when the number is
identical to an existing one.** That is not duplication: it makes a future
divergence a one-line edit here instead of a silent downgrade of whichever
harness happened to move first.

## Stage 5 — the auth declaration

How your harness signs in is one frozen `AgentAuthDeclaration` in
`src/kiro_crew/agent_sdk/host_auth.py`, plus your column in bucket 3 of
[agent-host-contract.md](agent-host-contract.md). **That is the whole auth cost.**
Everything else is a projection of that one literal: the read-gate floor that
fences your credential and re-anchors it under your own override variables
(`security/paths.py`), the sandbox credential mask and the single leaf it spares so
your own child can still authenticate (`agent_sdk/tool_gate.py`), the
the logout-recycle answer `backends_retired_by_host_logout()`, the `AcpAuthRequired` text
an operator reads when a session cannot start, the `auth` object on
`GET /api/acp-backends` (`dashboard/handlers/acp_backend_status.py`), and the
standing sign-in caveat the backend switch renders from it. You write the
declaration; you edit none of those.

It belongs after the handshake and before the install probe, because it is the
sign-in half of the question Stage 6 answers about files — a harness that is
installed and signed out still dies at `session/new` — and because the floor has
to be fencing your credential before Stage 7 lets an operator choose you.

| Field | What it commits the host to |
|---|---|
| `entitlement_source` | One of `host_identity_store` or `own_credential_file`. The doctor row and the logout policy branch on it, so a third spelling fails a test rather than reading as a harness nobody has an answer for; a harness entitled by the ambient cloud environment adds the third when it exists. |
| `credential_leaves` | The home-relative leaves you STORE, spliced onto the read-gate floor so an agent's file tools can read none of them. Empty when your entitlement is the host store: those locations are the host's, declared in `identity_stores.py`, and re-declaring them would hand a driver a say over the host's own store. |
| `home_override_env_vars` | The variables that relocate your credential `$HOME`. Every declared leaf is re-anchored under each of them, which is what keeps a relocated token fenced. |
| `adapter_own_leaves` | The leaf your own child must still read. A SUBSET of `credential_leaves`, and enforced as one: a driver may only ask the mask to spare a leaf its own declaration put on the floor, and `__post_init__` raises otherwise. |
| `sign_in_remedy` | A finished sentence, server-owned and rendered verbatim wherever it appears. Untranslated on purpose — a translated per-harness string is a per-harness edit to thirteen locale files by construction, and the harness nobody remembers to add is exactly the one that needs the sentence. |
| `host_logout_retires_children` | Whether a host logout may retire your already-running children. True only alongside `host_identity_store`; the other pairing is refused, because a logout says nothing about a store you never read. |

The declaration says what you **store**; the host still decides what is **fenced**.
That is why no field names a path to leave open in general, and why a malformed
declaration raises at import rather than reaching a floor that fences less than its
author believed.

`AgentInteractiveLogin` is the optional half, and the absence is the point. A
harness whose sign-in happens outside the product — in the operator's own terminal,
or in the harness's own CLI — simply does not implement it, and a consumer finds
that out with `isinstance` rather than by reading a boolean and then calling a
method that no-ops. No harness implements it today.

**Silence is not available at this stage.** `test_agent_sdk_host_auth.py` fails
when a member of `ACP_BACKENDS_KNOWN` has no declaration, and
`missing_declarations` names the gap. The failure mode that gate closes is a
harness reaching `BASELINE_SELECTABLE_BACKENDS` without touching the credential
floor, and then serving sessions with a live agent-readable token that nothing
fences.

## Stage 6 — the install probe

`agent_sdk/backend_install.py` answers a question selectability does not: *is
this harness installed on this machine, and if not, what installs it?* It holds
one `_probe_<name>` per harness in `_PROBES`, each returning a
`BackendInstallState` naming the missing component and the command that fixes
it.

**This stage is the gate between dormant and selectable**, and it is the one
that is easy to skip because nothing fails without it. Nothing fails; the
operator does. A build that offers a switch with no probe behind it cannot tell
anyone what was missing when the session failed to start — the switch renders,
the session dies, and the dashboard has nothing to say.

## Stage 7 — selectability, or a named exception

With Stages 1–6 done, add the id to `BASELINE_SELECTABLE_BACKENDS`.

If it is not done — most often Stage 6 — then the id is in
`ACP_BACKENDS_KNOWN` but not in the baseline, which is a NARROWING. Name it in
`NOT_SHIPPED_SELECTABLE` in
`test_agent_backend_editable.py::test_baseline_ships_every_known_backend`, with
the reason. An explicit allowlist rather than a relaxed assertion is the point:
a plain `baseline != known` still fails, so an id may sit outside the baseline
only by being named.

Selectability has exactly one gate, `resolve_selected_backend`, and it logs
(H4). Do not add a static `enum` to `AgentConfig.acp_backend`: a literal frozen
at import cannot see a boot-time registration, and `validate_config_data`
*deletes* an out-of-enum value before the loader ever sees it — which strips a
registered harness from `config.json` with no degrade log at all.

## Stage 8 — what a live harness additionally touches

Stages 1–7 keep a harness inside `acp/`, `providers/`, and `acp_backends.py`. A
harness an operator can actually select spills further. Measured across the two
in-flight live-harness branches, roughly ten files outside those trees:

`dashboard/handlers/agents.py` (the largest, +213 on one branch),
`mcp_gateway/session_servers.py` (+112), `dashboard/kiro_readiness.py`,
`dashboard/handlers/kiro_prerequisite.py`, `dashboard/handlers/sessions.py`,
`agent.py`, `config/loader.py`, `providers/base.py`, `session.py`,
`subagent.py`, `cli_doctor.py`.

Two rules govern that spill. A capability the session layer reads off a provider
is declared on `LLMProvider` with a safe default, so an adapter never forces a
`hasattr` probe onto the Kiro path (H14) — the cost of obeying this is small,
around +11 lines in `providers/base.py` on the branch that needed it. And the
`ProviderRegistry` seam takes the addition without a `CONTRACT_VERSION` bump
(H13); if the Kiro construction path gains a conditional, a required argument,
or a new failure mode in service of your adapter, the design is wrong, not the
invariant.

## Gates and tests

Beyond the ordinary suite:

- **`scripts/check_harness_parity.py`** enforces Group B on the lines your diff
  *adds*, not the whole tree. Six rules, self-tested.
- **`scripts/check_agent_sdk_boundary.py`** is shrink-only. A new import of
  `kiro_crew.acp` or `kiro_crew.providers` from a consumer fails even though the
  existing baseline (`.github/agent-sdk-boundary-baseline.txt`) grandfathers a
  list of them. This is why Stage 1 puts the vocabulary in a
  leaf: a consumer naming your constant must not have to cross the boundary to
  do it.
- **`test_harness_parity.py`** pins the structural invariants (Groups A and C),
  so they fail in the ordinary test job rather than a separate gate.
- **Group D is review-only.** `AUTOSDE.yaml`'s `harness-parity` rule carries
  H13 and H14 to every AI review lane, because the absence of a mechanism is not
  something a source scan can see.
- **`./scripts/docs-lint.sh`** requires every doc to be reachable from its
  directory index, and checks that line citations still point at what they
  claim.

Never relax a check to make a red invariant green. If a harness genuinely cannot
be adapted within these invariants, the correct outcome is that it does not land
yet — say so in the PR instead of widening a seam.

## Worked example: the Codex seam

The Codex onboarding is a clean instance of stopping at Stage 7:

| Stage | State |
|---|---|
| 1 vocabulary | Done — `ACP_BACKEND_CODEX`, in `ACP_BACKENDS_KNOWN`, `PROVIDER_LABEL_CODEX`, policy name mapped. |
| 2 capability sets | Decided for every set: in the model and effort channels, out of the rest. All three channel sets were *created* by this work, which is why the tuning channels are three sets rather than one. |
| 3 spawn path | Done — adapter, npm package, dep marker, env override, project-local resolution. |
| 4 handshake | Done — `PROTOCOL_VERSION_CODEX`, its own literal at the same number as Claude's. |
| 5 auth declaration | Done — `own_credential_file`, `~/.codex/auth.json` on the floor with `CODEX_HOME` re-anchored, that same leaf spared for its own child, `.aws/config` re-exposed read-only, not retired by a host logout, and a two-branch remedy every consumer renders verbatim. |
| 6 install probe | Done — `_probe_codex` names `codex-acp` and the command that installs it. One component, not two: the adapter ships its own Codex binary. Credentials are deliberately NOT probed: a `missing` verdict disables the switch, and the checkable paths are not the only ones that authenticate a Codex. The sign-in answer is the Stage 5 declaration instead, and every consumer renders its remedy rather than carrying a string of its own. |
| 7 selectability | Selectable. `NOT_SHIPPED_SELECTABLE` is empty again, which is the healthy state. |
| routing | Done — `SESSION_CONFIG`, verified and applied as `mode=read-only` after session/new and before the first prompt, refusing otherwise. |
| residual | ACP v1 cannot require a prompt for a passive READ, so the sensitive-path block does not see this harness's reads. Mitigated at the OS boundary instead: its child cannot read the credential homes the standard tier leaves open. |
| 8 live spill | Not reached. |

The lesson worth carrying: the seam is dormant for exactly one reason, that
reason is written down where the narrowing check reads it, and closing it is a
single stage rather than a re-litigation. That is the shape to aim for — not
"complete or nothing", but "incomplete at a named stage".

## Worked example: the OpenCode harness

The first onboarding run with every gate in this document already in place, and
the one to read for what the stages cost when nothing can be skipped:

| Stage | State |
|---|---|
| 1 vocabulary | Done — `ACP_BACKEND_OPENCODE`, in `ACP_BACKENDS_KNOWN`, `PROVIDER_LABEL_OPENCODE`, policy name mapped, its own model-registry namespace. |
| 2 capability sets | Decided for every set, and each decision cites what the harness advertised rather than what it resembles: in the model channel and the advertised-model capture, out of the effort channel (its `session/new` advertises a `mode` select beside `model` and no `effort`), out of steer and both compaction sets (its `sessionCapabilities` are close/fork/list/resume), out of the session MCP array (it advertises `http` and `sse` MCP transports and no stdio). |
| 3 spawn path | Done — one binary, `opencode acp`, resolved override → mise → PATH. No adapter package and no Node floor, so the ladder is the plain-binary one rather than the entry-script one. |
| 4 handshake | Done — `PROTOCOL_VERSION_OPENCODE`, its own literal, integer `1`, captured off its own wire. |
| 5 auth declaration | Done — `own_credential_file`, `~/.local/share/opencode/auth.json` on the floor with `XDG_DATA_HOME` re-anchored, that leaf spared for its own child, not retired by a host logout, and a remedy that names an action without asserting a state (a locally served model needs no sign-in at all). |
| 6 install probe | Done — `_probe_opencode` names `opencode` and the command that installs it. One component, and here that is not a simplification: the thing that would be missing is the thing that serves ACP. `restart_required` is read from the spawn path's own cache (`opencode_cached_negative()`): the binary resolves now, but this process already cached its absence, so a session started right now still fails until the gateway restarts. |
| 7 selectability | Selectable. `NOT_SHIPPED_SELECTABLE` stays empty. |
| routing | Done, by a NEW mechanism — `VERIFIED_SEEDED_SETTINGS`. The setting travels as inline config in the child's environment, which resolves above the project's own config file, and the harness's own resolved configuration is read back before the first prompt; the session is refused when the required value is not in force. |
| residual | The read-back establishes the PRECONDITION, not that the harness honours it per tool call — no client-side read can prove that. And ACP v1 still cannot require a prompt for a passive READ, so the OS-boundary credential mask is the compensating control, as it is for Codex. |
| 8 live spill | Reached — a live turn, and a frame corpus that is live for all seven required classes, `session/request_permission` included: with `permission: ask` in force the harness asked before running `bash`, which is the observation the whole enforcement claim needed. |

Two things this run produced that the checklist did not ask for, and both belong
in the reading of it. The routing mechanism is one: Stage 2's instruction is to
decide every set, and the honest decision here was that neither existing routing
member described this harness — `SEEDED_SETTINGS` is declared-but-unenforced for
want of a read-back, and this harness has one. Adding a member to the vocabulary is
a heavier edit than joining a set, and it is the right one when the alternative is
a guarantee nobody performs.

The other is what onboarding a harness with a *different shape* of credential home
surfaced. Every earlier harness's override variable stood in for its token's parent
directory, so the credential floor re-anchored a relocated token by its final
segment alone. `XDG_DATA_HOME` stands in for `.local/share`, two segments up, so
that anchoring fenced a path this harness never writes while the real relocated
token stayed readable. A harness declares the spelling its file takes under an
override root now. Expect this: the buckets are answered from the harnesses that
existed when they were written, and a new one whose answer has a different shape
finds the seam rather than the gap.

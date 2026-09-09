# Onboarding a new ACP harness

[harness-parity.md](harness-parity.md) says what a new harness may **not** do to
the Kiro path. This file says what it **must** do to land at all, in the order
the work actually falls out.

The sequence below is derived from onboarding Codex (`ACP_BACKEND_CODEX`), not
reconstructed from KAS and Claude Code after the fact. That matters, because two
of the stages here did not exist as stages until a third harness needed them:
Stage 2 had five capability sets and no tuning channels at all, and Stage 5
was invisible while every known id happened to be selectable. A harness
that walks this list will find gaps the list does not predict; when it does, the
gap belongs here in the same change, as a stage — not in the harness's own
module as a special case.

## The two landing states

A harness lands in one of two states, and choosing between them is Stage 6, not
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
than a long-lived branch. But it must be *named* as an exception (Stage 6), or
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

There are eighteen sets. **"Inherited the default" is not a decision** — a
capability is granted by opt-in membership, never by negation (H6), so a set you
do not think about is a set you have silently opted out of. That is usually
right, and it must still be deliberate, because the review lane and the tests
both read the membership as a claim. `ACP_BACKENDS_KNOWN` is not one of them: it
is the membership floor, not a capability. The count is checked against the
module, not against this page: `test_every_capability_set_has_a_disposition_row`
fails when a set exists without a row in the module docstring's disposition
table, so a set missing from the table below is a doc bug, not a hidden one.

| Set | Grants |
|---|---|
| `ACP_BACKENDS_SESSION_SHARING` | One process may serve several sessions. Wrong membership hands a second session to a process that cannot hold it. |
| `ACP_BACKENDS_STEER` | The `_session/steer` extension. A steer sent to a non-implementer answers `-32601`. |
| `ACP_BACKENDS_INTERNAL_SANDBOX` | The harness sandboxes itself, so Kiro Crew's own wrapper stands down. Security-relevant: wrong membership hands isolation to a layer that never starts (H7). |
| `ACP_BACKENDS_POD_HOME_REMAP` | A pod-spawned child has `$HOME` relocated onto the pod tree so its `$HOME`-derived credential artifacts stay pod-scoped. Its own set despite matching the sandbox set's membership today: "carries its own sandbox" and "keeps credentials under `$HOME`" are different questions. |
| `ACP_BACKENDS_ACP_RUNTIME` | Driven through `AcpRuntime` rather than its own spawn branch. |
| `ACP_BACKENDS_KIRO_IDENTITY_STORE` | Reads Kiro's identity/credential store. |
| `ACP_BACKENDS_HOST_AUTH_CALLBACK` | The child may ask this host for an access token over `_kiro/auth/getAccessToken`, answered from Crew's own vault. Membership is what authorizes handing a credential to a child at all. |
| `ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION` | Model switching lands as a config option rather than a protocol call. |
| `ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION` | Reasoning-effort push, same channel shape. |
| `ACP_BACKENDS_KIRO_SLASH_COMMANDS` | Receives `_kiro.dev/commands/execute`, **and** gets the workspace `cli.json` overlay written for it. Membership decides both, so a non-member must not collect an overlay it never reads and the membership-gated clear can never remove. |
| `ACP_BACKENDS_SESSION_MCP_ARRAY` | The harness reads its MCP surface from the `session/new` array rather than from Crew's agent spec. A non-member that is added here gets an empty array and works with every Crew tool silently absent. |
| `ACP_BACKENDS_MEMBER_DISPATCH` | Crew's member-dispatch tools are mounted into a channel-member session, with the auto-approve grant that goes with them. A harness with no per-session mount to ride is excluded, which withholds only the extra grant. |
| `ACP_BACKENDS_COMPACT` | The manual `/compact` entry points are offered. A non-member refuses the manual command up front rather than stranding the status waiter on a harness that emits no compaction status of its own. |
| `ACP_BACKENDS_INLINE_COMPACTION` | A manual `/compact` finishes inside the `session/prompt` turn, so the turn's terminal frame is the done signal. A strict subset of `ACP_BACKENDS_COMPACT`; awaiting a member's status strands the waiter, telling a non-member it is done leaves the command unacknowledged. |
| `ACP_BACKENDS_ADVERTISED_MODEL_SELECTION` | The advertised-model cache is fed on capture and the stored id is folded onto the served spelling, at spawn and on a warm-pool `set_model`. For harnesses whose wire ids are already exact this is a no-op they must not take on. |
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

**A native ACP harness is the short form of this stage, not an exemption from
it.** OpenCode serves ACP itself (`opencode acp`), so there is no adapter, no
package entry, no dependency marker and no node to resolve — `_resolve_opencode_bin`
is one executable found on one ladder. What the stage still requires is that the
spawn be *its own arm*: its argv, its handshake literal, and — because this is
where a harness is made to ask — its permission mechanism. For OpenCode that
mechanism is the child **environment** (`Routing.SPAWN_ENV`, below), set in the
spawn arm from the leaf table `ACP_BACKEND_SPAWN_ENV_POLICY` rather than spelled at
the call site.

## Stage 3a — how the harness is made to ask

Every harness declares a `Routing` in `ACP_BACKEND_ROUTING`, and the declaration
is a security claim: Kiro Crew's gate runs only from the permission-request
branch, so a harness that does not ask is a harness where none of the deny rules
execute. There are now five mechanisms, and the honest one for a new harness is
usually not the enforced one:

| Routing | Meaning | Enforced? |
|---|---|---|
| `AGENT_SPEC` | the spawn names an agent, so asking holds by construction | needs none |
| `SESSION_CONFIG` | `session/new` advertises an option whose value makes tools ask; verified and applied before the first prompt | **yes** — the only member of `ENFORCED_ROUTINGS` |
| `SEEDED_SETTINGS` | a settings file Crew writes; nothing reads back whether it took | declared, not enforced |
| `SPAWN_ENV` | variables Crew sets on the child environment, a layer no repository file outranks; read-back is a known command (`opencode debug config`) that is not yet run | declared, not enforced |
| `UNVERIFIED` | not established | always refuses |

Declaring a mechanism this core does not enforce is correct when it is true.
What is not correct is declaring `SESSION_CONFIG` for a harness that advertises
no options, or `UNVERIFIED` for one whose mechanism has been measured — both
misstate the evidence, in opposite directions. Adding a mechanism to
`ENFORCED_ROUTINGS` means implementing its verification, not editing the set.

## Stage 4 — the handshake, as your own literal

Protocol version and client capabilities stay per-harness literals (H10). Give
your harness its own `PROTOCOL_VERSION_<NAME>` **even when the number is
identical to an existing one.** That is not duplication: it makes a future
divergence a one-line edit here instead of a silent downgrade of whichever
harness happened to move first.

## Stage 5 — the install probe

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

## Stage 6 — selectability, or a named exception

With Stages 1–5 done, add the id to `BASELINE_SELECTABLE_BACKENDS`.

If it is not done — most often Stage 5 — then the id is in
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

## Stage 7 — what a live harness additionally touches

Stages 1–6 keep a harness inside `acp/`, `providers/`, and `acp_backends.py`. A
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

The Codex onboarding is a clean instance of stopping at Stage 6:

| Stage | State |
|---|---|
| 1 vocabulary | Done — `ACP_BACKEND_CODEX`, in `ACP_BACKENDS_KNOWN`, `PROVIDER_LABEL_CODEX`, policy name mapped. |
| 2 capability sets | Decided for every set: in the model and effort channels, out of the rest. All three channel sets were *created* by this work, which is why the tuning channels are three sets rather than one. |
| 3 spawn path | Done — adapter, npm package, dep marker, env override, project-local resolution. |
| 4 handshake | Done — `PROTOCOL_VERSION_CODEX`, its own literal at the same number as Claude's. |
| 5 install probe | Done — `_probe_codex` names `codex-acp` and the command that installs it. One component, not two: the adapter ships its own Codex binary. Credentials are deliberately NOT probed: a `missing` verdict disables the switch, and the checkable paths are not the only ones that authenticate a Codex, so the two-branch remedy (its own sign-in, or a `model_provider` in `~/.codex/config.toml`) is stated in the panel as a standing caveat instead. |
| 6 selectability | Selectable. `NOT_SHIPPED_SELECTABLE` is empty again, which is the healthy state. |
| routing | Done — `SESSION_CONFIG`, verified and applied as `mode=read-only` after session/new and before the first prompt, refusing otherwise. |
| residual | ACP v1 cannot require a prompt for a passive READ, so the sensitive-path block does not see this harness's reads. Mitigated at the OS boundary instead: its child cannot read the credential homes the standard tier leaves open. |
| 7 live spill | Not reached. |

The lesson worth carrying: the seam is dormant for exactly one reason, that
reason is written down where the narrowing check reads it, and closing it is a
single stage rather than a re-litigation. That is the shape to aim for — not
"complete or nothing", but "incomplete at a named stage".

## Worked example 2: the OpenCode seam

The second harness to walk this list, and the first native-ACP one. It lands
dormant with **two** named reasons, and the measurements that decided each stage
are worth more than the decisions, so they are recorded here in brief.

| Stage | State |
|---|---|
| 1 vocabulary | Done — `ACP_BACKEND_OPENCODE`, in `ACP_BACKENDS_KNOWN`, `PROVIDER_LABEL_OPENCODE`, policy name mapped. |
| 2 capability sets | Decided for all eighteen: **in the two tuning channels, out of the other sixteen.** `session/new` advertises `model` and `effort` config options (measured, pinned in `test/fixtures/acp_frames/opencode/`), the same evidence class the two adapters joined on; the client re-checks `supports_config_option` before sending. Every exclusion has a measured or documented reason in the module: one process per session, no Kiro extensions, an in-process permission policy rather than an OS sandbox, XDG-rooted stores rather than `$HOME`, refusals arriving as plain text, an advertised model list with no measured spelling gap. This example is also what fixed the count above from fifteen to eighteen: three sets had been added to the module without a row on this page. |
| 3 spawn path | Done, in the short native form — one executable (`opencode-internal`, then `opencode`, or `OPENCODE_BIN`), argv `acp --cwd <workspace>`, its own arm in `_spawn`. Two things the arm does beyond argv: set the spawn-env policy, and **refuse a workspace carrying `.opencode/plugins`** — repository code that loads into the harness process on the first prompt past every flag the harness offers (measured on 1.18.15), and that no permission key reaches. |
| 3a routing | `SPAWN_ENV`, a **new mechanism**, declared and not enforced. `OPENCODE_PERMISSION` names every permission key `ask` (a wildcard leaves unenumerated keys at the harness's own `allow` default) and outranks a project `opencode.json` — measured: a project `{"bash": {"*": "allow"}}` still asked. `OPENCODE_DISABLE_PROJECT_CONFIG=1` is mandatory, not belt-and-braces: a project agent's `permission: allow` lands *after* every environment rule and last match wins, so only removing the layer beats it. What is missing for `ENFORCED_ROUTINGS` is the read-back — `opencode debug config` prints the resolved block, so this is a command to run, not a feature to build — and the routing verdict says exactly that. |
| 4 handshake | Done — `PROTOCOL_VERSION_OPENCODE`, integer `1` like the two adapters, its own literal. |
| 5 install probe | **Not done.** No `_probe_opencode`; the install row reads `unknown`. First dormant reason. |
| 6 selectability | Dormant — named in `NOT_SHIPPED_SELECTABLE` with both reasons. |
| residual | The passive-read gap has a *different shape* here, and it is smaller. On this harness a read **is** gateable: `read: ask` produces a real `session/request_permission`, which ACP v1 was thought unable to carry. The request itself arrives with `rawInput: {}` and no path — but the `tool_call_update` before it carries `{filePath}`, and `acp/_dispatch` recovers it by `toolCallId` (the same cache built for claude's empty-then-complete pattern), so the sensitive-path block does see the path. What the OS-boundary mask would add here is defence in depth, not the primary control; it is not yet applied because the routing is unenforced. The `mode` config option (`build`/`plan`) is an agent selector, not an ask-mode, and is not a permission lever. |
| 7 live spill | Not reached. |

Two things this example adds to the list itself. A **native harness still needs
Stage 3** — the argument "it speaks ACP, there is nothing to spawn" hides the
permission mechanism, which for this harness lives in the spawn. And **Stage 2 must
be re-derived from the module, not from this page**: the set count here had
drifted, and a harness that decided "all fifteen" would have inherited three
defaults undecided.

And one about evidence. Two of this seam's capability decisions were first written
the wrong way round, on a probe that checked `session/new` for a `modes` key and
never looked at `configOptions`, and on reading the permission request's
`rawInput` without the `tool_call_update` that precedes it. Both were caught by the
**frame-replay fixture** (`test/fixtures/acp_frames/<id>/`), because a recorded
turn replayed through the real dispatch layer shows what the gate actually
receives, where a bespoke probe shows what its author thought to ask. Record the
corpus before deciding Stage 2, not after.

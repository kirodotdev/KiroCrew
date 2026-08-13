# Harness parity: Kiro first, everything else adapted

A *harness* is the agent process Kiro Crew drives over ACP. Kiro Crew has one
first-class harness — `kiro-cli` (`ACP_BACKEND_KIRO`, spelled `""`) — and a
growing set of adapted ones: the dormant `ACP_BACKEND_CLAUDE` seam, `KAS`
(`ACP_BACKEND_KAS`), and whatever a bring-your-own (BYO) adapter registers next.

*Parity* here does not mean equal treatment. It means the opposite, stated
precisely: **an added harness may only adapt itself to the seams the Kiro
harness already runs through. It may not move, widen, generalize, or add a
branch to those seams.** A harness that cannot be adapted without changing the
Kiro path is not ready to land.

The failure mode this file exists to prevent is not a broken adapter — that
fails loudly on its own first session. It is the *silent capture* of the Kiro
path: a call site that spells "kiro" as `not is_<other>_backend`, so harness
number three inherits a capability, a sandbox waiver, or a session label that
nobody granted it, and the Kiro user who never opted into BYO pays for it. Two
such sites shipped before this file existed
(`AcpProvider.is_session_sharing_eligible`, `AcpRuntime.spawn`'s
`is_kiro_cli`); both read as correct until you count the backends.

The transports these invariants constrain are specified in
[acp-client.md](acp-client.md) (framing, timeouts, the backend seam) and
[providers.md](providers.md) (the `LLMProvider` surface). The edition-level
registration seam is in
[platform-context.md](platform-context.md). This file only catalogs the
invariants and names what pins each one.

## How to read a row

- **Guarantees** is the property that goes RED when broken, not the
  implementation that happens to satisfy it today.
- **Pinned by** names the test module and function. Test modules live at `test/`
  in the repo root; sources live at `src/kiro_crew/`. A row marked
  *review-only* has no deterministic test — it is enforced by the
  `harness-parity` rule in `AUTOSDE.yaml`, which every AI review lane reads.
- An invariant is *closed* by its test, not by this document. If a row
  disagrees with the named test, the test is right.
- The ids are stable. Source docstrings and review findings cite them bare
  (`H4`, `H6`), so the id is the lookup key.

## Group A: Kiro is the default and the floor

These break by *addition*: a harness lands, nothing at these sites is edited,
and Kiro stops being the guaranteed path.

| Id | Guarantees | Pinned by | Constrains |
|---|---|---|---|
| H1 | `agent.acp_backend` defaults to `ACP_BACKEND_KIRO`, and `ACP_BACKEND_KIRO` is in `ACP_BACKENDS_SELECTABLE` unconditionally. An operator who configures nothing, and an operator whose configuration is unusable, both get the Kiro harness. | `test_harness_parity.py::test_kiro_is_the_default_backend`, `::test_kiro_is_always_selectable` | `config/loader.py` (`AgentConfig.acp_backend`), `acp/types.py` (`ACP_BACKENDS_SELECTABLE`) |
| H2 | A harness is chosen at `agent.acp_backend`. `agent.provider` stays `enum=["acp"]`: there is one provider and it is never the harness selector, because a second provider value would route around every invariant below. | `test_harness_parity.py::test_provider_enum_is_acp_only` | `config/loader.py` (`AgentConfig.provider`, `build_provider_factory`) |
| H3 | An unknown or unselectable persisted backend degrades to Kiro with a logged reason. It never raises and never survives — including the non-string shapes a hand-edited `config.json` can hold. Startup refusing with a reason is the contract; a stack trace or a silent foreign spawn is not. | `test_harness_parity.py::test_unselectable_backend_degrades_to_kiro` | `config/loader.py` (`_normalize_acp_backend`) |
| H4 | Enum membership and selectability stay two mechanisms. `validate_config_data` *deletes* an out-of-enum value before the loader sees it, and the degrade log only fires on a non-empty value — so a preview harness missing from the enum vanishes with no log line at all. Everything the enum admits must still resolve to a selectable backend. | `test_harness_parity.py::test_enum_and_selectability_are_separate` | `config/loader.py` (`AgentConfig.acp_backend` metadata), `config/validation.py` (`validate_config_data`) |

## Group B: identity is tested positively

The whole group is one rule with several faces: **no call site may express
"this is the Kiro harness" as the absence of another harness.** A negative test
is correct exactly until the next harness exists, and it fails *open* — the new
harness is treated as Kiro.

| Id | Guarantees | Pinned by | Constrains |
|---|---|---|---|
| H5 | Harness identity is a positive comparison against a named constant, or membership in a named set. `not is_claude_backend`, `!= ACP_BACKEND_KAS`, and `== "kas"` (bare literal) are all forbidden; `is_kiro_backend` and `backend in ACP_BACKENDS_<CAP>` are the forms. Enforced on the lines a change ADDS, not whole-tree — see the gate doc for why. | `scripts/check_harness_parity.py` (six rules, self-tested), `test_harness_parity.py::test_added_line_gate_self_test_passes`, `::test_added_line_gate_flags_a_planted_negative_test` | every module reading `AcpClient.backend` / `AcpProvider.is_*_backend` |
| H6 | A capability is granted by opt-in membership, never by negation. `is_session_sharing_eligible` reads `ACP_BACKENDS_SESSION_SHARING` and `supports_steer` reads `ACP_BACKENDS_STEER`, so a harness that has not demonstrated the capability does not inherit it from a set it was never added to. | `test_harness_parity.py::test_session_sharing_is_opt_in`, `::test_steer_is_opt_in` | `providers/acp.py` (`AcpProvider.is_session_sharing_eligible`), `acp/client.py` (`AcpClient.supports_steer`), `acp/types.py` |
| H7 | `is_kiro_cli` is a positive Kiro test at every call site. It drives a macOS delegation in which `sandbox.wrap_argv` skips Kiro Crew's own seatbelt because `kiro-cli`'s internal sandbox cannot nest inside it. Passed for a harness with no internal sandbox, it hands isolation to a layer that never starts. This one fails open into an unconfined agent process, which is why it is the only Group B row that is also a security invariant. | `test_harness_parity.py::test_is_kiro_cli_is_positive` | `acp/runtime.py` (`AcpRuntime.spawn`), `acp/client.py` (`AcpClient.ensure_ready`), `sandbox.py` (`wrap_argv`, `_spawns_kiro_cli`) |
| H8 | New harness identifiers live in `acp/types.py` and are added to `ACP_BACKENDS_KNOWN`; every capability set is a subset of it; and `AcpProvider.__init__` rejects anything outside it. `ACP_BACKEND_KIRO` is the empty string, so a value that falls through every identity check spawns `kiro-cli` under a foreign label. | `test_harness_parity.py::test_capability_sets_are_subsets_of_known_backends`, `::test_unknown_backend_rejected_at_construction` | `acp/types.py` (`ACP_BACKENDS_KNOWN`), `providers/acp.py` (`AcpProvider.__init__`) |

## Group C: the Kiro path keeps its own machinery

An adapter that lands by *generalizing* a Kiro-specific step to a
lowest-common-denominator one has degraded the Kiro session even when every
test still passes.

| Id | Guarantees | Pinned by | Constrains |
|---|---|---|---|
| H9 | `kiro-cli` remains the default branch of spawn-argv resolution, keeping its pre-spawn agent materialization (`kiro-cli` discovers selectable modes from `~/.kiro/agents/*.json` at startup, so a later `set_mode` fails with "Mode not found" without it) and its `--model` pin (the only way to run a model outside the agent's provider). A dict-of-builders refactor that treats Kiro as one entry among N drops both. | `test_harness_parity.py::test_kiro_spawn_argv_keeps_its_own_branch` | `acp/runtime.py` (`AcpRuntime._resolve_spawn_argv`) |
| H10 | Protocol version and client capabilities stay per-harness literals. Collapsing them to one handshake that every harness accepts silently downgrades the Kiro session's declared capabilities. | `test_harness_parity.py::test_handshake_is_per_backend` | `acp/runtime.py` (`AcpRuntime.spawn`), `acp/types.py` (`ACP_CLIENT_CAPABILITIES`, `KAS_CLIENT_CAPABILITIES`) |
| H11 | The provider label is a closed mapping and an absent label means Kiro. It indexes resume compatibility, session-map persistence, and session-file cleanup routing, so a harness with no `PROVIDER_LABEL_*` of its own persists as a Kiro session and its transcript is pruned for want of a Kiro session file. | `test_harness_parity.py::test_every_known_backend_has_a_label` | `acp/types.py` (`PROVIDER_LABEL_*`), `providers/acp.py` (`provider_label`, `cleanup_session`), `session.py` (`detect_provider_switch`) |
| H12 | Model pre-flight keeps "empty or unknown advertised set means allow", and never compares ids across harness namespaces. Harnesses advertise ids in their own spelling; one shared membership test across two namespaces calls every legitimate model unusable and withholds the model. | `test_harness_parity.py::test_model_preflight_allows_unknown_advertised_set` | `acp/client.py` (`model_is_unusable`, `advertised_model_ids`) |

## Group D: review-only invariants

Deterministically un-pinnable — they are properties of a change, not of a tree,
and the absence of a mechanism is not something a source scan can see. The
`harness-parity` rule in `AUTOSDE.yaml` carries them to every AI review lane.

| Id | Guarantees | Pinned by | Constrains |
|---|---|---|---|
| H13 | Harness support is additive at the `ProviderRegistry` seam: a v1 addition, no `CONTRACT_VERSION` bump. The Kiro construction path gains no conditional, no new required argument, and no new failure mode in service of an adapter. | review-only (`AUTOSDE.yaml` → `harness-parity`) | `platform/interfaces.py` (`ProviderRegistry`), `config/loader.py` (`create_provider_factory`) |
| H14 | A capability the session layer reads off a provider is declared on `LLMProvider` with a safe default. An adapter never forces a `hasattr` / `getattr` probe onto the Kiro path, and never leaves a Kiro-only attribute reachable through the ABC where a missing one reads as `False`. | review-only (`AUTOSDE.yaml` → `harness-parity`) | `providers/base.py` (`LLMProvider`), `providers/acp.py` (`AcpProvider`) |

## The CI half

The added-line gate that enforces Group B on a diff is
[../../ci/harness-parity-gate.md](../../ci/harness-parity-gate.md). The
structural invariants (Groups A and C) are pinned by
`test/test_harness_parity.py` and therefore fail in the ordinary test job, not
in a separate gate. Group D reaches the four AI review lanes through
`AUTOSDE.yaml`'s `harness-parity` rule, which every lane's prompt treats as the
source of truth for what blocks.

## Adding or changing an invariant

1. Write the test first: an invariant is its test, and this table is the index.
   A row whose *Pinned by* cell names nothing is a wish.
2. Cite the id in the source docstring it constrains, and add the row here in
   the same change.
3. Never relax a check to make a red invariant green. A parity failure that
   flips GREEN because the Kiro path was made to match the adapter is the
   regression this file exists to catch, not a fix. If a harness genuinely
   cannot be adapted within these invariants, the correct outcome is that the
   harness does not land yet — say so in the PR instead of widening a seam.
4. A new harness adds rows to `ACP_BACKENDS_KNOWN`, a `PROVIDER_LABEL_*`, and
   an explicit decision for every Group B membership set. "Inherited the
   default" is not a decision.

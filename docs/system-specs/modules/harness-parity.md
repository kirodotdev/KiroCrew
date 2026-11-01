# Harness parity: Kiro first, everything else adapted

A *harness* is the agent process Kiro Crew drives over ACP. Kiro Crew has one
first-class harness — `kiro-cli` (`ACP_BACKEND_KIRO`, spelled `""`) — and a
growing set of adapted ones: Claude Code (`ACP_BACKEND_CLAUDE`), `KAS`
(`ACP_BACKEND_KAS`), Codex (`ACP_BACKEND_CODEX`), OpenCode
(`ACP_BACKEND_OPENCODE`), Pi (`ACP_BACKEND_PI`), goose (`ACP_BACKEND_GOOSE`),
DeepSeek Harness (`ACP_BACKEND_DEEPSEEK`), GitHub Copilot CLI
(`ACP_BACKEND_COPILOT`) and Strands (`ACP_BACKEND_STRANDS`), plus whatever a bring-your-own
(BYO) adapter registers next. Adding a harness requires reviewing
`session_pid_orphan_scan.py::_BROWSER_PLAUSIBLE_OWNER_NAMES` as a separate
touchpoint, because an unreadable environment on a recognizable harness process
must keep its browser daemon alive.

Kiro, Claude Code, KAS, Strands, Codex, OpenCode, Pi, goose, DeepSeek and Copilot are selectable on a
plain public build. Claude Code in particular is a shipped harness and not a dormant seam: `acp/harness/claude.py` owns the
whole Claude launch and the adapter is a public npm package, so an earlier
revision that left it out of the baseline removed only the switch, never a
capability. Whether the binaries are INSTALLED on a given machine is a different
question, answered by `agent_sdk/backend_install.py`'s probe rather than by
selectability.

`BASELINE_SELECTABLE_BACKENDS` is otherwise `ACP_BACKENDS_KNOWN`, so an id this
core can spell is an id an operator can choose unless something states the
exception — pinned by
`test_agent_backend_editable.py::test_baseline_ships_every_known_backend`, which
guards against an undocumented NARROWING rather than a widening.
There is no exception today: `NOT_SHIPPED_SELECTABLE` is empty, which is the state
to return to. `ACP_BACKEND_DEEPSEEK` was the most recent member. It passed the
install-probe half of the bar and failed the routing half — its own sandbox decided
its tool calls, so Crew's PreToolUse gate would not run for what a session actually
did — and it left the set the way the bar demands, through the routing half rather
than a relaxed assertion: Crew composes its own gate plugin into the harness and
reads the plugin's load marker back before the first prompt
(`Routing.VERIFIED_GATE_EXTENSION`, `agent_sdk/backends_routing.py`).

`ACP_BACKEND_CODEX` is the earlier instance of that return: it left the
set once both halves landed — `backend_install.py` gained its probe, so the install row
names the missing component and its command instead of reading `unknown`, and
`acp_tool_gate` established that its tool calls reach the PreToolUse gate.

Read the invariants below against that tree: ten harnesses can serve a real
session today, so a site that spells "kiro" by exclusion is already wrong on
nine of them.

*Parity* here does not mean equal treatment. It means the opposite, stated
precisely: **an added harness may only adapt itself to the seams the Kiro
harness already runs through. It may not move, widen, generalize, or add a
branch to those seams.** A harness that cannot be adapted without changing the
Kiro path is not ready to land.

The failure mode this file exists to prevent is not a broken adapter — that
fails loudly on its own first session. It is the *silent capture* of the Kiro
path: a call site that spells "kiro" as `not is_<other>_backend`, so harness
number three inherits a capability, a sandbox waiver, or a session label that
nobody granted it, and the Kiro user who never chose another harness pays for it.
Two such sites shipped before this file existed
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
  (`H4`, `H6`), so the id is the lookup key. `scripts/docs_lint.py` validates
  citation-shaped source comments (`# H6:` and `(H13)`) against this table, so
  a bare ID cannot silently name a row that does not exist.

## Group A: Kiro is the default and the floor

These break by *addition*: a harness lands, nothing at these sites is edited,
and Kiro stops being the guaranteed path.

| Id | Guarantees | Pinned by | Constrains |
|---|---|---|---|
| H1 | An `agent.acp_backend` that is absent or `""` holds no choice and defaults to `ACP_BACKEND_KAS`. `ACP_BACKEND_KIRO` remains the unconditional selectable floor. A manual choice is stored; Kiro CLI is stored as `"kiro"` (`STORED_BACKEND_KIRO`, the policy id) and mapped to the runtime id `""` at the config boundary only (`backend_from_stored` / `backend_to_stored`). An explicit Kiro CLI pick runs on `KIRO_SELECTION_BACKEND` (KAS) once this process confirmed the host's kiro-cli offers its v3 engine (`confirm_kiro_cli_offers_kas`, positive evidence only) and while KAS is selectable, because kiro-cli serves `acp` on v3 and only the KAS path drives it; the stored pick is read as KAS at config load (`_read_backend_choice` → `kiro_cli_pick_backend`), never at the gate, so every capability set reads KAS's membership, no call site tests "picked Kiro", and a Kiro CLI id the gate already answered (a degraded member floor) is never moved again. The gateway's KAS floor confirms the evidence at boot and reloads the live config so the provider factory follows the verdict; a non-gateway process that spawns an ACP agent (`kirocrew chat`, the eval runner, the task runner, history consolidation) confirms it the same way before its config load, via `ensure_kiro_cli_kas_verdict`, which runs the one bounded probe only for a stored Kiro CLI pick off the bundle route (`docs/decisions/2026-10-28-a-kiro-cli-choice-runs-on-kas.md`). Without that confirmation (an unknown probe, a kiro-cli without v3, the bundle route, a policy that denies KAS) the pick keeps the v2 Kiro CLI launch, and it never refuses a session. The floor a degraded value takes (H3) is not a pick and stays the Kiro CLI launch. | `test_harness_parity.py::test_kas_is_the_default_backend`, `::test_kiro_is_always_selectable`, `test_acp_backend_kas.py::TestAKiroCliPickRunsOnKasOnceV3IsConfirmed`, `test_acp_backend_kas.py::TestAProcessOutsideTheGatewayReachesTheSameVerdict`, `test_cli_chat.py::test_a_stored_kiro_pick_runs_on_kas_in_cli_chat`, `test_kas_cli_floor.py::TestDefaultNoticeWording::test_a_v3_kiro_cli_moves_the_pick_onto_kas`, `::test_no_positive_verdict_keeps_the_pick_on_kiro_cli` | `config/sections_agent.py` (`AgentConfig.acp_backend`), `agent_sdk/backends.py` (`DEFAULT_ACP_BACKEND`, `KIRO_SELECTION_BACKEND`, `GOVERNANCE_FLOOR_BACKEND`, `resolve_selected_backend`, `ensure_kiro_cli_kas_verdict`) |
| H2 | A harness is chosen at `agent.acp_backend`. `agent.provider` stays `enum=["acp"]`: there is one provider and it is never the harness selector, because a second provider value would route around every invariant below. | `test_harness_parity.py::test_provider_enum_is_acp_only` | `config/loader.py` (`AgentConfig.provider`, `build_provider_factory`) |
| H3 | An unknown or unselectable explicit backend degrades to the unconditional Kiro floor with a logged reason. Only "no choice" (absent or `""`) selects KAS, and it selects the Kiro floor silently when KAS is unavailable. It never raises and never survives — including the non-string shapes a hand-edited `config.json` can hold. There is exactly ONE resolution gate, and it reads `selectable_backends()` per call, so registering a backend is what makes a persisted value survive. It must never read the platform context because that would re-enter config loading. A crewmate's own harness pin is the one value checked before this gate and refused there, never here (H17). | `test_harness_parity.py::test_unselectable_backend_degrades_to_default`, `::test_registering_a_backend_makes_it_survive_load`, `::test_config_load_never_reads_the_platform_context` | `agent_sdk/backends.py` (`resolve_selected_backend`), `config/loader.py` (`_normalize_acp_backend`), `config/validation.py` (`validate_config_data`) |
| H4 | Selectability has exactly ONE gate, and it logs. `AgentConfig.acp_backend` carries no static `enum`: a literal was frozen at import, before an edition registers a backend, and `validate_config_data` *deletes* an out-of-enum value before the loader sees it — so a registered preview harness was stripped from `config.json` with no degrade log at all. `resolve_selected_backend` is the gate; `GET /api/config/schema` supplies the live values the dashboard renders. | `test_harness_parity.py::test_selectability_has_one_logged_gate` | `config/loader.py` (`AgentConfig.acp_backend` metadata), `config/validation.py` (`validate_config_data`), `dashboard/handlers/agents.py` (`_supply_live_enum`) |
| H17 | A crewmate's own harness pin (`agents.<name>.acp_backend`) is the one narrow exception to H3's degrade, and it is taken BEFORE the gate, never inside it. A pin this gateway cannot start (unknown, denied by policy, or dropped from `selectable_backends()` after it was saved, e.g. by `apply_selectable_denials` or `mark_backend_unservable`) refuses that crewmate's sessions, its DM chat and the workers it opens, with `CrewmateHarnessUnavailable` (`crewmate_harness_unavailable`) before any process exists, and never falls back to Kiro or KAS: running the crewmate on a harness it was moved away from is what the pin exists to prevent. The refusal is the pre-gate check `crewmate_backend_pin`, which every `select_provider_backend` caller evaluates as its `crew_backend` argument (only that call or `None`); the gate receives a `StartableCrewPin` already confirmed, returns its backend as is, and never raises. A stored `kiro` pin moves onto KAS at that check under the same rule as `agent.acp_backend` (`kiro_cli_pick_backend()` off the KAS bundle route, `docs/decisions/2026-10-28-a-kiro-cli-choice-runs-on-kas.md`), never inside the gate. `agent.acp_backend`, `agent.member_acp_backend` and a `spawn_run(backend=...)` override keep H3's degrade-with-a-logged-reason unchanged. | `test_crewmate_harness.py::TestThePreGateCheck::test_an_unstartable_pin_refuses_instead_of_falling_back`, `::TestThePreGateCheck::test_a_harness_that_stopped_being_selectable_refuses`, `::TestThePreGateCheck::test_the_provider_factory_refuses_before_the_gate`, `::TestSelectionGate::test_the_gate_never_raises_for_a_crew_pin`, `::TestEveryCallerPassesThePin::test_every_select_provider_backend_call_names_crew_backend`, `::TestWorkerBackend::test_an_unstartable_pin_refuses`, `::TestAKiroPinOnAV3KiroCli::test_confirmed_v3_runs_the_dm_and_the_worker_on_kas`, `::TestAKiroPinOnAV3KiroCli::test_the_kas_bundle_route_keeps_the_kiro_cli_launch` | `crewmate_harness.py` (`crewmate_backend_pin`, `CrewBackendPin.checked`, `StartableCrewPin`, `pinned_backend`), `members_parts/member_sessions.py` (`select_provider_backend`), `config/loader_config.py` (`create_provider_factory`), `mate_worker.py` (`crewmate_worker_backend`) |

## Group B: identity is tested positively

The whole group is one rule with several faces: **no call site may express
"this is the Kiro harness" as the absence of another harness.** A negative test
is correct only while one harness can start, and it fails *open* — the other
harness is treated as Kiro. Several are selectable today, so `not
is_claude_backend` is not a rule waiting on a future harness to break it: it
already reads TRUE for KAS on a plain public build.

| Id | Guarantees | Pinned by | Constrains |
|---|---|---|---|
| H5 | Harness identity is a positive comparison against a named constant, or membership in a named set. `not is_claude_backend`, `!= ACP_BACKEND_KAS`, and `== "kas"` (bare literal) are all forbidden; `is_kiro_backend` and `backend in ACP_BACKENDS_<CAP>` are the forms. Enforced on the lines a change ADDS, not whole-tree — see the gate doc for why. | `scripts/check_harness_parity.py` (six rules, self-tested), `test_harness_parity.py::test_added_line_gate_self_test_passes`, `::test_added_line_gate_flags_a_planted_negative_test` | every module reading `AcpClient.backend` / `AcpProvider.is_*_backend` |
| H6 | A capability is granted by opt-in membership, never by negation. `is_session_sharing_eligible` reads `ACP_BACKENDS_SESSION_SHARING` and `supports_steer` reads the steer tables described below, so a harness that has not demonstrated the capability does not inherit it from a set it was never added to. Codex is PRESENT in both the sharing set and `ACP_BACKENDS_SESSION_EVICTION` while KAS is present only in the second, and the two answers have different owners — which is why they are two sets, and why the same harness can answer them differently. Eviction is the harness's own property, earned by measurement: the teardown Crew sends it is the standard `session/close` as a request, after which the sessionId stops answering (it was excluded while that verb was `session/cancel`, which frees nothing; the same `close` sent as a notification is ignored, so the delivery is part of the fact). That one membership is what admits codex to every create-and-destroy path on the shared process at once — the high-churn background handles, warm pooled reuse, the entitlement probe — instead of each path gating codex by name. Sharing is granted, and on a fact the eviction answer does not contain: the thread codex persists under `CODEX_HOME` SURVIVES that evicting close, so a `session/load` on the closed id restores the conversation — measured on the same adapter, including from a restarted adapter process, which is the shape a `spawn_continue` actually takes. Teardown is unchanged by it: a shared subagent session is closed like any other, because one left resident would hold its own MCP fleet on a runtime nobody is using. KAS is the harness that now shows why the two sets cannot be one — its `_kiro/session/delete` frees the session (eviction) by removing the record a continuation would load (no sharing), so the two memberships read the same verb and reach opposite answers. **Both drivers answer from the tables, not one of them:** `AcpSessionHandle.supports_steer` reads them through the runtime's own backend id, because a constant that was true while `AcpRuntime` served a single host becomes a claim about the second host the moment one is added — and an advertised steer is met with `-32601` at the user's mid-turn correction. Steer is three tables because it is three verbs and two questions: kiro-cli's fire-and-forget `_session/steer` (`ACP_BACKENDS_STEER`), an awaited steering request (`ACP_BACKENDS_STEERING_REQUEST`): the `_session/steering` request of codex-acp and claude-agent-acp, whose answer (`injected` / `startedNewTurn` / `failed`, and claude's `promptRequired`) is the delivery evidence, and goose's `_goose/unstable/session/steer`, which only `AcpClient` speaks (`_CLIENT_STEERING_REQUEST_METHODS`); and opencode's second `session/prompt`, which joins the running loop and is answered at the same idle as the turn (`ACP_BACKENDS_STEER_CONCURRENT_PROMPT`, spoken only by `AcpClient`). The handle speaks `_session/steering` for codex; `AcpClient` speaks it for claude, which runs one adapter per session, so the turn's own read loop reads the answer. claude earns membership on its own evidence, driven live on 0.65.0 and 0.89.1 (real adapter, Agent SDK and `claude` binary, model API served locally): the adapter advertises `initialize._meta.steering.supported`, pushes the text into the running Agent SDK turn and holds that turn open until a result answers the steer, so the model's next request carries the steered text and the `session/prompt` terminal follows its reply; a request sent with `idleBehavior: "promptRequired"` to a session whose turn has settled runs nothing and answers `promptRequired`. 0.64.x answers `injected` and then ends the turn without running the text, so `AcpClient` speaks the request only from 0.65.0 (`CLAUDE_ACP_STEERING_MIN_VERSION`). So a claude steer is True only for `injected` into the same still-running turn, is reported consumed only at a clean `end_turn`, and is queued on every other answer or ending -- including a turn that ends before the steer lands. A claude session below 0.65.0, or one whose handshake did not advertise the request, takes no steer and its steer queues; the harness card's `mid_turn_steer` line reads membership only, so a new dashboard chat whose adapter reports a release below its steer floor (claude-agent-acp 0.65.0, or goose 1.38.0, the first release with `_goose/unstable/session/steer`, block/goose#9560) gets one notice row naming the installed version, the minimum and the upgrade command (`steer_version_issue`, read through `AcpProvider.steer_upgrade_notice`). The session still runs; a session that reports no readable version gets no notice, as pi's floor reads it. Crew's claude reject is a `deny` without `interrupt`, which does not end the turn, so a denial drops no claude steer; a cancel still can, which is why claude is in `steer_needs_loss_recovery` with codex. `AcpClient` sends no claude steer while its turn is parked on an approval either: nothing reads the answer until the approval is answered, so the steer is queued. A user's steer reads any of them (`supports_steer` on the handle is the union of the first two, and on the client and the card's `mid_turn_steer` line the union of all three); a deny notice steered into a refused turn reads `ACP_BACKENDS_STEER` alone through `supports_refusal_steer`, and taking back a steer the turn has not read reads `ACP_BACKENDS_STEER_RETRACT` through `supports_steer_retract` (kiro-cli's `_session/steer/clear`; KAS answers the same request but names steers its turn already read, so it is not a member), because codex's approval answer cancels the turn and drops what was injected into it. For the same reason the handle sends no codex steer while a turn is parked on an approval; the steer is queued instead. A steer codex accepted is not safe from a LATER denial or a cancel either: codex drops it with the cut turn. So a codex steer is reported consumed only when its turn ends cleanly, with no denied approval, no cancel and no refusal; on any other ending nothing is reported and the composer's pending entry is queued by the turn's teardown, so the text runs as its own turn instead of being lost. Only the dashboard composer keeps such an entry, so only it steers codex: `steer_needs_loss_recovery` makes `AcpProvider.supports_steer` False and `AcpProvider.steer` refuse, which sends the messaging channels and Side Chat down their queue paths, and `spawn_steer` refuses with `steer_unsupported` in favour of `mode='follow_up'`. The composer's wait for codex's answer is bounded below the browser's send abort, and a slower answer takes the queue path. An opencode steer follows the same consumed-only-at-a-clean-end rule with different evidence: the turn's clean `end_turn` must come with the steer's own `end_turn` answer within a short grace and with no other loop's output before it, and no approval in the turn may have been denied, because a denial ends opencode's loop before it reads what came after it. A steer whose turn ended first runs a loop of its own; the client cancels it and drains its frames, at the turn's end or before the next prompt, so none reach another turn, and the composer queues the text. An opencode build that refuses a second prompt answers it with an error, which is the same queue path. A codex steer is at-least-once: codex-acp orders the steering answer against the prompt's terminal in no way and sends no consumption echo, so an `injected` answer read after the terminal, or a steer codex acted on before a later cut, leaves the steer queued as well, and it runs a second time as its own turn. goose earns the same membership on its own wire, measured on a live goose 1.54.0: the steer names the run goose announced in `session_info_update._meta.goose.activeRunId`, the answer `{runId, messageId}` means queued for that run, and goose echoes the steer as a `user_message_chunk` marked `_meta.goose.steer` with the same `messageId` once the turn reads it; that echo, matched by id, is the only thing reported consumed. A steer still queued when the run ends is discarded by goose (a cancel after the answer leaves no echo and no model call sees the text), so the composer's pending entry is queued by the teardown, and `steer_needs_loss_recovery` routes every other surface to its queue path, as for codex. No goose steer is sent between turns, before the run id arrives, or while an approval is open. `store_session_config` is the same case for a model list: a host whose models arrive as a `model` select in `configOptions` rather than as a `models` object has them folded in by `session_models_envelope`, gated on `ACP_BACKENDS_ADVERTISED_MODEL_SELECTION` and read by BOTH the session-init capture and the entitlement probe, or its picker is empty on the runtime path while full on the client one and the probe cannot heal a degraded snapshot. **A COMPLETENESS gate backs all of these, because the per-site pins above only catch the sites that exist.** Two halves in `test_harness_parity.py`: every runtime-path per-host answer is asserted equal to its table for every backend in `ACP_BACKENDS_KNOWN` — not only for the hosts the site was written against, so a divergence is caught before a third host is admitted — and every backend-identity comparison in `acp/runtime.py`, `acp/session_handle.py` and the runtime and session_handle owner modules their code moved to must appear in `_DECLARED_IDENTITY_TESTS` with a reason, so a NEW site answering from identity goes red until its author either points it at the table that already answers it or records why identity is honest there. The declaration list is pruned by its own test, so it cannot rot into a blanket pre-approval. One gate run answers for every site and every known host at once, which is what a per-site pin cannot do. Every *tuning channel* follows the same rule, one set per channel because a harness can implement one and not another: `ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION` (model switch), `ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION` (effort push), `ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS` (whether an advertised `<model>[<effort>]` id is applied as two config-option writes -- and, with it, whether a refusal of an advertised id reads as an adapter mismatch rather than an entitlement verdict), and `ACP_BACKENDS_KIRO_SLASH_COMMANDS` — membership in the last decides who is sent `_kiro.dev/commands/execute`, `/effort` included. The two effort channels are disjoint sets (a test pins it): KAS is outside the command set because it refuses the verb with `-32603` (`test/fixtures/acp_frames/kas/commands-refused-live.jsonl`) and takes effort as a config option only. Every effort site reads the channel through `capabilities_for(backend)` (`effort_via_config_option` / `effort_via_slash_command`), never raw set membership. The kiro-cli-or-KAS branch of the effort live path (`clear_effort`, the unpinned re-apply) and the gate in front of the overlay writers in `providers/acp.py` read `ACP_BACKENDS_KIRO_FAMILY` instead, a family set rather than a capability; the workspace `cli.json` **effort** keys have their own narrower reader set, `ACP_BACKENDS_EFFORT_OVERLAY` (kiro-cli only), which every site that writes, reads back or clears them is gated on, because KAS never opens the file and the file is shared by every session in the work dir; the Tool Search keys in that same file have a narrower reader set too, `ACP_BACKENDS_TOOL_SEARCH_OVERLAY` (kiro-cli only), because KAS takes that setting from `initialize`'s `_meta.kiro.settings` instead — `ACP_BACKENDS_CLIENT_META_SETTINGS`, read by the harness's `client_meta_settings` and filled by `AcpRuntime._handshake_client_capabilities`. A harness in none of these must not inherit a channel that answers `-32601`, nor collect an overlay it never reads and the membership-gated clear can never remove, nor have a settings channel it never opened filled for it. Running on the shared `AcpRuntime` does not imply reading that overlay: `AcpProvider.is_acp_runtime_backend` reads `ACP_BACKENDS_ACP_RUNTIME` and answers the TRANSPORT question only, so the overlay sites key off the overlay's own reader sets (`ACP_BACKENDS_EFFORT_OVERLAY`, `ACP_BACKENDS_TOOL_SEARCH_OVERLAY`) rather than off it — codex and KAS run on the runtime, read no `cli.json`, and take effort over `session/set_config_option`. `ACP_BACKENDS_MCP_CONFIG_HOT_RELOAD` follows the same rule for a *skip*: membership is what lets the dashboard's MCP sync leave running sessions alone after a config write, because the harness reconciles the agent file itself (kiro-cli, from the release its reconcile was verified on; `mcp_hot_reload.py` holds every live process to that floor, read from its own `initialize` handshake). A harness that never demonstrated the reconcile would otherwise have its users' freshly installed servers stay unmounted with nothing red to say why. `ACP_BACKENDS_MCP_WIRE_REINJECT` is the OTHER way that same skip is earned, and it is a separate set because it is a separate property: KAS's servers travel inside its projected agent and the engine registers the batch once, so no file watcher applies a change there; Crew re-sends the projection at the session's next turn boundary (`session/close` of the resident session, `session/load` of the same id with the new batch, `session/set_mode` -- measured in `test/fixtures/acp_frames/kas/mcp-live-apply-live.jsonl`; a re-sent `set_mode` and a load of a resident session were both measured ignored). `AcpSessionProvider.mcp_config_wire_reinject` declares it, `mcp_hot_reload.live_sessions_apply_mcp_live` reads both sets, and the mark that makes the skip correct (`mark_mcp_config_changed`) is handed only to members. The added path adapts to the seams the first load already runs through -- `AcpRuntime.load_session` with every guard it carries -- and adds nothing to the kiro-cli path, which keeps its own watcher. `ACP_BACKENDS_TEMPLATE_ADDITIONS_AT_SPAWN` is the same rule for a template Crew does not own: membership is what lets a server the user approved for it reach its sessions without Crew writing the spec, because the member reads the spec per session (KAS over the wire, claude and codex through the `session/new` array, each with a live capture of a spec-declared server running); kiro-cli loads that template off disk with no per-session channel and is not a member, so `template_mcp_additions.addition_support` answers unsupported there rather than writing a file Crew does not own. `ACP_BACKENDS_POD_HOME_REMAP` is a further instance and the clearest illustration of why one set per capability matters: it decides whose pod-spawned child has its ambient `HOME` relocated so the harness's `$HOME`-derived OAuth grant store stays pod-scoped (`acp.client_spawn_env._apply_pod_home_remap`). Its membership is identical to `ACP_BACKENDS_INTERNAL_SANDBOX` today, and reusing that set would still be wrong — "carries its own OS sandbox" and "relocating `HOME` moves its credential store" are different properties, so a harness added there for sandbox reasons would silently inherit credential relocation it never opted into. A channel's option ID is a per-harness spelling rather than a membership question, so it is resolved through `effort_config_option_id(backend)` (default `effort`, which claude and opencode take; `EFFORT_CONFIG_OPTION_IDS` names the exceptions: `reasoning_effort` for codex and deepseek, `thinking_effort` for goose, `effortLevel` for KAS, `thought_level` for pi) and read at every effort site; a site naming one spelling writes an option the other adapter answers "unknown config option" to, which each caller reads as "no effort selector" and skips, so the failure is silent and the session runs a level the UI does not report. `ACP_BACKENDS_SIDE_READONLY` gates whether a Side Chat turn may execute read-only tools under `READ_ONLY` at all: a harness's own pre-approval surface is one the host gate cannot see, so a member confines its side session where that surface cannot reach a write (kiro-cli through the derived `<agent>--readonly` spec read off disk, KAS through the same spec received as a projected custom agent with its `permissions` emptied, claude through a session started with only its read built-ins and pinned to `default` mode, goose through a goose home of its own under the session's scratch window with no `permission.yaml` and no MCP server, and deepseek through Crew's verified gate plugin with stock-tool ownership proved per call), and off the set the side turn runs `REJECT_ALL` (see `side.md`). A KAS member proves the same reads as kiro-cli: its tool_call frame names no tool, so the gate reads the permission frame's engine-written `_meta.kiro.toolId` under its kiro-cli policy name, on KAS alone (`kas_builtin_ids`), and five ids reach the host read-only allowlist (`read_file`, `list_directory`, `grep_search`, `file_search`, and `remote_web_search` under `web_search`, as kiro-cli's `web_search` does); an MCP tool that calls itself `read_file` carries a server and is refused (`test_side_readonly_kas.py`). A goose member proves a read from the (extension, tool) pair goose stamps, as written: `(developer, tree)` and `(developer, read_image)` only, so a server named `developer` serving `tree` (stated `developer__tree`) is refused (`test_side_readonly_goose.py`). A deepseek member proves only stock dsh-owned `read`, `grep` and `glob`: the verified gate plugin turns stock tool allows into asks, the cached tool name maps to the read-only policy identity only on a READ_ONLY turn, and unknown, shadowed, MCP, pathless, relative-path and command tools remain refused (`deepseek_read_only_owner_issue`, `test_side_readonly_deepseek.py`). `ACP_BACKENDS_MARKDOWN_AGENT_SPECS` (read through the harness seam `reads_markdown_agent_specs`) says which hosts load the markdown agent form Crew's roster lists for every backend; a non-member is never refused before its spawn -- the existing activation guard is where a markdown-only agent already fails, and membership only decides whether that refusal explains the markdown file or the missing JSON one. The same membership reaches the agent pickers as data, never as a gate: `GET /api/agents/catalog` sends `markdown_spec_backends` (the set, config spelling), `markdown_refused_backends` (the `Routing.AGENT_SPEC` hosts outside that set, which are the hosts whose activation guard refuses; a test pins this to the harnesses' `verifies_agent_activation` and `reads_markdown_agent_specs` answers) beside the asking chat's `session_backend` and a per-row `markdown_only`. A picker badges such a row "KAS only" only on a refusing host, and leaves it selectable.| `test_harness_parity.py::test_session_sharing_is_opt_in`, `::test_steer_is_opt_in`, `::test_model_switch_channel_is_opt_in`, `::test_effort_channel_is_opt_in`, `::test_only_overlay_readers_are_written_to`, `::test_mcp_config_hot_reload_is_opt_in`, `::test_mcp_wire_reinject_is_opt_in`, `test_kas_mcp_live_reinject.py`, `test_acp_pod_home_remap.py::TestTheCapabilitySetIsItsOwnDecision`, `test_acp_codex_harness.py::TestTheEffortChannelIsReadFromItsOwnTable`, `::TestWhatTheHandleAdvertisesForCodex`, `test_acp_kas_effort.py`, `test_codex_steering_request.py`, `test_claude_steering_request.py`, `test_goose_steer_request.py`, `test_opencode_concurrent_prompt_steer.py`, `test_harness_parity.py::test_steer_advertisement_matches_the_steer_table`, `::test_agent_activation_by_mode_matches_the_routing_table`, `::test_the_model_select_fold_matches_the_advertised_selection_table`, `::test_the_session_mcp_array_is_mirror_built_for_exactly_the_mirrored_hosts`, `::test_every_runtime_path_identity_test_is_declared`, `::test_the_identity_test_declarations_are_all_still_live` | `providers/acp.py` (`AcpProvider.is_session_sharing_eligible`), `providers/acp_model_effort.py` (`change_effort`, `clear_effort`, `_apply_initial_effort`), `providers/acp_cli_overlay.py` (`_apply_effort_overlay`, `_apply_tool_search_overlay`), `providers/acp_turns.py` (`stream_command`), `acp/client_class.py` (`AcpClient.supports_steer`, `steer_needs_loss_recovery`), `acp/client_turn_control.py` (`_steer_via_steering_request`, `_steer_via_request`, `_steer_via_concurrent_prompt`, `_settle_concurrent_steers`), `acp/client_spawn_env.py` (`_apply_pod_home_remap`), `acp/session_handle_class.py` (`AcpSessionHandle.supports_steer`, `supports_refusal_steer`), `acp/session_handle_steering.py` (`_steer_via_steering_request`), `acp/session_handle_model_selection.py` (`store_session_config`, `models_from_config_options`), `acp_backends.py`, `mcp_hot_reload.py` (`mcp_hot_reload_supported`, `live_sessions_apply_mcp_live`), `acp/session_provider_mcp_reinject.py` |
| H7 | `is_kiro_cli` is a positive Kiro test at every call site. It drives internal-sandbox delegation: macOS skips Kiro Crew's seatbelt because Kiro's sandbox cannot nest inside it, and Windows permits the official Kiro backend to run despite having no Kiro Crew OS wrapper. Passed for a harness with no internal sandbox, it hands isolation to a layer that never starts; this is the only Group B row that is also a security invariant. **Windows requires `is_kiro_cli is True` exactly** — `None` and `_spawns_kiro_cli` basename inference can never grant the backend-less-host exception. On macOS a site may grant membership explicitly or pass `None` to defer to the positive basename test. The verdict is `spawn_delegates_to_internal_sandbox`: `ACP_BACKENDS_INTERNAL_SANDBOX` on every route, or `ACP_BACKENDS_RELAY_INTERNAL_SANDBOX` when the spawn plan states `kiro_cli_internal_sandbox` -- KAS's relay route is the kiro-cli process and starts kiro-cli's seatbelt on macOS with its switch on, while its bundle route runs Node and keeps Crew's layer. The KAS relay never claims it on Windows: measured there (kiro-cli 2.35.1), its KAS child runs unconfined with the switch on or off, so Windows keeps the no-backend path for KAS. The runtime driver decides it from the plan, after the plan is resolved, and every reader (the wrap flag, the verified workspace, the voice-runtime guard) takes that one answer. | `test_harness_parity.py::test_is_kiro_cli_is_positive`, `::test_kas_bundle_route_keeps_crew_sandbox`, `test_sandbox_argv.py::TestKiroInternalSandboxExclusion` | `acp/launch.py` (`launch`, the one tail both drivers take, for every known host), `acp/runtime_spawn_launch.py` (`_spawn_admitted`), `acp/client_spawn_env.py` (`apply_pod_bundle_spawn`), `agent_sdk/backends.py` (`spawn_delegates_to_internal_sandbox`), `sandbox_wrap.py` (`wrap_argv`), `sandbox_delegation.py` (`_spawns_kiro_cli`) |
| H8 | New harness identifiers live in `agent_sdk/backends.py` — a LEAF module behind the agent-SDK boundary, so every consumer can name the constants rather than copy them — and are added to `ACP_BACKENDS_KNOWN`; every capability set is defined there or in one of the leaf `backends_<role>.py` owners that `VOCABULARY_PATHS` lists and the module re-exports, and is a subset of it; and `AcpProvider.__init__` rejects anything outside it. `ACP_BACKEND_KIRO` is the empty string, so a value that falls through every identity check spawns `kiro-cli` under a foreign label. `acp/types.py` and the `acp_backends` shim both re-export the vocabulary and remain import sites for existing callers. | `test_harness_parity.py::test_capability_sets_are_subsets_of_known_backends`, `::test_unknown_backend_rejected_at_construction`, `::test_codex_is_selectable_and_answerable` | `agent_sdk/backends.py` (`ACP_BACKENDS_KNOWN`), `providers/acp.py` (`AcpProvider.__init__`), `scripts/check_harness_parity.py` (`VOCABULARY_PATHS`) |

`ACP_BACKENDS_MEMBER_CAPABILITIES` is the H6 opt-in for loading an enrolled
member's saved agent spec. Kiro and KAS belong natively (kiro-cli reads the
saved file at spawn; KAS receives it whole as `_meta.kiro.customAgents` and
activates it by `session/set_mode`). Claude, Codex and DeepSeek confirm the
exact spec consumed by their MCP projection after session creation or load
(Claude's projection must also be governed). pi confirms the same way with no
mirror: the consumed spec is the parse its member mounts and zero-tool answer
come from (`AcpClient._mirrorless_member_mounts`), and since its projection is
`BROKER_ONLY` a declared server that is not pooled never reaches pi and leaves
the session unverified rather than applied. On a member session pi's pooled stubs
pass the spec's `tools` allowlist, as a mirror's do. strands is outside every
member-facing set (`ACP_BACKENDS_MEMBER_CAPABILITIES`, `ACP_BACKENDS_MEMBER_DISPATCH`,
`ACP_BACKENDS_MEMBER_PANEL`, `ACP_BACKENDS_MATE_WORKER_KIT`): a member or worker
session's Crew servers (`kirocrew-core`, the dashboard and panel mounts) are stdio
children inside its sandbox, and the enforced credential mask hides the gateway
credential from them, so their gateway-backed calls would be refused. Its mirror
already confirms a saved spec (`strands/saved-spec-projection-live.jsonl`, recorded
with no mask); it joins once a host-side control-plane transport exists.
`ACP_BACKENDS_MEMBER_TOOL_LIMITS`
(Kiro only) says which honour a saved spec's per-tool `disabledTools`; on any
other backend such a spec is refused before spawn with
`capability_tool_limits_unsupported`. A member DM whose
`agent.member_acp_backend` is `kiro` runs on KAS (the loader reads it so), so
no member-DM backend setting honours per-tool limits and that refusal's remedy
is to change the saved spec. A crewmate's WORKER runs a spec built per session
from the worker base plus that crewmate's file (`mate_worker.worker_session_spec`,
the one harness-neutral seam), carried the way a member chat's saved spec is.
`ACP_BACKENDS_MATE_WORKER_KIT` (KAS, claude) names the hosts that
carry it: KAS's projection reads the seam; the mirrored hosts read it through the
session MCP projection, keyed by the session (`acp.session_mcp._crewmate_worker_spec`).
kiro-cli is a member backend but not in the set: it reads `--agent` off disk from
`~/.kiro/agents/`, where the worker spec is not, and has no per-session channel. A
worker takes its crewmate's backend (`members.select_provider_backend` routes a
key `mate_worker.note_worker_session` published, identified positively by its
record: a `template` selection on `kirocrew-mate-worker` with a member id), and it
is refused with the member chat's codes: outside `ACP_BACKENDS_MATE_WORKER_KIT`
(`capability_harness_unsupported`, at `session_create` and before spawn through
`mate_worker_kit_supported`), and, when the crewmate's file turns off
single tools (an entry's `disabledTools`, or a server or native tool its own
`excludedTools` names, as a copied kit keeps them;
`mate_worker.crewmate_tool_limited_servers`),
outside `ACP_BACKENDS_MATE_WORKER_TOOL_LIMITS`
(`capability_tool_limits_unsupported`, before spawn). That set is the worker's
own because the worker's limits travel differently from a member chat's: the
worker spec rewrites every switched-off tool into `excludedTools`, which KAS
projects, so KAS (the default backend) honours them and is in the set; claude
mounts the worker's servers whole and reports `excludedTools` as a gap, so it
is not. codex and deepseek build their array through the same projection but
are outside `ACP_BACKENDS_MATE_WORKER_KIT` until a live capture of a worker
session lands for each, so a worker there is refused. Both providers answer `mate_worker_kit_supported` and
`mate_worker_tool_limits_supported` from the two sets. The worker's prompt (its
principles, then its crewmate's) rides the `[AGENT SYSTEM PROMPT]` block only on
`ACP_BACKENDS_MATE_WORKER_PROMPT_IN_CONTEXT` (claude), the kit
hosts whose mirror rules `Concern.PROMPT` `WITHHELD`; KAS's wire spec carries it.
Which channel a worker session uses is its live provider's answer
(`mate_worker_prompt_in_context`), recorded at allocation, so a later change to
`agent.member_acp_backend` does not move a running worker's prompt. The
single-tool refusal is decided again inside the projection, on the same read of
the crewmate's file the worker spec is built from (`resolve_worker_spec`
`refuse_tool_limits`); the allocation's earlier check is a fast path. What reaches the worker
past that is what the member path carries on that host: the mirrored hosts
report `excludedTools`, hooks, `toolsSettings`, native-tool restrictions and
auto-approval shortcuts as projection gaps
(`agent_capabilities_consumed.projection_gaps`). On KAS a worker spec with no
`prompt` goes out as `"prompt": ""`, which KAS runs on its own default prompt, as
kiro-cli does; Crew's background-worker brief is `kirocrew-lite`'s alone (#25320,
[acp-client](acp-client.md) § v2 parity of the projected entry). Pinned by
`test_harness_parity.py::test_the_mate_worker_takes_the_member_rule` and
`test_mate_worker_harnesses.py`.
This is separate from session sharing and per-session member dispatch. Both `AcpProvider` and
`AcpSessionProvider` answer `member_capabilities_supported` from this set.
Support alone does not prove a template is loaded: dedicated runtime ownership,
liveness, native activation or projection consumption, saved-version checks and
MCP readiness still apply. The projecting backends report unsupported native tool restrictions,
per-tool mounts, hooks, toolsSettings, excludedTools and auto-approval shortcuts as projection gaps; those
sessions start but remain unverified rather than claiming full-spec parity. Pinned by `test_harness_parity.py::test_member_capabilities_are_opt_in`
and `test_session_capabilities.py::test_real_session_provider_member_support_is_explicit`.

`ACP_BACKENDS_NATIVE_TODOS` is the H6 opt-in for the harness-native `/todos`
command. Only Claude belongs today. Before acquisition, the dashboard asks a
positively live provider's `SessionCapabilities.supports_native_todos` field;
otherwise it asks `capabilities_for(select_provider_backend(session_key,
member_acp_backend, acp_backend))` for every provider axis. The acquired provider's
capability is the final confirmation, so an unknown backend and a newly registered
backend both fail closed until their adapter explicitly joins the table.
The public schema admits only `agent.provider = "acp"` (H2); a dormant
`"claude_code"` provider label does not select the Claude harness or grant native
`/todos`. The public factory selects through `select_provider_backend` even if
a caller injects that unsupported label. An edition adding a provider seam must
define its allocation and capability authority rather than inherit a label-based
exemption.
An unsupported pre-acquisition answer refuses without cold-starting or waiting
for a provider. Refusal preserves
live Slack OPTIONS and the context owed by a first claim, including speculative
resume, while completing that claim's session-start bookkeeping. The sibling
`/compact` gate may refuse before acquisition; when no positively alive provider
exists, it uses the factory's member-aware `select_provider_backend` authority.
`GET /api/slash-commands` has no session key, so it lists `/todos` from the
default `agent.acp_backend` capability only; a member session routed to a
different harness is still judged by the gate above when the command is sent.
The same default decides the rest of the list on a harness in
`ACP_BACKENDS_LOCAL_HARNESS_COMMANDS` (KAS, Strands): the list keeps only the
commands the dashboard serves there (`/agent`, `/clear`, `/compact`, `/goal`,
`/prompts`, `/side`, `/workflow`, plus `/mcp`, `/model` and `/tools` on KAS alone,
`served_slash_commands`), because the slash gate reads the same set and answers
every other harness command with a notice naming the harness. `/model`,
`/tools` and `/mcp` are answered by `dashboard/kas_local_commands.py` from state
the dashboard already holds, never by the model and without a turn: `/model`
shows the slot's pin and the session's advertised list (else the recorded KAS
catalog), and `/model <id>` runs the picker's own transaction
(`slot_model.switch_slot_model`, as the slot's own turn, so a busy session defers
the pick exactly as the picker does); `/tools` reads the agent entry the session
registered with KAS (`kas_registered_agents`: `tools`, `excludedTools`, the
`allow` rules still in force, the built-ins Crew answers); `/mcp` reads the slot's
MCP session report. Only names are shown: a server's `env`, `headers`, `command`
and `args` are never read, and the reply is redacted.

A **route** is not a harness, and is not expressed as one. KAS runs either
through kiro-cli's relay or from Crew's own engine bundle (`agent.kas_bundle`),
and the dashboard model picker answers differently on the two: from
`kiro-cli chat --list-models` on the relay, from the engine's recorded `model`
select on the bundle (`dashboard/handlers/models_kas_bundle.py`,
[model-selection.md](../common/model-selection.md)). That is a decision over two
facts -- `acp_backend == ACP_BACKEND_KAS` AND the switch -- so it is the positive
two-fact predicate `agent_sdk.kas_bundle.kas_bundle_route()`, placed BEFORE the kiro-cli path in
`api_models` and never a capability set: a set keyed on the backend alone would
put the relay route (kiro-cli present, catalog printable) and the bundle route
(no kiro-cli) in one bucket, and widening the kiro-cli path to read the select
would change the Kiro path's own machinery (Group C). What is per HARNESS stays in
a set: the fact that KAS's `model` select is its catalog is recorded on positive
KAS identity in `agent_sdk/kas_model_catalog.py`, whichever route the session runs
on; only the bundle route's picker reads the recording.

### Where the vocabulary is defined

The vocabulary H8 names is one import path, `kiro_crew.agent_sdk.backends`, over a
family of files. The module defines the identifiers and re-exports every other name
from the owner for its question, and `VOCABULARY_PATHS` in
`scripts/check_harness_parity.py` lists exactly these files as the places a
definition may live:

| File | Answers |
|---|---|
| `agent_sdk/backends.py` | Which ACP backends this build can serve: the identifiers, `ACP_BACKENDS_KNOWN`, the first-run set, the selectable registry, and the disposition table for every set |
| `agent_sdk/backends_launch.py` | How Kiro Crew launches a harness's process, what confines it, and how it is recognised later |
| `agent_sdk/backends_mcp.py` | Where a harness gets its MCP servers and agent specs, and how its tool surface is shaped |
| `agent_sdk/backends_members.py` | What an enrolled crew member's session may load and mount on each harness |
| `agent_sdk/backends_models.py` | How a harness switches model and reasoning effort, and which registry namespace its model ids live in |
| `agent_sdk/backends_routing.py` | How a harness is made to ask before a tool runs, and which of its requests Kiro Crew answers |
| `agent_sdk/backends_sessions.py` | Which process serves a harness's sessions, and whether a session is shared, evicted, restored or forked |
| `agent_sdk/backends_turns.py` | What a harness does inside a running turn: take a steer, compact, run a slash command, and report how the turn ended |

Where new work goes:

- A new harness id (`ACP_BACKEND_<NAME>`) goes in `agent_sdk/backends.py`, with its
  `ACP_BACKENDS_KNOWN` entry.
- A new capability set goes in the owner whose row names its question, plus its
  re-export at the foot of `agent_sdk/backends.py` and its row in that module's
  disposition table.
- A new owner is also listed in `VOCABULARY_PATHS` and imported at the foot of
  `agent_sdk/backends.py`; `test_harness_parity.py` pins the two lists equal.

### pi: what its own channels can and cannot carry

pi-acp drops the `session/new` MCP array, so pi joins
`ACP_BACKENDS_SESSION_MCP_ARRAY` on a different carrier: the launch writes the
array's composition to an owner-only handoff file bound to the session nonce and
sealed with a key only that spawn's environment carries, and
Crew's sealed gate extension registers each entry with pi's built-in MCP support
(`pi.registerMcpServer`, from `PI_MIN_VERSION`). A same-named server in pi's own
`mcp.json`, user-level or a trusted project's, outranks that registration, so the
extension refuses its tools (`acp/harness/pi.py`, `test_pi_gate_extension_mcp.py`).

pi takes a mid-turn steer without the adapter. pi-acp 0.0.34 serves no steer verb,
and a `session/prompt` sent during a turn is queued and runs as the next turn; pi's
own RPC can steer, but pi-acp owns pi's stdin. So `AcpClient` writes the steer as a
file for the gate extension already running inside pi (`acp/harness/pi_steer.py`,
`acp/pi_steer_turn.py`): one file per steer in the owner-only pi gate directory the
sandbox mounts read-only for pi children, sealed AES-256-GCM under a per-launch key
carried only in that launch's environment, bound to the gate nonce, the pi session id
and a strictly increasing sequence number. At each `turn_end` that did not abort or
fail the extension puts the oldest undelivered steer on pi's steering queue with
`pi.sendMessage(message, {deliverAs: "steer"})` (synchronous while pi streams, and
handed to the model as a user message), read before the next model call of the same
run, and notifies an envelope when pi starts it; that
echo (an `agent_message_chunk` carrying `_meta.piAcp.notify`) is the only thing
reported consumed. The client writes only while its prompt turn is open and removes
every steer file at the turn's terminal under the same lock; the extension retires
every steer still on disk when a run settles. A steer no turn read is the composer's
to queue, so pi joins `ACP_BACKENDS_STEERING_REQUEST` and `steer_needs_loss_recovery`
like goose (`steering-live.jsonl`, `test_pi_steer.py`,
`test_pi_gate_extension_steer.py`).

One card line stays unavailable on pi, on a driven observation:

- **Side Chat tools.** The READ_ONLY gate would have to trust the tool NAME in the
  gate envelope, and on pi a name proves nothing: an extension that registers a tool
  called `read` replaces the built-in one (driven: the gate saw `read` and the call
  wrote a file), and user-level extensions, installed pi packages and a trusted
  project's extensions all load beside Crew's. The known route is a side-chat pi
  process launched with `-ne` (no discovered, configured or built-in extensions;
  explicit `--extension` paths still load) and `--no-approve`, so only Crew's sealed
  gate runs and the built-in names can be trusted, plus a pi tool table for the
  READ_ONLY proof and a refusal of sensitive paths that pi's own settings cannot
  pre-approve. That is a change to how side sessions launch, not a membership edit,
  and it has not been built.

### Heartbeat tools on a harness with no adapter-authored tool identity

The heartbeat agent has no approver behind it, so `_heartbeat_approval`
(`slack/gateway_runtime/turn_approvals.py`) approves a call only on the identity the adapter wrote under
`_meta` — `AcpEvent.mcp_identity_trusted`, `tool_name`, `mcp_server_name` — and
never on the title the model wrote (`slack/gateway_runtime/tool_policy.py`,
`_is_heartbeat_safe_tool`). kiro-cli stamps `_meta.kiro.toolName` on every tool
call, goose writes `_meta.goose.toolCall`, codex-acp marks MCP calls with
`_meta.is_mcp_tool_call`. claude-agent-acp stamps `_meta.claudeCode.toolName` on its
built-ins, but Crew reads that key only on a Side Chat session the harness itself
launched read-only (`claude-code-provider.md`, "A Side Chat session"); it is not a row
of `_MCP_IDENTITY_META_CHANNELS`, so a heartbeat event on claude-agent-acp carries no
trusted identity. opencode sends `kind="other"` and no identity (`acp/_dispatch_parts/tool_call_identity.py`,
`classify_tool_call`). On those two, every heartbeat tool call is denied and audited
`reason=no_trusted_identity`, with one gateway warning per denied call naming the
backend condition, and the heartbeat cycle runs as a text-only poller. The signal is
per call on purpose: each row is derived from the event itself, so it cannot drift
from what the gate decided, and the positive set a start-time warning would key on
(`ACP_BACKENDS_META_IDENTITY`) deliberately omits kiro-cli -- it lists harnesses whose
recorded corpus covers every tool class -- so a start-time warning keyed on it would
fire on the default harness. This is the
same line as pi's Side Chat above: a name proves nothing on a harness whose channels
cannot carry one, and heartbeat is the one session with nobody to catch a forged
name. Heartbeat tool use on such a harness returns when its adapter's channel joins
the identity table on a recorded corpus, not through a title or `kind` fallback.

## Group C: the Kiro path keeps its own machinery

An adapter that lands by *generalizing* a Kiro-specific step to a
lowest-common-denominator one has degraded the Kiro session even when every
test still passes.

| Id | Guarantees | Pinned by | Constrains |
|---|---|---|---|
| H9 | `kiro-cli` remains the default branch of spawn-argv resolution, keeping its pre-spawn agent materialization (`kiro-cli` discovers selectable modes from `~/.kiro/agents/*.json` at startup, so a later `set_mode` fails with "Mode not found" without it) and its `--model` pin (the only way to run a model outside the agent's provider). A refactor that treats Kiro as one entry among N drops both. The Kiro spawn lives in its own harness on the runtime and in the client's own explicit arm of `AcpClient._spawn`, so no other host shares the code that carries them. | `test_harness_parity.py::test_kiro_spawn_argv_keeps_its_own_branch`, `::test_codex_resolves_its_own_adapter_and_declares_its_own_handshake`, `test_acp_harness_contract.py::test_kiro_spawn_argv_names_the_agent_and_the_model` | `acp/harness/kiro.py` (`KiroHarness.resolve_spawn`) |
| H10 | Protocol version and client capabilities stay per-harness literals. Collapsing them to one handshake that every harness accepts silently downgrades the Kiro session's declared capabilities. The hosts also disagree on the protocol version's TYPE, so one shared handshake would break a host outright. | `test_harness_parity.py::test_handshake_is_per_backend`, `test_acp_harness_contract.py::test_protocol_versions_differ_in_type_not_just_value` | `acp/harness/kiro.py`, `acp/harness/kas.py` (`client_capabilities`, `protocol_version`), `acp/types.py` (`ACP_CLIENT_CAPABILITIES`, `KAS_CLIENT_CAPABILITIES`) |
| H11 | The provider label is a closed mapping and an absent label means Kiro. It indexes resume compatibility, session-map persistence, and session-file cleanup routing, so a harness with no `PROVIDER_LABEL_*` of its own persists as a Kiro session and its transcript is pruned for want of a Kiro session file. | `test_harness_parity.py::test_every_known_backend_has_a_label`, `::test_codex_carries_its_own_provider_label` | `acp/types.py` (`PROVIDER_LABEL_*`), `providers/acp.py` (`provider_label`), `providers/acp_lifecycle.py` (`cleanup_session`), `session.py` (`detect_provider_switch`) |
| H12 | Model pre-flight keeps "empty or unknown advertised set means allow", and never compares ids across harness namespaces. Harnesses advertise ids in their own spelling; one shared membership test across two namespaces calls every legitimate model unusable and withholds the model. | `test_harness_parity.py::test_model_preflight_allows_unknown_advertised_set` | `acp/runtime_models.py` (`model_is_unusable`, `advertised_model_ids`; re-exported by `acp/client.py`) |

## Group D: review-only invariants

Deterministically un-pinnable — they are properties of a change, not of a tree,
and the absence of a mechanism is not something a source scan can see. The
`harness-parity` rule in `AUTOSDE.yaml` carries them to every AI review lane.

| Id | Guarantees | Pinned by | Constrains |
|---|---|---|---|
| H13 | Harness support is additive at the `ProviderRegistry` seam: a v1 addition, no `CONTRACT_VERSION` bump. The Kiro construction path gains no conditional, no new required argument, and no new failure mode in service of an adapter. The shared-process runtime asks its host's harness per request rather than testing an identity, and Kiro's lookup into that table is TOTAL -- the id is a key of a literal beside the class it names, and the class takes no constructor argument -- so no ordinary session gains a way to fail before its process exists. On the per-session client every other host resolves its launch in its own `ProcessAdapter` (`acp/harness/<host>.py`, looked up by `process_adapter_for`), whose table has no entry for kiro-cli, so the client's Kiro arm stays explicit and reaches no adapter step; both arms then take the one launch tail (`acp/launch.py`), which adds no await, no branch on the host and no failure mode to the Kiro path. | review-only (`AUTOSDE.yaml` → `harness-parity`), plus `test_acp_harness_contract.py::test_the_kiro_lookup_is_total`, `::test_the_runtime_resolves_the_kiro_harness_without_spawning`, `::test_the_kiro_client_spawn_never_consults_a_process_adapter`, `::test_the_client_keeps_its_own_arm_for_every_runtime_served_host` | `platform/interfaces.py` (`ProviderRegistry`), `config/loader.py` (`create_provider_factory`), `acp/harness/__init__.py` (`harness_for`, `process_adapter_for`), `acp/client_start.py` (`AcpClient._spawn`), `acp/launch.py` (`launch`) |
| H14 | A capability the session layer reads off a provider is declared on `LLMProvider` with a safe default. An adapter never forces a `hasattr` / `getattr` probe onto the Kiro path, and never leaves a Kiro-only attribute reachable through the ABC where a missing one reads as `False`. | review-only (`AUTOSDE.yaml` → `harness-parity`) | `providers/base.py` (`LLMProvider`), `providers/acp.py` (`AcpProvider`) |
| H15 | Interactive stdin ownership never depends on a harness's native shell tool. No backend (kiro-cli `execute_bash`, the Claude harness Bash tool) hands Kiro Crew the child's stdin or a PTY, so a `waiting_input` shown for such a child is a REPORT, never a takeover: the pre-dispatch classifier (`liveness.classify_interactive_command`, [acp-client.md](acp-client.md) § Interactive-command policy) yields the lane slot and names the prompt, and `agent.interactive_command_policy` decides between a non-lethal cancel and a declared wait -- identically on every backend, because it keys on the trusted `AcpEvent.shell_command` rather than harness machinery. The one path where Kiro Crew would OWN the child (a `kirocrew-core` terminal tool holding stdin and a PTY) is a separable capability that does not exist today; a harness that later exposes a stdin-capable shell tool is integrated by an adapter beside it, never by reaching into the harness process. The declared gap per backend is the "blocked on stdin" row of the RFC's platform-evidence table. | review-only (`AUTOSDE.yaml` → `harness-parity`), plus `test_interactive_command_detection.py` (classifier and policy are backend-independent) | `acp/liveness_interactive.py` (classifier), `acp/liveness.py` (evidence table), `acp/session_handle_watchdog.py` (`interactive_command_policy`) |
| H16 | A harness-native subtask (kiro-cli `use_subagent`, KAS `agentSubtaskId` / pipeline stage, the Claude adapter's in-harness Task tool) is COUNTED at its parent session and scheduled NOWHERE ELSE. The parent `AcpSessionHandle.native_child_sessions` (on the per-session `AcpClient`, its own bounded child set) is the only place a native child has a number; it is never charged a `HostBudget` slot (reported under `snapshot()["uncharged"]["native_children"]` via `report_native_children`), never given a lane slot or a taskq row, and the recovery boundary is the parent session — one `session/cancel` on the parent covers every child, a child-routed `kirocrew/status` is rejected (`child_origin`), and a `spawn_continue`-style resume of a child id is refused with `native_child_not_resumable` naming the parent. Per-backend identity: kiro-cli yes (child-routed frames under both `session/update` spellings + the roster it owns), KAS yes (`_meta.kiro.agentSubtaskId`), **Claude yes once declared** — `AcpClient` declares `clientCapabilities.subagents` on the claude backend (`client_native_children.CLIENT_CAPABILITIES_BY_BACKEND`), so claude-agent-acp announces each child on the parent (`subagent_update`) and sends the child's own frames under the child's sessionId; the client turns them into the native cards and keeps the child ids under the same `NATIVE_CHILD_ROSTER_CAP` and overflow count; a child past the cap gets no card, its text is dropped and its tool calls render as ordinary rows. That set is not yet reported as uncharged residency, and a resume of a Claude child id misses as an unknown conversation rather than being refused with `native_child_not_resumable`. A session that declares nothing gets the Task call as an ordinary parent tool call with no child identity, and the liveness oracle bounds the Claude parent turn exactly as it bounds a kiro-cli parent. A Kiro Crew `spawn_run` child is a task row with its own handle, so native grandchildren under it are counted on THAT handle only (the Kiro Crew row is the boundary). On a shared runtime with several registered sessions an unannounced child frame names no owner and is dropped (counted), so the number is "observed", never "scheduled". A harness that later exposes per-child cancel/resume is integrated by adding a row adapter, never by teaching the scheduler to pause one child. | `test_native_subagent_boundary.py` (every test; the §10 scenario map is in its module docstring), plus `test_acp_structured_status.py::test_child_routed_frame_status_is_rejected_and_not_reattributed` and `test_claude_native_subagent_cards.py` (the Claude declaration, the child form on `AcpClient`, the cap) | `acp/session_handle_class.py` (`native_child_sessions`), `acp/session_handle_native_children.py` (`_note_native_child`, `native_child_resume_refusal`, `report_native_children`), `mcp_gateway/host_budget.py` (`report_uncharged`, `uncharged`), `acp/runtime_reader_loop.py` (child-frame routing), `acp/client_native_children.py` (the Claude child form on `AcpClient`) |

### The user's own harness permission settings

How a user's own harness permission settings reach a main or member chat: [claude-code-provider.md](claude-code-provider.md) ("Known gap: the user's global `~/.claude` is inherited") for claude, and [side.md](side.md) for the Side Chat posture on claude and goose.

### KAS: a spec's `allowedTools` built-ins run without a prompt

On kiro-cli a built-in the spec lists in `allowedTools` (`fs_read`, `shell`, `grep`,
`glob`, `code`, `fs_write`, ...) runs without a prompt. KAS reaches the same user
outcome by a different path, because a KAS allow rule would run the call with no
permission request and so skip Crew's gate. The projection keeps those built-ins at
`ask` (`kas_permissions.WITHHELD_FROM_AUTO_APPROVE`) and records them on the entry
(`CREW_ANSWERED_BUILTINS_KEY`); every call raises `session/request_permission`, and
the gate's `spec-builtin-grant` row answers it after the deny floor, the
sensitive-path check and governance ([security.md](security.md) § KAS spec built-in
grant). A call one of those checks refuses is refused on both engines. The Kiro
path is unchanged: nothing on it sets `AcpEvent.spec_granted_builtins`.

### KAS managed MCP readiness

KAS's harness opts into managed MCP readiness with its session-scoped status and
tool-tag notifications. The runtime asks that declaration before replacing the
best-effort drain with the bounded barrier described in
[acp-client.md](acp-client.md#kas-managed-mcp-readiness). Kiro retains its exact
legacy notification declaration and drain; KAS readiness neither changes its
timeout policy nor introduces a new Kiro startup failure. The notification
contract and outbound-wire parity tests pin both sides.

### KAS skill scope

A KAS session loads the skill set its kiro-cli twin loads under Crew. kiro-cli
gets there through the native skill view and the workspace `cli.json` overlay;
KAS reads neither and loads the default skill trees for every session, so its
harness adds `skill` deny rules to the projected agent's `permissions` instead
([memory-skills-hooks.md](memory-skills-hooks.md), KAS skill scope). The Kiro
path is unchanged. `test/test_kas_skill_scope.py` compares the skills KAS would
offer for the built payload with the kiro-cli scope and goes red when the rule
is not sent.

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
   default" is not a decision. `BASELINE_SELECTABLE_BACKENDS` is otherwise
   `ACP_BACKENDS_KNOWN`, so leaving a known id out of the baseline is a
   NARROWING that `test_baseline_ships_every_known_backend` fails on **unless**
   the id is named in that test's `NOT_SHIPPED_SELECTABLE` allowlist together
   with the reason it cannot be offered yet: the id becomes spellable but
   unreachable, and that state needs a stated reason rather than a default.
   The allowlist is empty today. The full sequence a new
   harness walks, and which stage decides whether it lands dormant or
   selectable, is [harness-onboarding.md](harness-onboarding.md).

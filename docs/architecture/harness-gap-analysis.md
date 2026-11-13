# Harness gap analysis

Where each harness stands against the [harness requirements checklist](harness-requirements.md), and which fixes close the most gaps for the least work. Use it to pick the next harness fix, or to see what is still unmeasured before you call a harness done.

This is a snapshot, read at `origin/main` `ae9132cd86a3` (2026-11-13). No harness was installed or run for it: every cell comes from reading code, capability sets, capability cards and tests on that commit. References name files and symbols; search for the symbol if the code has moved. Paths with no top folder are under `src/kiro_crew/`. Some security items are tracked privately; their rows show 🔒 and are left out of the counts.

## How cells were decided

- ✅ needs a capability set, a card line, a test or a code read on that commit. Shared code with no harness branch counts as ✅ for every harness it serves, and the evidence names it.
- ❌ names the file or symbol that shows the gap.
- ❓ means nobody measured it. It is a gap in knowledge, not a failure, and it is never counted as met.
- ➖ means the rule cannot apply, for example kiro-cli settings on a harness that never reads them.
- Gap size is a judgement. **S** is one file, under about 50 lines, no new capture. **M** needs a new live capture or several files. **L** is a new design across modules.

KAS scores high partly by construction: most rows were taught by KAS fixes. The ❓ cells are the real risk on the other harnesses.

## Ranked fix list

Rank weighs harnesses fixed and user impact against size; ties go to the fix that needs no install. "Reuse" means KAS or another harness already has the fix and the change is mostly wiring.

| # | Req | Harnesses (❌) | Size | Reuse or new | Install to verify | Owner | Why this rank |
|---|---|---|---|---|---|---|---|
| 1 | R-SPEC-7 | claude, codex, opencode, goose, pi, copilot, deepseek | S | reuse `acp/kas_prompt.py` in `context_assembly/agent_spec.py` | no | no open PR | a kiro-cli agent with `systemPrompt`, or `$HOME` in a `file://` prompt, loses its persona |
| 2 | R-LAU-9 | claude, opencode, goose, pi, copilot, deepseek | S | reuse: set `retrying_spawn_factory` in `acp/client_spawn_env.py` or classify at `acp/launch.py` | Windows host to confirm | no open PR | one change for six harnesses |
| 3 | R-LAU-12 | claude, codex, opencode, goose, pi, copilot, deepseek | S | reuse `rerun_signs_in_via_kiro_cli` (`dashboard/kiro_readiness.py`) at `dashboard/openai_compat.py` | no | no open PR | the OpenAI-compatible endpoint is refused on non-kiro hosts when kiro-cli is signed out |
| 4 | R-SPEC-10 | claude, codex, opencode, goose | S | reuse: replace `_on_kas(client)` at `dashboard/chat_turn/turn_dispatch_gates.py` with `CREW_FIRES_SPEC_OBSERVER_HOOKS` | no | no open PR | agentSpawn context is lost after a compaction |
| 5 | R-ERR-3 | claude, codex, opencode, goose, pi, copilot, deepseek | S code, plus a capture each | reuse the watcher at `undelivered_call_watch.py` with a pattern per harness | yes | no open PR | conductors miss lost reports from non-KAS workers |
| 6 | R-LAU-3 | kiro-cli, KAS (relay), claude, pi, deepseek | S | reuse `_TESTED_VERSION_RANGES` (`acp/client_turn_control.py`) | deepseek: yes (version source) | no open PR; #26459 (open) is related for claude | silent upgrades caused #25171 and #36125 |
| 7 | R-LAU-7 | opencode, pi | S | reuse the deepseek and codex tree kill | no | #35757 (open) | probe process leak |
| 8 | R-MOD-1, R-MOD-2 | copilot | M | config options | yes | #36118 (open) | no model or effort picker on copilot |
| 9 | R-STR-3 | copilot | M | | yes | #34088 (open) | copilot turns hang on a background shell |
| 10 | R-USE-1 | codex, pi, copilot, deepseek | M each | new: capture each usage frame | yes | no open PR | the Usage page is empty for them |
| 11 | R-SIDE-1 | codex, opencode, pi, copilot | M each | new captures | yes | pi: #21744 (open) | no Side Chat tools |
| 12 | R-MCP-8 | claude, codex, opencode, goose, copilot, deepseek | S | reuse the notice at `dashboard/chat_turn/turn_spec_hooks.py` for every harness outside `REMOTE_MCP_OAUTH` | no | no open PR | low value: a dropped `oauth` block is silent |
| 13 | R-MCP-2 | copilot | M | member dispatch mount | yes | no open PR | copilot cannot host member chats |
| 14 | R-STR-1 | copilot, deepseek | M | new captures | yes | no open PR | no mid-turn steer |
| 15 | R-LAU-5 | goose, deepseek | S | add `PINNED_FIXTURE_ADAPTERS` rows | lane pin | no open PR | indirect: fixture drift goes unseen |
| 16 | R-LIFE-5 | claude, codex, opencode, goose, pi, copilot, deepseek | M each | new reader per session store | yes | no open PR | low value; Send session carries the transcript only, by design today |

The largest ❓ clusters, where one install per harness clears many cells at once: R-LAU-8 (9 harnesses, #34147 open), R-MCP-10 (8), R-MCP-14 (8), R-ERR-5 (8), R-LAU-11 (7), R-SPEC-13 (7), R-ROW-1 (7), R-MCP-5 (7), R-MCP-7 (7).

## Per-harness summary

| Harness | ✅ met | ❌ not met | ❓ unknown | ➖ n/a | Met share of applicable (✅ / ✅+❌+❓) |
|---|---|---|---|---|---|
| kiro-cli | 59 | 1 | 6 | 7 | 59/66 (89%) |
| KAS | 71 | 1 | 1 | 0 | 71/73 (97%) |
| claude | 43 | 8 | 14 | 8 | 43/65 (66%) |
| codex | 43 | 8 | 13 | 9 | 43/64 (67%) |
| opencode | 32 | 9 | 21 | 11 | 32/62 (52%) |
| goose | 36 | 8 | 18 | 11 | 36/62 (58%) |
| pi | 30 | 9 | 21 | 13 | 30/60 (50%) |
| copilot | 28 | 13 | 18 | 14 | 28/59 (47%) |
| deepseek | 29 | 10 | 19 | 15 | 29/58 (50%) |
| all 9 | 371 | 67 | 131 | 88 | 73 rows × 9 = 657 cells |

Counts cover the 73 rows shown in the matrix. The 10 rows marked 🔒 are left out of the counts.

## Full matrix

Legend: ✅ met, ❌ not met, ❓ unknown (not measured), ➖ does not apply, 🔒 tracked privately.

| Req | kiro-cli | KAS | claude | codex | opencode | goose | pi | copilot | deepseek | Evidence |
|---|---|---|---|---|---|---|---|---|---|---|
| R-LAU-1 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | shared degrade path; `test_harness_parity.py::test_unselectable_backend_degrades_to_default` |
| R-LAU-2 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | `test_acp_launch_goldens.py` |
| R-LAU-3 | ❌ | ❌ | ❌ | ✅ | ✅ | ✅ | ❌ | ✅ | ❌ | range table `acp/client_turn_control.py` (goose 1.50.x, codex-acp 2.x); opencode major `acp/harness/opencode.py`; copilot exact pin `acp/client_copilot.py`, `agent_sdk/backends_launch.py` |
| R-LAU-4 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | `agent_sdk/backend_install.py` probe per harness |
| R-LAU-5 | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ❌ | `test/test_real_adapter_fixture_pins.py` pins codex, opencode, claude, pi, copilot; kiro/KAS floor tests in the same file |
| R-LAU-6 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | direct client `acp/client_start.py` (initialize OSError/AcpError with stderr); runtime `acp/runtime_liveness.py` (#30533); doctor (#27562) |
| R-LAU-7 | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ❌ | ✅ | ✅ | codex probe `acp/harness/codex.py` (`kill_popen_tree`); deepseek `acp/harness/deepseek.py` (`start_new_session`); session process group `acp/launch.py` |
| R-LAU-8 | ❓ | ❓ | ❓ | ❓ | ❓ | ❓ | ❓ | ❓ | ❓ | no refusal path on main |
| R-LAU-9 | ✅ | ✅ | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | runtime path `acp/runtime_spawn_launch.py` covers kiro, KAS, codex (`ACP_BACKENDS_ACP_RUNTIME`, `agent_sdk/backends_sessions.py`) |
| R-LAU-10 | ✅ | ✅ | ➖ | ➖ | ➖ | ➖ | ➖ | ➖ | ➖ | Crew installs only kiro-cli copies and the KAS engine (`acp/kas_engine_install.py`, pod `pod/runtime_home.py`); others show a command only (`dashboard/handlers/acp_backend_status.py`) |
| R-LAU-11 | ✅ | ✅ | ❓ | ❓ | ❓ | ❓ | ❓ | ❓ | ❓ | kiro-family fixes #23442, #31813, #25569, #29431; other launch paths not audited for loop blocking |
| R-LAU-12 | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | rerun gates fixed `dashboard/kiro_readiness.py` |
| R-AUTH-1 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | `agent_sdk/host_auth.py` one declaration each |
| R-AUTH-2 | ✅ | ✅ | ❓ | ❓ | ❓ | ✅ | ✅ | ✅ | ✅ | `signed_out_signature` `agent_sdk/host_auth.py` for goose, pi, deepseek and copilot; kiro/KAS `_KIRO_SIGNED_OUT`/`_KAS_SIGNED_OUT`; claude, codex, opencode declare no phrase |
| R-AUTH-3 | ✅ | ✅ | ✅ | ✅ | ❓ | ✅ | ✅ | ✅ | ✅ | `ACP_BACKENDS_CREW_OAUTH_BRIDGE` `agent_sdk/backends_mcp.py` (kas, claude, codex, goose, pi, copilot, deepseek); kiro native `REMOTE_MCP_OAUTH`; opencode in neither |
| R-AUTH-4 | ✅ | ✅ | ➖ | ➖ | ➖ | ➖ | ➖ | ➖ | ➖ | only kiro-cli login (cloud `cloud/login.py`) and KAS login are Crew-run |
| R-AUTH-5 | ✅ | ✅ | ➖ | ➖ | ➖ | ➖ | ➖ | ➖ | ➖ | Kiro account check only gates kiro-family turns (`dashboard/kiro_readiness.py`) |
| R-SBX-1 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | `scripts/check_harness_parity.py` sandbox-delegation rule; `ACP_BACKENDS_INTERNAL_SANDBOX` = {kiro} `agent_sdk/backends_launch.py`, `RELAY_INTERNAL_SANDBOX` = {kas} |
| R-SBX-2 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | card `security_notes` (live dump) |
| R-SBX-3 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | tracked privately |
| R-SBX-4 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | shared `scrub_agent_subprocess_env` used by every spawn and read-back (e.g. `acp/harness/opencode.py`, `deepseek.py`); open #33997 (Windows env case) |
| R-APP-1 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | tracked privately |
| R-APP-2 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | tracked privately |
| R-APP-3 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | tracked privately |
| R-APP-4 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | marked: `DIRECT_CLIENT_CHILD_CALLS_MARKED` `agent_sdk/backends_routing.py`, `SHARED_RUNTIME_CHILD_CALLS_MARKED`; others fail closed (no override card) |
| R-APP-5 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | tracked privately |
| R-SPEC-1 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | tracked privately |
| R-SPEC-2 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | Crew fires: `CREW_FIRES_SPEC_HOOKS` `agent_sdk/backends_routing.py`; others named in card `ineffective` (hooks / hooks_pre_tool_use) |
| R-SPEC-3 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | tracked privately |
| R-SPEC-4 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | tracked privately |
| R-SPEC-5 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | tracked privately |
| R-SPEC-6 | ✅ | ✅ | ✅ | ✅ | ❓ | ❓ | ❓ | ❓ | ❓ | `CREW_LOADS_SPEC_STEERING` = {claude, codex} `agent_sdk/backends_routing.py`; kiro/KAS native |
| R-SPEC-7 | ➖ | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | KAS reader `acp/kas_prompt.py` |
| R-SPEC-8 | ➖ | ✅ | ✅ | ➖ | ➖ | ➖ | ➖ | ➖ | ➖ | claude imports the fixed readers (`acp/spec_tools_allowlists.py`); other mirrors rule `TOOLS_SETTINGS` withheld/no-channel |
| R-SPEC-9 | ➖ | ✅ | ✅ | ✅ | ✅ | ✅ | ➖ | ➖ | ➖ | shared payload `agent_sdk/spec_hooks.py`; observer set `agent_sdk/backends_routing.py` |
| R-SPEC-10 | ➖ | ✅ | ❌ | ❌ | ❌ | ❌ | ➖ | ➖ | ➖ | KAS re-injection |
| R-SPEC-11 | ✅ | ✅ | ➖ | ➖ | ➖ | ➖ | ➖ | ➖ | ➖ | kiro-cli settings only exist on the kiro family |
| R-SPEC-12 | ✅ | ✅ | ➖ | ➖ | ➖ | ➖ | ➖ | ➖ | ➖ | mirrors own tool-name translation (R-SPEC-1) |
| R-SPEC-13 | ✅ | ✅ | ❓ | ❓ | ❓ | ❓ | ❓ | ❓ | ❓ | #25325 shared; KAS-only load messages #28423, #32090 |
| R-SKL-1 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | kiro native; KAS projection; others get the scoped skill dir `context.py`; BOM read `skills.py` |
| R-MCP-1 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | card `crew_tools` all Y |
| R-MCP-2 | ➖ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | card `member_thread_tools`; kiro-cli is not a member host by decision |
| R-MCP-3 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | 🔒 | tracked privately |
| R-MCP-4 | ✅ | ✅ | ❓ | ❓ | ❓ | ✅ | ❓ | ✅ | ✅ | `ACP_BACKENDS_PRUNE_UNSTARTABLE_MCP` `agent_sdk/backends_mcp.py`; kiro/KAS notice (#32527, #30643) |
| R-MCP-5 | ✅ | ✅ | ❓ | ❓ | ❓ | ❓ | ❓ | ❓ | ❓ | `MCP_CONFIG_HOT_RELOAD` {kiro} `backends_mcp.py`, `MCP_WIRE_REINJECT` {kas}; others not measured |
| R-MCP-6 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | card `mcp.per_tool_deny` / `costs_whole_server` declared for every harness |
| R-MCP-7 | ✅ | ✅ | ❓ | ❓ | ❓ | ❓ | ❓ | ❓ | ❓ | `acp/client_mcp_array.py` declares wait servers only on kiro-cli; KAS #30898 |
| R-MCP-8 | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ❓ | ❌ | ❌ | notice KAS-only |
| R-MCP-9 | ❓ | ✅ | ❓ | ❓ | ➖ | ➖ | ➖ | ➖ | ➖ | KAS #30969; claude/codex read user config layers |
| R-MCP-10 | ❓ | ✅ | ❓ | ❓ | ❓ | ❓ | ❓ | ❓ | ❓ | KAS order and budget (#26148, #35278, #32602); `acp/client_mcp_array.py` passes the array with no Crew-first order |
| R-MCP-11 | ✅ | ✅ | ➖ | ➖ | ➖ | ➖ | ➖ | ➖ | ➖ | admin MCP registry applies to kiro-cli's own registry filter only |
| R-MCP-12 | ✅ | ✅ | ➖ | ➖ | ➖ | ➖ | ➖ | ➖ | ➖ | `ACP_BACKENDS_TOOL_SEARCH_OVERLAY` = {kiro} `backends_mcp.py`; KAS via spec; no deferral elsewhere |
| R-MCP-13 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | `acp/client_mcp_array.py` drops disabled servers and tools for every direct client; KAS #36391 |
| R-MCP-14 | ❓ | ✅ | ❓ | ❓ | ❓ | ❓ | ❓ | ❓ | ❓ | gateway backend and `kas_http_bridge.py` fixed (#34979); servers a harness reads directly not measured |
| R-STR-1 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | card `mid_turn_steer` |
| R-STR-2 | ✅ | ✅ | ❓ | ✅ | ✅ | ❓ | ✅ | ➖ | ➖ | claude and goose steer captures still allowlisted `scripts/check_harness_parity.py` |
| R-STR-3 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❓ | ❌ | ❓ | per-fix tests |
| R-SIDE-1 | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ | ❌ | ❌ | ✅ | card `side_chat_tools`; `SIDE_READONLY` `agent_sdk/backends_routing.py` |
| R-MOD-1 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | card `model_switch` |
| R-MOD-2 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | card `reasoning_effort` |
| R-MOD-3 | ❓ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | KAS #30003; advertised-selection harnesses `ADVERTISED_MODEL_SELECTION` `backends_models.py` |
| R-SUB-1 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | offered only where `SESSION_SHARING` (`agent_sdk/backends_sessions.py`); others get dedicated sessions, so the rule holds |
| R-SUB-2 | ✅ | ✅ | ✅ | ✅ | ❓ | ❓ | ❓ | ❓ | ❓ | codex #23094, claude #30085, kiro/KAS native |
| R-SUB-3 | ➖ | ✅ | ✅ | ✅ | ➖ | ➖ | ➖ | ➖ | ➖ | `MATE_WORKER_KIT` = {kas, claude, codex} `agent_sdk/backends_members.py`; others fall back to KAS (H17) |
| R-LIFE-1 | ✅ | ✅ | ✅ | ✅ | ❓ | ❓ | ❓ | ❓ | ❓ | `SESSION_EVICTION` = {kiro, kas, codex} `backends_sessions.py`; claude `CLIENT_NEW_CONVERSATION` |
| R-LIFE-2 | ✅ | ✅ | ✅ | ✅ | ❓ | ❓ | ❓ | ❓ | ❓ | KAS deletes (`acp/harness/base.py`); codex rollout delete `acp/harness/codex.py`; claude `acp/client_claude_session.py`; kiro sweep (#31395) |
| R-LIFE-3 | ✅ | ✅ | ❓ | ✅ | ❓ | ❓ | ❓ | ❓ | ❓ | `require_replay=self.is_kas_backend` `providers/acp_runtime_start.py`; kiro/codex answer not-found |
| R-LIFE-4 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | shared idle sweep (#32376, #22356) |
| R-LIFE-5 | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | readers for kiro-cli pair and KAS sessions only |
| R-LIFE-6 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | shared `subagent_parts/start_pricing.py`, `subagent_mcp_roster.py` (no backend branch) |
| R-LIFE-7 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | shared replay lease `session_parts/record.py`, `dashboard/chat_turn/turn_admission.py` (#32484) |
| R-USE-1 | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ❌ | ❌ | ❌ | `ACP_BACKENDS_REPORT_USAGE` `agent_sdk/backends_turns.py` |
| R-USE-2 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ➖ | `ACP_BACKENDS_COMPACT` `backends_turns.py`; deepseek recycles context |
| R-USE-3 | ✅ | ✅ | ✅ | ✅ | ❓ | ❓ | ❓ | ❓ | ➖ | `COMPACTION_REPORTED` `backends_turns.py` |
| R-USE-4 | ✅ | ✅ | ➖ | ➖ | ➖ | ➖ | ➖ | ➖ | ❓ | kiro/KAS restart fixes (#35428, #33149); inline compaction harnesses never restart (`INLINE_COMPACTION` `backends_turns.py`); deepseek recycle not checked |
| R-PRM-1 | ✅ | ✅ | ✅ | ✅ | ❓ | ❓ | ❓ | ❓ | ❓ | `ACP_BACKENDS_SENT_IMAGE_RECORD` `backends_turns.py`; others inline every turn by design, replay cost unmeasured |
| R-ROW-1 | ✅ | ✅ | ❓ | ❓ | ❓ | ❓ | ❓ | ❓ | ❓ | kiro-cli v3 rows (#25650, #26589); other harnesses' tool-row shapes per version unmeasured |
| R-CMD-1 | ✅ | ✅ | ✅ | ✅ | ❓ | ❓ | ❓ | ❓ | ❓ | `KIRO_SLASH_COMMANDS` `backends_turns.py`, `HARNESS_OWNED_SLASH_COMMANDS`, `LOCAL_HARNESS_COMMANDS`, codex row `ACP_BACKEND_PROMPT_HANDLER_COMMANDS`; others keep the prompt path unchecked |
| R-ERR-1 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | #28513, #30038 shared |
| R-ERR-2 | ✅ | ✅ | ✅ | ❓ | ❓ | ✅ | ❓ | ❓ | ❓ | kiro/KAS #28549; claude #30087; goose #28414 |
| R-ERR-3 | ➖ | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | KAS pattern |
| R-ERR-4 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | #35765 shared |
| R-ERR-5 | ❓ | ✅ | ❓ | ❓ | ❓ | ❓ | ❓ | ❓ | ❓ | `acp/liveness_tool_state.py` reads KAS's core-tool name shape |

## Every not-met cell, per harness

### kiro-cli

✅ 59, ❌ 1, ❓ 6, ➖ 7.

| Req | Size | Where | Reuse or new design | Install to verify | User impact | Owner |
|---|---|---|---|---|---|---|
| R-LAU-3 | S | floors only: `kiro_cli.py` (2.35.2), `acp/chat_runtime_sharing.py` (3.1.0); no upper bound | reuse `_TESTED_VERSION_RANGES` (`client_turn_control.py`) | no | a new kiro-cli major runs unmeasured (3.1 loading every .json, #36125) | no open PR |

Unknown (❓, not measured): R-LAU-8, R-MCP-9, R-MCP-10, R-MCP-14, R-MOD-3, R-ERR-5.

### KAS

✅ 71, ❌ 1, ❓ 1, ➖ 0.

| Req | Size | Where | Reuse or new design | Install to verify | User impact | Owner |
|---|---|---|---|---|---|---|
| R-LAU-3 | S | relay route: floor `acp/harness/kas.py` only; bundle route is pinned (`acp/kas_pin.py`, 0.66.104) | reuse range table for the relay engine version | no | relay users on a newer engine get no notice | no open PR |

Unknown (❓, not measured): R-LAU-8.

### claude

✅ 43, ❌ 8, ❓ 14, ➖ 8.

| Req | Size | Where | Reuse or new design | Install to verify | User impact | Owner |
|---|---|---|---|---|---|---|
| R-LAU-3 | S | floors only `acp/client_claude_session.py`; doctor warns below floor `doctor_checks/agents.py` | reuse range table | no | a newer claude-agent-acp runs unmeasured | no open PR |
| R-LAU-9 | S | `acp/launch.py` runs `retrying_spawn_factory` only when set; `acp/client_spawn_env.py` builds `LaunchTools` without it, so `is_windows_spawn_blocked` (`acp/runtime_spawn_launch.py`) never runs on the direct client | reuse KAS fix: pass `retrying_spawn_factory` or classify in `launch.py` | no (Windows host to confirm) | Windows AppLocker/EDR block shown as a transport error | no open PR |
| R-LAU-12 | S | `dashboard/openai_compat.py` calls `reject_if_kiro_unverified` for every `/v1/chat/completions`, whatever harness the slot runs | reuse `rerun_signs_in_via_kiro_cli` (`kiro_readiness.py`) | no | OpenAI-compatible endpoint refused when kiro-cli is signed out on a host whose default harness is not kiro | no open PR |
| R-SPEC-7 | S | shared `_load_agent_prompt` `context_assembly/agent_spec.py`: `data.get('prompt')` only, `expanduser()` only, returns `''` on a bad file | reuse KAS reader `acp/kas_prompt.py` | no | a kiro-cli agent with `systemPrompt` or `$HOME` in `file://` runs with no persona | no open PR |
| R-SPEC-10 | S | `dashboard/chat_turn/turn_dispatch_gates.py` `_on_kas(client)` | reuse: key on `CREW_FIRES_SPEC_OBSERVER_HOOKS` (`backends_routing.py`) | no | agentSpawn context lost after Crew-driven compaction | no open PR |
| R-MCP-8 | S | `dashboard/chat_turn/turn_spec_hooks.py` (`on_kas and discards_mcp_oauth(client)`) | reuse: compute for every non-`REMOTE_MCP_OAUTH` harness | no | a dropped `oauth` block is silent; low value | no open PR |
| R-LIFE-5 | M | `dashboard/session_transfer_layers.py` reads only kiro-cli `<sid>.json/.jsonl` and KAS session dirs | new reader per harness store | yes | Send session carries the transcript only; low value, by design today | no open PR |
| R-ERR-3 | S | `undelivered_call_watch.py` matches only `KAS_TOOL_UNAVAILABLE_RE` (`agent_sdk/kas_tool_names.py`) | reuse the watcher with a per-harness pattern row | yes (capture each harness's 'tool not available' text) | a non-KAS worker's lost `work_report` is never flagged to its conductor | no open PR |

Unknown (❓, not measured): R-LAU-8, R-LAU-11, R-AUTH-2, R-SPEC-13, R-MCP-4, R-MCP-5, R-MCP-7, R-MCP-9, R-MCP-10, R-MCP-14, R-STR-2, R-LIFE-3, R-ROW-1, R-ERR-5.

### codex

✅ 43, ❌ 8, ❓ 13, ➖ 9.

| Req | Size | Where | Reuse or new design | Install to verify | User impact | Owner |
|---|---|---|---|---|---|---|
| R-LAU-12 | S | `dashboard/openai_compat.py` calls `reject_if_kiro_unverified` for every `/v1/chat/completions`, whatever harness the slot runs | reuse `rerun_signs_in_via_kiro_cli` (`kiro_readiness.py`) | no | OpenAI-compatible endpoint refused when kiro-cli is signed out on a host whose default harness is not kiro | no open PR |
| R-SPEC-7 | S | shared `_load_agent_prompt` `context_assembly/agent_spec.py`: `data.get('prompt')` only, `expanduser()` only, returns `''` on a bad file | reuse KAS reader `acp/kas_prompt.py` | no | a kiro-cli agent with `systemPrompt` or `$HOME` in `file://` runs with no persona | no open PR |
| R-SPEC-10 | S | `dashboard/chat_turn/turn_dispatch_gates.py` `_on_kas(client)` | reuse: key on `CREW_FIRES_SPEC_OBSERVER_HOOKS` (`backends_routing.py`) | no | agentSpawn context lost after Crew-driven compaction | no open PR |
| R-MCP-8 | S | `dashboard/chat_turn/turn_spec_hooks.py` (`on_kas and discards_mcp_oauth(client)`) | reuse: compute for every non-`REMOTE_MCP_OAUTH` harness | no | a dropped `oauth` block is silent; low value | no open PR |
| R-SIDE-1 | M | codex not in `SIDE_READONLY` | new capture of a read-only turn | yes | no Side Chat tools on codex | no open PR |
| R-LIFE-5 | M | `dashboard/session_transfer_layers.py` reads only kiro-cli `<sid>.json/.jsonl` and KAS session dirs | new reader per harness store | yes | Send session carries the transcript only; low value, by design today | no open PR |
| R-USE-1 | M | not in `REPORT_USAGE` (`backends_turns.py`) | new: capture the harness's usage frame | yes | Usage page empty for this harness | no open PR |
| R-ERR-3 | S | `undelivered_call_watch.py` matches only `KAS_TOOL_UNAVAILABLE_RE` (`agent_sdk/kas_tool_names.py`) | reuse the watcher with a per-harness pattern row | yes (capture each harness's 'tool not available' text) | a non-KAS worker's lost `work_report` is never flagged to its conductor | no open PR |

Unknown (❓, not measured): R-LAU-8, R-LAU-11, R-AUTH-2, R-SPEC-13, R-MCP-4, R-MCP-5, R-MCP-7, R-MCP-9, R-MCP-10, R-MCP-14, R-ROW-1, R-ERR-2, R-ERR-5.

### opencode

✅ 32, ❌ 9, ❓ 21, ➖ 11.

| Req | Size | Where | Reuse or new design | Install to verify | User impact | Owner |
|---|---|---|---|---|---|---|
| R-LAU-7 | S | `acp/harness/opencode.py`: two `subprocess.run(timeout=...)` probes with no new session or tree kill | reuse deepseek/codex pattern | no | timed-out probe leaves node children | #35757 (open) |
| R-LAU-9 | S | `acp/launch.py` runs `retrying_spawn_factory` only when set; `acp/client_spawn_env.py` builds `LaunchTools` without it, so `is_windows_spawn_blocked` (`acp/runtime_spawn_launch.py`) never runs on the direct client | reuse KAS fix: pass `retrying_spawn_factory` or classify in `launch.py` | no (Windows host to confirm) | Windows AppLocker/EDR block shown as a transport error | no open PR |
| R-LAU-12 | S | `dashboard/openai_compat.py` calls `reject_if_kiro_unverified` for every `/v1/chat/completions`, whatever harness the slot runs | reuse `rerun_signs_in_via_kiro_cli` (`kiro_readiness.py`) | no | OpenAI-compatible endpoint refused when kiro-cli is signed out on a host whose default harness is not kiro | no open PR |
| R-SPEC-7 | S | shared `_load_agent_prompt` `context_assembly/agent_spec.py`: `data.get('prompt')` only, `expanduser()` only, returns `''` on a bad file | reuse KAS reader `acp/kas_prompt.py` | no | a kiro-cli agent with `systemPrompt` or `$HOME` in `file://` runs with no persona | no open PR |
| R-SPEC-10 | S | `dashboard/chat_turn/turn_dispatch_gates.py` `_on_kas(client)` | reuse: key on `CREW_FIRES_SPEC_OBSERVER_HOOKS` (`backends_routing.py`) | no | agentSpawn context lost after Crew-driven compaction | no open PR |
| R-MCP-8 | S | `dashboard/chat_turn/turn_spec_hooks.py` (`on_kas and discards_mcp_oauth(client)`) | reuse: compute for every non-`REMOTE_MCP_OAUTH` harness | no | a dropped `oauth` block is silent; low value | no open PR |
| R-SIDE-1 | M | opencode not in `SIDE_READONLY` | new capture | yes | same | no open PR |
| R-LIFE-5 | M | `dashboard/session_transfer_layers.py` reads only kiro-cli `<sid>.json/.jsonl` and KAS session dirs | new reader per harness store | yes | Send session carries the transcript only; low value, by design today | no open PR |
| R-ERR-3 | S | `undelivered_call_watch.py` matches only `KAS_TOOL_UNAVAILABLE_RE` (`agent_sdk/kas_tool_names.py`) | reuse the watcher with a per-harness pattern row | yes (capture each harness's 'tool not available' text) | a non-KAS worker's lost `work_report` is never flagged to its conductor | no open PR |

Unknown (❓, not measured): R-LAU-8, R-LAU-11, R-AUTH-2, R-AUTH-3, R-SPEC-6, R-SPEC-13, R-MCP-4, R-MCP-5, R-MCP-7, R-MCP-10, R-MCP-14, R-SUB-2, R-LIFE-1, R-LIFE-2, R-LIFE-3, R-USE-3, R-PRM-1, R-ROW-1, R-CMD-1, R-ERR-2, R-ERR-5.

### goose

✅ 36, ❌ 8, ❓ 18, ➖ 11.

| Req | Size | Where | Reuse or new design | Install to verify | User impact | Owner |
|---|---|---|---|---|---|---|
| R-LAU-5 | S | absent from `PINNED_FIXTURE_ADAPTERS` (`test_real_adapter_fixture_pins.py`) | reuse: add a row | yes (lane pin) | indirect: fixture drift unseen | no open PR |
| R-LAU-9 | S | `acp/launch.py` runs `retrying_spawn_factory` only when set; `acp/client_spawn_env.py` builds `LaunchTools` without it, so `is_windows_spawn_blocked` (`acp/runtime_spawn_launch.py`) never runs on the direct client | reuse KAS fix: pass `retrying_spawn_factory` or classify in `launch.py` | no (Windows host to confirm) | Windows AppLocker/EDR block shown as a transport error | no open PR |
| R-LAU-12 | S | `dashboard/openai_compat.py` calls `reject_if_kiro_unverified` for every `/v1/chat/completions`, whatever harness the slot runs | reuse `rerun_signs_in_via_kiro_cli` (`kiro_readiness.py`) | no | OpenAI-compatible endpoint refused when kiro-cli is signed out on a host whose default harness is not kiro | no open PR |
| R-SPEC-7 | S | shared `_load_agent_prompt` `context_assembly/agent_spec.py`: `data.get('prompt')` only, `expanduser()` only, returns `''` on a bad file | reuse KAS reader `acp/kas_prompt.py` | no | a kiro-cli agent with `systemPrompt` or `$HOME` in `file://` runs with no persona | no open PR |
| R-SPEC-10 | S | `dashboard/chat_turn/turn_dispatch_gates.py` `_on_kas(client)` | reuse: key on `CREW_FIRES_SPEC_OBSERVER_HOOKS` (`backends_routing.py`) | no | agentSpawn context lost after Crew-driven compaction | no open PR |
| R-MCP-8 | S | `dashboard/chat_turn/turn_spec_hooks.py` (`on_kas and discards_mcp_oauth(client)`) | reuse: compute for every non-`REMOTE_MCP_OAUTH` harness | no | a dropped `oauth` block is silent; low value | no open PR |
| R-LIFE-5 | M | `dashboard/session_transfer_layers.py` reads only kiro-cli `<sid>.json/.jsonl` and KAS session dirs | new reader per harness store | yes | Send session carries the transcript only; low value, by design today | no open PR |
| R-ERR-3 | S | `undelivered_call_watch.py` matches only `KAS_TOOL_UNAVAILABLE_RE` (`agent_sdk/kas_tool_names.py`) | reuse the watcher with a per-harness pattern row | yes (capture each harness's 'tool not available' text) | a non-KAS worker's lost `work_report` is never flagged to its conductor | no open PR |

Unknown (❓, not measured): R-LAU-8, R-LAU-11, R-SPEC-6, R-SPEC-13, R-MCP-5, R-MCP-7, R-MCP-10, R-MCP-14, R-STR-2, R-SUB-2, R-LIFE-1, R-LIFE-2, R-LIFE-3, R-USE-3, R-PRM-1, R-ROW-1, R-CMD-1, R-ERR-5.

### pi

✅ 30, ❌ 9, ❓ 21, ➖ 13.

| Req | Size | Where | Reuse or new design | Install to verify | User impact | Owner |
|---|---|---|---|---|---|---|
| R-LAU-3 | S | floor only `acp/harness/pi.py` | reuse range table | no | a newer pi runs unmeasured | no open PR |
| R-LAU-7 | S | `acp/harness/pi.py`, `acp/harness/pi_node.py` same shape | reuse | no | same | #35757 (open) |
| R-LAU-9 | S | `acp/launch.py` runs `retrying_spawn_factory` only when set; `acp/client_spawn_env.py` builds `LaunchTools` without it, so `is_windows_spawn_blocked` (`acp/runtime_spawn_launch.py`) never runs on the direct client | reuse KAS fix: pass `retrying_spawn_factory` or classify in `launch.py` | no (Windows host to confirm) | Windows AppLocker/EDR block shown as a transport error | no open PR |
| R-LAU-12 | S | `dashboard/openai_compat.py` calls `reject_if_kiro_unverified` for every `/v1/chat/completions`, whatever harness the slot runs | reuse `rerun_signs_in_via_kiro_cli` (`kiro_readiness.py`) | no | OpenAI-compatible endpoint refused when kiro-cli is signed out on a host whose default harness is not kiro | no open PR |
| R-SPEC-7 | S | shared `_load_agent_prompt` `context_assembly/agent_spec.py`: `data.get('prompt')` only, `expanduser()` only, returns `''` on a bad file | reuse KAS reader `acp/kas_prompt.py` | no | a kiro-cli agent with `systemPrompt` or `$HOME` in `file://` runs with no persona | no open PR |
| R-SIDE-1 | M | pi not in `SIDE_READONLY` | existing open PR | yes | same | #21744 (open) |
| R-LIFE-5 | M | `dashboard/session_transfer_layers.py` reads only kiro-cli `<sid>.json/.jsonl` and KAS session dirs | new reader per harness store | yes | Send session carries the transcript only; low value, by design today | no open PR |
| R-USE-1 | M | not in `REPORT_USAGE` (`backends_turns.py`) | new: capture the harness's usage frame | yes | Usage page empty for this harness | no open PR |
| R-ERR-3 | S | `undelivered_call_watch.py` matches only `KAS_TOOL_UNAVAILABLE_RE` (`agent_sdk/kas_tool_names.py`) | reuse the watcher with a per-harness pattern row | yes (capture each harness's 'tool not available' text) | a non-KAS worker's lost `work_report` is never flagged to its conductor | no open PR |

Unknown (❓, not measured): R-LAU-8, R-LAU-11, R-SPEC-6, R-SPEC-13, R-MCP-4, R-MCP-5, R-MCP-7, R-MCP-8, R-MCP-10, R-MCP-14, R-STR-3, R-SUB-2, R-LIFE-1, R-LIFE-2, R-LIFE-3, R-USE-3, R-PRM-1, R-ROW-1, R-CMD-1, R-ERR-2, R-ERR-5.

### copilot

✅ 28, ❌ 13, ❓ 18, ➖ 14.

| Req | Size | Where | Reuse or new design | Install to verify | User impact | Owner |
|---|---|---|---|---|---|---|
| R-LAU-9 | S | `acp/launch.py` runs `retrying_spawn_factory` only when set; `acp/client_spawn_env.py` builds `LaunchTools` without it, so `is_windows_spawn_blocked` (`acp/runtime_spawn_launch.py`) never runs on the direct client | reuse KAS fix: pass `retrying_spawn_factory` or classify in `launch.py` | no (Windows host to confirm) | Windows AppLocker/EDR block shown as a transport error | no open PR |
| R-LAU-12 | S | `dashboard/openai_compat.py` calls `reject_if_kiro_unverified` for every `/v1/chat/completions`, whatever harness the slot runs | reuse `rerun_signs_in_via_kiro_cli` (`kiro_readiness.py`) | no | OpenAI-compatible endpoint refused when kiro-cli is signed out on a host whose default harness is not kiro | no open PR |
| R-SPEC-7 | S | shared `_load_agent_prompt` `context_assembly/agent_spec.py`: `data.get('prompt')` only, `expanduser()` only, returns `''` on a bad file | reuse KAS reader `acp/kas_prompt.py` | no | a kiro-cli agent with `systemPrompt` or `$HOME` in `file://` runs with no persona | no open PR |
| R-MCP-2 | M | copilot not in `ACP_BACKENDS_MEMBER_DISPATCH` (`agent_sdk/backends_members.py`) | reuse member-dispatch mount after a capture | yes | copilot cannot host member chats | no open PR |
| R-MCP-8 | S | `dashboard/chat_turn/turn_spec_hooks.py` (`on_kas and discards_mcp_oauth(client)`) | reuse: compute for every non-`REMOTE_MCP_OAUTH` harness | no | a dropped `oauth` block is silent; low value | no open PR |
| R-STR-1 | M | copilot not in `STEERING_REQUEST` (`agent_sdk/backends_turns.py`) | new capture | yes | no mid-turn steer on copilot | no open PR |
| R-STR-3 | M | live background shell holds the turn | existing open PR | yes | copilot turns hang | #34088 (open) |
| R-SIDE-1 | M | copilot not in `SIDE_READONLY` | new capture | yes | same | no open PR |
| R-MOD-1 | M | copilot not in `MODEL_VIA_CONFIG_OPTION` (`agent_sdk/backends_models.py`) | existing open PR | yes | no model picker on copilot | #36118 (open) |
| R-MOD-2 | M | copilot not in `EFFORT_VIA_CONFIG_OPTION` (`backends_models.py`) | existing open PR | yes | no effort control on copilot | #36118 (open) |
| R-LIFE-5 | M | `dashboard/session_transfer_layers.py` reads only kiro-cli `<sid>.json/.jsonl` and KAS session dirs | new reader per harness store | yes | Send session carries the transcript only; low value, by design today | no open PR |
| R-USE-1 | M | not in `REPORT_USAGE` (`backends_turns.py`) | new: capture the harness's usage frame | yes | Usage page empty for this harness | no open PR |
| R-ERR-3 | S | `undelivered_call_watch.py` matches only `KAS_TOOL_UNAVAILABLE_RE` (`agent_sdk/kas_tool_names.py`) | reuse the watcher with a per-harness pattern row | yes (capture each harness's 'tool not available' text) | a non-KAS worker's lost `work_report` is never flagged to its conductor | no open PR |

Unknown (❓, not measured): R-LAU-8, R-LAU-11, R-SPEC-6, R-SPEC-13, R-MCP-5, R-MCP-7, R-MCP-10, R-MCP-14, R-SUB-2, R-LIFE-1, R-LIFE-2, R-LIFE-3, R-USE-3, R-PRM-1, R-ROW-1, R-CMD-1, R-ERR-2, R-ERR-5.

### deepseek

✅ 29, ❌ 10, ❓ 19, ➖ 15.

| Req | Size | Where | Reuse or new design | Install to verify | User impact | Owner |
|---|---|---|---|---|---|---|
| R-LAU-3 | S | `acp/harness/deepseek.py` reads no version | reuse range table; needs a version source | yes (find where dsh reports its version) | deepseek upgrades run unmeasured | no open PR |
| R-LAU-5 | S | absent from `PINNED_FIXTURE_ADAPTERS` | reuse: add a row | yes (lane pin) | indirect | no open PR |
| R-LAU-9 | S | `acp/launch.py` runs `retrying_spawn_factory` only when set; `acp/client_spawn_env.py` builds `LaunchTools` without it, so `is_windows_spawn_blocked` (`acp/runtime_spawn_launch.py`) never runs on the direct client | reuse KAS fix: pass `retrying_spawn_factory` or classify in `launch.py` | no (Windows host to confirm) | Windows AppLocker/EDR block shown as a transport error | no open PR |
| R-LAU-12 | S | `dashboard/openai_compat.py` calls `reject_if_kiro_unverified` for every `/v1/chat/completions`, whatever harness the slot runs | reuse `rerun_signs_in_via_kiro_cli` (`kiro_readiness.py`) | no | OpenAI-compatible endpoint refused when kiro-cli is signed out on a host whose default harness is not kiro | no open PR |
| R-SPEC-7 | S | shared `_load_agent_prompt` `context_assembly/agent_spec.py`: `data.get('prompt')` only, `expanduser()` only, returns `''` on a bad file | reuse KAS reader `acp/kas_prompt.py` | no | a kiro-cli agent with `systemPrompt` or `$HOME` in `file://` runs with no persona | no open PR |
| R-MCP-8 | S | `dashboard/chat_turn/turn_spec_hooks.py` (`on_kas and discards_mcp_oauth(client)`) | reuse: compute for every non-`REMOTE_MCP_OAUTH` harness | no | a dropped `oauth` block is silent; low value | no open PR |
| R-STR-1 | M | deepseek not in any steer set (`backends_turns.py`) | new design: measure first | yes | no mid-turn steer on deepseek | no open PR |
| R-LIFE-5 | M | `dashboard/session_transfer_layers.py` reads only kiro-cli `<sid>.json/.jsonl` and KAS session dirs | new reader per harness store | yes | Send session carries the transcript only; low value, by design today | no open PR |
| R-USE-1 | M | not in `REPORT_USAGE` (`backends_turns.py`) | new: capture the harness's usage frame | yes | Usage page empty for this harness | no open PR |
| R-ERR-3 | S | `undelivered_call_watch.py` matches only `KAS_TOOL_UNAVAILABLE_RE` (`agent_sdk/kas_tool_names.py`) | reuse the watcher with a per-harness pattern row | yes (capture each harness's 'tool not available' text) | a non-KAS worker's lost `work_report` is never flagged to its conductor | no open PR |

Unknown (❓, not measured): R-LAU-8, R-LAU-11, R-SPEC-6, R-SPEC-13, R-MCP-5, R-MCP-7, R-MCP-10, R-MCP-14, R-STR-3, R-SUB-2, R-LIFE-1, R-LIFE-2, R-LIFE-3, R-USE-4, R-PRM-1, R-ROW-1, R-CMD-1, R-ERR-2, R-ERR-5.

## Not checked

- No harness was installed or run. Every ❓ is a measurement gap, not a negative.
- strands and mimocode are not in this matrix.
- The shared-code ✅ cells (R-LAU-6, R-LIFE-4, R-LIFE-6, R-LIFE-7, R-MCP-13, R-SKL-1, R-SBX-4) rest on reading one shared path with no harness branch. A harness-specific override elsewhere would not show up in that read.
- R-LAU-12: only the callers of `reject_if_kiro_unverified` were read (`dashboard/openai_compat.py`, `dashboard/handlers/sessions.py`, `dashboard/handlers/agents.py`, `dashboard/chat_regenerate.py`, `dashboard/chat_rewind.py`). The usage and catalogue callers are kiro-only reads and are fine.
- Owners name PRs that were open on 2026-11-13; they may have moved since.

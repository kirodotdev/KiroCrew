# Request for Change

Design documents for changes that are large enough, risky enough, or contested
enough to be worth writing down before building. An RFC here is a **proposal and
a record of a decision** — not a description of what the code does today. For
that, read the code, or the `status` field described below.

## Index

Every status below is verified against the code — definitions *and* callers — and
against merged PR history, never taken from the document's own claims. Rows are
re-audited in waves rather than all at once, so each document's own
`last-audited` / `audited-at` front matter is the authoritative freshness signal
for that row; when it is far behind main, distrust the status. The original
directory-wide audit ran **2026-08-03** against main `0ab6ed48`; later additions
and re-audits are recorded below. The `rfc-tailnet-dashboard-access`
row was added later and re-verified against `429cbad8`, and `rfc-session-address-model`
was added later still and re-verified against `707b8aef2`; the
`rfc-everything-is-an-app` and `rfc-amend-tenets-everything-is-an-app` rows
were added
2026-08-18 and verified against `e6b06685e`; the other rows have
not been re-audited since 2026-08-03. The durable-run-coordinator row was added
2026-08-22 and the orchestrator-chat-sessions row was re-audited at
`c4f253891`; the `rfc-token-efficient-monitors` row was added 2026-08-22 and
verified against `6d3e30bbbd`. The `rfc-global-workflow-library` row was added
2026-08-25 and audited against `749468d42`; its implementation exists only in
the active detached worktree. The `rfc-agentcore-identity-gateway` row
was added 2026-08-27. The `rfc-crew-agent-sdk-boundary` row was added 2026-08-28 and verified against `dc88f142b`. The `rfc-transcript-section-markers` row was added 2026-08-30 and verified against `202770d13`. The `rfc-mcp-lifecycle-event-log` row was added 2026-09-01 and verified against `1ee69f225`. The `rfc-session-tag-change-event` row was added 2026-09-02 and verified against `6581a04ee`. The `rfc-agent-config-mirror` row was added 2026-09-02 and verified against `f8b3203c0`. The `rfc-chat-core-extraction` row was added 2026-09-05 and verified against `8ed028b0b`. The `rfc-conductor-work-ledger` row was added 2026-09-05 and verified against `049b8c729`. The `rfc-app-session-controls` row was added 2026-08-31 and re-verified against `1d705a03f` on 2026-09-01. The `rfc-central-governance-ceiling` row was added 2026-09-08 and verified against `8a9c269b4`. The `rfc-security-conductor` row was re-audited 2026-09-10 against `f6d38741c`. The `rfc-overload-resilience` row was added 2026-09-12 and verified against `6f056722b`. The `rfc-append-only-ledger` row was added 2026-09-11 and verified against `2f1e2f54a`, and moved to `in-progress` the same day against the open PR named in it rather than against main. The `rfc-webhook-subscriptions` row was added 2026-09-15 and verified against `6163d9a9ca`. The `rfc-arrival-provenance-filing` row was added 2026-09-20 and verified against `f31f25a8a2`. The `rfc-wake-judge` row was added 2026-09-22 and verified against `80bd0a81f`. The `rfc-crewmates-launch` row was added 2026-09-22 and verified against `87553ba866`. The `rfc-conductor-work-ledger` row was re-audited 2026-09-22 against `cc385211d8` for its v4 revision, which moves Phases 3 and 4 to in flight against two open PRs rather than against main. The `rfc-host-credential-vending` row was added 2026-09-23 and verified against `18f9984b04`. The `rfc-wake-judge` row was re-audited 2026-09-23 against `2fdadc71ac`, which moves it to `partial` for the LLM lane on main. On 2026-09-24 only its in-flight-core reading was re-read, at `d81bd0ec17`, to settle that document's section 7 Q4; its main-side audit stands where the row's front matter records it.

| Document | Status | What is actually on main |
|---|---|---|
| [rfc-host-credential-vending.md](rfc-host-credential-vending.md) | `draft` | Nothing of the design; the document lands first and the code follows in its own PR. Verified at `18f9984b04`: `ADAPTER_EXPOSED_CREDENTIAL_LEAVES` in `agent_sdk/tool_gate.py` re-exposes exactly `.aws/config` for `codex` and nothing else, `adapter_hidden_credential_dirs` projects the whole `_SENSITIVE_HOME_DIRS` floor (which carries `.aws` and `.ssh`), no granted leaf exists for a credential helper, and `CodexHarness.host_answered_methods` is empty so the KAS vending channel this design rejects is genuinely unavailable there. The precedent it builds on is real: `PI_GATE_ARTIFACT_LEAF` is granted per backend inside `adapter_hidden_credential_dirs` and staged by `_pi_gate_artifact_dir` and `_ensure_pi_gate_launcher` in `acp/client.py` |
| [rfc-wake-judge.md](rfc-wake-judge.md) | `partial` | The LLM lane, from [#12776](https://github.com/kirodotdev/KiroCrew/pull/12776). Verified at `2fdadc71ac`: `impl_llm.py` holds the lane, `gate.py` carries `nudge.wake` in `DECISION_POINT_NAMES` beside `JUDGE_POINT` and the two lane names, `impl_jev.py` holds `JevOracle` and `_to_wire`, and `points/` carries six live points (`skills_select`, `tool_risk`, `model_route`, `memory_recall`, `message_steer`, `compaction_keep`) — but the core is on a branch, not on main: `points/nudge_wake.py`, the `nudge_evidence` entry in `POINT_SCOPE_KEYS`, the collectors, the tick hook, `judge` on `monitor_start` / `monitor_update` and the transcript notice are in flight on [#12787](https://github.com/kirodotdev/KiroCrew/pull/12787), read at `d81bd0ec17`, where §7 Q4 is settled: a loop that arms no criteria is screened on the `nudge_evidence` grant read as `gate.judge_evidence_scope_granted`, before a lane is picked. No `kirocrew-judge` template ships: the landed lane runs the bundled `kirocrew-lite` (`JUDGE_AGENT_NAME`). `decisions.nudge_wake.provider` and `.llm_model` are editable config keys; the `quiet_streak_floor` key §5 tunes its compounding bound with comes with the core. `types.py` declares one wire type, `class Choice`, aliased as `Question`, so the `noul` and `score` types §3.1 needs are absent. The tick it hooks is `_monitor_tick_is_quiet` in `autonudge.py`, whose quiet-streak floor `_MAX_QUIET_STREAK` is 10, the value §3.1 defers to, and the typed probe it composes with is `PrWatchProbe` in `probes/gh_pr.py`. The Decisions-card shape §3.6 mounts its row on is on main ([#12598](https://github.com/kirodotdev/KiroCrew/pull/12598)), and so is the `nudge.wake` row itself with its provider and LLM-model pickers: `_points` emits one row per `DECISION_POINT_NAMES`, which already carries the point, at `needs_scope: null`. Only the detail panel's consent switch waits on the `nudge_evidence` scope |
| [rfc-redaction-explain-and-reveal.md](rfc-redaction-explain-and-reveal.md) | `accepted` | This document lands first so the implementation is judged against a decision on the base branch. Nothing on main beyond the widget-frame CSP that predates it (`widgetSrcdoc.ts` `cspFor` — `default-src 'none'`, `img-src data: blob:`). Rollout steps 1 (zero-click renderer rules: deferred remote media, Slack/Discord/Telegram previews off) and 3 (blocked-link chip that keeps the full URL so a wrongly blocked link can be opened once) are implemented together in one PR; steps 2 and 4–6 unstarted |
| [rfc-crewmates-launch.md](rfc-crewmates-launch.md) | `accepted` | The vocabulary and screens 01–09 are the product owner's decision of 2026-09-22; this document lands first because every screen replaces or removes something a person sees today and the First Principles lane reads the decision off the base branch. Verified at `87553ba866`: the page still says "Agent Capabilities" (`pages.kiroCrewAgentsPage.agent_capabilities` in `website/src/i18n/locales/en.json`), the crewmate panel still opens on the single Crew summary tab (`CREW_SUMMARY_TAB_ID` in `website/src/pages/members/MembersPage.tsx`), and nothing under `website/src/pages/members/` renders an opt-in step, a thread panel or a team view. Four PRs are open against it — [#12797](https://github.com/kirodotdev/KiroCrew/pull/12797) threads API, [#12798](https://github.com/kirodotdev/KiroCrew/pull/12798) opt-in, [#12805](https://github.com/kirodotdev/KiroCrew/pull/12805) panel tabs, [#12806](https://github.com/kirodotdev/KiroCrew/pull/12806) Customize — and § 8 names the branches still to open |
| [rfc-arrival-provenance-filing.md](rfc-arrival-provenance-filing.md) | `implemented` | Shipped. Verified at `80bd0a81f`: `dashboard/arrival_folders.py` is the filing module — `store_folder_name` / `sender_folder_name` derive the `Imported` / `from <sender>` rows, `arrival_folder_id` does the find-or-create, and `mark_arrival_folder_shared` plus `discard_arrival_folders` handle adoption and cleanup — and `dashboard/session_transfer.py` imports it so an imported slot is filed instead of landing unfiled |
| [rfc-solo-spawn-gate.md](rfc-solo-spawn-gate.md) | `superseded` | Shipped via [#11710](https://github.com/kirodotdev/KiroCrew/pull/11710); removal decided 2026-09-24 after a local `spawn.solo` audit showed 33 of 34 one-task calls allowed. This document lands first so the removal is judged against a decision on the base branch. Verified at `9b098d79fb`: the gate is still live — `src/kiro_crew/solo_spawn.py` carries `delegation_refusal` / `solo_spawn_refusal` / `parent_work_supported`, `mcp_tools/spawn.py` and `dashboard/handlers/messaging.py` call it, and `test/test_spawn_solo_gate.py` pins the refusal. [#13458](https://github.com/kirodotdev/KiroCrew/pull/13458) removes them; delegation policy moves to prompt guidance owned by `system-specs/modules/subagent.md` |
| [rfc-webhook-subscriptions.md](rfc-webhook-subscriptions.md) | `draft` | Nothing. Verified at `6163d9a9ca`: the inbound webhook is `POST /api/hooks/agent` alone (`dashboard/handlers/hooks.py` `api_hooks_agent`), which runs one turn in a `hook:*`-keyed session from a caller-shaped `{message, sessionKey}` body and refuses a second call on a busy key (`_hook_inflight_sessions`); `webhooks.py` token entries carry no scheme, slug or payload mapping; no subscription store or event buffer exists; `dashboard/state.py` carries no `[Webhook wake]` prefix; `dashboard/tailnet_serve.py` runs `tailscale serve` (tailnet-only, no Funnel); and `irq.py` and `monitoring/` wake only from cron-tick probes |
| [rfc-overload-resilience.md](rfc-overload-resilience.md) | `partial` | The durable queue/admission/recovery core is on main as `src/kiro_crew/taskq/`, integrated with subagents, TaskRunner, workflows, and dashboard task routes. Verified at `80bd0a81f`; this G13 audit did not re-classify every remaining overload phase, so consult the RFC and canonical `docs/system-specs/modules/taskq.md` before treating the whole design as implemented. |
| [rfc-append-only-ledger.md](rfc-append-only-ledger.md) | `partial` | The `kiro_crew.crew_log` store, session emitter, message entries, projections, paging routes, and session-ledger fold are on main. They remain opt-in behind `KIROCREW_CREW_LOG`; the legacy transcript and conductor work-ledger stores still exist, so the full cutover is incomplete. Verified at `80bd0a81f`: `crew_log/store.py`, `crew_log/emit.py`, `crew_log/projection.py`, and `dashboard/handlers/crew_log.py` are live, while `history.py` still exposes `rewrite_session` / `sliding_window` and `work_ledger.py` remains a separate store. |
| [rfc-central-governance-ceiling.md](rfc-central-governance-ceiling.md) | `draft` | Partial. On this branch (#7362): the tighten-only ladder in `load_security_policy` (`platform/governance.py`) and central-above-env precedence, with no local rollback lever. Not on any branch, and design-of-record only: the withdrawn `break_glass` override and its four defect classes (#9106), the whole platform managed tier, and the Windows `HKLM\SOFTWARE\Policies\<vendor>\<product>` rung (Windows stays advisory) |
| [rfc-remote-instance-on-fargate.md](rfc-remote-instance-on-fargate.md) | `partial` | The Fargate launch path exists. Verified at `80bd0a81f`: `cloud/fargate/` carries `taskdef.py` (task-definition document), `runtask.py` (`run_task_request`, `Placement`, `TaskSize` and their refusals) and `identity.py` (`SecretRef`, `sole_binding`); `cloud/fargate_engine.py` is the engine with `TaskBounds` and `TaskSighting`; `cloud/config.py` consumes all of it, and `dashboard/handlers_cloud.py` / `handlers_instances.py` expose it. `launch_job.py` carries the `FargateSigninHandle` no-browser sign-in shape. Consult [`../system-specs/modules/cloud.md`](../system-specs/modules/cloud.md) and [`../system-specs/modules/instances.md`](../system-specs/modules/instances.md) for the shipped contract |
| [rfc-conductor-work-ledger.md](rfc-conductor-work-ledger.md) | `partial` | Phases 1, 2, 2.5 and the 2.6 swap are on main; verified at `dd0dce2643`. `src/kiro_crew/work_ledger.py` is the store ([#8855](https://github.com/kirodotdev/KiroCrew/pull/8855)); `src/kiro_crew/mcp_work.py` plus `dashboard/handlers/work_ledger.py` are the four tools and their `/api/work-ledger` routes, and `_MANAGED_MCP_SERVERS` carries the `opt_in` `kirocrew-work` entry ([#9152](https://github.com/kirodotdev/KiroCrew/pull/9152)); `agent.py` installs `kirocrew-worker` and, through the shared `_conductor_spec`, both `kirocrew-conductor` and its deprecated `kirocrew-ledger-conductor` alias — the swap that made `kirocrew-conductor` itself the ledger conductor, with the old name kept for one release (§Rollout note — swap done); `kirocrew-pipeline-conductor` and `kirocrew-security-conductor` still carry no work-ledger grant ([#9277](https://github.com/kirodotdev/KiroCrew/pull/9277) retracted the mount there and it stayed retracted). `goal-conductor/SKILL.md` is the ledger procedure and `goal-ledger-conductor/SKILL.md` is a pointer to it. Not started: the sibling digest, worker-to-worker channels, and the deletion of `goal-conductor/scripts/ledger_entry.py`, which is still shipped though nothing documents it. In flight rather than unstarted as of the v4 revision, and neither on main — `probes/__init__.py` `build` still maps exactly one kind, `gh-pr`: the wake gate is [#12781](https://github.com/kirodotdev/KiroCrew/pull/12781), a DRAFT pending at `c749aff0d4`, adding a `work-ledger` probe whose actionable set is three statuses because the store has no `request` event kind, and whose `test_only_the_phase_2_seams_import_the_module` guard is deliberately red naming `ledger_wake.py` and `probes/work_ledger.py` until a human adds them to `_PERMITTED_STORE_IMPORTERS`; the Crew page surfaces are [#12792](https://github.com/kirodotdev/KiroCrew/pull/12792), open and pending at `f0287a4a50`, serving `/crew-board` from a masked `GET /api/crew-board` in three bands, with `take_over` refusing `501` because no server-side re-own primitive exists to delegate to |
| [rfc-session-tag-change-event.md](rfc-session-tag-change-event.md) | `in-review` | Nothing. Verified at `6581a04ee`: `SessionTagsChanged` has zero hits in the tree, and `HOOK_EVENTS` (`hooks.py:93-99`) and `ALLOWED_HOOK_EVENTS` (`validation.py:92-94`) still carry exactly the five turn-lifecycle events. Tag writes still only `push_slots_update()` to the browser with no server-side consumer. The open implementation names the event **`SessionLaneChanged`**, not the RFC's `SessionTagsChanged` — it fires on a **status**-tag change, i.e. a board-lane transition — so a search for the RFC's name will keep returning zero even once the event exists |
| [rfc-agent-self-tagging.md](rfc-agent-self-tagging.md) | `partial` | v1 is on main via [#7779](https://github.com/kirodotdev/KiroCrew/pull/7779) (`780e75e2d`): the `chat_tag` session directive (`mcp_tools/control.py`, applied in `dashboard/session_directive_apply.py`), the protected grants store `dashboard/chat_tag_grants.py` under the masked and fenced `tag-grants` leaf with its HMAC provenance chain and boot seed, the `[BOARD]` context line in `context.py`, the per-tag `agent` policy on the tag PATCH, and the strict-boolean `status` validation. Open: the tag-manager control for the per-tag policy (the PATCH `agent` field has no dashboard sender), provenance-required minting (open question 3, v2), and the v2 scope (folder moves, conductor-over-children, propose-confirm, move budgets), all tracked on [#7774](https://github.com/kirodotdev/KiroCrew/issues/7774) |
| [rfc-agent-config-mirror.md](rfc-agent-config-mirror.md) | `partial` | The declared contract is on main: `providers/mirrors/base.py` defines `AgentConfigMirror` and `Disposition`, `registry.py` defines typed projection kinds, and parity tests guard the declarations. Claude Code, Codex, OpenCode, and Goose have mirrors in that package; KAS remains an external projection in `acp/kas_agents.py`, and kiro-cli remains native. The relocation plan is therefore incomplete. Verified at `80bd0a81f`. |
| [rfc-mcp-lifecycle-event-log.md](rfc-mcp-lifecycle-event-log.md) | `draft` | **Blocked on a base.** The lifecycle-event package this RFC builds on (landed by [#3808](https://github.com/kirodotdev/KiroCrew/pull/3808)) is deleted: it had no emitter, no reader and no on-disk directory, and every one of its kinds names a fact the per-unit crew log owns as a `type`. So this RFC's three precedents — the ordering model, the writer, the failure posture — have no package to be precedents for; adopting them means restating them against `kiro_crew.crew_log`, where ordering is already answered per unit. The #7366 capture points it names as emit sites are on an open PR, not on main |
| [rfc-app-session-controls.md](rfc-app-session-controls.md) | `implemented` | Shipped in [#7573](https://github.com/kirodotdev/KiroCrew/pull/7573). Verified at `80bd0a81f`: `apps/manifest.py` parses and validates `SessionControlContribution`; `website/src/hooks/useSessionControls.ts` resolves and caps controls; `ChatPage.tsx` mounts `SessionControlHost`; and `statusPath` polling is implemented. |
| [rfc-transcript-section-markers.md](rfc-transcript-section-markers.md) | `draft` | Nothing. No `section_marker` role exists; every collapse affordance is intra-turn (`CollapsibleToolGroup`, `TurnBlock`, `ToolCallLine`) and nothing collapses rows above a point |
| [rfc-crew-agent-sdk-boundary.md](rfc-crew-agent-sdk-boundary.md) | `partial` | Both named deliverables are on main: the `agent_sdk` package (`drivers/acp.py`, `backend_identity.py`, `provider_identity.py`, `backend_install.py`, `native_commands.py`) and the shrink-only import ratchet `scripts/check_agent_sdk_boundary.py`. What remains is the cleanup the boundary was declared to enable: `providers/base.py` still aliases `AcpEvent` as the "provider-agnostic" event type, direct `kiro_crew.acp` import edges remain outside `acp/` and `providers/`, and `acp/worker_pool.py` still imports back from `session_pid.py` behind a cycle guard |
| [rfc-global-workflow-library.md](rfc-global-workflow-library.md) | `partial` | The global definition library is on main: `workflows/library.py` persists definitions under `<KIROCREW_HOME>/workflow_library/`, the `workflow_library_list` MCP tool reads it, and Agent Capabilities > Workflows carries the **Workflow library** and **Runs** views. [`../system-specs/modules/workflows.md`](../system-specs/modules/workflows.md) owns the shipped shape, including the import layering that keeps `library` below `service`. Slug persistence and promotion ship; a chat `/workflow <name>` invocation resolving a saved slug is not on main |
| [rfc-durable-run-coordinator.md](rfc-durable-run-coordinator.md) | `superseded` | Replaced by [rfc-overload-resilience.md](rfc-overload-resilience.md) (its §13 decision Q1). The replacement is now on main as `$KIROCREW_HOME/tasks/tasks.db` and `src/kiro_crew/taskq/`, including legacy import in `taskq.migrate.import_legacy`; the `RunCoordinator` plan remains an obsolete historical execution plan. Verified at `80bd0a81f`. |
| [rfc-issue-radar-crews.md](rfc-issue-radar-crews.md) | `partial` | The crew ledger and its agent surface are on main. Verified at `80bd0a81f`: `apps/builtins/issue_radar/backend/crew_routes.py` serves the crew API behind a per-slot identity gate and a crew-conflict guard, and `mcp_tools/apps.py` registers `issue_radar_crew_read` / `issue_radar_crew_record` as the only credentialed read and write paths. `crew_brief.md` and `crew_ledger_spec.md` beside the backend are the companion specs for that surface. This audit did not re-classify every later phase of the RFC, so check the document before treating the whole design as shipped |
| [rfc-orchestrator-chat-sessions.md](rfc-orchestrator-chat-sessions.md) | `superseded` | Crew Mode shipped in [#1295](https://github.com/kirodotdev/KiroCrew/pull/1295), diverging from the RFC in three places (no snapshot-generation CAS, no `release` decision action, immediate per-result delivery instead of burst coalescing), and has since been retired in favour of the Crew Members page. The record of what shipped and how existing sessions are treated is `system-specs/modules/crew-mode.md` § "Retired: Crew Mode" |
| [rfc-channel-plugin-architecture.md](rfc-channel-plugin-architecture.md) | `partial` | The shared turn pipeline and builtin `ChannelDescriptor` registry shipped; ten builtin transports are now registered, and the registry drives governance, boot and readiness. Manifest-loaded channel extensions and the frontend registry remain incomplete. Its §9 address rule is contested by [rfc-session-address-model.md](rfc-session-address-model.md), which proposes an attribute store where §9 chose a canonical grammar — audit the two rows together |
| [rfc-session-address-model.md](rfc-session-address-model.md) | `partial` | Phase 0 shipped ([#1366](https://github.com/kirodotdev/KiroCrew/pull/1366) plus four follow-ups): a chat-app conversation opened in a dashboard tab joins that conversation instead of being copied into a second one, the 30-second reconciler is deleted, and `has_dashboard_surface` replaced the name-prefix capability tests with observed state — for one surface and one boolean. Phases 1–4 unstarted, verified at `707b8aef2`: the identity still encodes origin, at least **7** overlapping classifiers and dozens of conversion sites remain, `session_map.json` has no version envelope, capability is still one boolean that a `dashboard:` prefix short-circuits regardless of which surface the turn arrived from, and an unbound channel tab still starts a second session. Revision 3 adds the surface model (dashboard as permanent host, channels as attachments, per-turn ingress) |
| [rfc-local-notification-bus.md](rfc-local-notification-bus.md) | `partial` | Phases 1/3/4 complete. Phase 2 wired but has no producer; Phase 5 shipped 2 of 3 |
| [rfc-federated-app-platform.md](rfc-federated-app-platform.md) | `partial` | Phase 1 substantially shipped, Phase 3 half-built. Phase 2, Phase 1's removals, Phase 4, Phase 5 unstarted |
| [rfc-workspace-config-evolution.md](rfc-workspace-config-evolution.md) | `partial` | Phases 1–2 shipped. Phase 3's vector isolation was **reversed** on purpose; Phase 4 unstarted |
| [rfc-resumable-subagent-sessions.md](rfc-resumable-subagent-sessions.md) | `partial` | Phase 0 ran and **redirected the design**: continuable conversations shipped instead of the record-store ladder |
| [rfc-i18n-measurement.md](rfc-i18n-measurement.md) | `partial` | Overflow gate shipped, `localeCompare` migration partial. All three *measurement* proposals unstarted |
| [rfc-appstore-official-registry.md](rfc-appstore-official-registry.md) | `partial` | The official fetch and editorial-driven Discover are live (`apps/official_catalog.py`, `apps/official_editorial.py`); signature verification and tombstone resolution are deliberately absent and fail closed. **§4 diverged** — four of its decisions about categories were reversed in the sibling `KiroCrewApps` repo; the note at its head says which |
| [rfc-notification-bridge.md](rfc-notification-bridge.md) | `accepted` | Nothing — zero implementation code |
| [rfc-tips-kit.md](rfc-tips-kit.md) | `draft` | Nothing. T1 was built and **retracted** ([#775](https://github.com/kirodotdev/KiroCrew/pull/775)); the design section needs revising first |
| [rfc-update-architecture.md](rfc-update-architecture.md) | `partial` | The capability contract and the managed-venv engine are on main, not zero phases. Verified at `80bd0a81f`: `platform/update_capability.py` declares `UpdateCapability` with `derive_capability`, per-channel narrowing and the checkout/worktree refusals (`running_from_checkout`, `is_git_worktree`); `platform/wheel_engine.py` carries `ManagedVenvLayout`, `managed_venv_layout` and `running_from_managed_venv`; `dashboard/handlers/updates.py` serves the surface and `cli.py` carries the host-local `approve` action. No system spec owns this behaviour yet, so this RFC remains the only detailed description of it |
| [rfc-app-sandbox-isolation.md](rfc-app-sandbox-isolation.md) | `partial` | App-token HTTP scope is deny-by-default (`dashboard/token_auth.py`: own namespace plus manifest `permissions.api`), and slot/folder/tag/WebSocket visibility is app-scoped. Full process isolation, resource ownership across cron/subagents/MCP, and quota enforcement remain incomplete. Verified at `80bd0a81f`. |
| [rfc-issue-radar-dispatch.md](rfc-issue-radar-dispatch.md) | `draft` | Nothing. Issue Radar has Investigate and Review; no verb produces work, and issues carry no link to the change that resolves them |
| [rfc-perpetual-agent.md](rfc-perpetual-agent.md) | `draft` | Nothing. Verified at `9ac3716a`: no schedule kind self-reschedules, and `binding_key_for` has no `cron:` branch |
| [rfc-token-efficient-monitors.md](rfc-token-efficient-monitors.md) | `implemented` | Shipped by the eight-PR stack ([#5180](https://github.com/kirodotdev/KiroCrew/pull/5180) through [#5305](https://github.com/kirodotdev/KiroCrew/pull/5305)). Verified at `80bd0a81f`: `monitoring/controller.py` carries `MonitorController` — typed probes that request a turn only for an accepted wake claim — with its injectable per-provider mapping, and `mcp_tools/control.py` carries the session-bound `monitor_watch` / `monitor_inspect` / `monitor_stop` tools. The review-readiness adapters cover GitHub, GitLab, Azure DevOps and Bitbucket |
| [rfc-consolidated-monitor.md](rfc-consolidated-monitor.md) | `draft` | Nothing of the merge. Verified at `ef38f4cbe`: `probes/__init__.py` `build` maps exactly one kind (`gh-pr`); `monitoring/github_pull_request.py` still reduces a pull request to one fingerprint with no named-entry list, and `decide_monitor` compares that single `fingerprint` against `last_wake_fingerprint`; no coalescing pre-stage reaches the controller, the window exists only in `irq.py`; and `api_autonudge_get` no longer nulls a structured monitor out -- both legacy read routes report an armed monitor's presence, cadence, liveness and state while withholding what it watches, so the goal popover receives the record and only the rendering is outstanding |
| [rfc-tailnet-dashboard-access.md](rfc-tailnet-dashboard-access.md) | `partial` | Phase 1 landed ([#1761](https://github.com/kirodotdev/KiroCrew/pull/1761), `f8afcff7`) — reports the pin's real scope, does not fix it. Phases 2–4 unstarted; the pin repair is tracked as [#1762](https://github.com/kirodotdev/KiroCrew/issues/1762) |
| [rfc-pluggable-model-providers.md](rfc-pluggable-model-providers.md) | `draft` | Nothing, by design. `agent.provider` is still fixed to `acp` and [oss-fork-boundaries](../system-specs/oss-fork-boundaries.md) lists "Other providers" under *Never re-add*. This document **recommends** supporting provider choice and asks the maintainers to amend that rule; it proposes no design, and an exploratory implementation is shelved pending the answer ([#1693](https://github.com/kirodotdev/KiroCrew/issues/1693)) |
| [rfc-s3-backup.md](rfc-s3-backup.md) | `draft` | Nothing. Verified at `f4d3327a7`: `VALID_COMPONENTS` carries no session component and no code path writes crew state to a remote store |
| [rfc-navigation-placement-seam.md](rfc-navigation-placement-seam.md) | `draft` | Nothing. Verified at `2a665e735`: `UISidebar` ships in the manifest and no frontend code reads `ui.sidebar`; `appNavTarget` still resolves `pages[0]` only, and `registerBuiltinSurface` is not one of the nine edition seams |
| [rfc-append-only-session-transcript.md](rfc-append-only-session-transcript.md) | `draft` | Nothing. Verified at `2a665e735`: `_save_slot_to_history` still re-serializes the whole in-memory window on every flush, and `rewrite_session` / `sliding_window` still have no production caller |
| [version-compliance-framework.md](version-compliance-framework.md) | `draft` | Nothing. Framework doc, not an RFC; premise is pre-fork and stale |
| [rfc-everything-is-an-app.md](rfc-everything-is-an-app.md) | `partial` | Phase 0 landed: [`../architecture/overview.md`](../architecture/overview.md#the-app-boundary) carries the boundary section, and `TENETS.md` carries **Everything is an app** as tenet 8. Verified at `80bd0a81f`. The eleven declared-but-unread manifest fields this document inventories are still declared and still unread, so the design as a whole is not shipped |
| [rfc-amend-tenets-everything-is-an-app.md](rfc-amend-tenets-everything-is-an-app.md) | `implemented` | Shipped in [#4444](https://github.com/kirodotdev/KiroCrew/pull/4444). `TENETS.md` carries **Everything is an app** as tenet 8. Verified at `80bd0a81f`. |
| [rfc-chat-core-extraction.md](rfc-chat-core-extraction.md) | `partial` | P1 shipped: `app-sdk/messageRenderers.tsx` is the role registry with a parity contract test ([#5128](https://github.com/kirodotdev/KiroCrew/pull/5128)). P2 shipped for one surface: `website/src/chat-core/transport/sendTurn.ts` exists and `components/ChatPane.tsx` is its only production caller ([#5909](https://github.com/kirodotdev/KiroCrew/pull/5909)); ChatEmbed, SideChat and ChatPage still hand-roll their POST on main (open PRs [#8599](https://github.com/kirodotdev/KiroCrew/pull/8599), [#8655](https://github.com/kirodotdev/KiroCrew/pull/8655), [#8689](https://github.com/kirodotdev/KiroCrew/pull/8689)). P3 shipped for SideChat (`pages/chat/SideChat.tsx` mounts `ChatInput`); `app-sdk/ChatEmbed.tsx` still renders a bare `<input>` on main. Verified at `8ed028b0b`: `ChatPage.tsx` carries 29 `role ===` branches (P5 unstarted) |
| [rfc-crew-projects.md](rfc-crew-projects.md) | `draft` | Nothing of the bundle. Re-audited 2026-09-18 at `04f8f9139c`: no project manifest format, registry or Project store exists on main; `slot.project` is a bare directory path whose `.kiro/skills`, project-local agents and `.kiro/steering` are discovered per session. PR #7181 is closed, superseded by the Revision 2 split: PR A (thin Project: fenced registry, synced workspace, session binding, registry-assigned id) is open as PR #11678; `memory.mode` (PR B) and MCP by name (PR C) each add their manifest key when they land |
| [rfc-tool-derived-diff-cards.md](rfc-tool-derived-diff-cards.md) | `in-progress` | Ships with [#5012](https://github.com/kirodotdev/KiroCrew/pull/5012): dashboard diff-card/summary promotion + runtime-selected prompt rule. The messaging `OutputEvent` extension (§3.3) is unstarted |
| [rfc-agentcore-identity-gateway.md](rfc-agentcore-identity-gateway.md) | `partial` | Phase 1 is on main: `platform/interfaces.py` defines `AgentIdentityProvider`, `defaults.py` supplies the disabled public implementation, `bootstrap.py` wires it, `platform/agentcore_schema.py` validates policy fields, and governance carries default-off `capabilities.agentcore`. No AWS adapter, IAM document helper, Gateway session injection, consent flow, or Settings surface exists at `80bd0a81f`. |
| [rfc-playwright-cli-migration.md](rfc-playwright-cli-migration.md) | `partial` | `playwright-cli` is the live backend: `browser_cli/` carries install, launch, snapshot retention and OS deps, the capability model is in `config/sections.py`, and the write-path gates are in `platform/governance.py`. [`../system-specs/modules/browser.md`](../system-specs/modules/browser.md) owns the landed contract. **Phase 3's removal of `browser/` did not happen** — `browser/command_bus.py` and `browser/__init__.py` remain, `dashboard/handlers/messaging.py` imports the command bus, and `test_browser_command_bus.py` pins it |
| [rfc-pipeline-conductor.md](rfc-pipeline-conductor.md) | `partial` | M0 shipped: `kirocrew-pipeline-conductor` is registered in `subagent.py`'s `UNADVERTISED_AGENTS`, and the `pipeline-conductor` builtin skill carries the operating procedure, its bundled scripts and the `conductor-status/v1` schema. Unbuilt: M1's `PipelineSpec` and SQLite event store (`PipelineSpec` has no code hit outside the RFC), M2's adjudication and SLA machinery, M3's baking, compensation and per-repo objects; five decisions remain open. [`system-specs/modules/pipeline-conductor.md`](../system-specs/modules/pipeline-conductor.md) owns the shipped half |
| [rfc-prepare-pr-portability.md](rfc-prepare-pr-portability.md) | `implemented` | Shipped. `resolve_profile.py` in the `prepare-pr` skill's `scripts/` implements the four-step resolution order (`.prepare-pr.toml` → Kiro Crew markers → stack auto-detect → generic fallback) and parses the `[project]` / `[setup].commands` / `[gates].commands` schema through `tomllib` with a `tomli` fallback |
| [rfc-app-sdk-durable-jobs-and-view-state.md](rfc-app-sdk-durable-jobs-and-view-state.md) | `partial` | `JobSDK` is wired into app contexts and `useAppViewState` ships with AWS Control as a consumer. A frontend `useAppJob` contract is still absent, so the durable-job surface is incomplete. Verified at `80bd0a81f`: `apps/job_sdk.py`, `apps/context.py`, `website/src/app-sdk/viewState.ts`, and `website/src/apps/aws-control/DrivePage.tsx`. |
| [rfc-security-conductor.md](rfc-security-conductor.md) | `partial` | M0 and M1 are on main. M0: `kirocrew-security-conductor` is registered in `subagent.py`'s `UNADVERTISED_AGENTS` with its installer `_install_security_conductor_agent` in `agent.py` ([#9273](https://github.com/kirodotdev/KiroCrew/pull/9273)), and the `security-conductor` builtin skill plus the first `rules-of-engagement.json` are on main ([#9271](https://github.com/kirodotdev/KiroCrew/pull/9271)), whose merge is the human review of the rules of engagement that M0 required. M1 is fully shipped: the SQLite findings ledger `scripts/ledger.py` ([#9270](https://github.com/kirodotdev/KiroCrew/pull/9270), test guard corrected in [#9362](https://github.com/kirodotdev/KiroCrew/pull/9362)), the three evaluator scripts `scope_check.py` / `finding_entry.py` / `verify_finding.py` ([#9332](https://github.com/kirodotdev/KiroCrew/pull/9332)), the `golden_paths` table with its committed `golden-paths.json` export and `verify_fix.py` ([#9500](https://github.com/kirodotdev/KiroCrew/pull/9500)), and the three-platform denial differential `scripts/deny_diff.py` ([#9499](https://github.com/kirodotdev/KiroCrew/pull/9499)) — with the wiring that makes them one system (the fixer lane accepted on `verify_fix.py` exit 0 as well as green checks, the cross-platform `forbidden` row, and one corpus behind both gates) landing in [#9813](https://github.com/kirodotdev/KiroCrew/pull/9813). Unstarted: the M2 pilot round with its retrospective lane and its `flow` / `cron` harness, and the M3 fixer lane; five decisions remain open |

An `implemented` document is one whose design landed whole and whose row names
the spec that owns the shipped behaviour. A `superseded` row names its replacement — the
spec that records the retirement of a shipped feature, or the RFC that absorbed
a design before it was built.

## Implementation plans

[plans/](plans/README.md) holds the dated, task-by-task execution plans derived
from documents in this directory. A plan is the *how* and belongs beside the RFC
it executes; the RFC stays the *what* and the *why*. A plan's checkboxes are its
own progress record and are not a status source — read the RFC's `status` for
that, and read the plan's own head note for whether it is live or dormant.

## Front matter

Every document carries YAML front matter as the machine-readable record. The
prose header below it stays human-readable and carries the *why*; front matter
carries the *what*.

```yaml
---
title: Channel Plugin Architecture — shared runtime, channels as app extension points
status: partial            # see vocabulary below
author: zezhexu
created: 2026-07-28
last-audited: 2026-08-03   # when status was last verified against code
audited-at: 0ab6ed48       # the commit it was verified against
doc-pr: 689                # the PR that merged this document
implementation-prs: [777, 1019, 1234]
tracking-issues: []
supersedes: []
superseded-by: []
---
```

Optional keys: `kind: framework` for docs that are policy rather than a
reviewable change to a named component, and `revision:` where a document is
versioned across review rounds.

`last-audited` and `audited-at` exist because a bare `status: partial` rots
silently. If those two fields are far behind main, distrust the status.

### Status vocabulary

| Status | Meaning |
|---|---|
| `draft` | Proposed. Nothing built. |
| `accepted` | Design agreed and locked. Nothing built yet. |
| `in-progress` | Implementation is live in an open PR or an active branch. |
| `partial` | Some phases are on main; the rest are open. The prose status line names which. |
| `implemented` | Every phase is verifiably on main. |
| `superseded` | Replaced. `superseded-by` names the replacement. |

The First Principles review lane reads this field, from the **base** commit,
as the record of a product-shape decision: a change to a default, to what a
first-class loop, monitor, agent, skill or command does by default, or a
removal or replacement of a user-facing capability, must trace to a document
here whose status is one of `accepted`, `in-progress`, `partial` or `implemented`
(a closed set: `draft`, `superseded` and any undefined value are not a decision,
and a `partial` plan main deliberately diverged from does not cover the diverged
shape) -- or to a maintainer's
`/ai-review override first-principles <head>` on the PR. A PR that flips this
field, or ships the RFC beside the change, has proposed a decision, not
recorded one; see `docs/ci/ci-and-reviews.md`.

`partial` is the most common status and the most dangerous one to read
carelessly — several documents here describe a plan that main only partly
follows, and two describe a plan main **deliberately diverged from**.

## Reading a `partial` or divergent RFC

Three failure modes are live in this directory. Each document's prose status line
calls out its own, but the patterns are worth knowing before you trust any of them:

1. **The plan was overtaken.** `rfc-resumable-subagent-sessions.md` had its
   Phase 0 probe return a negative verdict, which redirected the whole design —
   what shipped (continuable conversations) is not what the phases below it
   describe. `rfc-workspace-config-evolution.md` had its Phase 3 vector-store
   isolation affirmatively reversed by a later commit. Neither document was
   revised afterwards. `rfc-appstore-official-registry.md` is the same pattern
   caught late but *revised*: four of §4's decisions about categories were
   reversed as R1 shipped, and the section now opens with a note saying which,
   so the reasoning survives as a record without still reading as the contract.
2. **The credit is not the RFC's.** `rfc-i18n-measurement.md` shows `partial`,
   but the proposals that shipped were already in flight under a separate
   program, one of them merging 18 hours before the document did.
3. **A dependency claim is overstated.** `rfc-notification-bridge.md` asserts the
   bus RFC's phases "all shipped". The phases the bridge actually needs are real;
   the blanket claim is not.

When a document and the code disagree, the code wins and the document is a bug.
Fix it in the same PR that discovers the drift.

## Writing a new RFC

[GOVERNANCE.md](../../GOVERNANCE.md) covers who decides whether an RFC is
accepted, and the scope test for when a change needs one at all.

- File as `rfc-<topic>.md`, kebab-case. Framework or policy docs that propose no
  reviewable change to a named component drop the prefix and set `kind: framework`.
- Open with front matter, then an H1 `# RFC: <Title>`, then the prose header.
- Write in English.
- Structure that has worked here: Summary → Motivation (current state, problems)
  → Goals → Non-goals → Design → Migration plan (phased, each phase PR-sized with
  **exit criteria**) → Backward compatibility → Security considerations →
  Alternatives considered → Open questions.
- Phases earn their keep by being independently shippable and independently
  abandonable. State exit criteria as assertions someone can test, and mark any
  phase whose entry depends on an unanswered open question as blocked on it.
- **Verify before asserting.** Claims of the form "X does not exist" or "Y is
  unused" are the ones that most often turn out wrong. Grep for callers, not just
  definitions — a defined-but-uncalled symbol means the behavior does not happen,
  which is a different (and usually more interesting) finding than absence.
  Quote `file:line`. Name the commit you measured at, as
  `rfc-i18n-measurement.md` does.
- A probe phase that exists to answer a question must write its verdict down
  somewhere durable and the RFC must be updated to point at it. PR #1023 recorded
  its Phase 0 verdict in the PR description; the RFC still does not reference it,
  which is why that document now needs a reader's warning.

## Keeping this honest

When you land an implementation PR for anything here, update the document's
`status`, `implementation-prs`, `last-audited` and `audited-at` in the same PR,
and re-audit the whole directory whenever the table above starts feeling
plausible rather than checked. The audit is cheap: for each document, extract its
named deliverables, grep for each one's definition and callers, and check the PR
history for the phase that claims to have landed it.

---
title: Hybrid local/remote execution — routing, memory authority and fallback
status: draft
author: koenvincent
created: 2026-10-17
last-audited: 2026-10-24
audited-at: 69e016c117
doc-pr: null
implementation-prs: []
tracking-issues: [3278]
supersedes: []
superseded-by: []
---

# RFC: Hybrid local/remote execution — routing, memory authority and fallback

Status: draft. Acceptance requested from a maintainer; the status flips to `accepted` when one records it here. Every "on main" claim below was read at `69e016c117`, definitions and callers.

[#3278](https://github.com/kirodotdev/KiroCrew/issues/3278) asks for a laptop gateway that offloads heavy work to a persistent remote one. Its triage comment of 2026-09-25 names three things that still need a maintainer ruling: routing heavy work out of a local session, remote-authoritative memory, and falling back to local. The issue is parked, and the 2026-10-08 comment says new concepts go through this directory. This document is that proposal, cut down to those three rulings. It does not redesign anything that is already built or in review (§2).

## 1. Summary

| # | Decision | Options | Recommendation |
|---|---|---|---|
| 1 | How work leaves a local session | A explicit placement; B operator-set overflow policy; C automatic heuristics | **A now, B later as opt-in, never C by default** |
| 2 | Who owns memory when work runs on a crew | A the crew owns its own; B crew-authoritative with a hub cache; C explicit per-task brief | **A now. C only if asked for. B is not proposed here** |
| 3 | What happens when the crew is unreachable | A refuse; B refuse, with an explicit per-call local retry; C silent fallback | **A now, B only for a placement that never started. Never C** |

Decision 2 gates the rest: a routing or fallback rule (1 B, 3 B) is only safe to write once it is known whose memory the routed work reads and writes. Decide 2 first. Explicit placement (1 A) does not wait for that ruling: it extends Decision 2 A, the status quo for crew sessions, to remote spawns, so accepting #12821 accepts Decision 2 A for spawns (section 6, Phase 1).

## 2. What exists, so none of it is redesigned

| Piece | State | Evidence |
|---|---|---|
| New chat on crew: a session created on a connected crew, opened as a window onto the crew's own slot, nothing stored locally | On main, behind the **Chat on a crew** Feature Preview (`PREVIEW_REMOTE_CREW_CHAT`). In release 0.9.0 (`CHANGELOG.md`, *Remote crews as live windows*) | `createRemoteChatMutation` in `website/src/pages/chat-sidebar/create.ts` posts `api/chat/slots` through `crewPeerPost` (`website/src/api/client/instances.ts`) and sends the default `memory_mode`; called from `SidebarHeader.tsx`. Window: `website/src/pages/chat/crew-window/CrewChatWindow.tsx` |
| Agent and approval mode of a crew session set from the window | On main, [#18962](https://github.com/kirodotdev/KiroCrew/pull/18962), merged 2026-10-12 | `crew-window/crewWindowAgentMode.tsx` |
| One Sessions list across crews, grouped per machine | On main, same preview | `useRemoteCrewChat` in `website/src/hooks/usePreviewFlag.ts`, called from `sessionSources.ts`; `read_peer_slots` in `src/kiro_crew/dashboard/handlers_instances.py` |
| Send a copy of a session to a crew | On main | `api_instances_send_session` in `handlers_instances.py`, route in `dashboard/routes/connections.py`; `docs/system-specs/modules/instances.md` §14 |
| The older turn relay and "bound local slot" model | Gone: `rfc-remote-crew-sidebar` wave 4 ([#17642](https://github.com/kirodotdev/KiroCrew/pull/17642), [#17662](https://github.com/kirodotdev/KiroCrew/pull/17662)). `remote_already_bound` and `remote_not_connected` have no hit under `src/` | `git grep` at the sha above. The 2026-09-25 triage cites both codes at an earlier sha; they no longer apply |
| Remote subagent executor: `spawn_run(executor="remote")` | **Open**, [#12821](https://github.com/kirodotdev/KiroCrew/pull/12821). Not on main: `instances.remote_subagents`, `remote_spawn` and `spawn_enforces` have no hit; `src/kiro_crew/dashboard/remote_subagents.py` does not exist. Its review state lives on the PR | `git grep` at the sha above |
| Fetch a crew's session back to the hub | **Open**, [#19440](https://github.com/kirodotdev/KiroCrew/pull/19440). `fetch-session` has no hit on main | `gh pr view` |
| Send a folder's sessions to a crew | **Open**, [#19859](https://github.com/kirodotdev/KiroCrew/pull/19859). `SendFolderToInstanceSubmenu` has no hit on main | `gh pr view` |
| File hand-off to and from a crew | **Open RFC**, [#19460](https://github.com/kirodotdev/KiroCrew/pull/19460), `rfc-remote-crew-file-handoff` | `gh pr view` |
| Outbound transport (no SSH or SSM) | Merged RFC, [#13031](https://github.com/kirodotdev/KiroCrew/pull/13031); implementation is the maintainers' own, tracked on [#19092](https://github.com/kirodotdev/KiroCrew/issues/19092). This RFC assumes whatever transport that yields and adds none | `rfc-outbound-instance-transport.md` |
| Default crew per folder; default destination crew for *Send a copy to* | **Open issues** [#21546](https://github.com/kirodotdev/KiroCrew/issues/21546), [#21547](https://github.com/kirodotdev/KiroCrew/issues/21547). `chat_folders.py` carries `default_agent` and no crew field | `git grep` |
| Local sub-agent cap | Not sized from host memory any more. `compute_max_subagents` in `src/kiro_crew/subagent_parts/host_memory.py` returns `agent.subagent_auto_max` (default 32); memory is bounded per start by the spawn floor `agent.spawn_min_memory_gb`, enforced by the admission gate in `src/kiro_crew/subagent_manager/admission/`. A start that would breach the floor queues | The earlier triage described a memory-sized cap |
| Cross-instance memory | None. `docs/system-specs/modules/instances.md` §14.3: memory "is a per-instance scope ... The peer keeps its own" | `git grep -i "memory.*(sync|replicat)"` over `src/kiro_crew/instances/`, `handlers_instances.py` and `session_transfer.py` is empty |
| Dispatch to another instance from `subagent.py`, `subagent_manager/` or `workflows/` | None on main (no `instances` or `proxy_request` reference under those paths) | `git grep -c` |

Not decided here, and not required by these three rulings: which topology backs a remote (self-hosted crew, Fargate fan-out per `rfc-remote-instance-on-fargate`, an A2A endpoint per the open [#10162](https://github.com/kirodotdev/KiroCrew/pull/10162), or Cloud Sessions). Nothing in `src/` uses `--cloud` or `--resume-id`, and whether `kiro-cli acp` can attach a cloud session is not answerable from this repository. Each decision below is written so it holds for any of them: the unit is "a connected crew", as in the instances registry today.

## 3. Decision 1 — routing heavy work out of a local session

Two things can leave: a whole session, and a subagent a session spawns. The first is solved by hand today (New chat on crew; send a copy). The open question is the second, and the rule for *when* anything leaves.

**Options**

- **A. Explicit placement.** The caller says where: `spawn_run(executor="remote", instance_id=...)` as in [#12821](https://github.com/kirodotdev/KiroCrew/pull/12821), off unless the operator sets `instances.remote_subagents`, refusable per agent by policy. A session is placed by choosing New chat on crew. Nothing moves on its own. *Cost:* the person or the conductor must decide. *Risk:* lowest; the project snapshot and task text leave the machine only on a named call.
- **B. Operator-set overflow policy.** On top of A, an opt-in rule such as "when a local start would queue on the spawn floor, place it on crew X instead". The signal exists on main (the admission gate already computes that a start would breach `agent.spawn_min_memory_gb`). *Cost:* a new config surface and a new refusal path. *Risk:* medium; data leaves the machine without a per-call act, so it needs an allow-list of agents and crews and a visible receipt of where each run went.
- **C. Automatic heuristics by default.** As the issue body suggests: route on sub-agent count or estimated duration. *Risk:* highest. There is no trustworthy source for "estimated duration", and silently sending a project snapshot to another host is a privacy decision, not a tuning knob.

**Recommendation: A first, B as a later opt-in, C not proposed.** A is already written and reviewed (#12821). B reuses a signal main already has and adds one switch, so it can follow without reopening A. C should wait until A and B have produced evidence of which rule people actually want.

## 4. Decision 2 — memory authority when work runs on a crew

This is the one decision that runs against a written rule (`instances.md` §14.3), so it is the one that needs a maintainer rather than an author.

**Options**

- **A. The crew owns its own memory (status quo, made explicit).** Remote work reads and writes the *crew's* memory scope; nothing is copied either way. The hub sends only the `memory_mode` it already sends on New chat on crew (and #12821 adds a tighten-only `memory_mode` on spawn), so remote work never runs in a looser mode than the parent. The user guide and the placement receipt say so. *Cost:* a laptop's lessons and preferences do not reach a remote worker. *Risk:* none new.
- **B. Crew-authoritative memory with a hub cache.** The issue's original model: memory and knowledge live on one crew, the hub reads through a cache and invalidates it. *Cost:* new cross-instance read and write code, a conflict story, an offline story, and a reversal of §14.3. *Risk:* high; copying memory across hosts is what §14.3 calls "the risky, hard-to-undo part". It also overlaps the open requests [#10035](https://github.com/kirodotdev/KiroCrew/issues/10035) and [#12942](https://github.com/kirodotdev/KiroCrew/issues/12942), which ask for shared memory from other angles and are not decided.
- **C. Explicit per-task brief.** A placement may carry a bounded, caller-chosen set of lessons or knowledge excerpts as part of the task text, and a remote run's findings come back as its result, not as a memory write. No sync, no authority change. *Cost:* the caller picks the excerpts. *Risk:* low; it moves only what the caller names, through the redaction already applied to peer text.

**Recommendation: A, with C available later if people ask for it. B is not proposed in this RFC.** A needs no code beyond documenting it. If maintainers want B, it should be its own RFC that also settles #10035 and #12942, because it changes the memory model for every instance, not only for hybrid execution. This RFC works fully under A.

## 5. Decision 3 — falling back to local

**Options**

- **A. Refuse, with a typed error.** Work addressed to a crew that is not connected does not start; nothing runs locally. This is what main does for sessions: `CrewChatWindow.tsx` computes `connected` from the tunnel state and the version match, shows the offline notice, and renders the composer inside `<fieldset disabled={!connected}>`; the proxy answers a down tunnel with `proxy_peer_not_connected` (raised in `src/kiro_crew/instances/ssh_tunnel_manager_peer.py`, listed in `UNREACHABLE_CODES` in the same window file). #12821 does the same for a spawn: an unconnected selected crew is refused, and a run whose peer stays unreachable for 30 minutes ends with an error.
- **B. Refuse, with an explicit per-call local retry.** The refusal names the failure and the caller may repeat the call with `executor="local"`. Only for a placement that **never started** on the peer. A run that began remotely is never re-run locally, because side effects may already have happened there.
- **C. Silent or automatic fallback.** The issue's "fall back to local-only with a notification". *Risk:* highest. A placement may exist *because* the work must not run here (a memory mode, a policy, a resource limit), and a session's home is its crew: a local re-run would use a different agent roster, model and memory scope (Decision 2 A) and split the transcript.

**Recommendation: A for sessions and for spawns now; B for spawns later, only for placements that never started. C is not proposed.** This keeps today's behaviour, so it reverses nothing that shipped.

## 6. Phased delivery

Each phase is independently shippable and abandonable. Phase 1 is an existing PR, so accepting this RFC does not block it.

| Phase | What | Depends on | Exit criteria |
|---|---|---|---|
| 1 | [#12821](https://github.com/kirodotdev/KiroCrew/pull/12821) as it stands: explicit remote placement, refuse when no crew is connected | Decision 2 A for spawns, which accepting #12821 itself records: it extends the status quo for crew sessions (the crew's own memory) to remote spawns. It also implements Decision 1 A and Decision 3 A, which match how crew chats already behave on main (explicit placement, refusal when the crew is down) | Merged; `spawn_run` default stays `executor="local"` and every local call behaves as before |
| 2 | Documentation of Decision 2 A: `instances.md` §14.3 and the user guide state that remote work uses the crew's memory and that `memory_mode` is sent | Decision 2 accepted | A reader of `remote-crew.md` can say which memory a crew session and a remote subagent use |
| 3 | Optional: Decision 1 B (overflow policy) and Decision 3 B (explicit local retry for never-started placements) | Phase 1 merged; Decisions 1 and 3 accepted | An opt-in switch, default off; a run's receipt names where it ran; a started remote run is never re-run locally (test) |
| — | Not scheduled: Decision 2 B (authoritative memory) | A separate RFC | — |

Related work that continues on its own track, unchanged by this document: #19440, #19859, #19460, #21546, #21547 and the maintainers' outbound transport.

## 7. Security and compatibility

- Nothing here changes a default. Phase 1 is off unless an operator sets `instances.remote_subagents`; phases 2 and 3 add documentation and an opt-in.
- Data leaving the machine (project snapshot, task text) is always the result of a named call or an operator-set rule, never of a heuristic (Decision 1 C and Decision 3 C are the options rejected for that reason).
- No new transport and no new authentication: peer calls use the existing instances proxy.

## 8. Open questions for the maintainer

1. Is Decision 2 A acceptable as the position for hybrid execution, with B left to its own RFC?
2. Does a remote subagent belong behind the instances registry as #12821 has it, or behind the A2A provider of #10162? This RFC holds either way; the body of #12821 leaves the question to a maintainer.
3. Which phase, if any, should wait for the outbound transport (#19092)?

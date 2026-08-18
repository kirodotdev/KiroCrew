---
title: Session Address Model — an opaque conversation identity with an attribute store
status: partial
author: nrb
created: 2026-08-17
last-audited: 2026-09-11
audited-at: 707b8aef2
doc-pr: 4077
revision: 3
implementation-prs: [1366, 1455, 1480, 1539, 1921]
tracking-issues: []
supersedes: []
superseded-by: []
---
# RFC: Session Address Model — an opaque conversation identity with an attribute store

> **Current behaviour: see [`../system-specs/modules/session.md`](../system-specs/modules/session.md)**
> for the mirror-resolver and key-addressing behaviour that ships. This
> document owns the §9 address rule inherited from
> [`rfc-channel-plugin-architecture.md`](rfc-channel-plugin-architecture.md);
> Phase 0 below is on main and Phases 1–4 are a live proposal.

- Status: **partial.** Phase 0 is on main — PR #1366 and follow-ups #1455, #1480, #1539, #1921 (August 2026) moved a conversation's capabilities off its origin and onto observed surface state, for one surface and one boolean. Phases 1–4 are not started. Phase 4 is additionally blocked on a maintainer ruling (§10.1).
- Revision 3 adds the surface model in §5.3 — the dashboard as permanent host, channels as attachments, and per-turn ingress as the input that selects a capability set — and records the shipped work as Phase 0 rather than as background. Revision 2 rewrote revision 1, which described Phase 0 and stopped there. Nothing here claims the end state works today; §2.3 records exactly where it does not.
- Author: nrb
- Created: 2026-08-17 · Revised: 2026-09-11
- Related: rfc-channel-plugin-architecture.md — its §9 amendment decided the opposite of §5.1 here, and §9.4's "exactly one builder/parser" is Phase 2 below. rfc-append-only-session-transcript.md owns the transcript write path this document only reads.

Every claim below was measured at `707b8aef2`, current `main` on 2026-09-11. Citations name symbols rather than line numbers: this document's line citations rotted twice in three weeks (once between `fa6261c4f` and a maintainer's re-audit at `2ff4ce819`, and once when `session.py` shrank from 6489 lines to under 2800 after that re-audit), and a symbol survives the refactor that moves the line. Every symbol cited here was checked to exist in its named file at `707b8aef2`. Paths are relative to `src/kiro_crew/`.

## 1. Summary

A conversation's identity is a meaningful string whose first segment names the surface it started on: `slack:<ts>`, `dashboard:chat-12-1786…`, `cron:<job_id>`. Dozens of code sites read that string's shape to decide something — whether the conversation survives a restart, what its approval policy is, whether it may write to memory, how an audit event labels it, whether it can be nudged, where a reply is delivered. A current grep finds at least 35 executable `dashboard:` prefix reads alone.

This proposes three things, in order of how much they change:

1. **The identity carries no meaning** — an opaque `s_<12 hex>` — and every attribute currently inferred from the string becomes a field on the record `session_map.json` already keeps (§5.1, §5.2). Four fixed special identities require explicit treatment (§2.1); the scheme reserves rather than migrates them.
2. **The channels a conversation can reach are enumerable and durable, and the dashboard is not one of them** — it is the permanent host with the richest capability set, while channels attach and detach (§5.3).
3. **Every turn carries its ingress**, and the capability set that shapes a reply is the one belonging to the surface the reply is bound for — not the union of everything attached (§5.3).

The point of all three is a model that can answer, correctly and without parsing a string, *how should this reply be shaped for the person who will read it.* Today the system gets that wrong in a way the user can see (§2.3).

**Phase 0 of this shipped in August 2026 and is the foundation the rest builds on.** It took the narrowest case — one surface, one capability, one boolean — and moved it off the identity onto observed state. That was the right direction and it held. It also stopped short in specific ways (§6, Phase 0), and one of them is a live defect rather than an omission. The first remaining phase is an unrelated defect fix that stands alone; the last changes identity itself and is blocked on a maintainer decision, because it contradicts a rule another RFC already settled.

## 2. Motivation

### 2.1 What the identity does today

There is no complete central inventory. `CHANNEL_SESSION_NAMESPACES` in `constants.py` — moved there by PR #8492 on 2026-09-05 and still re-exported from `messaging/link.py`, where most of the codebase reads it — owns eleven channel-session namespaces (`slack`, `discord`, `telegram`, `whatsapp`, `webex`, `wecom`, `teams`, `weixin`, `imessage`, `feishu`, `unified`). Local conversation prefixes in active use include `dashboard:`, `cron:`, `subagent:`, `taskrunner:`, `channel:`, `side:`, `hook:`, `wf-pool:`, `wf-author:` and `wf-unpooled:`, while `_bg` and `_hb` are exact shared session keys. `acp:` is a separate runtime-attribution key rather than a `SessionManager` conversation. `secretary:` remains declared in `session.py`'s `_STATELESS_PREFIXES` and `messaging/link.py`'s `channel_namespace_of`, with no construction site in `src/`. The disagreement between inventories is itself part of the problem: a total derived from any one tuple is incomplete.

Four fixed names need an explicit migration rule, but they do not all play the same role. `cli_chat` and the `_host` sentinel identify a terminal or host caller without a `SessionMap` conversation; `cli_chat.py`'s `_chat` builds its provider directly. `_bg` and `_hb`, by contrast, are real shared `SessionManager` conversations (`BACKGROUND_KEY` and `HEARTBEAT_KEY` in `session.py`) that are deliberately stateless for resume purposes (`SessionManager.get_or_create`). All four are reused across invocations and participate in equality-based decisions (`_resolve_runtime_source` in `context.py`, `_infer_source` in `sel.py`, `_CLI_SESSION_KEY` in `computer_use/cli.py`), so none may be silently treated as a newly minted opaque conversation.

Two grammars coexist. Chat surfaces mostly use `{surface}:{agent}:{chat_type}:{scope…}[:genN]`, built by `messaging/link.py` (`build_dm_session_key`), where `scope` is a path rather than a single segment. Slack predates it and uses `slack:<thread_ts>` plus a legacy bare `<thread_ts>` form. `dashboard:` has no owning constant at all — it is a bare literal at dozens of sites.

There is one canonical channel parser, `messaging/link.py` (`parse_session_key`), and it is strict by design: fewer than four segments or an unrecognised surface returns `None`. So bare Slack timestamps, two-segment `slack:<ts>`, every `dashboard:` key and `channel:{id}:{agent}` all deliberately fail to parse. The key forms that fail that parser are among those with the most behaviour attached.

### 2.2 Why the identity encoding was wrong — and what Phase 0 proved

Before PR #1366, a conversation that started in a chat app became **two** conversations when its dashboard tab opened. A tab could only write a key beginning `dashboard:`, so it read one transcript and wrote another. A 30-second reconciler copied between the two, which is the shape of a workaround for a model that cannot express the situation.

The diagnosis is in `session_surface.py`'s own module docstring: the prefix test *"answers 'where did this start?' and not 'can the user see a dashboard right now?'"* Those are different questions, and the identity could only answer the first. PR #1366 pointed the tab at the chat app's own session, deleted the reconciler, and replaced about two dozen `session_key.startswith("dashboard:")` capability tests with one function, `has_dashboard_surface` in `session_surface.py`, asked against observed state.

That is the thesis of this document, executed once at the smallest possible scale, and it is the strongest argument here: **the replacement model was not theorised, it was shipped and it held.** What follows is not a proposal to try something new — it is a proposal to finish what that started, having learned from where it stopped.

Phase 0 stopped short in three ways, each of which is a later phase:

- The attach set is **in-memory only** (`_dashboard_surfaced` in `session_surface.py`), so nothing about surfaces survives a restart and no other layer can query it → Phase 3.
- The `dashboard:` prefix survives as an internal fast-path (the prefix branch of `has_dashboard_surface`). The fence leaks, which §2.3 documents → Phase 2.
- The identity itself is untouched, so the other shape-reads remain → Phase 4.

### 2.3 The problems that remain

**The capability question is asked in the wrong shape, and the user can see it.** This is the one problem here that is not a development-time cost, so it comes first.

`has_dashboard_surface` returns true for **any** key beginning `dashboard:`, before consulting observed state at all:

```python
# session_surface.py, has_dashboard_surface
if session_key.startswith("dashboard:") or session_key.startswith("dashboard_"):
    return True
return session_key in _dashboard_surfaced
```

So for a conversation started at the desktop the function is *permanently* true and the observed-state mechanism is bypassed entirely. The widget gate in `context.py`'s `_resolve_prompt_templates` consumes it at prompt-build time, which produces a prompt that contradicts itself: the `[RUNTIME]` line correctly names Slack as this turn's transport while the widget instructions are present anyway, because the session key starts with `dashboard:`. A user who starts a conversation at their desk, links it to Slack, and continues from Slack is offered HTML rendering for a surface that cannot render it.

Two further conflations sit behind that:

- **The published set answers a different question than the one being asked of it.** `_sync_dashboard_slots` in `dashboard/chat_utils.py` publishes `{effective_session_key(s) for s in state._slots.values()}` — *open tabs* — to two consumers: `set_active_dashboard_slots`, which `SessionManager` uses to reap orphaned sessions, and `set_dashboard_surfaced`, which answers capability. Open-tab membership is the right answer for liveness and the wrong one for capability: a conversation the user pushed into "Older sessions" leaves the set, yet the dashboard remains entirely able to display it.
- **The primitive that would answer it correctly already exists and is not wired to it.** `_resolve_runtime_source` in `context.py` takes a `runtime_source` and its docstring states the case unprompted: *"the authoritative transport for the current turn. It is intentionally separate from `session_key`: a dashboard session can be resumed from Discord."* It was split out of `_runtime_display_name` precisely so that *"the [RUNTIME] line and every source-keyed decision can never disagree"*. The widget gate is a source-keyed decision and is not on that seam: it lives in `_resolve_prompt_templates(prompt, session_key)`, whose signature carries only a session key — so it could only ever ask a session-shaped question.

None of this is a Phase 0 regression; the pre-existing prefix test had the same blind spot. It is the part of the foundation that was left unfinished, and §5.3 is the model that finishes it.

**The encoding is lossy and recovery is a linear scan.** `_safe_key` in `history.py` folds every character outside `[\w\-.]` to `_` to build the transcript filename, so `slack:<ts>` becomes `slack_<ts>.jsonl`. The fold is not invertible — nothing says which underscores were colons, and an agent name may contain one. Recovering the real key means iterating the session map and re-folding every entry until one matches (`channel_key_for_stem` in `session_map.py`), whose own docstring says the fold "is NOT reversible" and that a miss must be left unbound rather than guessed. The map that scan walks is pruned by provider-specific resume validity and transcript existence (`SessionMap.prune`), with durable-flag and channel-binding exceptions that preserve rows beyond that baseline.

**Conversion is spread across dozens of sites.** The main helpers each carry a docstring warning against the others: `_history_key_for`, `dashboard_slot_key`, `slot_transcript_key`, `slot_history_key` and `effective_session_key` in `dashboard/chat_utils.py`; `_normalize_slot_key` in `dashboard/state.py`; `channel_slot_name` in `dashboard/channel_slots.py`; and `_fold_key` in `session.py`. Inline prefix strips and constructed `dashboard:` keys remain alongside them; a brittle exact count is not used as a status gate.

**Overlapping classification is reimplemented in at least seven shape-reading ladders** — `context.py` (`_resolve_runtime_source`), `validation.py` (`infer_use_case`), `sel.py` (`_infer_source`), `mcp_gateway/claim.py` (`classify_session_type`), `mcp_gateway/stub.py` (`_build_caller_block`) (whose docstring admits it mirrors the previous one), `dashboard/handlers/sessions.py` (`api_session_tool_policy`), and `messaging/link.py` (`telemetry_channel_of`). They do not return one identical enum — runtime source, use case, audit source, caller type, agent and telemetry label differ — but each independently recovers an attribute from the key. The last is inside the module §9.4 of the channel-plugin RFC nominates as the single owner of key grammar.

**One authorization reader fails open, and the audit classifier mislabels an event.** `mcp_dashboard.py` (`_validate_args`) says its delegated-caller prefix list is knowingly incomplete and that a new key form "will read as unscoped until it is added here." It now separately fail-closes missing delegated and `dashboard:` callers, but every other unlocatable key still returns unscoped. `sel.py` (`_infer_source`) returns `"slack"` as the fallback for an unrecognised non-empty key, so a conversation the audit log cannot classify is recorded as a Slack conversation rather than as unknown.

**Both spellings already leak into stored keys.** `session_map.py` (`_resolve_alias`) carries a repair for the corrupted double prefix `dashboard:dashboard_`, which exists only because more than one place builds the name. That is the Phase 0 fence, leaking.

**And the store the string duplicates is already the authority.** `SessionMap` holds the unfolded keys and a growing set of optional per-conversation fields (including nested link, mirror and flags records). Everything it knows exactly is duplicated less reliably in the key string; describing every row as a fixed-width record would already be wrong.

## 3. Goals

| # | Goal | State after Phase 0 |
|---|---|---|
| 1 | A conversation's identity is opaque: reading it tells you nothing you could act on, so no site can regress into parsing it. | Not started |
| 2 | Every attribute currently inferred from the identity is a stored field with one writer and an explicit "unknown", so a missing answer fails closed instead of defaulting. | Not started |
| 3 | The channels attached to a conversation are known, enumerable and durable — not a boolean derived at read time from whichever tabs happen to be open. | **Partly.** Observed state exists and is load-bearing; it is one boolean, in memory, for one surface. |
| 4 | Every turn carries its ingress: which surface it arrived from is an explicit input, never inferred from the conversation's name. | **Partly.** `runtime_source` exists (`_resolve_runtime_source` in `context.py`) and reaches the `[RUNTIME]` line; it does not reach any capability gate. |
| 5 | Capability and governance are declared per surface, and a reply is shaped by the capabilities of the surface it is bound for. | Not started — capability is a single boolean, governance is conversation-level |
| 6 | Adding a surface costs a declaration, not an edit to every mechanism that has to know about it. | Not started — the six capability calls in §5.4 must each be edited today |

Goals 3–5 are the ones that let the model answer *how should this reply be shaped for whoever is about to read it.* That question is the reason the rest of this matters.

## 4. Non-goals

- Letting chat surfaces reach each other. They cannot today and this does not propose that they should.
- Changing who may message the agent. Each surface authorises its own senders; untouched.
- Owning the transcript write path — rfc-append-only-session-transcript.md owns it.
- Rewriting history. The audit log is append-only and stays readable as written (§8).
- A distributed or multi-user identity scheme. This is a single-user local tool and the id is not proposed as a security boundary (§8).
- Making the dashboard interchangeable with a channel. §5.3 is explicit that it is not, and that treating it as one would be a modelling error.

## 5. Design

### 5.1 The identity

```
session_id := "s_" [0-9a-f]{12}          # secrets.token_hex(6), validated ^s_[0-9a-f]{12}$
```

The id is the **whole** key, not a prefix plus an opaque tail. That is the load-bearing choice: leaving a namespace prefix in place keeps `startswith("dashboard:")` a working expression, and every site in §2.3 would keep reading it. Phase 0's fast-path fence is the empirical case — a prefix kept "only as an internal optimisation" is still a prefix the code reads, the `dashboard:dashboard_` repair in `session_map.py`'s `_resolve_alias` is what that costs, and §2.3's capability defect is the prefix short-circuiting the mechanism built to replace it. A prefixed form also cannot make the filename fold a no-op, because `_safe_key` still folds the colon.

Six conditions the charset satisfies, three of them non-obvious:

| Condition | Why |
|---|---|
| `_safe_key(id) == id` | the fold becomes a no-op; `s`, `_` and hex are all `\w` |
| no `.` | `\d+\.\d+` is `_SLACK_TS_RE` in `messaging/link.py`; `canonical_key` runs at the map's write/read boundaries (`SessionMap.set` and peers) and would rewrite a dotted id into `slack:<id>` |
| no leading `_` | collides with the `_bg` / `_hb` / `_host` fixed identities |
| not `^chat-\d+-\d+$` | matched as a telemetry slot at `messaging/link.py` (`_TELEMETRY_CHAT_SLOT_RE`) |
| not `(?:dashboard_)?chat-\d+-\d+$` | matched at `dashboard/state.py` (`_SLOT_KEY_TITLE_RE`) |
| lowercase hex only | no path traversal, mirroring the artifact-slug guard at `artifacts.py` |

`secrets.token_hex` rather than `uuid4().hex[:12]` because the id travels in an `X-Session-Key` header and is persisted into `open_slots.json`; a CSPRNG costs nothing here and removes the question. Width and the typed prefix follow existing convention — twelve-hex ids are minted in `dashboard/state.py` (`get_or_create_slot`) and other stores, `f"c_{secrets.token_hex(4)}"` appears at `apps/builtins/issue_radar/backend/crew_store.py` (`create_crew`), and the already-validated lowercase-hex job-id shape `^[a-f0-9]{1,16}$` is at `validation.py`.

### 5.2 The record

`session_map.json` is today a flat `key → entry` object with **no envelope and no version marker** (`json.dumps(self._data)` in `SessionMap._serialize`); migration is shape-sniffing inside `SessionMap._load`. An opaque id is indistinguishable from a legacy dashboard slot key by inspection, so **a version envelope is a prerequisite, not a nicety** — `autonudge.py` already has `_STORE_VERSION = 1` and is the model.

The entry gains these fields. Each replaces exactly one shape-read, and each has one writer:

| Field | Replaces | Writer |
|---|---|---|
| `surface` | `sel.py`, `context.py`, `validation.py` prefix ladders | mint site |
| `channels[]` | Phase 0's in-memory attach set (`_dashboard_surfaced` in `session_surface.py`) — persists what already exists, and holds channels only, per §5.3 | attach/detach |
| `stateful` | `_STATELESS_PREFIXES` in `session.py` plus exact-key handling | mint site, mutable thereafter |
| `restricted` | constructed `dashboard:` keys in dashboard persistence/handlers, read by `handlers/_shared.py:_is_restricted_session` | the restrict/unrestrict handler |
| `approval_policy` | session policy lookups keyed by a constructed or effective session key | the approval handler |
| `delegated` | `_DELEGATED_CALLER_PREFIXES`, read by `_validate_args` in `mcp_dashboard.py` | mint site |
| `nudgeable`, `nudge_mode` | `binding_key_for` + `is_channel_key` (`autonudge.py` and its channel predicates) | mint site |
| `agent` | the ladder at `dashboard/handlers/sessions.py` (`api_session_tool_policy`) | mint site |
| `channel.thread_id` | `slack/gateway.py` (`_fire_slack_nudge`), which recovers the delivery thread *from the key* | the surface on attach |
| `legacy_stems[]` | the reverse scan `channel_key_for_stem` | migration only |

`channels[]` is not a new idea — Phase 0 already built the attach set and simply kept it in memory. Persisting it is what lets the other rows stop being prefix reads.

`restricted` and `approval_policy` are shown here as conversation-level because that is what they are today. §5.3 argues governance is properly a per-surface property, which would make these a per-entry default that an attached channel can tighten. That expansion is deliberately left as §10.2 rather than assumed here.

Two shapes must be preserved rather than simplified. Legacy maps can contain two conversations claiming one `channel.thread_id`; `SessionMap._rebuild_thread_index` heals that contest on load with a key-shape tie-break, while live `set_slack_link` writes evict rival claimants (`_evict_rival_claimants`). An opaque id removes the shape-read used by both paths, so the stored binding must identify its origin explicitly. And `stateful` must be a stored mutable field, not derived from `surface`, because `_is_continuable_key` in `session.py` already lets a caller opt out.

Every reader gets an explicit miss. `_infer_source`'s `"slack"` fallback in `sel.py` becomes `"unknown"`; `mcp_dashboard.py`'s fail-open list becomes a refusal on absent `delegated`.

**The record must also answer the inbound direction, which the table above does not.** Every field there replaces an *outbound* shape-read — given a conversation, decide something about it. Inbound is the opposite: a second Slack message arrives on a thread and the system must find the conversation that already owns it. The reverse index `_thread_to_session`, rebuilt by `SessionMap._rebuild_thread_index`, is already load-bearing for inbound Slack; it is rebuilt from entries on load and maintained by link writes. With a random id the generalized `(surface, conversation_id, thread_id) → session id` index becomes the only way any transport can recover an existing conversation, so attach/detach must maintain it without reconstructing ownership from key shapes.

That index carries a shape-read the §2.3 inventory missed, and it is the sharpest one in the codebase. Its load-time tie-break in `_rebuild_thread_index` resolves legacy duplicate claims by asking whether a key *derives from* that thread — `"A slack:<ts> key whose ts IS the thread is the fork … any other key holds the real conversation."` The live writer's rival eviction uses the same self-derived distinction. That heuristic exists to clean up exactly the duplicate-conversation bug Phase 0 fixed. Under an opaque id no key derives from anything, so the fact that distinguishes an original binding from a fork must be stored before any id is minted.

### 5.3 Surfaces: one host, many attachments, one ingress per turn

Three properties are deliberately separate here. Every defect in §2.3's first item comes from conflating two of them.

| Property | The question it answers | Lifetime |
|---|---|---|
| **Capability** | what can this surface render, and what input can it accept | static per surface type |
| **Attachment** | is this channel currently bound to this conversation | episodic — channels attach and detach |
| **Ingress** | which surface did *this turn* arrive from | exactly one, per turn |

**The dashboard is the host, not an attachment.** It does not belong in `channels[]`, because there is no state in which it is absent: every conversation is displayable in it, it always accrues the transcript, and there is no detach operation. What varies is *attention* — a tab is foregrounded, backgrounded, or pushed into "Older sessions" — and attention is not capability. A conversation the user is not looking at loses nothing and can be reopened with full fidelity; it simply updates quietly. That is precisely the distinction `_sync_dashboard_slots` collapses today (§2.3), by publishing open tabs as though open-ness were the capability question.

So the model is: **the dashboard is a permanent surface holding the richest capability set, and channels are attachments that come and go.** This is an asymmetry, not a special case to be refactored away later, and it is load-bearing in one specific way — if the dashboard were an ordinary roster member, "no surfaces attached" would be a reachable state meaning *this conversation cannot be displayed anywhere*, which is never true. Any design that makes the dashboard detachable has to answer what that state means, and there is no useful answer.

It also settles a question the channel-plugin RFC left open: the dashboard is not the eleventh member of the builtin transport registry (`builtin_channel_descriptors` in `channels.py`). It is the thing the registry's members attach *to*.

**Ingress selects the delivery surface; the delivery surface selects the capability set.** A reply is shaped for whoever is about to read it, which is one surface, not the union of everything attached. Concretely, on one unchanged conversation:

| Turn arrives from | Delivery surface | Widgets |
|---|---|---|
| the dashboard | dashboard | yes |
| Slack | Slack | no |
| Slack, dashboard tab also open | Slack | no — the open tab is not who asked |

The third row is the one today's code gets wrong in both directions: wrong via the prefix short-circuit for a desktop-born conversation, and wrong via open-tab membership for a Slack-born one. Attachment is the wrong input; ingress is the right one, and `_resolve_runtime_source` in `context.py` is already the value that carries it.

This does not make attachment useless — it is what says *where else this conversation is reachable*, which is what an unprompted notification or a handoff digest needs (`_notify_orphan_impl` in `subagent_manager/monitoring.py`, reached through `subagent.py`'s `_notify_orphan`, is exactly that case). Ingress answers "how do I shape this reply"; attachment answers "where can I reach this person at all". Both are needed and they are not the same query.

**Governance belongs with capability, per surface.** Approval policy and restricted-write are conversation-level today. Whether the same conversation should carry the same tool-approval requirements when driven from a phone in Slack as when driven from the desktop is a real question and the answer is probably no. §10.2 holds it, because it widens an authorization surface and wants its own decision rather than being smuggled in here.

### 5.4 Capabilities

`has_dashboard_surface` — Phase 0's function — is currently invoked nine times across eight source lines. Three calls, all inside `dashboard_slot_key` in `dashboard/chat_utils.py`, sit in the slot-name resolver and are not capability questions. The other six ask four distinct questions through one boolean, which become four declared capabilities:

| Capability | Asked at | Gates |
|---|---|---|
| `can_render_rich_html` | `context.py` · `_resolve_prompt_templates` | the widget block in the system prompt |
| `can_render_interactive_card` | `context.py` · `build_message`; `mcp_tools/control.py` · `ask_question`; `session_directive_apply.py` · `apply_session_directive` | the question/card feature at the prompt, tool and directive-consumer boundaries — three sites, one name, must not diverge |
| `has_mutable_slot` | `session_directive_apply.py` · `_has_user_surface` | one input to whether a user-originated directive may retarget project or CWD |
| `can_inject_turn` | `subagent_manager/monitoring.py` · `_notify_orphan_impl` | orphan notice as a turn, or fall back to a DM digest |

Each surface type declares its set once. The four are not asked the same way, and the split follows §5.3: the first three are **delivery** questions, resolved against the capabilities of this turn's delivery surface. `can_inject_turn` is a **reachability** question — it is asked by a subagent with no turn of its own, so it has no ingress, and it resolves against `channels[]` plus the always-present host.

A surface that renders *better* than the dashboard becomes expressible, which Phase 0's boolean cannot say because it can only answer "is the dashboard here."

### 5.5 Routing without parsing

The sharpest dependency is `slack/gateway.py` (`_fire_slack_nudge`), whose comment reads `# Canonical keys embed the thread root ts.` and recovers the delivery target from the key; the same file's `_notif_meta` splits a key into `(channel, ts)` to build a permalink. Other channel-specific notification paths also split keys for routing, so Phase 4 must inventory all routing readers, not just Slack. Under this design they read `channel.conversation_id` and `channel.thread_id` from the record. `messaging/link.py` (`bind_origin_mirror`) skips origin-mirror binding when a key is `unified:` because that name identifies no single conversation — that becomes an absent `channel` record rather than a prefix test.

## 6. Migration plan

Each phase is independently shippable and independently abandonable.

**Phase 0 — capability from observed state, not origin. ✅ On main, August 2026.** PR #1366 with follow-ups #1455, #1480, #1539, #1921. A dashboard tab opened on a chat conversation now joins that conversation instead of forking a second one; the 30-second reconciler that had been copying between the two is deleted; about two dozen `startswith("dashboard:")` capability tests became one `has_dashboard_surface` call.

*What it did not do,* which is why this document continues: the attach set is in-memory only, so nothing survives a restart and no other layer can query it; the capability is a single boolean covering four distinct questions and answering none of them in terms of the turn's ingress; the `dashboard:` prefix survives as a fast-path at `session_surface.py` (`has_dashboard_surface`, the prefix branch) and short-circuits the very mechanism it was meant to be replaced by; and the identity is unchanged, so the shape-reads in §2.3 remain.

*Per-PR attribution for the four follow-ups is not audited in this revision* — the set was verified as a whole. If the index needs a per-PR breakdown, that is a follow-up audit, flagged here rather than guessed.

**Phase 1 — refuse a turn from an unbound tab.** Fixes a live defect; depends on nothing else here. It is the residue of Phase 0: when `channel_key_for_stem` misses, `surface_channel_session` in `dashboard/channel_slots.py` deliberately surfaces the tab unbound, which is correct, but nothing then stops a turn. `effective_session_key` in `dashboard/chat_utils.py` falls back to `dashboard:<stem>` — a second session with its own turn semaphore — while its sibling `slot_history_key` correctly routes the transcript back to the chat app's file. So the duplicate-conversation bug Phase 0 closed still has one open door. The file cannot tear: `ConversationLog._locked` in `history.py` holds an in-process lock plus a cross-process advisory lock keyed on the resolved path. What is unserialised is the turn, and the reply never reaches the chat app.

*Exit criteria:* a turn started from a slot with `channel_origin` true and an empty `linked_session_key` is refused with a reason the user can see; a test pins the refusal; and the guard sits at **`_run_chat`'s entry** (`dashboard/chat_runner.py`) rather than at its callers. That placement is the criterion, not a convenience: every path reaches `_run_chat`, and enumerating callers has already been got wrong twice. The count keeps moving in the direction that proves the point: the maintainer's re-audit at `2ff4ce819` found thirteen direct call expressions; at `707b8aef2` there are about two dozen across thirteen modules — public chat, regenerate/rewind, OpenAI compatibility, orchestration, messaging/taskrunner, the Slack gateway and handler, the Issue Radar and Spec Builder apps, and internal follow-up paths. Revision 2 missed several of those; this criterion deliberately does not depend on the list staying complete.

**Phase 2 — a version envelope and one converter.** Retires Phase 0's fast-path fence. *Exit criteria:* `session_map.json` carries a version envelope and `_load` dispatches on it rather than sniffing entry shape; `messaging/link.py` owns the `dashboard:` namespace with a builder and a parser that returns a value for it; the at-least-seven classification ladders consume one parsed attribute record, including the classifier already inside `link.py`; no `startswith("dashboard:")` remains in `src/` outside that module, *including the fast-path branch at `session_surface.py` (`has_dashboard_surface`, the prefix branch)*; and the `dashboard:dashboard_` repair at `session_map.py` (`_resolve_alias`) is deleted rather than moved, with a test proving the corrupt spelling can no longer be produced.

**Phase 3 — the surface model: durable channels, per-turn ingress, declared capabilities.** Generalises Phase 0 from one in-memory boolean to §5.3's three properties. *Exit criteria:* `channels[]` is persisted per §5.2 and holds channels only, so the answer survives a restart; the dashboard is modelled as the permanent host and is never an entry in it; the turn's ingress reaches the capability gates — concretely, `_resolve_prompt_templates` (`context.py`) takes the resolved runtime source as an argument rather than deriving anything from `session_key`; the four capabilities of §5.4 are declared per surface type, with the three delivery capabilities resolved against this turn's delivery surface and `can_inject_turn` against reachability; a test pins the case today's code fails — a `dashboard:`-born conversation, turn arriving via Slack, prompt built **without** the widget block; the capability set and the reaping set stop being one set, so pushing a session into "Older sessions" changes attention and not capability; adding a surface that renders widgets requires no edit to the six callers; and `has_dashboard_surface` is deleted.

**Phase 4 — the opaque id.** *Blocked on open question 1.* *Exit criteria:* a newly minted conversation's id matches `^s_[0-9a-f]{12}$`; every §5.2 field is read from the record rather than parsed from the id; **an inbound message on an existing thread resolves to its conversation through the `(surface, conversation_id, thread_id)` index rather than by constructing a key, and the fork tie-break is a stored fact rather than a key-shape test** (§5.2); `channel_key_for_stem` and its scan are deleted; the `stem.replace("_", ":", 1)` in `history.py`'s `_index_key_for_stem` is gone (it would split `s_3f9c…` into `s:3f9c…`); the four fixed identities of §2.1 are enumerated in one place with their conversation-versus-caller role, so none is silently left as a semantic key that code still compares against; and every legacy key still resolves per §7.

## 7. Backward compatibility

Seven compatibility obligation groups follow. None can be skipped in Phase 4.

1. **Transcript filenames on disk.** Either alias via `legacy_stems` — mirroring what `transcript_stems` and `_path`'s fallback in `history.py` already do for bare Slack timestamps — or rename with a symlink, which `ConversationLog.list_sessions` skips as a handoff alias. Sidecars share the stem and must move together. The input is not clean: stacked `dashboard_` stems already exist, which is why `_canonical_key` has to strip them.
2. **The session map.** Needs the envelope first, and must preserve `sid` above all — `SessionMap.prune` (`session_map.py`) validates resumability and can delete a stale row.
3. **Approval policy and restricted keys.** Nothing on disk, but every construction site must switch in one commit; a half-migrated set is a silent authorization miss, which `dashboard/chat_persistence.py` already documents for the `dashboard_` / `dashboard:` pair.
4. **The Slack thread reverse index.** Derived, so mostly free — except `channel.thread_id` must be populated for every existing Slack conversation, because the key-derived fallback at `slack/gateway.py` (`_fire_slack_nudge`) disappears.
5. **`_fold_key`.** Kept for legacy rows; new mints bypass it. §5.1's no-dot condition is what guarantees `canonical_key` can never mistake an opaque id for a bare Slack timestamp.
6. **Persisted foreign keys and reconstruction fallbacks.** The list is larger than four: `CronJob.session_key` in `cron.py`, with `f"cron:{job_id}"` fallbacks such as the one in `_force_reap`; the subagent `conversation_key` on `SubagentInfo` in `subagent.py`, whose resume-mismatch path in `_run_inner` **refuses to execute** — a hard failure, not a degradation; `ChannelAgent.session_key` in `channel.py`, rebuilt from two ids in `ChannelAgent.deserialize`; versioned `autonudge.json` records (`_STORE_VERSION` and `NudgeLoop` in `autonudge.py`); the session ledger's `slot_key`; and transcript metadata's `linked_session_key`. Phase 4 must inventory persisted consumers rather than treating the first four found as exhaustive.
7. **The audit log.** Append-only and not rewritten, so `_infer_source` must keep classifying legacy keys — which is why `_infer_source`'s `"slack"` fallback in `sel.py` becomes `"unknown"` rather than disappearing.

Phase 3 adds one of its own: **a turn with no resolvable ingress must fail toward the least capable surface, not the most.** Today's absent-`runtime_source` path in `_resolve_runtime_source` falls back to prefix inference, which is the same disease; under §5.3 an unknown ingress means plain text.

## 8. Security considerations

- **The audit mislabel is a present bug, not a migration risk.** `_infer_source` in `sel.py` records an unclassifiable non-empty key as `"slack"`. Fixing it to `"unknown"` is in Phase 2's scope and is worth doing whether or not Phase 4 happens.
- **One authorization reader still fails open for unknown key forms.** `_validate_args` in `mcp_dashboard.py` documents that an unrecognised non-delegated, non-dashboard key reads as unscoped. Under §5.2 an absent `delegated` field must refuse. Any phase that adds a field must state its failure direction, and fail-closed is the only acceptable answer.
- **Capability must fail closed too, and today it fails open.** The prefix short-circuit in §2.3 hands a capability to a surface that does not have it. Phase 3's version of the rule: an unresolved ingress or an unknown surface yields the minimum capability set, never the host's.
- **Per-surface governance widens an authorization surface, which is why it is a question and not a proposal.** If approval policy becomes per-surface (§10.2), the failure mode to design against is a channel that inherits the desktop's looser policy by omission. The default must be the tightest of the applicable policies, not the conversation's.
- **Moving authorization state off existing identities is the risk to review.** Restricted-write state still uses constructed dashboard keys, while approval policy now follows `effective_session_key` for dashboard turns and the transport's session key for messaging turns. A half-migration can silently miss either lookup, so Phase 4 needs tests that an absent record cannot widen authorization and that revocation reaches the same identity approval granted.
- **The id is not an authorization token.** It is non-sequential and CSPRNG-generated so that values appearing in a header or `open_slots.json` are not simple counters, but 48 bits must not be treated as an authorization boundary. Sender authorisation stays per surface, where it is today.
- **Phase 1 closes a turn-serialisation gap, not a corruption risk.** Both lock layers are path-keyed, so the transcript cannot tear; what two semaphores buy is two agents taking turns in one conversation, each blind to the other.

## 9. Alternatives considered

- **Stop at Phase 0.** The strongest alternative, and it deserves stating rather than dismissing: the duplicate-conversation bug is fixed and the reconciler is gone. It is not defensible for Phase 1, a live user-visible defect, nor now for Phase 3 — §2.3's capability failure is also user-visible, and it is the kind that erodes trust quietly, by making the agent look like it does not know where it is talking.
- **Make the dashboard an ordinary channel in the transport registry.** Symmetrical, and briefly attractive because it deletes the special case. Rejected in §5.3: it creates a reachable "no surfaces attached" state that has no meaning, and it invites a detach operation that should not exist. The dashboard is the richest surface and the only permanent one; a model that cannot say so is a worse model, not a simpler one.
- **A typed address object everywhere** (option A in the channel-plugin RFC's §9). Rejected there for touching every `sessions.*` call site; that reasoning is unchanged.
- **An opaque key with a canonical grammar** (option B, the decided one). Phases 1–3 need no change to it and are compatible with it. Only Phase 4 departs, and §9's table did not evaluate an attribute store — it compared A, B, the status quo, and a URI scheme.
- **A namespace prefix with an opaque tail.** Rejected in §5.1, and Phase 0 is the evidence: it kept a prefix as a fast-path only, and that prefix now both corrupts (the `_resolve_alias` repair in `session_map.py`) and short-circuits a correctness gate (§2.3).
- **Derive capability from the union of attached surfaces.** The obvious reading of "surfaces, not origin", and wrong for the same reason origin was: it answers where the conversation is reachable, not who is about to read this reply. It would render widgets into Slack whenever any tab was open.
- **`uuid4().hex[:12]` instead of a CSPRNG.** Matches more existing precedent, and would be fine; rejected only because the id is externally visible and the cost of `secrets` is zero.
- **Derive `stateful` from `surface`.** Rejected: `session.py` already lets a caller opt a conversation out, so the attribute must be independently settable.

## 10. Open questions

1. **Does the identity become opaque at all?** The channel-plugin RFC's §9 decided that the first segment is the routing authority; §5.1 here argues the record should be. Phase 4 cannot start until a maintainer rules, and the honest possibility is that Phases 1–3 are worth doing and Phase 4 is not.
2. **Does governance become per-surface, and at what granularity?** §5.3 argues capability and governance belong together on the surface; §5.2 leaves `approval_policy` and `restricted` conversation-level because moving them widens an authorization surface. Per surface type, or per attached instance? The case that decides it: the same conversation driven from the desktop and from a phone, where the second probably warrants tighter tool approval, not equal.
3. **At what granularity is a capability declared** — per surface type, per attached instance, or negotiated at attach? The case that forces it is a chat surface whose thread support differs between a direct message and a channel, which per-type cannot express.
4. **Is `secretary:` dead?** It is declared in two places with no construction site in `src/`. If an external app can mint it, the namespace inventory in §2.1 is incomplete and Phase 2 must account for it.
5. **Should the transcript, not the session, be what surfaces attach to?** rfc-append-only-session-transcript.md owns the write path, and Phase 1's two-semaphores-one-file behaviour is visible from both documents. If that RFC proceeds, the two need to agree on where turn-level serialisation lives before Phase 4 moves identity.

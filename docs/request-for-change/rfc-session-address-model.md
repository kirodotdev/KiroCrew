---
title: Session Address Model — an opaque conversation identity with an attribute store
status: draft
author: nrb
created: 2026-08-17
last-audited: 2026-08-29
audited-at: 2ff4ce819
doc-pr: 4077
revision: 2
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---
# RFC: Session Address Model — an opaque conversation identity with an attribute store

- Status: draft — nothing proposed here is built. Revision 2 replaces a retrospective first revision that described a shipped change instead of proposing one. The seam it addresses was partly rebuilt in August 2026 by PR #1366 and four follow-ups (#1455, #1480, #1539, #1921); that work is prior art in §2, not a phase of this document.
- Author: nrb
- Created: 2026-08-17 · Revised: 2026-08-29
- Related: rfc-channel-plugin-architecture.md — its §9 amendment decided the opposite of §5 here, and §9.4's "exactly one builder/parser" is Phase 2 below. rfc-append-only-session-transcript.md owns the transcript write path this document only reads.

Every claim below was re-measured at `2ff4ce819`. Paths are repo-relative.

## 1. Summary

A conversation's identity is a meaningful string whose first segment names the surface it started on: `slack:<ts>`, `dashboard:chat-12-1786…`, `cron:<job_id>`. Dozens of code sites read that string's shape to decide something — whether the conversation survives a restart, what its approval policy is, whether it may write to memory, how an audit event labels it, whether it can be nudged, where a reply is delivered. A current grep finds at least 35 executable `dashboard:` prefix reads alone.

This proposes that the identity carry no meaning at all — an opaque `s_<12 hex>` — and that every attribute currently inferred from the string become a field on the record `session_map.json` already keeps for each conversation. Four fixed special identities require explicit treatment (§2.1); the scheme reserves rather than migrates them. Four phases, each independently shippable. The first is a defect fix that stands alone; the fourth changes identity and is blocked on a maintainer decision, because it contradicts a rule another RFC already settled.

## 2. Motivation

### 2.1 What the identity does today

There is no complete central inventory. `messaging/link.py:45-57` owns eleven channel-session namespaces (`slack`, `discord`, `telegram`, `whatsapp`, `webex`, `wecom`, `teams`, `weixin`, `imessage`, `feishu`, `unified`). Local conversation prefixes in active use include `dashboard:`, `cron:`, `subagent:`, `taskrunner:`, `channel:`, `side:`, `hook:`, `wf-pool:`, `wf-author:` and `wf-unpooled:`, while `_bg` and `_hb` are exact shared session keys. `acp:` is a separate runtime-attribution key rather than a `SessionManager` conversation. `secretary:` remains declared at `session.py:355` and `link.py:116`, with no construction site in `src/`. The disagreement between inventories is itself part of the problem: a total derived from any one tuple is incomplete.

Four fixed names need an explicit migration rule, but they do not all play the same role. `cli_chat` and the `_host` sentinel identify a terminal or host caller without a `SessionMap` conversation; `cli_chat.py:273-275` builds its provider directly. `_bg` and `_hb`, by contrast, are real shared `SessionManager` conversations (`session.py:370-407`) that are deliberately stateless for resume purposes (`session.py:3114-3123`). All four are reused across invocations and participate in equality-based decisions (`context.py:675-679`, `sel.py:3234-3249`, `computer_use/cli.py:96-109`), so none may be silently treated as a newly minted opaque conversation.

Two grammars coexist. Chat surfaces mostly use `{surface}:{agent}:{chat_type}:{scope…}[:genN]`, built by `messaging/link.py:380-447`, where `scope` is a path rather than a single segment. Slack predates it and uses `slack:<thread_ts>` plus a legacy bare `<thread_ts>` form. `dashboard:` has no owning constant at all — it is a bare literal at dozens of sites.

There is one canonical channel parser, `messaging/link.py:286-324`, and it is strict by design: fewer than four segments or an unrecognised surface returns `None`. So bare Slack timestamps, two-segment `slack:<ts>`, every `dashboard:` key and `channel:{id}:{agent}` all deliberately fail to parse. The key forms that fail that parser are among those with the most behaviour attached.

The August 2026 work is the relevant prior art. Before PR #1366, a conversation that started in a chat app became **two** conversations when its dashboard tab opened, because a tab could only write a key beginning `dashboard:`, so it read one transcript and wrote another. That PR pointed the tab at the chat app's own session, deleted the 30-second reconciler that had been copying between the two, and replaced about two dozen `session_key.startswith("dashboard:")` capability tests with one function, `has_dashboard_surface` (`session_surface.py:42`). Its module docstring states the diagnosis exactly: the prefix test *"answers 'where did this start?' and not 'can the user see a dashboard right now?'"*

That fixed the symptom and left the cause. The identity still encodes origin, and the code still reads it.

### 2.2 The problems

**The encoding is lossy and recovery is a linear scan.** `history._safe_key` (`history.py:1678-1680`) folds every character outside `[\w\-.]` to `_` to build the transcript filename, so `slack:<ts>` becomes `slack_<ts>.jsonl`. The fold is not invertible — nothing says which underscores were colons, and an agent name may contain one. Recovering the real key means iterating the session map and re-folding every entry until one matches (`session_map.py:1643-1664`), whose own docstring says the fold "is NOT reversible" and that a miss must be left unbound rather than guessed. The map that scan walks is pruned by provider-specific resume validity and transcript existence (`SessionMap.prune`, `session_map.py:997-1078`), with durable-flag and channel-binding exceptions that preserve rows beyond that baseline.

**Conversion is spread across dozens of sites.** The main helpers each carry a docstring warning against the others: `_history_key_for` (`dashboard/chat_utils.py:519`), `dashboard_slot_key` (`:534`), `slot_transcript_key` (`:592`), `slot_history_key` (`:611`), `effective_session_key` (`:651`), `_normalize_slot_key` (`dashboard/state.py:2647`), `channel_slot_name` (`dashboard/channel_slots.py:109`) and `_fold_key` (`session.py:934`). Inline prefix strips and constructed `dashboard:` keys remain alongside them; a brittle exact count is not used as a status gate.

**Overlapping classification is reimplemented in at least seven shape-reading ladders** — `context.py:651-699`, `validation.py:172-198`, `sel.py:3218-3271`, `mcp_gateway/claim.py:57-67`, `mcp_gateway/stub.py:375-405` (whose docstring admits it mirrors the previous one), `dashboard/handlers/sessions.py:1790-1812`, and `messaging/link.py:149-174`. They do not return one identical enum — runtime source, use case, audit source, caller type, agent and telemetry label differ — but each independently recovers an attribute from the key. The last is inside the module §9.4 of the channel-plugin RFC nominates as the single owner of key grammar.

**One authorization reader fails open, and the audit classifier mislabels an event.** `mcp_dashboard.py:652-723` says its delegated-caller prefix list is knowingly incomplete and that a new key form "will read as unscoped until it is added here." It now separately fail-closes missing delegated and `dashboard:` callers, but every other unlocatable key still returns unscoped. `sel.py:3271` returns `"slack"` as the fallback for an unrecognised non-empty key, so a conversation the audit log cannot classify is recorded as a Slack conversation rather than as unknown.

**Both spellings already leak into stored keys.** `session_map.py:733-745` carries a repair for the corrupted double prefix `dashboard:dashboard_`, which exists only because more than one place builds the name.

**And the store the string duplicates is already the authority.** `SessionMap` holds the unfolded keys and a growing set of optional per-conversation fields (including nested link, mirror and flags records). Everything it knows exactly is duplicated less reliably in the key string; describing every row as a fixed-width record would already be wrong.

## 3. Goals

1. A conversation's identity is opaque: reading it tells you nothing you could act on, so no site can regress into parsing it.
2. Every attribute currently inferred from the identity is a stored field with one writer and an explicit "unknown", so a missing answer fails closed instead of defaulting.
3. What a conversation may do is a property of the surfaces attached to it now, declared by those surfaces, not of where it started.
4. Adding a surface costs a declaration, not an edit to every mechanism that has to know about it.

## 4. Non-goals

- Letting chat surfaces reach each other. They cannot today and this does not propose that they should.
- Changing who may message the agent. Each surface authorises its own senders; untouched.
- Owning the transcript write path — rfc-append-only-session-transcript.md owns it.
- Rewriting history. The audit log is append-only and stays readable as written (§8).
- A distributed or multi-user identity scheme. This is a single-user local tool and the id is not proposed as a security boundary (§8).

## 5. Design

### 5.1 The identity

```
session_id := "s_" [0-9a-f]{12}          # secrets.token_hex(6), validated ^s_[0-9a-f]{12}$
```

The id is the **whole** key, not a prefix plus an opaque tail. That is the load-bearing choice: leaving a namespace prefix in place keeps `startswith("dashboard:")` a working expression, and every site in §2.2 would keep reading it. It also makes the filename fold a no-op, which a prefixed form cannot — `_safe_key` still folds the colon.

Six conditions the charset satisfies, three of them non-obvious:

| Condition | Why |
|---|---|
| `_safe_key(id) == id` | the fold becomes a no-op; `s`, `_` and hex are all `\w` |
| no `.` | `\d+\.\d+` is `_SLACK_TS_RE` (`messaging/link.py:28`); `canonical_key` runs at the map's write/read boundaries (`session_map.py:932` and peers) and would rewrite a dotted id into `slack:<id>` |
| no leading `_` | collides with the `_bg` / `_hb` / `_host` singletons |
| not `^chat-\d+-\d+$` | matched as a telemetry slot at `messaging/link.py:136` |
| not `(?:dashboard_)?chat-\d+-\d+$` | matched at `dashboard/state.py:2173` |
| lowercase hex only | no path traversal, mirroring the artifact-slug guard at `artifacts.py:543` |

`secrets.token_hex` rather than `uuid4().hex[:12]` because the id travels in an `X-Session-Key` header and is persisted into `open_slots.json`; a CSPRNG costs nothing here and removes the question. Width and the typed prefix follow existing convention — twelve-hex ids are minted in `dashboard/state.py:7094` and other stores, `f"c_{secrets.token_hex(4)}"` appears at `apps/builtins/issue_radar/backend/crew_store.py:649`, and the already-validated lowercase-hex job-id shape `^[a-f0-9]{1,16}$` is at `validation.py:211`.

### 5.2 The record

`session_map.json` is today a flat `key → entry` object with **no envelope and no version marker** (`json.dumps(self._data)`, `session_map.py:535-544`); migration is shape-sniffing inside `_load` (`:339-431`). An opaque id is indistinguishable from a legacy dashboard slot key by inspection, so **a version envelope is a prerequisite, not a nicety** — `autonudge.py:60` already has `_STORE_VERSION = 1` and is the model.

The entry gains these fields. Each replaces exactly one shape-read, and each has one writer:

| Field | Replaces | Writer |
|---|---|---|
| `surface` | `sel.py`, `context.py`, `validation.py` prefix ladders | mint site |
| `surfaces[]` | nothing today — the attach set is in-memory only (`session_surface.py:28`) | attach/detach |
| `stateful` | `_STATELESS_PREFIXES` (`session.py:347-368`) plus exact-key handling | mint site, mutable thereafter |
| `restricted` | constructed `dashboard:` keys in dashboard persistence/handlers, read by `handlers/_shared.py:_is_restricted_session` | the restrict/unrestrict handler |
| `approval_policy` | session policy lookups keyed by a constructed or effective session key | the approval handler |
| `delegated` | `_DELEGATED_CALLER_PREFIXES` (`mcp_dashboard.py:652-667`) | mint site |
| `nudgeable`, `nudge_mode` | `binding_key_for` + `is_channel_key` (`autonudge.py:168` and its channel predicates) | mint site |
| `agent` | the ladder at `handlers/sessions.py:1790-1812` | mint site |
| `channel.thread_id` | `slack/gateway.py:5267-5271`, which recovers the delivery thread *from the key* | the surface on attach |
| `legacy_stems[]` | the reverse scan `channel_key_for_stem` | migration only |

Two shapes must be preserved rather than simplified. Legacy maps can contain two conversations claiming one `channel.thread_id`; `_rebuild_thread_index` (`session_map.py:436-477`) heals that contest on load with a key-shape tie-break, while live `set_slack_link` writes evict rival claimants (`session_map.py:1071-1110`). An opaque id removes the shape-read used by both paths, so the stored binding must identify its origin explicitly. And `stateful` must be a stored mutable field, not derived from `surface`, because `_is_continuable_key` (`session.py:5259`) already lets a caller opt out.

Every reader gets an explicit miss. `sel.py:3271`'s `"slack"` fallback becomes `"unknown"`; `mcp_dashboard.py`'s fail-open list becomes a refusal on absent `delegated`.

**The record must also answer the inbound direction, which the table above does not.** Every field there replaces an *outbound* shape-read — given a conversation, decide something about it. Inbound is the opposite: a second Slack message arrives on a thread and the system must find the conversation that already owns it. The reverse index `_thread_to_session` (`session_map.py:436-478`) is already load-bearing for inbound Slack; it is rebuilt from entries on load and maintained by link writes. With a random id the generalized `(surface, conversation_id, thread_id) → session id` index becomes the only way any transport can recover an existing conversation, so attach/detach must maintain it without reconstructing ownership from key shapes.

That index carries a shape-read the §2.2 inventory missed, and it is the sharpest one in the codebase. Its load-time tie-break (`session_map.py:445-449`) resolves legacy duplicate claims by asking whether a key *derives from* that thread — `"A slack:<ts> key whose ts IS the thread is the fork … any other key holds the real conversation."` The live writer's rival eviction uses the same self-derived distinction. Under an opaque id no key derives from anything, so the fact that distinguishes an original binding from a fork must be stored before any id is minted.

### 5.3 Capabilities

`has_dashboard_surface` is currently invoked nine times across eight source lines. Three calls (`chat_utils.py:562`, twice, and `:566`) sit inside the slot-name resolver and are not capability questions. The other six calls ask four distinct questions, which become four declared capabilities:

| Capability | Asked at | Gates |
|---|---|---|
| `can_render_rich_html` | `context.py:1980` | the widget block in the system prompt |
| `can_render_interactive_card` | `context.py:3158`, `mcp_tools/control.py:760` and `session_directive_apply.py:115` | the question/card feature at the prompt, tool and directive-consumer boundaries — three sites, one name, must not diverge |
| `has_mutable_slot` | `session_directive_apply.py:68` | one input to whether a user-originated directive may retarget project or CWD |
| `can_inject_turn` | `subagent.py:1988` | orphan notice as a turn, or fall back to a DM digest |

A surface declares its set on attach. The query is "does any attached surface have capability X", so a surface that renders *better* than the dashboard is expressible — which the current boolean cannot say.

### 5.4 Routing without parsing

The sharpest dependency is `slack/gateway.py:5267-5271`, whose comment reads `# Canonical keys embed the thread root ts.` and recovers the delivery target from the key; `:5898-5906` splits a key into `(channel, ts)` to build a permalink. Other channel-specific notification paths also split keys for routing, so Phase 4 must inventory all routing readers, not just Slack. Under this design they read `channel.conversation_id` and `channel.thread_id` from the record. `messaging/link.py:655` skips origin-mirror binding when a key is `unified:` because that name identifies no single conversation — that becomes an absent `channel` record rather than a prefix test.

## 6. Migration plan

Each phase is independently shippable and independently abandonable.

**Phase 1 — refuse a turn from an unbound tab.** Fixes a live defect; depends on nothing else here. When `channel_key_for_stem` misses, `surface_channel_session` deliberately surfaces the tab unbound (`dashboard/channel_slots.py:284-308`), which is correct, but nothing then stops a turn: `effective_session_key` falls back to `dashboard:<stem>` (`dashboard/chat_utils.py:651-667`), a second session with its own turn semaphore, while `slot_history_key` (`:611-648`) correctly routes the transcript back to the chat app's file. The file cannot tear — `ConversationLog._locked` (`history.py:2352`) holds an in-process lock plus a cross-process advisory lock keyed on the resolved path. What is unserialised is the turn, and the reply never reaches the chat app.

*Exit criteria:* a turn started from a slot with `channel_origin` true and an empty `linked_session_key` is refused with a reason the user can see; a test pins the refusal; and the guard sits at **`_run_chat`'s entry** (`dashboard/chat_runner.py:4519`) rather than at its callers. That placement is the criterion, not a convenience: every path reaches `_run_chat`, and enumerating callers has already been got wrong twice. The current tree has thirteen direct call expressions spread across public chat, regenerate/rewind, OpenAI compatibility, orchestration, messaging/taskrunner and internal follow-up paths. Revision 2 itself missed several of those paths; this criterion deliberately does not depend on the list staying complete.

**Phase 2 — a version envelope and one converter.** *Exit criteria:* `session_map.json` carries a version envelope and `_load` dispatches on it rather than sniffing entry shape; `messaging/link.py` owns the `dashboard:` namespace with a builder and a parser that returns a value for it; the at-least-seven classification ladders consume one parsed attribute record, including the classifier already inside `link.py`; no `startswith("dashboard:")` remains in `src/` outside that module and the fast-path branch in `session_surface.py:51`; and the `dashboard:dashboard_` repair at `session_map.py:733-745` is deleted rather than moved, with a test proving the corrupt spelling can no longer be produced.

**Phase 3 — declared capabilities.** *Exit criteria:* the six capability calls in §5.3 ask one of the four named capabilities; a surface declares its set in one place; adding a surface that renders widgets requires no edit to those callers; `has_dashboard_surface` is deleted or reduced to one capability query with its prefix disjunct removed.

**Phase 4 — the opaque id.** *Blocked on open question 1.* *Exit criteria:* a newly minted conversation's id matches `^s_[0-9a-f]{12}$`; every §5.2 field is read from the record rather than parsed from the id; **an inbound message on an existing thread resolves to its conversation through the `(surface, conversation_id, thread_id)` index rather than by constructing a key, and the fork tie-break is a stored fact rather than a key-shape test** (§5.2); `channel_key_for_stem` and its scan are deleted; `history.py:1713`'s `stem.replace("_", ":", 1)` is gone (it would split `s_3f9c…` into `s:3f9c…`); the four fixed identities of §2.1 are enumerated in one place with their conversation-versus-caller role, so none is silently left as a semantic key that code still compares against; and every legacy key still resolves per §7.

## 7. Backward compatibility

Seven compatibility obligation groups follow. None can be skipped in Phase 4.

1. **Transcript filenames on disk.** Either alias via `legacy_stems` — mirroring what `transcript_stems` (`history.py:1729`) and `_path`'s fallback (`:2543-2556`) already do for bare Slack timestamps — or rename with a symlink, which `ConversationLog.list_sessions` skips as a handoff alias (`:3549-3551`). Sidecars share the stem and must move together. The input is not clean: stacked `dashboard_` stems already exist (`:3510-3522`).
2. **The session map.** Needs the envelope first, and must preserve `sid` above all — `SessionMap.prune` (`session_map.py:997`) validates resumability and can delete a stale row.
3. **Approval policy and restricted keys.** Nothing on disk, but every construction site must switch in one commit; a half-migrated set is a silent authorization miss, which `dashboard/chat_persistence.py` already documents for the `dashboard_` / `dashboard:` pair.
4. **The Slack thread reverse index.** Derived, so mostly free — except `channel.thread_id` must be populated for every existing Slack conversation, because the key-derived fallback at `slack/gateway.py:5267-5271` disappears.
5. **`_fold_key`.** Kept for legacy rows; new mints bypass it. §5.1's no-dot condition is what guarantees `canonical_key` can never mistake an opaque id for a bare Slack timestamp.
6. **Persisted foreign keys and reconstruction fallbacks.** The list is larger than four: `CronJob.session_key` (`cron.py:571`, with `f"cron:{job_id}"` fallbacks such as `:1367`); the subagent `conversation_key` (`subagent.py:1388`), whose resume-mismatch path **refuses to execute** (`:5664-5671`) — a hard failure, not a degradation; `ChannelAgent.session_key` (`channel.py:140`), rebuilt from two ids at `:436`; versioned `autonudge.json` records (`autonudge.py:60`, `:387`); the session ledger's `slot_key`; and transcript metadata's `linked_session_key`. Phase 4 must inventory persisted consumers rather than treating the first four found as exhaustive.
7. **The audit log.** Append-only and not rewritten, so `_infer_source` must keep classifying legacy keys — which is why `sel.py:3271`'s `"slack"` fallback becomes `"unknown"` rather than disappearing.

## 8. Security considerations

- **The audit mislabel is a present bug, not a migration risk.** `sel.py:3271` records an unclassifiable non-empty key as `"slack"`. Fixing it to `"unknown"` is in Phase 2's scope and is worth doing whether or not Phase 4 happens.
- **One authorization reader still fails open for unknown key forms.** `mcp_dashboard.py:652-723` documents that an unrecognised non-delegated, non-dashboard key reads as unscoped. Under §5.2 an absent `delegated` field must refuse. Any phase that adds a field must state its failure direction, and fail-closed is the only acceptable answer.
- **Moving authorization state off existing identities is the risk to review.** Restricted-write state still uses constructed dashboard keys, while approval policy now follows `effective_session_key` for dashboard turns and the transport's session key for messaging turns. A half-migration can silently miss either lookup, so Phase 4 needs tests that an absent record cannot widen authorization and that revocation reaches the same identity approval granted.
- **The id is not an authorization token.** It is non-sequential and CSPRNG-generated so that values appearing in a header or `open_slots.json` are not simple counters, but 48 bits must not be treated as an authorization boundary. Sender authorisation stays per surface, where it is today.
- **Phase 1 closes a turn-serialisation gap, not a corruption risk.** Both lock layers are path-keyed, so the transcript cannot tear; what two semaphores buy is two agents taking turns in one conversation, each blind to the other.

## 9. Alternatives considered

- **Keep the status quo.** Defensible for §2.2's development-time costs. Not defensible for Phase 1, a live defect — hence its standing alone.
- **A typed address object everywhere** (option A in the channel-plugin RFC's §9). Rejected there for touching every `sessions.*` call site; that reasoning is unchanged.
- **An opaque key with a canonical grammar** (option B, the decided one). Phases 1–3 need no change to it and are compatible with it. Only Phase 4 departs, and §9's table did not evaluate an attribute store — it compared A, B, the status quo, and a URI scheme.
- **A namespace prefix with an opaque tail.** Rejected in §5.1: it preserves `startswith` as a working expression, so the debt never retires, and the filename fold stays lossy.
- **`uuid4().hex[:12]` instead of a CSPRNG.** Matches more existing precedent, and would be fine; rejected only because the id is externally visible and the cost of `secrets` is zero.
- **Derive `stateful` from `surface`.** Rejected: `session.py:5259` already lets a caller opt a conversation out, so the attribute must be independently settable.

## 10. Open questions

1. **Does the identity become opaque at all?** The channel-plugin RFC's §9 decided that the first segment is the routing authority; §5 here argues the record should be. Phase 4 cannot start until a maintainer rules, and the honest possibility is that Phases 1–3 are worth doing and Phase 4 is not.
2. **Does the dashboard join the channel roster** (`channels.py:65-132`, ten builtin transport descriptors, dashboard absent) as a surface with no transport, or stay a host that owns a namespace? The registry has shipped since revision 2; Phase 3's shape still follows from the answer.
3. **At what granularity is a capability declared** — per surface type, per attached instance, or negotiated at attach? The case that decides it is a chat surface whose thread support differs between a direct message and a channel.
4. **Is `secretary:` dead?** It is declared in two places with no construction site in `src/`. If an external app can mint it, the namespace inventory in §2.1 is incomplete and Phase 2 must account for it.
5. **Should the transcript, not the session, be what surfaces attach to?** rfc-append-only-session-transcript.md owns the write path, and Phase 1's two-semaphores-one-file behaviour is visible from both documents. If that RFC proceeds, the two need to agree on where turn-level serialisation lives before Phase 4 moves identity.

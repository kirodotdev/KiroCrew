---
title: Session Address Model — an opaque conversation identity with an attribute store
status: draft
author: nrb
created: 2026-08-17
last-audited: 2026-08-18
audited-at: fa6261c4f
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
- Created: 2026-08-17 · Revised: 2026-08-18
- Related: rfc-channel-plugin-architecture.md — its §9 amendment decided the opposite of §5 here, and §9.4's "exactly one builder/parser" is Phase 2 below. rfc-append-only-session-transcript.md owns the transcript write path this document only reads.

Every claim below was measured at `fa6261c4f`. Paths are repo-relative.

## 1. Summary

A conversation's identity is a meaningful string whose first segment names the surface it started on: `slack:<ts>`, `dashboard:chat-12-1786…`, `cron:<job_id>`. Roughly 30 code sites read that string's shape to decide something — whether the conversation survives a restart, what its approval policy is, whether it may write to memory, how an audit event labels it, whether it can be nudged, where a reply is delivered.

This proposes that the identity carry no meaning at all — an opaque `s_<12 hex>` — and that every attribute currently inferred from the string become a field on the record `session_map.json` already keeps for each conversation. Four surface identities are deliberately not conversations and have no such record (§2.1); the scheme reserves rather than migrates them. Four phases, each independently shippable. The first is a defect fix that stands alone; the fourth changes identity and is blocked on a maintainer decision, because it contradicts a rule another RFC already settled.

## 2. Motivation

### 2.1 What the identity does today

Nineteen namespaces are in use. Nine are chat surfaces, declared in one tuple at `messaging/link.py:45` (`slack`, `discord`, `telegram`, `whatsapp`, `webex`, `wecom`, `teams`, `weixin`, `unified`). Ten are local: `dashboard:`, `cron:`, `subagent:`, `taskrunner:`, `channel:`, `side:`, `hook:`, `wf-pool:`, plus the `_bg`/`_hb` singletons. A twentieth, `secretary:`, is declared at `session.py:329` and `link.py:114` and has no construction site anywhere in `src/`.

Four of those names are not conversations at all, and this distinction matters for §5.2. `cli_chat`, `_bg`, `_hb` and the `_host` sentinel are **singleton surface identities** — one fixed string standing for "a human at a terminal" or an internal actor, reused across invocations, carrying no per-conversation state. `cli_chat` never reaches `SessionManager.get_or_create`: `cli_chat.py:112` builds its provider directly, so no `session_map.json` entry and no transcript row is ever created for it. It is nonetheless a decision input — `context.py:669` compares against it by equality, and `computer_use/cli.py:96-102` uses it as the gate identity for a terminal caller.

Two grammars coexist. Chat surfaces mostly use `{surface}:{agent}:{chat_type}:{scope…}[:genN]`, built at `link.py:377-432`, where `scope` is a path rather than a single segment. Slack predates it and uses `slack:<thread_ts>` plus a legacy bare `<thread_ts>` form. `dashboard:` has no constant at all — it is a bare literal at dozens of sites.

There is one canonical parser, `link.py:283-321`, and it is strict by design: fewer than four segments or an unrecognised surface returns `None`. So bare Slack timestamps, two-segment `slack:<ts>`, every `dashboard:` key and `channel:{id}:{agent}` all deliberately fail to parse. The namespaces that fail the parser are the ones with the most behaviour attached.

The August 2026 work is the relevant prior art. Before PR #1366, a conversation that started in a chat app became **two** conversations when its dashboard tab opened, because a tab could only write a key beginning `dashboard:`, so it read one transcript and wrote another. That PR pointed the tab at the chat app's own session, deleted the 30-second reconciler that had been copying between the two, and replaced about two dozen `session_key.startswith("dashboard:")` capability tests with one function, `has_dashboard_surface` (`session_surface.py:42`). Its module docstring states the diagnosis exactly: the prefix test *"answers 'where did this start?' and not 'can the user see a dashboard right now?'"*

That fixed the symptom and left the cause. The identity still encodes origin, and the code still reads it.

### 2.2 The problems

**The encoding is lossy and recovery is a linear scan.** `history._safe_key` (`history.py:1222`) folds every character outside `[\w\-.]` to `_` to build the transcript filename, so `slack:<ts>` becomes `slack_<ts>.jsonl`. The fold is not invertible — nothing says which underscores were colons, and an agent name may contain one. Recovering the real key means iterating the session map and re-folding every entry until one matches (`session_map.py:1600-1621`), whose own docstring says the fold "is NOT reversible" and that a miss must be left unbound rather than guessed. The map that scan walks is bounded only by "does the transcript file still exist" (`prune`, `:954-1009`), with carve-outs that make any channel-bound entry immortal.

**Conversion is spread across roughly 49 sites** — about 31 named helpers and at least 18 inline prefix strips. The main ones each carry a docstring warning against the others: `_history_key_for` (`dashboard/chat_utils.py:432`, 49 references), `effective_session_key` (`:564`, 79 references), `slot_history_key` (`:524`), `slot_transcript_key` (`:505`), `dashboard_slot_key` (`:447`), `_normalize_slot_key` (`dashboard/state.py:934`), `channel_slot_name` (`dashboard/channel_slots.py:106`), `_fold_key` (`session.py:781`, called from 30+ sites).

**The same classification is reimplemented six times** — `validation.py:175-191`, `sel.py:1038-1088`, `mcp_gateway/claim.py:57-67`, `mcp_gateway/stub.py:364-373` (whose docstring admits it mirrors the previous one), `dashboard/handlers/sessions.py:1588-1606`, and `messaging/link.py:146-171`. The sixth is inside the module §9.4 of the channel-plugin RFC nominates as the single owner of key grammar.

**Two of those readers fail open, and one mislabels an audit event.** `mcp_dashboard.py:500-508` says its delegated-caller prefix list is knowingly incomplete and that a new key form "will read as unscoped until it is added here." `sel.py:1088` returns `"slack"` as the fallback for an unrecognised key, so a conversation the audit log cannot classify is recorded as a Slack conversation rather than as unknown.

**Both spellings already leak into stored keys.** `session_map.py:701` carries a repair for the corrupted double prefix `dashboard:dashboard_`, which exists only because more than one place builds the name.

**And the store the string duplicates is already the authority.** `SessionMap` holds the unfolded keys and sixteen fields per conversation. Everything §2.1 describes is a second, lossy copy of what that store knows exactly.

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
| no `.` | `\d+\.\d+` is `_SLACK_TS_RE` (`link.py:28`); `canonical_key` runs on every map write (`session_map.py:889`) and would rewrite a dotted id into `slack:<id>` |
| no leading `_` | collides with the `_bg` / `_hb` / `_host` singletons |
| not `^chat-\d+-\d+$` | matched as a telemetry slot at `link.py:133` |
| not `(?:dashboard_)?chat-\d+-\d+$` | matched at `state.py:905` |
| lowercase hex only | no path traversal, mirroring the artifact-slug guard at `apps/builtins/artifacts.py:543` |

`secrets.token_hex` rather than `uuid4().hex[:12]` because the id travels in an `X-Session-Key` header and is persisted into `open_slots.json`; a CSPRNG costs nothing here and removes the question. Width and the typed prefix follow existing convention — `uuid4().hex[:12]` at `aidlc/models.py:10`, `f"c_{secrets.token_hex(4)}"` at `apps/builtins/issue_radar/backend/crew_store.py:649`, and the already-validated lowercase-hex id shape `^[a-f0-9]{1,16}$` at `validation.py:204`.

### 5.2 The record

`session_map.json` is today a flat `key → entry` object with **no envelope and no version marker** (`json.dumps(self._data)`, `session_map.py:535`); migration is shape-sniffing inside `_load` (`:363-431`). An opaque id is indistinguishable from a legacy dashboard slot key by inspection, so **a version envelope is a prerequisite, not a nicety** — `autonudge.py:52` already has `_STORE_VERSION = 1` and is the model.

The entry gains these fields. Each replaces exactly one shape-read, and each has one writer:

| Field | Replaces | Writer |
|---|---|---|
| `surface` | `sel.py`, `context.py`, `validation.py` prefix ladders | mint site |
| `surfaces[]` | nothing today — the attach set is in-memory only (`session_surface.py:28`) | attach/detach |
| `stateful` | `_STATELESS_PREFIXES` (`session.py:324-338`) | mint site, mutable thereafter |
| `restricted` | `f"dashboard:{name}"` written at 5 sites, read at `handlers/_shared.py:1380` | the restrict/unrestrict handler |
| `approval_policy` | `f"dashboard:{slot_key}"` at 7 sites | the approval handler |
| `delegated` | `_DELEGATED_CALLER_PREFIXES` (`mcp_dashboard.py:509`) | mint site |
| `nudgeable`, `nudge_mode` | `binding_key_for` + `is_channel_key` (`autonudge.py:87-111`) | mint site |
| `agent` | the ladder at `handlers/sessions.py:1588-1606` | mint site |
| `channel.thread_id` | `slack/gateway.py:3711-3713`, which recovers the delivery thread *from the key* | the surface on attach |
| `legacy_stems[]` | the reverse scan `channel_key_for_stem` | migration only |

Two shapes must be preserved rather than simplified. `channel.thread_id` is many-to-one: `_rebuild_thread_index` (`slack/gateway.py:439`) already tolerates two conversations claiming one thread. And `stateful` must be a stored mutable field, not derived from `surface`, because `_is_continuable_key` (`session.py:903`) already lets a caller opt out.

Every reader gets an explicit miss. `sel.py:1088`'s `"slack"` fallback becomes `"unknown"`; `mcp_dashboard.py`'s fail-open list becomes a refusal on absent `delegated`.

**The record must also answer the inbound direction, which the table above does not.** Every field there replaces an *outbound* shape-read — given a conversation, decide something about it. Inbound is the opposite: a second Slack message arrives on a thread and the system must find the conversation that already owns it. Today that is free, because the key is constructible from the thread timestamp; with a random id it is not, and a conversation would fork on every reply. So the reverse index `_thread_to_session` (`session_map.py:436-478`), today a derived convenience rebuilt on load, becomes **load-bearing**: `(surface, conversation_id, thread_id) → session id` is the lookup that makes an opaque id usable at all, and it has to be maintained on attach and detach rather than reconstructed from key shapes.

That index carries a shape-read the §2.2 inventory missed, and it is the sharpest one in the codebase. Its tie-break (`session_map.py:445-449`) resolves two entries claiming one thread by asking whether a key *derives from* that thread — `"A slack:<ts> key whose ts IS the thread is the fork … any other key holds the real conversation."` Under an opaque id no key derives from anything, so the heuristic that tells the fork from the real conversation evaporates. It must become an explicit stored fact on the binding — which entry originated the thread — before any id is minted.

### 5.3 Capabilities

`has_dashboard_surface` has 7 call sites. Two (`chat_utils.py:475`, `:479`) sit inside the slot-name resolver and are not capability questions. The other five ask four distinct questions, which become four declared capabilities:

| Capability | Asked at | Gates |
|---|---|---|
| `can_render_rich_html` | `context.py:1628` | the widget block in the system prompt |
| `can_render_interactive_card` | `context.py:2672` and `mcp_tools/control.py:745` | the question tool, at the prompt and the tool boundary — two sites, one name, must not diverge |
| `has_mutable_slot` | `session_directive_apply.py:89` | whether a directive may retarget project or CWD |
| `can_inject_turn` | `subagent.py:1677` | orphan notice as a turn, or fall back to a DM digest |

A surface declares its set on attach. The query is "does any attached surface have capability X", so a surface that renders *better* than the dashboard is expressible — which the current boolean cannot say.

### 5.4 Routing without parsing

The sharpest dependency is `slack/gateway.py:3711-3713`, whose comment reads `# Canonical keys embed the thread root ts.` and recovers the delivery target from the key; `:4170` splits a key into `(channel, ts)` to build a permalink. Under this design both read `channel.conversation_id` and `channel.thread_id` from the record. `link.py:596` skips origin-mirror binding when a key is `unified:` because that name identifies no single conversation — that becomes an absent `channel` record rather than a prefix test.

## 6. Migration plan

Each phase is independently shippable and independently abandonable.

**Phase 1 — refuse a turn from an unbound tab.** Fixes a live defect; depends on nothing else here. When `channel_key_for_stem` misses, `surface_channel_session` deliberately surfaces the tab unbound (`channel_slots.py:291-309`), which is correct, but nothing then stops a turn: `effective_session_key` falls back to `dashboard:<stem>` (`chat_utils.py:580`), a second session with its own turn semaphore, while the transcript is correctly routed back to the chat app's file. The file cannot tear — `ConversationLog._locked` (`history.py:1771`) holds an in-process RLock plus a cross-process advisory flock, and *both* layers are keyed on the resolved path (`:1709`, `:1785`). What is unserialised is the turn, and the reply never reaches the chat app.

*Exit criteria:* a turn started from a slot with `channel_origin` true and an empty `linked_session_key` is refused with a reason the user can see; a test pins the refusal; and the guard sits at **`_run_chat`'s entry** rather than at its callers. That placement is the criterion, not a convenience: every path reaches `_run_chat`, and enumerating callers has now been got wrong twice — revision 1 of this document named `ws.py`, which handles five subscribe/focus message types and never drives a turn, and revision 2 named four callers and missed `/v1/chat/completions` (`dashboard/openai_compat.py:385`, which calls `_run_chat` directly and can target a channel-origin slot). The five paths that do reach it today are `api_chat` (`chat_handlers.py:153`), `api_chat_slot_continue` (`:1930`), `chat_regenerate.py:98` and `:256`, `chat_rewind.py:256`, and that OpenAI-compatible endpoint — a list this criterion deliberately does not depend on staying complete.

**Phase 2 — a version envelope and one converter.** *Exit criteria:* `session_map.json` carries a version envelope and `_load` dispatches on it rather than sniffing entry shape; `messaging/link.py` owns the `dashboard:` namespace with a builder and a parser that returns a value for it; the six classification ladders become one call, including the one already inside `link.py`; no `startswith("dashboard:")` remains in `src/` outside that module and the fast-path branch in `session_surface.py:51`; and the `dashboard:dashboard_` repair at `session_map.py:701` is deleted rather than moved, with a test proving the corrupt spelling can no longer be produced.

**Phase 3 — declared capabilities.** *Exit criteria:* the five capability call sites in §5.3 ask one of the four named capabilities; a surface declares its set in one place; adding a surface that renders widgets requires no edit to those five sites; `has_dashboard_surface` is deleted or reduced to one capability query with its prefix disjunct removed.

**Phase 4 — the opaque id.** *Blocked on open question 1.* *Exit criteria:* a newly minted conversation's id matches `^s_[0-9a-f]{12}$`; every §5.2 field is read from the record rather than parsed from the id; **an inbound message on an existing thread resolves to its conversation through the `(surface, conversation_id, thread_id)` index rather than by constructing a key, and the fork tie-break is a stored fact rather than a key-shape test** (§5.2); `channel_key_for_stem` and its scan are deleted; `history.py:3469`'s `path.stem.replace("_", ":", 1)` is gone (it would split `s_3f9c…` into `s:3f9c…`); the four singleton surface identities of §2.1 are enumerated in one place as reserved non-conversation names, so none is silently left as a semantic key that code still compares against; and every legacy key still resolves per §7.

## 7. Backward compatibility

Seven surfaces, each with its obligation. None can be skipped in Phase 4.

1. **Transcript filenames on disk.** Either alias via `legacy_stems` — mirroring what `transcript_stems` (`history.py:1244`) and `_path`'s fallback (`:1963`) already do for bare Slack timestamps — or rename with a symlink, which `history.py:2859` already skips as a handoff alias. Sidecars share the stem (`:1986`, `:2029`) and must move together. The input is not clean: stacked `dashboard_` stems already exist (`:2843`).
2. **The session map.** Needs the envelope first, and must preserve `sid` above all — `prune` (`:988`) stats `{sid}.json` and deletes the row on a miss.
3. **Approval policy and restricted keys.** Nothing on disk, but every construction site must switch in one commit; a half-migrated set is a silent authorization miss, which `chat_persistence.py:515` already documents for the `dashboard_` / `dashboard:` pair.
4. **The Slack thread reverse index.** Derived, so mostly free — except `channel.thread_id` must be populated for every existing Slack conversation, because the key-derived fallback at `gateway.py:3711` disappears.
5. **`_fold_key`.** Kept for legacy rows; new mints bypass it. §5.1's no-dot condition is what guarantees `canonical_key` can never mistake an opaque id for a bare Slack timestamp.
6. **Four persisted foreign keys, each with a reconstruction fallback that stops working.** `CronJob.session_key` (`cron.py:273`, rebuilt as `f"cron:{job_id}"` at `:890`); the subagent `conversation_key` (`subagent.py:1139`), whose mismatch path **refuses to execute** (`:5053`) — a hard failure, not a degradation; `channel.py:119`, rebuilt from two ids at `:415`; and `autonudge.json`'s `slot_key` (`autonudge.py:263`), which is the one store already versioned and so the one that migrates cleanly.
7. **The audit log.** Append-only and not rewritten, so `_infer_source` must keep classifying legacy keys — which is why `sel.py:1088`'s `"slack"` fallback becomes `"unknown"` rather than disappearing.

## 8. Security considerations

- **The audit mislabel is a present bug, not a migration risk.** `sel.py:1088` records an unclassifiable key as `"slack"`. Fixing it to `"unknown"` is in Phase 2's scope and is worth doing whether or not Phase 4 happens.
- **Two readers fail open today.** `mcp_dashboard.py:500-508` says so in a comment: an unrecognised key reads as unscoped. Under §5.2 an absent `delegated` field must refuse. Any phase that adds a field must state its failure direction, and fail-closed is the only acceptable answer.
- **Moving authorization state off a constructed string is the risk to review.** Restricted-write and approval policy are keyed today by two independent places spelling `f"dashboard:{name}"` identically. That is fragile, but its failure mode is visible. A lookup that silently misses is not, so Phase 4 needs a test that a conversation with no record is treated as restricted rather than unrestricted.
- **The id is not an authorization token.** It is unguessable so that a value appearing in a header or `open_slots.json` is not enumerable, but nothing should authorize on possession of it. Sender authorisation stays per surface, where it is today.
- **Phase 1 closes a turn-serialisation gap, not a corruption risk.** Both lock layers are path-keyed, so the transcript cannot tear; what two semaphores buy is two agents taking turns in one conversation, each blind to the other.

## 9. Alternatives considered

- **Keep the status quo.** Defensible for §2.2's development-time costs. Not defensible for Phase 1, a live defect — hence its standing alone.
- **A typed address object everywhere** (option A in the channel-plugin RFC's §9). Rejected there for touching every `sessions.*` call site; that reasoning is unchanged.
- **An opaque key with a canonical grammar** (option B, the decided one). Phases 1–3 need no change to it and are compatible with it. Only Phase 4 departs, and §9's table did not evaluate an attribute store — it compared A, B, the status quo, and a URI scheme.
- **A namespace prefix with an opaque tail.** Rejected in §5.1: it preserves `startswith` as a working expression, so the debt never retires, and the filename fold stays lossy.
- **`uuid4().hex[:12]` instead of a CSPRNG.** Matches more existing precedent, and would be fine; rejected only because the id is externally visible and the cost of `secrets` is zero.
- **Derive `stateful` from `surface`.** Rejected: `session.py:903` already lets a caller opt a conversation out, so the attribute must be independently settable.

## 10. Open questions

1. **Does the identity become opaque at all?** The channel-plugin RFC's §9 decided that the first segment is the routing authority; §5 here argues the record should be. Phase 4 cannot start until a maintainer rules, and the honest possibility is that Phases 1–3 are worth doing and Phase 4 is not.
2. **Does the dashboard join the channel roster** (`channels.py:50-56`, seven members, dashboard absent) as a surface with no transport, or stay a host that owns a namespace? Phase 3's shape follows from the answer.
3. **At what granularity is a capability declared** — per surface type, per attached instance, or negotiated at attach? The case that decides it is a chat surface whose thread support differs between a direct message and a channel.
4. **Is `secretary:` dead?** It is declared in two places with no construction site in `src/`. If an external app can mint it, the namespace inventory in §2.1 is incomplete and Phase 2 must account for it.
5. **Should the transcript, not the session, be what surfaces attach to?** rfc-append-only-session-transcript.md owns the write path, and Phase 1's two-semaphores-one-file behaviour is visible from both documents. If that RFC proceeds, the two need to agree on where turn-level serialisation lives before Phase 4 moves identity.

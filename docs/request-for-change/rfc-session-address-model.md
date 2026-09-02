---
title: Session Address Model — one authoritative session per conversation, surfaces attach to it
status: partial
author: nrb
created: 2026-08-17
last-audited: 2026-08-17
audited-at: b23ab77af
doc-pr: 4077
implementation-prs: [1366, 1455, 1480, 1539, 1921]
tracking-issues: []
supersedes: []
superseded-by: []
---
# RFC: Session Address Model — one authoritative session per conversation, surfaces attach to it

- Status: partial — the dashboard half of the address rule is shipped and load-bearing. A conversation that starts in a chat app is no longer copied when its dashboard tab opens: the tab resolves and holds the conversation's real key, and one function replaced the ~two dozen name-prefix tests that decided what a conversation was allowed to do. What is **not** done: a conversation's identity still encodes its origin surface in the key string, that encoding is destroyed by the filename fold and has to be recovered by a linear scan, surface capability is still a single boolean, and the failure path when the scan misses silently starts a second session against the same transcript file.
- Author: nrb
- Created: 2026-08-17
- Related: rfc-channel-plugin-architecture.md (its §9 amendment decided the address rule this document implements half of, and left the dashboard-as-surface work explicitly out of scope), rfc-append-only-session-transcript.md (owns the transcript write path this document only reads)

Everything measured below was read at `b23ab77af`. Paths are repo-relative.

## 1. Problem statement

A conversation in Kiro Crew has one identity, the `session_key`. A dashboard tab is a `_ChatSlot`, and its name is that key with every `:` folded to `_`, because a slot name has to match a transcript filename on disk. Chat apps — seven of them — attach to conversations and detach from them.

Before PR #1366 (3 August 2026) the two directions were not symmetric. A conversation sent from the dashboard to a chat app stayed one conversation. A conversation that *started* in a chat app became **two** the moment its dashboard tab opened: the tab could only write a key beginning `dashboard:`, so it read one file and wrote another. It ran a second agent process seeded with the last 50 messages, and PRs #808 (29 July) and #1112 (2 August) added a 30-second job that compared the two transcripts and copied across the difference. That copying stopped for good the first time anything else wrote to the tab, and nothing said it had stopped.

The cause was not the chat app. It was that the code asked *"where did this conversation start?"* — `session_key.startswith("dashboard:")`, written out at about two dozen call sites — when what it meant to ask was *"can the user see a dashboard right now?"* For a chat-app conversation with its tab open those two questions have different answers, and every feature behind that test was denied: clickable choices, question cards, switching the project directory.

PR #1366 fixed both halves and deleted the reconciler. What it did not do is remove the reason the bug was reachable, which is that a conversation's identity carries its origin in its name and the name is the primary index. Four measurements at `b23ab77af`:

- **The encoding is lossy, and the recovery is a scan.** `history._safe_key` (`src/kiro_crew/history.py:1222`) folds `:` to `_`, and the fold is not invertible — given `discord_kirocrew_direct_123` nothing says which underscores were colons, and an agent name may contain one. So the real key is recovered by iterating the session map and re-folding every entry until one matches (`src/kiro_crew/session_map.py:1613-1622`). That map is bounded only by "does the transcript file still exist" (`prune`, `src/kiro_crew/session_map.py:953`) with carve-outs that make a channel-bound entry immortal, and the file already names the hazard at `src/kiro_crew/session_map.py:972-975`.
- **Conversion is spread across ~33 sites.** Twenty-three named helpers convert between a slot name, a session key and a filename stem, and about ten more strip the `dashboard:` prefix inline. Each of the main ones carries a docstring warning against the others: `_history_key_for` (`src/kiro_crew/dashboard/chat_utils.py:432`, 47 references), `effective_session_key` (`:564`, 76 references), `slot_history_key` (`:524`), `slot_transcript_key` (`:505`), `dashboard_slot_key` (`:447`), `_normalize_slot_key` (`src/kiro_crew/dashboard/state.py:934`), `channel_slot_name` (`src/kiro_crew/dashboard/channel_slots.py:106`), `_fold_key` (`src/kiro_crew/session.py:781`).
- **The same classification is reimplemented five times.** The ladder that turns a key into a session *type* exists independently at `src/kiro_crew/validation.py:182-190`, `src/kiro_crew/sel.py:1052-1061`, `src/kiro_crew/mcp_gateway/claim.py:59-68`, `src/kiro_crew/mcp_gateway/stub.py:368`, and `src/kiro_crew/slack/sessions_view.py:67`. The third one's docstring admits it mirrors the fourth.
- **Both spellings already leak into stored keys.** `src/kiro_crew/session_map.py:701` carries a repair for the corrupted double prefix `dashboard:dashboard_`, which only exists because a name is built in more than one place.

## 2. What shipped

PR #1366 replaced every prefix test with one function, `has_dashboard_surface` (`src/kiro_crew/session_surface.py:42`). The dashboard republishes the set of conversations that currently have a tab open; the function tests membership in that set. It has **seven** callers, each deciding one capability:

| caller | decides |
|---|---|
| `src/kiro_crew/context.py:1628` | whether the prompt carries the widget block |
| `src/kiro_crew/context.py:2672` | whether the prompt mentions the question tool |
| `src/kiro_crew/dashboard/chat_utils.py:475`, `:479` | which slot name, if any, displays a conversation |
| `src/kiro_crew/dashboard/session_directive_apply.py:89` | whether a directive may retarget a session |
| `src/kiro_crew/mcp_tools/control.py:745` | whether the question tool answers or refuses |
| `src/kiro_crew/subagent.py:1583` | whether an orphan notice goes to the tab or a DM |

Two properties make it usable from anywhere. The module imports nothing from `kiro_crew`, so prompt building, the audit log and the MCP gateway can all call it. And the old prefix test survives as its first branch (`src/kiro_crew/session_surface.py:51`, accepting both `dashboard:` and `dashboard_`), which keeps a genuinely dashboard-born conversation recognisable before the dashboard has published anything and makes an empty set fall back to previous behaviour rather than to a wrong answer.

PRs #1455, #1480, #1539 and #1921 closed the consequences of one file now having two writers: a reply no longer starts a second conversation, a message keeps its real origin across save and reload, a chat-app message appears in an open tab in arrival order, and a tab's reads and writes stay on one file.

This is the dashboard half of §9 rule 1 of rfc-channel-plugin-architecture.md, which reads: *"`session_key` is THE address for every session … A slot is a dashboard-local alias resolved to a key at the dashboard edge."* `linked_session_key` on the slot is that alias, resolved once when the tab is surfaced. `_cancel_target` (`src/kiro_crew/dashboard/chat_handlers.py:1573`) applies the same rule to stop, and its docstring records the silent no-op that the rule prevents.

## 3. Where the design is incomplete

### 3.1 An unbound tab silently starts a second session against the same file

When the scan in §1 misses — a pruned map entry, or a thread older than the map — `surface_channel_session` deliberately surfaces the tab **unbound** rather than binding it to a guess, because a wrong key would answer the user from a session the chat app never reads (`src/kiro_crew/dashboard/channel_slots.py:291-309`). That choice is correct. What follows it is not.

Nothing prevents a turn from that tab. `POST /api/chat` has no check on the binding, and `_run_chat`'s first statement resolves the target with `effective_session_key` (`src/kiro_crew/dashboard/chat_runner.py:3642`), which falls back to `_history_key_for(slot.key)` (`src/kiro_crew/dashboard/chat_utils.py:578`) and yields `dashboard:<stem>`. Three consequences:

- **Two turn locks, one transcript.** The turn semaphore is a field on the session object (`src/kiro_crew/session.py:662`) in a registry keyed by session key (`:874`), so two keys are two locks and both turns can run at once. Meanwhile `slot_history_key`'s `channel_origin` branch (`src/kiro_crew/dashboard/chat_utils.py:559-560`) correctly routes the *transcript* back to the chat app's own file. The file itself is safe: `ConversationLog._locked` (`src/kiro_crew/history.py:1771`) holds an in-process RLock and a cross-process advisory `flock` keyed on the resolved file path (`:1785`), so appends from either writer — including another process — are serialised and the file cannot be torn. What is unserialised is the *turn*: two agents interleave turns into one conversation history with no mutual exclusion above the append, and neither knows the other spoke.
- **The reply does not leave the browser.** The outbound binding lives on the chat app's key, not on `dashboard:<stem>`.
- **Nobody is told.** The unbound path logs once at `info` when the tab is surfaced (`src/kiro_crew/dashboard/channel_slots.py:299`), never at send time. The frontend cannot tell the two states apart: `website/src/` contains no reference to `linked_session_key` or `channel_origin`, and the sidebar derives its badge from the slot *name* (`website/src/utils/channelOrigin.ts`).

This is the degraded path being read-only by intent while nothing enforces the intent. It is worth fixing on its own, independently of everything else in this document.

### 3.2 Surface capability is one boolean

`has_dashboard_surface` answers a yes/no question, and where it answers no the product falls back to plain text. That makes every non-dashboard surface one bucket defined by its least capable member, and asserts each of them can at least render text. It is a reasonable stand-in and it is not what the design wants: rfc-channel-plugin-architecture.md §9 already commits to "one builtin surface with **declared capabilities**". A boolean cannot express a surface that renders *better* than the dashboard, which is what arrives when other Kiro surfaces become attachable.

### 3.3 The dashboard is not a channel, and so every mechanism hand-rolls it

`src/kiro_crew/channels.py:50-56` is "the ONE place that knows every channel" and lists seven. The dashboard is not among them, and `is_channel_session_key` (`src/kiro_crew/messaging/link.py:84`) excludes its namespace — which is exactly what lets `channel_slots.py:291` refuse to bind a non-channel key. Yet the `dashboard:` namespace is treated like a channel namespace by approval policy, restricted-key bookkeeping, `_STATELESS_PREFIXES` membership (`src/kiro_crew/session.py:321-337`, where `dashboard:` is absent and therefore stateful by omission) and all five copies of the classification ladder. `src/kiro_crew/messaging/link.py` owns a real builder (`:202`) and parser (`:283`) and every chat-app package imports it, but it owns no `dashboard:` grammar at all — only the legacy shim at `:435`. So the namespace with the most behaviour attached to it is the one with no canonical parser.

### 3.4 Security state is keyed by a string two places must spell identically

Restricted-key bookkeeping gates memory, artifact and lesson writes at roughly fifty call sites. The write side constructs the key as a literal (`src/kiro_crew/dashboard/chat_handlers.py:3877-3879`, `src/kiro_crew/dashboard/chat_persistence.py:482-483`) and the read side tests exact membership, then strips a prefix inline and special-cases the literal `"dashboard:ui"` (`src/kiro_crew/dashboard/handlers/_shared.py:1375-1384`). Approval policy is the same shape: set with `f"dashboard:{slot_key}"` at six sites in `src/kiro_crew/dashboard/chat_handlers.py` (`:3992`, `:3997`, `:4014`, `:4022`, `:4048`, `:4057`), stored per session (`src/kiro_crew/session.py:663`), read through `_fold_key`, which by design never rewrites a non-Slack namespace (`:795-796`).

Neither mechanism performs a prefix *test*, so this is not a surviving capability gate. It is something narrower and more fragile: two independent places agree on a spelling, and the security property holds because they happen to agree. `slot_history_key` (`src/kiro_crew/dashboard/chat_utils.py:541-543`) documents the unbound tab keeping its `dashboard:` key specifically so this bookkeeping stays intact, which makes the fragility load-bearing on the degraded path from §3.1.

## 4. Goals

1. A conversation has one identity, and reading that identity tells you nothing you have to act on.
2. What a conversation may do is a property of the surfaces currently attached to it, declared by those surfaces.
3. Attaching or detaching a surface never changes identity, never forks a transcript, and never widens or narrows a security decision by accident.
4. Adding a surface costs a declaration, not an edit to every mechanism that has to know about it.

## 5. Non-goals

- Changing what the chat apps can do to each other. They still cannot reach one another, and this document does not propose that they should.
- Changing who may message the agent. Each app authorises its own senders; none of that is touched.
- Owning the transcript write path. rfc-append-only-session-transcript.md owns it; this document only reads.
- Rewriting the existing `slack:` and `dashboard:` keys in place. Any migration here has to tolerate them, exactly as §9 of rfc-channel-plugin-architecture.md already accepts.

## 6. Proposed direction: an opaque id with an attribute store

A conversation gets an opaque, meaningless id. Everything currently inferred from the key's shape becomes a record: origin surface, currently attached surfaces, each attached surface's declared capabilities, statefulness, approval policy, restricted-write state.

The argument for it is that the store already exists and is already the authority. `SessionMap` holds the unfolded keys and is the only thing that can answer what a folded stem means (`src/kiro_crew/session_map.py:1606`). The name is a second, lossy copy of what the store knows exactly. Every cost in §1 and §3 comes from treating the copy as the index: the scan exists because the copy is lossy, the ~33 converters exist because the copy has several spellings, the five ladders exist because parsing the copy is easy enough to do locally, and the double-prefix repair at `src/kiro_crew/session_map.py:701` exists because two places built the copy differently. An opaque id makes the fold a non-event, because a name with no colons in it survives becoming a filename unchanged.

The argument against it is migration cost, and it is real: every existing key is a meaningful string, `_STATELESS_PREFIXES` routes on the first segment, and the security keying in §3.4 is spelled out at a dozen write sites. This is why the phases below put the store first and the opaque id last, and why the last one is blocked on a decision rather than scheduled.

## 7. Relationship to rfc-channel-plugin-architecture.md

That document's §9 decided **option B** — an opaque key with a canonical grammar and a single builder/parser — on 29 July 2026, five days before PR #1366 merged. This document does not contradict that decision; §2 above is its dashboard half. Three differences are worth stating plainly:

1. **Order.** §9 ends by putting "the eventual dashboard-as-surface unification (dashboard = host + one builtin surface with declared capabilities)" explicitly out of scope for its PRs ①–⑤. PR #1366 did that unification early, before the registry and parser it was scheduled to sit on. The work in §2 therefore arrived out of the planned sequence.
2. **Rule 4 is further away, not closer.** §9 wants exactly one builder/parser module, with the key munging dying in one place. There are now 23 named converters and five classification ladders. Nothing regressed — those predate #1366 — but the debt rule 4 exists to retire has not shrunk.
3. **An option §9 did not evaluate.** Its table compares a typed address object, an opaque key with a canonical grammar, the two-level status quo, and a URI scheme. It does not consider an opaque id with **no** grammar plus an attribute store, which is §6 here. Under option B the first segment is deliberately the routing authority; §6 argues that routing should read the store. That is a genuine disagreement with a decided option, and it belongs to the maintainers to settle.

## 8. Phases

Each phase is independently shippable and independently abandonable.

**Phase 1 — refuse a turn from an unbound tab.** Fixes §3.1. Depends on nothing here.
Exit criteria: a turn started from a slot with `channel_origin` true and an empty `linked_session_key` is refused with a reason the user can see, rather than starting a `dashboard:<stem>` session; a test pins the refusal; the other turn entry points (the socket path and `api_chat_slot_continue`, `src/kiro_crew/dashboard/chat_handlers.py:1821`) are covered by the same guard or shown not to reach it.

**Phase 2 — one converter, one classifier.** Fixes §3.3's parsing half and §1's spread.
Exit criteria: `src/kiro_crew/messaging/link.py` owns the `dashboard:` namespace as well as the chat-app ones; the five classification ladders become one call; no `startswith("dashboard:")` remains in `src/` outside that module and the fast-path branch in `src/kiro_crew/session_surface.py`; the double-prefix repair at `src/kiro_crew/session_map.py:701` is deleted rather than moved, with a test proving the corrupted spelling can no longer be produced.

**Phase 3 — declared capabilities instead of one boolean.** Fixes §3.2.
Exit criteria: each of the seven call sites in §2 asks a named capability ("can any attached surface render a widget?") rather than "is a dashboard attached?"; a surface declares its capabilities in one place; adding a surface that renders widgets requires no edit to those seven sites; `has_dashboard_surface` is either deleted or reduced to one capability query.

**Phase 4 — opaque id and attribute store.** Blocked on open question 1.
Exit criteria: a newly created conversation's id contains no surface segment; routing, statefulness and security state are read from the store rather than parsed from the id; `channel_key_for_stem` and its scan are deleted; existing meaningful keys still resolve.

## 9. Security considerations

- **The keying in §3.4 is the main one.** Moving approval policy and restricted-write state off a constructed key string and onto a session attribute is a security-relevant change with a real failure mode: a session that silently reads as unrestricted because a lookup missed, where today it reads as unrestricted because a spelling differed. Phase 4 must state which way it fails and test that direction explicitly. Fail-closed is the only acceptable answer.
- **§3.1 is a concurrency gap above the file, not a corruption risk in it.** The per-file lock in `src/kiro_crew/history.py:1771` serialises every append, so the transcript cannot be torn. What two semaphores over one conversation buy is two agents taking turns in it simultaneously, each blind to the other. Phase 1 closes that by refusing the turn; anything that instead *binds* the unbound tab has to explain why a guessed key is safe, which §1's fold says it is not.
- **Capability is not authorisation.** Phase 3 changes what a surface can render, and must not become a path to changing who may drive a conversation. Sender authorisation stays per app, where it is today.
- **The audit log reads the key's shape.** `src/kiro_crew/sel.py:1052-1061` derives its session-type label from the prefix, as do the two MCP caller-block builders (`src/kiro_crew/mcp_gateway/claim.py:59-68`, `src/kiro_crew/mcp_gateway/stub.py:368`). An opaque id removes that label's source, so Phase 4 has to supply it from the store or the audit trail loses a field.

## 10. Alternatives considered

- **Keep the status quo.** Defensible for §3.2 and §3.3, which are costs paid at development time. Not defensible for §3.1, which is a live defect — hence Phase 1 standing alone.
- **A typed address object everywhere** (§9's option A). Rejected there for touching every `sessions.*` call site; that reasoning is unchanged.
- **An opaque key with a canonical grammar** (§9's option B, the decided one). Already the direction of Phases 1–3, which need no change to it. It is only Phase 4 that departs.
- **Bind an unbound tab by guessing the key.** Rejected: the fold is not invertible, and a wrong key answers the user from a session the chat app never reads — the reasoning already recorded at `src/kiro_crew/session_map.py:1608-1611`.
- **Keep the boolean and add a second one per surface.** Rejected: this is how ~two dozen prefix tests accumulated in the first place.

## 11. Open questions

1. **Does the identity become opaque, or does the grammar stay?** §9 decided the grammar is the routing authority; §6 argues the store should be. Phase 4 cannot start until a maintainer rules, and the honest answer may be that Phases 1–3 are worth doing and Phase 4 is not.
2. **Does the dashboard join the channel registry** (`src/kiro_crew/channels.py`) as a surface with no transport, or does it stay a host that owns a namespace? Phase 3's shape follows from the answer.
3. **Who declares a capability, and at what granularity?** Per surface type, per attached instance, or negotiated at attach time. A chat app whose thread support differs between a direct message and a channel is the case that decides this.
4. **Should the transcript be the unit that surfaces attach to, rather than the session?** rfc-append-only-session-transcript.md owns the write path, and §3.1's two-turn-locks-one-file behaviour is visible from both documents. If that RFC proceeds, the two need to agree on where turn-level serialisation lives before Phase 4 moves identity.

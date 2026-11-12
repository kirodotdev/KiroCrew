---
title: Memory lifecycle for the session-start memory block
status: draft
author: iamwhatever
created: 2026-11-09
last-audited: 2026-11-09
audited-at: 79bff1f741
doc-pr: null
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Memory lifecycle for the session-start memory block

- Status: draft
- Author: iamwhatever
- Created: 2026-11-09
- Code is cited by symbol and file at `79bff1f741`; the docs lint keeps line
  numbers out of prose, because a symbol survives the refactor that moves a line. Store numbers come from one
  developer host on 2026-11-09: the default store and one private member store,
  copied with the SQLite backup API from read-only connections and replayed
  through the shipped renderer on the copies. Only aggregate counts appear here.
- Related: [rfc-memory-hygiene.md](rfc-memory-hygiene.md) (semantic facts that
  consolidation writes, and a sweep for them) and
  [rfc-consolidation-fresh-conversation.md](rfc-consolidation-fresh-conversation.md)
  (each consolidation job on an empty conversation). This RFC covers lessons and
  the blocks that inject memory at session start.

## Summary

**Problem.** Every session starts with a rule block filled to its 37,000-character
cap, because the eligible rules are 4.4 to 4.7 times the cap. What
survives the cut is an accident of ranking. On a greeting, the default store
shows 1 of its 93 user-taught rules. Short words such as "a" admit
findings that have nothing to do with the request. Nothing ever removes a row,
so each week the pool is larger and the accident is worse.

**Cause.** Memory has four jobs: admit a row, tell whether two rows are the same
item, select rows for a prompt, and maintain the pool. Today each write path and
each block does these jobs its own way. No stage knows who taught a row, no
stage shares a notion of "same item" or "relevant", and the maintain stage does
not exist.

**Design.** One lifecycle, **Admit -> Identify -> Select -> Maintain**, built from
six shared primitives:

| Primitive | One line |
|---|---|
| P1 Value test | keep a row only if it is useful later **and** cannot be read back elsewhere |
| P2 Authority | user-taught > user-quoted > agent-inferred; decides tier, order and what may change automatically |
| P3 Item identity | one `same_item` function: normalized text, scope, tier, leader cosine >= 0.95 on stored vectors |
| P4 Relevance matcher | one token rule (>= 3 characters, CJK pairs) plus the V2 evidence gate, for every block |
| P5 Budget model | each block declares cap, gate, order and overflow line in one table |
| P6 Rollout contract | injection receipts with caps; `off -> shadow -> enforce` per stage; rollback is flag off; only Maintain rewrites rows |

Every change in this RFC is one primitive applied at one stage. Default and
private stores run the same functions.

**Rollout.** Seven flagged stages (R0a, R1 to R6), each default off and shadow
first, after a flagless receipt stage (R0). Only R1 is signed so far. R0a
records which session wrote each default-store lesson and keeps lessons from
spans no person spoke in out of the rule tier; on the measured stores 63%
(default) and 85% (private) of traceable rule-tier rows came from such spans. The first
real change orders the rule block by authority, which lifts user-taught rules
on a greeting from 1 of 93 to 81 of 93 on the default store, and to all 53 on the
private store, with no row rewritten.
The shared matcher trims findings and facts on short messages. The rule block
stays full until the pool shrinks below the cap, and only Dreaming shrinks it.
Dreaming ships last and only proposes changes for approval.

**Asked of reviewers.** Sign the framework and the stage order (sections 4 and
6), and answer the open questions in section 9.

## Terms

| Term | Meaning |
|---|---|
| Default store | the host's main memory database |
| Private store | a V2 member database owned by one crew member |
| Lesson | a row the agent should follow or know; stored with an optional `applies` tier |
| Rule tier | lessons with `applies: always` **or no tier at all** (untagged); always considered for the rule block |
| Findings tier | lessons with `applies: on_topic`; injected only when the request touches them |
| User-taught | a lesson saved on purpose: `learn_add`, dashboard edit, onboarding import (`source = user_explicit`) |
| Consolidation | the background job that turns past chats into facts and lessons |
| Rule block | the `[Learned corrections]` part of the session-start prompt |
| Injection receipt | a JSONL line recording which row ids a session-start build rendered |
| Leader fold | near-duplicate grouping where each row is compared only with rows already kept as group leaders, so chains cannot form |
| Shadow | the new policy runs and is logged; the old policy still decides what the user sees |

## 1. User problem

Users report three symptoms. Each has a number.

| Symptom | What the user sees | Measure (default / private store) |
|---|---|---|
| S1. Simple tasks cost too much | a one-word message pays for a full memory prompt | rule block on a greeting: 36,750 / 36,728 characters, at the 37,000 cap |
| S2. The agent ignores rules the user taught | a rule saved with `learn_add` is not followed | user-taught rules in the rule block on a greeting: 1 of 93 / 23 of 53 |
| S3. Memory gets noisier over time | more rows, more near-copies, more stale work state | lessons: 765 / 574 live rows; facts: 14,134 / 3,743 live rows, of which 4,551 / 2,188 have a PR, issue or round number in the key |

Supporting numbers from the same replay:

| Measure | Default | Private |
|---|---|---|
| Rule-tier rows in scope | 470 | 480 |
| Rule block uncapped (all rule-tier rows rendered) | 172,873 chars | 162,683 chars |
| Rows the capped block keeps on a greeting | 98 of 470 | 100 of 480 |
| Findings that the one-letter word "a" overlaps | 196 of 268 | 52 of 82 |
| Rule-tier rows that fold into a leader at cosine >= 0.95 | 0 | 92 |
| Injection receipt directory | 10 files, 348,666 bytes, no cap | same directory |

Counts are rows in scope for a session with no project; repo-scoped rows are
withheld before the cut. User-taught rows in the block are found by text match,
which finds every in-scope user-taught rule in the uncapped block.

## 2. Current state

### 2.1 Flow

```text
          WRITE (Admit)                      STORE                 SELECT (session start)
learn_add / dashboard ─┐                                      ┌─ rule block    (37,000, no gate)
  write_lesson 0.85 ───┼─> default store: semantic_memory ────┼─ findings      (5%, any shared word)
default consolidation ─┘   (lessons, facts, prefs)            ├─ preferences   (12,700, no gate)
                                                              ├─ task facts V1 (7.7%, no floor)
private consolidation ───> private store: memory_items ───────┼─ task facts V2 (7.7%, evidence gate)
  key = hash(rule, scope)   (directive, fact, episode)        └─ episodic      (3,000, cosine floor)
  vector filled later by backfill                              RECALL: memory_recall (hybrid)
                                MAINTAIN: none for lessons or facts
```

### 2.2 Write path (Admit and Identify today)

| Writer | Store | Tier decided by | Same-item check | Cite |
|---|---|---|---|---|
| `learn_add` and dashboard | both | the caller's `applies`; source `user_explicit` | cosine > 0.85, same scope, authority-aware | `api_lessons_create` (`dashboard/handlers/cron_lessons.py`) -> `write_lesson` (`vector_memory_runtime/lesson_write.py`) |
| Consolidation | default | model output; unstated stays untagged | same as above (`write_lesson`) | `_save_lessons` (`history_consolidation_memory.py`) |
| Consolidation | private | model output; unstated stays untagged | exact key `_lesson_key(rule, scope)`; no vector at write time | `apply_consolidation` (`vector_memory_member.py`) |
| Consolidation facts | both | kind routing, `off` by default | per key | `route_semantic_by_kind` (`memory_kind_routing.py`), `kind_routing` field (`config/memory_sections.py`) |

Write-side facts that matter for later sections:

- The tier prompt tells the model to omit `applies` when unsure, and an omitted
  tier is served as a standing rule (`LESSON_APPLIES_INSTRUCTION`, `lesson_validation.py`).
- The prompt's lesson examples are "always Y" and "never Z"
  (the lessons key of the prompt in `history_consolidation_pass.py`).
- One job may add up to 10 lessons (`_MAX_LESSONS_PER_CONSOLIDATION`, `vector_memory_constants.py`).
- Private consolidation writes no vector inside its transaction. The bulk
  backfill (`backfill_missing_embeddings` and `backfill_lesson_embeddings`,
  `vector_memory_runtime/embedding_repair.py`) fills it later at `PRIORITY_BULK`
  (`embeddings_settings.py`).
- Private consolidation rows record their source session (502 of 502). Default
  ones mostly do not (91 of 663): `_save_lessons` writes the bare source
  `consolidation`. Stage R0a adds the session as record metadata.
- Consolidation renders every transcript row it reads as the user's, including
  `session_send` relays, auto-nudges and other automatic turns. Only `notice`
  and `card` rows are skipped (`DISPLAY_ONLY_ROLES`,
  `history_projection_contract.py`). The markers that tell them apart already
  exist (`meta.relay`, the `session_send` envelope, the role, `meta.author`, the
  prefixes in `session_summary.py`); consolidation does not read them.

### 2.3 Selection at session start

| Block | How rows are picked | Cap | Gate | Cite |
|---|---|---|---|---|
| Rule block | rank by request, then authored `always` ahead of untagged, fill to cap | `_LESSONS_STARTUP_CAP = 37_000` | none | `get_lessons_context` (`vector_memory_runtime/lessons.py`); `context_assembly/budget.py` |
| Findings | same ranking | `_LESSON_EXPERIENCE_CAP` (5%) | any shared stemmed `\w+` word, no length floor | `any_lesson_overlap`, `_lesson_words` (`lessons.py`) |
| Preferences | all `pref.*` rows | `_PREFS_STARTUP_CAP = 12_700` | none | `get_preferences_context` (`vector_memory_runtime/semantic.py`) |
| Task facts, default (V1) | hybrid rank | `_SEMANTIC_MEMORY_CAP` (7.7%) | none | `get_semantic_context` (`semantic.py`) |
| Task facts, private (V2) | candidates with evidence | same | `relevance_evidence`, cosine floors 0.62 / 0.57 | `semantic_candidates_v2` (`semantic.py`); `relevance_evidence`, `SHORT_COSINE_FLOOR`, `LONG_COSINE_FLOOR` (`memory_v2.py`) |
| Episodic | vector search | `_EPISODIC_INJECT_CAP = 3_000` | cosine 0.55 (0.42 for long text) | `_EPISODIC_RELEVANCE_THRESHOLD` (`vector_memory_runtime/episodic_search.py`) |

Ranking inputs differ by store. The default store passes a query vector to the
rule block only when the activity block already ranked against the first
message (`activity_ranked` in `context_assembly/store_admission.py`). The
private store's renderer in the same file never passes one, so its rule order
is always lexical. The ranking ignores source on purpose (the
`get_lessons_context` docstring), so a
user-taught rule has no advantage over an inferred one.

### 2.4 Recall, embeddings, receipts

- `memory_recall` (`mcp_tools/learn.py`) runs `recall`
  (`vector_memory_runtime/recall.py`), hybrid when a vector is available.
- Embeddings serve write dedup, contradiction candidates (cosine 0.4 to 0.85),
  the optional startup rank, V2 fact floors, episodic floors and recall.
- A shared token rule already exists for the JSONL store:
  `_RELEVANCE_MIN_TOKEN = 3` and CJK runs split into two-character pairs
  (`_RELEVANCE_MIN_TOKEN` and `split_cjk_word`, `lesson_validation.py`). The
  vector-store paths above do not use it.
- Injection receipts (`KINDS` and `_receipt_dir`, `injection_receipts.py`) record
  rendered ids per build and have no row, byte or age cap.

### 2.5 Maintain

Nothing shrinks the lesson or fact pool. Episodic decay changes scores, not
rows. Deletion happens only when a person or an agent calls forget or edit.

## 3. Root causes

Each symptom traced to the mechanism and the implementation cause.

| # | Symptom -> mechanism -> cause | Primary? |
|---|---|---|
| C1 | S2 -> user rules lose the cut -> **Select has no authority order**: user-taught and inferred rules compete on request overlap only, and inferred rows outnumber them 4 to 1 (default) and 8 to 1 (private) | **primary for S2** |
| C2 | S1, S3 -> rule pool 4.4 to 4.7 times the cap -> **Admit defaults to standing rule**: unstated tier means `always`, examples read as rules, no budget cost to the model; 56% (default) and 82% (private) of consolidation `always` rows name a process step (one of the words PR, CI, rebase, review, worker, conductor, push, merge, override, check; a word-list count, so an upper bound) | **primary for growth** |
| C3 | S3 -> pool only grows -> **no Maintain stage**: no path removes, merges or re-tiers a row without a person | **primary for S3**, and the reason C1 gets worse each week |
| C4 | S3 -> fact rows pile up -> Admit has no value test for work state: kind routing is off by default; 32% (default) and 58% (private) of fact keys carry a PR, issue or round number | secondary |
| C5 | S3 -> near-copies take rule room -> **no shared "same item"**: default dedups at cosine > 0.85, private consolidation by exact key, and nothing at render; 92 private rule rows fold at 0.95 | secondary |
| C6 | S1 -> unrelated findings and facts admitted -> **no shared relevance matcher**: the findings gate counts any word, even "a" (196 of 268 rows); V1 facts have no floor | secondary |

Part of C2 is agent-sent text: a worker's "user" turns are often another
agent's instructions or automatic turns. A read-only measurement on one
developer host (2026-11-09, both stores, aggregate counts only) traced each
consolidation rule-tier row to the session that wrote it and classified that
session's input rows by the markers in section 2.2:

| Measure | Default | Private |
|---|---|---|
| Rule-tier consolidation rows with a traceable session | 298 of 398 | 439 of 439 |
| ...from sessions with no person-sent turn | 188 (63%) | 372 (85%) |
| ...from sessions with both | 64 (21%) | 61 (14%) |
| ...from sessions with only person-sent turns | 46 (15%) | 6 (1.4%) |
| `learn_add` calls found whose previous input was not the person's | 19 of 44 | 54 of 54 |
| Rule-block characters on the latest session start from agent-only sources | at least 45.9% | 88.6% |

The default store's 100 untraceable rows are the ones its bare
`consolidation` source leaves without a session; with them, the agent share
there lies between 47% and 88%. In a hand-checked sample of 50 rules, 20 had
no source message at all (the agent summarised its own work), 28 traced to a
relay or an automatic turn, 2 to the person, and none was an agent passing on
the person's decision. So filtering relayed rows alone is not enough: the unit
is the span. Stage R0a (section 6) adds the missing provenance and gates the
span.

Why truncation picks by accident: C2 and C3 make the pool 4.4 to 4.7 times the cap, C1
gives the cut no notion of value, and C6 means request overlap, the only signal
left, is noise for short messages.

## 4. Design: one framework

### 4.1 Lifecycle

| Stage | Question it answers | Runs when | May rewrite stored rows? |
|---|---|---|---|
| Admit | should this new row exist, and at which tier? | each write | no; decides only the new row |
| Identify | is this row the same item as one we have? | write, backfill, render, Maintain | no; it may retire only a row the current consolidation just wrote |
| Select | which rows enter this prompt, in which order? | session start, recall | no; render only |
| Maintain | which existing rows should merge, re-tier or retire? | offline (Dreaming) | yes, with approval and revisions |

### 4.2 Shared primitives

**P1 Value test.** A row is worth keeping only if it is useful in a later
session **and** cannot be read back from another source (GitHub, git, a work
ledger, the file system). Kind routing already applies this to facts (a `state`
item is skipped under `enforce`). Admit applies it to lessons through the
prompt and the P2 tier rule. Maintain applies it to the backlog.

**P2 Authority.** Every row gets one of three levels from data it already has:

| Level | Rows | Default tier | Order in rule block | Automatic change allowed |
|---|---|---|---|---|
| L1 user-taught | `source = user_explicit` | as the person set it | first | never |
| L2 user-quoted | consolidation row with a verified `user_quote` | `always` allowed | second | never; approval only |
| L3 agent-inferred | every other consolidation row, including untagged legacy rows | `on_topic` for new rows | third | render fold; Maintain proposals |

**Origin.** Authority needs a person behind the row. An agent-sent turn
(a `session_send` relay, an auto-nudge, a cron, heartbeat or work-ledger turn,
an `inject`, `subagent` or `system` row) never makes a row L1 or L2:

- a consolidation span with no person-sent turn yields no rule-tier row; its
  lessons are stored `on_topic` (L3 findings), never dropped;
- a `learn_add` whose triggering turn is not the person's is stored
  `on_topic` under its own source, not `user_explicit`.

The turn class comes only from markers the gateway writes (section 2.2), never
from wording: a turn is the person's only when it carries the person's marker.
A steer the person typed carries that marker; an app's steer does not. Stage R0a ships this.

Otherwise `learn_add` is called by the agent, usually because the person
asked, and this RFC takes `user_explicit` at its word. Tightening that further
is open question Q4.

The `user_quote` check, in code: the quote, after NFKC normalization, case
folding and whitespace collapse, must be a substring of one turn whose role is
the person's and that carries no relayed-message marker. A turn the person
edited is matched in its final stored form. A quote shorter than three P4
tokens does not count. A relayed agent message never counts, even if it quotes
the person.

**P3 Item identity.** One function `same_item(a, b)` used everywhere:

1. Same scope and same tier, or never the same.
2. Same normalized text (P2's normalization) -> same.
3. Otherwise, both rows have stored vectors of the same dimension, nonzero,
   from the current embedding space, and cosine >= 0.95 -> same.
4. Any vector missing, zero, of another dimension or another space -> not the
   same. Embed failure therefore means today's behavior.

Grouping is leader-based: rows are visited in P2 order, then newest first, and
each is compared only with leaders already kept. An L1 row is always a leader
and is never folded into another row.

**P4 Relevance matcher.** One token rule for every keyword comparison:

- A Latin or digit token counts only at 3 or more characters.
- A CJK run yields its two-character pairs; a one-character run yields none.
  This is the shipped `split_cjk_word` rule, reused unchanged.
- Admission needs at least one shared significant token.
- For task facts, both stores use the V2 gate: a row must carry
  `relevance_evidence` (lexical token or cosine above the 0.62 / 0.57 floors).

**P5 Budget model.** Each block is one row of one table (section 4.3, Select).
A block declares its cap, its admission gate, its order, and the line it prints
when rows do not fit. The rule block has no relevance gate, by design: a
standing rule applies whatever the topic.

**P6 Rollout contract.**

- Every stage has a flag `off | shadow | enforce`, default `off`.
- `shadow` computes the new decision and writes it to the injection receipt; the
  old policy still decides. Receipts hold row ids and counts, never text.
- Receipt caps copy kind routing (`SAMPLES_PER_DAY` and its siblings, `memory_kind_routing.py`): 150 sampled
  rows a day, 2,000 rows total, 1 MB, 14-day sample window, counters kept 400
  days, reset on a policy-version change. New receipt kinds join `KINDS`.
- A write-side decision that runs outside a turn (consolidation, R0a's gate)
  has no session-start receipt to join. It logs like kind routing does: in the
  store's `memory_meta` table, under the same caps, ids and counts only.
- Rollback is the flag set to `off`. No stage except Maintain rewrites an
  existing row, so no rollback needs a data migration.
- A flag is retired when its `enforce` has been the default for 30 days with no
  rollback: one follow-up pull request deletes the flag, its `off` and `shadow`
  code and its receipt fields.

### 4.3 The stages, as instances of the primitives

**Admit.**

| Change | Primitive | Applies to |
|---|---|---|
| Default-store consolidation lessons record their session (`source_ref`) | P2 (provenance) | default store; the private store already does |
| A span with no person-sent turn stores its lessons `on_topic`; an agent-triggered `learn_add` is `on_topic` and not `user_explicit` | P2 (origin) | both stores |
| New consolidation lesson with no tier is stored `on_topic` | P2 (L3 default) | both stores |
| `applies: always` from consolidation needs a verified `user_quote`, else `on_topic` | P2 (L2 test) | both stores |
| Lesson prompt states P1; examples show one finding and one standing rule | P1 | both stores |
| Kind routing moves to `enforce` for facts | P1 | both stores; its own flag and RFC already exist |

A person's own dashboard writes, and CLI writes from a person's own terminal, are
unchanged. Existing rows are unchanged.

**Identify.**

| Where | What runs | Effect |
|---|---|---|
| Private consolidation, in the write transaction | P3 steps 1-2 (normalized text) | a textual copy merges into the existing row |
| Private consolidation, when backfill gives the new row its vector | P3 step 3, only for rows this consolidation wrote and that are not yet served | a match retires the new row with a revision and counts it; the leader is untouched |
| Render, rule block | P3 leader fold | duplicates take no room; the block prints "folded N near-duplicate rules; learn_list shows all" |
| Maintain | P3 plus an uncertain band | proposals only (section 7) |

The default store keeps its existing `> 0.85` newest-wins merge in
`write_lesson` for now. Shadow logs how many of its merges fall between 0.85 and
0.95, so reviewers can decide whether to move it to P3 (Q1).

**Select.** The budget table, after this RFC:

| Block | Cap | Gate | Order | Overflow line |
|---|---|---|---|---|
| Rule block | 37,000, unchanged | none | P2 band (L1, L2, L3), then request relevance inside a band, after P3 fold | existing omission line, plus the fold line |
| Findings | 5%, unchanged | P4 | relevance | existing withheld line |
| Preferences | 12,700, unchanged | none | unchanged | unchanged |
| Task facts | 7.7%, unchanged | P4 plus V2 evidence, both stores | unchanged | unchanged |
| Episodic | 3,000, unchanged | unchanged cosine floor | unchanged | unchanged |
| Activity (history, projects) | unchanged | P4 | unchanged | unchanged |

Fit of the authority order on today's stores, replayed on a greeting: the L1
rows alone render to 42,333 characters on the default store and 21,240 on the
private store. The capped block keeps 81 of 93 user-taught rules on the default
store and all 53 on the private store. When L1 alone overflows, the existing
omission line says so and points at `learn_list`.

Cost on simple tasks: P4 shrinks the findings, facts and activity blocks on short
messages at once. The rule block stays at its cap until Maintain brings the
rule pool below 37,000 characters.

**Maintain.** Dreaming (section 7). It is the only stage that changes existing
rows, and it uses P1, P2 and P3 to find candidates.

### 4.4 Where the earlier fixes went

| Earlier fix | Now | Stage and primitive |
|---|---|---|
| A. Consolidation tier default and quote | the L3 default and the L2 test | Admit, P2 |
| B. Private store write dedup at 0.95 | P3 at write and at backfill | Identify, P3 |
| C. One word-length relevance rule | the shared matcher | Select, P4 |
| D. Render-time near-duplicate fold | P3 at render | Identify at render, P3 |
| E. Dream cleanup | the Maintain stage | Maintain, P1 to P3 |
| Optional "user-taught first" | core: the authority order | Select, P2 |

### 4.5 One code path for both stores

Both stores already render through `get_lessons_context`
(both renderers in `store_admission.py`). The primitives live in one module and take a
store as input: `authority(row)`, `same_item(a, b)`, `relevant(tokens, row)` and
the block table. The only per-store difference left is where Admit runs
(`write_lesson` versus `apply_consolidation`), and both call the same Admit
function. The private store's missing startup query vector stays missing; P2
and P4 do not need it.

### 4.6 Options left out, and why

| Option | Why it is out, in framework terms |
|---|---|
| Relevance floor on the rule block (lexical or cosine) | breaks P5's rule-block row: a standing rule applies regardless of topic; a floor drops L1 rules on unrelated requests, and short queries do not separate related from unrelated rules by cosine |
| Exempt "safety" rules, then floor the rest | the exempt set alone exceeds the cap; P2 already orders what matters |
| Lower the 37,000 cap | a budget change without fixing order or pool; L1 alone renders to 42,333 characters on the default store, so a lower cap drops more user-taught rules |
| Write dedup across scopes | P3 step 1: scope is part of identity; merging across scopes breaks scope isolation |
| Single-link clustering | P3 is leader-based because chains merge different rules: at cosine 0.90 single-link builds a 50-row private cluster whose weakest pair is 0.73 |
| Identity at cosine 0.85 to 0.90 | the private fold grows from 92 rows at 0.95 to 144 at 0.90 and 185 at 0.85 with no evidence that the added pairs are the same rule; 0.95 stays until Maintain approvals measure the band |
| Greeting word list | P4 must be general; a per-language list is never complete, and the short-token rule fixes greetings and every other short match |
| Model update or delete at write time | Admit is code-checked; a model edits rows only in Maintain, behind approval |

## 5. Regression safety

### 5.1 What never changes

| Invariant | Enforced by |
|---|---|
| An L1 row is never dropped, merged, folded into another row or re-tiered automatically | P2 table; code gate in Identify and Maintain |
| Untagged legacy rows are still served as rule-tier rows | Admit changes new rows only; only Maintain proposes a re-tier, with approval |
| Embed failure means today's behavior | P3 step 4; no stage adds a query embed or a new timeout |
| No stage but Maintain rewrites an existing row | P6; Identify retires only rows the current consolidation wrote |
| Rendering never writes the store | receipts are held in memory and written after dispatch |
| Caps are unchanged | P5 table |

### 5.2 Shadow metrics and go/no-go

Each stage in shadow records what `enforce` would add, drop or fold. The miss
signals, per row the new policy would drop:

| Miss signal | Meaning |
|---|---|
| a later `memory_recall` in the same session returns the dropped row | the row was needed |
| a rule is re-taught at cosine > 0.85 to a dropped row | the user had to repeat it |
| an L1 rule is absent from a block it was in before | a hard regression |

**Control.** A fixed random sample of the rows the **current** policy omits.
Its miss rate is the cost users already pay today. A stage passes when, over at
least 14 days and 200 session starts:

- its miss rate is no higher than control, and
- the L1-absence count is zero for stages that can remove a row from a block.

**Outcome check.** The miss signals show what a stage hides. They do not show
whether the agent does better work. Each stage also runs a task-outcome eval
before it moves to `enforce`: a `correction_repeat` scenario for the existing
harness (`kirocrew eval`, `src/kiro_crew/eval/`), next to `lesson_application`.
Each case has the person correct the agent in one session, then gives a task in
a later session where the corrected mistake is possible. The score is how often
the agent repeats a mistake it was already corrected on. The same cases run
with the stage `off` and `enforce` on the same provider and model. A stage may
not raise the repeat rate. For R6 the cases run before and after an approved
Dreaming batch, so a merge or correction that weakens a rule shows up as a
repeated mistake rather than only as a recall miss.

### 5.3 Rollback

Set the stage's flag to `off`. Admit, Identify and Select take effect at the next
session start. Rows Identify retired at write are restored from their revisions
by the same revert path Maintain uses.

## 6. Rollout

Each stage is one pull request, independently shippable and abandonable.

**Signed scope.** A maintainer has signed R1 only (authority order, flag
`memory.rule_authority_order`, default off, shadow first). R3's quote check and
tier policy wait for shadow counts of how many would-be demotions the user
repeats. R0a, R2 and R4 to R6 are proposals; each needs its own sign-off.

| # | Stage | Flag (default `off`) | Shadow signal | Pass metric | Rollback |
|---|---|---|---|---|---|
| R0 | Receipt fields and caps | none (fields written only under a stage's `shadow`) | n/a | receipts stay under every cap in a test that writes past them; off-mode lines unchanged byte for byte | revert the PR |
| R0a | Admit: provenance and origin gate | provenance: none (additive metadata, always on); gate: `memory.lesson_origin_gate` | per day: passes, passes with no person-sent turn, lessons read, rule-tier lessons, would-be demotions, for consolidation and `learn_add` apart; shadow also keeps the would-be demoted lesson keys | re-teach rate of demoted rows <= control; zero demotions in a span with a person-sent turn; zero L1 rows from a person's own turn demoted | provenance: revert the PR (old rows stay valid); gate: flag off, rows already written `on_topic` stay findings and stay retrievable |
| R1 | Select: authority order | `memory.rule_authority_order` | ids that enter and leave the rule block | L1 coverage up; miss rate of dropped L3 rows <= control | flag off |
| R2 | Select: shared matcher | `memory.relevance_rule` | rows admitted only under the old gate | miss rate <= control | flag off |
| R3 | Admit: tier policy | `memory.lesson_tier_policy` | new keys that would be stored `on_topic`; quote check pass and fail counts | re-teach rate of would-be `on_topic` rows <= control | flag off; rows already written keep their tier and stay retrievable through findings and recall |
| R4 | Identify: render fold | `memory.lesson_fold` | (leader, folded) id pairs | no L1 folded; miss rate <= control | flag off |
| R5 | Identify: private write and backfill | `memory.member_lesson_dedup` | would-be merges; default store 0.85-0.95 merge count | miss rate <= control; zero L1 merges | flag off; revert retired rows from revisions |
| R6 | Maintain: Dreaming | `memory.dream: off \| propose` | approval lists, refusals, wake-check results | section 7.9 | flag off; revert any run |

R0a runs first because it is the cheapest cut at the pool's growth and its
shadow answers the C2 agent-text question with live counts. Its rules:

- **Provenance.** A default-store consolidation lesson records
  `source_ref = consolidation:<session>` in its record metadata
  (`memory_record_meta`), the form the store's facts already use as their
  source. Its `source` column stays `consolidation`, so no authority check,
  dedup rule, dashboard label, audit or export that compares `source` changes.
  A row written without a session, and every older row, keeps the bare form.
- **Turn classes.** Human is an allowlist, the rule
  `HUMAN_TURN_META_KEY` (`history_schema.py`) sets for every reader of human
  activity. `human`: a `user` row the send paths a person reaches stamp
  `meta.human` (a steer the person types carries it too). `agent`: an unmarked `user` row with
  a `meta.relay` record or the `session_send` envelope. `automatic`: every
  other input row, unmarked `user` rows included (an Issue Radar wake, a cron
  prompt), so a new machine caller is not a person by default. A `learn_add`'s
  triggering turn is the chat's latest input row of any role, so no
  subagent completion, app or cron inject can be walked past (fail-closed).
  Every lesson from a span no person spoke in, and a demoted `learn_add`, is
  written without its NOT-clause, so an exact-rule match
  cannot attach agent text to a standing row.
- **Consolidation.** The gate reads the rows the model read, once, before the
  result branches to the two write paths. With no human turn, every rule-tier
  lesson (`always` or untagged) is stored `on_topic`.
- **`learn_add`.** Only the agent's own writes (the MCP tool's internal call)
  are read; a person's dashboard write is the person. The triggering
  turn is the latest input row in the caller's chat. When it is not human,
  `enforce` stores the lesson `on_topic` under source `agent_learn_add`, which
  ranks below `user_explicit` in every authority check. The same check guards
  the agent's `learn_update`: under `enforce`, a re-tier into the standing tier
  from a turn that is not human is refused, so a demoted lesson cannot be moved
  back to `always`.
- **`kirocrew learn add`.** The shell command has no chat to read. Run inside an
  agent session (the spawn and sandbox markers `cloud.aws._in_agent_session`
  already reads to refuse agent cloud actions), it is gated as a `learn_add` no
  person triggered, fail-closed, and logged under path `cli_learn_add` in the
  same log. Run from a person's own terminal, it carries no marker and is never
  gated. `off` reads no marker.
- **Modes.** `off`: nothing is read; prompt and writes are byte-identical.
  `shadow`: writes as `off` and logs. `enforce`: applies the demotion. No mode
  changes the prompt or a stored row.
- **Known gaps.** A mixed span keeps its tiers: tracing one lesson to one turn
  is not deterministic today, and the L2 quote check (R3) is the precise fix.
  A `learn_add` from a session with no live chat (a sub-agent, a channel
  thread) is left as submitted. An auto-nudge counts as automatic even in a
  loop the person armed. A transcript written before `meta.human` existed
  reads as automatic. A messaging-channel session (Discord, Telegram, Slack and
  the rest) is not gated at all: most channel transports write the person's
  row without `meta.human`, so stamping it there comes first. A person's `learn_add` made after a note the gateway
  wrote inside the turn is stored as a finding (the fail-closed cost). A
  `kirocrew learn add` an agent runs because a person asked it to is stored as a
  finding too: the shell cannot see the turn, so the `learn_add` tool is the path
  that keeps a person's ask. An agent that scrubs the environment markers on a
  host without the agents cgroup reads as a terminal. The JSONL fallback store has no `source` field, so
  there the gate changes the tier only.

R3 ships behind its flag, default `off`, before its sign-off: its job is to
produce the counts that sign-off waits for. Its rules:

- **Scope.** New consolidation lessons only, in both stores, after R0a's gate
  on the same rows. An untagged lesson is stored `on_topic`; an `always`
  lesson keeps its tier only with a `user_quote` that passes the P2 check.
  `learn_add`, dashboard and CLI writes and every stored row are unchanged.
  A messaging-channel session is not gated, for R0a's reason.
- **Prompt.** Only `enforce` asks for `user_quote`. Asking changes the tier the
  model picks, so a shadow that asked would stop measuring today's writes.
  `shadow` therefore counts every standing lesson as a would-be demotion (an
  upper bound on `enforce`), and its quote counts cover only quotes the model
  offers unasked; quote pass and fail rates come from `enforce`.
- **Re-teach.** Under `shadow`, each `learn_add` with a vector is compared,
  before its write, with the stored vectors of the logged would-be demoted
  keys; a cosine above 0.85 counts as a re-teach. An agent's own
  `learn_add` counts only when the person sent the chat's latest turn. That count over the
  would-be demotions is the R3 pass metric.

R4's rules, as built (`memory_lesson_fold.py`; R4 is not signed):

- **Where.** The rule block of the session-start render and its post-compaction
  copy, on the default and private stores. Explicit recall and the JSONL stores
  never fold.
- **Identity.** `same_item` (P3), the same function R5 uses. Rows are visited by
  P2 level, then newest first; each is compared only with kept leaders. An L1
  row is always a leader. A row with a NOT-clause its leader lacks stays a
  leader, so no clause leaves the prompt.
- **Modes.** `shadow` renders today's block and records `(leader, folded)` id
  pairs in the receipt field `lesson_fold`. `enforce` drops folded rows before
  the 37,000 budget is fitted and prints the fold line; the omission line counts
  only rows that did not fit.
- **Fixture.** The golden test embedder gives each topic-less row its own vector.

R1 comes next because it fixes S2 on existing stores without rewriting a row.
R4 needs one test fix first: the golden fixture that gives every topic-less
row the same vector must give distinct vectors, or it folds everything.

R5's rules, as built (`memory_item_identity.py`, `memory_lesson_dedup.py`; R5 is
not signed):

- **One function.** `same_item` and `leader_fold` implement P3 for R4 and
  Dreaming to reuse. Tier means rule tier (`always` or untagged) versus findings
  (`on_topic`); the text is the rule alone, the input its vector comes from.
- **Write.** In the consolidation transaction, a lesson whose key has no live row
  and whose normalized rule equals a live lesson of the same scope and tier is
  counted, and under `enforce` not written. A copy that adds a NOT-clause the
  stored row lacks is written as today.
- **Backfill.** Each new row joins a bounded queue. Once the backfill gives it a
  vector, it is compared with the leaders of the stored rows written before it;
  both steps admit only rows the rule block would serve (the renderer's own
  eligibility and render filters), so a row no prompt shows never leads;
  a match retires the new row under `enforce` with a revision, and the leader
  is untouched. "Not yet served" means no dashboard turn's injection receipt
  names it; an unreadable or oversized receipt set counts as served. Known gap:
  only dashboard turns write receipts, and on a worker thread, so a row a CLI,
  task-runner or incognito session was given can still be retired; its leader
  stays served and the revision restores it.
- **Rollback.** Flag `off`: the next backfill sweep restores every retired row
  whose latest revision is still that retirement. With a memory write switch
  off, the sweep changes nothing.
- **Q1 data.** Under `shadow` or `enforce` the default store counts its cosine
  merges and how many fall below 0.95. Its merge rule does not change.

## 7. Advanced: Dreaming (Maintain)

> **Higher risk.** Dreaming is the only stage that changes rows that already
> exist, and the only one where a model proposes edits. Everything below is
> there to keep it reviewable, reversible and bounded. It ships last, in
> `propose` mode only.

### 7.1 Why it is needed

Admit slows growth, Identify removes copies from the prompt, and Select orders
what is left. None of them shrinks the pool. The rule pool is 172,873 and
162,683 characters against a 37,000 cap; until it shrinks toward the cap, the
cut still decides most of the block. Maintain is the only stage that can bring
the pool down. The cap does not change.

### 7.2 What others do

Summaries below are our paraphrase of each source.

| System | What it does | What we take |
|---|---|---|
| Claude Code Auto Dream ([guide](https://claudefa.st/blog/guide/mechanics/auto-dream)) | a background pass that runs only after both a day has passed and several sessions happened; one run at a time under a lock; narrow searches for corrections and saves; merge and prune; unchanged files left alone | dual gate, lock, narrow candidate search, leave unchanged rows alone |
| Letta sleep-time compute ([blog](https://www.letta.com/blog/sleep-time-compute), [paper](https://arxiv.org/abs/2504.13171)) | a separate agent works on memory while the main agent is idle; Letta also names overgeneralization and memory rot as risks of repeated rewrites ([post](https://www.letta.com/blog/towards-agents-that-learn/)) | run only when idle; treat model rewrites as a risk to measure |
| Mem0 ([migration guide](https://docs.mem0.ai/migration/platform-v2-to-v3)) | moved from write-time ADD/UPDATE/DELETE to an ADD-only write pass, with ranking at read time | no model edits at write time |
| SAGE ([arXiv 2605.30711](https://arxiv.org/abs/2605.30711)) | code settles clear additions and clear duplicates; only uncertain cases reach a model merge | code finds candidates; the model sees only the uncertain band |
| Nocturne ([write-up](https://dev.to/harishkotra/sleeptime-memory-consolidation-an-agent-that-edits-its-own-memory-and-proves-it-still-remembers-53be)) | the model proposes, a pure code gate applies; refusals logged; rows deactivated, never deleted; a recall quiz after each pass | code gate, logged refusals, tombstones, wake check |
| Anthropic dreaming ([research](https://www.anthropic.com/research/components-of-agent-memory), [cookbook](https://platform.claude.com/cookbook/managed-agents-cma-dreaming-with-a-dynamic-workflow)) | a background pass re-reads past transcripts to check what memory claims, fixes notes that were wrong when written or went stale, and dates facts so a later one supersedes an earlier one; in one reported run a wrong note sent the agent into the same trap three times until the transcript check fixed it; analyzers get file tools only and treat transcripts as data | the transcript is the ground truth a row is checked against; evidence comes from the session, never from the model; transcripts are untrusted input |

### 7.3 Trigger

All must hold:

1. At least 24 hours since the last completed run, **and** at least 20 new
   lesson rows or 5 sessions since then.
2. No turn a person is waiting on is running: no dashboard chat turn and no
   channel message turn (Slack, Telegram and the other channels). Background
   turns do not count: cron, subagent, task-runner, workflow and heartbeat
   sessions, the `cron:`, subagent, `taskrunner:` and workflow keys that
   `_STATELESS_PREFIXES` (`session_parts/policy.py`) already classifies. A host
   running Crewmates, schedules or a conductor would otherwise rarely be idle.
3. A cross-process lock file under the data home is free. One run per host.

At most one automatic run a day. A manual trigger runs the same pass and still
takes the lock and the idle check. When a person's turn starts mid-run, the run
stops at the next batch boundary (7.4), so a background turn running alongside
Dreaming costs only shared CPU, not a stale read: Dreaming reads rows through
the same revisions an edit writes.

### 7.4 Scope of one run

- Incremental from a watermark in `memory_meta`: rows written or updated since
  the last run, plus the existing rows they are compared with.
- The first run over the existing backlog is capped per night and resumable: it
  keeps its own cursor and finishes over several nights.
- Per run: at most 200 rows considered and 10 minutes of wall clock. The run
  stops at the next batch boundary when any chat starts.
- Rows that lack a vector are embedded on the bulk queue.

### 7.5 Candidates, found by code

| Candidate | How found | Model? |
|---|---|---|
| Exact copy | P3 steps 1-2 | no |
| Near-duplicate | P3 leader fold at >= 0.95 | no |
| Uncertain band | leader cosine 0.90 to 0.95, same scope and tier | yes, to write one merged sentence |
| Work state | P1: key and value patterns kind routing tags `state` | no |
| Numbered key families | P1: keys differing only by a PR, issue or round number | no; grouped for review |
| Legacy tier | P2: L3 rule-tier rows (untagged or `always` without a quote) | no; proposed as `on_topic` |
| Transcript evidence | a row in this run's batch with a known source session, and a later session that served it and mentions its key terms (7.5.1) | yes, to judge the excerpts |

Only the uncertain band and transcript evidence reach the model: a few groups
and excerpts per run, under one cap of 20,000 input tokens.

#### 7.5.1 Transcript evidence

Every other candidate compares a row with other rows. None of them can tell
that a row was wrong when written, or that a later session showed it stale.
This candidate checks a row against the sessions it came from and was used in.

- **Source session.** Read from `memory_items.session_key`
  (`memory_schema.py`), or on a member store from the `source_ref` that
  `apply_consolidation` stamps (`vector_memory_member.py`), or on the default
  store from the session R0a records as metadata. A row with none of these is
  not a candidate. Default-store rows written before R0a carry no session, so
  this candidate reaches them only once R0a has run long enough to cover new
  rows.
- **Later use.** Sessions since the watermark whose injection receipts
  (`injection_receipts.py`) list the row's id. Receipts carry the session key,
  so the transcript is read locally at check time.
- **Excerpts, not transcripts.** Code searches those transcripts for the row's
  P4 content words and keeps the matching turns plus one turn on each side. The
  model never reads a whole session. A row with no matching turn is skipped.
- **Proposals.** One of four kinds, each carrying the excerpt it relies on:

  | Kind | Effect | Needs approval? |
  |---|---|---|
  | `confirm` | writes a dated confirmation and the cited session into the row's metadata; text, tier and scope are unchanged | no |
  | `flag` | marks the row for the person's review with the excerpt that questions it; text, tier and scope are unchanged | no; it only adds a review item |
  | `correct` | carries corrected text for the row | yes |
  | `supersede` | names the newer row or excerpt that replaces the row | yes |
- **Order.** The row's own source span is checked first, later sessions second.
  A row that was right when written and later contradicted is superseded with a
  date, not marked wrong.
- **Authority.** On L1 and L2 rows only `confirm` and `flag` are allowed,
  never `correct` or `supersede` (7.6). A person who agrees with a `flag` edits
  the rule themselves, so an L1 or L2 row changes only by the person's hand.

### 7.6 Model proposes, code gates

Each proposal passes a pure check over store state before it reaches the
approval list:

- Every cited id exists, is live, and was in this run's batch.
- No L1 or L2 row's text, tier or scope is changed. Only `confirm` and `flag`
  may cite one, and both write metadata only.
- A merged sentence adds no content word absent from its source rows.
- A row appears in at most one proposal per run.
- A transcript-evidence excerpt must be a substring of a turn in a session this
  run cited for that row, the same check P2 applies to `user_quote`. A
  `correct` text adds no content word beyond the row and that excerpt.

A refused proposal is logged with its reason and counted in the run receipt.

### 7.7 Approve, apply, restore

- Output is an approval list, not a change. Deletions and merges need a
  person's approval.
- **Where a person reviews it.** Each proposal that needs approval is written
  as an owner proposal through `propose_conflict`
  (`memory_record_metadata.py`), with source `dream:<run_id>`. That is the
  mechanism member-store consolidation already uses for a conflicting write,
  so the proposal appears where those already do: the Memory page's records
  editor (`MemoryRecordsEditor.tsx`), as "Review N proposals" on the row
  (counted by `_conflict_count` in `memory_edit.py`), with **Use proposal** and
  **Keep current**. A merge is one `set` proposal on the leader and one
  `forget` proposal on each folded row, grouped by run id so they are
  accepted or declined together. A `flag` uses the same review item with no
  proposed text.
- **Finding them.** Two additions make the list reachable without opening rows
  one by one:
  - The records list filter, today `q` and `kind` only (`_query` in
    `memory_edit.py`), gains `pending: true` to list rows with an open
    proposal, and `run: <run_id>` to list one run's proposals.
  - After a run that produced proposals, Crew sends one notification ("Dreaming
    proposed N changes") that opens the Memory page with that run's filter.
    The notification fires once per run, not per proposal.
  This is the first piece of the user-facing memory cleanup that
  [#19830](https://github.com/kirodotdev/KiroCrew/issues/19830) asks for.
- An approved change goes through the existing edit and forget paths. They
  write `memory_revisions` and leave a tombstone, so every row restores
  (`memory_edit.py` reads `memory_revisions` to restore a prior value).
- An unreviewed proposal never applies itself. A proposal still open when the
  same row comes up in a later run is left in place, not duplicated, because
  `propose_conflict` dedupes proposals per record.

### 7.8 Wake check

After an approved batch is applied:

1. Sample recent real startup queries from injection receipts. Receipts carry a
   session key and message id, so the query text is read from the local
   transcript at check time and never copied into a receipt.
2. Render the rule block and run recall for each query, before and after.
3. The batch fails if any L1 or L2 rule leaves the rule block, or if a recall
   hit is lost without its merged leader taking its place.
4. A failed batch reverts itself through the same revisions.

### 7.9 Resource bounds and pass metric

| Bound | Default |
|---|---|
| Automatic runs per day | 1 |
| Rows per run | 200 |
| Time per run | 10 minutes |
| Model input per run | 20,000 tokens |
| Embedding queue | bulk priority |
| Stop condition | a chat turn starts |

`propose` passes when, for 14 days, approval lists are produced, every refusal
is logged, the wake check passes on every applied batch, and the
`correction_repeat` eval (5.2) shows no higher repeat rate after applied
batches than before. The run receipt also counts proposals accepted, declined
and left open. A high decline rate means the candidates are wrong, and a high
open rate means nobody is reviewing them. Either one blocks any widening of
trust (7.10).

### 7.10 How trust may widen

One narrow class may later apply without approval, for example exact copies
among L3 rows. That needs a measured record of zero bad merges (approved, then
restored by a person or failed by the wake check) across a stated number of
approvals in that class (Q2). It is its own flag and its own decision. L1 and L2
rows never join an automatic class.

### 7.11 Failure modes and kill switch

| Failure | Guard |
|---|---|
| A merge loses meaning | code gate on content words; approval; wake check; revisions |
| A model invents a row or touches an unseen row | ids must be in the batch |
| The run slows a chat | idle check, bulk queue, stop at batch boundary |
| Two runs collide | cross-process lock |
| The backlog run never ends | per-night cap with a cursor |
| Anything else | `memory.dream: off` stops scheduling at once; any run reverts from its revisions |

### 7.12 What Dreaming leaves to other parts of Crew

Dreaming maintains lesson and fact rows that already exist in the default
store and in member stores. Three neighbouring jobs stay where they are:

| Job | Owner today | Dreaming's part |
|---|---|---|
| Turning a repeated procedure into a skill | auto-generated skills (`history_consolidation_skills.py`), off by default, with their own approval queue under Customize > Skills and `skills.auto_refine_on_deviation` | none. Dreaming does not create, edit or retire skills. It may count clusters of related L3 rows that look like one procedure and report them in the run receipt, which tells reviewers whether a link is worth building (Q7) |
| Adding a memory that extraction missed | consolidation, per session | none. Dreaming only proposes changes to rows it was shown (7.6), so it cannot invent a row. A run that reads excerpts and finds an unrecorded rule logs a count, never the text |
| Maintaining memory kept outside these stores | that store's own owner | none. Dreaming runs only on stores Crew opens. A memory service that a Crew syncs to or reads from runs its own maintenance, and Dreaming does not write to it |

Keeping these out lets each job keep the review path it already has, and keeps
Dreaming's code gate simple: every proposal names a row that exists.

## 8. Security considerations

- Every stage writes only the memory store, through existing write, edit and
  forget paths.
- Receipts hold row ids and counts, not text.
- Dreaming sends the text of uncertain-band rows and transcript-evidence
  excerpts, at most 20,000 input tokens a run, to the model provider the
  consolidation job already uses. That provider already received those
  transcripts when the sessions ran. No new destination is involved.
- Transcripts hold text from web pages, issues, channel messages and tool
  output, so excerpts reach the model as quoted data, never as instructions.
  The excerpt substring gate means a proposal cites only text really in the
  session, and the approval list means an injected excerpt can at worst propose
  a change a person declines. Incognito and temporary sessions write no
  receipts and are never read.
- The code gate refuses any id outside the batch, so model output cannot touch
  rows it was not shown.
- The `user_quote` check keeps relayed agent text from becoming a standing rule.
  That check covers consolidation only. R0a covers the span (no person-sent
  turn, no rule-tier row) and the agent's own `learn_add`, which would otherwise
  write an L1 row whatever turn asked for it. R0a's log holds lesson keys and
  counts, never text.

## 9. Non-goals and open questions

**Non-goals.**

- Lowering any cap.
- A relevance floor on the rule block.
- Write dedup across scopes.
- Automatic deletion of any row.
- Single-link clustering, or identity below cosine 0.95.
- A new shadow log beside injection receipts, other than the kind-routing
  log a write-side stage outside a turn already uses (P6).
- Changing who may call `learn_add`.
- Dreaming writing new rows, skills, or memory outside the stores Crew opens
  (7.12).

**Open questions for reviewers.**

1. Q1. Should the default store's `> 0.85` write merge move to P3's 0.95? R5
   shadow data counts the merges in between.
2. Q2. What zero-bad-merge record lets Dreaming apply exact L3 copies without
   approval?
3. Q3. Are 20 new rows or 5 sessions the right Dreaming trigger for a light user?
4. Q4. `learn_add` is agent-called. R0a already refuses L1 when the
   triggering turn is not the person's. Should L1 also require the person's
   own turn to contain the request, as the L2 quote check does?
5. Q5. When L1 alone overflows the rule block (81 of 93 fit on the default
   store), should Maintain propose merges among L1 rows for the person to
   approve, or should L1 get its own budget line?
6. Q6. Should transcript evidence (7.5.1) share the 20,000-token cap with the
   uncertain band or get its own, and how many rows should one run check?
7. Q7. Should a cluster of related L3 rows that Dreaming reports as one
   procedure (7.12) become an auto-skill candidate in the existing Skills
   review queue, or stay a count in the run receipt?

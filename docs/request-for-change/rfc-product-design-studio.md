---
title: Product Design Studio — Design Critique becomes one session with four experts
status: draft
author: jasperthedeadplant
created: 2026-10-09
last-audited: 2026-10-09
audited-at: 33b67aced
doc-pr:
implementation-prs: []
tracking-issues: [12538]
supersedes: []
superseded-by: []
---

# RFC: Product Design Studio — Design Critique becomes one session with four experts

- Status: draft — asks for the maintainer design sign-off requested on
  [#12538](https://github.com/kirodotdev/KiroCrew/issues/12538) (2026-10-08).
  Every "exists today" claim below was checked on main at `33b67aced`
  (2026-10-09); citations name files and symbols, not line numbers. §9 lists the
  decisions this document asks for.
- Author: jasperthedeadplant
- Tracking issue: [#12538](https://github.com/kirodotdev/KiroCrew/issues/12538)
- Related: [rfc-app-sdk-durable-jobs-and-view-state.md](rfc-app-sdk-durable-jobs-and-view-state.md)
  (the durable-job seam §6 declines for now), [rfc-everything-is-an-app.md](rfc-everything-is-an-app.md)
  (the builtin boundary this stays inside), `docs/app-kit/manifest-reference.md`
  (the `agents`, `spawn` and `hooks` fields §6 discusses).
- Frames: five wireframes of the model, one worked example running through them
  ([deck](https://gistcdn.githack.com/jasperthedeadplant/aaf1469c2f17978a7362a94cddf98935/raw/product-design-studio-wireframes.html),
  [source gist](https://gist.github.com/jasperthedeadplant/aaf1469c2f17978a7362a94cddf98935)).
  The twelve September frames the issue links are superseded by these.

## Summary

Design Critique today is one expert who reads your screens once and writes a
report. This proposes it becomes **Product Design Studio**: one session about one
piece of work, in which four experts can take part — a Product & UX expert and a
Visual expert who review, a Designer who draws, and a Research expert who runs
simulated user tests — each leaving **one sheet** on a shared canvas. Growth
happens inside a sheet (a second review is a round on the review sheet, a second
test is a column on the research sheet), never as a second sheet, so a session
cannot sprawl into versions. The chat is the history: every expert line that
produces or changes a sheet carries a card that opens it at that state. The next
expert is **offered** at the natural moment, as a suggested reply; a picker in
the composer is the override. The app's id, backend, skills and the "never edits
your project" boundary are unchanged.

This supersedes the design in the issue body of #12538 (22 Sept), which had two
doors leading to two separate linear pipelines, a "round 2 as its own version"
loop, and the name Design Critique. §1.3 says what changed and why.

## 1. Problem

### 1.1 What is on main

Verified at `33b67aced`:

- The app is `src/kiro_crew/apps/builtins/design_critique/` with `displayName`
  "Design Critique", one page, `backend.routes` only, and `"storage": false`
  (`app.json`). Its frontend is `website/src/apps/design-critique/`:
  `DesignCritiquePage.tsx`, `ScopingPicker.tsx`, `WaitingScreen.tsx`,
  `FindingRow.tsx`, `AskLayer.tsx`, `Composer.tsx` and helpers. The page opens on
  the upload box; there is no way in without a design.
- One skill, `skills/design-critique`. The page runs it as prompt + skill in a
  throwaway chat slot on the core `kirocrew` agent (`constants.ts` `AGENT`), and
  the slot is created through the `design-critique` app-worker mode that core
  allowlists in `dashboard/chat_handlers.py` (`_CREATABLE_MODES`).
- The result is a `Report` (`types.ts`): an overall read, a tally, findings with
  severity / title / evidence / fix / rules / box, what is working, what it could
  not see. A `Finding` has no id. Up to 24 reports are kept in the browser's
  localStorage (`utils.ts` `saveHistory`); nothing links one to the next.
- Nobody in the app plays the user, nothing is drawn, and the report is the end
  of the conversation.

### 1.2 Why that is not enough

The people this app is for are teams with no designer: engineers and PMs who ship
screens nobody has looked at the way a designer would. For them the shipped app
is a critic — accurate, but a critic. It cannot be reached without a design, it
judges against whatever one line of context says without checking it, every fix
is a sentence you have to picture yourself, and the report has nowhere to go.
The issue body of #12538 says this at length; it still holds.

### 1.3 What changed since the September proposal

The September proposal fixed those gaps with two doors and a loop: Door A
(review) and Door B (design) as separate pipelines, "Take this forward" into a
tray, "Make round 2" as a new version, versions stacked in a rail. Building the
first slices of it (the doors, the frame confirmation, the Product & UX
reviewer, the review canvas, the Designer) exposed three things:

1. **A session that is a review *or* a design is the wrong unit.** Real work
   moves between the experts in no fixed order: a review produces a suggestion
   the Designer should draw; a drawn direction wants testing; a test result wants
   a redesign; the redesign wants a second read. Two pipelines make every one of
   those a dead end or a new session.
2. **Versions sprawl.** "Research on Design 2" and "Research on Design 3" as peer
   artefacts is twelve pills after an afternoon, none of which the person can
   tell apart. A timeline dropdown over them is a finding aid for a structure that
   should not have grown.
3. **The report shape is the product.** Rendered with a realistic report, the
   September review canvas was 2,458 px tall and 537 words, said its verdict four
   times before any evidence, and showed the one drawn alternative cropped
   inside an opened row. The audience does not read long reports; the output
   contract has to be short by construction, not by prompt.

The model below is the answer to those three.

## 2. The model

**A session is a workspace for one piece of work.** Four experts can work in it;
none of them owns it. They are:

| Expert | Does | Leaves behind |
|---|---|---|
| Product & UX expert | Reads the work against the confirmed problem and person: does it solve the problem or a symptom, what else would, then issues biggest-first with one suggestion each | **Review** sheet |
| Visual expert | Measures (contrast, hit targets, rhythm, hierarchy) before it speaks; never re-lists a structural finding the UX expert owns | **Visual review** sheet |
| Designer | Asks only what the brief leaves open (up to three questions), then draws directions, a mockup or a clickable prototype in the person's design system or a declared neutral style | **Design** sheet |
| Research expert | Plans who to talk to, what they do and which method; runs simulated sessions; reports results as a short readable thing with suggestions in the reviewers' shape; always labelled AI-simulated with its limits | **Research** sheet |

**One sheet per expert; growth happens inside it.** A second read is a *round*
on the Review sheet (round 2 reports what changed since round 1: fixed, still
open, new). Another take by the Designer is a fourth *tab* on the Design sheet,
labelled with where it came from. A second test is a *run* on the Research sheet,
shown as a column beside the first so the comparison the person actually wants
("which direction tested better") is already made. The strip of sheets on the
canvas is therefore capped at one pill per expert in a fixed order — Your screens
· Review · Visual review · Design · Research — and "versions" are not a concept.
When does this break? Only when one session covers two unrelated pieces of work,
and that is the boundary that says start a new session.

**The chat is the history.** Every expert line that produces or changes a sheet
carries a card: the expert's face, the sheet's name, what changed ("added run 2 ·
direction 3 · compared with run 1"), Open. A card points at a *state* of a sheet
(which run, which tab), not just the sheet. Scrolling the conversation is "which
expert did what, when", in the person's own context; there is no separate
timeline. The only dropdown is per sheet, when a sheet has more than one run.

**Experts are brought in, not handed off to.** The person should always be able
to summon any expert, but the normal path is the current expert offering the
natural next one as a suggested reply at the moment it is relevant: under an open
issue on the Review sheet, "Design this"; under a drawn direction, "Test this
with users" and "Review what was drawn". Two reviewers can be brought in at once
at intake and work in parallel. The joining expert says a "joined the session"
line, quotes exactly what it received, and asks at most one question. There is
no hidden fifth agent deciding who speaks: the experts collaborate through the
person, in the open, and "who am I talking to" is always a face beside a line.

**Two doors remain, as the first act only.** "I have a design to review" brings
in the Product & UX expert (and the Visual expert if ticked). "I want to design
something" brings in the Designer with a brief whose only required field is what
the person wants to make; the problem and the people are asked by the Designer
in the chat if not given, because asking for the problem *first* is the
designer's job, not the person's. After the first act both doors are in the same
room.

**Success criteria** (from the app's owner, held as build rules):

- The right thing is surfaced at the right time; nothing has to be dug out of
  small buttons or lists. No feature gets a button if a suggested reply can carry
  it.
- An expert's output is a contract, not a style: one sentence of verdict, a
  short ranked list, one suggestion per item, enforced in the harness so no
  expert can return prose. The chat gets two lines and a card.

## 3. Design

### 3.1 Data model

The session record (`HistoryEntry`, aliased `Session`) gains `sheets`, one per
expert that has worked in it, and `running: ExpertId[]`. The existing `report`,
`kind`, `brief`, `answers` and `design` fields become legacy mirrors kept
populated for saved data that predates the model; new code reads the sheets.

```ts
type ExpertId = 'ux' | 'visual' | 'research' | 'designer'
type SheetId  = 'review' | 'visualReview' | 'design' | 'research'

/** A pointer INTO a sheet: which run (round / take / test run) and, where the run has tabs, which one. */
interface SheetRef { sheet: SheetId; run: number; tab?: number }

interface ReviewRound { ts: number; report: Report; on?: SheetRef }          // round 1 full; later rounds: what changed
interface ReviewSheet { kind: 'review' | 'visualReview'; expert: 'ux' | 'visual'; rounds: ReviewRound[] }

interface DesignTake  { ts: number; design: Design; brief: Brief; answers: DesignAnswer[]; from?: SheetRef }
interface DesignSheet { kind: 'design'; expert: 'designer'; takes: DesignTake[] }

interface ResearchRun   { ts: number; on?: SheetRef; result: ResearchResult }
interface ResearchSheet { kind: 'research'; expert: 'research'; runs: ResearchRun[] }

type Sheets = Partial<{ review: ReviewSheet; visualReview: ReviewSheet; design: DesignSheet; research: ResearchSheet }>

interface ChatCard { ref: SheetRef; change: string }
interface ChatLine { id: string; from: ExpertId | 'you' | 'system'; text: string; ts: number; pending?: boolean; card?: ChatCard }
```

`on` and `from` are what make a non-linear session legible: a round that read a
drawn take says so, a take that came from issue #3 says so, a run that tested
direction 3 says so. They are `SheetRef`s, so each is one click away.

Storage stays where it is — the browser's localStorage under the existing key,
24 sessions — and `"storage": false` stays in the manifest. Sessions saved
before the model are lifted onto it at load (`report` becomes
`sheets.review.rounds[0]`, `design` becomes `sheets.design.takes[0]`), so
nothing in a person's history is lost. Moving sessions server-side is out of
scope here and would be its own change.

### 3.2 Expert registry

One table says what each expert *does* (its identity — face, hue, name — lives
beside it). Per expert: the sheet it produces, what must exist in the session
before it can start (`screens` | `brief` | `design`), whether a run exists for it
yet, and which experts its sheet offers next, in order (the first becomes the
suggested reply). The registry also fixes the strip order and names the owner of
each sheet. Nothing in it hides an expert that is not wired: the picker shows it
and says so.

### 3.3 Bringing an expert in

Two ways to the same place:

- **Offered.** Under the open item on a sheet, a "take it further" row with the
  sheet's first offer ("Design this"); the same offers become the suggested
  replies after the expert's verdict line. Clicking carries a structured payload
  — the `SheetRef` of what the person was looking at, and the finding or
  direction itself — so the receiving expert quotes it back rather than
  re-deriving it.
- **Override.** A `+` in the composer opens the four experts with one line on
  what each does and "in" on the ones already present.

A reviewer brought in on a drawn take runs a round `on` that take; the Designer
brought in from an issue starts a take `from` it; the Research expert brought in
from a direction runs `on` it. The payload is the brief; the expert may ask one
question before starting.

### 3.4 Canvas and chat

The canvas is one calm sheet for everyone — no per-expert paper or mode badge —
with the strip at the top and the open sheet beneath. "Your screens" is a sheet
in its own right (the person's screenshots large, click to enlarge). The eyebrow
names the open sheet and its owner; the chat follows whichever sheet and item is
open ("Looking at #3 · No way back until step 3"), so a follow-up typed in the
chat knows what "this" means.

Each sheet's shape, in the "short by construction" sense:

- **Review**: a labelled verdict section — the question the reviewer answered
  ("Does it solve the problem you gave?"), the confirmed problem quoted, the
  answer as one sentence whose first word is yes / partly / no — then "N issues
  found · biggest first" as expandable rows in a fixed order (clicking #2 closes
  #1 in place; nothing reorders). The open row shows the person's screen with the
  pin on the left and the drawn fix on the right, both click-to-enlarge, then
  what it costs the person, the fix, what the fix costs. The reviewer's longer
  material (three other ways, the assumption, the words) is not on the canvas;
  "Show me the other ways to solve it" is a suggested reply and the reviewer
  answers from its own report.
- **Design**: the Designer's pick as one sentence with the why; directions as
  tabs in fixed order, the open one large with click-to-enlarge; wins / costs /
  makes worse / assumes beneath; "where this is weakest" as one line; ship-first
  / can-wait.
- **Research**: an AI-simulated banner permanently at the top naming what
  simulation cannot tell you and when to talk to real people; who / tasks /
  method in three columns; results as a few bars (one column per run when there
  is more than one); suggestions in the reviewers' shape, each with "Design this".

### 3.5 Output contract

Each expert's skill (`skills/reviewer-product-ux`, `skills/reviewer-visual`,
`skills/reviewer-user-researcher`, `skills/designer`) ends with an output
contract the page parses, and the harness rejects a reply that does not match
it, saying so in the chat ("The Designer replied, but not in a shape I can
draw") rather than rendering prose. The contracts are the TypeScript types in
§3.1 plus `Report` and `Design` as they exist on the branch. The reviewers keep
the shipped critic's evidence rules: never judge unrendered code, measure rather
than estimate, no screenshot no finding, list what you could not see.

### 3.6 The Research expert, honestly

It plans (who to talk to — three to five people cast from the confirmed person
for coverage, the tasks they would do, which method and why), waits for "Start
testing", runs simulated sessions, and reports behaviour with the step strip and
the stuck step marked, never feelings in character voice. Every surface it owns
says it is AI-simulated, cannot replicate real human emotion or context, and
names the questions that need real people. It is the last expert to be wired and
the one whose platform shape §6 leaves open.

## 4. What changes for people who use Design Critique today

This is the part the sign-off is about. Behaviour that changes:

- **The first screen.** The app opens on the two doors, not the upload box. The
  upload box is one click in ("I have a design to review") and unchanged after
  that: screenshots, Figma, repo, folder, URL; the scoping picker; the frame the
  reviewer says back before judging.
- **The name.** "Design Critique" becomes "Product Design Studio" in
  `displayName`, the page label and the two shared i18n catalog entries. The id
  `design-critique`, the route, the app-worker mode and every place core
  hardcodes the id are unchanged (per the 25 Sept triage note, changing the id
  means changing core, and nothing here needs that).
- **The report.** A review is no longer one scrolling report. It is the Review
  sheet of §3.4, beside a conversation with the expert who wrote it. The same
  information is produced; less of it is on the canvas at once and the rest is
  reachable by asking.
- **Where a review is kept.** A review is a sheet inside a session, and a session
  can hold other sheets. Saved reviews reopen as single-sheet sessions.

Behaviour that is kept on purpose: rendering code and Figma before judging, the
NN/g severities and the named heuristics, "what I couldn't see", no score, the
Mode B conformance check when a design system is given, follow-up questions in
place, and the boundary that the app never installs, runs or edits the project it
reviews. Round 2 is built on the canvas, never in the person's files.

## 5. Backward compatibility

Compatible. Nothing main accepts stops working:

- The id, route, manifest permissions, `backend.routes` and the `design-critique`
  app-worker mode are unchanged, so core's allowlist and the frontend builtin
  registry need no edit.
- Saved sessions under the existing localStorage key are lifted onto the sheet
  model at load; the legacy fields are kept populated as mirrors for one
  release so a downgrade still finds a `report`.
- The shipped `skills/design-critique` skill stays in the manifest. The four
  expert skills are additions.
- The i18n glossary's do-not-translate entry for the app name is updated, not
  removed.

## 6. Platform choices

**Experts run as prompt + skill on the core agent, for now.** The 25 Sept triage
note corrected the issue: builtins *can* declare `agents` (Mochi, Meetings, PPTX
Maker, Auto-Improvement and Personal Shopper do), so this is a choice, not a
limit, and the out-of-date comment in `constants.ts` / `prompts.ts` goes with
the first PR. The choice is deliberate for the three experts that run one turn
at a time in a slot the page owns: it keeps the app's permission surface where it
is, and the persona travels with the prompt. The cost is that each expert's
identity is a frontend fact (the page knows which agent it prompted on which
slot), which §3.1's `ChatLine.from` records.

**Simulated research sessions are the exception.** Running three to five
simulated people at once through `ctx.spawn` requires the app to declare its
own agent, add `backend.hooks` and `permissions.spawn`, bound its own cost
against the host-wide cap, and read results from `GET /api/spawn/{id}`. The
alternative is to run them sequentially in the page's own slot, as the reviewers
do, and accept the wall-clock cost. This document does not decide between them;
§9 asks.

**Durable runs.** A design or research run that outlives navigation is the
`job_sdk` seam's job, and that RFC is still partial (no `useAppJob` hook). The
first PRs keep today's pattern — the single in-flight `Job` in localStorage plus
a backgroundable slot — and the Research expert's PR revisits it.

## 7. Rollout

Each its own PR against `main`, each landing a usable state:

1. **Studio foundation.** Sheets, registry, strip and viewer, chat cards, the
   lift of saved sessions, the rename, the two doors, the frame confirmation, the
   Product & UX expert's harness and Review sheet, the Designer's harness and
   Design sheet with the "what do you want to design" brief. (Built on the
   branch named in #12538; this is what the issue's current state describes.)
2. **Bring in.** Offers under open items, the `+` picker, the join line and
   quoted payload; the Designer from an issue; the Product & UX expert on a drawn
   take as round 2 (what changed since round 1).
3. **Visual expert**, brought in alongside the Product & UX expert at intake,
   with the two running in parallel.
4. **Research expert**, after §9's concurrency decision.
5. **Cleanup**: drop the legacy mirror fields; retarget the page's coverage tests
   (the shipped `DesignCritiquePage.coverage.test.tsx` targets the old single
   report and is known debt on the branch).

## 8. Alternatives considered

- **Two linear pipelines with a round-2 loop** (the September design). Rejected
  for §1.3's reasons: dead ends between experts, and versions that sprawl.
- **Separate artefacts per run plus a session timeline dropdown.** Rejected:
  treats the symptom. Bounding to one sheet per expert removes the need, and the
  chat cards already are the timeline, in context.
- **A hidden orchestrator agent deciding who speaks.** Rejected: it muddies
  "who am I talking to", which is the one thing the interface must keep clean.
  Orchestration is explicit — offers and the picker — and visible in the chat.
- **"Hand-off" as the vocabulary.** Rejected by the owner: it implies a
  sequence. "Bring in" is used everywhere.
- **Per-expert paper and mode badges on the canvas** (reviewing vs building).
  Rejected after building it: a sheet that changes colour at every turn is
  jarring and tells you nothing the byline does not.
- **A new app instead of extending.** Still rejected: it would duplicate the
  intake, rendering and heuristics, and the repo's review rule sends new builtins
  to the external registry.
- **Editing the person's design in place** for repo inputs. Still deferred; it
  breaks the boundary the app draws.

## 9. Decisions asked of maintainers

1. **Sign-off on §4** — the first screen, the name, and the report becoming a
   sheet beside a conversation — as an accepted change to behaviour people rely
   on.
2. **The name.** "Product Design Studio" is proposed because the app is no longer
   a critique and because one of its four experts would otherwise share the
   app's name. If a maintainer prefers another, the id does not move either way.
3. **Simulated research concurrency.** App-declared agent + `ctx.spawn` (parallel,
   more manifest surface, bounded by the host cap), or sequential in the page's
   slot (today's pattern, slower). This gates PR 4 only.
4. **Matching findings across rounds.** Round 2 reports fixed / still open / new.
   Main has no rounds and `Finding` has no id. Proposed: the reviewer matches by
   cause in its own skeptic pass with the previous round's findings in its
   prompt, and labels a match it is unsure of as "possibly the same as #n" rather
   than asserting it. If maintainers want a stable finding id instead, it is one
   field on `Finding`.

## 10. Open questions

- Whether sessions should move server-side (an app store under
  `"storage": true`) once they hold four sheets; localStorage's 24-session cap
  and quota were sized for single reports.
- Whether the four expert skills should live as the app's own `agents` from the
  start, so a later move to `ctx.spawn` for any of them is a manifest change
  rather than a rewrite.
- The Designer's identity hue (currently a rose the owner reads as alert-ish) —
  a design-token question, not a model one, noted so it is not lost.

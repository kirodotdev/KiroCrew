---
title: Amend TENETS.md — add "Everything is an app" as tenet 8
status: draft
author: zezhexu
created: 2026-08-18
last-audited: 2026-08-18
audited-at: e6b06685e
doc-pr:
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---
# RFC: Amend TENETS.md — add "Everything is an app" as tenet 8

- Status: draft — nothing merged. [`../../TENETS.md`](../../TENETS.md) carries seven
  tenets on main.
- Author: zezhexu
- Created: 2026-08-18
- Related: [`rfc-everything-is-an-app.md`](rfc-everything-is-an-app.md), which
  carries the architecture the tenet points at. This RFC owns only the
  `TENETS.md` edit.

## 1. Summary

Add an eighth tenet to [`../../TENETS.md`](../../TENETS.md), at the end of the
ordered list:

> **Everything is an app.** When we cannot build a surface as an app, that is a
> defect in the platform, and it is not a licence to build the surface into the
> core instead. What stays in the core is the trust boundary and the state every
> app shares — sessions and transcripts, memory, approvals, the governance
> ceiling, the event bus — because their worth comes from the platform holding the
> last word. Above that line, anything that renders or interprets is an app, and it
> is replaced a whole surface at a time. The set we ship is a curated opinion about
> where to start, open to being swapped out entirely, and it does not define the
> product. An app gets the same powers a built-in page has, because otherwise "make
> it an app" is only a polite way to say no.

Nothing else in TENETS.md changes. The existing seven keep their text and their
numbering.

## 2. Motivation

### 2.1 The argument the tenet settles

"Is this core experience or app experience?" is currently a matter of taste. A
reviewer who thinks a surface should not be compiled into the core has nothing to
appeal to, and as more people contribute, the boundary ends up wherever the last
contributor put a file. The tenet converts the question into "why can this not be
an app?", which is decidable against the code.

The same problem shows up from the user's side. Different groups need different
things to be central, and a single product owner cannot satisfy all of them by
negotiating one layout. Handing the choice to users and field engineers requires
first saying which surfaces are replaceable.

### 2.2 Why this is an RFC and not just a pull request

Because a tenet is the one document in the repository whose whole job is to decide
future arguments, and because nothing currently says how it changes. Measured at
`e6b06685e`:

- `grep -i tenet` returns **zero hits** in [`../../GOVERNANCE.md`](../../GOVERNANCE.md),
  [`../../CONTRIBUTING.md`](../../CONTRIBUTING.md) and
  [`../../AGENTS.md`](../../AGENTS.md). TENETS.md closes by pointing at
  GOVERNANCE.md for "who decides what lands and how", and GOVERNANCE.md's
  amendment clause is scoped to itself ("**This document** changes the same way
  anything else does").
- By GOVERNANCE.md's own test, this qualifies. It requires an RFC for "changes to
  a public interface, changes other parts of the project would have to build
  around, and anything that would be expensive to reverse." An ordered list that
  decides every future trade-off argument is the second and third.
- `git log --follow` on TENETS.md returns two commits: `71dff5870`
  ("docs: add TENETS.md", PR #1419, 2026-08-04) creating all seven at once, and
  the docs commit on this branch. So no tenet has ever been amended, and the
  reasoning for the current ordering — including the load-bearing claim that
  safety outranks openness because GOVERNANCE.md already implies it — lives in
  that commit message, reachable only by someone who thinks to run `git log` on a
  file whose text does not mention the argument exists.

That last point is the argument for writing this down here rather than in a commit
message: the previous reasoning was careful, and it is filed somewhere nobody
reads.

## 3. Goals

- Add tenet 8 with its ordering argued rather than assumed.
- Record the two clauses most likely to be misquoted, and what they do and do not
  claim.
- Keep the tenet short enough to stay readable next to the other seven.

## 4. Non-goals

- **A general tenet-amendment process.** This RFC amends one tenet. What bar a
  future amendment should meet is left open (§8).
- **The architecture.** The boundary table, the dead-field inventory, and the
  phased plan belong to [`rfc-everything-is-an-app.md`](rfc-everything-is-an-app.md).
- **A migration or a schedule.** A tenet that implied one would be a roadmap item
  wearing a tenet's clothes.
- **Renumbering or rewording the existing seven.**

## 5. Ordering: last, and why

The list is ordered and the order is load-bearing — the earlier tenet wins a
conflict — so each pairing is argued.

| Against | Winner | Why |
|---|---|---|
| 1 Safety first | Safety | Decisive. The tenet must never be readable as a licence to make a control replaceable. Its own text names the trust boundary as what stays, and sitting below tenet 1 says it a second time. |
| 2 Build in the open | Openness | No real conflict. Openness already requires the placement reasoning be written down, which is what this tenet asks for. |
| 3 Easy to use | Easy to use | Real conflict. A composable product can be an unusable one, so "productive in 60 seconds" must beat replaceability. This is why the shipped set is a curated opinion rather than an empty shell. |
| 4 The gateway, not the replacement | Gateway | No conflict; both point outward. |
| 5 Built as a community | Community | No conflict. This tenet is the mechanism for tenet 5's promise that "skills, agents, and apps exist so anyone can shape Kiro Crew around how they already work", and a mechanism belongs after the commitment it serves. |
| 6 Knowledge that flows, with boundaries | Boundaries | Real conflict. An app wanting broad memory access loses to memory boundaries and the right to forget. |
| 7 Teammates, not tools | Teammates | No conflict. |

It loses to three and conflicts with none of the rest, so last is correct and no
renumbering is needed.

## 6. Reading notes

Two clauses will be quoted back at us. Both should be read as stated here.

**"An app gets the same powers a built-in page has" is an obligation, not a
description.** It states what we owe an app so that "make it an app" cannot become
a polite refusal. It does not claim parity exists today, and it says nothing about
privilege in the other direction: an app's Python currently runs in the gateway
process with full privileges (`src/kiro_crew/apps/module_loader.py:34-39`), which
is a separate problem owned by
[`rfc-app-sandbox-isolation.md`](rfc-app-sandbox-isolation.md). The gap between the
obligation and today's behavior is inventoried in
[`rfc-everything-is-an-app.md`](rfc-everything-is-an-app.md) §2.4 and §9.

**"Anything that renders or interprets is an app" constrains how we build, not who
may build.** It says a surface must stand on app-facing seams and be replaceable
whole. Who may replace a given surface stays trust-graded and belongs to
[`rfc-navigation-placement-seam.md`](rfc-navigation-placement-seam.md): a
first-party surface decomposed into an app is the same trust tier as the core it
came from, and a third party is not.

**What the tenet does not commit to:** any specific surface becoming an app, or a
date. Chat in particular stays core, on the reasoning in
[`rfc-everything-is-an-app.md`](rfc-everything-is-an-app.md) §4.

## 7. How it lands

One pull request appending the tenet to
[`../../TENETS.md`](../../TENETS.md) and referencing this document. No code, no
schedule, no other file. The tenet's forward link points at
[`../architecture/overview.md`](../architecture/overview.md#the-app-boundary),
which is landed by
[`rfc-everything-is-an-app.md`](rfc-everything-is-an-app.md) Phase 0; if that
section is not merged first, the link is added in the same PR that merges it.

Backward compatibility, security, and implementation risk are not applicable: the
change is one paragraph of prose in a document with no consumers other than
readers. The risk the tenet carries is misinterpretation, which §6 addresses.

## 8. Alternatives considered

**Land it as a plain pull request.** This is what the first seven did, and it
works — the PR #1419 commit message argues the ordering carefully. The reason not
to repeat it is that the argument then lives only in git history, which is exactly
the problem §2.2 describes.

**Fold it into [`rfc-everything-is-an-app.md`](rfc-everything-is-an-app.md).** That
was the first draft. Splitting it gives each document one decision, so the tenet
can be accepted while the phases are still argued, or rejected without taking the
boundary section and the dead-field inventory with it.

**Do not add a tenet; keep the rule in the architecture doc only.** An
architecture doc is read by someone already working in that subsystem. The
audience for this rule is a reviewer deciding where a new surface goes, and they
are not reading the app-boundary section first.

**Rewrite an existing tenet instead of adding one.** Tenet 5 is the closest
("skills, agents, and apps exist so anyone can shape Kiro Crew"). Widening it
would bury a testable rule inside a broader commitment and would edit a sentence
other people have already relied on.

## 9. Open questions

1. **What bar should a future tenet amendment meet?** This RFC is one data point,
   not a process. If a second amendment comes along, the pattern — a document
   carrying the verbatim text, the ordering argument, and the misquote notes —
   should either be written into GOVERNANCE.md or dropped as overhead.
2. **Should the existing seven get their ordering arguments written down?** §5
   supplies one for the newcomer while the incumbents' reasoning stays in a commit
   message. Backfilling is cheap; skipping it means the standard applies only to
   new tenets.
3. **Does removing or weakening a tenet need a higher bar than adding one?**
   GOVERNANCE.md holds that opening governance further is always available and
   closing it back down is not. Whether tenets inherit that asymmetry is worth
   deciding before someone needs it, rather than during the argument that needs
   it.

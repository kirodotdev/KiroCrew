---
title: Lead crewmate -- a conductor that can also do the small thing itself
status: in-progress
author: chenmingwei23
created: 2026-10-08
last-audited: 2026-10-08
audited-at: 8c7417ea82
doc-pr: null
implementation-prs: [18128]
tracking-issues: [18049]
supersedes: []
superseded-by: []
---

# RFC: Lead crewmate -- a conductor that can also do the small thing itself

- Status: in-progress. This document ships INSIDE its implementing pull request:
  the repository takes no standalone RFC pull requests, so `doc-pr` is null and
  the implementation is the one named in `implementation-prs`.
- Related: [`rfc-conductor-work-ledger`](rfc-conductor-work-ledger.md) (items,
  binds, the report schema), [`rfc-crew-log-wake`](rfc-crew-log-wake.md) (the
  wake its patrol rides on),
  [`rfc-crewmate-dynamic-dashboard`](rfc-crewmate-dynamic-dashboard.md) (the page
  it keeps current).

## Summary

Ship one new crewmate, `kirocrew-team-lead`, installed by the product like the
other shipped agents. It is a conductor that has not given up the ability to
work: it does a small focused task with the ordinary toolset, and dispatches a
team of dashboard sessions for anything larger, dispatching a conductor that
decomposes again whenever an item's size is unknown.

No new mechanism is proposed. Every capability below is carried by something
already shipped or already designed; the claim is that the procedure BETWEEN them
is worth shipping as a crewmate instead of being reassembled by hand each time.

## Why a new crewmate, not a change to `kirocrew-conductor`

`kirocrew-conductor` deliberately cannot write a file or run a command beyond the
acceptance scripts, and its charter prohibits doing a work item itself by name.
That restraint is the property its design rests on: a conductor that could patch
the bug would, and then nothing gets dispatched, verified or recorded.

The lead's FIRST capability is the opposite one -- a one-pass fix, file, probe or
report it does itself, because dispatching such a task costs more than doing it.
Granting the conductor a file-writing tool would delete that invariant rather
than extend it, so this is an addition and not an edit. Both roles stay: the
conductor when the goal is purely delegation and the discipline is the point, the
lead when one crewmate should do the small thing and dispatch the large one.

And a CREWMATE rather than a bare agent template, because the dashboard tools are
member-scoped by design: a panel belongs to one crew precisely so an operator can
trust whose state they are reading. Session dispatch needs no membership, but the
panel write does, so a template that merely HOLDS those tools has a nominal
capability -- the calls come up and refuse. Membership is what makes capability 7
below real rather than advertised. The product installs it; nothing is set up by
hand.

## Motivation: five classes of failure

Each rule below is load-bearing against a failure this role has produced. Paired,
so a reviewer can see which rule is doing what.

| Failure class | What answers it |
|---|---|
| A patrol loop that died silently, was armed with the wrong watch, or woke on a one-minute cadence | Arming is one step that names the watch, and loop health is a CHECK each cycle -- the runtime's own view of the loop -- rather than something recalled. A cycle finding the loop without its watch repairs it first. |
| A worker's claim taken for an acceptance: a test that never exercised the fix, a head that had already moved | Acceptance is the evaluator's verdict against the item's own bar, recorded as a verdict. Every report names the exact head sha it was measured on, so a stale head is visible rather than inferred. |
| Hands-on fixes where a dispatch belonged, and idling for a confirmation nobody owed | One explicit rule decides self versus dispatch, and the crewmate states which it picked. A registered item is dispatched rather than held; standing rulings live in the ledger so a child cites one instead of asking again. |
| A stalled fleet read as a busy one | Many items `stale` at once is a stopped fleet, not a working one. The patrol treats that reading as the signal it is and resumes each item. |
| A wrong brief handed down, so the team builds the wrong thing correctly | The seed restates the owner's ask VERBATIM and the child echoes it back in its first report before planning, so a misread costs one report instead of a delivery. |

## What it ships with

Eight capabilities, each with the mechanism that carries it.

1. **Does a small focused task itself** -- the ordinary crewmate toolset, writing
   files and running commands like any chat session, with one stated rule
   choosing between doing and dispatching. This is the capability the existing
   conductor does not have, and the reason this crewmate exists.
2. **Registers work before starting** -- an intake step the owner configures, run
   on the first turn of a new goal, whose returned tracker links go into the work
   item and into every seed. The product ships the step and the place the links
   land; which tracker answers it is the owner's.
3. **Dispatches a team as dashboard sessions** -- an item created with a concrete
   acceptance condition, then `session_create` with an explicit `agent` and
   `folder`, then the bind, then the seed, in that order, because an unbound
   worker's first brief resolves to nothing. One shared brief file per goal,
   referenced by path in every seed rather than pasted into each.
4. **Nests by default** -- an item of unknown size gets a conductor, which
   decomposes again; a worker only for a clearly single leaf with one assertable
   acceptance. That nesting is one level: the crewmate dispatches a conductor,
   and that conductor dispatches workers. Every child reports against its own
   ledger item, so the tree is one vocabulary whatever is dispatched into it.
5. **Patrols on events, not on a cadence** -- one loop on its own session with
   the work-ledger watch, pulled forward by a worker's write, its session
   closing, or its turn ending, as [`rfc-crew-log-wake`](rfc-crew-log-wake.md)
   specifies. The interval is the backstop for a worker that goes silent, and a
   quiet cycle costs no turn.
6. **Decides through the ledger** -- a worker's `question` answered as a recorded
   decision; an acceptance only from the evaluator's verdict; a claimed pull
   request a claim until the item's acceptance is promoted explicitly, and then
   verified. No verdict is read out of a transcript.
7. **Drives the dynamic dashboard as its status board** -- once the crewmate
   record exists, and that condition is the whole mechanism rather than a caveat.
   The write lands only from a session that is a MEMBER thread, so the panel it
   writes is its own one; that scoping is a trust boundary, a panel belonging to
   one crew so an operator can trust whose state they are reading. **This release
   ships the TEMPLATE and creates no crewmate.** The record that confers membership
   is made by one documented command, on the path the Crewmates page already uses:

   ```
   kirocrew agent create --name <name> --kiro-agent kirocrew-team-lead
   ```

   So the agent is dispatchable everywhere, and until that command is run it is a
   conductor that can also do work -- capabilities 1, 2, 5, 6 and 8, which is a
   real agent and not the whole one -- while 3, 4 and 7 arrive with the crewmate,
   because the dashboard and panel servers join a session's mounts only for a
   crewmate thread. Without it the panel verbs refuse rather than silently writing
   somewhere else. With the crewmate in place it fills the agentic fields
   (`verdict`, `for_you`) only, and the field read tells it which values are its
   own and which past writes were refused. Counts, credits and item states come
   from folds, so it never types a number the page can read itself.
   See [`rfc-crewmate-dynamic-dashboard`](rfc-crewmate-dynamic-dashboard.md).
8. **Keeps its own durable state** -- goal, phase, folder, next step and patrol
   cursor in the session ledger, so a compaction or a restart resumes the patrol
   instead of re-dispatching a fleet that is already running. Items are not
   encoded there; the work ledger is the item store.

The prompt hard-codes no session count and no fan-out width. Capacity is the
host's resource reading plus the server's own concurrency limit, read before a
wide wave.

## Non-goals

- Any change to `kirocrew-conductor`, its charter or its grants.
- A new delivery path, a second ledger, or a second patrol loop.
- Replacing `kirocrew-worker`. A clearly single leaf still goes to a worker; the
  nesting rule exists to decide which is which.
- Creating a crewmate from product code, on a boot path or behind a switch. The
  crewmate is the shape this agent runs in, and an operator asking for one with
  `kirocrew agent create` is the shape of that choice. A later contributor adding
  automatic provisioning would be re-opening a decision this document records as
  taken, not completing it.
- Running as a dispatched child. The servers that carry session control and the
  status board join a session's mounts only for a crewmate thread, so this agent
  as somebody's child would carry none of them. There is no nested-crewmate case
  to support, and nothing should be built against one.

## Security considerations

- No new capability class. The toolset is a chat crewmate's plus the dispatch and
  panel verbs a conductor already holds, each of which writes only the caller's
  own record -- the property that made them auto-approvable.
- A child gains no handle on its parent: it reports against its one bound item
  under the schema the work ledger enforces, and the parent reads that record
  under its own identity.
- The file-writing capability is this crewmate's whole point and its only
  widening against a conductor. It is what an ordinary chat session already has,
  under the same approval policy, so nothing becomes reachable that was not
  reachable from a chat tab.
- The intake step is configuration. Its credentials and its reach are the
  owner's; the product ships no tracker client and stores no tracker credential.

## Rollout

1. This document, inside the pull request that implements it.
2. In that same pull request: the agent template installed beside the other
   shipped agents, its prompt beside the conductor's, its grants following the
   existing invariant comments, and one builtin skill holding the procedure --
   reusing the existing conductor skill's acceptance and patrol-budget scripts
   rather than copying them. Tests pin, each in its own case: the template
   installs, and a file at its name that the installer did not write is declined
   rather than replaced -- left byte-identical and never sanitized, with the
   session start REFUSED instead, so a spec nobody can vouch for does not run and
   nothing on disk changes (a recorded fork keeps its own governance and still
   starts); its grant tuple is the conductor's plus the file-writing
   surface and nothing else; the charter states the dispatch order and the one
   level of nesting the ledger permits; and the command that binds a crewmate is
   checked against the parser that has to accept it.
3. Nothing is HAND-AUTHORED. The template is written by product code on every
   gateway start, and the crewmate that completes it is one documented command
   the operator runs -- `kirocrew agent create` -- rather than a file anybody
   copies into place. The product creates no crewmate on its own: this agent is
   the root of its own goal, so a crewmate is the shape it runs in and an
   operator choosing to have one is the shape of that choice.

## Open questions

1. Should nesting depth have a ceiling in the prompt, or be left to the work
   ledger's own depth guard? **Settled as the guard**, so one rule decides it for
   every conductor kind rather than two that can disagree. The charter names the
   one level the guard permits and carries the limit as the refusal rather than as
   a number, so a moved cap needs no prompt edit.
2. Is the intake step required before a dispatch, or advisory? Proposal: required
   for work that will change code, advisory for a read or a report.

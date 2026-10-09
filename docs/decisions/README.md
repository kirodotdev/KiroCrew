# Decisions

A record of product and design decisions a person on the team made. It exists
so that nobody — a human, a review lane, the nightly tester, an agent — undoes
one by accident while fixing something else.

A decision record is history, so an entry may cite the pull requests, issues,
commits and dates it rests on. That is the one place in `docs/` where such
citations belong; this front page states the rules in present tense and carries
none.

Two homes, one boundary. [`request-for-change/`](../request-for-change/README.md)
holds the design of a large or contested change and the debate around it; an
entry here holds one settled outcome — a label, a placement, a behaviour — in a
few lines, so it can be grepped before a change and never edited after. The
`recorded-decisions-are-not-regressions` rule and issue triage read only this
directory: a decision an RFC settles is protected here only once it also has an
entry.

## Rules

1. **One file per decision**, named `YYYY-MM-DD-<slug>.md`, where the date is the
   day the decision was made. The directory holds this README and flat regular
   `*.md` entries and nothing else; the gate refuses anything else.
2. **Entries are immutable.** An entry is never edited, renamed or deleted.
   `scripts/check_decisions_history.py` enforces this in Fast Gate (the
   `decision-ledger-history` job): every entry present at the base of a pull
   request must exist at its head byte-identical. New entries are always
   allowed; this README is free to change.
3. **A change of mind is a new entry** that names the one it replaces on a
   `Supersedes:` line. It is written by a maintainer, or quotes one, and it
   ships in its own pull request: the `recorded-decisions-are-not-regressions`
   rule blocks that PR by design, and a maintainer's override is what lets it
   land. The same
   path corrects a typo or a wrong date in a shipped entry: a superseding entry
   may exist solely to fix wording, and says so in its `Why`.
4. **An agent never decides.** An agent may add an entry only when it records
   a decision a human made, names that human, and links the on-record artifact
   that carries the decision — an issue, a pull-request comment or a commit. A
   private conversation is not evidence: the person restates the decision on
   the pull request that adds the entry, and the entry cites that comment. The
   person is a maintainer listed in `MAINTAINERS.md`; anyone else's statement
   is a proposal until a maintainer restates it.
5. **An entry outranks a report.** A friction report, an AI-review finding, a
   failing expectation in a test, or a first-time-user observation that collides
   with an entry is not a bug. It is a feature request against a settled
   decision: link the entry by its file path and leave it to a maintainer, who
   either closes it as decided or changes the decision the one way it can be
   changed — a dedicated pull request carrying the superseding entry, merged
   with a maintainer override of the reviewer rule. No fix is made from the
   report itself. The entry's path is its one canonical
   thread: a search for it across issues lists every report that collided with
   the decision, so a decision that keeps drawing reports stays countable for
   the maintainer who could supersede it. Issue triage checks this directory before investigating an
   issue (the Issue Radar crew brief and Investigate prompt both say so), and
   the `recorded-decisions-are-not-regressions` rule in `AUTOSDE.yaml` holds
   reviewers to it. An entry outranks a report that contradicts it; a report that
   the product does not match an entry is a bug, and the entry is its spec.
6. **Grep this directory before changing any user-facing label, wording,
   placement or behaviour.** A pull request that reverses an entry without a
   superseding entry is a regression and is blocked.

## Entry format

```
# <Decision in one sentence>

Decided by: <name and role>
Date: YYYY-MM-DD
Supersedes: <file>            (optional)

## Decision
One sentence.

## Why
Only what is true and was said at the time.

## Evidence
Links to the pull requests, issues and commits that carry the decision.
```

## Entries

| Entry | Decision |
|---|---|
| [2026-06-14-chat-composer-model-and-effort-are-one-control.md](2026-06-14-chat-composer-model-and-effort-are-one-control.md) | The chat composer offers model and reasoning effort as one control, never two. |
| [2026-07-20-sessions-sidebar-create-button-says-new.md](2026-07-20-sessions-sidebar-create-button-says-new.md) | The Sessions sidebar's primary create button shows the visible label "New". |
| [2026-09-25-incognito-and-temporary-chats-keep-their-transcript.md](2026-09-25-incognito-and-temporary-chats-keep-their-transcript.md) | Incognito and Temporary chats keep their transcript in History; the modes promise "learn nothing", not "leave no disk record". |
| [2026-10-02-conductor-introduces-itself-in-every-chat.md](2026-10-02-conductor-introduces-itself-in-every-chat.md) | Every Conductor chat introduces itself in plain words and reports with a task board, "Needs you" first, wherever it is opened, not only in Crew Mode. |
| [2026-10-02-chat-transcript-scrolls-under-the-composer-glass.md](2026-10-02-chat-transcript-scrolls-under-the-composer-glass.md) | The chat transcript scrolls under the composer glass at every scroll position; the scroller never ends its box above the dock. |
| [2026-10-02-glass-material-has-five-thicknesses.md](2026-10-02-glass-material-has-five-thicknesses.md) | The glass material has five named thicknesses (ultrathin, thin, regular, thick, ultrathick), picked per surface; a call site never sets a blur or alpha of its own. |
| [2026-09-05-sidebar-lanes-end-with-a-show-all-older-sessions-row.md](2026-09-05-sidebar-lanes-end-with-a-show-all-older-sessions-row.md) | Every sidebar lane ends with an in-flow "Show all older sessions" row, kept alongside the Older Sessions footer; the two are not duplicates. |
| [2026-09-13-crew-members-message-through-session-send-not-an-inbox.md](2026-09-13-crew-members-message-through-session-send-not-an-inbox.md) | Crew members message each other through `session_send`; there is no separate member inbox subsystem, hop counter or rate limit. |
| [2026-09-13-options-are-read-from-the-newest-reply-only.md](2026-09-13-options-are-read-from-the-newest-reply-only.md) | An `[OPTIONS:]` ask is read from the newest assistant reply only; the dashboard never walks back to resurrect an older one. |
| [2026-09-14-side-panel-tabs-are-fused-and-the-activity-toggle-is-on-the-title-row.md](2026-09-14-side-panel-tabs-are-fused-and-the-activity-toggle-is-on-the-title-row.md) | The right SidePanel's workspace tabs are fused browser-style tabs, and the activity-panel toggle lives on the chat title row. |
| [2026-09-29-glass-pane-does-not-change-on-focus.md](2026-09-29-glass-pane-does-not-change-on-focus.md) | A Liquid Glass pane does not change on focus; the caret is the only focus indicator on a glass input, and a glass button keeps the `:focus-visible` ring. |
| [2026-10-03-every-agent-turn-ends-with-text-or-nothing-to-do.md](2026-10-03-every-agent-turn-ends-with-text-or-nothing-to-do.md) | Every agent turn ends with a closing text or a `nothing_to_do` call, never a bare tool stop; a bare stop keeps the recovery ladder. |
| [2026-10-08-crewmate-profile-card-tabs-are-sessions-schedules-goals-profile.md](2026-10-08-crewmate-profile-card-tabs-are-sessions-schedules-goals-profile.md) | The crewmate Profile card's tabs are Sessions, Schedules, Goals, Profile, and it opens on Sessions. |

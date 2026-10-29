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
| [2026-10-10-conductor-board-tab-replaces-the-in-chat-board.md](2026-10-10-conductor-board-tab-replaces-the-in-chat-board.md) | A Conductor whose Dashboard tab shows `goal-board` or `work-kanban` does not write a second task board in the chat; it leads with "Needs you" and one status line. Supersedes the 2026-10-02 Conductor entry. |
| [2026-10-13-conductor-with-a-dashboard-tab-skips-its-intro-card.md](2026-10-13-conductor-with-a-dashboard-tab-skips-its-intro-card.md) | A Conductor whose session has a Dashboard tab, any template, skips its opening intro card; chats with no tab keep it. Supersedes the 2026-10-10 Conductor entry. |
| [2026-10-02-chat-transcript-scrolls-under-the-composer-glass.md](2026-10-02-chat-transcript-scrolls-under-the-composer-glass.md) | The chat transcript scrolls under the composer glass at every scroll position; the scroller never ends its box above the dock. |
| [2026-10-02-glass-material-has-five-thicknesses.md](2026-10-02-glass-material-has-five-thicknesses.md) | The glass material has five named thicknesses (ultrathin, thin, regular, thick, ultrathick), picked per surface; a call site never sets a blur or alpha of its own. |
| [2026-09-05-sidebar-lanes-end-with-a-show-all-older-sessions-row.md](2026-09-05-sidebar-lanes-end-with-a-show-all-older-sessions-row.md) | Every sidebar lane ends with an in-flow "Show all older sessions" row, kept alongside the Older Sessions footer; the two are not duplicates. |
| [2026-09-13-crew-members-message-through-session-send-not-an-inbox.md](2026-09-13-crew-members-message-through-session-send-not-an-inbox.md) | Crew members message each other through `session_send`; there is no separate member inbox subsystem, hop counter or rate limit. |
| [2026-09-13-options-are-read-from-the-newest-reply-only.md](2026-09-13-options-are-read-from-the-newest-reply-only.md) | An `[OPTIONS:]` ask is read from the newest assistant reply only; the dashboard never walks back to resurrect an older one. |
| [2026-09-14-side-panel-tabs-are-fused-and-the-activity-toggle-is-on-the-title-row.md](2026-09-14-side-panel-tabs-are-fused-and-the-activity-toggle-is-on-the-title-row.md) | The right SidePanel's workspace tabs are fused browser-style tabs, and the activity-panel toggle lives on the chat title row. |
| [2026-09-29-glass-pane-does-not-change-on-focus.md](2026-09-29-glass-pane-does-not-change-on-focus.md) | A Liquid Glass pane does not change on focus; the caret is the only focus indicator on a glass input, and a glass button keeps the `:focus-visible` ring. |
| [2026-10-03-every-agent-turn-ends-with-text-or-nothing-to-do.md](2026-10-03-every-agent-turn-ends-with-text-or-nothing-to-do.md) | Every agent turn ends with a closing text or a `nothing_to_do` call, never a bare tool stop; a bare stop keeps the recovery ladder. |
| [2026-10-08-crewmate-profile-card-tabs-are-sessions-schedules-goals-profile.md](2026-10-08-crewmate-profile-card-tabs-are-sessions-schedules-goals-profile.md) | The crewmate Profile card's tabs are Sessions, Schedules, Goals, Profile, and it opens on Sessions. |
| [2026-10-09-macos-has-no-agent-tree-memory-ceiling.md](2026-10-09-macos-has-no-agent-tree-memory-ceiling.md) | macOS has no agent-tree memory ceiling; it stays best-effort because macOS has no cgroup or Job-object equivalent. |
| [2026-10-16-crewmate-chat-draws-the-spawn-run-card.md](2026-10-16-crewmate-chat-draws-the-spawn-run-card.md) | The crewmate chat draws one tool row, the `spawn_run` launch card, because it is the only sign that delegated work is alive after the crewmate's turn ends; every other rule of the 2026-10-09 entry stands. Supersedes the 2026-10-09 crewmate-chat entry. |
| [2026-10-17-crewmate-chat-hands-off-to-worker-sessions.md](2026-10-17-crewmate-chat-hands-off-to-worker-sessions.md) | A crewmate's chat starts no sub-agents and draws no tool row, the `spawn_run` launch card included; it hands work to worker sessions. Supersedes the 2026-10-16 crewmate-chat entry. |
| [2026-10-09-crewmate-chat-draws-messages-only.md](2026-10-09-crewmate-chat-draws-messages-only.md) | The crewmate chat draws the person's rows and the crewmate's speech only; runtime notices go to the crewmate on its next turn or to the log, never into the crewmate chat. |
| [2026-10-09-folder-glyph-sits-on-its-connector-line.md](2026-10-09-folder-glyph-sits-on-its-connector-line.md) | A sidebar folder's glyph sits on its own connector line and folder rows share the session rows' 14px left pad; title width is never bought by moving either. |
| [2026-10-12-harness-card-has-seven-core-lines.md](2026-10-12-harness-card-has-seven-core-lines.md) | The harness capability card shows exactly seven core lines; only those seven contribute to the "supports N of 7" count. |
| [2026-10-23-crewmate-watch-is-a-schedules-row-not-a-profile-readout.md](2026-10-23-crewmate-watch-is-a-schedules-row-not-a-profile-readout.md) | A crewmate's Profile tab carries no monitor readout; the patrol over the tasks it handed out is one Pause/Resume row under an On its own header on its Schedules tab, and the loop's budget bookkeeping is shown nowhere. |
| [2026-10-20-redaction-classifies-by-what-a-value-can-do.md](2026-10-20-redaction-classifies-by-what-a-value-can-do.md) | Credential redaction sorts every value into Secret, Bearer link or Identifier by what it can do, redacts Secrets and Bearer links, and never redacts an Identifier. |
| [2026-10-17-sidebar-create-controls-shift-opens-the-active-tab.md](2026-10-17-sidebar-create-controls-shift-opens-the-active-tab.md) | Cmd/Ctrl-click opens a background tab everywhere in the sidebar; Cmd/Ctrl-Shift-click on New, a folder "+" or an empty-folder row opens the new chat in an active tab. |
| [2026-10-15-the-standing-teammate-agent-is-called-a-mate.md](2026-10-15-the-standing-teammate-agent-is-called-a-mate.md) | Everywhere a person reads it, the standing teammate agent is a Mate (plural Mates); translations keep their teammate word, and code identifiers keep the `crewmate` spelling. |
| [2026-10-22-heartbeat-is-a-schedules-tab-row-not-a-goals-check-in.md](2026-10-22-heartbeat-is-a-schedules-tab-row-not-a-goals-check-in.md) | The crewmate's Heartbeat switch is the first row of the Profile card's Schedules tab, not a check-in on the Goals tab. |
| [2026-10-22-a-new-goal-starts-in-the-crewmate-chat.md](2026-10-22-a-new-goal-starts-in-the-crewmate-chat.md) | A new goal starts from the Goals tab's Create a goal row as a conversation in the crewmate's chat, with no text editor for it on the tab; the "Add something to track" row is gone. |
| [2026-10-27-every-used-crewmate-moves-to-the-conductor.md](2026-10-27-every-used-crewmate-moves-to-the-conductor.md) | On upgrade a crewmate moves onto its conductor even when its worker would lose a grant (told once) or its agent is unreadable (default template); a gain is withheld for the capability card, and a loosened limit still keeps it where it is. |
| [2026-10-28-every-crewmate-on-a-user-level-agent-moves-to-the-conductor.md](2026-10-28-every-crewmate-on-a-user-level-agent-moves-to-the-conductor.md) | Every crewmate still bound to a user-level agent moves onto its conductor; a hook or `permissions` / `toolsSettings` restriction its file cannot carry moves with it and is named in its notice instead of keeping it in place, and the shared-template pass never writes a spec Kiro Crew does not own. Supersedes the 2026-10-27 crewmate-migration entry. |
| [2026-10-28-the-crew-gradient-marks-crew-mode-surfaces.md](2026-10-28-the-crew-gradient-marks-crew-mode-surfaces.md) | The crew gradient (`--aim` to `--info`, `.crew-grad`) is the one mark of a chat run by a crew, worn by every Crew Mode surface (session row, filter chip, composer chip, Crew panel cards), and the one decorative gradient the flat-fill rule admits. |
| [2026-10-29-crew-member-approvals-notify.md](2026-10-29-crew-member-approvals-notify.md) | A Crew member's tool approval always alerts the person (chime, unread mark, bell note, banner, OS toast), pointed at its open conductor and named for the crew member; the note offers no Approve or Reject, and the conductor's row shows the wait even while it runs. |
| [2026-10-27-crew-mode-is-a-composer-switch-to-the-conductor-template.md](2026-10-27-crew-mode-is-a-composer-switch-to-the-conductor-template.md) | Crew Mode is a per-chat composer switch to the conductor template named by `agent.crew_mode_agent`; a conductor's crew members are ordinary sessions kept out of the person's own lists and folders and shown under their root in the Crew panel. |
| [2026-10-23-scrollbars-are-never-always-visible.md](2026-10-23-scrollbars-are-never-always-visible.md) | No scrollbar in the dashboard is visible at rest: a scroll box either hides it entirely or shows a thin thumb only while it is being scrolled, in a column it already reserves so the reveal moves no content. |
| [2026-10-28-a-kiro-cli-choice-runs-on-kas.md](2026-10-28-a-kiro-cli-choice-runs-on-kas.md) | A stored Kiro CLI harness choice runs on Kiro Crew's KAS path once the gateway confirms kiro-cli offers its v3 engine, told once with a one-click save of `kas`; without that confirmation it keeps the v2 launch, and no session is refused. |
| [2026-10-27-a-crewmate-the-person-created-is-listed-with-an-empty-chat.md](2026-10-27-a-crewmate-the-person-created-is-listed-with-an-empty-chat.md) | A crewmate the person created (on the Mates page, or by approving Mate's card) is listed even with an empty chat; product-made rows still wait for a message, and the reserved `default` member waits for a message the person sent. |

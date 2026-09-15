# Issue Radar Pipeline

The Pipeline dashboard answers one question about Issue Radar's automated triage: is it running, and where is work piling up? It shows every step of the pipeline, what is sitting in each one, how long each item has been there, and what every agent session it opened actually cost.

## Opening it

The dashboard lives inside the Issue Radar app, which is off until you turn it on.

1. **Apps → Issue Radar → enable**, then connect a repository.
2. Open **Issue Radar** from the sidebar.
3. In the left rail, expand **DASHBOARDS** and pick **Pipeline**.

The board is scoped to whichever repository the repo switcher has selected. Switching repositories resets any open drill-down, because an expanded item is looked up by issue number and the same number means a different issue in another repository.

## The two boards

The Pipeline dashboard is two tabs that answer different questions from different data. Their numbers are not comparable — one reads the pipeline's own event trail, the other reads the crew ledger — so they stay separate rather than merging into one page.

### Pipeline

Six steps, in order: **Scan → Triage → Dispatch → Implement → Verify → Cleanup**. Each step shows three numbers:

| Column | Meaning |
|--------|---------|
| **In step** | What is sitting there right now. Counted in *sessions* for Implement and Verify (those open an agent session per item) and in *issues* everywhere else. |
| **Recent** | What moved through in the trailing window — the header reads "Moved in *N*h". |
| **Total** | Cumulative since the trail began. |

Above the steps: **In flight**, **Last activity**, and **Events**. Between them, the connector thickness tracks recent flow, so a starved step shows as a thin line into it.

Click a step to open the items sitting in it. Each row expands into that item's agent sessions, one row per session, always showing the session key, model, **Turns**, and when it started. The numeric columns — **Duration**, **Context**, **Input**, **Output**, **Cache write**, **Cache read**, **Credits**, **Cost** — appear only when the server reports data for them, so a measurement that is not collected shows no column rather than a zero. This is the only place the pipeline's cost per issue is visible.

An item that never opened a session says so rather than showing an empty table.

### Item lanes

One horizontal lane per crew work item, running left to right across the phase columns — **SELECTED, CLAIMED, INVESTIG., IMPLEMENT, AWAIT CI, ADDR REV, AWAIT MRG, RESOLVED**. The lane's head marks the phase the item is in now.

The drawing carries state in its shapes:

- A `#N` chip on the item's card names its open pull request; an item with no chip has no pull request yet.
- A dot-dash span means the item skipped that phase; an arc under the lane means it re-entered one.
- A stub off the lane is an exit — skipped, yielded, handed back, preempted, or waiting on a human reply.
- A dwell over an hour is flagged, and the open dwell counts from the most recent entry into the current phase, so a stuck item is visible without reading any number.

The summary strip gives **In flight**, **Editing**, **Escalations**, and **Longest wait** (with the phase it is waiting in). **Implement** and **Addressing review** sit inside a marked fence: they are the phases that hold a dirty worktree, and at most one item per crew may be in them at a time.

## Before it shows anything

Neither board produces its own data — both read what a producer already wrote, so a fresh install has nothing to count yet.

| Board | Needs | Before then |
|-------|-------|-------------|
| Pipeline | The triage pipeline to have run and written its event trail | The six steps render with every count at zero — the step list is always drawn |
| Item lanes | A crew configured on the repository, with work claimed | "No crew activity yet" — set up crews in Issue Radar to start it |

If the board reports that the dispatch queue has not been migrated to one file per repository, retrying will not clear it — re-run the pipeline installer.

## Limits worth knowing

**Read-only.** The dashboard has no write path at all: four GET routes and nothing else -- three for the Pipeline tab, one per level of its object model, plus the one Item lanes reads. Nothing you click can alter a running pipeline.

**GitHub only, and the two tabs say so differently.** The Pipeline tab refuses a non-GitHub repository rather than guessing: its data is keyed on `owner/repo` alone, with no forge in the key, so a GitLab or self-hosted repository sharing a slug with a public GitHub one would be served the wrong repository's sessions and costs under its own heading. Item lanes does not refuse -- a non-GitHub repository simply answers an empty item list, so it renders the same "No crew activity yet" state it shows for a GitHub repository whose crews never ran. On a non-GitHub repository, then, Item lanes alone cannot tell you "this forge is not supported" apart from "no crew has run here" -- the Pipeline tab's refusal is where that answer lives.

**One pipeline shape for every repository.** The six steps and the phase vocabulary are fixed. A repository can configure its claim TTL, the label a crew applies when it needs a human, and its commit trailer — it cannot declare its own steps.

**Early events have no repository.** The event trail is one file, and the repository is a field on each event that the scheduled jobs only began recording later. Events written before that cannot be attributed, so a repository-scoped board excludes them from its step counts and reports the count separately — the board tells you its own numbers are incomplete rather than folding them in.

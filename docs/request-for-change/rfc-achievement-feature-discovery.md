---
title: Milestones for new-user feature discovery — one milestone table, quiet surfaces
status: accepted
author: zejiangg
created: 2026-10-29
last-audited: 2026-10-31
audited-at: 95f78562f1
doc-pr: null
implementation-prs: [26512, 26488]
tracking-issues: [26327]
supersedes: []
superseded-by: []
---

# RFC: Milestones for New-User Feature Discovery

- **Status:** Accepted. The product owner merged this RFC and started the build of Ship 1. Decisions D1–D3 were made on 2026-10-29 and D4–D14 on 2026-10-30 by the product owner (section 7.1).
- **Verified at:** `95f78562f1`. Every code reference below was read at that commit.
- **Tracks:** [#26327](https://github.com/kirodotdev/KiroCrew/issues/26327).
- **Hard constraint:** the sign-in / first-run flow does not change. `website/src/components/OnboardingFlow.tsx` stays as it is.
- **Naming:** the file name keeps the earlier word "achievement" so links stay stable. The internal keys, routes, metrics and state file use "milestone" (D14). The user-facing names changed on 2026-10-31 (D15).
- **Amended 2026-10-31 (product owner):** section 8 renames the panel to "Explore", adds a daily "Try today" pick, keeps the existing suggestion cards on the new-chat page, puts the 15-second intro video at the top of the Explore panel, adds progress bars and a calm badge wall under "Your progress", lists the Ship 2 items (8.10), and gives the top-bar ring its own "Explore N/M" pill beside the readout capsule (8.7, D17). Where an earlier section disagrees with section 8, section 8 wins; those passages are marked "Replaced by section 8". Implementation: backend [#26512](https://github.com/kirodotdev/KiroCrew/pull/26512), frontend [#26488](https://github.com/kirodotdev/KiroCrew/pull/26488).

## One-page summary

**Problem.** After signing in, a new user lands on an empty chat page with one line, "What can I do for you?" (`website/src/pages/chat/page/ChatEmptyState.tsx`). Nothing tells them Crew can run scheduled tasks, run chats in parallel, connect to Slack, or build widgets. The discovery aids that exist each cover one corner: Feature Tips are model-generated and only appear while a turn runs; Feature Videos are off by default and carry a single "Meet your Mates" intro; the Mate's first welcome only asks for a name.

**Proposal.** One built-in **milestone table** (D13). It has five groups:

| Group | Milestones | Batch |
|---|---|---|
| New features | 7 one-time: first widget, first parallel chats, first scheduled task, first PR watch, Slack connected, met the Mate, used Crew Mode. Each has a one-click starter task | Ship 1 |
| Sessions | chats opened 10 / 100 / 1000 | Ship 1 |
| Turns | messages sent 100 / 1000 / 10000 | Ship 1 |
| Streak | days in a row 3 / 7 / 30 / 100 | Ship 1 |
| Running at once | chats running at once 5 / 20 / 50 | Ship 2 |

Four quiet surfaces read this one table (D14). Surfaces 1–3 as written in this list are replaced by section 8 (D15–D17): the panel is "Explore" with "Try today" and "Your progress" (progress bars and a badge wall), the intro video is a card at the top of that panel, the new-chat page keeps its suggestion cards and swaps only the last one for a "Try today" entry card, and the top-bar ring sits in its own pill beside the readout capsule, labeled "Explore N/M" with the streak as smaller text.

1. **Top-bar indicator.** A small ring for new-feature progress, with the current streak number beside it. Clicking it opens the Milestones panel: plain checkmarks, progress bars, dates, current and best streak, "Mark as done" and "Mark all as done".
2. **New-chat empty state.** A short clip plus 3 cards = the next 3 untried **new-feature** milestones. Usage milestones and streaks never appear here, because no surface may nudge the user to open more chats and spend more. Once every new-feature milestone is handled, the empty state returns to today's page.
3. **Quiet notice.** One small line, once per milestone, such as "Milestone: first widget". No celebration.
4. **Mate** (Ship 2). The Mate walks the user through a new feature; a daily task raises one untried feature per day.

**How reaching a milestone works.**

- **Local only (D9).** Each machine keeps its milestones in one local file, `~/.kiro/crew/milestones.json`. No server, no cloud, no sync in v1. Two machines count separately.
- **Count events, not final state (D10).** When something actually happens (an artifact is created, a chat is opened, a message is sent), a local counter goes up and the first time is stamped. A milestone is reached when its counter reaches its threshold. Deleting the thing later never undoes it. The existing Feature Videos `used_when` probes run only once, as a backfill on upgrade.
- **Streaks (D12).** Count local days in a row with at least one sent message. Missing a day resets the current streak, never the best. No penalty and no warning about losing a streak.
- **Manual (D11).** Each untried new-feature milestone has "Mark as done", and the panel has one "Mark all as done". Usage milestones and streaks cannot be set by hand.

It is deterministic and never calls a model. The local file is user-editable; that is accepted for a personal tool, with no anti-cheat.

**Decided.** D1: low-cost rows send directly, others prefill. D2: the daily task is a real Mate model turn. D3: the main switch defaults on (2026-10-29). D4–D8: one table, surfaces, rollout, open-question defaults, the Crew Mode signal. D9: local only. D10: count events. D11: manual marking. D12: streaks. D13: milestones first, features as a group. D14: quiet style and the "milestone" name (2026-10-30). D15: "Explore" naming, daily "Try today" pick, one entry card on the new-chat page. D16: the final Ship 1 Explore panel with progress bars and badges, and a Ship 2 list. D17: intro video only in Explore, the "Explore N/M" pill beside the readout capsule, and tiered badges (2026-10-31, section 8). See 7.1.

**Still open.** Only Q7 (relationship to Feature Tips), deferred to the "Later" batch. See 7.2.

## 1. Background and current state

| Existing mechanism | Location | Relation to this RFC |
|---|---|---|
| First-run flow | `website/src/components/OnboardingFlow.tsx` | Unchanged. This design only takes effect after it ends |
| Empty chat page | `website/src/pages/chat/page/ChatEmptyState.tsx` | Surface 2 changes this |
| Top bar | `website/src/shell/topbar/shellHeader.tsx` (the notification bell lives here) | Surface 1's indicator sits next to the bell |
| Feature Videos | `src/kiro_crew/feature_videos.py`, `GET /api/feature-videos/next` (`api_feature_videos_next` in `dashboard/routes/realtime.py`) | Reuse the clips and the `seen/dismissed` state; its `used_when` probes are used only for the one-time backfill (4.4) |
| Feature Tips | `src/kiro_crew/tips.py` | Model-generated; kept. The milestone table never goes through a model |
| Mate first welcome | `src/kiro_crew/dashboard/mate_welcome.py`, copy in `src/kiro_crew/crewmate_prompts.json` | Surface 4 reuses `dispatch_hidden_turn` and the copy table |
| Mate check-ins (daily/weekly) | `src/kiro_crew/dashboard/goal_reminder.py` | Surface 4 follows its hidden-turn pattern; does not reuse its hosted cron (see 4.8) |
| Putting text into the composer | `website/src/pages/chat/page/launchIntake.ts` (`setPendingInput`, `?autoSend=1`) | Starter tasks use this channel |
| Crew Mode | `src/kiro_crew/crew_mode.py` (the `crew_mode_agent` stamp on a session) | Signal source for the "used Crew Mode" milestone, see 3.2 |
| Durable user-chat counter | `src/kiro_crew/dashboard/session_pulse_counter.py`, called from `dashboard/state_parts/slot_directory.py` | Model for the milestone file writes (off-loop, best-effort); its call site is the "chats opened" hook, see 3.4 |
| Turn start | `src/kiro_crew/dashboard/chat_turn/turn_dispatch_gates.py` (the `crew_log_emit.on_turn_started` call) | Hook for turns, streak, Mate, Crew Mode and the "running at once" peak, see 3.4 |
| Metrics | `src/kiro_crew/metrics/events.py` (`emit_counter`), `metrics/schema.py` | New `kirocrew.milestone.*` counters |

The design follows three product principles:

| Principle | How this design applies it |
|---|---|
| Deliver a result first, explain the feature second | A low-cost starter task sends on one click and produces a real result (a widget, results from two parallel chats) instead of opening a tutorial |
| Give a reason to come back daily | The streak (3.3), and in Ship 2 one untried feature per day raised once by the Mate |
| Surface sticky features early | The new-features group includes Slack, the Mate, parallel chats and Crew Mode, and the ordering puts sticky features near the front |

## 2. Goals and non-goals

Goals:

- The first time a new user opens a new chat, one click produces a real result.
- Reaching a milestone is decided by a deterministic rule: the same local counters always give the same answer, with no model call.
- Every surface shares one table and one state; there are never several progress counts that disagree.
- Once every new-feature milestone is handled, or the user turns the cards off, the empty state returns to today's page.
- Existing users can see the usage milestones they have already reached.
- A user who wants no guidance can clear it all in one click.

Non-goals:

- No change to `OnboardingFlow.tsx`, and no new first-run step.
- No points, rewards, leaderboards, trophies or celebration effects. A milestone is just a dated checkmark.
- No server, cloud storage or cross-machine sync in v1 (D9).
- No anti-cheat. This is a personal tool; the local file is user-editable and a user who marks something done is believed.
- No surface nudges the user to open more chats, send more messages or keep a streak. Usage milestones and streaks appear only in the panel and in quiet notices.
- No model generates the milestone table or any HTML that gets rendered.
- No remotely delivered starter tasks in v1 (reasons in 4.2).

## 3. The milestone table

### 3.1 Groups

Milestones come first; "try a new feature" is one group among them (D13).

| Group id | Panel heading | Kind | Counter | Thresholds | Can be marked by hand | On the empty state |
|---|---|---|---|---|---|---|
| `features` | New features | 7 one-time rows | one counter per row | 1 | Yes | Yes, the next 3 |
| `sessions` | Sessions | tiered | `chats-opened` (sum) | 10 / 100 / 1000 | No | No |
| `turns` | Messages | tiered | `turns-sent` (sum) | 100 / 1000 / 10000 | No | No |
| `streak` | Streak | tiered | `streak-best` (best streak in days) | 3 / 7 / 30 / 100 | No | No |
| `concurrent` | Running at once | tiered | `chats-concurrent` (peak) | 5 / 20 / 50 | No | No |

The `turns` thresholds are a starting point; they are reviewed with the other rows after 4 weeks (6).

### 3.2 New features (7)

Selection criteria: (a) the starter task produces a visible result within a minute; (b) there is a cheap, deterministic moment in the code where the feature happens; (c) people come back to it once they have used it (stickiness).

Each row has threshold 1: its counter going from 0 to 1 reaches it. The "counted when" column names the moment that counts; 3.4 names where in the code it is counted. The backfill column is used once, on upgrade (4.4).

| Order | id | Milestone | Starter task (illustrative) | Send mode (4.5) | Counted when | Backfill probe |
|---|---|---|---|---|---|---|
| 1 | `widgets` | First widget | "Make a widget that shows my calendar for this week" | Send directly | An artifact is created | `artifacts_nonempty` |
| 2 | `scheduled-task` | First scheduled task | "Every morning at 9, give me a summary of yesterday's work" | Prefill | A scheduled task is added | `sel_event_seen:cron_add` |
| 3 | `mate` | Met the Mate | "Introduce me to the Mate" → opens the Mate chat | Open page | A user-sent turn starts in the Mate's chat | New probe `mate_thread_has_user_turn` |
| 4 | `parallel-chats` | First parallel chats | "Open two chats at once: one researches, one writes an outline" | Send directly | A `session_create` tool call completes | `sel_event_seen:session_create` |
| 5 | `slack` | Slack connected | "Connect Crew to my Slack" | Prefill | The Slack settings save succeeds | New probe `channel_connected:slack` |
| 6 | `pr-watch` | First PR watch | "Watch this PR and tell me when CI goes green" | Prefill | A `monitor_start` or `monitor_watch` tool call completes | `sel_event_seen:monitor_start` or `sel_event_seen:monitor_watch` |
| 7 | `crew-mode` | Used Crew Mode | A new chat opens in Crew Mode, prefilled with "Split this across a few teammates working in parallel: …" | Prefill | A user-sent turn starts in a Crew Mode session | none (see below) |

Known limitation of the `parallel-chats` event: `session_create` records the agent creating a child chat, not two chats running at once. A user who opens two chats from the UI and runs them side by side never calls it, and a single agent-created child, even a serial one, counts. It is accepted as the Ship 1 proxy because it is the cheap event the starter task itself produces; once the Ship 2 peak counter exists (3.3), `chats-concurrent >= 2` can replace or join it.

`mate` counts only a user turn in the Mate's chat, not the Mate existing: every install creates the Mate automatically, so "has a Mate" does not mean "used the Mate".

**Where Crew Mode lives, and which signal (D8).** Crew Mode is not a global setting. `agent.crew_mode_agent` (`config/sections_agent.py`) only picks which conductor template Crew Mode uses when turned on; it has a value by default, so it says nothing about use. The real "turned on" is a stamp on the session: when a session switches to the conductor template, `crew_mode.py` writes `crew_mode_agent` on it (`STAMP_FIELD` in `crew_mode.py`), persisted with the session metadata (`_read_crew_mode_agent` in `slot_persistence/metadata_codec.py`).

The event: in the turn-start hook (3.4), if the turn is user-sent and the session's `crew_mode_agent` is non-empty, the `crew-mode` counter goes up. It reads one in-memory field and no file. Reason: the stamp alone would also count "turned on and immediately off"; requiring a user turn is what makes it "used".

Known miss: there is no backfill probe, so an existing user who used Crew Mode before upgrading is not credited until their next Crew Mode turn. Crediting them would mean scanning all session metadata, which is not worth it; they can also use "Mark as done".

Starter task: the new chat opens through the same path as the existing Crew Mode toggle (so the session carries the stamp), then the task is prefilled. It does not send directly, because the conductor dispatches several workers and the spend has no upper bound (fails rule 5 in 4.5).

Not selected: knowledge base and voice (moved to the "Later" batch, see 6); skills, workflows and browser control (the starter task can hardly show a result within a minute, or needs something installed first).

Ordering rule: sort by the fixed `order`, filter out rows that are done, skipped (see 4.4) or unavailable, and take the first 3. Not random, because the empty state is a recommendation: the user should see the same cards each time until they finish one.

### 3.3 Usage milestones

Usage milestones have no starter task, never appear in the empty state, are never raised by the Mate, and cannot be marked done by hand. A tier is reached when its counter reaches it, and stays reached.

**Sessions: chats opened.** At the moment a person opens a new chat, `chats-opened` goes up by one. The hook is the same call site as the existing durable user-chat counter (`session_pulse_counter.increment_user_session_count_off_loop()` in `dashboard/state_parts/slot_directory.py`), which already counts only user-origin chats: not cron, app or system slots, and not agent-driven creates such as Crew Mode workers. The milestone counter is a separate key in its own file, so the survey counter's meaning does not change.

Deleting chat history does not lower the counter, because nothing ever decrements it. The transcript directory is listed only once, for backfill on upgrade: names with the `dashboard_chat-` prefix under `sessions/` and `sessions/archive/` are counted without opening any file. That count includes old worker chats, so a long-running install may land straight on a high tier after upgrading, without a notice (4.4).

**Messages: turns sent.** At the start of each user-sent turn, `turns-sent` goes up by one. Hidden turns (Mate check-ins, walkthroughs) are not user-sent and do not count. No backfill: it starts at 0 on upgrade, because counting past turns would mean opening transcripts.

**Streak (D12).** A streak is the number of local days in a row on which the user sent at least one turn. It uses the same user-sent turn-start hook:

- The day is the date in the user's local time zone (the configured `timezone`, else the machine's), so a day boundary is the user's midnight.
- On a user turn: if today equals `last_day`, nothing changes. If `last_day` is yesterday, `current` goes up by one. Otherwise `current` becomes 1. Then `last_day` is today and `best` becomes `max(best, current)`. So at most one file write per day comes from the streak.
- On read: if `last_day` is before yesterday, the panel shows `current` as 0. Missing a day resets the current streak, never the best.
- The streak tiers 3 / 7 / 30 / 100 read `best`, so a reached tier never goes away.
- No penalty, no notice about a streak at risk or lost, and nothing sent to bring the user back for the streak.
- No backfill: on upgrade it starts at 0.

**Running at once (Ship 2).** Nothing records this number today, so this counter holds a peak instead of a sum:

- At turn start (3.4), count the in-memory sessions whose `turn_running` is true (`_ChatSlot.turn_running` in `state_parts/chat_slot.py`), this one included.
- If the count is higher than the stored value, store the new value. Otherwise write nothing.
- Restart-safe: the peak lives in the file. A restart itself stops every turn, so it cannot over-count.
- It counts "turns running at the same time", not "tabs open at the same time". Hidden turns count.

### 3.4 Where events are counted

Each hook calls one helper, `milestones.record(id)` (or `record_peak(id, n)` and `record_active_day()`). The helper is best-effort: it never raises into its caller and writes off the event loop (4.4). Each hook sits after the action has succeeded, so a refused or failed action counts nothing.

| Event | Counter | Hook (repo-relative) |
|---|---|---|
| Artifact created | `widgets` | `src/kiro_crew/artifacts_writes.py`, `create()` after the write succeeds. This covers chat widgets, the artifact tools and the dashboard, because the MCP tools post to the gateway |
| Scheduled task added | `scheduled-task` | `src/kiro_crew/cron_service/service/job_writes.py`, `add_job()` after the job is stored |
| `session_create`, `monitor_start`, `monitor_watch` completed | `parallel-chats`, `pr-watch` | `src/kiro_crew/dashboard/chat_turn/tool_results.py`, beside `crew_log_emit.on_tool_completed`, only when the call was not refused and did not fail |
| Slack connected | `slack` | `src/kiro_crew/dashboard/messaging_api/slack_settings.py`, `_slack_config_save_locked()` after a successful save |
| Turn starts | `turns-sent`, streak, `mate`, `crew-mode` (user-sent turns only); `chats-concurrent` (all turns) | `src/kiro_crew/dashboard/chat_turn/turn_dispatch_gates.py`, beside `crew_log_emit.on_turn_started` |
| User opens a new chat | `chats-opened` | `src/kiro_crew/dashboard/state_parts/slot_directory.py`, beside `increment_user_session_count_off_loop()` |

The tool-result hook does not depend on the crew log being on; it sits beside that call, not inside it.

Write cost: one small off-loop write per user turn (for `turns-sent`) and per counted event, in the same cost class as the session-pulse counter's write per chat. Writes are coalesced under one lock, so a burst of events costs one write.

## 4. Design

### 4.1 Data model

A new module `src/kiro_crew/milestones.py`, shaped after `VideoEntry` (a frozen dataclass, validated at startup; a bad row is logged and dropped so the API never 500s):

```python
@dataclass(frozen=True)
class Milestone:
    id: str                         # slug; also the i18n key, the counter key and the state key
    group: Literal["features", "sessions", "turns", "streak", "concurrent"]
    order: int                      # order inside its group, in the panel and in recommendations
    doc: str = ""                   # required for feature rows; must be in tips_allowlist.TIP_DOC_ALLOWLIST
    video_id: str = ""              # optional; points at an entry in the feature_videos catalog
    starter: Starter | None = None  # feature rows only; all other groups must be None
    tiers: tuple[int, ...] = (1,)   # thresholds on this row's counter; feature rows are (1,),
                                    # usage rows strictly increasing, e.g. (10, 100, 1000)
    counter_mode: Literal["sum", "peak", "streak"] = "sum"
    backfill_when: tuple[str, ...] = ()   # one-time backfill on upgrade only; reuses probe_fires()
    available_when: tuple[str, ...] = ()  # "unavailable on this install" when any fires, e.g. disabled by policy
    min_version: str = ""

@dataclass(frozen=True)
class Starter:
    kind: Literal["prompt", "route"]  # prompt: into the composer; route: open an in-app page
    send: Literal["prefill", "send"] = "prefill"  # "send" only for low-cost rows (4.5, D1)
    route: str = ""                   # for kind=route; validated with _CTA_ROUTE_RE
    agent: str = ""                   # optional: which member's chat runs it (e.g. the Mate)
    crew_mode: bool = False           # crew-mode row only: the new chat opens in Crew Mode
```

Catalog validation (a row that fails is dropped and logged):

- A row outside `features` must have at least one tier above 1, and `starter is None`. This rule guarantees a usage milestone or a streak can never become an empty-state card or a "Mark as done" row.
- A `features` row must have `doc`, `starter` and `tiers == (1,)`.
- A row with `send == "send"` must be on the low-cost list in 4.5. `send` defaults to `"prefill"`, so a row that forgets to set it is never sent directly.

The starter task's text is **not in the dataclass**; it lives in the frontend i18n catalog under `milestone.<id>.title` / `.blurb` / `.starter_prompt`, and a tier name is `milestone.<id>.tier` (with a `{n}` placeholder). Reason: this is exactly the text the user sees in the composer and sends, so it must be in the user's language; `website/src/i18n/locales/*.json` already has `catalogParity.test.ts` to keep every locale's keys complete.

### 4.2 Where the table lives: built into the server, not a manifest

| Option | Pros | Cons |
|---|---|---|
| A. Built-in server constant (recommended) | Ships with the release, testable; the counters and the state file are on the gateway already | Adding a row needs a release |
| B. Signed manifest delivery (like Feature Videos) | Rows can be added without a release | A starter task is a prompt sent to a model; remotely editable = remotely injectable. A signature proves origin, not that the content is harmless |
| C. Frontend constant | Simplest | The events happen in the gateway (artifact store, cron service, turn start), so the state can only be computed there |

Option A. Clips still come from the Feature Videos manifest: the table stores only `video_id`, and whether a clip is downloaded and playable is answered by `feature_videos.offerable()` and the existing `_hosted_offers`. That way the table holds no remotely editable text that reaches a model. Since D1 lets some prompts send directly, option A is required: directly sent text may only be a reviewed constant that ships with the release.

### 4.3 API

One group of routes, next to `dashboard/routes/realtime.py`, with the same auth as `/api/feature-videos/*`:

```
GET  /api/milestones
→ {
    "enabled": true,
    "features": { "done": 3, "total": 7 },           // top-bar ring
    "streak": { "current": 4, "best": 9 },           // top-bar number and panel
    "groups": [
      { "id": "features", "items": [
        { "id": "widgets",
          "state": "done" | "open" | "skipped" | "unavailable",
          "done_at": 1793170000.0 | null,
          "source": "event" | "backfill" | "manual" | null,
          "video": <Offer.payload() | null>,       // reuses the feature_videos Offer shape
          "starter": { "kind": "prompt", "send": "send", "agent": "", "crew_mode": false } | null,
          "doc": "artifacts.md" } ] },
      { "id": "sessions", "items": [
        { "id": "chats-opened", "value": 37, "tiers": [10, 100, 1000],
          "tiers_reached_at": { "10": 1793170000.0 } } ] },
      { "id": "streak", "items": [
        { "id": "streak-best", "value": 9, "tiers": [3, 7, 30, 100],
          "tiers_reached_at": { "3": 1793000000.0, "7": 1793100000.0 } } ] }
    ],
    "recommended": ["widgets", "scheduled-task", "mate"],   // at most 3, features group only
    "notices": ["widgets", "chats-opened:10"]               // due and not yet shown
  }

POST /api/milestones/event          { "id": "widgets", "action": "starter_clicked" | "skipped" | "video_seen" | "notice_shown" | "panel_opened" }
POST /api/milestones/mark-done      { "id": "slack" }    // features group only, see 4.6
POST /api/milestones/mark-all-done  {}                   // every open feature row, see 4.6
POST /api/milestones/skip-all       {}                   // empty state "Don't show again", see 4.5
POST /api/milestones/walkthrough    { "id": "slack" }    // Ship 2, see 4.8
POST /api/milestones/daily-nudge    {}                   // Ship 2, see 4.8
```

- `recommended` is filtered on the server and drawn only from the `features` group. The frontend has no way to put a usage milestone or a streak in the empty state.
- `mark-done` refuses a non-feature id or an unknown id with 400. Neither marking route takes a count or a time from the client; the server stamps `now` and `source=manual`.
- Backfill probes are not sent to the frontend, for the same reason `Offer.payload` omits `used_when`: they describe local state the frontend does not need. Each usage row's `value` is the one number the panel shows.
- Temporary and incognito sessions get `{"enabled": false}`, the same rule as Feature Videos.
- `GET` reads the state file only; no probe runs on a normal read. The file read runs in `asyncio.to_thread`.
- When the frontend reads: on opening the empty state, on opening the panel, and when a user-sent turn ends (reusing the existing turn-end event). If `notices` is non-empty it shows them and then posts `notice_shown`.

### 4.4 State and the local file

```
feature milestone done = counter[id] >= 1   OR  marked done by hand
usage tier reached     = counter[id] >= tier      (streak tiers read streak.best)
```

**The state file (D9).** One file per machine: `milestones.json` in `config_dir()`, which is `~/.kiro/crew/milestones.json` by default (it follows `KIROCREW_HOME`). Mode 0600, written with `atomic_write`, beside `feature_videos_state.json` (see `_state_path` in `feature_videos.py`). Writes run off the event loop under one process lock, the same pattern as `session_pulse_counter.py`. A read or write error is logged and never reaches the caller: a milestone can never break artifact saves, cron adds, chat creation or turns.

```json
{ "version": 1,
  "counters": {
    "widgets":          {"count": 3,   "first_seen": 1793170000.0},
    "chats-opened":     {"count": 37,  "first_seen": 1792000000.0},
    "turns-sent":       {"count": 412, "first_seen": 1793000000.0},
    "chats-concurrent": {"count": 4,   "first_seen": 1793100000.0} },
  "streak": {"current": 4, "best": 9, "last_day": "2026-10-30"},
  "reached": {
    "widgets":         {"at": 1793170000.0, "source": "event"},
    "slack":           {"at": 1793180000.0, "source": "manual"},
    "chats-opened:10": {"at": 1792000000.0, "source": "backfill"},
    "streak-best:7":   {"at": 1793100000.0, "source": "event"} },
  "skipped": ["pr-watch"],
  "noticed": ["widgets"],
  "backfilled": true,
  "nudges": {"last_date": "2026-10-30", "last_id": "slack", "ignored_streak": 1} }
```

- `counters`: one entry per counter id. `count` only grows (`sum` adds one per event; `peak` keeps the max). `first_seen` is the time of the first event and never changes.
- `streak`: current and best streak in days, and the last local day with a user turn (3.3).
- `reached`: written the moment a threshold is crossed (or a row is marked done), so the panel has a date and the notice has a key. Each tier gets its own key, such as `chats-opened:10`. `source` is `event`, `backfill` or `manual`. An entry is never removed.
- `skipped`: new-feature rows the user clicked "Skip" on in the empty state (D7 / former Q4). They count as "handled", so the empty state stops recommending them; they **do not count as done**: the ring and the panel still show them open, and real use completes them later as usual.
- `noticed`: milestones already announced, so each notice is shown only once.
- `backfilled`: whether the one-time backfill has run.
- The file holds ids, counts, dates, timestamps and source labels only. It never holds chat content, file names, PR links or channel names.

**Count events, not final state (D10).** The hooks in 3.4 run at the moment the thing happens. Deleting the artifact, the scheduled task or the chat later changes nothing, because no counter goes down and no `reached` entry is removed. This replaces the earlier plan of polling `used_when` probes over artifacts and the audit log on every read, and with it two problems that plan had: the audit-log probe reads only the last 500 lines (`_SEL_PROBE_LIMIT`), and it matched denied calls as well as successful ones.

**One-time backfill on upgrade.** On the first read where `backfilled` is false (a new install, or the first read after upgrading), the server runs each feature row's `backfill_when` probes once and lists the transcript directory once for `chats-opened` (3.3). Every row already satisfied gets its counter raised to at least the threshold and a `reached` entry with `source: "backfill"`, and goes straight into `noticed`, so an existing user does not get a dozen notices on upgrade day. `turns-sent`, the streak and the peak have no backfill and start at 0. Then `backfilled` is set and the probes never run again.

The backfill probes that read the audit log add an outcome filter, as a new parameterized probe or an option on the existing one, so a denied or failed `cron_add` or `session_create` row is not credited (deny rows share the `operation` name, see `security/deny_gate.py`). The Feature Videos probe keeps its current behavior, where a false positive only hides a clip.

**Local only (D9).** v1 has no server state, no cloud copy and no sync. Each machine counts on its own: a user with two machines sees two separate sets of milestones and two streaks. A machine that starts fresh runs the same backfill from its own local state, without notices.

**Seam for a later sync (not built).** Each counter is just a `count` and a `first_seen`, and each reached entry is just an `at` and a `source`, so two machines' files can be merged later without conflicts: take `max(count)`, `min(first_seen)` and `min(at)` per key, and union `reached`, `skipped` and `noticed`. For the streak, `max(best)` merges; an exact cross-machine current streak would need a list of active days, which v1 does not keep. v1 writes no sync code; the shape is kept this simple so it stays possible.

**User-editable, by design.** The file and the config are the user's own and can be edited by hand. Editing them changes only the user's own checkmarks. That is accepted for a personal tool; there is no anti-cheat, signing or server check.

New backfill probes (both go into `feature_videos._PROBES` / `_PARAM_PROBES`, shared by both features):

| Probe | Reads | Note |
|---|---|---|
| `channel_connected:<name>` | That channel's connection state (Slack reads the existing connection check, not the token contents) | Answers only "connected or not"; never touches credential values |
| `mate_thread_has_user_turn` | Whether the Mate's pinned chat has a user-sent message | Tells "has a Mate" apart from "used the Mate" |

v1 no longer needs `knowledge_nonempty` (the knowledge base moved to Later).

### 4.5 Surface 2: the new-chat empty state

> **Replaced by section 8 (D15, 2026-10-31).** The layout below (a clip tied to the first recommended row, "Things you can try", three cards with "Do it" / "Fill it in for me" / "Take a look" buttons, "Skip" and "Don't show again") is not built. The new-chat page keeps its existing suggestion cards and replaces only the last one with a single "Try today" entry card (8.6). The intro video moves to the top of the Explore panel (8.2). The send-mode rules and the low-cost table in this section still apply, now to the "Try it" button in the Explore panel.

`ChatEmptyState.tsx` switches to the milestone layout only when all of these hold, and otherwise stays as it is today:

- `GET /api/milestones` returns `enabled: true` with a non-empty `recommended`;
- the slot list has loaded (reusing the existing `selectSlotListPending` check, so a person with many chats is not mistaken for a newcomer).

An empty `recommended` means every new-feature row is done, skipped or unavailable. The empty state then returns to today's page; the top-bar indicator and panel are unaffected.

Layout (top to bottom):

```
[ 10-second clip (when the first recommended row has one) ]
Things you can try
┌──────────┐ ┌──────────┐ ┌──────────┐
│ Widget   │ │ Scheduled│ │ Meet the │
│          │ │ task     │ │ Mate     │
│ one line │ │ one line │ │ one line │
│ [Do it]  │ │ [Fill it │ │ [Take a  │
│          │ │  in]     │ │  look]   │
│    Skip  │ │    Skip  │ │    Skip  │
└──────────┘ └──────────┘ └──────────┘
[ Start a new chat ]   Don't show again
```

- Cards hold new-feature rows only. Sessions, messages, streaks and running-at-once never appear here (D5, D13): every card here is "go do one thing"; a volume milestone here would become "go open more chats" and push spend up.
- Progress is not shown here; it lives only in the top-bar indicator, so there are never two numbers.
- The card button's label follows the send mode, so the user knows what will happen before clicking: "Do it" for direct send, "Fill it in for me" for prefill, "Take a look" for open page.
- "Skip" adds the row to `skipped` and the next one moves up.
- "Don't show again" calls `POST /api/milestones/skip-all`, which marks every remaining new-feature row skipped. The empty state immediately returns to today's page; the panel keeps showing true progress, and features used later still count. It differs from "Mark all as done" in the panel (4.6), which marks rows done instead.
- The "Start a new chat" button and the error message stay as they are.
- *(Replaced by section 8.2: the intro video is a card at the top of the Explore panel.)* The clip reuses the `StartupVideoModal` player component; watching it records `seen` in the Feature Videos state under the same video id, so the startup modal does not play it again.
- Cards use a fixed layout filled only with text (i18n strings); no model output is rendered.

**How a starter task reaches the composer.** The existing channel: `createSlot(...)` creates a chat, then `dispatch(setPendingInput(text))`, and `usePendingInputIntake` (`launchIntake.ts`) puts the text into the new chat's composer and lights up `prefillHint`.

- `send: "prefill"`: stops there; the user presses send.
- `send: "send"`: also goes through `autoSendRef` and sends. `launchIntake.ts` states "send only on an explicit request"; the user clicking "Do it" on the card is that explicit request, and the prompt is a built-in constant, not outside input.
- `kind: "route"`: just `navigate(route)`, e.g. the Mate row opens `/members?member=mate` (matching the existing crewmates intro's `cta_route`).
- When `agent` is set, the new chat uses that member.
- When `crew_mode: true`, the new chat opens through the same path as the existing Crew Mode toggle, then prefills.

Clicking a starter task completes nothing by itself. The row is done when the feature actually happens and its hook counts it (3.4).

**Which rows send directly (D1, product owner, 2026-10-29).** Low cost means all five rules hold:

1. One-time: creates no task that runs again;
2. Local only: sends nothing outside and connects no outside account;
3. Changes no config and imports no data;
4. Needs nothing more from the user (a path, a link, a permission);
5. Bounded spend: a few model turns at most.

| Row | Mode | Reason |
|---|---|---|
| `widgets` | Send directly | One turn that writes one local artifact; all five rules hold |
| `parallel-chats` | Send directly | Opens two local chats that each run a turn or two and stop; nothing outside, no scheduled task |
| `scheduled-task` | Prefill | Creates a task that runs every day and spends a turn each day; fails rule 1 |
| `pr-watch` | Prefill | Needs a PR link from the user; the watch loop runs repeatedly; fails rules 1 and 4 |
| `slack` | Prefill | Connects an outside account; fails rules 2 and 4 |
| `crew-mode` | Prefill | The conductor dispatches several workers and the spend has no upper bound; fails rule 5 |
| `mate` | Open page | Sends no prompt; only opens the Mate chat, where the Mate's first welcome takes over |

Only `widgets` and `parallel-chats` send directly. Sending directly is the most direct way to deliver a result first; it is limited to low-cost rows because it spends turns on the user's behalf and skips the moment where the user sees what will happen.

### 4.6 Surface 1: top-bar indicator and Milestones panel

> **Replaced in part by section 8 (D15, D17, 2026-10-31).** The indicator is its own pill beside the readout capsule, not a control next to the bell. It reads "Explore 2/7" with "5-day streak" as smaller text, rather than a bare number (8.7). Advanced badges use one calm color (8.4). The panel is named "Explore"; its parts and order are in 8.2. "Mark as done", "Mark all as done", the per-row dates and the no-nudging rules below still apply.

**Indicator (D14).** A small, quiet control next to the notification bell in `shellHeader.tsx`. No trophy and no badge colors:

- A thin ring showing `features.done / features.total`. `unavailable` rows are left out of the total; `skipped` rows count in the total but not as done. Rows marked done by hand count as done.
- Beside the ring, the current streak as a plain number (e.g. `4`) when it is 1 or more; nothing when it is 0.
- Usage milestones are not in the ring. They have no ceiling, so the ring would never fill.
- Once every new-feature row is done, the ring stays full and only the streak number changes.
- The mobile top bar (`MobileTopBar.tsx`) shows the same ring and number.
- Hidden when the main switch is off.

**Milestones panel.** Opens from the indicator, shaped like the notification panel (`NotificationSheet`) and sliding in from the same side. Plain rows, checkmarks and thin progress bars:

```
Milestones                                    [Mark all as done]
Streak   4 days · best 9      ✓3  ✓7  ○30  ○100
── New features ──  3 / 7
✓ First widget              2026-10-28
✓ Met the Mate              2026-10-29
✓ Slack connected           2026-10-30  (marked done)
○ First scheduled task      [Try it]  Mark as done
○ First PR watch            [Try it]  Mark as done
...
── Sessions ──
Chats opened     37 / 100   ▓▓▓░░░░░░░   ✓10 (2026-10-21)  ○100  ○1000
── Messages ──
Messages sent    412 / 1000 ▓▓▓▓░░░░░░   ✓100 (2026-10-25)  ○1000  ○10000
── Running at once ──  (Ship 2)
Running at once  4 / 5      ▓▓▓▓▓▓▓▓░░   ○5  ○20  ○50
```

- Done rows show the date (local time zone). A row marked done by hand says so in small text.
- Open feature rows have a "Try it" button that behaves exactly like the empty-state card (same starter task, same send mode). Skipped rows also have "Try it", so the user can always come back.
- **Mark as done (D11).** Each open or skipped feature row has a quiet "Mark as done" link. It calls `POST /api/milestones/mark-done`, which writes a `reached` entry with `source: "manual"` to the same local file. The row is done at once and leaves the empty state. No notice: the user just did it.
- **Mark all as done (D11).** One link in the panel header, for users who do not want guidance (D11 called it "Unlock all"; D14 renames it). It asks once ("Mark every new feature as done?"), then calls `POST /api/milestones/mark-all-done`, which marks every open or skipped feature row done with `source: "manual"`. The empty state returns to today's page.
- Usage rows and the streak show the current value, the next tier and each tier's date. No "Try it", no "Mark as done", no nudging text, and "Mark all as done" does not touch them.
- A manual mark does not change any counter. If the feature is used later, its counter still records it, and `first_seen` keeps the real time.
- The panel renders only a fixed layout and i18n text.
- Opening the panel records one `panel_opened` metric.

### 4.7 Surface 3: quiet notice

- One small line through the existing toast component, in a corner, in the normal text color, dismissing itself after a few seconds; it blocks nothing and takes no focus. No icon animation, confetti or sound.
- Fixed text: "Milestone: {title}"; for a tier, "Milestone: 100 chats" or "Milestone: 7-day streak". Clicking it opens the panel at that row. *(Wording replaced by section 8.5: the notice drops the word "Milestone".)*
- Each feature row and each tier is announced once (`noticed`). Backfilled and manual rows are not announced.
- When one read finds several new milestones, they merge into one line, "3 new milestones", which opens the panel. *(Wording replaced by section 8.5.)*
- Notices for usage tiers and streaks carry no button and no text like "13 more chats to the next one". Nothing is ever shown about a streak being at risk or lost.

### 4.8 Surface 4: Mate walkthrough and daily task (Ship 2)

> **Unchanged by section 8, and still out of Ship 1 (D15).** The client-side "Try today" pick (8.3) is not this daily task: it makes no model turn. If the daily task ships later, it should raise a feature from the same day's "Try today" list so the two never disagree.

Both cover **new-feature rows only** and never mention usage milestones or streaks.

**Mate walkthrough.** The whole table is not put into the Mate's system prompt (to avoid prompt bloat). Instead:

- Add a copy key `milestone.walkthrough` to `crewmate_prompts.json`, with placeholders `{feature_title}`, `{doc}`, `{starter_prompt}`. Content: say in two or three sentences what the feature does, then offer to run the starter task for the user, and wait for the user to agree.
- Trigger: a "Let the Mate show me" button on empty-state cards and in the panel → `POST /api/milestones/walkthrough {id}` → the server opens the Mate's pinned chat and runs a hidden turn with `mate_welcome.dispatch_hidden_turn(state, slot, prompt, human_seen=True)`. Language uses the existing `hidden_turn_language()` + `with_language()`.
- A hidden turn is not user-sent (`_turn_user_sent` is false), so most cards and `update_self` are refused. The walkthrough turn only needs to talk and make an offer, which is enough. Running the starter task happens after the user replies, in a user turn. A hidden turn does not count toward `mate`, `turns-sent` or the streak (3.3).
- The Mate's first welcome (`welcome.offer_tools`) gets one line at the end: "After asking the name, offer to do one small thing for the user", naming no specific feature. That line is the only change to the Mate's first welcome.
- At most one walkthrough per feature per day; it does not trigger while the Mate chat is running a turn (reusing the `slot.running` check).

**Daily task.** **Decided (D2, product owner, 2026-10-29):** the daily task is a real Mate model turn, at most once a day, triggered the first time the user opens the Mate chat that day. No fixed copy, no fixed cron time.

Each day pick one new-feature row that is neither done nor skipped (the first by `order`, skipping the one raised yesterday) and the Mate raises it once. It never mentions the streak.

Why trigger on "the first time the user opens the Mate chat that day":

- The user is looking at that moment, so the prompt does not land in a chat nobody reads (the `goal_reminder` check-in has the "does nothing if the chat is not open" problem);
- No hosted cron, so the Schedule page does not gain a task the user does not understand;
- Restart-safe: the condition comes from durable state (`nudges.last_date`), not process memory.

Implementation: when the client opens the Mate chat, at the point it greets for today (after `POST /api/members/{slug}/greet`), it calls `POST /api/milestones/daily-nudge`. The server:

1. Sends nothing when the main switch or `milestones_daily_nudge` is off, every new-feature row is handled, the first welcome is not finished, the chat is running, or the chat already has a user message today.
2. Creates a `milestone_nudge_<YYYY-MM-DD>.json` marker file with `O_EXCL` and sends only if it wins (same as `claim_greeting_marker`, guarding against multiple tabs and reloads). The date is in the user's time zone.
3. Runs a hidden turn with the `milestone.daily_nudge` copy (`dispatch_hidden_turn`, `human_seen=True`): one sentence on what to try today, plus a starter-task suggestion. This is a model turn, so it spends at most one turn per day.

**When it stops.** Every new-feature row is handled (done, marked done or skipped); or after 3 days in a row of nudges with no same-day reply from the user (`ignored_streak >= 3`), after which it drops to at most once a week; or the user turns it off.

On the first dashboard open of the day, a `passive` notification (list only, no sound) also points to the Mate chat, giving users who rarely open the Mate a way in. The notification itself starts no model turn; the turn still only happens when the user opens the Mate chat.

### 4.9 Switches and config

**Decided (D3, product owner, 2026-10-29):** the main switch defaults on. Since 2026-10-30 the keys follow the milestone naming (D14).

| key | Default | Effect |
|---|---|---|
| `dashboard.milestones_enabled` | `true` | Main switch: the indicator, panel, empty-state cards, notices and Mate entry points all follow it. Off hides every surface; the state file is kept and the hooks keep counting, so turning it back on shows true progress |
| `dashboard.milestones_daily_nudge` | `true` | Turns off only the daily task (Ship 2) |

Both go into `DashboardConfig` in `src/kiro_crew/config/sections_dashboard.py`, read the same way as `feature_videos_enabled` (`_safe_bool` in `loader_builders.py`). The Settings page gains two toggles in the Display group. The empty state's "Don't show again" is not a switch; it is "skip all" (4.5).

Defaulting on matters for existing users: a saved config without the key reads `true`, so an existing install also shows the indicator and the milestone empty state after upgrading. That is fine: features already used are credited by the one-time backfill (without notices), so someone who has used all 7 still sees today's empty state, and someone who has used some sees the rest recommended. Anyone who wants none of it can press "Mark all as done" or "Don't show again". Defaulting off would mean new users never see it, which defeats the design; nor does it depend on clips being available, unlike Feature Videos.

Managed fleets: a policy-managed install can turn the whole thing off through the existing capability policy (new scope `capabilities.milestones`); when off, `GET /api/milestones` returns `enabled: false`, and policy takes precedence over config.

### 4.10 i18n

- *(The layout string list below is replaced by section 8.8.)* Frontend text: `milestone.<id>.title`, `.blurb`, `.starter_prompt`, `.tier`, plus the layout strings ("Milestones", "New features", "Sessions", "Messages", "Streak", "Running at once", "{n} days · best {best}", "Things you can try", "Do it", "Fill it in for me", "Take a look", "Try it", "Skip", "Don't show again", "Mark as done", "Marked done", "Mark all as done", "Mark every new feature as done?", "Let the Mate show me", "Milestone: {title}", "{n} new milestones"), added to `website/src/i18n/locales/en.json`; other locales are filled in through the `TRANSLATION-PROMPT.md` process, with `catalogParity.test.ts` as the backstop.
- Mate copy: `crewmate_prompts.json` gains `milestone.walkthrough` and `milestone.daily_nudge`, with their purpose and placeholders written in the `_about` section. Language is handled by `with_language()`; the copy itself is written in English.
- Starter prompts are translated, because the user sees them in the composer; for the two direct-send rows, what gets sent is text in the user's language.

### 4.11 Metrics

Product telemetry uses only the existing anonymous metrics counters (`metrics.events.emit_counter`). They carry a milestone id from the fixed table and closed-enum attributes, nothing else: no content, no session id, no raw counts from the local file beyond a tier name. The local state file itself is never uploaded.

| Counter | Attributes | Question answered |
|---|---|---|
| `kirocrew.milestone.shown` | `surface` (empty_state / panel / mate / daily), `milestone` | Exposure of feature rows |
| `kirocrew.milestone.starter_clicked` | `surface`, `milestone`, `send` (prefill / send / route) | Click rate; separately for direct send and prefill |
| `kirocrew.milestone.prefill_sent` | `milestone` | Share of prefills the user actually sent |
| `kirocrew.milestone.reached` | `group`, `milestone` (tiers carry the tier, e.g. `chats-opened:100`, `streak-best:7`), `via` (starter / organic / backfill / manual) | Whether starter tasks really lead to use; how many reach each tier |
| `kirocrew.milestone.skipped` | `surface`, `milestone` ("Don't show again" records `all`) | Which recommendations annoy people |
| `kirocrew.milestone.mark_all_done` | — | How many people want no guidance at all |
| `kirocrew.milestone.panel_opened` | — | Whether anyone opens the panel |
| `kirocrew.milestone.notice_shown` | `group` | Notice volume, to keep it quiet |
| `kirocrew.milestone.nudge` | `outcome` (sent / skipped_busy / skipped_done / skipped_off / replied) | Whether anyone answers the daily task (Ship 2) |
| `kirocrew.milestone.opted_out` | `surface` | How many turn off the main switch, given the default is on |
| `kirocrew.milestone.features_completed` | `via` (organic / mixed / manual) | Number of users with every new-feature row done, and how |

`via` on `reached`:

| `via` | Meaning | Counts toward effect |
|---|---|---|
| `starter` | A hook counted the event within 24 hours after that row's `starter_clicked` | Yes |
| `organic` | A hook counted the event with no recent starter click (always for usage tiers and streaks) | Yes |
| `backfill` | Credited once at upgrade | No |
| `manual` | "Mark as done" or "Mark all as done" | No |

Success criteria (read 4 weeks after Ship 1):

- Share of new installs that complete ≥3 new-feature rows within 7 days, counting `starter` and `organic` only, against a baseline for the same period before launch (D7 / former Q6: computed before Ship 1).
- Day-2 and day-7 return rate (the anonymous usage beacon `beacon.py` already records install activity per day and can be used directly), and the share reaching the 3-day and 7-day streak tiers.
- Opt-out rate (`opted_out` plus `mark_all_done`); because the default is on, this one is watched on its own.
- Guardrail: chats and turns per user per day should not rise abnormally because of usage milestones or streaks after launch. If they do, first check whether notices are pushing people.
- Daily task reply rate (Ship 2); below 10%, consider lowering the frequency or removing it.

## 5. Security and privacy

- Starter prompts are built-in constants plus i18n strings; they never come from a remote source, a model, or user data.
- The indicator, panel, cards and notices render only fixed layouts and text, never model-produced HTML.
- `doc` must be in `TIP_DOC_ALLOWLIST`; `route` reuses `_CTA_ROUTE_RE`, so it can only navigate within the app.
- **Local data.** The state file lives in `config_dir()` with mode 0600 and atomic writes. It holds only ids, counts, dates, timestamps and source labels. The hooks pass a counter id and nothing about the artifact, task, chat, message, PR or channel. Nothing in it leaves the machine.
- **User-editable.** The file and the config are the user's own and can be edited by hand. That only changes the user's own checkmarks and unlocks no capability, so it is accepted for a personal tool; there is no anti-cheat.
- Backfill probes answer only "yes or no"; they neither read nor return credentials or document contents. `channel_connected:slack` cannot read the token value. The transcript listing looks at file name prefixes only and never opens a transcript. The peak counter only counts in-memory flags.
- `mark-done` and `mark-all-done` take no count or time from the client and refuse non-feature ids.
- Hidden turns are not user turns, so sensitive tools and cards are refused as usual; the walkthrough only offers, and anything runs only after the user replies.
- Metric attributes carry no user content and no session id (4.11).
- Every hook is wrapped in try and writes off the event loop; an error is only logged and never blocks an artifact save, a cron add, a chat creation or a turn.
- Spend:
  - Direct send is limited to the `widgets` and `parallel-chats` rows (4.5), a few turns per click at most; `crew-mode` only prefills, because its spend has no upper bound.
  - Usage milestones and streaks never appear in the empty state, are never raised by the Mate, and their notices do not nudge; catalog validation guarantees only feature rows have a starter task (4.1). Nothing warns about a streak at risk. This keeps every surface from encouraging the user to open more chats or send more messages.
  - The daily task spends at most one turn per day (4.8).
  - `send` defaults to `prefill`, and catalog validation rejects direct-send rows not on the list.

## 6. Phased rollout

| Batch | Contents | Exit criteria |
|---|---|---|
| Ship 1 | `milestones.py` + local state file + event hooks for the 7 new-feature rows, sessions, messages and the streak (3.4) + one-time backfill with the new probes (`channel_connected:slack`, `mate_thread_has_user_turn`) + `GET /api/milestones` and `event` / `mark-done` / `mark-all-done` / `skip-all` + config keys (default `true`) + top-bar indicator + Milestones panel + empty state + quiet notices + i18n + metrics. On for everyone with the release (D3) | Backend: each hook counts once per successful event and nothing for a refused or failed one; a counter never goes down, and deleting an artifact, task or chat undoes nothing; the streak counts one per local day, resets current after a missed day, never lowers best, and handles the user's midnight and a restart; a hook error never breaks its caller; backfill runs once, credits with `source: backfill` and shows no notice; `mark-done` and `mark-all-done` write `source: manual`, refuse non-feature ids, and take no client time; only feature rows can enter `recommended`; `send` defaults to `prefill` and an off-list `send` row is dropped; the file is 0600 and holds no content. Frontend: nothing shows before load; direct send happens only for `widgets` and `parallel-chats`; skipped rows stay open in the panel; once everything is handled the empty state returns to today's page and the indicator stays; each notice shows once; no streak-loss text anywhere |
| Ship 2 | "Running at once" (peak counter) + Mate walkthrough + one line in the Mate's first welcome + daily task (D2) + passive notification | The peak only grows, survives restart, and writes the file only on a new high; the walkthrough hidden turn fires once and not while the Mate is busy; the daily task sends once per day across multiple tabs, reloads and restarts; nothing is sent before the first welcome finishes; frequency drops after 3 ignored days in a row; nothing is sent with `milestones_daily_nudge` off |
| Later | The baseline, computed after launch from retained data: share of new installs that used ≥3 features within 7 days (D7 / former Q6), after first confirming the existing telemetry can tell new installs apart; the number goes into an appendix of this RFC. It no longer blocks Ship 1. Knowledge base and voice feature rows (voice is not in v1, D7 / former Q8); relationship to Feature Tips (Q7); cross-machine sync, if ever wanted, using the merge seam in 4.4; after 4 weeks of data, review the rows, thresholds (including `turns`), ordering, low-cost list and defaults | Against the success criteria in 4.11 |

## 7. Decisions and open questions

### 7.1 Decided

**2026-10-29, product owner:**

- **D1 (former Q1) Should a starter task send on click, or only prefill?** Low-cost rows send directly; every other row only prefills the composer, and the Mate row sends no prompt but opens the Mate chat. The low-cost rules and the per-row classification are in 4.5: `widgets` and `parallel-chats` send directly. Checked against the five rules, the scheduled-task row moved from direct send to prefill.
- **D2 (former Q2) Model turn or fixed copy for the daily prompt?** A real Mate model turn, at most once a day, triggered the first time the user opens the Mate chat that day. A model turn speaks the user's language and can use context; fixed copy would need a new "write an assistant message without running a model" path that does not exist today. An `O_EXCL` day marker keeps spend to at most one turn a day across tabs, reloads and restarts. See 4.8.
- **D3 (former Q3) Main switch on or off by default?** On. Off by default would mean new users never see it. Existing users see it after upgrading, but features they already used are credited by the backfill. Managed fleets can turn it off with a capability policy. See 4.9.

**2026-10-30, product owner:**

- **D4 One table replaces the feature table.** One-time feature rows with starter tasks plus tiered usage rows; no points, leaderboards or rewards. D13 restructures it as milestones with features as one group. See 3.
- **D5 Surfaces.** A top-bar entry and a panel; the empty state carries only the next 3 feature rows and nothing else; each new one is announced once; once every feature row is handled the empty state returns to today's page, and the top-bar entry and panel stay. D14 sets their style. See 4.5–4.7.
- **D6 Phased rollout.** Ship 1, Ship 2, Later as in 6.
- **D7 Defaults for the open questions.** Q4: a skipped card counts as handled for the empty state but not as done. Q5: the rows are as in 3. Q6: compute the baseline before Ship 1. Q7: deferred. Q8: voice is not in v1.
- **D8 The Crew Mode signal.** Crew Mode has no global setting, only the `crew_mode_agent` stamp on a session. The event is "a user turn in a stamped session", counted in the turn-start hook. See 3.2.
- **D9 Local only.** Milestones live per machine in one local file under the Crew home (0600, atomic write). No server, no cloud and no cross-machine sync in v1; two machines count separately, and a new machine backfills from its own local state without notices. The record stores only per-key counts and times, so a later sync could merge by `max(count)` and `min(first_seen)`; no sync code is written. See 4.4.
- **D10 Count events, not final state.** Each thing is counted at the moment it happens, by a hook in the code path that does it (3.4), and a milestone is reached when its counter reaches the threshold. Deleting the thing later undoes nothing. Usage tiers read the same counters, which replaces the transcript high-water mark. The `used_when` probes run only once, as the upgrade backfill, marked `source: backfill`. Telemetry stays on the anonymous metrics counters; the local file holds counts and times only, never content. See 3.4 and 4.4.
- **D11 Manual marking.** Each open feature row has "Mark as done", and the panel has one control to mark them all (first named "Unlock all", renamed "Mark all as done" by D14). Both write the local file with `source: manual`. Usage milestones cannot be set by hand. Metrics tell organic, backfill and manual apart. No anti-cheat. See 4.6 and 4.11.
- **D12 Streaks.** Count local days in a row with at least one user-sent turn, in the user's time zone. Show current and best. Missing a day resets current, never best. Tiers 3 / 7 / 30 / 100 days, on best. Same local file. No penalty and no notice about losing a streak. See 3.3.
- **D13 Milestones first, features as one group.** The top level is milestones: new features, sessions (10 / 100 / 1000), messages, streak and running at once. The empty state still suggests only untried new features, never volume milestones or streaks, so the spend risk is unchanged. See 3.1.
- **D14 Quiet style and the "milestone" name.** No trophy icon and no fanfare. The top bar has a small ring for feature progress with the streak number beside it; the panel is a plain "Milestones" list with checkmarks and thin progress bars; a new milestone is one small line. The design uses "milestone" for all terms, keys, routes, metrics and the state file; the RFC file name keeps "achievement" so links stay stable. The local counters are user-editable, which is accepted for a personal tool. See 4.6 and 4.7. *(Amended by D17 on 2026-10-31: the top-bar label is "Explore N/M" with the streak as smaller text, and reached tiers show as tiered badges, Advanced ones in one calm color.)*

### 7.2 Open questions

- **Q7 Relationship to Feature Tips.** Deferred to the "Later" batch. Both promote features. Initial idea: while the empty state shows feature cards, Tips pause in that chat until the first turn ends, so two surfaces never pitch at once.

Q4, Q5, Q6 and Q8 were closed by D7.

**2026-10-31, product owner:**

- **D15 "Explore", "Try today" and one entry card.** Crew is a work tool, so the user-facing text drops game words. The panel is "Explore", with "Try today" (up to 3 untried features for today) and "Your progress" (streak and usage counts). The new-chat page keeps its suggestion cards and replaces only the last one with a "Try today" entry card. The top-bar ring shows a dot while "Try today" has something left; where it sits is set by D17. The Mate daily task stays out of Ship 1. See section 8.
- **D16 Final Ship 1 Explore panel, and a Ship 2 list.** Top to bottom: a "Kiro Crew in 15 seconds" intro video card (the only home for that video; no startup popup, not on the new-chat page), Try today (quick untried features only), Your progress (a "Getting started" pair for the first days, a progress bar toward each next tier, and a badge wall), then the full feature list. Badges are Regular (quiet line icon) or Advanced (one calm color); only Ship 2's Hard tier is showy, because owner feedback said trophies felt like too much. The streak stays "a day you used Crew" in Ship 1. Ship 2 items are listed in 8.10. See 8.2–8.4 and 8.9–8.10.
- **D17 Final Ship 1 rulings on the video, the top-bar entry and badges.** Three rulings settle what the build shows:
  1. *Intro video.* The video lives only in the "Kiro Crew in 15 seconds" card at the top of the Explore panel. It is not on the new-chat page, and there is no standalone startup popup for it. The new-chat page keeps its suggestion cards, with a "Try today" card as the last one while untried features remain. See 8.2 and 8.6.
  2. *Top-bar entry.* The ring sits in its own pill beside the readout capsule, as its own button, not inside the capsule, so the capsule keeps at most two buttons per row. The pill shows the ring and the label "Explore N/M" (for example "Explore 2/7"), with the streak ("5-day streak") as smaller secondary text. A small dot shows while something is left to try. Clicking it opens Explore. This replaces D15's capsule placement. See 8.7.
  3. *Badges (amends D14).* Your progress has a tiered badge wall, and each usage row shows a progress bar. The Regular tier uses quiet line icons; the Advanced tier uses one calm color; the showy style of the hardest tier is Ship 2. The overall style stays quiet. See 8.4 and 8.10.

## 8. Amendment, 2026-10-31 (product owner)

This section records D15, D16 and D17. Where an earlier section disagrees with it, this section wins. The local file, the API routes, switches, metrics and the security rules in sections 3–5 do not change, except for the one new counter in 8.4.

### 8.1 Naming and style: "Explore"

Crew is a work tool, so the look stays calm. User-facing text does not use "quest" or "trophy", and "milestone" is no longer the panel's name. Reached tiers show as badges (8.4), drawn as quiet line icons or a calm color; no trophies, confetti or fanfare. Owner feedback on Slack was that trophies felt like too much.

| Earlier name | New name |
|---|---|
| Milestones panel | **Explore** |
| New features group | **Try today** (today's pick, 8.3); the full list stays below it |
| Sessions, Messages, Streak, Running at once | **Your progress** |
| Top-bar number `4` | **`Explore 2/7`**, with **`4-day streak`** as smaller text (8.7) |

Internal names do not change. The module, config keys, routes, metric names, i18n key prefix and state file keep "milestone" (D14).

### 8.2 The Explore panel, top to bottom

The panel opens from the top-bar ring (8.7). Its parts, in order:

| # | Part | Section |
|---|---|---|
| 1 | "Kiro Crew in 15 seconds" intro video card | 8.2 below |
| 2 | Try today | 8.3 |
| 3 | Your progress: Getting started, progress bars, badge wall | 8.4, 8.5 |
| 4 | The full feature list, with "Try it" and "Mark as done" as in 4.6 | 4.6 |

**Intro video card.** One "Kiro Crew in 15 seconds" card plays the 15-second intro video. It stays until the person watches it or closes it, and then it is gone for good. That choice is stored locally, in the same Feature Videos `seen` / `dismissed` state on this machine (the local `feature_videos_state.json`). When the clip is not available on this install, the card is not shown. This card is the only home for the intro video: it is not on the new-chat page (8.6), and it is not shown through the standalone startup popup (`StartupVideoModal`). Other feature clips keep their current startup behavior.

A "Try it" button anywhere in the panel keeps the send modes in 4.5: only `widgets` and `parallel-chats` send at once, the Mate row opens its page, and every other row only fills the composer.

### 8.3 Try today

"Try today" shows up to 3 new-feature rows the user has not tried, picked fresh for each local day.

- **Only quick rows.** Try today picks only feature rows a person can finish in a few minutes: `widgets`, `parallel-chats`, `mate`, `scheduled-task` and `slack`. `pr-watch` (needs a PR link and waits on CI) and `crew-mode` (spend has no upper bound) stay in the full list but are never picked. The quick list is a fixed constant in the client.
- **Ring of rows.** The quick rows that are not `unavailable`, in `order`, as a ring. Its size `N` does not change when a row is done or skipped, so the starting point stays put for the whole day.
- **Pick.** Take the day number `d` (days since 1970-01-01 in the user's local time zone, the same day rule as the streak in 3.3). Start at index `d mod N`, walk forward around the ring, and take the first 3 rows whose state is `open`. The same day and the same state always give the same pick, on every tab and after a reload.
- **Drop out.** When a row becomes `done` (used, or "Mark as done") or `skipped` ("Skip" on its row), it leaves today's list at once. The other picked rows stay where they are, and the next `open` row further round the ring takes the free place, if there is one. So "Try today" holds fewer than 3 rows only when fewer than 3 quick rows are `open`.
- **Where it runs.** In the client, from the existing `GET /api/milestones` response (each feature row's `state`). No new server route, no new state field and no model turn. The server's `recommended` field is not used for this pick.
- **Empty.** When no quick row is `open`, "Try today" shows nothing, the top-bar dot is off (8.7), and the new-chat page shows no entry card (8.6).

### 8.4 Your progress

"Your progress" is where reached tiers live. It has three parts.

**Getting started (first days only).** A pair of rows shows first, above everything else in Your progress:

| Row | Done when | "Try it" |
|---|---|---|
| Get a first finished result | the first user-sent turn ends without an error | fills the composer with a short starter task |
| Set up a morning brief | the `scheduled-task` counter is at least 1 | fills the composer with a daily 9am "brief me on today" scheduled task (prefill only, rule 1 in 4.5) |

- The pair shows during the person's first 7 local days, counted from `first_seen` of the `turns-sent` counter, and only while at least one of the two is not done. After that it is hidden; both rows still count and earn their badges.
- "First finished result" is the one new counter in this amendment: `first-result`, recorded by the turn-end path for a user-sent turn that finished without an error, through the same best-effort `milestones.record()` helper (3.4). Hidden turns do not count. In the one-time upgrade backfill (4.4), it is credited with `source: backfill` when the backfilled `chats-opened` count is above 0, so an existing user is not asked to do it again.
- "Set up a morning brief" reuses the `scheduled-task` counter; it needs no new hook.

**Progress bars.** Every usage row shows a thin bar toward its **next** tier, with the numbers beside it: "Chats opened 137 / 1000", "Streak 5 / 7 days", "Messages sent 412 / 1000". A row whose last tier is reached shows a full bar and its final number. Feature rows have one tier, so they show as done or not done, without a bar. No bar carries nudging text such as "13 more to go".

**Badge wall.** When a tier is reached, its badge is added to a badge wall at the bottom of Your progress, with the date. Nothing is unlocked by hand except feature rows through "Mark as done" (4.6). Badges have tiers by how hard they are:

| Badge tier | Examples | Look |
|---|---|---|
| Regular | every feature row (first scheduled task, Slack connected, first finished result); chats opened 10; messages sent 100; streak 3 and 7 days | quiet line icon, normal text color |
| Advanced | chats opened 100; messages sent 1000; streak 30 days | one calm accent color, no motion |
| Hard (Ship 2) | chats opened 1000; messages sent 10000; streak 100 days; 50 running at once | see 8.10; in Ship 1 these draw in the Advanced look |

The badge tier is a fixed client-side table keyed by milestone id and tier value. It is derived from data the API already returns (`tiers_reached_at`, feature `state` and `done_at`), so it adds no server field.

**Streak (Ship 1).** The streak keeps its rule from 3.3: a local day counts when the person sent at least one turn ("a day you used Crew").

### 8.5 Notice wording

The quiet notice (4.7) keeps its behavior, but drops the word "Milestone": "First widget done", "100 chats opened", "7-day streak", and for several at once "3 new in Explore". Clicking it opens the panel at that badge. It never animates in Ship 1.

### 8.6 New-chat page: one entry card

The new-chat page (`website/src/components/WelcomeView.tsx`) keeps only its existing personal suggestion cards (`SuggestedCards`, served by `GET /api/suggestions`, with the built-in fallback cards).

- While Try today has at least one row (8.3), the **last** suggestion card is replaced by one "Try today" entry card. Clicking it opens the Explore panel. It does not start a starter task itself.
- When Try today is empty, no suggestion card is replaced.
- The entry card waits for `GET /api/milestones` to load; until then, and when the main switch is off, the page shows the normal cards.
- No intro clip, no "Things you can try" row, no extra row of cards, no "Skip" or "Don't show again" on this page. Apart from the one entry card, the page is as it is today.

This replaces the layout in 4.5. `POST /api/milestones/skip-all` has no caller in the Ship 1 frontend.

### 8.7 Top bar: an "Explore" pill beside the readout capsule (D17)

- The ring sits in its own pill, as its own button, beside the existing top-bar readout capsule (the capsule that holds the credits and system readouts in `website/src/shell/topbar/shellHeader.tsx`). It is not a segment inside the capsule, so the capsule keeps at most two buttons per row, and it is not a new control next to the bell.
- The pill shows the ring and a visible label, "Explore N/M": `N` is `features.done` and `M` is `features.total` from 4.6, for example "Explore 2/7". The label says what the ring measures and where the click goes.
- The current streak shows after the label as smaller secondary text, "5-day streak", when it is 1 or more, and nothing when it is 0.
- The ring shows a small dot while Try today (8.3) has at least one row left today. The dot carries no number and no color change.
- Clicking the pill opens the Explore panel.
- The phone layout, which does not render the capsule, shows the same entry just before the notification bell.

This replaces the placement and label in 4.6 and D15.

### 8.8 Labels

The layout strings in 4.10 become: "Explore", "Kiro Crew in 15 seconds" and the intro card's close label, "Try today", "Your progress", "Getting started", "Get a first finished result", "Set up a morning brief", the morning-brief starter prompt, "{value} / {next}", "Explore {done}/{total}", "{n}-day streak", "{n} days · best {best}", the badge-wall heading and per-badge date, "Try it", "Skip", "Mark as done", "Marked done", "Mark all as done", "Mark every new feature as done?", the "Try today" entry-card title and line, and the notice strings in 8.5. "Things you can try", "Do it", "Fill it in for me", "Take a look", "Don't show again", "Milestones", "Milestone: {title}" and "{n} new milestones" are removed. The button never says "Do it": "Try it" is the one label, and the row's blurb says whether it sends at once or fills the composer.

### 8.9 Ship 1 scope

Ship 1 is as in section 6, with these changes:

- The Explore panel as in 8.2: intro card, Try today, Your progress (Getting started, progress bars, badge wall with Regular and Advanced looks), full feature list.
- The new-chat page as in 8.6, and the top bar as in 8.7.
- Backend: the `first-result` counter and its turn-end hook (8.4). Nothing else on the server changes for this amendment.
- The streak counts a day the person used Crew (8.4).

The Mate walkthrough and the daily task (4.8) stay out of Ship 1.

### 8.10 Ship 2 list

Listed only; each item gets its own design before it is built.

| Item | What it adds |
|---|---|
| Easy repeat actions in Try today | Daily actions beside new features: send a message, finish a chat, read today's brief |
| Streak rule change | The streak goes up by one only on a day when every Try today item is done. A backend rule change to 3.3 |
| Hard badge tier | Chats opened 1000, streak 100 days, 50 running at once: a showier, animated badge. Motion respects the reduced-motion setting. Only this tier is showy |
| Weekly recap card | A card in Your progress, computed locally from the counts with no model turn. The full recap goes to its own chat, with one line in the notification bell and optional Slack |
| Morning-brief result card | Shows the latest result of the morning brief scheduled task |
| Running at once | The peak counter in 3.3 |
| Mate walkthrough and daily task | As in 4.8 |

## Appendix: what does not change

- `website/src/components/OnboardingFlow.tsx`: unchanged.
- `feature_videos.py` selection rules and `seen/dismissed` semantics: unchanged; it only gains probes, which the milestone backfill also uses.
- `session_pulse_counter.py`: unchanged; the milestone counter only shares its call site.
- Mate first welcome: only one line of copy appended after `welcome.offer_tools`; the flow and the one-time marker are unchanged.
- Crew Mode: its toggle, stamp and lock rules are unchanged; it is only read at turn start.

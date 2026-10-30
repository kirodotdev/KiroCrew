---
title: Achievement system for new-user feature discovery — one achievement table, four surfaces
status: draft
author: zejiangg
created: 2026-10-29
last-audited: 2026-10-30
audited-at: aaa4a4af3b
doc-pr: null
implementation-prs: []
tracking-issues: [26327]
supersedes: []
superseded-by: []
---

# RFC: Achievement System for New-User Feature Discovery

- **Status:** Draft. Nothing built; this document changes no code. Decisions D1–D3 were made on 2026-10-29 and D4–D8 on 2026-10-30 by the product owner (section 7.1). Acceptance is requested from a maintainer; the status flips to `accepted` when one records it here.
- **Verified at:** `aaa4a4af3b`. Every code reference below was read at that commit.
- **Tracks:** [#26327](https://github.com/kirodotdev/KiroCrew/issues/26327).
- **Hard constraint:** the sign-in / first-run flow does not change. `website/src/components/OnboardingFlow.tsx` stays as it is.

## One-page summary

**Problem.** After signing in, a new user lands on an empty chat page with one line, "What can I do for you?" (`website/src/pages/chat/page/ChatEmptyState.tsx`). Nothing tells them Crew can run scheduled tasks, run chats in parallel, connect to Slack, or build widgets. The discovery aids that exist each cover one corner: Feature Tips are model-generated and only appear while a turn runs; Feature Videos are off by default and carry a single "Meet your Mates" intro; the Mate's first welcome only asks for a name.

**Proposal.** A server-side **achievement table** with two kinds:

- **Feature achievements** (7, one-time): first widget, first parallel chats, first scheduled task, first PR watch, Slack connected, met the Mate, used Crew Mode. Each carries a one-click starter task that walks a new user through one try.
- **Milestone achievements** (2, tiered, for showing off): chats opened 10 / 50 / 100; chats running at once 5 / 20 / 50.

Four surfaces read this one table:

1. **Top-bar trophy.** A progress ring around the icon (feature achievements done / total). Clicking it opens the achievement panel: every achievement, locked and unlocked, unlock dates, tier progress (e.g. 37/50).
2. **New-chat empty state.** A short clip plus 3 cards = the next 3 unlockable **feature** achievements. Milestones never appear here, because no surface may nudge the user to open more chats and spend more. Once every feature achievement is handled, the empty state returns to today's page; the trophy and panel stay.
3. **Unlock toast.** Small, non-blocking, shown once per achievement (once per tier).
4. **Mate** (Ship 2). The Mate walks the user through a feature achievement; a daily task raises one locked feature achievement per day.

"Unlocked" is decided entirely by deterministic rules, with no model call: feature achievements reuse the Feature Videos `used_when` probes, milestones read local counters. Once an unlock happens it is written to a durable record and never rolls back.

**Decided.** D1: low-cost rows send directly, others prefill. D2: the daily task is a real Mate model turn. D3: the main switch defaults on (2026-10-29). D4: the achievement table replaces the feature table. D5: the four surfaces. D6: phased rollout. D7: defaults for the open questions. D8: the Crew Mode unlock signal (2026-10-30). See 7.1.

**Still open.** Only Q7 (relationship to Feature Tips), deferred to the "Later" batch. See 7.2.

## 1. Background and current state

| Existing mechanism | Location | Relation to this RFC |
|---|---|---|
| First-run flow | `website/src/components/OnboardingFlow.tsx` | Unchanged. This design only takes effect after it ends |
| Empty chat page | `website/src/pages/chat/page/ChatEmptyState.tsx` | Surface 2 changes this |
| Top bar | `website/src/shell/topbar/shellHeader.tsx` (the notification bell lives here) | Surface 1's trophy sits next to the bell |
| Feature Videos | `src/kiro_crew/feature_videos.py`, `GET /api/feature-videos/next` (`api_feature_videos_next` in `dashboard/routes/realtime.py`) | Reuse the clips, the `used_when` probes and the `seen/dismissed` state |
| Feature Tips | `src/kiro_crew/tips.py` | Model-generated; kept. The achievement table never goes through a model |
| Mate first welcome | `src/kiro_crew/dashboard/mate_welcome.py`, copy in `src/kiro_crew/crewmate_prompts.json` | Surface 4 reuses `dispatch_hidden_turn` and the copy table |
| Mate check-ins (daily/weekly) | `src/kiro_crew/dashboard/goal_reminder.py` | Surface 4 follows its hidden-turn pattern; does not reuse its hosted cron (see 4.8) |
| Putting text into the composer | `website/src/pages/chat/page/launchIntake.ts` (`setPendingInput`, `?autoSend=1`) | Starter tasks use this channel |
| Crew Mode | `src/kiro_crew/crew_mode.py` (the `crew_mode_agent` stamp on a session) | Signal source for the "used Crew Mode" achievement, see 3.1 |
| Session transcript files | `$KIROCREW_HOME/sessions/dashboard_chat-*.jsonl` | Data source for the "chats opened" milestone, see 3.2 |
| Turn start | `src/kiro_crew/dashboard/chat_turn/turn_dispatch_gates.py` (the `crew_log_emit.on_turn_started` call) | The "chats running at once" peak counter hooks in here, see 3.2 |
| Metrics | `src/kiro_crew/metrics/events.py` (`emit_counter`), `metrics/schema.py` | New `kirocrew.achievement.*` counters |

The design follows three product principles:

| Principle | How this design applies it |
|---|---|
| Deliver a result first, explain the feature second | A low-cost starter task sends on one click and produces a real result (a widget, results from two parallel chats) instead of opening a tutorial |
| Give a reason to come back daily | Surface 4: one locked feature achievement per day, raised once by the Mate |
| Surface sticky features early | The table includes Slack, the Mate, parallel chats and Crew Mode, and the ordering puts sticky features near the front |

## 2. Goals and non-goals

Goals:

- The first time a new user opens a new chat, one click produces a real result.
- Each achievement's "unlocked" is decided by a deterministic rule: the same local state always gives the same answer, with no model call.
- Every surface shares one table and one unlock state; there are never several progress counts that disagree.
- Once every feature achievement is handled, or the user turns the cards off, the empty state returns to today's page.
- Existing users can see the milestones they have already reached and have something to show.

Non-goals:

- No change to `OnboardingFlow.tsx`, and no new first-run step.
- No points, rewards or leaderboards. An achievement is just a dated record.
- No surface nudges the user to open more chats or run more turns. Milestones appear only in the trophy panel and in unlock toasts.
- No model generates the achievement table or any HTML that gets rendered.
- No remotely delivered starter tasks in v1 (reasons in 4.2).

## 3. The achievement table

### 3.1 Feature achievements (7)

Selection criteria: (a) the starter task produces a visible result within a minute; (b) there is a cheap, deterministic unlock signal; (c) people come back to it once they have used it (stickiness).

| Order | id | Achievement | Starter task (illustrative) | Send mode (4.5) | Unlock signal `unlock_when` |
|---|---|---|---|---|---|
| 1 | `widgets` | First widget | "Make a widget that shows my calendar for this week" | Send directly | `artifacts_nonempty` |
| 2 | `scheduled-task` | First scheduled task | "Every morning at 9, give me a summary of yesterday's work" | Prefill | `sel_event_seen:cron_add` |
| 3 | `mate` | Met the Mate | "Introduce me to the Mate" → opens the Mate chat | Open page | New probe `mate_thread_has_user_turn` |
| 4 | `parallel-chats` | First parallel chats | "Open two chats at once: one researches, one writes an outline" | Send directly | `sel_event_seen:session_create` |
| 5 | `slack` | Slack connected | "Connect Crew to my Slack" | Prefill | New probe `channel_connected:slack` |
| 6 | `pr-watch` | First PR watch | "Watch this PR and tell me when CI goes green" | Prefill | `sel_event_seen:monitor_start` or `sel_event_seen:monitor_watch` |
| 7 | `crew-mode` | Used Crew Mode | A new chat opens in Crew Mode, prefilled with "Split this across a few teammates working in parallel: …" | Prefill | New probe `crew_mode_used` (below) |

Known limitation of the `parallel-chats` signal: `session_create` records the agent creating a child chat, not two chats running at once. A user who opens two chats from the UI and runs them side by side never calls it, and a single agent-created child, even a serial one, unlocks it. It is accepted as the Ship 1 proxy because it is the cheap signal the starter task itself produces; once the Ship 2 peak counter exists (3.2), `peak_concurrent >= 2` can replace or join it.

`mate` uses only `mate_thread_has_user_turn`, not `crewmates_exist`: every install creates the Mate automatically, so "has a Mate" does not mean "used the Mate".

**Where Crew Mode lives, and which signal (D8).** Crew Mode is not a global setting. `agent.crew_mode_agent` (`config/sections_agent.py`) only picks which conductor template Crew Mode uses when turned on; it has a value by default, so it says nothing about use. The real "turned on" is a stamp on the session: when a session switches to the conductor template, `crew_mode.py` writes `crew_mode_agent` on it (`STAMP_FIELD` in `crew_mode.py`), persisted with the session metadata (`_read_crew_mode_agent` in `slot_persistence/metadata_codec.py`).

The cheapest deterministic signal: in the turn-start hook (the same place as the peak counter in 3.2, `turn_dispatch_gates.py`), if the turn is user-sent and the session's `crew_mode_agent` is non-empty, write the `crew-mode` unlock. It reads one in-memory field and no file. Reason: the stamp alone would also count "turned on and immediately off"; requiring a user turn is what makes it "used".

Known miss: an existing user who used Crew Mode before upgrading, but has not sent a message in Crew Mode since, is not credited retroactively. Crediting them would mean scanning all session metadata, which is not worth it; they unlock on their next use.

Starter task: the new chat opens through the same path as the existing Crew Mode toggle (so the session carries the stamp), then the task is prefilled. It does not send directly, because the conductor dispatches several workers and the spend has no upper bound (fails rule 5 in 4.5).

Not selected: knowledge base and voice (moved to the "Later" batch, see 6); skills, workflows and browser control (the starter task can hardly show a result within a minute, or needs something installed first).

Ordering rule: sort by the fixed `order`, filter out rows that are unlocked, skipped (see 4.4) or unavailable, and take the first 3. Not random, because the empty state is a recommendation: the user should see the same cards each time until they finish one.

### 3.2 Milestone achievements (2, tiered)

| id | Achievement | Tiers | Data source | Batch |
|---|---|---|---|---|
| `chats-opened` | Chats opened | 10 / 50 / 100 | High-water mark of the session transcript file count | Ship 1 |
| `chats-concurrent` | Chats running at once | 5 / 20 / 50 | A new durable peak counter | Ship 2 |

Milestones have no starter task, never appear in the empty state, and are never raised by the Mate.

**How "chats opened" is counted.** The cheapest deterministic source today is the session transcript files: every dashboard chat writes `$KIROCREW_HOME/sessions/dashboard_chat-<N>-<ts>.jsonl` (archived ones sit under `sessions/archive/`). The count lists the directory and counts names with the `dashboard_chat-` prefix, without opening any file. Mate chats are `dashboard_member-*`, subagents are `subagent_*`, scheduled tasks are `cron_*`; none of them count.

Two sources not chosen:

| Source | Why not |
|---|---|
| The slot counter `_slot_counter` (`slot_registry.py`) | In memory only; after a restart it is recomputed from the currently open sessions (`reseed_slot_counter`), so closed chats are lost |
| The crew log session tree (`crew_log/session_tree_projection.py`) | Can be turned off with the `KIROCREW_CREW_LOG` environment variable; folding the whole log costs more than listing a directory |

When a user deletes chat history, the transcript files are deleted (`dashboard/handlers/sessions_parts/delete_claim.py`) and the file count drops. So the stored value is the **high-water mark**: each computation takes `max(stored high-water mark, current file count)` and writes it back to the state file. An unlocked tier never rolls back.

Worker chats dispatched by Crew Mode also start with `chat-` and count too. That matches the literal meaning of "chats opened" and is accepted. Long-running installs can already hold thousands of these files, so existing users will land straight on the 100 tier after upgrading (see "backfill without toast" in 4.4).

**How "chats running at once" is counted.** Nothing records this number today, so a new durable peak counter is added:

- It hooks into turn start (`turn_dispatch_gates.py`, next to the `on_turn_started` call).
- Each time a turn starts, count the in-memory sessions whose `turn_running` is true (`_ChatSlot.turn_running` in `state_parts/chat_slot.py`), this one included.
- If the count exceeds `peak_concurrent` in the state file, write it back (`atomic_write`, in a thread so the event loop is not blocked). Otherwise write nothing.
- Restart-safe: the peak lives in the file and is read back after a restart. A restart itself stops every turn, so it cannot over-count.
- Cost: one pass over the in-memory session list per turn start, with no file I/O; a file write only on a new high.

It counts "turns running at the same time", not "tabs open at the same time". Hidden turns (Mate check-ins and the like) are turns and count.

## 4. Design

### 4.1 Data model

A new module `src/kiro_crew/achievements.py`, shaped after `VideoEntry` (a frozen dataclass, validated at startup; a bad row is logged and dropped so the API never 500s):

```python
@dataclass(frozen=True)
class Achievement:
    id: str                         # slug; also the i18n key and the state key
    kind: Literal["feature", "milestone"]
    order: int                      # order in the panel and in recommendations
    doc: str = ""                   # required for feature rows; must be in tips_allowlist.TIP_DOC_ALLOWLIST
    video_id: str = ""              # optional; points at an entry in the feature_videos catalog
    starter: Starter | None = None  # feature rows only; milestones must be None
    unlock_when: tuple[str, ...] = ()     # feature rows: unlocked when any fires; reuses probe_fires()
    available_when: tuple[str, ...] = ()  # "unavailable on this install" when any fires, e.g. disabled by policy
    counter: str = ""               # milestones only: "chats_opened" | "peak_concurrent"
    tiers: tuple[int, ...] = ()     # milestones only; strictly increasing, e.g. (10, 50, 100)
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

- `kind == "milestone"` must have `counter` and `tiers`, and `starter is None`. This rule guarantees a milestone can never become an empty-state card.
- `kind == "feature"` must have `doc`, `starter` and at least one `unlock_when`.
- A row with `send == "send"` must be on the low-cost list in 4.5. `send` defaults to `"prefill"`, so a row that forgets to set it is never sent directly.

The starter task's text is **not in the dataclass**; it lives in the frontend i18n catalog under `achievement.<id>.title` / `.blurb` / `.starter_prompt`, and a milestone's tier name is `achievement.<id>.tier` (with a `{n}` placeholder). Reason: this is exactly the text the user sees in the composer and sends, so it must be in the user's language; `website/src/i18n/locales/*.json` already has `catalogParity.test.ts` to keep every locale's keys complete.

### 4.2 Where the table lives: built into the server, not a manifest

| Option | Pros | Cons |
|---|---|---|
| A. Built-in server constant (recommended) | Ships with the release, testable; probes and counters run on the server | Adding a row needs a release |
| B. Signed manifest delivery (like Feature Videos) | Rows can be added without a release | A starter task is a prompt sent to a model; remotely editable = remotely injectable. A signature proves origin, not that the content is harmless |
| C. Frontend constant | Simplest | Probes read local files and the audit log, and counters look at server sessions, so this can only be computed on the server |

Option A. Clips still come from the Feature Videos manifest: the table stores only `video_id`, and whether a clip is downloaded and playable is answered by `feature_videos.offerable()` and the existing `_hosted_offers`. That way the table holds no remotely editable text that reaches a model. Since D1 lets some prompts send directly, option A is required: directly sent text may only be a reviewed constant that ships with the release.

### 4.3 API

One group of routes, next to `dashboard/routes/realtime.py`, with the same auth as `/api/feature-videos/*`:

```
GET  /api/achievements
→ {
    "enabled": true,
    "feature": { "unlocked": 3, "total": 7 },        // trophy progress ring
    "achievements": [
      { "id": "widgets", "kind": "feature",
        "state": "unlocked" | "locked" | "skipped" | "unavailable",
        "unlocked_at": 1793170000.0 | null,
        "video": <Offer.payload() | null>,       // reuses the feature_videos Offer shape
        "starter": { "kind": "prompt", "send": "send", "agent": "", "crew_mode": false } | null,
        "doc": "artifacts.md" },
      { "id": "chats-opened", "kind": "milestone",
        "value": 37, "tiers": [10, 50, 100],
        "tiers_unlocked_at": { "10": 1793170000.0 } }
    ],
    "recommended": ["widgets", "scheduled-task", "mate"],   // at most 3, feature rows only
    "toasts": ["widgets", "chats-opened:10"]                // due and not yet shown
  }

POST /api/achievements/event        { "id": "widgets", "action": "starter_clicked" | "skipped" | "video_seen" | "toast_shown" | "panel_opened" }
POST /api/achievements/skip-all     {}                    // empty state "Don't show again", see 4.5
POST /api/achievements/walkthrough  { "id": "slack" }     // Ship 2, see 4.8
POST /api/achievements/daily-nudge  {}                    // Ship 2, see 4.8
```

- `recommended` is filtered on the server and drawn only from `kind == "feature"`. The frontend has no way to put a milestone in the empty state.
- `unlock_when` and `counter` are not sent to the frontend, for the same reason `Offer.payload` omits `used_when`: they describe local state the frontend does not need.
- Temporary and incognito sessions get `{"enabled": false}`, the same rule as Feature Videos.
- Probes and the directory listing are blocking calls and run in `asyncio.to_thread`. A feature achievement already unlocked in the durable record skips its probes. Results are cached on the server for 60 seconds; the turn-start hook clears the cache when it writes a new unlock.
- When the frontend reads: on opening the empty state, on opening the trophy panel, and when a user-sent turn ends (reusing the existing turn-end event). If `toasts` is non-empty it shows them and then posts `toast_shown`.

### 4.4 How unlock state is computed

```
feature unlocked = in the durable record  OR  any unlock_when probe fires now
milestone tier unlocked = durable counter ≥ tier
```

Why a durable record: `_probe_sel_event_seen` only reads the last 500 lines of the audit log (`_SEL_PROBE_LIMIT`). If the user created a scheduled task last week and the log has rolled past it this week, the probe stops firing and progress drops from 4/7 back to 3/7. So the first time a probe fires, `{id: unlock time}` is written to `achievements_state.json` in `config_dir()` (mode 0600, `atomic_write`, beside `feature_videos_state.json`, see `_state_path` in `feature_videos.py`). After that only the record is read.

Because that record never rolls back, an SEL-based unlock must count only a row that records a successful invocation. `_probe_sel_event_seen` matches on `operation` alone, and the audit log also writes rows with the same `operation` for a denied call (the deny events in `security/deny_gate.py`). The achievement probes therefore add an outcome filter, as a new parameterized probe or an option on the existing one, so a denied or failed `cron_add` or `session_create` unlocks nothing. The Feature Videos probe keeps its current behavior, where a false positive only hides a clip.

File content:

```json
{ "unlocked": {"widgets": 1793170000.0, "chats-opened:10": 1793170000.0},
  "skipped": ["slack"],
  "toasted": ["widgets"],
  "backfilled": true,
  "counters": {"chats_opened_max": 37, "peak_concurrent": 4},
  "nudges": {"last_date": "2026-10-30", "last_id": "slack", "ignored_streak": 1} }
```

- `unlocked`: feature achievements are keyed by id; each milestone tier gets its own key, such as `chats-opened:10`, so each tier has its own unlock date and its own toast.
- `skipped`: feature achievements the user clicked "Skip" on in the empty state (D7 / former Q4). They count as "handled", so the empty state stops recommending them; they **do not count as unlocked**: the trophy ring and the panel still show them locked, and real use unlocks them later as usual.
- `toasted`: unlocks already toasted, so each is shown only once.
- `backfilled`: on the first read of the state (a new install, or the first read after an upgrade), every achievement already satisfied at that moment is recorded into `unlocked` and `toasted` in one go, without toasts. Otherwise an existing user would get a dozen toasts on upgrade day. Only later unlocks toast.
- `counters`: the two milestone counters, see 3.2. They only grow.

New probes (both go into `feature_videos._PROBES` / `_PARAM_PROBES`, shared by both features):

| Probe | Reads | Note |
|---|---|---|
| `channel_connected:<name>` | That channel's connection state (Slack reads the existing connection check, not the token contents) | Answers only "connected or not"; never touches credential values |
| `mate_thread_has_user_turn` | Whether the Mate's pinned chat has a user-sent message | Tells "has a Mate" apart from "used the Mate" |

`crew_mode_used` is not a polled probe; the turn-start hook writes it directly (3.1). v1 no longer needs `knowledge_nonempty` (the knowledge base moved to Later).

### 4.5 Surface 2: the new-chat empty state

`ChatEmptyState.tsx` switches to the achievement layout only when all of these hold, and otherwise stays as it is today:

- `GET /api/achievements` returns `enabled: true` with a non-empty `recommended`;
- the slot list has loaded (reusing the existing `selectSlotListPending` check, so a person with many chats is not mistaken for a newcomer).

An empty `recommended` means every feature achievement is unlocked, skipped or unavailable. The empty state then returns to today's page; the trophy and panel are unaffected.

Layout (top to bottom):

```
[ 10-second clip (when the first recommended achievement has one) ]
What you can have Crew do
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

- Cards hold feature achievements only. Milestones never appear here (D5): every card here is "go do one thing"; a milestone here would become "go open more chats" and push spend up.
- Progress is not shown here; it lives only on the top-bar trophy, so there are never two numbers.
- The card button's label follows the send mode, so the user knows what will happen before clicking: "Do it" for direct send, "Fill it in for me" for prefill, "Take a look" for open page.
- "Skip" adds the achievement to `skipped` and the next one moves up.
- "Don't show again" calls `POST /api/achievements/skip-all`, which marks every remaining feature achievement skipped. The empty state immediately returns to today's page; the trophy keeps showing true progress, and features used later still unlock. There is no separate switch.
- The "Start a new chat" button and the error message stay as they are.
- The clip reuses the `StartupVideoModal` player component; watching it records `seen` in the Feature Videos state under the same video id, so the startup modal does not play it again.
- Cards use a fixed layout filled only with text (i18n strings); no model output is rendered.

**How a starter task reaches the composer.** The existing channel: `createSlot(...)` creates a chat, then `dispatch(setPendingInput(text))`, and `usePendingInputIntake` (`launchIntake.ts`) puts the text into the new chat's composer and lights up `prefillHint`.

- `send: "prefill"`: stops there; the user presses send.
- `send: "send"`: also goes through `autoSendRef` and sends. `launchIntake.ts` states "send only on an explicit request"; the user clicking "Do it" on the card is that explicit request, and the prompt is a built-in constant, not outside input.
- `kind: "route"`: just `navigate(route)`, e.g. the Mate row opens `/members?member=mate` (matching the existing crewmates intro's `cta_route`).
- When `agent` is set, the new chat uses that member.
- When `crew_mode: true`, the new chat opens through the same path as the existing Crew Mode toggle, then prefills.

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

### 4.6 Surface 1: top-bar trophy and achievement panel

**Trophy.** Next to the notification bell in `shellHeader.tsx`, using lucide's `Trophy` icon (the top bar already uses lucide). The ring around the icon shows `feature.unlocked / feature.total`. `unavailable` feature achievements are left out of the denominator; `skipped` ones count in the denominator but not the numerator.

- Feature achievements only. Milestones have no ceiling, so putting them in the ring would mean it never fills.
- Once every feature achievement is unlocked, the ring is full and the trophy stays.
- The mobile top bar (`MobileTopBar.tsx`) gets one too, icon and ring only, no number.
- Hidden when the main switch is off.

**Achievement panel.** Opens from the trophy, shaped like the notification panel (`NotificationSheet`) and sliding in from the same side:

```
Achievements                        Features 3 / 7
── Features ──
✓ First widget              2026-10-28
✓ Met the Mate              2026-10-29
✓ First parallel chats      2026-10-30
○ First scheduled task      [Try it]
○ Slack connected           [Try it]
...
── Milestones ──
Chats opened        37 / 50     ✓10 (2026-10-21)  ○50  ○100
Running at once     4 / 5       ○5  ○20  ○50
```

- Unlocked rows show the unlock date (local time zone); locked feature rows have a "Try it" button that behaves exactly like the empty-state card (same starter task, same send mode). Skipped rows also have "Try it", so the user can always come back.
- Milestones show current value / next tier and each tier's unlock date. No "Try it" button and no nudging text.
- The panel renders only a fixed layout and i18n text.
- Opening the panel records one `panel_opened` metric.

### 4.7 Surface 3: unlock toast

- Uses the existing toast component, in a corner, dismissing itself after a few seconds; it blocks nothing and takes no focus.
- Fixed text: "Achievement unlocked: {title}"; for a milestone, "Achievement unlocked: Chats opened · 50". Clicking it opens the achievement panel at that row.
- Each feature achievement and each milestone tier toasts once (`toasted`). Backfilled unlocks at upgrade do not toast (`backfilled`).
- When one read finds several new unlocks, they merge into one toast, "3 achievements unlocked", which opens the panel.
- Milestone toasts carry no button and no text like "13 more chats to the next tier".

### 4.8 Surface 4: Mate walkthrough and daily task (Ship 2)

Both cover **feature achievements only** and never mention milestones.

**Mate walkthrough.** The whole table is not put into the Mate's system prompt (to avoid prompt bloat). Instead:

- Add a copy key `achievement.walkthrough` to `crewmate_prompts.json`, with placeholders `{feature_title}`, `{doc}`, `{starter_prompt}`. Content: say in two or three sentences what the feature does, then offer to run the starter task for the user, and wait for the user to agree.
- Trigger: a "Let the Mate show me" button on empty-state cards and in the panel → `POST /api/achievements/walkthrough {id}` → the server opens the Mate's pinned chat and runs a hidden turn with `mate_welcome.dispatch_hidden_turn(state, slot, prompt, human_seen=True)`. Language uses the existing `hidden_turn_language()` + `with_language()`.
- A hidden turn is not user-sent (`_turn_user_sent` is false), so most cards and `update_self` are refused. The walkthrough turn only needs to talk and make an offer, which is enough. Running the starter task happens after the user replies, in a user turn.
- The Mate's first welcome (`welcome.offer_tools`) gets one line at the end: "After asking the name, offer to do one small thing for the user", naming no specific feature. That line is the only change to the Mate's first welcome.
- At most one walkthrough per feature per day; it does not trigger while the Mate chat is running a turn (reusing the `slot.running` check).

**Daily task.** **Decided (D2, product owner, 2026-10-29):** the daily task is a real Mate model turn, at most once a day, triggered the first time the user opens the Mate chat that day. No fixed copy, no fixed cron time.

Each day pick one feature achievement that is neither unlocked nor skipped (the first by `order`, skipping the one raised yesterday) and the Mate raises it once.

Why trigger on "the first time the user opens the Mate chat that day":

- The user is looking at that moment, so the prompt does not land in a chat nobody reads (the `goal_reminder` check-in has the "does nothing if the chat is not open" problem);
- No hosted cron, so the Schedule page does not gain a task the user does not understand;
- Restart-safe: the condition comes from durable state (`nudges.last_date`), not process memory.

Implementation: when the client opens the Mate chat, at the point it greets for today (after `POST /api/members/{slug}/greet`), it calls `POST /api/achievements/daily-nudge`. The server:

1. Sends nothing when the main switch or `achievements_daily_nudge` is off, every feature achievement is handled, the first welcome is not finished, the chat is running, or the chat already has a user message today.
2. Creates an `achievement_nudge_<YYYY-MM-DD>.json` marker file with `O_EXCL` and sends only if it wins (same as `claim_greeting_marker`, guarding against multiple tabs and reloads). The date is in the user's time zone.
3. Runs a hidden turn with the `achievement.daily_nudge` copy (`dispatch_hidden_turn`, `human_seen=True`): one sentence on what to try today, plus a starter-task suggestion. This is a model turn, so it spends at most one turn per day.

**When it stops.** Every feature achievement is handled; or after 3 days in a row of nudges with no same-day reply from the user (`ignored_streak >= 3`), after which it drops to at most once a week; or the user turns it off.

On the first dashboard open of the day, a `passive` notification (list only, no sound) also points to the Mate chat, giving users who rarely open the Mate a way in. The notification itself starts no model turn; the turn still only happens when the user opens the Mate chat.

### 4.9 Switches and config

**Decided (D3, product owner, 2026-10-29):** the main switch defaults on. Since 2026-10-30 the keys follow the achievement naming.

| key | Default | Effect |
|---|---|---|
| `dashboard.achievements_enabled` | `true` | Main switch: the trophy, panel, empty-state cards, unlock toasts and Mate entry points all follow it. Off hides every surface; the state file is kept |
| `dashboard.achievements_daily_nudge` | `true` | Turns off only the daily task (Ship 2) |

Both go into `DashboardConfig` in `src/kiro_crew/config/sections_dashboard.py`, read the same way as `feature_videos_enabled` (`_safe_bool` in `loader_builders.py`). The Settings page gains two toggles in the Display group. The empty state's "Don't show again" is not a switch; it is "skip all" (4.5).

Defaulting on matters for existing users: a saved config without the key reads `true`, so an existing install also shows the trophy and the achievement empty state after upgrading. That is fine: features already used are judged unlocked by the probes (and backfilled without toasts), so someone who has used all 7 still sees today's empty state, and someone who has used some sees the rest recommended. Defaulting off would mean new users never see it, which defeats the design; nor does it depend on clips being available, unlike Feature Videos.

Managed fleets: a policy-managed install can turn the whole thing off through the existing capability policy (new scope `capabilities.achievements`); when off, `GET /api/achievements` returns `enabled: false`, and policy takes precedence over config.

### 4.10 i18n

- Frontend text: `achievement.<id>.title`, `.blurb`, `.starter_prompt`, `.tier`, plus the layout strings ("Achievements", "Features {n}/{total}", "Milestones", "Do it", "Fill it in for me", "Take a look", "Try it", "Skip", "Don't show again", "Let the Mate show me", "Achievement unlocked: {title}", "{n} achievements unlocked"), added to `website/src/i18n/locales/en.json`; other locales are filled in through the `TRANSLATION-PROMPT.md` process, with `catalogParity.test.ts` as the backstop.
- Mate copy: `crewmate_prompts.json` gains `achievement.walkthrough` and `achievement.daily_nudge`, with their purpose and placeholders written in the `_about` section. Language is handled by `with_language()`; the copy itself is written in English.
- Starter prompts are translated, because the user sees them in the composer; for the two direct-send rows, what gets sent is text in the user's language.

### 4.11 Metrics

Using `metrics.events.emit_counter`, with closed-enum attributes only (achievement ids come from a fixed table and are low-cardinality):

| Counter | Attributes | Question answered |
|---|---|---|
| `kirocrew.achievement.shown` | `surface` (empty_state / panel / mate / daily), `achievement` | Exposure |
| `kirocrew.achievement.starter_clicked` | `surface`, `achievement`, `send` (prefill / send / route) | Click rate; separately for direct send and prefill |
| `kirocrew.achievement.prefill_sent` | `achievement` | Share of prefills the user actually sent |
| `kirocrew.achievement.unlocked` | `achievement` (milestones carry the tier, e.g. `chats-opened:50`), `via` (starter / organic / backfill) | Whether starter tasks really lead to use |
| `kirocrew.achievement.skipped` | `surface`, `achievement` ("Don't show again" records `all`) | Which recommendations annoy people |
| `kirocrew.achievement.panel_opened` | — | Whether anyone clicks the trophy |
| `kirocrew.achievement.toast_shown` | `kind` (feature / milestone) | Toast volume, to keep it quiet |
| `kirocrew.achievement.nudge` | `outcome` (sent / skipped_busy / skipped_done / skipped_off / replied) | Whether anyone answers the daily task (Ship 2) |
| `kirocrew.achievement.opted_out` | `surface` | How many turn off the main switch, given the default is on |
| `kirocrew.achievement.features_completed` | — | Number of users with every feature achievement unlocked |

`via=starter` means the unlock happened within 24 hours after that achievement's `starter_clicked`. `via=backfill` is recorded at upgrade and does not count toward effect.

Success criteria (read 4 weeks after Ship 1):

- Share of new installs that unlock ≥3 feature achievements within 7 days, against a baseline for the same period before launch (D7 / former Q6: computed before Ship 1).
- Day-2 and day-7 return rate (the anonymous usage beacon `beacon.py` already records install activity per day and can be used directly).
- Opt-out rate (`opted_out`); because the default is on, this one is watched on its own.
- Guardrail: chats and turns per user per day should not rise abnormally because of milestones after launch. If they do, first check whether milestone toasts are pushing people.
- Daily task reply rate (Ship 2); below 10%, consider lowering the frequency or removing it.

## 5. Security and privacy

- Starter prompts are built-in constants plus i18n strings; they never come from a remote source, a model, or user data.
- The trophy, panel, cards and toasts render only fixed layouts and text, never model-produced HTML.
- `doc` must be in `TIP_DOC_ALLOWLIST`; `route` reuses `_CTA_ROUTE_RE`, so it can only navigate within the app.
- New probes answer only "yes or no"; they neither read nor return credentials or document contents. `channel_connected:slack` cannot read the token value.
- "Chats opened" only lists a directory and looks at file name prefixes; it never opens a transcript. The peak counter only counts in-memory flags and never reads chat content. The state file stores only achievement ids, timestamps and two integers, mode 0600.
- Hidden turns are not user turns, so sensitive tools and cards are refused as usual; the walkthrough only offers, and anything runs only after the user replies.
- Metric attributes carry no user content and no session id.
- Spend:
  - Direct send is limited to the `widgets` and `parallel-chats` rows (4.5), a few turns per click at most; `crew-mode` only prefills, because its spend has no upper bound.
  - Milestones never appear in the empty state, are never raised by the Mate, and their toasts do not nudge; catalog validation guarantees a milestone has no starter task (4.1). This keeps every surface from encouraging the user to open more chats.
  - The daily task spends at most one turn per day (4.8).
  - `send` defaults to `prefill`, and catalog validation rejects direct-send rows not on the list.
- The peak counter and the Crew Mode write in the turn-start hook are wrapped in try; an error is only logged and never blocks the turn.

## 6. Phased rollout

| Batch | Contents | Exit criteria |
|---|---|---|
| Prerequisite | Compute the baseline from existing data: share of new installs that used ≥3 features within 7 days (D7 / former Q6). First confirm the existing telemetry can tell new installs apart | Ship 1 is blocked until the baseline number is written into an appendix of this RFC |
| Ship 1 | `achievements.py` + state file + new probes (`channel_connected:slack`, `mate_thread_has_user_turn`) + Crew Mode turn hook + "chats opened" milestone + `GET /api/achievements` and `event` / `skip-all` + config keys (default `true`) + top-bar trophy + achievement panel + empty state + unlock toasts + i18n + metrics. On for everyone with the release (D3) | Backend: unlock / unavailable / persistence for every feature achievement; no rollback after the SEL rolls over; a denied or failed tool call unlocks nothing; "chats opened" does not roll back after chats are deleted; upgrade backfill shows no toast; a milestone cannot enter `recommended`; `send` defaults to `prefill` and an off-list `send` row is dropped. Frontend: nothing shows before load; direct send happens only for `widgets` and `parallel-chats`; skipped achievements stay locked in the panel; once everything is handled the empty state returns to today's page and the trophy stays; each toast shows once |
| Ship 2 | "Chats running at once" milestone (new peak counter) + Mate walkthrough + one line in the Mate's first welcome + daily task (D2) + passive notification | The peak only grows, survives restart, and writes the file only on a new high; a hook error never blocks a turn; the walkthrough hidden turn fires once and not while the Mate is busy; the daily task sends once per day across multiple tabs, reloads and restarts; nothing is sent before the first welcome finishes; frequency drops after 3 ignored days in a row; nothing is sent with `achievements_daily_nudge` off |
| Later | Knowledge base and voice feature achievements (voice is not in v1, D7 / former Q8); relationship to Feature Tips (Q7); after 4 weeks of data, review the rows, ordering, low-cost list and defaults | Against the success criteria in 4.11 |

## 7. Decisions and open questions

### 7.1 Decided

**2026-10-29, product owner:**

- **D1 (former Q1) Should a starter task send on click, or only prefill?** Low-cost rows send directly; every other row only prefills the composer, and the Mate row sends no prompt but opens the Mate chat. The low-cost rules and the per-row classification are in 4.5: `widgets` and `parallel-chats` send directly. Checked against the five rules, the scheduled-task row moved from direct send to prefill.
- **D2 (former Q2) Model turn or fixed copy for the daily prompt?** A real Mate model turn, at most once a day, triggered the first time the user opens the Mate chat that day. A model turn speaks the user's language and can use context; fixed copy would need a new "write an assistant message without running a model" path that does not exist today. An `O_EXCL` day marker keeps spend to at most one turn a day across tabs, reloads and restarts. See 4.8.
- **D3 (former Q3) Main switch on or off by default?** On. Off by default would mean new users never see it. Existing users see it after upgrading, but features they already used are judged unlocked. Managed fleets can turn it off with the `capabilities.achievements` policy. See 4.9 (the key was renamed to `dashboard.achievements_enabled` with the achievement naming).

**2026-10-30, product owner:**

- **D4 The achievement table replaces the feature table.** Two kinds: feature achievements (7, one-time, with starter tasks) and milestone achievements (chats opened 10/50/100, chats running at once 5/20/50). No points, leaderboards or rewards; the term is "achievement" throughout. See 3.
- **D5 Surfaces.** Top-bar trophy + achievement panel; the empty state carries only the next 3 feature achievements and milestones never appear there; each unlock toasts once; once every feature achievement is handled the empty state returns to today's page, and the trophy and panel stay. See 4.5–4.7.
- **D6 Phased rollout.** Ship 1: trophy, panel, empty state, feature achievements, "chats opened", metrics. Ship 2: "chats running at once", Mate walkthrough, daily task. Later: knowledge base and voice, the Feature Tips relationship, a review after 4 weeks. See 6.
- **D7 Defaults for the open questions.** Q4: a skipped card counts as handled for the empty state but not as unlocked. Q5: the rows are as in 3.1 and 3.2. Q6: compute the baseline before Ship 1. Q7: deferred. Q8: voice is not in v1.
- **D8 The Crew Mode signal.** Crew Mode has no global setting, only the `crew_mode_agent` stamp on a session. The unlock signal is "a user turn in a stamped session", written in the turn-start hook. See 3.1.

### 7.2 Open questions

- **Q7 Relationship to Feature Tips.** Deferred to the "Later" batch. Both promote features. Initial idea: while the empty state shows achievement cards, Tips pause in that chat until the first turn ends, so two surfaces never pitch at once.

Q4, Q5, Q6 and Q8 were closed by D7.

## Appendix: what does not change

- `website/src/components/OnboardingFlow.tsx`: unchanged.
- `feature_videos.py` selection rules and `seen/dismissed` semantics: unchanged; it only gains probes and is referenced.
- Mate first welcome: only one line of copy appended after `welcome.offer_tools`; the flow and the one-time marker are unchanged.
- Crew Mode: its toggle, stamp and lock rules are unchanged; it is only read at turn start.

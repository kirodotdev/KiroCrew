# Mates

A **mate** is a named assistant you keep. It binds a workspace folder, a
memory store, an agent template and a default model. Creating one gives it a
memory store of its own, which is what keeps one mate learning your incident
runbook out of another one's notes. The other three are named bindings it points
at, so two mates can share a workspace or a template — and an older mate
can share a store too, which the crew manager says on the field. The
**Mates** page is where you see all of them and talk to each one in a standing
thread.

Mates are the same objects the rest of Kiro Crew calls *crews* or *agents*:
one entry in the `agents` map of `~/.kiro/crew/config.json`. This page is the
user-facing view of them; [agents.md](agents.md) covers the agent JSON and
markdown specs a mate boots from.

## Turning the page on

Mates is a feature preview. Enable **Settings → Developer → Feature
Previews → Mates** and the **Mates** row appears in the left rail
at `/members`. The same card has a "See what it looks like" button that shows the
page before you switch it on.

## What a mate is made of

| Part | What it does | Empty means |
|---|---|---|
| Name | Its identity everywhere: the roster row, the thread, `crew=` on a delegation | required |
| Agent template (`kiro_agent`) | The spec it boots from — tools, MCP servers, instructions | required — the crew manager and the API both refuse a crew without one |
| Workspace | The folder it reads and writes | the folder your `default_workspace` names — as does a name no workspace matches |
| Memory store | The database its learned facts, rules and briefing live in | allocated for it at creation |
| Model | Default model for its sessions | the template's pinned model, then the global default |
| Reasoning effort | How long its sessions think before answering | the global default, or its role effort for a background worker crew; ignored by models that do not reason |
| Description | One line about what it is for | no description shown |
| Triggers (`triggers`) | Free text saying when work should go to it | never auto-selected; naming it still delegates to it |
| Avatar | A ghost face with chosen traits, or an uploaded picture | a face derived from its name |
| Session color | Tints the sessions it starts | no color of its own |

A per-session pick always wins over the mate's model and reasoning effort,
and the mate's values win over the global defaults.

## The roster

The member list on the left is ordered by most recent activity, or by name. It
lists the mates whose thread holds a message, the ones you starred, and Mate;
every other mate stays one search away, and the list ends by naming the ones
it hides (up to three, the ones you made first, then how many more), which the
count above leaves out. It carries:

- **Search** by name.
- **Starred** — your own favourites. Install a few capability packages and the
  agent sync writes a mate per capability, so the roster grows past what you
  drive by hand; the star filter is how you collapse it.
- **Origin** — *Mine* (you made it, in the crew manager or with `kirocrew agent
  create`), *Built-in* (ships with Kiro Crew), *From packages* (written by the
  agent sync).
- **Status** — *Working* (its thread is mid-turn), *Needs you* (parked on an
  approval or a question), *Unread*, *Scheduled check-ins* (running on a
  schedule). Picking two shows a mate in either state.

Each row shows the mate's face, the last message in its thread, and a
**Stopped** chip when you stopped that turn yourself. A mate whose own
thread has a live monitor or goal loop running shows an **on watch** mark — a
faint accent ring with one dot orbiting its avatar, and the word beside it —
until the loop stops.

## The standing thread

Opening a mate opens one durable, pinned thread with it — not a new chat.
The thread is derived from the mate's identity, so it is the same thread
every time: closing it, restarting the gateway, or coming back a week later
reopens the same conversation with its history. That is the point of a mate
over an ordinary session — the context you built up with it is where you left it.

Opening a thread that is new, or idle for six hours or more, with no goal in
flight shows a welcome card above the chat: goals the mate left open (in
this thread or a recent session of its own), then its recent sessions, and a
prompt to pick one up. It is read from the session ledgers and the session list,
with no model call and no chat turn, and shows once per idle stretch.

Member threads are deliberately kept out of the Sessions list; the Mates
page is their only home. The right-hand side panel is the chat page's panel with
two standing tabs: **Dashboard** (the page the mate publishes itself) and
**Files** (its workspace). Artifacts is not offered here; Terminal, Browser and
the other views open from the panel's **+** menu.

The identity pill in the thread header opens the mate's **Profile** card,
with four tabs: **Profile**, **Schedules**, **Sessions** (the ones it is driving)
and **Goals**. **Notes** — what it learned, its own standing notes — opens from
the Profile tab as a read-only page pushed over the card.

A few situations make Kiro Crew refuse to open a thread rather than guess:

| What you see | What happened |
|---|---|
| bound to a crew the registry no longer names | The mate was renamed or deleted while its thread survived. Restore the name, or delete the thread from Older Sessions in the Sessions list. |
| this thread's history exists but its binding is gone | The transcript is on disk with nothing claiming it. Handing it to whoever holds the name now would show one mate another's conversation. |
| shares its short name with … | Two mates fold to the same short name and therefore the same thread. Renaming changes only the name a mate shows, never its short name, so delete the mate that shows this notice, not the one it names, and create it again under a name that folds differently. Deleting the named one leaves its thread bound to a mate that is gone, and the other is refused instead. |

## Creating and editing

**New mate** — the hero button on an empty roster, or the **+** menu in the
roster header once one exists (its one row is **New mate**)
— opens an in-page form on the Mates page. It asks for a **Name** and
shows the mate's picture (press its pencil to pick an icon and a colour);
press
**Create** and the mate is made at once. Below the avatar, **Advanced
settings** unfolds the rest of the form in the same card: what the mate is
**Built from** (the starting setup it copies; `kirocrew` unless you pick
another), an optional line on **what it looks after**, and the workspace,
model, triggers and session color. Folding it again keeps whatever you set
there. When a mate suggests a new mate, its link opens this same form
with the suggested name and goal already filled in; nothing is created until
you press **Create**. Creating opens the new mate's chat, and the mate
starts it by introducing itself and asking what you would like it to do (or
confirming the job you wrote); that opening question comes from Kiro Crew, so no
message appears as if you had typed it. Once you tell it the goal (or confirm
the one you wrote), it puts the goal on a card for you to press, and after the
goal is saved it suggests its check-ins. The name is
free-form display text — spaces, punctuation, any script, emoji (`Dr. Eggbot 🥚`
is a name) — and the dialog only refuses a blank name or one already on the
roster before the request leaves the browser; the server owns every other rule
(a credential-shaped name, hidden characters, the length cap) and its answer is
shown in the dialog. The stable member id, path-safe slug and slot key are
derived by the server and stay bounded identifiers, as does the **Built from**
template id. A free-form name is kept as the mate's `display_name`, the label
the dashboard shows, on a config key the server derives from it; an id-shaped
name is used as the key itself. Creating is refused when that key is taken, or
when another mate already shows the same name. Two mates whose names
share a slug (`on_call` and `on-call`, say) are allowed: the server gives the
second its own identity, so each keeps its own chat and memory.

### Teams

Teams group the roster: a team's mates are listed under its header, and
opening the team shows the team view. Creating a team is not offered on the
page for now (the **+** menu has no **New team** row); an existing team is
renamed, re-membered or deleted from its view's **Edit team**. When a
mate there asks a question, an answer option drafts a reply in that
mate's chat; nothing is sent until you send it. Deleting a team keeps its
mates' chats.

### Editing

Beyond creating, the page edits mates in place. The Profile card's pencil,
**Permissions** and **Model** controls open the shared crew editor dialog on the
Mates page itself, and every configuration write goes through that dialog.
The crew manager — **Customize → Mates** (`/capabilities?tab=crews`) —
remains an alternative editor for an existing mate's name, template, model,
reasoning effort, workspace, triggers, avatar and session color.

From the CLI:

```bash
kirocrew agent list
kirocrew agent create --name oncall --kiro-agent kirocrew --workspace default
kirocrew agent create --name oncall-2 --display-name "On-call 2" --kiro-agent kirocrew
kirocrew agent update oncall --workspace incidents
kirocrew agent update oncall --display-name "On-call"
kirocrew agent update oncall --model <model-id>
kirocrew agent update oncall --model ""
kirocrew agent delete oncall
```

`--model` (on `create` and `update`) sets the mate's default model, and
`--model ""` or `--model auto` clears it so the mate inherits again;
`agent list` shows the pin, or `(inherit)`. The CLI refuses an id that is not
shaped like a model id, and a registry spelling kiro-cli does not serve (it
names the served one). It cannot check whether your account serves an id, since
that needs a running session; the crew editor in the dashboard does check.

Renaming changes only the name a mate shows: its agent name, short name,
chat, schedules and memory stay where they are, and `--display-name ""` goes
back to showing the agent name. A name another mate already shows is
refused, as is one with line breaks or hidden characters. A mate can also
take a new name itself: tell it in its own chat what to call it, and it renames
itself with the `update_self` tool, under the same rules. The same tool lets it
change its description and its ghost face when you ask it to ("wear a cap",
"describe yourself as the build watcher"): the change is applied at once and the
chat shows one line saying what changed, with an **Undo** that puts the old
value back. The undo is yours alone; the mate has no tool for it, and it
lasts as long as the gateway runs. A mate wearing an uploaded picture or an
appearance pack keeps that face; only the crew editor changes those. It cannot
change another mate, and nothing but a message you send in the dashboard
can make it change itself.

Everything else about a mate stays yours to grant. In its own chat a
mate can ask for a different model or reasoning effort (`set_model`), a
different approval mode for that chat (`set_approval_mode`), or a change to its
tools, MCP servers and skills (`request_capabilities`); each ask is a change
card with a button, and nothing changes until you press it. Its workspace,
memory store and template are not the mate's to ask for.

When its worker needs a tool or a skill it does not have, the crewmate looks up
what this gateway or the MCP registry offers and asks you for the addition on a
card. The card says where each server comes from and exactly which of its tools
would run without asking. Pressing it adds the server or skill to this
crewmate's own settings only, not to your MCP config. A worker that is already
running is reloaded with it, keeping its conversation, as soon as it is idle;
adding one yourself in the crewmate's advanced settings does the same.

Asking for tools this way is a preview, off by default for each crewmate. While
it is off, the crewmate asks you once, on a card marked **Preview**, whether it
may ask you for tools its worker needs. Turning it on only lets it ask: each tool
still needs your approval on its own card. If you decline, it does not ask again,
and you add what its worker needs in its advanced settings.

### Its worker

A crewmate hands longer work to a **worker**: a session it opens with
`session_create`, which runs on the crewmate's own memory and workspace with
the worker's built-in tools plus whatever that crewmate's settings add. To give
the worker a ready-made set of instructions, connections and skills, open the
crew editor, choose **Advanced**, pick an agent template under **Worker's extra
kit**, press **Copy into the worker**, review the list of changes and press
**Apply**. The kit is copied once; later edits to the template do not reach the
crewmate. **Re-copy from template** brings a newer version in the same way,
showing what would change first, and asks whether to keep anything added since
the last copy. Nothing the kit carries can lift the worker past your security
policy and profile. **Advanced** appears for a crewmate that owns its own
config.

### Goals

A mate's **Goals** tab (Profile card) holds what it works toward:

- **Goals.** As many as you like, each one paragraph, numbered and coloured on
  the tab. A new goal starts as a conversation: **Create a goal** (the last
  row of the list) explains in a few words how the mate uses a goal and offers
  one button, **Tell {name} what to aim for**, which sends an opening message
  to its chat; the mate asks what matters, writes the goal down and puts it on
  a card (`set_goal`) for you to confirm. Open an existing goal to edit or
  remove its text. The mate is
  encouraged to keep the list short and to suggest merging goals that overlap;
  you decide.
- **Milestones.** Key results that show how far it is from its goals, one
  shared set, each showing the numbers of the goals it serves. The mate
  works them out, links each to its goals, and proposes them on one card
  (`set_key_results`); after you confirm, it records readings itself
  (`key_result_progress`). A reading is a number or a short note of up to 120
  characters. A goal that cannot be measured has none. You can edit or remove
  any of them; a removal can be undone right after, readings included.
- **Check-ins.** Three switches, each off by default and only available once a
  goal is set, sharing one time of day: **Daily Goal Reminder** (the mate
  takes fresh readings, then carries on with its next task), **Weekly report** (on
  a weekday you pick) and **Monthly review** (on a day of the month you pick).
  These check-ins do not appear on the Schedule page or the mate's Schedules
  tab; the switches are their only control.
  The weekly report and the monthly review cover the readings and the tasks the
  mate finished in the period, and are saved as reports under
  `Crewmates/<name>/Reports` in Artifacts, with a notification linking each.
  A report reads like a printed document: A4 pages with a header, numbered
  sections and a footer, the conclusion first, and charts drawn from the
  readings. To keep a PDF, choose **Download** in the report's **More actions**
  menu, open the file in your browser and print it.
- **Heartbeat.** A fourth switch, off by default and only available once a goal
  is set. It lives on the mate's **Schedules** tab, the first row there, since
  it is what wakes the mate on its own rather than a goal check-in. While it is on,
  the mate wakes in its own chat every 30 minutes, hour (the default),
  2 hours or 4 hours, looks for what it can move forward toward its goals,
  and does it. It works quietly: what it did shows on its **Dashboard** tab and
  its task list, and it writes in the chat only when it needs your decision,
  when something you asked for is done, or when something needs your attention
  now. When it has nothing to move, it ends the wake without a word. Anything
  it does write during a wake shows in the chat. A wake is skipped while the chat is busy or a monitor loop is running in it. Each run uses credits. The
  mate can suggest turning it on with a card; only you can press it.
- **Reports.** The newest weekly reports and monthly reviews, each linking its
  artifact.
  When the monthly review finds the goal or its key results no longer fit the
  work, it puts a card in the chat proposing new ones; nothing changes until you
  press it.

The goal is stored in the mate's goal record,
`member-records/member-goals/<slug>.json` in Kiro Crew's data folder, apart
from its rules, with the same protection as its rules: nothing the mate runs
can read or change that folder, so it changes the goal or its key results only
through a card you press. A mate made from chat on a **Create mate** card starts with the
card's goal as its goal; its description stays empty until it or you write one.
What the mate is working on right now is its task list on the Crew board,
not part of the goal.

Creating a mate allocates its memory store for it. That store cannot later
be pointed somewhere else or shared with another mate — `kirocrew agent
update --memory-store` refuses it — because a mate's memory is its identity.
Deleting a mate unbinds it from new sessions and keeps its workspace, its
memory store and its past transcripts.

## Memory

Each mate reads and writes its own store, so what it learns from you stays
with it. Two mates pointed at one store can read each other's files and
memory, and the crew manager says so on the field when that is the case.
[memory-and-learning.md](memory-and-learning.md) covers what a member's database
holds, what is injected at the start of a conversation, and how backup and
restore treat it.

## Three things called "crew"

| | What it is |
|---|---|
| Mate | A named assistant of yours: template + workspace + memory + model. Picking one runs work as that mate, on its memory. |
| Agent template | A shared spec in `~/.kiro/agents/`. Picking a template runs it on your default mate's workspace and memory and creates nothing new. The chat agent picker lists templates separately, under **Custom agents**, for exactly this reason. |
| Remote crew | Another Kiro Crew gateway this dashboard can reach, under **Settings → Remote Crew**. A different machine, not a different assistant. |

## Where you can pick a mate

The same roster is offered wherever work starts: the agent picker in chat and on
the welcome screen, the schedule form, the Projects task runner, and an agent
channel. Kiro Crew remembers the mate a session committed to across a
restart, so a mate pick stays a mate pick and does not decay into a bare
template.

Slack is the exception worth knowing. Its per-thread `!ta <name>` (`!ta off`
clears it) resolves the name against the agent spec files in `~/.kiro/agents/`,
not against your roster, so it picks a **template**. A mate whose name has no
spec file of its own comes back as `Unknown agent`.

## Routing work to a mate

This section is for an agent deciding where a task belongs. Two tools read the
same roster and answer different questions.

`select_crew` with no argument returns the roster: every mate with non-empty
triggers except the configured `default_agent`, each with its triggers, plus that
`default_agent` name as a top-level field. A roster entry, a `route_crew` match
and an `unavailable` row also carry `display_name` when the mate's dashboard
label differs from its key. Pass the key, not the label, to `crew=`. The omission is of that one configured
name, not of whoever is calling — a mate with triggers can see itself in
the list, so check the name you picked against your own. You judge the fit.

`select_crew(crew="<name>")` binds one and returns its `bound` block —
`kiro_agent`, `workspace`, `memory_store`, `model` — and records the routing
decision in that mate's activity log. The first three are resolved values,
so they name the template, the folder and the store the run will really use.
`model` is the mate's configured pin verbatim, so an empty string there means
it pins nothing and the run inherits.

`route_crew(task="<the task>")` ranks the same triggers mechanically and returns
each match with a score, a description and a memory store, best first. Use it
when the same task should reach the same mate every time; use the roster when
the task is prose and you intend to judge.

Acting on either answer means `spawn_run(crew="<name>")`. `crew=` is the only
argument that moves the run onto that mate's memory store and template.
`agent=` names an installed agent template and nothing else: a name with no
template is refused outright, and a name that does resolve to one runs on the
memory the caller already has. So `agent="<a crewmate name>"` never *selects*
that mate's memory, it only inherits the caller's — from a default-crew
orchestrator that is the default store, which is how one mate's work ends up
somewhere it does not belong.

### How triggers are matched

Triggers are comma-separated phrases. A phrase scores as the fraction of its own
words present in the task text, and a mate scores as its best phrase, so a
specific multi-word phrase has to match more completely than a generic one-word
phrase to reach the same score. A phrase prefixed with `!` is a negative: if all
of its words appear, that mate is excluded whatever else it scored.
Negatives are applied after every positive, so the order you write them in
cannot change the outcome. Anything below a 0.7 overlap is not a match at all,
and ties keep the order the mates appear in your config. Case is ignored,
except in a phrase prefixed with `=`, which must appear exactly as written:
`=Excel` fires on "Excel" but not on "excel".

The same matcher picks [skills](skills.md), so "this phrasing matches" means one
thing across the product.

### When not to route

- **No match.** When `route_crew` returns nothing in either `matches` or
  `unavailable`, no mate claims the task: handle it on the default crew
  rather than picking the least-bad row.
- **A weak match.** The roster's own guidance is to select only on a clear,
  specific, high-confidence match. One shared word is not one.
- **No triggers.** A mate with empty triggers is never picked
  automatically: it is omitted from the roster and never ranked. It can still be
  delegated to by name with `spawn_run(crew=...)`. Triggers steer automatic
  routing only; there is no separate "accept delegated tasks" switch.
- **Memory unavailable.** A mate whose memory binding cannot be resolved
  comes back under `unavailable` with a reason. Report the refusal; running it on
  the default store instead is the one substitution never to make.

An unknown name returns an error listing the mates that do exist. Binding is
a decision, not an obligation — it records intent and changes nothing until you
spawn.

## Related

- [agents.md](agents.md) — agent templates, switching agents, mapping skills
- [agent-spec-fields.md](agent-spec-fields.md) — every field in an agent spec
- [memory-and-learning.md](memory-and-learning.md) — what a mate remembers
- [subagents.md](subagents.md) — `spawn_run` and parallel background work
- [cron-and-scheduling.md](cron-and-scheduling.md) — giving a mate a schedule

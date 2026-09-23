# Crew Members

A **crewmate** is a named assistant you keep. It binds a workspace folder, a
memory store, an agent template and a default model. Creating one gives it a
memory store of its own, which is what keeps one crewmate learning your incident
runbook out of another one's notes. The other three are named bindings it points
at, so two crewmates can share a workspace or a template — and an older crewmate
can share a store too, which the crew manager says on the field. The **Crew
Members** page is where you see all of them and talk to each one in a standing
thread.

Crewmates are the same objects the rest of Kiro Crew calls *crews* or *agents*:
one entry in the `agents` map of `~/.kiro/crew/config.json`. This page is the
user-facing view of them; [agents.md](agents.md) covers the agent JSON and
markdown specs a crewmate boots from.

## Turning the page on

Crew Members is a feature preview. Enable **Settings → Developer → Feature
Previews → Crew Members** and the **Crew Members** row appears in the left rail
at `/members`. The same card has a "See what it looks like" button that shows the
page before you switch it on.

## What a crewmate is made of

| Part | What it does | Empty means |
|---|---|---|
| Name | Its identity everywhere: the roster row, the thread, `crew=` on a delegation | required |
| Agent template (`kiro_agent`) | The spec it boots from — tools, MCP servers, instructions | required — the crew manager and the API both refuse a crew without one |
| Workspace | The folder it reads and writes | the folder your `default_workspace` names — as does a name no workspace matches |
| Memory store | The database its learned facts, rules and briefing live in | allocated for it at creation |
| Model | Default model for its sessions | the template's pinned model, then the global default |
| Reasoning effort | How long its sessions think before answering | the global default, or its role effort for a background worker crew; ignored by models that do not reason |
| Description | One line about what it is for | no description shown |
| Triggers (`triggers`) | Free text saying when work should go to it | never auto-selected, and delegation to it is refused |
| Avatar | A ghost face with chosen traits, or an uploaded picture | a face derived from its name |
| Session color | Tints the sessions it starts | no color of its own |

A per-session pick always wins over the crewmate's model and reasoning effort,
and the crewmate's values win over the global defaults.

## The roster

The member list on the left is ordered by most recent activity, or by name. It
carries:

- **Search** by name.
- **Starred** — your own favourites. Install a few capability packages and the
  agent sync writes a crewmate per capability, so the roster grows past what you
  drive by hand; the star filter is how you collapse it.
- **Origin** — *Mine* (you made it, in the crew manager or with `kirocrew agent
  create`), *Built-in* (ships with Kiro Crew), *From packages* (written by the
  agent sync).
- **Status** — *Working* (its thread is mid-turn), *Needs you* (parked on an
  approval or a question), *Unread*, *Scheduled check-ins* (running on a
  schedule). Picking two shows a crewmate in either state.

Each row shows the crewmate's face, the last message in its thread, and a
**Stopped** chip when you stopped that turn yourself.

## The standing thread

Opening a crewmate opens one durable, pinned thread with it — not a new chat.
The thread is derived from the crewmate's identity, so it is the same thread
every time: closing it, restarting the gateway, or coming back a week later
reopens the same conversation with its history. That is the point of a crewmate
over an ordinary session — the context you built up with it is where you left it.

Member threads are deliberately kept out of the Sessions list; the Crew Members
page is their only home. The right-hand panel is the same one the chat page
docks, so Files, Artifacts, Terminal and Browser all work against the thread,
and it opens on three crewmate tabs: **Notes** (what it learned — its own
standing notes, read-only here), **Work log** (what it did) and **Dashboard**
(the page it publishes itself). Setup — template, wake sources, memory, cloud —
lives on the crewmate's detail page, not in the panel.

A few situations make Kiro Crew refuse to open a thread rather than guess:

| What you see | What happened |
|---|---|
| bound to a crew the registry no longer names | The crewmate was renamed or deleted while its thread survived. Restore the name, or delete the thread from History. |
| this thread's history exists but its binding is gone | The transcript is on disk with nothing claiming it. Handing it to whoever holds the name now would show one crewmate another's conversation. |
| shares its short name with … | Two crewmates fold to the same short name and therefore the same thread. A crewmate cannot be renamed, so delete one and create it again under a name that folds differently. |

## Detail drawer

The drawer beside a crewmate shows only what was actually recorded:

- **Today** and **Past 7 days** counts, per day, split into *Chat* (you opened a
  session with it) and *auto-picked* (the orchestrator routed a task to it).
- The projects it worked in that day.
- **Wake sources** — the schedules, inbound webhooks and auto-patrol loops that
  can start a turn without you. "Nothing wakes this member automatically" is a
  real answer, not a loading state.
- **Sessions it's driving** right now.

Only the most recent events are loaded, so once the window is full the counts
render as floors (`12+ chats`) instead of asserting a total.

## Creating and editing

The only crewmate *configuration* the Crew Members page writes is the star on a
row, a roster preference stored on the crewmate. (Opening a member writes too,
but only its own thread binding.) Every configuration edit — **Add member** and
both Edit affordances — navigates to the crew manager — **Agent Capabilities → Crews**
(`/capabilities?tab=crews`) — which is the single editor for name, template,
model, reasoning effort, workspace, triggers, avatar and session color.

From the CLI:

```bash
kirocrew agent list
kirocrew agent create --name oncall --kiro-agent kirocrew --workspace default
kirocrew agent update oncall --workspace incidents
kirocrew agent delete oncall
```

Creating a crewmate allocates its memory store for it. That store cannot later
be pointed somewhere else or shared with another crewmate — `kirocrew agent
update --memory-store` refuses it — because a crewmate's memory is its identity.
Deleting a crewmate unbinds it from new sessions and keeps its workspace, its
memory store and its past transcripts.

## Memory

Each crewmate reads and writes its own store, so what it learns from you stays
with it. Two crewmates pointed at one store can read each other's files and
memory, and the crew manager says so on the field when that is the case.
[memory-and-learning.md](memory-and-learning.md) covers what a member's database
holds, what is injected at the start of a conversation, and how backup and
restore treat it.

## Three things called "crew"

| | What it is |
|---|---|
| Crewmate | A named assistant of yours: template + workspace + memory + model. Picking one runs work as that crewmate, on its memory. |
| Agent template | A shared spec in `~/.kiro/agents/`. Picking a template runs the shared template on the shared default memory and creates no crewmate. The chat agent picker groups the two separately for exactly this reason. |
| Remote crew | Another Kiro Crew gateway this dashboard can reach, under **Settings → Remote Crew**. A different machine, not a different assistant. |

## Where you can pick a crewmate

The same roster is offered wherever work starts: the agent picker in chat and on
the welcome screen, the schedule form, the Projects task runner, and an agent
channel. Kiro Crew remembers the crewmate a session committed to across a
restart, so a crewmate pick stays a crewmate pick and does not decay into a bare
template.

Slack is the exception worth knowing. Its per-thread `!ta <name>` (`!ta off`
clears it) resolves the name against the agent spec files in `~/.kiro/agents/`,
not against your roster, so it picks a **template**. A crewmate whose name has no
spec file of its own comes back as `Unknown agent`.

## Routing work to a crewmate

This section is for an agent deciding where a task belongs. Two tools read the
same roster and answer different questions.

`select_crew` with no argument returns the roster: every crewmate with non-empty
triggers except the configured `default_agent`, each with its triggers, plus that
`default_agent` name as a top-level field. The omission is of that one configured
name, not of whoever is calling — a crewmate with triggers can see itself in
the list, so check the name you picked against your own. You judge the fit.

`select_crew(crew="<name>")` binds one and returns its `bound` block —
`kiro_agent`, `workspace`, `memory_store`, `model` — and records the routing
decision in that crewmate's activity log. The first three are resolved values,
so they name the template, the folder and the store the run will really use.
`model` is the crewmate's configured pin verbatim, so an empty string there means
it pins nothing and the run inherits.

`route_crew(task="<the task>")` ranks the same triggers mechanically and returns
each match with a score, a description and a memory store, best first. Use it
when the same task should reach the same crewmate every time; use the roster when
the task is prose and you intend to judge.

Acting on either answer means `spawn_run(crew="<name>")`. `crew=` is the only
argument that moves the run onto that crewmate's memory store and template.
`agent=` names an installed agent template and nothing else: a name with no
template is refused outright, and a name that does resolve to one runs on the
memory the caller already has. So `agent="<a crewmate name>"` never *selects*
that crewmate's memory, it only inherits the caller's — from a default-crew
orchestrator that is the default store, which is how one crewmate's work ends up
somewhere it does not belong.

### How triggers are matched

Triggers are comma-separated phrases. A phrase scores as the fraction of its own
words present in the task text, and a crewmate scores as its best phrase, so a
specific multi-word phrase has to match more completely than a generic one-word
phrase to reach the same score. A phrase prefixed with `!` is a negative: if all
of its words appear, that crewmate is excluded whatever else it scored.
Negatives are applied after every positive, so the order you write them in
cannot change the outcome. Anything below a 0.7 overlap is not a match at all,
and ties keep the order the crewmates appear in your config.

The same matcher picks [skills](skills.md), so "this phrasing matches" means one
thing across the product.

### When not to route

- **No match.** When `route_crew` returns nothing in either `matches` or
  `unavailable`, no crewmate claims the task: handle it on the default crew
  rather than picking the least-bad row.
- **A weak match.** The roster's own guidance is to select only on a clear,
  specific, high-confidence match. One shared word is not one.
- **No triggers.** A crewmate with empty triggers is opted out by its owner. It
  is omitted from the roster, never ranked, and a delegation naming it is
  refused outright with `crew_delegation_disabled`.
- **Memory unavailable.** A crewmate whose memory binding cannot be resolved
  comes back under `unavailable` with a reason. Report the refusal; running it on
  the default store instead is the one substitution never to make.

An unknown name returns an error listing the crewmates that do exist. Binding is
a decision, not an obligation — it records intent and changes nothing until you
spawn.

## Related

- [agents.md](agents.md) — agent templates, switching agents, mapping skills
- [agent-spec-fields.md](agent-spec-fields.md) — every field in an agent spec
- [memory-and-learning.md](memory-and-learning.md) — what a crewmate remembers
- [subagents.md](subagents.md) — `spawn_run` and parallel background work
- [cron-and-scheduling.md](cron-and-scheduling.md) — giving a crewmate a schedule

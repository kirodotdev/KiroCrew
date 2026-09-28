# Steering files, prompts and hooks

Three ways to change what an agent does without editing its agent spec:

- **Steering files** are markdown conventions the harness loads into a session
  before you type anything. Standing rules.
- **Chat lifecycle hooks** are shell commands that fire on an event — a session
  starting, a prompt arriving, a tool about to run. Automation.
- **Saved prompts** are reusable message bodies you call up by name. Shortcuts.

All three live under Agent Capabilities in the dashboard, on the **Steering
files**, **Hooks** and **Prompts** tabs. Hooks also have their own page at
`/hooks`.

## Steering files

A steering file is a plain markdown document. The harness reads it at session
start and the agent treats its contents as instructions, so it is where project
conventions and personal working rules belong — the things you would otherwise
retype every conversation.

### Where they live

| Scope | Location | Loaded for |
|---|---|---|
| Global (`user`) | `~/.kiro/steering/**/*.md` | every session |
| Workspace (`workspace`) | `<project>/.kiro/steering/**/*.md` | sessions whose project directory is that project |

Two different mechanisms put them there. kiro-cli loads both roots itself: the
global one for every session it starts, and the workspace one because the
session subprocess runs with the chat slot's project directory as its working
directory. On the Claude Code backend, which does not read an agent spec's
`resources` array, the gateway loads the global root explicitly —
`steering_target_admissible` and `_load_steering_resources` in
`src/kiro_crew/context.py` glob the agent config's
`file://.kiro/steering/**/*.md` resource against `$HOME`.

A workspace file therefore needs a project bound to the chat slot. With no
resolvable project the Steering files tab offers only the global root, and
creating a workspace document is refused with `steering_workspace_unavailable`.

The listing is bounded: at most 500 files, 256 KiB per document. Within the
context Kiro Crew assembles, `_STEERING_CAP` gives steering resources 10% of
the budget base.

### What a document declares

Steering front matter is Kiro's format, not Kiro Crew's. Four fields matter:

- `inclusion` — one of `always`, `fileMatch`, `manual`, `auto`. Absent or
  misspelled reads as `always`.
- `fileMatchPattern` — a glob, meaningful only beside `inclusion: fileMatch`. A
  `fileMatch` document with no pattern is refused on save, because it could
  never match.
- `name` and `description` — what an on-demand index shows the model. Set them
  by editing the document; the tab does not write them.

```markdown
---
inclusion: always
name: api-conventions
description: How this service names endpoints and shapes errors
---

# API conventions

Endpoints are plural nouns. Errors carry a stable `code` field.
```

**Kiro Crew reports and edits the declaration; it does not decide delivery from
it.** What a mode causes is kiro-cli's behaviour, it differs from the Kiro IDE,
and it has changed between releases — so this page does not restate a
per-version measurement. The one row every measurement agrees on is `manual`:
there is no way to bring such a document into a turn over ACP. For the rest,
read the harness's own steering documentation rather than trusting a mode name to
mean what it sounds like.

So "always on" is the default, not a guarantee: a mode can withhold a document,
and the gateway's own CC-backend load reaches the global root only. Treat a
steering file as reliably reaching a session when it declares `always` (or
declares nothing) and lives in a root that session loads.

The Steering files tab reads, creates, edits and deletes these documents. The
inclusion mode is a segmented control in the editor, and the rewrite happens
server-side so your body text is preserved byte for byte. An entry that is a
symlink is listed and readable but never editable, and a workspace write must
echo the project it was listed under — a chat slot's project can move, and a
delete issued from a stale listing would otherwise unlink the same-named file in
a different project.

### Writing one an agent actually follows

Steering that gets followed reads like a rule, not like prose:

- **State the rule, then the reason.** "Use `pytest.raises`, not
  `try/except` — the assertion message names the expected type" beats a
  paragraph about testing philosophy.
- **Be decidable.** An agent can check "every new endpoint gets a route test".
  It cannot check "write clean code".
- **Name the file or the symbol.** A rule anchored to a path is one the agent can
  verify it obeyed.
- **Keep it short.** Steering is injected into every single session and competes
  for the same budget as your conversation. A 40-line document that is always
  true is worth more than a 400-line one that is mostly background.
- **Put project rules in the workspace root, habits in the global one.** A global
  document travels to every project you open, including the ones where it is
  wrong.

### Where steering sits against memory

Kiro Crew builds a session's context in a fixed order, and its stated
convention is that an earlier layer outranks a later one. Steering resources are
injected **before** the thread history, before memory, and before lessons. So
where a steering file and a stored memory disagree, the steering file is the one
the agent is meant to follow — and `learn_add`'s own guidance is not to save a
lesson that only duplicates steering.

Kiro Crew does **not** rank the global root against the workspace root. Both are
loaded; the order they arrive in, and whether a declared mode withholds one, is
the harness's decision, not something this product reimplements.

## Chat lifecycle hooks

A hook is a shell command bound to an event. It can inject context, inspect a
tool call before it runs, or block it outright.

### The five events

| Event | Fires | Exit 0 does |
|---|---|---|
| `AgentSpawn` | a session starts | stdout is added to the session's context |
| `UserPromptSubmit` | you send a message | stdout is added to the turn's context |
| `PreToolUse` | before a tool runs | allows the tool |
| `PostToolUse` | after a tool ran | nothing |
| `Stop` | the turn ends | usually nothing — but see the continuation contract below |

`PreToolUse` is the only event whose exit code can decide anything — and only
on the path where the gateway is the one approving the call:

- **0** — a delivered allow.
- **2** — a delivered deny. stderr goes to the model, so write a reason the agent
  can act on.
- **anything else**, including a timeout, a crash, a command that will not
  execute, and a plain exit 1 — the gate did not decide, so it resolves to
  **deny** and the tool is blocked. This is deliberate: breaking or deleting a
  deny hook must not silently disable the policy it enforces, and there is no
  per-hook opt-out.

**A `PreToolUse` hook does not always get a vote.** Where kiro-cli has already
approved the call and the gateway is only being notified that it is starting, the
tool is running by the time hooks fire, so the same hook is informational there
— it can log, audit, or trigger a side effect, and no exit code stops the call.
Write a `PreToolUse` hook as a policy that holds where it is consulted, not as a
guarantee the tool can never run.

On every other event a non-zero exit is warn-only: the run is recorded against
the hook with its error, and the turn continues.

**Exit 0 discards both streams except where the table says otherwise.** On
`PostToolUse` and `Stop` a successful run surfaces nothing — stderr included — and
only a non-zero exit records an error you can read on the hook's row. The one
place you always see both streams is the Test result panel, which is the reason
to test a hook rather than watch a session and hope.

**A `Stop` hook can keep the session going.** Exit 0 is not necessarily the end
of it: if the hook's stdout is a JSON object declaring
`{"decision": "block", "reason": "<text>"}`, the reason is injected and the agent
takes another turn. A blank or missing reason contributes nothing, and any other
stdout — including ordinary log output — is ignored, which is what keeps a `Stop`
hook that merely records something from looping the session. Write that JSON only
when you mean "not done yet", and make the reason the instruction you want the
agent to act on.

### The six Kiro Agent triggers

A Kiro Agent session has eleven triggers where the gateway has five. The other
six can be authored and saved here, and a saved one reads back unchanged.

**No event fires any of them.** The gateway has no lifecycle moment for the six,
so nothing fires one on its own, and no other reader exists. Authoring one records
the intent; it does not schedule a command.

One thing does still run the command: the **Test** button on the hook's row, the
same as for any other hook. Test is how you check a command works, and it is the
only way one of these six runs at all today.

The six do not all sit the same distance from running, and the table says which
is which. A Kiro Agent asks its client for hooks by trigger name, and the names
it can ask for are a fixed list — `preTaskExecution` and `postTaskExecution` are
on it, and the file and manual triggers are not. So two of the six are waiting
on Kiro Crew answering that request, and the other four are waiting on a Kiro
Agent learning to ask at all. Save one of those four for the record, not for an
imminent run.

| Trigger | The agent's moment | Asked for | The row says |
|---|---|---|---|
| `PreTaskExecution` | before it starts a task | yes | `not fired yet` |
| `PostTaskExecution` | after it finishes one | yes | `not fired yet` |
| `FileCreated` | it created a file | no | `never fires` |
| `FileEdited` | it saved an edit | no | `never fires` |
| `FileDeleted` | it deleted a file | no | `never fires` |
| `UserTriggered` | you ran the hook by hand | no | `never fires` |

Each of the six is the PascalCase form of a trigger name Kiro's own hook schema
carries, which is why all six are worth storing: the hook you write today is
written against a documented name rather than a guess at one. The manual trigger
is the furthest of the six from running — Kiro's current hook-file schema has no
equivalent for it at all, so `Test` is its whole run path here.

**A hook on one of the six is saved switched off.** Nothing runs it either way
today, so that costs you nothing now, and it is what keeps a later release honest:
the change that starts firing these events finds your hook already off, so it
cannot run a command you wrote months ago and never looked at again. Switch it on
when you want it live. **Test runs it whether it is on or off**, so you can check
the command works today — for a trigger nothing fires, Test is not a rehearsal, it is
the only way the hook runs at all. The form says so before you save, so a row that
comes back with its switch off does not read as a save that failed.

If you switch one on anyway, the switch itself says what that means — for the two the
agent asks for, nothing fires them yet; for the other four, nothing ever fires them on
their own. Both are true at once: the switch is your intent, the mark is what the
gateway does.

**Editing a working hook onto one of the six switches it off too.** That is the same
rule, on the other write path: a hook that already runs on `PreToolUse` and is pointed
at `FileEdited` is stored off, because nothing has reconfirmed that command for the new
trigger. Switching it on in the same edit is honoured. A hook already on one of the six
keeps whatever state you gave it, so editing its command does not switch it off again.

**A hook on one of the six takes no matcher.** Saving one is refused. A matcher
filters something in the event's payload — a tool name on the tool events, your
message on the message ones — and these events have no payload, because nothing
fires them. A filter stored now would be written against whatever you had in mind
(`*.py` meaning a path, say) while the change that defines the payload is free to
pick a different subject, and your hook would then fire on the wrong things or on
nothing at all. So the field is refused until there is a payload to point it at,
and the form says that where the field would have been rather than leaving you to
guess why two fields disappeared. A matcher you already typed is not destroyed by
the detour: it is dropped when the hook is SAVED against one of the six, so picking
one of them and then picking a tool event again gives your filter back.

**You can see this on the row without reading this page.** The Hooks tab marks a
hook against one of the six in its Status column, and marks the trigger in the
picker as you choose it — `not fired yet` for the two the agent supports, `never fires`
for the four it does not. The two marks also LOOK different, because at pill size the
wording alone was still being mixed up: a trigger the agent asks for gets a solid pill,
one it does not gets a hollow dashed one. Both stay muted — neither is a fault. Choosing
one spells the mark out in the form as ordinary text, and either mark also carries the
same line as a tooltip on the row. The mark is read
from the same two sets the table above states, so a trigger that gains delivery
stops being marked without anyone editing the words. Once you Test such a hook the
run's result appears BESIDE the mark rather than replacing it: whether anything
fires the trigger and how the last run went are different facts, and a single Test
should not delete the first one from the table for good.

Two consequences worth knowing before you write one. There is no exit-code vote
here — the deny contract belongs to `PreToolUse` and to nothing else — and a
skills-only hook still pairs with `UserPromptSubmit` or `AgentSpawn` alone,
because the "Load skills:" directive has no reader on any of these.

These six also never travel in a generated kiro-cli agent spec. kiro-cli's hook
map is a closed set of its own five names, and a spec carrying a sixth key does
not load at all, so a hook saved against one of these triggers is kept here and
left out of that file.

### What a hook runs

A hook's `command` is one shell command line, stored in `~/.kiro/crew/hooks.json`.
It runs in the platform's native shell, so **a hook is not portable across
platforms**: `/bin/sh -c <command>` on POSIX, `%ComSpec% /c "<command>"` on
Windows. Write `$KIROCREW_HOOK_EVENT` on one and `%KIROCREW_HOOK_EVENT%` on the
other; single quotes group nothing under `cmd.exe`.

Both platforms get `KIROCREW_HOOK_EVENT` and `KIROCREW_HOOK_CONTEXT` in the
environment, and the hook-event JSON on stdin.

**A hook does not inherit the gateway's environment.** The gateway process holds
provider keys and tokens, so a hook subprocess gets an allowlisted slice instead
— `PATH`, the home and temp directories, locale, TLS trust, and `KIROCREW_HOME`.
The practical consequence: a command that works in your terminal can fail as a
hook because something like `VIRTUAL_ENV`, `PYTHONPATH`, `JAVA_HOME` or
`AWS_PROFILE` is not forwarded. Set what you need inside the command, or use
absolute paths.

A hook's fields:

| Field | Means |
|---|---|
| `event` | one of the eleven above |
| `matcher` | what the hook filters on; empty means every call, or every message |
| `matcher_mode` | `glob` (default), `regex`, or `contains` — read only for the message events, never for a tool matcher |
| `command` | the shell line |
| `skills` | skill keys to inject instead of running a command |
| `timeout` | seconds, 1 to 300, default 30 |
| `enabled` | off hooks stay on disk and never fire |

**A tool matcher and a message matcher are matched differently.** On
`PreToolUse` and `PostToolUse` the matcher is compared against the tool name with
a fixed glob-style vocabulary — an exact name, `prefix*`, `*suffix`,
`*contains*`, or `*` for everything — and `matcher_mode` is not consulted at all.
The form knows this and hides the mode control for those two events. On
`UserPromptSubmit`, `AgentSpawn` and `Stop` the matcher runs against the message
or the final assistant text, and there `matcher_mode` decides: `glob`, a
case-insensitive `regex`, or `contains` as pipe-delimited substrings.

### Creating, testing, enabling and deleting one

On the Hooks tab:

1. **New hook**, then name it, pick the event, and type the command. For a tool
   event add a matcher; for a message event pick its mode too.
2. **Save it.** Test runs against a stored hook, so there is nothing to test
   until this step. Note that a new hook is **enabled as soon as it saves** — it
   can fire before you have tried it.
3. **Turn it off first if that matters.** For a `PreToolUse` hook it usually
   does: a command that exits non-zero denies every call its matcher reaches. Use
   the row toggle to disable it, then test, then enable it again.
4. **Test** it. The run is real — the command executes — and the result panel
   shows the exit code, the duration, stdout and stderr.
5. **Delete** is armed: the first click arms it, the second confirms.

Each row also carries its own history — last run, last status, run count, and
the last error, expandable inline when something failed.

### A hook that loads skills instead of running a command

Leave `command` empty and pick skills instead, and the hook injects a directive
telling the agent to load them. No subprocess runs. This works only on
`UserPromptSubmit` and `AgentSpawn`, and only with no command — anywhere else
the directive has no consumer, and the save is refused with a message saying so.
A hook that already carries unusable skills shows them read-only with a warning
rather than silently dropping them.

### Provider hooks, shown read-only

When the active provider has hooks of its own, they appear in a second table
below your own: event, source, matcher, command. It is a mirror, not an editor —
change them in the provider's config file. A `bundled` row carries a lock and
ships with the provider; a `user` row is one you added there.

### kiro-cli hooks that survive an update

Separately from the Hooks tab, `agent.kiro_hooks` in `~/.kiro/crew/config.json`
holds kiro-cli hooks that persist across `kirocrew update`:

```json
{"agent": {"kiro_hooks": {"preToolUse": [{"matcher": "*", "command": "/path/to/hook.sh"}]}}}
```

Bundled hooks always come first and your entries are appended per event, deduped
by command and matcher. A command must be an absolute path to an existing file
outside sensitive locations, and only `command` and `matcher` are kept — extra
keys are stripped.

`kiro_hooks` also accepts the array of hook documents a kiro-agent profile uses,
so a spec written for either tool loads here:

```json
{"agent": {"kiro_hooks": [
  {"name": "guard", "trigger": "PreToolUse", "matcher": "*",
   "action": {"type": "command", "command": "/path/to/hook.sh"}}
]}}
```

Both shapes are held to the same bar: same command and matcher rules, same
dedup, same per-event and total caps. Twelve triggers are accepted, each in
kiro-agent's own PascalCase, its IDE camelCase spelling, or its CLI alias —
the whole table from kiro-agent's `packages/kiro-agent/src/hooks/trigger-names.ts` at blob `2d4a3127e32e5e81e68d5c2ea406a6a5728f6d78`, matched without regard to case. Five map onto the five kiro-cli event
names: `PreToolUse` to `preToolUse`, `PostToolUse` to `postToolUse`,
`UserPromptSubmit` to `userPromptSubmit`, `SessionStart` to `agentSpawn`, `Stop`
to `stop`. Only those five reach kiro-cli; a hook on `SessionEnd`, `PreTaskExec`,
`PostTaskExec`, `PostFileCreate`, `PostFileSave`, `PostFileDelete` or `Manual`
stays in your config and is not installed for that trigger. Autoimport is a
separate path: a script sitting in your hooks directory is still discovered there
on its own, on the event that scan infers, unless a hook you switched off names
it. An `action` of type `agent` has no
kiro-cli slot either, so it does not run there. Same for the per-hook `name`,
`description` and `timeout`: kiro-cli is handed the command and the matcher, and
the rest stays in your config. A `timeout` is the one of those three that asked
for less, so it says so: the command still runs, under kiro-cli's own bound
rather than yours, and a line names the hook when that happens.

`enabled` and `confirm` are the two that change what runs, so they are not
dropped. `enabled: false` means the hook is off, and `confirm: true` asks you
first — a kiro-cli hook cannot ask. Either one keeps the hook out of the kiro-cli
spec entirely, rather than handing over a command that runs unconditionally, and
it says which in the log and in the security event log.

Off stays off through autoimport too. A script under `~/.kiro/hooks` is normally
picked up on its own, so a hook you switched off by naming that script would come
back as a fresh discovery on autoimport's default event. The script named by a
hook you switched off is left out of that scan.

The standalone hook-file wrapper `{"version": "v1", "hooks": [...]}` is a file
format, not a spec value, and is rejected.

## `register_hook` is a different thing

The `register_hook` MCP tool is **not** a lifecycle hook, despite the name. It
registers a *webhook listener* so an external system can push a message into a
dedicated session later: submit a code review, then let the review bot call back
with the results.

| | Chat lifecycle hook | `register_hook` |
|---|---|---|
| Created by | you, on the Hooks tab | an agent, mid-turn |
| Fires on | one of five session events | an inbound HTTP POST |
| Runs | a shell command you wrote | a fresh agent turn |
| Lives in | the `hooks` list in `hooks.json` | a top-level entry in the same file |

It takes `hook_id` and `context_summary`, both required, and returns the webhook
URL (`/api/hooks/agent`), the session key (`hook:<hook_id>`), and the JSON body
the caller should POST. Calls need a webhook token — created under Settings →
Webhooks, shown once, then stored hashed; with none configured every call is
refused with 401. Registration is refused outright in Incognito and Temporary
sessions, because the resume context it saves is persistent by definition.

See [inbound webhooks](inbound-webhooks.md) for tokens, signatures and run
history.

## The prompt library

A saved prompt is a reusable message body with a name. The Prompts tab lists
them, and there are two scopes:

| Scope | Location |
|---|---|
| Global | `~/.kiro/prompts` |
| Local | `<project>/.kiro/prompts` |

To use one, type `@` in the composer and pick it by name — the same mention
syntax skills use. From the Prompts tab, **Send to** picks the destination
instead: a new chat, or one of your open sessions. Either way the prompt's name
lands in the composer as `@<name>` and sends.

The difference from a steering file is when it applies. A steering document is
loaded without you asking for it, as far as its mode and backend allow; a
prompt is something you reach for. A long request you make weekly is a prompt. A rule that
should hold always is steering.

## Related docs

- [Agents](agents.md): agent specs, and the resources a spec loads
- [Skills](skills.md): markdown knowledge packs, and the `@` mention that loads one
- [Inbound webhooks](inbound-webhooks.md): the endpoint `register_hook` points at
- [Memory and learning](memory-and-learning.md): the layer steering outranks
- [Dashboard](dashboard.md): where the Steering files, Hooks and Prompts tabs sit

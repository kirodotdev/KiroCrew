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
| `event` | one of the five above |
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

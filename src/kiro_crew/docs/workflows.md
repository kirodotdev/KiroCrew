# Dynamic workflows

A dynamic workflow turns a goal into a multi-phase run you can watch, restart in
part, and save for reuse. Ask for one in plain language and the agent authors the
orchestration script itself, then runs it in the background — you get a run id
immediately and the result arrives back in the chat when it finishes.

Reach for a workflow instead of a plain sub-agent fan-out when the work has
distinct phases, when you want to see it progress, or when you expect to re-run
part of it after changing your mind about one step.

## Starting one

Say what you want. "Use a workflow to compare these three approaches and
recommend one" is enough — the agent passes your goal as the intent, and the
authoring and launching happen in one step. You do not write the script.

Two watching surfaces show a live run:

- The chat side panel's **Workflows** tab, next to Changes and Subagents.
- **Agent Capabilities → Workflows**, which is the saved library rather than the
  live view: it creates, edits, and runs reusable workflows.

A run streams to the panel while it executes and injects its result into the
chat on completion, so you do not have to poll it yourself.

## Watching and steering a run

| Ask for | What you get |
|---|---|
| The live status | Whether the run is running, finished, failed, or cancelled, plus how many agents and events it has produced |
| The full result | Every phase, each agent's outcome, the logs, and the final return value |
| Recent runs | The newest runs first, with their status |
| Cancel | The run stops |

A run that ended without a usable return value still reports the agent payloads
that did complete, plus a per-agent failure reason for each one that did not — so
a partial failure is diagnosable rather than a bare error.

## Restarting part of a run

This is the reason to prefer a workflow over a one-shot fan-out. A restart
replays the unchanged prefix from cache and re-executes only from the step you
name: agent calls before that point reuse the prior run's results without
spending a model call, and calls from that point on run fresh. Restarting from
the very beginning re-runs everything.

Each restart produces a new run id, so the original run's record is preserved
rather than overwritten.

## Saving one for reuse

A workflow saved to the library can be run again by name instead of re-authored
from intent. Ask what is already saved before describing a new workflow — an
existing one that fits skips the authoring step entirely.

## What comes back

Credentials and exfiltration-shaped URLs are stripped from every workflow
response before it reaches you, including from mapping *keys* and not only
values — agent output is parsed into these structures, so a credential can arrive
as a key.

## Related docs

- [Subagents](subagents.md): parallel fan-out when the work needs no phases or restarts
- [Task runner](task-runner.md): autonomous multi-step execution from a spec file
- [Dashboard](dashboard.md): the chat side panel and the Capabilities tabs

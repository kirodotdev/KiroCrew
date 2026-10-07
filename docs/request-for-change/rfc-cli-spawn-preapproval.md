---
title: The owner's terminal may approve its own spawn up front
status: draft
author: buluoray
created: 2026-10-07
last-audited: 2026-10-07
audited-at: 676545447b
doc-pr: null
implementation-prs: []
tracking-issues: [16313]
supersedes: []
superseded-by: []
---

# RFC: The owner's terminal may approve its own spawn up front

- Status: draft. Acceptance requested from a maintainer; the status flips to
  `accepted` when one records it here. Nothing below is on main.
- Depends on [#15593](https://github.com/kirodotdev/KiroCrew/pull/15593) (open),
  which makes `kirocrew spawn run` authenticate as the dashboard owner with a
  token carrying a signed `origin: "cli"` claim. Every mechanism this document
  relies on comes from that PR, and the implementation waits for it to merge.
- Tracking issue: [#16313](https://github.com/kirodotdev/KiroCrew/issues/16313).

## The decision

`kirocrew spawn run` gets an `--approve` flag. When the owner passes it, the run
the command creates starts without raising the spawn prompt, because the person
at the terminal has just approved it. Without the flag nothing changes: the run
faces the ordinary spawn prompt, as #15593 ships it.

The flag answers the spawn prompt and nothing else. It is not a standing waiver,
it is not a config key, it is off unless typed on the command, and it never
pre-approves a tool call inside the run.

## Why this does not loosen the gate

Skipping an approval prompt is owner-only unless it is equivalent to a grant the
caller already holds. This one is. The caller is the holder of an owner token
minted by `GET /api/token/local?origin=cli`, and that holder can already skip the
prompt in two ways on the code #15593 lands:

1. **It can ask for no prompt.** `POST /api/spawn` accepts `approval_mode: "auto"`
   from any caller the route admits (`api_spawn` in
   `src/kiro_crew/dashboard/messaging_api/spawn.py`), and #15593 admits an owner
   token on that route. `approval_mode="auto"` is the second rung of the cascade in
   `spawn_impl` (`src/kiro_crew/subagent_manager/admission/gate.py`): it skips the
   spawn prompt AND sets the run's tool approval to `auto` for its whole lifetime.
2. **It can answer the prompt itself.** The prompt is raised as `spawn:<run id>`
   and `POST /api/approvals/{id}/approve` (`api_approval_resolve` in
   `src/kiro_crew/dashboard/handlers/sessions.py`) resolves it for an owner
   credential.

So the question #16313 asks is not "may this caller skip the prompt" (it can) but
"should the owner's terminal have a narrow, named way to do it". The flag is
strictly narrower than route 1, since tools keep their own approval, and it
replaces route 2's poll-then-approve race with one decision recorded at
submission.

### Who can reach it

The threat is whoever holds the owner's local secret at the host. On Linux and
macOS that is not enough: `api_token_local` also requires
`local_owner_bootstrap_allowed` (`src/kiro_crew/member_memory_auth.py`), which
admits only a process in the gateway's own namespaces on Linux and an
unsandboxed process on macOS. A sandboxed agent shell cannot mint the token, so
it cannot pass the flag.

Where that check proves nothing (the sandbox is off, or Windows, where
`_verified_host_process` returns true for any process), an agent can already mint
an owner token and use either route above. The flag adds no reach there; it only
gives the owner a cleaner way to do what such a host already allows. That gap is
the sandbox's, and this design neither widens nor closes it.

## Scope

| Case | With `--approve` |
|---|---|
| The run this command creates, started at once | Starts without the spawn prompt |
| The same run, queued (capacity, memory) in the accepting process | Keeps the approval while it waits, held in process memory the way `_held_approval_modes` holds `approval_mode` today |
| The same run, still queued when the gateway restarts | Faces the ordinary prompt |
| `kirocrew spawn` retry, continue, or any later turn of the run | Faces the ordinary prompt |
| A run the CLI run's model spawns | Faces the ordinary prompt (`derive_execution` already clears `origin`) |
| A schedule the CLI run creates | Unaffected (`bind_cron_memory` already clears `origin`) |
| Tool calls inside the run | Unchanged: their ordinary approval applies |

### Restart

The approval is never written to the durable task row. A row that outlives the
process that accepted it is started by a process that never saw the owner's
command, so it raises the ordinary prompt. This fails closed and follows the
precedent `docs/system-specs/modules/subagent.md` records for `approval_mode`:
the row never carries the grant, both the write and the read side strip it, and
only the accepting process holds it while the row waits. Persisting the
approval on the row would let one request's consent start a run under a later
build or a later governance profile, which is the replay that precedent exists to
prevent.

## Wire and governance

- The CLI sends `spawn_approval: "owner"` in the `POST /api/spawn` body.
  `api_spawn` honours it only when the request's token is the owner principal's
  and its signed origin is `cli` (`validated_token_origin`, from #15593). Any
  other caller sending the field (internal secret, app token, a dashboard owner
  token without the CLI origin) gets 400 `invalid_spawn_approval`, so the field
  cannot become a second spelling of `approval_mode`.
- It is one more rung in the existing cascade, placed after `approval_mode`, and
  it runs after every governance check: a `surface:cli` profile that disables
  spawning, or a `scopes.agents` allow-list that refuses the agent, still refuses
  the run first. The run-time re-check #15593 adds still applies.
- No config key. A standing switch in `config.json` would be writable by any
  auto-approved agent shell (`_spawn_with_approval_impl` in
  `src/kiro_crew/subagent_manager/admission/pump.py` records why the remedy text
  for this gate stays out of agent-visible output), and it would apply to every
  CLI call rather than the one the owner typed.

## Audit

The skip writes the same SEL row the other rungs write: `outcome:
"auto_approved_spawn"`, `tool_name: "spawn_run"`, `reason: "owner_cli_approved"`,
under the `cli_chat` governance key #15593 assigns. A CLI run has no parent
session, so `dispatch_origin` finds no session to file a crew-log
`approval/requested` entry under, and none is written; the SEL row is the record.

## Alternatives not taken

- **The on-by-default waiver #15593 first shipped.** It turned an approval off
  for every CLI run without the owner asking, which is the governance change that
  PR removed for want of a decision.
- **Exposing `--approval-mode auto` on `kirocrew spawn run`.** No gate code, but
  it also pre-approves every tool call in the run, which is far more than skipping
  one prompt.
- **A config key such as `hooks.cli_spawn_skips_prompt`.** A silent standing
  waiver, broader than the command it is meant for, and writable by the party the
  gate constrains.
- **Doing nothing and pointing at `auto_approve_subagent_spawn`.** That key
  approves every spawn on the gateway, including every agent's, to solve a
  problem that is about one terminal command.

## Rollout

One implementation PR after acceptance and after #15593 merges: the flag and its
help text in `cli_commands.py`, the body field and its refusal in `api_spawn`,
the cascade rung in `gate.py`, the SEL reason, and the `subagent.md` § CLI
paragraph that today says the prompt skip is undecided. Tests pin each row of the
Scope table, the 400 for every non-CLI caller, and that a queued approved row
faces the prompt after a simulated restart.

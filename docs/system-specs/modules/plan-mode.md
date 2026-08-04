# Plan Mode Module

Last Updated: 2026-08-03 (initial). Plan mode is a per-session, server-owned
read-only gate for the dashboard: while it is armed the agent investigates and
writes a plan, and file writes and mutating shell commands are denied before
they run. Enforcement is a single chokepoint on the permission plane; the
documented limits below are as load-bearing as the mechanism.

## Overview

A dashboard chat slot can be put into plan mode from the composer's "+" menu.
While armed:

- Reads, searches, LSP lookups, web fetches and read-only shell commands run
  normally.
- File writes and edits, mutating shell commands, `git`, and package installs
  are denied at the tool gate, ahead of every convenience approval setting.
- The agent closes its plan by asking whether to implement it; the user's
  answer is what lifts the gate.

Plan mode is native rather than a mode swap to kiro-cli's builtin
`kiro_planner` agent. That agent's spec carries an explicit `tools` allowlist
with no MCP entries and `includeMcpJson: false`, so entering it would cost the
whole KiroCrew toolset (subagents, memory, crons, artifacts), and `set_mode`
exists only on the kiro backend.

## Modules

| File | Role |
|---|---|
| `plan_mode.py` | Registry (`activate` / `deactivate` / `set_active` / `is_active` / `inherit` / `session_key_for_slot`), the read allowlists, `deny_reason`, denial vocabulary |
| `tool_identity.py` | `normalize_tool_identity` → `(bare, qualified)` |
| `bash_readonly.py` | The read-only shell classifier, extracted so a security leaf can import it without the dashboard graph; `dashboard/state.py` re-exports its three public names |
| `hooks.py` | The one enforcement chokepoint (`HookManager.on_tool_call`) |
| `context.py` | `PLAN_MODE_BLOCK`, injected per turn |
| `dashboard/chat_folders.py` | `PATCH …/plan-mode`, `POST …/plan-approve` |
| `dashboard/session_directive_apply.py` | Authorizes the end-of-plan handoff card |

## Enforcement: one chokepoint, on the permission plane

`HookManager.on_tool_call` consults `plan_mode.deny_reason` as the first rung
after the shell deny-by-default guard, so it runs **ahead of** auto-approve,
trusted patterns, trust-reads, trust and YOLO on every surface. The point is
that plan mode cannot be waived by a convenience setting the user already
enabled.

Three rules govern what the gate is allowed to trust:

1. **Shell is decided on the recovered `command`, never the title.**
   `select_tool_title` prefers a model-authored `description`, so the title is
   prose. A shell call whose command cannot be recovered is denied upstream.
2. **Non-shell allow decisions key on the canonical `_meta.kiro` identity**
   (`AcpEvent.tool_name` / `.mcp_server_name`), threaded into the hook as
   `trusted_tool_name` / `trusted_server_name`. Keying on the title was wrong in
   both directions: a write tool described as "Read" matched the read allowlist
   and was auto-approved, while a genuine read titled "Read /etc/hosts" was
   denied. When the backend emits no `_meta.kiro` the value is `""`, which
   matches nothing and therefore denies — loudly unusable rather than silently
   bypassable.
3. **MCP tools match on `@server/tool` only.** A bare-name match would let any
   server inherit another's allowance.

The `code` multiplexer is decided per `operation` from `raw_params`, which is
trusted for the same reason `command` is: it is the real input the call
executes with.

The allowlists are deliberately **not** config-sourced. Config is
agent-writable, so a gated session could otherwise widen its own gate — the same
reasoning as `platform/interfaces.py::heartbeat_safe_tools`.

## Documented limits (assert-don't-assume)

**Auto-approved tools are not gated.** kiro-cli approves anything in the shipped
`allowedTools` locally and emits **no permission request**, so those calls never
reach `on_tool_call`. That currently covers the `code` builtin's write
operations (making `CODE_READ_OPERATIONS` unreachable for the default agent) and
the whole `@kirocrew-core` server, including `task_run`, `learn_add`,
`artifact_save`, `artifact_delete`, `send_message` and `deploy_artifact`, plus
five cron verbs. `cron_add` / `cron_update` are not auto-approved and are
correctly denied.

**A dispatch-side gate cannot fix this as-is.** `mcp_shared.call_tool_with_logging`
runs inside the `mcp-core` / `mcp-cron` stdio child process that kiro-cli spawns,
while `plan_mode._ACTIVE` is mutated only gateway-side. An earlier revision put a
gate there; `is_active()` returned `False` unconditionally, so it was inert while
appearing to work, and its test passed only because it called the wrapper
in-process after arming in the same interpreter. The gate was removed and the
absence is now asserted (`TestAutoApprovedMcpIsADocumentedGap`) rather than left
to be rediscovered.

**Closing it properly** requires the flag to reach that process. The plausible
route is extending the `GET /api/session-tool-policy` response `mcp_shared`
already fetches with `X-Internal-Secret`; that needs a short cache TTL (the
current per-session cache never re-fetches, so a mid-session toggle would go
stale) and a deliberate fail-open-versus-closed choice, since plan mode has no
independent enforcement to fall back on the way `disabledTools` does. Tracked
separately rather than half-built.

## State

Server-owned. `_ChatSlot.plan_mode` is persisted to the session's JSONL metadata
line, and `chat_runner._run_chat` re-syncs the process registry from it on
**every** turn — that is what restores the gate after a gateway restart, when the
process-global registry starts empty.

`PATCH /api/chat/slots/{slot}/plan-mode` refuses with a coded 409 while a turn is
running **and** while sub-agents are still live: a fire-and-forget child inherits
the gate only at its own start, so arming mid-flight would leave those children
ungated for the rest of their run. The key comes from
`plan_mode.session_key_for_slot`, which prefers `linked_session_key` — a cron- or
workflow-driven slot runs under `cron:<job>`, and keying on `dashboard:<slot>`
would arm a string the gate never consults.

Sub-agents inherit through `plan_mode.inherit` at the top of
`SubagentManager._run`, released in the run's `finally` conditioned on whether
this run did the inheriting. An AST test ties `spawn_run`'s allowlist entry to
the propagation existing.

## Leaving plan mode

A **typed action**, never a matched phrase. Deciding "this message means
approval" by reading prose breaks under translation or a lightly edited answer.

1. The agent calls `ask_question` with `plan_handoff: true` and one question.
2. `mcp_core` only *forwards* the flag and replaces any model-supplied options
   with two server-authored ones — the model must not word the control that
   lifts its own restriction.
3. `session_directive_apply._ask_question`, in the gateway, re-checks
   `plan_mode.is_active(session_key_for_slot(slot))` before honouring the flag.
   An unarmed session gets an ordinary card, so the flag cannot conjure a
   control that leaves a gate which was never on.
4. The frontend renders the two choices from its own catalog and reports the
   user's pick as a typed value derived from the option **index**. A typed
   custom answer counts as feedback however affirmative it reads.
5. "Start implementing" calls `POST /api/chat/slots/{slot}/plan-approve`, which
   disarms the gate **and then** starts the turn, in that order — the reverse
   denies the implementation's own first tool call. A failed disarm keeps the
   card and leaves plan mode on.

## Shell classifier hardening

The read-only classifier gated `trust-reads` before plan mode existed and let
several mutating commands through. All were pre-existing on main:

- a `--help` / `--version` **suffix** exempted the whole line, so
  `bash -c '<payload>' --help` classified read-only (now anchored both ends);
- `git branch` / `tag` / `remote` matched by prefix, so `git branch -D` read as
  read-only (now whole-command matching);
- write flags slipped through: `sort -o`, `tree -o`, `git diff --output=`, and
  attached short forms like `-oFILE` (now a per-command `_WRITE_FLAGS` table).
  `git` bans only long `--output`, because `git ls-files -o` means untracked;
- `uniq [INPUT [OUTPUT]]` writes its **second positional operand**, invisible to
  a flag table (now a per-command operand cap; sibling filters keep multiple
  operands because theirs are additional inputs).
- The shell rewrites the argv **after** every per-token check has run, so the
  string the classifier inspects is not the one that executes. Four forms hit
  this, and they do not all defeat the same check: `sort -{u,o/tmp/f}` expands to
  `sort -u -o/tmp/f` and `sort "-o/tmp/f"` executes `sort -o/tmp/f`, both beating
  the **flag table**; `uniq [ab]` with files `a` and `b` runs `uniq a b`, beating
  the **operand cap**, which counted one operand where two will exist; and
  `sort $IFS-o$IFS /tmp/f` expands to `sort -o /tmp/f`, beating both, since the
  flag is not a token at all beforehand. The responses differ by what giving up
  the form costs: braces are rejected outright (`_UNSAFE_SHELL_RE`), being a
  convenience no read needs; quoting is **parsed** with `shlex`, since
  `grep "a b"` is an ordinary read, and unbalanced quoting fails closed; globbing
  and `$` expansion are rejected only for commands that can actually write — those
  with a write flag or a writing operand — so `ls *.py`, `cat $HOME/.bashrc` and
  `grep 'x$'` keep working while `sort *` and `uniq [ab]` do not. All of them
  reached trust-reads as well as plan mode, so none is exempted in the looser
  mode. Note the glob and `$` vectors need a planted `-o` filename or a second
  operand, which plan mode denies but trust-reads permits before auto-approving
  the read.

## Tests

`test/test_plan_mode_gate.py` (gate, trusted identity, MCP policy, the
documented gap, sub-agent wiring), `test/test_chat_slot_plan_mode.py` (endpoint
refusals, disk round trip), `test/test_context_plan_mode.py` (per-turn injection,
marker forgery), `test/test_ask_question_mcp_tool.py` (handoff authorization),
`test/test_trust_reads.py` (classifier escapes), and
`website/src/test/PendingQuestionCard.test.tsx` +
`ChatInput.planMode.test.tsx` (typed choice, fail-closed disarm, toggle/chip).

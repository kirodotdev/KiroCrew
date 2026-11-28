# Which tools prompt, and what the evaluator runs

Moved verbatim from the goal-conductor `SKILL.md` core; the core says when to read each part.

## What the evaluator runs

Everything else belongs in a work item. This spec has **no file-writing tool at
all** (no `fs_write`, no `code`); `fs_read` and `web_fetch` are what you read the
world with. A file to write, or a build, test or fix to run, is a work item, not
something you do: the evaluator only CHECKS what is already true. The acceptance evaluator this skill bundles
(`scripts/accept_eval.py`) runs as the `kirocrew-work::accept_eval` tool. It
imports the same script, takes typed arguments (no command, path to run or shell
string), and is auto-approved, so a verify cycle never waits on a click.
`execute_bash` is still mounted as its fallback only, for a spec without that tool; it is
deliberately kept out of `allowedTools`, so every shell call prompts and is
audit-logged. The evaluator accepts **no command, argv array or shell string
from a spec**: it builds every argv from a fixed template (`pr_checks` becomes
`gh pr checks <n> [--repo <owner/name>]` between two
`gh pr view <n> [--repo <owner/name>] --json headRefOid -q .headRefOid` reads, and behind a `pending` verdict also
`gh pr view <n> [--repo <owner/name>] --json isDraft -q .isDraft`, `pr_merged` becomes `gh pr view <n> --repo <owner/name>
--json state,headRefOid,mergeCommit`, `edition_check` becomes `kirocrew acceptance-check --json <check>
<ref>`, `git_commit` becomes `git -C <repo> rev-parse --verify` and `git -C
<repo> rev-list --count`), and nothing else runs. A spec that could name a command would make this
approved wrapper a general way to run one, which the denied-command floor cannot
see inside. Widening means a purpose-built kind, never an accepted argv.

Each check is bounded by `TIMEOUT_SECS`, and a check that runs past it is an
`error` verdict. Inside the gateway the argv runs through
`platform_compat.run_killing_tree_on_timeout`, so on Windows a timeout also ends
a launcher's descendants (a `.cmd` shim's child, the real `git.exe` behind
Git for Windows' launcher) and the verdict arrives on time. Run as a bare file
with no `kiro_crew` importable, the script falls back to plain `subprocess.run`.

## What prompts

**What prompts.** Granted by name, so a patrol wake with nobody at the keyboard
never blocks: `session_create`, `session_read_message`, `session_status`,
`work_ledger_read`, `work_ledger_record`, `work_ledger_rebuild`,
`work_ask_record`, `work_brief`, `work_report`, `accept_eval`, and these
`@kirocrew-core` verbs only: `monitor_start`, `monitor_update`,
`autonudge_stop`, `wait`, `resource_status`, `list_sessions`,
`session_ledger_read`, `session_ledger_record`, `skill_search`, `skill_fetch`,
`select_crew`, `send_message`, `send_notification`, `ask_question`,
`nothing_to_do`, `reset_conversation`. `session_send` and `session_close` into
a session you created do not prompt either (`agent.conductor_own_child_verbs`,
on by default); aimed at any other session they prompt, because a send runs as
that session's own turn and you ingest external content by design.
`work_dispatch` prompts once per item, since its last step sends the seed.
`session_stop` (it discards the target's in-flight work), `execute_bash` and
every other core tool prompt, including `task_run`, `workflow_run` and the
`spawn_*` family, which you never route an item to. A governance ceiling or an operator `hooks.confirm_tools` pattern can
make even a granted verb prompt. A prompt is a tool approval, never a reason to
hand a worker's choice to the person: decide it as Patrol step 2 says.

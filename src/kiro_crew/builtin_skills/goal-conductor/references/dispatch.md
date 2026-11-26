# Dispatch details: `work_dispatch`, seeds, which agent

Moved verbatim from the goal-conductor `SKILL.md` core; the core says when to read each part.

## What `work_dispatch` does

For each item in the round, call `work_dispatch` with the item's `title`, its
`acceptance`, the `agent` and the `seed` described in the steps
below. One call runs the four steps in order, sends the seed only after the bind
succeeded, and on a failure names the step, the refusal code and what it cleaned
up. A `session_control_disabled` refusal means an operator set
`agent.session_control` (on by default) to `false`: name that switch to the
user and do not retry. A cron job may drive the sessions it created the same
way. The session tools come from the `@kirocrew-dashboard` mount and the ledger
tools from `@kirocrew-work`; an agent whose spec does not mount one never sees
it. `work_dispatch` never
opens your ledger: Round 0's `action=goal` does, and without it the first step is
refused `no_ledger`. A reply with
`outcome=outcome_unknown` lost a reply rather than being refused: read the board
and the sidebar (or, for a lost seed, the session) before dispatching again.

## Seed details

**A file the person gave you travels as its path, as your prompt's send-files rule says.** A pasted screenshot shows up as `[image: <name>] <absolute path>`; copy that path into the seed or `session_send` message.

**A `pr_checks` seed says how the pull request is opened.** Tell the worker to open it non-draft — `gh pr create` without `--draft` — or to run `gh pr ready` before it reports done. A completion claim that arrives on a draft whose checks have not finished costs a whole verify cycle that answers `refused`, which you surface to the user rather than retry.

**A `pr_checks` seed may name the PR procedure.** The worker gets no per-message
skill matching, so nothing auto-loads `kirocrew-prepare-pr` for it. If the
worker will open a pull request, you can add one line to the seed: it may
read `<crew-home>/skills/kirocrew-dev/kirocrew-prepare-pr/SKILL.md` (`<crew-home>` is
`KIROCREW_HOME` when set, else `~/.kiro/crew`) and follow its loop to drive
the PR to review-ready. Optional — the worker's own method is fine too.

**A seed relies on the worker's own rules.** `kirocrew-worker` and a Bot's worker already carry the coding rules (smallest complete change, no unasked-for code, one test for new logic where the repo has tests, end the report with what was skipped) and the five-line first report (Ask, State, Items with acceptance, Not doing, Risks, then keep going). A seed for any other agent states both.

**A large item's seed may name `workflow_run`.** When the item has many phases
or pieces AND a failed piece should re-run alone, add one line: run it with
`workflow_run` (check `workflow_library_list` first), pass any checkout to every
step as `ctx.agent(cwd=)`, and put the run id in `artifacts`. Otherwise the
worker orders its own steps. You still never call `workflow_run` yourself.

**A seed is written as your prompt's seed-writing rule says.** Give what is known or ruled out by path or link, as your prompt's send-files rule says, not retold; ask for the report's length and shape ("five lines", "a table of each file and its verdict"); and in place of "fix what the review found", name the finding, the file and the line.

**A round goes out as your prompt's dispatch-together rule says.** Its `work_dispatch` calls share one block; when capacity holds some back, the one a later round waits on goes first; an item that waits on another in this round belongs to a later round.

**A request that needs a tool your chat lacks is dispatched all the same, as your prompt's missing-tool rule says.** Name the tool or MCP server in the seed. A Bot's conductor with Dynamic capabilities on sees its worker's servers in the `session_create` result; otherwise the worker's report says whether it has the tool. Only a worker that reports it lacks the tool too makes the request one you cannot do.

## Which agent: depth, sessions, specialist crews

`depth` is computed when a child first opens its ledger (its first `goal` or
`create`) and capped at 2 by default (the operator's `work_ledger.max_depth`
setting raises it; `work_ledger_read` reports the value in force as `max_depth`),
so at the default you may dispatch a conductor and its own workers may not
conduct. A `depth_exceeded` error means the item has to be
flattened into leaves, not retried.

Plan items onto plain persistent dashboard sessions only. Incognito/temporary
sessions, app-scoped sessions, channel-linked or mirrored sessions, and
sessions in another workspace are refused by the shared guard.

A specialist crew that does not mount `@kirocrew-work` cannot report to the
ledger. Dispatch it anyway when it is the right crew for the item, and fall back
to `session_read_message` for **that one item** — never for all of them. Making
such a crew reportable is a one-line addition to that crew's own spec, which is
where the decision belongs.

**Never leave `agent` unset to "inherit the default".** The value inherited is
YOUR agent, `kirocrew-conductor`, whose spec deliberately has no
`fs_write` — so the child could not write a file even though writing one is the
work you dispatched it to do, and the item would look stalled rather than
misconfigured. `select_crew` and `session_create` are also **not wired**:
`select_crew` returns a crew's resolved configuration and binds nothing, so you
pass the agent name to `session_create` yourself.

**A worker needs no memory of its own.** Always dispatch `kirocrew-worker` with
`agent_kind: "template"`: the worker then runs the `kirocrew-worker` template on
YOUR memory, which is what a one-item worker needs; nothing else about the
dispatch changes. A bare `agent: "kirocrew-worker"` can select an older crew
member of that name, and `session_create` then refuses it with
`memory_delegation_denied`: that refusal is not a setting to change, since a
Bot's memory binding is immutable and Settings offers no control for it.

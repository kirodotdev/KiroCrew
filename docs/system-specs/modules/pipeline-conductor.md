# Pipeline Conductor

`kirocrew-pipeline-conductor` is a generated kiro-cli agent that runs one
issue/PR pipeline on one repository as a supervised worker fleet. It picks up
queued work items, stands up one worker session per item, patrols the fleet with
one deterministic probe call per cycle, verifies claimed results independently,
intervenes on stalls, adjudicates blocked items, governs host capacity and
per-item credit budgets, and reports verified greens to the person.

**It never does a work item's work.** No file edits, no builds, no fixes in its
own turns. That is a property of the generated spec, not only of the prompt: the
agent has no dedicated file-writing tool, and a work item never goes to
`spawn_run`, `task_run` or `workflow_run`.

The shape is *agent plus agent skill*: the `pipeline-conductor` builtin skill
carries the operating procedure, three bundled scripts carry the bookkeeping, and
the agent carries the judgment. The skill is the procedure of record; this spec
is the contract for the machinery underneath it.

## Components

| File | Role |
|---|---|
| `src/kiro_crew/agent.py` | `_install_pipeline_conductor_agent`, `_PIPELINE_CONDUCTOR_SYSTEM_PROMPT`, `_PIPELINE_CONDUCTOR_CORE_GRANTS`, `_PIPELINE_CONDUCTOR_DASHBOARD_GRANTS` |
| `src/kiro_crew/agent_files.py` | `PIPELINE_CONDUCTOR_AGENT_FILENAME`, and its membership in `OWNED_KIRO_AGENT_FILES` |
| `src/kiro_crew/subagent.py` | `UNADVERTISED_AGENTS` — the conductor is never offered in a rendered agent roster |
| `src/kiro_crew/builtin_skills/pipeline-conductor/SKILL.md` | The operating procedure: pipeline spec, claim preflight, work-order brief, probe cycle and action table, intervention ladder, adjudication and override protocol, admission table, credit rules, `conductor-status/v1`, cleanup |
| `.../pipeline-conductor/scripts/claim_preflight.py` | One claim verdict per candidate item |
| `.../pipeline-conductor/scripts/fleet_probe.py` | The batch patrol probe |
| `.../pipeline-conductor/scripts/credit_spend.py` | Per-item credit rollup and budget verdict |
| `src/kiro_crew/dashboard/session_control.py` | Worker-session stand-up and control: `create_session`, `send_to_target`, `read_messages`, `stop_target`, `authorize_target` |
| `src/kiro_crew/session_ledger.py` | The conductor's durable item table and the `[work ledger]` snapshot it must not mistake for the record |

## The generated agent

`_install_pipeline_conductor_agent` runs on the same boot path that installs the
other managed agent specs, writing `kirocrew-pipeline-conductor.json` atomically
into the kiro agents directory. It derives from `build_agent_config` and then
narrows, and each narrowing is a permission decision:

- **Tools:** `execute_bash`, `fs_read`, `web_fetch`, `session`, `report`,
  `tool_search`, plus the `@kirocrew-core` and `@kirocrew-dashboard` servers.
  Neither `fs_write` nor `code` is mounted, which is what makes "never does the
  work" structural.
- **`allowedTools` is verb by verb, never a whole server.** The conductor ingests
  untrusted content by design (issue text, PR bodies) on unattended cycles, so a
  server-wide grant would let that content start persistent work or spawn
  arbitrary subagents with nobody in the loop.
- **`mcpServers` is narrowed** to `kirocrew-core` and a hand-built
  `kirocrew-dashboard` entry. That entry carries `type` in registry mode (a
  registry-mode client silently drops an entry without it) and the managed MCP
  env (without the data-home pin the shim reads the default home while the
  gateway runs under an override).
- **`permissions` is derived** from the filtered `allowedTools` through the
  agent-SDK boundary, never restated.
- **A withheld grant is audited.** Every reference is filtered through
  `_may_auto_approve`; anything the governance ceiling withholds is recorded in
  the SEL as `mcp_auto_approve_withheld` and then goes through the ordinary
  approval gate.

Auto-approved core verbs are reads (`resource_status`, `list_sessions`,
`skill_search`, `skill_fetch`), the conductor's own patrol lifecycle
(`monitor_start`, `monitor_update`, `autonudge_stop`, `wait`), its own durable
ledger (`session_ledger_read`, `session_ledger_record`), and reporting to the
owner (`send_message`, `send_notification`, `ask_question`). Auto-approved
dashboard verbs are create-or-read only: `chat_folder_tree`,
`chat_folder_create`, `session_create`, `session_read_message`.

`session_send`, `session_stop` and `spawn_run` are mounted and **never**
auto-approved, even though the intervention ladder uses all three. They start
agent work from ingested context, so unattended operation gets them from the
operator arming the conductor's own session in trust mode, not from a standing
spec-level bypass. `execute_bash` is the same case and is the sharper one in
practice: every script call goes through it, because `allowedTools` cannot match
arguments, so trusting the three bundled scripts cannot be told apart from
trusting arbitrary shell. A conductor session that was not armed therefore stalls
on its first probe, not on its first intervention.

## The pipeline spec file

The operator's seed message names a JSON spec. It is data the conductor reads,
never values inlined from memory, and it is not a modelled type in the codebase
today (see [Not implemented](#not-implemented)). The fields consumed now, shown
with the defaults that apply when the spec omits them:

```json
{
  "id": "issue-fix",
  "repo": "<owner>/<repo>",
  "default_branch": "main",
  "work_source": {"kind": "gh_issues", "select_labels": ["auto-fixable"],
                  "skip_signals": ["claimed", "in-progress"]},
  "worker_contract": {"branch_pattern": "fix/{slug}-{n}",
                      "worktree_pattern": "../{repo_name}-fix-{n}"},
  "governance": {"max_in_flight": 32, "max_per_cycle": 3,
                 "idle_alert_secs": 900, "session_ceiling": 30,
                 "credit_budget_per_item": 100, "topup_ceiling": 2},
  "interface": {"folder_name": "pipeline-{id}", "digest_language": "auto"}
}
```

The governance block is what bounds the fleet: a ceiling on dispatched workers, a
per-cycle dispatch limit, the silence a worker may accumulate before the probe
fires `IDLE`, a session budget for the run, a per-item credit allowance, and how
many budget top-ups an item may receive. The interface block names the chat
folder the pipeline's sessions live in and the language its digests are written
in.

The spec file's directory is the run's state home: the probe config is
`<spec-dir>/probe-config.json` and the probe owns
`<spec-dir>/probe-config.json.state.json` as its handled-set. `fleet_worktrees`
is optional in that config and is what makes `cwd=fleet` reachable, so omitting
it classifies every banned-process line as `foreign` or `unknown` and the one
enforcing row of the banned-ops table never fires.

## The three scripts

Anything the procedure states as prose rots silently; anything a script computes
can be tested. So every decision the skill delegates is the script's answer to
read, never a predicate for the agent to re-derive.

**`claim_preflight.py`** answers one candidate item with one verdict, branched on
the exit code: `CLAIM` 0, `UNKNOWN` 3, `SKIP` 10, `CLOSE` 11, `REVIEW` 13. Two
rules are load-bearing. `UNKNOWN` is never permission. And `REVIEW` is a closure
request read out of the item's prose, which the conductor confirms itself,
because prose never closes an item.

**`fleet_probe.py`** answers, in one call per cycle, whether anything in the
fleet needs judgment: per-session tail classification, tail index, idle age,
error tails, a banned-process scan, host load, and the delivery counters. Three
properties matter beyond the classification:

- **Paths are derived, never configurable.** Transcripts come only from
  `<data home>/sessions`, the handled-set state file is always
  `<config path>.state.json`, and the banned-process scan reads `/proc`. The
  config is authored by an agent with no write tool, and a config-chosen path
  would quietly widen what an approved run can reach.
- **Output is metadata only.** No transcript-derived text appears in it, so no
  private session content crosses into the conductor's context whatever keys the
  config watches. Content, when a ruling needs it, is read through the
  workspace-authorized session tools.
- **`i=` is a monotonic per-session production index.** It counts rows that
  session produced, never an inbound nudge or user row, so a supervisor's own
  nudge cannot read as worker progress, and it is counted from the start of the
  file so it does not saturate once a transcript passes `tail_bytes`. The probe
  makes the comparison against the previous cycle itself and fires `NOPROGRESS`,
  rather than leaving two numbers for someone to diff.

A malformed regex in the config is reported as malformed config with the
offending pattern, never a crash mid-cycle.

**`credit_spend.py`** sums the credits an item's sessions burned from the
gateway's usage shards and answers `within`, `exhausted`, `truncated` or
`unmetered`. `exhausted` is monotone (more shards can only add spend, so it
stands on a partial view), and `unmetered` means at least one watched slot had no
shard row, which the caller must treat as unknown spend rather than zero.

## The patrol cycle

The conductor patrols with `monitor_start`, never `wait`, at roughly a 90-second
interval, and arms the loop with both the full cycle instructions and the exit
condition. Two cycle-order rules are structural rather than stylistic:

**The ledger is read at the top of every cycle, before the probe.** The
`[work ledger]` block prefixed onto a nudge turn is a teaser: `render_snapshot`
caps the whole block at 1600 characters, every field at 300, and the `tried` list
at its last 3 entries. A fleet's item table does not fit in that, and every row
of the action table is a comparison against recorded state, so acting first means
dispositioning a fleet against a summary of it.

**Item changes are written back as the whole `artifacts` map in one call.** The
ledger merges per key and keeps only the newest `_MAX_ARTIFACTS` (32) entries, so
a partial write ages an active item out. The corollary the skill states as
"reclaim before you admit": settled entries are collapsed first, so a full map
means no capacity rather than no tidying.

Admission is keyed on **delivery capacity first**, load and memory second. The
probe's `OK` line carries `deliver init-timeout <a>, watchdog <b>`; either
counter appearing twice in one cycle stops dispatching until two consecutive
clean cycles, while in-flight work continues, because what is short is delivery,
not compute. Load and memory can both read healthy while turns are killed by the
stall watchdog and sessions fail to initialize, so admission keyed on load alone
keeps dispatching into a fleet that cannot deliver and the failures then present
as the workers' fault.

Banned-operation lines are reported for every ownership class and **enforced with
a stop only for `cwd=fleet`**. `cwd=unknown` re-injects the directive without
stopping the session, a line with no `cwd=` field gets one attribution attempt at
action time, and `cwd=foreign` is counted only.

## Conductor-owned state

The session ledger records the items. `conductor-status/v1`, written beside the
spec and rewritten whole each cycle, records the conductor's own obligations:
`tally`, `workers` (with `last_index`, the previous cycle's probe `i=`),
`parked`, `open_rulings`, `conductor_tasks`, a bounded `events_tail`, and the
last `resource` posture.

Per-item state, session keys and PRs are the **ledger's**, cached here for one
cycle's fleet view; when the two disagree the ledger wins and this file is what
gets fixed. Two independent spellings of item state would drift, and the drift
would be silent.

`open_rulings` is reviewed every cycle independently of what the probe fired.
That is structural: the probe is right not to re-fire a signal already marked
handled, and that suppression is what keeps a quiet cycle quiet, so a worker on
an escalation hold goes silent by design and a debt the conductor owes becomes
invisible unless it keeps its own list.

## Failure modes

- **An absent script reads as `UNKNOWN`, never as permission.** Presence is
  checked at first use rather than assumed, so an install that does not carry a
  script loses that script's answers instead of gaining a default yes.
- **The patrol expires silently.** `monitor_start` defaults to 24 cycles, so a
  90-second patrol runs out in well under an hour, long before a fleet drains,
  and the loop simply stops with no symptom. `max_cycles` is passed explicitly
  and raised mid-run with `monitor_update`. Coasting into the cap is a failure,
  not a finish, so `autonudge_stop` is called deliberately.
- **The conductor cannot detect its own loop's death from the inside.** The
  procedure bounds what recovery it can and says so.
- **Credit metering covers dashboard-session turns only.** Inspector `spawn_run`
  turns and non-chat sessions burn invisibly, which is why `unmetered` exists as
  a distinct verdict.
- **Forge labels and assignees are the cross-operator lock.** The ledger is a
  cache and never the authority on anything another operator can also touch.
- **A no-write agent still needs a read boundary.** Every probe path is derived
  rather than configurable precisely because the config is agent-authored.

## Not implemented

The design of record is
[`../../request-for-change/rfc-pipeline-conductor.md`](../../request-for-change/rfc-pipeline-conductor.md)
(status `partial`). M0 is what ships and what this spec documents: the generated
agent, the builtin skill, its three scripts, and the `conductor-status/v1`
schema. Unbuilt, and deliberately not described above as behaviour:

- **M1:** a modelled PipelineSpec type and a SQLite event store. That type has
  no code hit outside the RFC; the spec file is read as plain JSON by the agent.
- **M2:** adjudication and SLA machinery as code. Adjudication today is the
  skill's protocol plus the status file, not an engine.
- **M3:** baking, compensation and per-repo objects.
- Five RFC decisions remain open.

Related but separate: `issue_radar_crew_read` / `issue_radar_crew_record` are the
**Issue Radar** app's own repository crew ledger, owned by
[issue-radar.md](issue-radar.md). The pipeline conductor does not mount them and
is not granted them; its durable state is the session work ledger
([session-work-ledger.md](session-work-ledger.md)) plus
`conductor-status/v1`. A work-ledger tool family generalized from the Issue
Radar one is proposed in
[`../../request-for-change/rfc-conductor-work-ledger.md`](../../request-for-change/rfc-conductor-work-ledger.md)
and is unbuilt.

The goal conductor (`kirocrew-conductor`, the `goal-conductor` skill) is a
different agent with a different charter: it decomposes a free-form goal, where
this one supervises a repository pipeline. The two installers deliberately share
mechanics and nothing else.

## Tests that pin this

| Test | What it holds |
|---|---|
| `test/test_pipeline_conductor_agent.py` | Identity and charter, the owned filename, the verbosity placeholder, patrol via `monitor_start` rather than `wait`, that the prompt names the tools and scripts it runs on, that no file-writing tool is mounted, that dashboard grants are create-and-read only, that core grants are named verbs rather than a whole server, that `mcpServers` is narrowed, and that a governed host withholds and audits |
| `test/test_pipeline_conductor_skill_contract.py` | That the skill cites the script rather than a prose predicate, that every exit code has a documented action, that all five verdicts are named, that `UNKNOWN` is never permission, that a prose closure request needs author authorization, and that an absent script has defined behaviour |
| `test/test_pipeline_conductor_probe_roundtrip.py` | That the probe classifies what the conversation log actually wrote, that the watchdog patterns match the constants the gateway emits, that the index needle matches the real writer, that a raw slot key finds the transcript the dashboard writes, and that `credit_spend.py` sums what the recorder wrote |
| `test/test_pipeline_conductor_claim_preflight.py` | The claim verdict lattice: merged-PR coverage and its near misses, fork PRs, prose self-claims, closure requests outranking claims, and absent-symbol risk handling |

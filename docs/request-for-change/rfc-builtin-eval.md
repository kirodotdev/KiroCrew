---
title: A built-in eval tool -- tiered tasks across harnesses, a Crew headless CLI, a weekly run, and harvesting merged PRs into standing cases
status: accepted
author: kirocrew agent session, directed by the repository owner
created: 2026-11-15
last-audited: 2026-11-16
audited-at: cb2f78b946
doc-pr: 37669
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: A built-in eval tool for Kiro Crew

Status: accepted. The repository owner, a maintainer, asked for the
direction on 2026-11-15, widened it the same day from long tasks only to a
weekly eval tool with three tiers, a Crew headless CLI and a case-harvest
step, and accepted it on 2026-11-16 with the PR that ships §12's first part
(`kirocrew chat --headless`). `kirocrew eval run --suite tasks` v1 is built
(§14.1); the rest is not. Every code reference was
read at `4bc26f6ecb`, and §12's at `cb2f78b946`.

It lands on its own because it adds new first-class surfaces (`kirocrew eval
run | report | harvest | admit`, headless flags on `kirocrew chat`, an eval
suite under `evals/`), publishes real-work-derived cases in a public
repository, and fixes what an unattended run may be granted in an eval home.
Those are product-shape and trust-boundary decisions.

Related: [`rfc-reroute-first-ask-policy.md`](rfc-reroute-first-ask-policy.md)
and open PR [#37609](https://github.com/kirodotdev/KiroCrew/pull/37609) (a
turn nobody typed skips the conductor's plan gate; §5 relies on it),
[`rfc-conductor-work-ledger.md`](rfc-conductor-work-ledger.md) (the ledger the
conductor arm is judged by),
[`evals/README.md`](../../evals/README.md) (the skill A/B evals, reused as a
suite) and `src/kiro_crew/eval/` (`kirocrew eval`, reused as the runner core).

## 1. The ask

The owner's goals, in their order:

1. Compare **Crew chat**, **Crew conductor mode**,
   **kiro-cli V3 alone**, **Claude Code** and **Codex**. Any ask of a person
   fails the run: the measure is fully unattended, end to end. Trust, tool
   permissions and Crew's startup approvals must not stall a run.
2. Use the same tool to compare a prompt or mechanism change before and after.
3. Collect real tasks into this repository, as the base for later evals and so
   others can check the results.
4. When several harnesses pass the same task, compare the code they wrote.
5. Leave room for non-code tasks, such as writing a doc.
6. Make it a built-in eval **tool** that runs every week, over three tiers
   (simple, medium, complex), not only long tasks.
7. Claude Code and Codex run through their own CLIs, so Crew gets a headless
   CLI too, and both Crew arms use it.
8. The key step: collect real tasks a little at a time from delivered, merged Crew PRs, convert them
   carefully, and turn them into standing cases (§13).

## 2. What exists today, and what is reused

| piece | today | reused as |
|---|---|---|
| `evals/<skill>/run_evals.py` (`evals/README.md`) | skill A/B; `--check` (deterministic, CI gate) and `--run` (needs a model) kept apart; the CLI under test comes from an env var (`EXPLAIN_FOR_EVAL_CLI`) | the `skills` suite. Same `--check` / `--run` split for every suite: `--check` never spends tokens and is the CI gate |
| `src/kiro_crew/eval/` (`kirocrew eval`) | short multi-session Crew scenarios; `--repeat N` with pass^k (`merge_repeats` in `eval/runner.py`); `--baseline` / `--max-drop` regression exit; `--junit`; `write_eval_artifacts` in `cli_commands_eval.py` writes `eval_<ts>.md` / `.json` | the runner core and the `scenarios` suite. pass^k, baseline compare, JUnit and the artifact writer serve every suite |
| `kirocrew chat -m` (`register_chat_parser` in `cli_parser_agent.py`) | one message, `--agent`, `--model`; runs in-process; a tool prompt waits for `y` / `n` / `o` on the terminal; returns after one turn; no structured result | extended into the Crew headless CLI (§12) |
| `kirocrew cron add --approval-mode auto`, `--script`, `--command` | zero-token scheduled jobs; `auto` auto-approves the job's session | the weekly schedule (§10) and the pattern for `--approval-mode` |
| an out-of-tree bench (the owner's, not in this repo) | Crew conductor and Claude Code on synthetic repos with a local PR/CI stand-in and a hidden check on origin main | its case shape, stand-in and counters become the `tasks` suite |

Not reused: `kirocrew run TASK.md` already exists as the autonomous task
runner (`register_run_parser` in `cli_parser_agent.py`), so the name is taken
(§12.1).

## 3. Tiers, arms, and one runner

### 3.1 Three tiers

| tier | shape | timeout | spend cap per run | runs per week (k) | candidates today |
|---|---|---|---|---|---|
| simple | 1 round, 1 item, reference wall <= 10 min, no PR wait | 20 min | 30 Kiro credits / USD 3 | 5 | out-of-tree `tasks-v2` T2, T3, T4, T5 (3.5-6 min median wall for Crew); `tasks` G4a; the `scenarios` and `skills` suites |
| medium | 1 round, up to 3 items or up to 90 min, one PR wait | 120 min | 120 credits / USD 12 | 3 | `tasks-v2` T1 (85 min median, a 56-min background job); `tasks` G1, G2, G3 |
| complex | 2+ rounds, or 4+ items, or over 90 min; CI and review waits in real wall time; main moves mid-run | 240 min | 400 credits / USD 40 | 2 (5 in a before/after pair, §6) | out-of-tree `tasks-real` TR1, TR2, TR3 (1-4 h) |

- The caps are starting values from measured runs (15 short Crew runs spent
  about 340 credits, about 23 each); each case may set its own inside its tier's
  cap, and the weekly report shows how close runs came to it.
- Simple cases are a floor, not a discriminator: the current build passed
  T2-T5 3/3 (the out-of-tree calibration called them too easy for a
  before/after). In the weekly run a simple-tier failure is a regression signal
  on its own.
- `tasks` G4b and G4c test that the agent DOES ask when it must (an owner-only
  limit, an open blocker). They contradict "any ask fails", so they stay out of
  the scored suite and run as a separate safety probe whose expected outcome
  is an ask.
- The tier is a field of `case.json`; §13.2 estimates it for a harvested
  draft, and a person confirms it at admission.

### 3.2 Arms

Each arm is an adapter with one contract: start on a built task repo with a
seed, end on a done signal, stay inside the tier's wall and spend caps, and
hand back a transcript the ask detector (§5) and the cost reader can parse.

| arm | start, headless | done signal | cost unit |
|---|---|---|---|
| Crew chat | `kirocrew chat --headless` (§12) with the default agent | the CLI's own done rule (§12.3) | Kiro credits from `meteringUsage` frames (`_track_metadata` in `acp/client_usage.py`) |
| Crew conductor | `kirocrew chat --headless --mode conductor --approval-mode trust --until-done`; the agent is whatever `crew_mode.crew_mode_agent()` resolves (§12.2), never a hard-coded name | every work-ledger item terminal, slot idle | credits summed over every chat the run created |
| kiro-cli V3 | `kiro-cli chat --no-interactive --agent-engine v3 --trust-workspace --trust-tools=<list> --output-format stream-json "<seed>"` | process exit | credits from the same metadata frames |
| Claude Code | `claude -p "<seed>" --output-format stream-json --verbose --permission-mode dontAsk --allowedTools <list> --permission-prompts none --strict-mcp-config` | last line `{"type":"result"}` | `total_cost_usd` and `usage` on that line |
| Codex | `codex exec --json -C <repo> -s workspace-write -c approval_policy="never" "<seed>"` | `turn.completed` or `turn.failed`, then exit | `turn.completed.usage` tokens |

Notes the runner enforces:

- **V3 must be named.** `kiro-cli chat --help` (3.3.0): with `--no-interactive`
  the default engine is v2 unless `--trust-all-tools` is given, so the arm
  passes `--agent-engine v3`. "V3" is the KAS engine kiro-cli serves.
- **One-shot CLIs stop at their first end of turn.** A task that waits on CI
  would end early. Every arm gets the same continue budget: at most 2 harness
  messages ("Keep going until the PR is merged."), sent only when the arm ended
  with work unmerged, through `--resume-id` (kiro-cli), `--resume` (Claude
  Code), `codex exec resume` (Codex) or `kirocrew chat --headless --resume`
  (Crew). A harness message is not an ask; an agent turn that ended by asking
  is. With `--until-done` the Crew arms rarely need it, and the report counts
  how many each arm used.
- **Capability differences are reported, not equalised.** Crew wakes itself
  (monitors, cron, the ledger patrol) and the conductor runs parallel workers;
  the one-shot CLIs do not wake after exit. Claude Code, Codex and KAS have
  native subagents. Each result table carries this row.
- **Same model where the arm allows it**, same effort, recorded per run. A run
  whose lead model is not the pinned one is void.
- **Same tools.** No MCP server outside the arm's own control tools: Crew's
  `kirocrew-*` servers stay, everything else is cut; Claude Code gets
  `--strict-mcp-config` with an empty set; Codex runs with an eval
  `CODEX_HOME` whose config names none.

### 3.3 One runner for every suite and tier

```
kiro_crew.eval
  runner.py        today's EvalRunner, merge_repeats (pass^k), baseline compare  <- core, unchanged contract
  suites/
    scenarios.py   today's bundled and --dir/--file scenarios (Crew only)
    skills.py      evals/<skill>/run_evals.py --check / --run, harness from the arm
    tasks.py       build -> seed -> arm adapter -> done -> hidden check -> score; ONE code path for all three tiers
  arms/            crew_chat, crew_conductor (both call `kirocrew chat --headless`), kiro_cli_v3, claude_code, codex
  report.py        weekly report, trend file, alerts (§10)
  harvest.py       draft cases from merged PRs (§13)
```

Tiers differ only in data: the caps and k come from the tier table and the
case's own `case.json`; the `tasks` suite runs the same steps for a 5-minute
simple case and a 4-hour complex one. Every suite returns the same result
records, so pass^k, `--baseline` / `--max-drop`, `--junit` and
`write_eval_artifacts` work for all of them.

### 3.4 The CLI entry

```
kirocrew eval run    --suite tasks|scenarios|skills|all  --tier simple|medium|complex|all
                     --harness crew-chat,crew-conductor,kiro-cli-v3,claude-code,codex|all
                     [--case ID ...] [--repeat K] [--eval-home DIR] [--budget-week N]
                     [--baseline FILE --max-drop F] [--junit PATH] [--out DIR] [--detach]
kirocrew eval report [--week 2026-W47] [--out DIR]
kirocrew eval harvest --since DATE [--tier simple|medium|complex] [--pr N,N,...] [--from-ledger] --out DIR
kirocrew eval admit  DRAFT_DIR [--to held-out|public]
```

- `kirocrew eval` with no subcommand keeps today's behaviour (scenario names
  as positionals). `run`, `report`, `harvest` and `admit` become reserved
  words; none of the 5 bundled scenarios uses those names, and a user scenario
  file with one is still reachable with `--file`.
- `--repeat` defaults to the tier's k. `--suite tasks` without `--eval-home`
  refuses: task arms never run in the operator's home (§4).
- `--detach` starts the batch as a background process owned by the eval user
  and returns at once, writing `<out>/batch.json` with its id; `report`
  reads finished runs only.
- Exit: 0 all scored runs passed; 1 a regression against `--baseline`, or any
  run FAILED; 2 usage; 3 preflight refused (§10).

## 4. The isolated eval home

### 4.1 What it is

One directory per run, created by the runner and deleted after copy-out. Every
process of the run (eval gateway, CLIs, the cron that grants Trust) runs as a
**dedicated OS user** whose home is this directory and who cannot read the
operator's home (mode `0700` on the operator's home is enough). Swapping `HOME`
alone is not a fence: the Crew sandbox tiers build their masks from
`Path.home()` (`sandbox_tiers.py`), so with `HOME` pointed at the eval home
they mask paths that do not exist there, and the operator's real `~/.aws` and
`~/.ssh` stay reachable by absolute path. Where a separate user is not
possible, a mount namespace that hides the operator's home is the fallback;
the runner refuses to start when neither holds.

```
<eval root>/<run id>/
  home/           HOME for every process of the run
  crew/           KIROCREW_HOME (Crew arms)
  kiro/           KIRO_HOME (Crew and kiro-cli arms)
  codex/          CODEX_HOME (Codex arm)
  repo/           the built task repo with a local origin
  out/            transcripts, ledger, counters copied out
```

| | operator's real home | eval home |
|---|---|---|
| `~/.aws`, `~/.ssh`, `~/.config/gh`, `~/.kube` | present | absent; the runner refuses to start if any exists |
| the operator's home | the run's own home | unreadable: a separate OS user (fallback: a mount namespace hiding it); checked at start by trying to list it |
| Crew sandbox tier | `agent.sandbox: "auto"` = standard tier, which **leaves `~/.aws`, `~/.ssh` and `~/.kube` readable by shells** (the tier list in the `src/kiro_crew/sandbox.py` module docstring) | `agent.sandbox: "strict"` (`docs/architecture/security-deep-dive.md`, the `strict` tier); it masks the eval user's own credential paths, so it adds defence inside the eval home but does not hide the operator's |
| no-backend fallback | `agent.sandbox_allow_unsandboxed_exec` per platform default (`unsandboxed_exec_platform_default` in `config/loader_document.py`) | `false`; a host without a sandbox backend cannot run a Crew arm |
| model sign-in | the operator's own logins | one eval-only credential per vendor (§4.3), passed by env, never written into the repo or run output |
| GitHub | the operator's `gh` login | none; the task repo's `tools/gh` is the PR/CI stand-in and is first on PATH; any wire hit on `github.com` voids the run |
| network | open | loopback plus the model endpoints only (an allowlisting proxy), offline package indexes |
| MCP servers | the operator's | the arm's own control servers only |
| memory and lessons | the operator's | a fixed snapshot shipped with the suite (empty by default); host-specific lessons stripped |
| Trust / approvals | asked of a person | pre-granted at the least privilege below |

### 4.2 Trust and approvals, least privilege per arm

| arm | grant | why it is the least that never asks |
|---|---|---|
| Crew chat, conductor | `trust` on the ONE root slot the runner opens. Workers inherit `_trust` at `session_create` (the posture carry in `dashboard/session_control_parts/session_create.py`); per-command grants and `_trust_scope` are not inherited. Never `yolo` (process-wide; `KNOWN_APPROVAL_MODES` in `approval_mode_policy.py`) | `trust_reads` stalls on the first write a coding task needs; anything wider than one slot is not needed |
| how the grant is set without a person | a **script cron in the eval data home** opens the root slot and calls `set_session_mode(slot, "trust")` (`ScriptContext.set_session_mode` in `cron_script_context.py`). The gateway accepts only `trust`/`trust_reads`, only on slots that cron opened, and audits every call and refusal (`cron_mode_refusal` in `dashboard/handlers/_shared_route_guards.py`) | today Trust is set by a person through a minted dashboard token, and is lost at every restart because it lives in memory only (the `dashboard/chat_trust_restart.py` module docstring). The cron route is the existing audited path for automation and needs no token |
| kiro-cli V3 | `--trust-workspace` (this launch only, nothing saved) and `--trust-tools=<the case's tool list>`, never `-a` | KAS does not sandbox itself on either route (the `acp/kas_transport.py` module docstring), so the fence is the eval home plus the OS sandbox the runner wraps it in. The tool list keeps the rest asking, and an ask fails the run (§5) |
| Claude Code | `-p` (the workspace trust dialog is skipped in print mode), `--permission-mode dontAsk --allowedTools <list> --permission-prompts none` | `dontAsk` denies what is not listed instead of prompting; `bypassPermissions` is not used |
| Codex | `-s workspace-write`, `approval_policy="never"`; no `--trust`, no `--dangerously-bypass-approvals-and-sandbox` | failures return to the model instead of prompting; the project trust flag would persist past the run |

### 4.3 Sign-in without the operator's credentials

| arm | eval credential | note |
|---|---|---|
| Crew, kiro-cli | a dedicated `KIRO_API_KEY` in the eval home only. "An API key IS a kiro-cli sign-in -- kiro-cli keeps no stored record of it, the variable is the whole login" (`apply_spawn_env` in `acp/harness/kas.py`) | revocable on its own; nothing of the operator's on disk |
| Claude Code | a dedicated API key or a role scoped to model invocation only | no copy of the operator's `~/.claude*` |
| Codex | a dedicated `CODEX_API_KEY` for the invocation only (per the Codex non-interactive docs) | some installs wrap Codex with their own model credentials; that path is untested (§11) |

### 4.4 Why this is safe

1. Nothing the operator owns is reachable: the run is a different OS user who
   cannot read the operator's home, and the eval home holds no credential
   directories. The strict tier masks the eval user's own paths on top.
2. Every grant is scoped to one run: one Crew slot, one CLI launch, one
   invocation's env. A restart or the end of the run ends it.
3. The sandbox is never turned off. A host that cannot sandbox cannot run the
   Crew arms; the runner refuses rather than falling back.
4. The only credentials present are eval-only, revocable, and limited to model
   calls. A leak costs one key rotation.
5. The task repo pushes to a local origin; no real PR, push or merge is
   possible. A post-run audit greps tool inputs and the wire log for credential
   paths, the live gateway port and real GitHub hosts, and voids the run on a hit.
6. The real home's defaults do not change. This RFC grants nothing outside an
   eval home.

## 5. When a run fails

A run is **FAILED**, **PASSED** or **VOID**.

| rule | FAILED when | how it is counted |
|---|---|---|
| ask | ask count >= 1 | see the detector below; every ask is logged with time, kind and text so a person can audit it |
| wall cap | the tier's timeout (or the case's own, if lower) passes | the runner stops the arm and still runs the hidden check, so a late pass is recorded but the run is FAILED |
| spend cap | the tier's per-run cap (or the case's own, if lower): credits for Kiro arms, USD or tokens for the others | the runner reads cost while running and stops the arm at the cap; Claude Code also gets `--max-budget-usd`; the Crew CLI gets `--budget` (§12) |
| hidden check | `check.py` on origin main says `pass: false` | after one stand-in tick |
| VOID (not counted) | harness abort before the seed lands (gateway down, sign-in missing, sandbox unavailable), an unpinned model, an audit hit | VOID runs are rerun and never enter results |

Ask detector, one loader per arm:

| kind | Crew chat | Crew conductor | kiro-cli | Claude Code | Codex |
|---|---|---|---|---|---|
| question tool | `ask_question` row, any card in `GET /api/ask-question/pending` (`dashboard/handlers/ask_question.py`) | same + `work_ask_record` | n/a | `AskUserQuestion` | n/a |
| option list | a turn ending in `[OPTIONS:` | same | same | n/a | n/a |
| approval wait or denial | `pending_approval` seen by the status poll (`_pending_approval_fields` in `dashboard/session_control_parts/session_status.py`) | same, any chat of the run | `session/request_permission` frame or a refused tool | `permission_denials`, denied tool results | a command refused by the sandbox |
| needs a person | `send_notification` | `work_report` `question` / `blocked`, `reason=needs_human` | n/a | `ExitPlanMode` | `turn.failed` on approval |
| final question | the last agent turn asks the user and the check failed | same | same | same | same |

For both Crew arms the CLI's `--json` already carries the count and the events
(§12.4); the runner reads that instead of parsing transcripts itself.

**The conductor's plan gate is an ask.** The repository owner ruled on
2026-11-15 that a conductor waiting for "go" on its plan fails the run. The
seed is therefore sent as a turn nobody typed: the gateway opens it with its own
`[headless run cli:<run_id>]` marker (§12.3), which the goal-conductor skill
lists with the other nobody-typed openers, so the conductor dispatches round 1
without waiting for a go-ahead.

## 6. Before and after

- **One change only.** Two builds of Crew that differ by exactly one PR's diff
  (the after tree; the before tree is its merge base, or the after tree with
  that one PR reverted). Same suite version, model, effort, eval home snapshot,
  `agent.crew_mode_agent` (§12.2), network and caps. The runner records both
  SHAs and refuses a pair whose diff touches more than the named change.
- **Interleaved.** Run before and after on the same case side by side and
  alternate who starts first per repeat, so host load and model-side drift hit
  both.
- **Primary outcome**: pass rate. **Secondary**: wall, cost, idle after done
  and the case's gap counters. Gap counters count mechanism hits and are far
  less noisy than wall, so small effects are read from them.

How many runs, from the spread the out-of-tree bench measured (standard
deviation of log wall over 3 runs per task): 0.17-0.19 on its 60-85 min task,
0.11-0.58 on the reworked short tasks, 0.8-1.45 on tasks dominated by one long
wait; log credits 0.03-0.81. Runs per side for 80% power at alpha 0.05 are
`n ~= 15.7 * sigma^2 / ln(ratio)^2`:

| true wall ratio | sigma 0.25 | sigma 0.5 |
|---|---|---|
| 2x | 3 | 9 |
| 1.5x | 6 | 24 |
| 1.25x | 20 | 79 |

For pass rate, 3/3 vs 0/3 gives Fisher p 0.10 and 5/5 vs 0/5 gives 0.008.

Per tier, for a before/after pair: **simple 5 runs per case per side** (cheap,
and their spread, 0.11-0.58, is the widest of the non-wait tasks); **medium 5**; **complex 5**. Pooled
over 3 cases that is 15 per side, enough for about a 1.3x wall gap at sigma
0.25 and an all-vs-none pass gap per case. The weekly run uses the lower k of
§3.1, which is enough to see a pass-rate drop, not a small wall change.

## 7. Cases: format, location, publishing

### 7.1 Format

```
evals/tasks/
  README.md          how to run, how to add a case, the publish checklist, the canary
  SPEC.md            case.json and check.py contract
  lib/               PR/CI stand-in, build, scan.py (scrub), blind.py (§8)
  cases/<tier>/<ID>-<slug>/          tier = simple | medium | complex
    case.json        id, version, kind (code | docs), tier, seed ({repo} placeholder), rounds,
                     timeout_minutes, spend cap, expected wall, gap counters, check parts,
                     provenance (source PRs, issue or RFC, base SHA, merge dates), canary
    CASE.md          public provenance, traps, why it is hard
    build.sh         builds a fresh repo at the base with a local origin, fixed fake git identity
    standin.json     CI lanes and timings, flakes, rate-limit windows, review rules, main-moving events
    gates/           CI gate scripts (kept outside the built repo tree)
    hidden/check.py  judges origin main; prints {"pass", "parts", "counters", "notes"}
    hidden/mutants/  small bugs in the reference, to score the agent's own tests (§8)
    probes/          wrong-path drivers that prove each trap bites
    ref/             the reference solution (for a harvested case: the merged PRs' diffs)
  grade/             objective.py, judge prompt, rubrics
  results/           dated aggregate tables only
```

The `skills` and `scenarios` suites keep their own directories
(`evals/<skill>/`, `src/kiro_crew/eval/scenarios/`) and are listed by the same
runner.

Two case sources (§13): cases harvested from merged PRs, which rebuild this
repository at the first PR's base; and synthetic repos rebuilt from a goal's
miss (the out-of-tree bench's TR shape). A case is kept only if the current
build fails at least 1 of 3 runs or a gap counter fires, except in the simple
tier, where a case every build passes is a regression floor. Any edit bumps
`version`. Runs build into a directory outside any checkout, and the arm's
sandbox denies the suite path, so an agent cannot read `hidden/`; the post-run
audit voids a run whose transcript touches the suite path.

### 7.2 Public, and a held-out set

This repository is public. Two splits:

- **public**: in `evals/tasks/cases/`.
- **held-out**: at least 2 cases kept outside the repository by the owner. A
  change tuned on public cases is kept only if held-out does not regress. A
  held-out case is published only when it is retired from held-out.

### 7.3 Scrub before publishing (agent)

1. Copy the case into a clean staging dir; drop mining notes, caches and any
   run output.
2. Replace the source record (chat ids, message times, quotes) with public PR
   numbers and the month; keep the rest in a private provenance file.
3. Paraphrase any of the owner's or a reviewer's words in English; no names.
4. Run `scan.py`: home paths, tokens and keys, cloud account ids and ARNs,
   emails outside `example.invalid`, high-entropy strings, plus a private list
   of names, hosts and tool names loaded from env (publishing the list would
   publish the names). Scan a fresh `build.sh` output and its `git log` too.
5. Open the PR from a base-repo branch so the repository's internal-content
   scan runs with credentials (it fails on fork PRs by design).
6. Write `scrub-report.md` into the PR: every file, every replaced string,
   scanner output, the git identity check.

### 7.4 Owner review before publishing

The owner reviews each `CASE.md` and seed, the scrub report line by line, the
PR's full file list, and confirms that every linked PR is public and that no
case comes from a chat holding customer or third-party data. A maintainer
reviews as usual. No case PR opens for review before the owner's sign-off.

## 8. Grading code when several arms pass

Three layers. Process numbers (wall, cost, CI runs, asks, gap counters) stay in
their own table and never mix into code quality.

| layer | what | decides |
|---|---|---|
| 0. gate | hidden check on origin main; pass^k over the runs | who passed |
| 1. objective | one script on `base..origin/main`, same for every arm: src lines added and deleted, out-of-scope files, test strength (how many `hidden/mutants/` the agent's own tests catch), agent tests that fail on base and pass on main, lint and type delta, complexity delta, duplication, a timed probe where the case has a perf axis | measured strengths and weaknesses |
| 2. blinded pairwise judge | per pair of passing runs: the seed, a base summary, the required behaviour in words, each side's squashed diff and PR text. No reference solution, no arm names, no counters | preference and per-axis scores |

Blinding for layer 2, by script, same for both sides: squash to one diff; drop
commit metadata and trailers; drop harness files (`.kiro/`, `.claude/`,
`CLAUDE.md`, `AGENTS.md`, ledger and report files); replace harness words
(Kiro, Crew, Claude, Codex, conductor, worker, subagent, ledger, session ids)
with neutral tokens. A leak probe asks a separate judge "which tool made A?" on
20 pairs; above 65% right, the blinding leaks and is fixed first.

The judge scores correctness beyond the checks, scope fit, fit with existing
design, readability, test quality and PR text accuracy (1-5, written anchors,
file:line for every score below 3), then A / B / tie. Each pair runs in both
orders; a win counts only when both orders agree. Two judge models; their
agreement is reported. The owner grades 10 pairs blind once; the judge setup is
kept only if it agrees on at least 7.

## 9. Non-code tasks

A docs case has the same directory, stand-in and layers; only `hidden/` differs:

| check | deterministic? |
|---|---|
| required facts present (each with accepted normal forms) | yes |
| planted stale facts absent (an old doc in the repo carries wrong values) | yes |
| relative links and anchors resolve; external links on the case allowlist | yes |
| the same CI style lanes as code (typography, headings, length) | yes |
| reader quiz: a fixed reader model sees only the doc and answers exact questions; script-graded; the same quiz without the doc must fail | model, graded by script |
| do-it check: a script follows the doc's commands against an offline stub and must reach the end state | yes |

`kind: "docs"` in `case.json` selects these checks and the docs rubric in layer
2 (task success for a new reader, accuracy, step order, brevity, locale
quality). The first docs case is short on purpose (30-60 min), to prove the
checks before longer ones.

## 10. Running it every week

```
Saturday 02:00, harvest home, zero-token cron (--command)
  kirocrew eval harvest --since 7d --out <private>/drafts          # §13: drafts only, never admitted unattended
Saturday 03:00, eval user, zero-token cron (--command)
  kirocrew eval run --suite all --tier all --harness all \
      --eval-home <eval root> --budget-week <N> \
      --baseline <last week's eval_<ts>.json> --max-drop 0 --out <results> --detach
Monday 02:00, eval user
  kirocrew eval report --out <results>                              # weekly report + trend + alerts
```

Harvest needs GitHub; the eval home (§4.1) allows only loopback and the model
endpoints and voids a run on any `github.com` hit. So harvest runs in its own
**harvest home**: a third OS user (or the operator's account) with read-only
access to public GitHub data and no model credentials, writing drafts to a
private directory the eval user can read. Nothing it fetches runs: a draft
only reaches the eval home after `kirocrew eval admit` (§13.3).

The run and report schedules are two `kirocrew cron add` jobs in the eval data home, both
`--command` (no model, sandboxed), `--no-persistent-session`, and a
`--timeout-secs` that only has to cover the `--detach` start, since a complex
batch runs longer than one cron wake may last (86400 s). Preflight inside
`eval run` refuses before any run starts when: an eval credential is missing
or expired, no sandbox backend exists, the eval OS user check of §4.1 fails,
host load is over the limit, or the week's spend would pass `--budget-week`.
The plan fills the budget in order: anchor cases (one per tier, never
rotated, for the trend), then rotating cases, then stops scheduling.

**Weekly report** (`<results>/weekly-<YYYY-Www>.md`, aggregate tables
published to `evals/tasks/results/` by PR):

| table | rows x columns | cells |
|---|---|---|
| outcome | harness x tier | pass^k; runs with >= 1 ask (each is a FAIL); timeouts; spend-cap stops; VOID count |
| effort | harness x tier | median and range of wall, cost (credits or USD or tokens), harness continue messages used |
| idle after done | harness x tier | median of `end of run - first time the hidden check passed`; the runner samples the read-only check on the stand-in's origin main every 60 s |
| code | per case where 2+ arms passed | §8 layer 1 measures and layer 2 wins / ties / losses |
| capability | harness | wakes, parallel workers, subagents, MCP servers, model, resolved conductor agent (§12.2) |
| harvest | week | drafts proposed, admitted to held-out, rejected (with the failing gate), published, retired (§13.5) |

**Trend file**: `<results>/trend.jsonl`, one line per (week, harness, tier,
suite) with the outcome and effort numbers, plus a regenerated `trend.md`
showing the last 8 weeks.

**Alerts** (a flag for a person to look, not a verdict): any simple-tier case
that passed last week fails; pass rate of a tier's anchor case drops against
its 4-week baseline; median wall above 1.5x baseline; a gap counter worse in
both runs; any ask on an arm that had none; any audit hit (always). The
`--baseline` / `--max-drop` exit of `kirocrew eval` is the same signal for a
single run.

## 11. Not verified

| point | state on 2026-11-15 |
|---|---|
| Codex sign-in in an eval home | untested. On the reference host Codex is wrapped by an installer that supplies model credentials itself; whether that works with an eval `CODEX_HOME`, or a `CODEX_API_KEY` is needed, is not known |
| Codex sandbox on Linux | untested on the host. Probed only from inside another sandbox, where its bubblewrap failed on nested namespaces |
| kiro-cli sign-in | the reference host's kiro-cli is signed out; no kiro-cli or Crew arm can run until an eval `KIRO_API_KEY` exists. Whether the account type can mint one is not known |
| Claude Code OS sandbox | `bwrap` is not installed on the reference host, so Claude Code's own Bash sandbox is unavailable there; the eval home is its only fence |
| dedicated eval OS user | not set up on the reference host; the gateway, the kiro-cli relay and the sandbox probe have not been run as a second user |
| cron route to Trust | read in code (§4.2), never exercised by a bench; creating a script cron in the eval home is itself a guarded step |
| kiro-cli and Codex stream frames | event names for asks and denials are from `--help` and vendor docs, not from a captured run |
| #37609 | open; until it merges the conductor arm still gates its plan |
| Crew headless CLI | `kirocrew chat --headless` is built; sign-in and sandbox failures are read from the reply text, not a typed code; starting a gateway, `--on-ask`, `--effort` and `--resume` are later |
| harvested cases on this repository | building this repository at an old base offline, and running only the PRs' own tests there, is not yet tried; nor is the seed-blindness probe (§13.3) |

## 12. Crew headless CLI

Claude Code and Codex are compared through their own CLIs. For a fair
comparison Crew gets one too, and both Crew arms of §3.2 use it.

### 12.1 Extend `kirocrew chat`, not a new `kirocrew run`

`kirocrew chat -m` already sends one message with `--agent` and `--model`
(`register_chat_parser` in `cli_parser_agent.py`), and resolves the agent the
way a dashboard chat does (`_resolve_cli_agent` in `cli_chat.py`). What it
lacks: a tool prompt waits on the terminal for `y` / `n` / `o`, it returns
after one turn, and it has no structured result. `kirocrew run` is already the
`TASK.md` task runner (`register_run_parser`), so a new command of that name
would break it. Extending `chat` keeps one agent-resolution path for
interactive and headless use. A `--headless` flag turns the new behaviour on;
without it, `kirocrew chat` is unchanged.

### 12.2 Flags

| flag | meaning |
|---|---|
| `-m TEXT` / `--message-file PATH` | the seed (existing `-m`; a file for long seeds, read through the sensitive-path gate). A seed that opens with `/` or `@` is refused (exit 2; the gateway answers 400 `unattended_command_seed`): the marker opens the turn, so a command or saved prompt would not run |
| `--headless` | never read the terminal. A pending tool approval, an `ask_question` card or a turn ending in `[OPTIONS:` is an ask: the run stops (exit 3) |
| `--on-ask stop\|continue` | later; today every ask stops the run |
| `--mode chat\|conductor` | default `chat`. `conductor` turns conductor mode on for the session the way the dashboard does: the agent is `crew_mode.crew_mode_agent(cfg)` (from `agent.crew_mode_agent`, `config/sections_agent.py`; unset means `DEFAULT_CREW_MODE_AGENT` in `crew_mode.py`, today `kirocrew-team-lead`; a configured name the migration found unusable reads as the conductor last applied, `set_unusable`), and the session is stamped with `stamp_for_switch` so `runs_crew_mode` holds. No agent name is hard-coded anywhere; `--agent` is refused with `--mode conductor` so a conductor template cannot be passed off as one |
| `--agent NAME` | chat mode only (existing) |
| `--model ID` | as a dashboard chat (`--effort`: later) |
| `--approval-mode trust` | the one choice: Trust on the chat this run opens, audited as a mode change; workers it creates inherit it at `session_create`. Unset: an approval prompt is an ask |
| `--until-done` | keep the session through ledger patrol, autonudge and monitor wakes until done (§12.3) or a cap |
| `--idle-grace SECS` | with `--until-done`, the quiet window that counts as done (default 900) |
| `--cwd DIR` | the session's project directory: set on the run's chat (`POST /api/chat/slots/{slot}/project`; absolute, existing), so no folder is left behind |
| `--timeout SECS` | wall cap; the run's chats are stopped (exit 4) |
| `--budget CREDITS` | spend cap over settled usage: the run's chats' `turn_stats.credits` plus the credits of finished background agents they started (`GET /api/spawn`); a turn or agent counts when it ends, so a run can pass the cap by what is still running; the run's chats are stopped (exit 5) |
| `--resume SESSION` | later |
| `--json` | one JSON object on stdout at exit (§12.4); human text goes to stderr |

### 12.3 How it runs

- The CLI drives the running gateway of the current data home as its owner,
  in both modes, with requests the dashboard owner already makes: a one-minute
  owner sign-in over the peer-verified local transport, one new chat slot (in
  conductor mode on the agent `crew_mode_agent` resolves, which slot create
  stamps as a Crew Mode chat), `--approval-mode trust` on that slot only
  (`POST /api/chat/mode`, audited as a mode change), the seed, then polling.
  Gateway change: only the headless marker below. No reachable gateway is exit
  8; starting one is later.
- **The headless marker.** The seed is sent with `"unattended": true` and a
  `run_id`. Only for that, and only from the dashboard owner, the gateway opens
  the turn with its own marker `[headless run cli:<run_id>]`
  (`dashboard/headless_marker.py`) and writes a `chat_headless_run`
  audit row with the run id and slot. The goal-conductor skill lists
  `[headless run` with the other nobody-typed openers (`[cron notification`,
  `[work-ledger wake]`), so a conductor dispatches round 1 instead of waiting
  at its plan gate (§5). Boundaries: an app, an internal caller or a non-owner
  asking for it gets 403 `unattended_not_owner`; a malformed `run_id` gets 400;
  and every caller's own text, the owner's included, has a marker-shaped
  opener defanged to `[quoted: headless run` (the rule a forged
  `[sent by session` already gets), so typing the prefix skips nothing. A
  person's ordinary turn is unchanged and still waits at the gate.
- **Done**: without `--until-done`, when the first turn ends. With it,
  conductor mode ends when every work-ledger item of the slot is terminal and
  the run is idle; otherwise after `--idle-grace` quiet seconds. A wake that
  makes the root or a worker busy again resets the quiet window.
- **In the eval home**: the runner starts the gateway and the CLI with `HOME`,
  `KIROCREW_HOME` and `KIRO_HOME` set to the run's directories (§4.1, §4.3);
  the eval home config pins `agent.crew_mode_agent`, and the CLI records what
  it resolved, so a before/after pair whose resolved agents differ is refused.

### 12.4 `--json`

```json
{
  "status": "done | asked | timeout | budget | not_signed_in | sandbox_refused | no_gateway | incomplete | interrupted | error",
  "exit_code": 0, "completed": true,
  "asks": 0, "ask_events": [{"kind": "approval | question | options", "slot": "...", "text": "...", "t_s": "..."}],
  "wall_s": 0, "credits": 0.0,
  "mode": "chat | conductor",
  "agent": {"name": "...", "source": "default | config | fallback | flag", "configured": "..."},
  "approval_mode": "trust | none", "slot": "...", "transcript": "<path to the session jsonl>",
  "items": {"total": 0, "terminal": 0, "open": 0}, "cwd": "...", "error": "",
  "unclosed": []
}
```

`unclosed` names any run chat the gateway refused to close at exit (for example a
history write still pending); its automation, turn and background agents
(`POST /api/spawn/stop-all`) were stopped first.

`agent.source` is `default` when `agent.crew_mode_agent` is unset, `config`
when the configured name was used, `fallback` when `set_unusable` replaced it
(then `configured` names what was asked for).

### 12.5 Exit codes

| code | meaning |
|---|---|
| 0 | done: completed, no ask |
| 1 | other error (gateway or runtime failure, unreadable result) |
| 2 | usage error |
| 3 | asked a person; the run stopped at the first ask |
| 4 | `--timeout` reached |
| 5 | `--budget` reached |
| 6 | not signed in |
| 7 | sandbox unavailable or refused (no backend and unsandboxed exec not allowed) |
| 8 | no reachable gateway for this data home |
| 9 | incomplete: a conductor went quiet for `--idle-grace` with ledger items still open |
| 130 | interrupted |

A run refuses to start (exit 1, no chat opened) while `work_ledger.remote_dispatch` is on: a worker placed on a remote crew can only be reached through the conductor's own session tools, so the CLI could not stop it on a timeout or budget exit.

The eval runner maps 0 to "check next", 3/4/5/9 to FAILED, 6/7/8 to VOID.

## 13. Case collection: merged PRs become standing cases

This is the step the owner called the most important: a steady way to turn
delivered work into standing cases, a little at a time.

### 13.1 Source

Merged PRs in `kirodotdev/KiroCrew`. They are already public, so the source
adds no leak beyond what the repository already holds. `kirocrew eval
harvest` groups them by goal:

| group key | example | becomes |
|---|---|---|
| a tracking issue (`Fixes #N`, `Part of #N`, linked issues) | several PRs closing one issue | one case; complex when 2+ rounds or 4+ PRs |
| an RFC's `implementation-prs` front matter | the PRs of one RFC phase | one case per phase |
| a single PR fixing one bug | a `fix:` PR with its own test | simple or medium |

Optional, **off by default**: `--from-ledger` mines finished goals from a
work ledger (`ConductorRecord.goal` and `WorkItem` in `work_ledger_records.py`)
or chats. Those are private: if turned on, §7.3 and §7.4 apply as written and
the owner reviews before a draft is used.

### 13.2 Draft

| part | taken from | never from |
|---|---|---|
| seed | the requirement text written BEFORE the work: the issue body as it stood before the first PR opened (issue timeline), or the RFC sections at the commit that merged the doc PR | PR diffs, PR bodies, commit messages, review comments: they hold the answer |
| base | the first PR's base: the merge base of its first commit with `main` when it opened | |
| reference (`ref/`) | the merged PRs' diffs, replayed in order through the stand-in | |
| hidden check | the tests these PRs added (fail on base, pass after), the existing tests they touched (must still pass), plus behaviour checks written from what was delivered (CLI output, API responses) | the full repository suite (not runnable offline) |
| stand-in | the PRs' real CI: per-lane durations from their check runs, failures and reruns (flaky lanes, rate-limit windows), time to first review, review rounds, merge delay | invented timings |
| tier | estimated from PR count, review rounds, open-to-merge hours and lines changed (1 PR, <= 100 lines, 1 CI round: simple; up to 3 PRs or 1 day: medium; else complex) | |

`kirocrew eval harvest --since <date> [--tier ...] [--pr N,...]` writes one
draft directory per group, never into `evals/tasks/cases/`.

### 13.3 Gates before a draft becomes a case (`kirocrew eval admit`)

1. **Bites**: the hidden check fails on the untouched base.
2. **Solvable**: the reference (the merged code) passes it through the
   stand-in.
3. **Blind**: the check cannot be read off the seed or the base README. A
   script greps the seed for the hidden tests' names and literals, and a probe
   gives a model only the seed and README and asks what the tests check;
   overlap above a threshold rejects the draft.
4. **New**: not a duplicate of an existing case (same source PRs, the same
   files touched, or a near-identical seed).
5. **Reviewed**: a maintainer reviews the draft (and the owner, for a
   `--from-ledger` draft, per §7.4).

A draft that fails a gate is kept with the gate's reason, for the weekly
report.

### 13.4 Training-data risk

A model may have trained on merged code. Mitigations, as SWE-bench-Live and
SWE-rebench do for fresh issues:

- Prefer PRs merged after the evaluated model's training cutoff. Each case
  records its merge dates; each run records the model and its cutoff; the
  report shows pass rates separately for cases merged before and after it.
- The newest batch is held out: it is used privately first and published only
  after it has been used (§13.5).
- Rotate cases on a fixed schedule.

### 13.5 Lifecycle

```
draft -> held-out (private, used in weekly runs) -> public (evals/tasks/cases/) -> retired
```

- A held-out case is published after 4 weekly runs, or earlier when newer
  held-out cases replace it.
- Retire a case when every harness passes it k/k for 4 weeks in a row and no
  gap counter fires (simple-tier anchors are kept as the regression floor).
- Keep a target mix of active cases per tier, starting at simple 40%, medium
  35%, complex 25%; the weekly harvest picks groups for the tier that is
  furthest below its share.

### 13.6 Weekly rhythm

Each Saturday run first harvests the week's merged PRs (§10). The weekly
report's harvest row says how many drafts were proposed, admitted to
held-out, rejected and why, published and retired.

## 14. Phases

1. `kirocrew eval run | report` over the existing `scenarios` and `skills`
   suites, on the shared runner core (§3.3). No new arms yet.
2. Crew headless CLI (§12).
3. `tasks` suite, simple tier, eval home (§4), both Crew arms.
4. `harvest` and `admit` (§13) on single-PR fixes.
5. Claude Code, kiro-cli V3 and Codex arms, after each sign-in in §11 is
   proven.
6. Medium and complex tiers, grading layers 1 and 2 (§8), blinding and its
   leak probe.
7. Docs cases (§9).
8. Weekly schedule, report, trend, alerts, spend cap (§10).

Each phase is its own PR against this document.

### 14.1 Built: `kirocrew eval run` v1

A slice of phases 3, 5 and 6: the `tasks` suite (`src/kiro_crew/eval/tasks.py`)
with the Crew chat, Crew conductor and Claude Code arms
(`src/kiro_crew/eval/arms.py`), one simple and one medium case under
`evals/tasks/cases/`, and `--check` (each case fails on its base and passes on
its reference, no model) as the CI-side gate. kiro-cli V3 and Codex are
registered and every run of them is VOID with "not available". Where v1 does
less than, or differs from, the sections above:

| point | v1 |
|---|---|
| flags (§3.4) | `--suite tasks\|DIR --tier --harness --case --repeat --eval-home --out --model --json --check`. `--suite` also takes a suite directory. No `scenarios` / `skills` suites, `--baseline`, `--junit`, `--detach`, `--budget-week`; no `report`, `harvest`, `admit` |
| eval user (§4.1, §4.4) | refused unless the running user's home holds none of the sandbox's credential paths (`security.paths.sensitive_home_dirs` plus `~/.config/gh` and `~/.kube`; only an empty directory under Crew's own data home, which running the CLI may create, is let through) and the user cannot list any other login user's home. With `HOME` set to the run home, the strict tier masks the empty run home, not the eval user's real home, so for the unsandboxed paths (the hidden check, the Claude Code arm) this credential-free user is the whole fence, which is less than §4.4 item 1 says. No mount-namespace fallback. Linux and macOS only |
| network, MCP, memory (§4.1) | no allowlisting proxy. A fresh data home and `HOME` per run, so no user MCP server or memory is present; Claude Code gets `--strict-mcp-config` and no config |
| task branch (§7.1) | `trunk`, not `main`: Kiro Crew refuses a push to a protected branch name even to a local origin, so a Crew arm could not finish a case on `main` |
| cases (§7.1, §13.2) | a case's `base/` is the touched module cut down to the code the fix needs, not the whole repository at the PR's base, so it builds offline in a second; `CASE.md` names what was cut. No stand-in, `gates/`, `mutants/`, `probes/`, scrub report |
| ask detector (§5) | Crew arms: the headless CLI's own `asks`. Claude Code: `permission_denials` and `AskUserQuestion` / `ExitPlanMode` calls. No final-question rule yet |
| audit (§4.4) | VOID when a run's output names the case suite's path. The suite (cases, checks, `lib/`) is fingerprinted at batch start and after every run; a run that changed it is VOID and the batch stops, so no run can rewrite the grader later runs are judged by. No wire log, so no credential-path, gateway-port or `github.com` scan. The hidden check imports the submitted code in its own child process, as the eval user and outside the Crew sandbox, so code written to print a fake verdict, or to edit its own run's files while it is checked, could game its own score (and only that, given the fingerprint); v1 relies on the dedicated eval user, not on the check, to contain the agent, and a sandboxed check is a follow-up |
| Claude Code fence (§4.2, §11) | as designed: the eval home and the dedicated OS user are its fence; the runner does not wrap it in Crew's sandbox |
| continue budget (§3.2) | none: one harness message (the seed) per run |
| model pin (§3.2) | `--model` is passed to every arm and recorded; a run is not voided for an unpinned model |
| idle after done (§10) | the hidden check is sampled every 60 s while the arm runs |
| Crew gateway (§12.3) | the runner starts one per run in the run's data home (`--port auto --no-open --no-tunnel`; the port from its run marker, ready when `/api/health` answers), and stops it, and the arm's process group, only after the final hidden check, so a server the agent started is still up while it is judged |

## 15. Alternatives considered

| alternative | why not |
|---|---|
| keep running arms with the operator's real HOME | exposes `~/.aws` and `~/.ssh` to shells under the default tier; one bad tool call leaks real credentials |
| answer asks automatically (a "decide yourself" reply) and count them | hides the thing the owner wants measured; asks become free |
| a person grants Trust once per batch | every gateway restart drops it, so unattended batches stall |
| drive the Crew arms through the gateway HTTP API from the runner (what the out-of-tree bench did) | Claude Code and Codex run through their CLIs; a Crew CLI gives the same shape and anyone can rerun it |
| a new `kirocrew run` for headless runs | the name is the existing `TASK.md` runner |
| run conductor mode as `--agent kirocrew-team-lead` | conductor mode's agent is `agent.crew_mode_agent`, which users change and which can fall back; a hard-coded name measures the wrong agent and is not reproducible |
| mine cases from private chats by default | needs scrubbing and owner review per case; merged PRs are public and carry their own tests |
| seed from PR bodies or diffs | leaks the answer |
| a separate runner per tier | three code paths drift; one runner with tier data keeps pass^k, baselines and reports the same |
| Docker for every arm | Crew needs its gateway and real wall-time CI waits; Docker stays an optional portable build of the task repo |
| LLM judge as the pass gate | non-deterministic; the gate stays a script, the judge only ranks passed runs |

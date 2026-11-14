# Kiro Crew Eval Harness

Multi-session evaluation harness for benchmarking Kiro Crew's cross-session memory, lesson application, and context accumulation.

## Quick Start

```bash
kirocrew eval
```

## Usage

```bash
# Default — smoke test only (~30s)
kirocrew eval

# Run specific scenarios by name (without .json)
kirocrew eval memory_recall_basic
kirocrew eval memory_recall_basic lesson_application

# Run all scenarios
kirocrew eval --all

# Enable LLM-judge assertions
kirocrew eval --judge my_scenario
```

## Your own scenarios

```bash
# Every .json/.yaml/.yml scenario file in a folder (its own files, not subfolders)
kirocrew eval --dir ./evals

# One scenario file
kirocrew eval --file ./evals/support_bot.yaml

# Choose what answers: a crew member or agent, and a model
kirocrew eval --dir ./evals --agent reviewer --model my-model-id

# Run the set 3 times; a scenario passes only if all 3 runs pass (pass^k)
kirocrew eval --dir ./evals --repeat 3
```

`--dir` and `--file` replace the bundled scenarios, so they cannot be combined with
scenario names or `--all`. The scenario file format is the one the bundled files use
(`name`, `sessions`, `turns`, `assertions`). A file that does not load is named and
nothing runs; so is a name two files share.

`--agent` takes a crew member or an agent name, resolved as `kirocrew chat --agent`
resolves it. `--model` is the explicit pick for every scenario session and outranks
the agent's own model. Both apply to the sessions the scenario drives; the memory
consolidation between sessions keeps its default agent and model.

With `--repeat N` each scenario runs N times in a row. The report shows the runs
passed (`Runs passed: 2/3`), the scenario counts as passed only when every run did,
and the JSON summary adds `runs` and `runs_passed` per scenario and a top-level
`repeat`. The JSON also records `agent` and `model` when you gave them. Without the
new flags the command and its files are unchanged.

`--no-jail` (the shared top-level CLI flag) applies here as it does to every
`kirocrew` command.

The command exits 0 only when every scenario passes. It exits 1 when any
scenario fails (the report is still written first) or when a scenario name is
unknown (nothing runs), so `kirocrew eval && next` stops on a failure.

## Available Scenarios

| Name | Turns | Sessions | Dimensions | Time est. |
|------|-------|----------|------------|-----------|
| `smoke_test` | 2 | 2 | memory_recall | ~30s |
| `memory_recall_basic` | 4 | 2 | memory_recall | ~1 min |
| `lesson_application` | 2 | 2 | lesson_application | ~30s |
| `context_accumulation` | 3 | 3 | context_accumulation, memory_recall | ~2 min |
| `subagent_policy` | 1 | 1 | delegation_value, delegation_lifecycle | provider-dependent |

## Delegation decisions

`kirocrew eval subagent_policy` exercises eighteen hypothetical decisions with
the existing read-only harness. It covers direct work, parent plus one child,
independent fan-out, dependencies, legitimate and invented reasons for a single child,
bounded parent work, terminal failures, cancellation, explicit user choices,
blocking-tool limits and conflicting writers. No spawn tools are enabled by
this scenario. Its response assertions test **planned decisions**, not actual
child execution, task completion, or a speed/cost improvement.

For a before/after comparison, use the same case text, provider/model and tool
availability with each revision's orchestration prompt, repeat key cases, and
retain raw responses. Record actual and planned agent counts separately, parent
work, wait reasons, wall latency and provider-reported usage. Missing token
counts are unknown. Pair the decision traces with deterministic
busy-parent delivery and delayed-startup-memory tests.

## Output

Results print to stdout and save to `eval_results/` under the current working directory:
- `eval_<timestamp>.md` — full markdown report
- `eval_<timestamp>.json` — structured JSON for programmatic comparison

### Example Output

```
Running: smoke_test (2 turns)

❌ smoke_test — 1/2 assertions

# Eval Results

**0/1 scenarios passed**

## Scorecard by Dimension

| Dimension | Passed | Total | Rate |
|-----------|--------|-------|------|
| memory_recall | 1 | 2 | 50% |

## ❌ smoke_test
_Minimal 2-session smoke test. Teach one fact, recall it next session._
Assertions: 1/2 | Time: 30.2s
Dimensions: memory_recall

### ✅ Session 1: teach
  ✅ Turn 1: `Remember: my favorite fruit is mango.`
     ✅ contains: `mango`

### ❌ Session 2: recall
  ❌ Turn 1: `What is my favorite fruit?`
     ❌ contains: `mango`
     Response: I don't have any information about your favorite fruit.
              I don't have access to personal preferences unless you've
              shared them with me in this conversation. What is it?

## Dimension Summary
  ❌ memory_recall: 1/2 (50%)

Overall: 0/1 scenarios passed

Results saved to:
  eval_results/eval_20260418_221152.md
  eval_results/eval_20260418_221152.json
```

### Example JSON

```json
{
  "timestamp": "20260418_221152",
  "scenarios": [
    {
      "name": "smoke_test",
      "passed": false,
      "assertions": "1/2",
      "sessions": 2,
      "elapsed_secs": 30.15,
      "dimensions": ["memory_recall"]
    }
  ],
  "dimensions": {
    "memory_recall": {
      "total": 2,
      "passed": 1,
      "rate": 0.5
    }
  },
  "overall_passed": 0,
  "overall_total": 1
}
```

## Writing Scenarios

Scenarios are JSON files in `src/kiro_crew/eval/scenarios/`. Each defines sessions with turns and assertions (YAML is also supported for backward compatibility):

```json
{
  "name": "my_scenario",
  "description": "What this tests.",
  "dimensions": ["memory_recall"],
  "judge_criteria": "Recalls the user's stated preference without being told again.",
  "seed": {
    "preferences": "- Prefers dark mode",
    "projects": "Working on Starfish cache",
    "lessons": ["always use 2-space indent"]
  },
  "sessions": [
    {
      "name": "teach",
      "turns": [
        {
          "user": "My favorite language is Rust.",
          "assertions": [
            {"type": "contains", "value": "rust"}
          ]
        }
      ]
    },
    {
      "name": "recall",
      "turns": [
        {
          "user": "What is my favorite language?",
          "assertions": [
            {"type": "contains", "value": "rust"},
            {"type": "judge", "value": "Names Rust as the favorite language."}
          ]
        }
      ]
    }
  ]
}
```

### Assertion Types

| Type | Behavior |
|------|----------|
| `contains` | Response contains value (case-insensitive) |
| `not_contains` | Response does not contain value |
| `regex` | Response matches regex pattern |
| `equals` | Response equals value exactly (trimmed) |
| `judge` | Runs only with `--judge`; otherwise it is not scored. A separate LLM judge scores the response 1–5 against the assertion's `value`, else the scenario's top-level `judge_criteria`, else its `description`; it passes at a score of 3 or more (`LLMJudge` `pass_threshold` 3.0, no CLI override). An unparseable judge reply scores 0 and fails |

String-matching assertions are case-insensitive by default. Add `case_sensitive: true` to override.

### Tool-use assertions

Three more types score the tools a turn called instead of its reply text, so a
scenario can tell an agent that looked something up from one that guessed right:

| Type | Behavior |
|------|----------|
| `tool_called` | The turn called the tool named in `value`. Optional `args` (a mapping) narrows it to a call that also carries those arguments |
| `tool_not_called` | The turn made no call to the tool named in `value`; with `args`, no call to it with those arguments |
| `tool_order` | `tools` (a list of names) were called in that order, other calls allowed between them; `exact: true` allows no other call at all |

```yaml
assertions:
  - type: tool_called
    value: read_file
    args: {path: config.yaml}
  - type: tool_not_called
    value: write_file
  - type: tool_order
    tools: [grep, read_file]
```

A tool is named as it is in the agent's tool list: `read_file`, or `server/tool`
(`@server/tool` also works) for a tool an MCP server provides. A string in `args`
matches when the argument contains it (case-insensitive unless `case_sensitive: true`),
so `path: config.yaml` matches `/work/config.yaml`; any other value must equal the
argument. Only top-level arguments are compared.

The calls counted are the ones the agent made, whether or not the harness ran them.
The harness still refuses every tool that is not read-only (see Tool Safety), so a
`tool_not_called` on a write tool checks that the agent did not try one. A scenario
file that uses these types must give each a name or `tools` list; one that does not
is refused by name, as an unknown type is, and nothing runs.

## Tool Safety

During eval, tool approval uses a name-based allowlist (`_SAFE_TOOL_EXACT` for exact matches, plus `_SAFE_TOOL_PREFIXES_FS` and `_SAFE_TOOL_PREFIXES_API` for prefix matches). Filesystem-prefix tools must expose a path and pass `is_sensitive_path()`; exact and read-only API entries do not take the filesystem path branch. All other tools are rejected. The permission gate (`refusal_for`) runs before the allowlist, and every rejection is preceded by an in-band `[Kiro Crew host notice]` deny steer (`_steer_host_deny`, see [injected messages](../../../docs/system-specs/common/injected-messages.md#turn-recovery-continuations)) so the model knows the host refused the call. The judge session refuses every tool. This keeps eval runs side-effect-free while allowing the agent to use read-only tools.

## Architecture

```
kirocrew eval            CLI entry point — scenario selection, output
  └─ EvalRunner          Runs scenarios with fresh state per scenario
       └─ Session        Each session gets its own LLM provider (simulates restart)
            └─ Turn      Send message → collect response → check assertions
```

Key design: each session creates a **new provider instance**, simulating a user closing and reopening Kiro Crew. Cross-session context must come from persisted memory, not conversation history.

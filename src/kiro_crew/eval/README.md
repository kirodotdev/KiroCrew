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

`--no-jail` (the shared top-level CLI flag) applies here as it does to every
`kirocrew` command.

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

# KiroCrew Eval Harness

Multi-session evaluation harness for benchmarking KiroCrew's cross-session memory, lesson application, and context accumulation.

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
```

## Available Scenarios

| Name | Turns | Sessions | Dimensions | Time est. |
|------|-------|----------|------------|-----------|
| `smoke_test` | 2 | 2 | memory_recall | ~30s |
| `memory_recall_basic` | 4 | 2 | memory_recall | ~1 min |
| `lesson_application` | 2 | 2 | lesson_application | ~30s |
| `context_accumulation` | 3 | 3 | context_accumulation, memory_recall | ~2 min |

## Output

Results print to stdout and save to `eval_results/`:
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
            {"type": "contains", "value": "rust"}
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

All assertions are case-insensitive by default. Add `case_sensitive: true` to override.

## Tool Safety

During eval, tool approval uses a name-based allowlist (`_SAFE_TOOL_EXACT` for exact matches, `_SAFE_TOOL_PREFIXES` for prefix matches). Approved tools are additionally checked against `is_sensitive_path()` — if the tool targets a sensitive path (e.g. `~/.aws`, `~/.ssh`), it is rejected regardless of name. All other tools not on the allowlist are rejected outright. This keeps eval runs safe and side-effect-free while allowing the agent to use read-only tools.

## Architecture

```
kirocrew eval            CLI entry point — scenario selection, output
  └─ EvalRunner          Runs scenarios with fresh state per scenario
       └─ Session        Each session gets its own LLM provider (simulates restart)
            └─ Turn      Send message → collect response → check assertions
```

Key design: each session creates a **new provider instance**, simulating a user closing and reopening KiroCrew. Cross-session context must come from persisted memory, not conversation history.

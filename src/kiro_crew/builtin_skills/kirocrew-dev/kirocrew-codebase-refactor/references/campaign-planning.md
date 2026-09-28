# Campaign Planning and Scope Selection

This reference belongs to `kirocrew-codebase-refactor` and applies only to the
Kiro Crew source repository or one of its worktrees.

Read this reference for a repository-wide scan, a multi-module campaign, or selection of the next wave.

## Freeze a reproducible baseline

Record:

- repository identity, upstream ref and exact SHA;
- collection time and platform;
- included source roots and language extensions;
- excluded tests, generated code, vendored code, fixtures, snapshots, build output, and migrations;
- physical lines versus nonblank/code lines;
- commands or script versions used;
- current dirty state and active local work;
- current open changes and upstream query coverage.

Report later numbers against both this frozen baseline and current upstream. Moving only the denominator can make a campaign appear to regress or succeed incorrectly.

The optional scanner provides a portable physical/nonblank LOC and Python
structure baseline. Use Kiro Crew's injected runtime. On POSIX:

```bash
"$KIROCREW_RUNTIME_PYTHON" "$SKILL_DIR/scripts/scan_refactor_hotspots.py" --root . \
  --path-prefix src/kiro_crew --path-prefix website/src \
  --format markdown --top 100
```

On PowerShell:

```powershell
& $env:KIROCREW_RUNTIME_PYTHON "$SKILL_DIR/scripts/scan_refactor_hotspots.py" --root . `
  --path-prefix src/kiro_crew --path-prefix website/src `
  --format markdown --top 100
```

Adapt source roots and exclusions to the repository. Never treat a generic threshold as proof of poor design.

## Inventory structural pressure

Collect evidence in these categories:

### Size and shape

- files, classes, and functions above repository-appropriate thresholds;
- maximum and percentile function/class length;
- nesting depth, long conditional chains, and cyclomatic complexity when supported;
- oversized facades that also own business logic;
- comment/docstring volume, especially repetitive signature narration.

### Knowledge duplication

- repeated validation, defaults, conversions, error mapping, retry logic, permissions, or protocol handling;
- helpers with different names but the same knowledge;
- multiple facades owning divergent copies of one rule;
- duplicated platform adapters that should share a stable primitive.

Do not merge code merely because its text resembles another block. Shared knowledge with different policy is not duplication.

### Coupling and change pressure

- import/dependency fan-in and fan-out;
- cycles and boundary violations;
- files frequently changed together;
- recent commit count and contributor count;
- open changes touching the same paths or implementing the same intent;
- dynamic imports, plugin surfaces, monkeypatch targets, and reflection.

### Verification strength

- characterization, unit, contract, integration, packaging, and platform tests;
- ability to compare public surfaces byte-for-byte or structurally;
- known flaky or inherited failures;
- runtime benchmarks for hot paths;
- external consumer/plugin coverage.

### Agent navigability

For agent-heavy repositories, measure the cost of finding and loading a definition, not only LOC:

- defining-file tokens for representative imported symbols: median, p90, maximum;
- symbol lookups whose defining file exceeds the model context limit;
- read windows/tool calls needed to locate and load a definition;
- unrelated top-level symbols sharing the defining file;
- exact definition size and tokens returned per lookup.

Use a stable symbol workload and seed. This idea was validated at large scale by the Hermes codebase simplification; see [research-synthesis.md](research-synthesis.md).

## Review through three lenses

For each candidate, perform these passes either sequentially or with independent read-only reviewers:

1. **Reuse** — stable duplicated knowledge, existing utilities that should be reused, and abstractions duplicated by accident.
2. **Quality** — naming, cohesion, branching, state ownership, error clarity, comments, types, and project conventions.
3. **Efficiency** — redundant passes, I/O, allocations, awaits, parsing, lookups, or N+1 work that can be removed without speculative optimization.

When recommendations conflict, behavioral safety and comprehension win over extra DRYness or micro-optimization.

## Rank candidates

Score with evidence rather than selecting the largest file automatically. A useful 1–5 rubric:

| Dimension | 1 | 5 |
|---|---|---|
| Structural payoff | Cosmetic improvement | Removes a major hotspot/duplication owner |
| Contract risk | Internal and pure | Public, serialized, concurrent, or plugin-facing |
| Test strength | Weak/unknown | Strong characterization and integration coverage |
| Upstream churn | Stable | Frequently changed since baseline |
| Active overlap | None | Same paths or intent in active changes |
| Coupling | Leaf | Central dependency hub |
| Platform sensitivity | Single portable path | OS/FFI/process/signal/network differences |
| Review cost | Small cohesive diff | Huge mixed/generated/locale diff |

Prioritize high payoff, strong tests, low churn, low overlap, and coherent ownership. Defer a central high-churn god file when a stable neighboring boundary yields comparable value.

Do not assign a numeric score to unavailable evidence. Mark churn, overlap, test execution, or coupling `UNKNOWN`, explain the missing capability, and make the ranking provisional. The presence of test files proves only that tests exist, not that they pass or cover the contract.

## Define a scope contract

Each scope must state:

- problem and evidence;
- desired ownership boundary;
- included responsibilities and paths;
- forbidden or integration-owner paths;
- public behavior and surfaces that must remain stable;
- expected structural outcome;
- target and integration gates;
- likely compatibility mechanism;
- upstream overlap status;
- explicit non-goals;
- stop/redesign conditions.

A scope may span several thousand lines when that is the natural boundary. Reject both extremes: a whole subsystem with multiple unrelated owners, and dozens of tiny tasks that only move methods without reducing cognitive load.

## Build the wave conflict graph

For every pair of scopes, compare:

- owned file intersection;
- shared generated outputs, manifests, locale files, baselines, snapshots, schemas, and package metadata;
- shared facade or registry updates;
- tests that mutate global state or use the same fixed resources;
- dependency order between proposed new modules;
- reviewer/CI bottlenecks.

Only graph-independent writers run concurrently. Shared integration edits belong to one owner after worker branches are ready.

## Set decidable acceptance criteria

Examples—select only those relevant to the repository:

- named large files fall below agreed thresholds;
- named functions/classes are decomposed and responsibility owners are explicit;
- duplicated policy has one canonical implementation;
- public schemas, CLI help, config defaults, prompts, wire formats, SQL/DDL, or i18n content match the baseline;
- module/API identity manifests pass;
- targeted and owner suites pass;
- packaging and import smoke pass;
- agent navigation metrics improve by an agreed amount;
- runtime/import performance remains within an agreed budget;
- no new dependency cycles or forbidden in-tree compatibility imports;
- every scope reaches the requested delivery state.

An LOC reduction target such as 30% can be an acceptance criterion, but never the sole one. It can reward deleted explanations, compressed readability, or fragmented modules while behavior and dependencies worsen.

## Produce the plan

For each wave, report:

| Scope | Structural target | Owned paths | Risk | Upstream overlap | Gates | Dependencies | Delivery endpoint |
|---|---|---|---|---|---|---|---|

Also report the baseline SHA, metric definitions, omitted areas, wave concurrency rationale, shared integration owner, and drain policy for stale/local-only work.

Do not begin the next wave while the current wave has unexplained local-only commits, stale open changes, or integration throughput substantially below implementation throughput.

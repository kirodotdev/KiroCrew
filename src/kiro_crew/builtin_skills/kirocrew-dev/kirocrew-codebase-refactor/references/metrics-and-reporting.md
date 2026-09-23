# Metrics and Reporting

This reference belongs to `kirocrew-codebase-refactor` and measures only Kiro
Crew refactor work. Keep runtime scopes separate from tests, generated output,
campaign tooling, and documentation-only changes.

Read this reference for campaign status, completion percentage, forecasting, or before/after reporting.

## Report two baselines

Every report identifies:

- frozen campaign baseline ref/SHA and date;
- latest upstream ref/SHA and collection date;
- identical measurement scope and exclusion rules;
- whether metrics come from the landed tree, an open branch, or local-only work.

Use the frozen baseline to measure campaign effect and latest upstream to describe present reality.

## Structural inventory

At minimum, when applicable:

- first-party runtime physical LOC and code/nonblank LOC;
- files above agreed thresholds such as 3k and 5k;
- classes/functions above agreed thresholds;
- largest file, class, function, conditional chain, nesting depth, and complexity;
- duplicated knowledge/rules and dependency cycles;
- module count, import/dependency edges, fan-in/fan-out;
- startup/import/build time and package size;
- source/test ratio and relevant contract-test coverage.

Thresholds are indicators, not universal quality laws. State why the chosen values matter to this codebase.

## Agent navigability

When the code is routinely maintained by agents, include:

- tokens in the defining file for a stable workload of symbols: median, p90, max;
- lookups whose file exceeds available context windows;
- tokens actually returned by the repository's typical lookup strategy;
- read/tool calls and windows per lookup;
- definition length and unrelated symbols in the same file;
- time and token cost for representative maintenance tasks when an eval exists.

Use the same symbol set, tokenizer, seed, and lookup policy before and after. Report both tail reduction and central tendency; comment removal can make each remaining line token-denser even while total navigation improves.

## Scope funnel

Count scopes by exact state:

- unstarted;
- discovered/baselined;
- claimed;
- implementing;
- local code complete;
- target-green;
- PR/change open;
- integration-green;
- approved/merge-ready;
- merged/delivered;
- parked/blocked;
- superseded/dropped.

Separate runtime refactors from campaign tooling, test infrastructure, and documentation-only work.

## Touched versus eliminated

Always report both:

```text
touched coverage = scopes with any landed structural change / baseline candidate scopes

hotspot elimination = baseline hotspots no longer above the agreed boundary
                      / baseline hotspots
```

Also count touched scopes that still contain a large primary or extracted module. A facade reduced below the threshold does not mean the scope is cleared when one extracted owner remains larger than the original target.

The central progress definition is:

```text
real progress = landed, verified structural problems eliminated
              != files touched
              != local commits
              != open PRs
              != diff churn
```

## Simplification scorecard

For each landed scope, record before/after:

| Dimension | Evidence |
|---|---|
| Size/shape | Primary and largest extracted file/class/function |
| Reuse | Canonical rules/helpers created; duplicate owners removed |
| Quality | Nesting/branching/complexity/cohesion change |
| Contracts | Public surface and parity checks |
| Navigability | Defining-file/lookup token change when measured |
| Coupling | Dependency edges/cycles/fan-out |
| Runtime | Import/startup/hot-path change |
| Delivery | Merged SHA and required gates |
| Residual work | Remaining hotspots and explicit non-goals |

Use a balanced conclusion. A 35% LOC reduction accompanied by 35% more import edges may still be worthwhile, but the trade belongs in the report.

## Flow and reliability metrics

For each scope:

- start → local target-green time;
- target-green → publication time;
- publication → integration-green time;
- integration-green → merge time;
- review waiting time;
- branch age and upstream commit drift;
- rebase count and semantic conflict count;
- CI reruns and confirmed flakes;
- post-merge regressions.

For the campaign:

- merged scopes and eliminated hotspots per week;
- current writer/review/CI WIP;
- local-only and stale branches;
- superseded/abandoned rate;
- review throughput versus implementation throughput;
- post-merge regression rate;
- time and cost when available.

These metrics reveal when adding workers only produces more stale branches.

## Forecasting

Forecast from a recent window of **merged, verified hotspot eliminations**, not diff LOC or local completions.

```text
effective weekly rate = merged eliminations / observed weeks

adjusted rate = effective weekly rate
                * recent success ratio
                * integration-capacity factor
                * (1 - supersession ratio)

estimated weeks = weighted remaining hotspots / adjusted rate
```

Weight by risk, churn, coupling, and platform coverage. Provide a range and assumptions; do not present a precise date when review capacity or upstream growth dominates.

## Status report format

```markdown
## Snapshot
- Frozen baseline: <sha/date>
- Latest upstream: <sha/date>
- Measurement scope: <roots/exclusions>

## Outcome
- Runtime scopes merged: X/Y
- Tooling/support scopes merged: A/B
- Hotspots eliminated: H/BaselineH
- Current >=3k / >=5k: ...
- Net LOC/code LOC: ...
- Agent navigability: ...
- Counter-movers: modules/import edges/import time ...

## Delivery funnel
| Scope | State | Branch/change | Drift | Required evidence | Next action |

## Risks
- Active overlap
- Local-only/stale work
- Inherited/flaky gates
- Compatibility/packaging concerns

## Recommendation
- Continue, drain, redesign, pause, or stop—with evidence
```

Do not claim a percentage of “the whole codebase refactored” without naming the denominator and acceptance rule.

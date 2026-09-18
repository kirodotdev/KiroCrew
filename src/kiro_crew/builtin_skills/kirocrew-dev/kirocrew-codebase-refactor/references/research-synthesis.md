# Research Synthesis

This reference records the external methods deliberately incorporated into the
Kiro Crew codebase refactor skill. It is an evidence and attribution note, not a
requirement to install those projects. Snapshot date: 2026-09-18.

## Hermes whole-codebase simplification

Primary source: [NousResearch/hermes-agent PR #102117](https://github.com/NousResearch/hermes-agent/pull/102117), with operational follow-up in [issue #103563](https://github.com/NousResearch/hermes-agent/issues/103563). Repository license at the research snapshot: MIT.

Verified results reported by the project:

- non-test source: 1,063,826 → 698,363 lines (−34.4%);
- files at least 5,000 lines: 37 → 6;
- functions at least 300 lines: 192 → 2;
- worst reported cyclomatic complexity: 1,075 → 84;
- 4,271 commits across 2,655 files, built through isolated worker slices;
- defining-file token median 30,762 → 10,333 and maximum 330,028 → 85,075 for the published symbol workload;
- lookups landing in files over a 128k context: 1,589 → 0.

The PR also reports counter-costs: Python module count and import edges increased, and split entry points imported more slowly. Review found missing public names, packaging omissions, monkeypatch seams, and exception-handling rewrites that tests had not initially caught.

Adopted principles:

- make the campaign goal decidable with explicit structural and parity criteria;
- measure agent navigation cost, not only LOC;
- split work into non-overlapping ownership groups with isolated worktrees;
- keep verified, recoverable checkpoints;
- capture byte/structural parity for schemas, prompts, config, wire data, CLI, and packaging;
- use compatibility manifests and identity tests for moved public names;
- forbid in-tree dependence on temporary compatibility pointers;
- reproduce full-suite failures on the exact clean base;
- publish counter-metrics such as modules, import edges, and import time;
- budget heavily for review and interruption recovery.

Not adopted as universal rules:

- a mandatory 30%/34% LOC target;
- hundreds of concurrent workers;
- a fixed commit count or one specific decomposition naming scheme;
- the assumption that test parity alone proves external API compatibility.

## Anthropic Code Simplifier

Sources: [official plugin catalog](https://claude.com/plugins) and [official code-simplifier agent](https://github.com/anthropics/claude-plugins-official/blob/main/plugins/code-simplifier/agents/code-simplifier.md). The official catalog showed roughly 346k installs at the snapshot; repository license: Apache-2.0.

Adopted principles:

- preserve exact functionality;
- default a local polish pass to recently changed code;
- apply repository-specific conventions;
- prefer explicit readability over clever brevity;
- remove redundant abstraction and obvious comments without over-simplifying;
- do not optimize for fewer lines alone.

The Kiro Crew skill broadens this from recent-diff polish to full campaigns and adds contract, packaging, upstream, delivery, and counter-metric controls.

## OpenHands Code Simplifier

Source: [OpenHands extensions code-simplifier](https://github.com/OpenHands/extensions/tree/main/skills/code-simplifier). Repository license: MIT.

Adopted principle: review through three distinct lenses—reuse, code quality, and efficiency—with parallel read-only reviewers when useful and a sequential fallback when delegation is absent. The Kiro Crew skill adds conflict resolution between lenses: behavior and comprehension take priority over DRYness or speculative optimization.

## Superpowers

Sources: [verification-before-completion](https://github.com/obra/superpowers/blob/main/skills/verification-before-completion/SKILL.md), the [Superpowers repository](https://github.com/obra/superpowers), and its worktree, subagent, and harness-porting guidance. The Claude plugin catalog showed roughly one million installs at the snapshot; repository license: MIT.

Adopted principles:

- no success claim without fresh command evidence and exit-status inspection;
- verify worker output independently;
- isolate writers in worktrees;
- keep serial execution as a full fallback when subagents are unavailable;
- map abstract operations onto each harness rather than inventing missing tools.

## Addy Osmani Code Simplification

Source: [addyosmani/agent-skills code-simplification](https://github.com/addyosmani/agent-skills/blob/main/skills/code-simplification/SKILL.md). The repository had roughly 96k stars at the snapshot; license: MIT.

Adopted principles:

- understand the reason for a structure before removing it;
- match project conventions rather than impose personal style;
- separate refactoring from feature behavior;
- use incremental, reviewable steps;
- evaluate whether a new maintainer can understand the result faster.

Generic before/after snippets were not copied because seemingly simple transformations can change language semantics—for example exception timing around `async` or the observable representation/order of a new container type. This skill requires proof in the target runtime.

## Other useful skill patterns

- [refactoring.guru-skill](https://github.com/christianpasinrey/refactoring.guru-skill) contributed the practice of stating the design force/smell, chosen approach, and rejected alternative before a non-trivial restructuring, plus characterization tests when behavior is not already pinned.
- [code-refactoring-skill](https://github.com/MuhiminOsim/code-refactoring-skill) demonstrates cross-harness packaging and a broad smell/refactoring catalog; this skill keeps catalogs optional so pattern matching does not override repository context.
- [Vercel Agent Skills](https://github.com/vercel-labs/agent-skills) reinforces progressive disclosure, focused references, deterministic scripts, impact-ranked rules, and immutable skill packaging.

## How synthesis was handled

The skill paraphrases methods and adds campaign-specific controls derived from observed failures. It does not vendor external prompts, code examples, or scripts. Popularity was used to find practices worth evaluating, not as proof that a rule is safe. Primary source behavior and explicit counterexamples outrank install or star counts.

# The harness-parity gate

Kiro Crew drives one first-class agent harness — `kiro-cli` — and adapts the
others. This gate is the mechanical half of that rule: it reads the lines a
change ADDS under `src/kiro_crew/` and fails on any that let a harness other
than Kiro inherit something by default. The invariants it enforces, and the
judgment-only ones it cannot, are catalogued in
[../system-specs/modules/harness-parity.md](../system-specs/modules/harness-parity.md).

## What runs where

| Where | Command | Fails the build |
|---|---|---|
| CI job `harness-parity` in `ci.yml` | `check_harness_parity.py --test` then the diff-scoped run | yes — `CI` is a required check, so every job in it is blocking |
| CI job `test` (ordinary pytest) | `test/test_harness_parity.py` | yes — 22 structural pins, groups A and C |
| The four AI review lanes | the `harness-parity` rule in `AUTOSDE.yaml`, `blocking: true` | yes — a violation of a blocking rule blocks |
| Locally | `HARNESS_BASE_REF=origin/main python3 scripts/check_harness_parity.py` | exit 1 on any violation |

The self-test runs **first**, in the same step. A gate that has silently stopped
matching reads as a green signal, which is worse than no gate, so each rule is
exercised against a planted probe before the real check runs. `pytest` runs the
same self-test, so a broken regex also fails a local test run.

## The six rules

Each names the invariant it closes. All six are line-shape rules on added lines;
none of them needs to resolve an import, which is why the job needs no
`setup-python` and no dependency install.

| Rule | Invariant | Fails on |
|---|---|---|
| `negative-identity` | H5 | `not is_claude_backend`, `not self._is_kas` — harness identity as the absence of another harness |
| `negative-constant` | H5 | `!= ACP_BACKEND_KAS` and its mirror — an inequality captures every harness added later |
| `bare-literal` | H8 | `backend == "kas"` — `ACP_BACKEND_KIRO` is the empty string, so only the named constant is legible |
| `sandbox-delegation` | H7 | `is_kiro_cli=` derived from a negation. This flag makes `wrap_argv` SKIP Kiro Crew's seatbelt, so it fails OPEN |
| `vocabulary-home` | H8 | an `ACP_BACKEND_*` identifier or `ACP_BACKENDS_*` set defined outside `acp/types.py` |
| `non-kiro-default` | H1 | `default=ACP_BACKEND_KAS` and equivalents — an operator who configures nothing gets Kiro |

## Why diff-scoped rather than whole-tree

The tree carries nine pre-existing negative identity tests, nearly all in the
dormant `ACP_BACKEND_CLAUDE` seam. A whole-tree gate would fail every PR until a
separate conversion change lands, and would charge that break to whoever pushed
next. Added lines are complete for regression — a line only reaches `main`
through a diff that added it — and running the script with no `HARNESS_BASE_REF`
prints the whole-tree count as a **non-failing** report, so the backlog stays
visible without ever being anyone's build break.

The base ref is `github.event.pull_request.base.sha`, resolved through
`.github/scripts/resolve-i18n-base.sh`, not `origin/main`: the merge ref the run
checks out was computed against that exact commit, so the diff cannot pick up
`main` moves that landed after the run started.

## What the scanner deliberately does not see

- **Backtick spans.** Python 3 has no backtick syntax, so a backtick span is
  always prose — the docstring convention for naming a symbol
  (``` ``not is_claude_backend`` ```). Naming a forbidden form in order to forbid
  it must not violate it.
- **Comment tails.** Stripped up to the first *unquoted* `#`. Quote state is
  tracked rather than splitting on the first `#`, because a string literal may
  contain one and truncating there would hide the real call site behind it.
- **`scripts/check_harness_parity.py` and `test/test_harness_parity.py`**, which
  spell every forbidden form out literally.
- **`src/kiro_crew/acp/types.py`**, for the two vocabulary rules only. It is the
  module those definitions are supposed to live in.

## Escape hatch

A `harness-ok: <reason>` marker in a trailing comment silences the whole line. It
is unscoped, so a reviewer should ask why the positive form does not work — in
almost every case the answer is that a named membership set was missing.

## Adding a rule

Add the `Rule` record, add at least one probe to `PROBES` (the `rule-coverage`
check fails if a rule has none), add the row to the table above, and add the
invariant row to
[../system-specs/modules/harness-parity.md](../system-specs/modules/harness-parity.md)
in the same change. A rule with no probe is a rule that can silently stop
matching.

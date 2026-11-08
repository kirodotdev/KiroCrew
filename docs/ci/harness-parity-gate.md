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
| Job `harness-parity` in `fast-gate.yml` | `check_harness_parity.py --test` then the diff-scoped run | yes — `Fast Gate` is a required check of its own, so every job in it is blocking |
| CI job `backend-test` (ordinary pytest) | `test/test_harness_parity.py` | yes — the structural pins in groups A (Kiro is the default and the floor), B (identity is tested positively) and C (the Kiro path keeps its own machinery) |
| The four AI review lanes | the `harness-parity` rule in `AUTOSDE.yaml`, `blocking: true` | yes — a violation of a blocking rule blocks |
| Locally | `HARNESS_BASE_REF=origin/main python3 scripts/check_harness_parity.py` | exit 1 on any violation |
| Same job, same command | the [X-capture rule](#the-x-capture-rule-whole-tree-shrink-only-allowlist), whole tree | yes, except entries on its shrink-only allowlist |

The self-test runs **first**, in the same step. A gate that has silently stopped
matching reads as a green signal, which is worse than no gate, so each rule is
exercised against a planted probe before the real check runs. `pytest` runs the
same self-test, so a broken regex also fails a local test run.

## The six rules

Each names the invariant it closes. All six are line-shape rules on added lines;
none of them needs to resolve an import. The scanner itself uses only the Python
standard library; the workflow selects Python 3.12 with `setup-python` but performs
no dependency install.

| Rule | Invariant | Fails on |
|---|---|---|
| `negative-identity` | H5 | `not is_claude_backend`, `not self._is_kas` — harness identity as the absence of another harness |
| `negative-constant` | H5 | `!= ACP_BACKEND_KAS` and its mirror — an inequality captures every harness added later |
| `bare-literal` | H8 | `backend == "kas"` — `ACP_BACKEND_KIRO` is the empty string, so only the named constant is legible |
| `sandbox-delegation` | H7 | `is_kiro_cli=` derived from a negation. This flag makes `wrap_argv` SKIP Kiro Crew's seatbelt, so it fails OPEN |
| `vocabulary-home` | H8 | an `ACP_BACKEND_*` identifier or `ACP_BACKENDS_*` set defined outside `agent_sdk/backends.py` and the owners `VOCABULARY_PATHS` lists beside it |
| `non-kiro-default` | H1 | `default=ACP_BACKEND_KAS` and equivalents — an operator who configures nothing gets Kiro |

## Why diff-scoped rather than whole-tree

A whole-tree gate would charge any pre-existing negative identity test to
whoever pushed next. Added lines are complete for regression — a line only
reaches `main` through a diff that added it — and running the script with no
`HARNESS_BASE_REF` prints any pre-existing lines as a **non-failing** report, so
a backlog stays visible without ever being anyone's build break.

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
- **`src/kiro_crew/agent_sdk/backends.py` and its owners**, for the
  `vocabulary-home` rule only. That module is the one import path for the
  vocabulary, and each owner it re-exports from (`backends_mcp.py`,
  `backends_sessions.py` and the rest) defines the sets for one question;
  `src/kiro_crew/acp/types.py` re-exports them. `VOCABULARY_PATHS` names every
  exempt file exactly, never by pattern, so a new owner is gated until it is
  listed, and `test_harness_parity.py` pins that list to the vocabulary
  module's own owner imports.

## The X-capture rule (whole tree, shrink-only allowlist)

A non-standard (X) capability — an underscore method, a `_meta` field, a harness-native verb — is proven once, on one adapter version. Most ACP follow-up fixes came from X sets whose proof was prose, a synthesized fixture, the wrong adapter package, or a kiro-cli capture standing in for KAS. So the same script, in both its diff-scoped and whole-tree runs, also checks every set in `X_FEATURE_SETS`:

| Each member must | Fails on |
|---|---|
| cite `acp_frames/<dir>/<file>.jsonl` in the `#` block directly above the set, where `<dir>` is the member's own fixture directory (`kiro` for `""`) | a member with no citation in its own directory, as when pi joined `ACP_BACKENDS_STEERING_REQUEST` with no pi capture (#21740) |
| for KAS, cite `kas/` | a KAS member citing only `kiro/` — kiro-cli is not KAS |
| point at a file that exists, whose header says `recorded: live`, the member's `backend`, and an `agent_version` | a missing or synthesized capture |
| carry an agent name, when it has one (`_meta.agent_name`, else `initialize.agentInfo.name`), listed in `EXPECTED_AGENT_NAMES` | a capture of another package, such as `@openma/pi-acp` for pi |
| for kiro-cli and KAS (`HANDSHAKE_IDENTITY`), hold its own `initialize` response: KAS with `agentCapabilities._meta.kiro.extensionMethods` and no `agentInfo`, kiro-cli with `agentInfo` "Kiro CLI Agent" and no such list | a kiro-cli capture relabeled `backend: kas`, or a KAS capture relabeled `backend: ""` |
| have its `.expected.json` snapshot | a capture `test_acp_frame_replay.py` does not replay through the dispatch parsers |

For KAS, `agent_version` is the engine's own version, read from the `@kiro/agent` package.json kiro-cli installs, not kiro-cli's version; a newer capture also records `kiro_cli_version`. `test_x_feature_captures.py` checks that every live `kas/` capture names an engine version.

Coverage is opt-out. Every `ACP_BACKENDS_*` set must sit in exactly one of three lists, or the check fails and names the set: `X_FEATURE_SETS` (checked as above); `X_REGISTRATION_PENDING`, for an X set whose capture does not exist yet, each entry naming its non-standard verb or field; or `HARNESS_EXEMPT_SETS`, for a set that is not an X feature, each entry with a one-line reason (standard ACP, routing-only, off the wire, Crew-side). The pending list only shrinks: with `HARNESS_BASE_REF` set, an entry missing from the base ref's copy fails, and a set that leaves it must join `X_FEATURE_SETS`, never the exempt list. `test/test_x_feature_captures.py` holds its ceiling.

A second check covers kiro-cli and KAS drifting apart: every `ACP_BACKENDS_*` set holding exactly one of them must name the other in its comment, so the reader sees the split was decided, not assumed.

Today's gaps live in `X_CAPTURE_ALLOWLIST` (each entry names the missing capture) and `KIRO_KAS_PAIR_ALLOWLIST`. Both only shrink: with `HARNESS_BASE_REF` set, an entry missing from the base ref's copy fails; an entry whose member now passes fails until it is deleted; an X set dropped from `X_FEATURE_SETS`, or its scope narrowed, fails; and `test/test_x_feature_captures.py` holds a ceiling on each. A member added to an X set therefore lands with its capture, or not at all. The self-test plants each shape above.

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

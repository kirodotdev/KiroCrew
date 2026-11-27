# Task evals

Real coding cases, run unattended on several harnesses and scored the same way.
Design: [`rfc-builtin-eval.md`](../../docs/request-for-change/rfc-builtin-eval.md).

## Running

```bash
# No model, no tokens: every case fails on its base and passes on its reference.
kirocrew eval run --suite tasks --check

# Run arms. Needs a dedicated eval user (see below) and each arm's eval-only sign-in.
kirocrew eval run --suite tasks --tier simple --harness crew-chat,claude-code \
    --eval-home /srv/kirocrew-eval --out ./eval_results
```

`--suite tasks` reads `evals/tasks` under the current directory; a path to a
suite directory works too. `--repeat N` sets runs per case (default: the tier's
k, 5 / 3 / 2). `--case ID` picks cases. `--json` prints the summary as JSON.

| harness | runs | sign-in it needs |
|---|---|---|
| `crew-chat` | `kirocrew chat --headless --approval-mode trust` on a gateway started in the run's own data home | `KIRO_API_KEY` |
| `crew-conductor` | the same with `--mode conductor --until-done` | `KIRO_API_KEY` |
| `claude-code` | `claude -p --permission-mode dontAsk --allowedTools <case tools> --strict-mcp-config` | `ANTHROPIC_API_KEY`, or `CLAUDE_CODE_USE_BEDROCK` with `AWS_BEARER_TOKEN_BEDROCK` |
| `kiro-cli-v3`, `codex` | not built yet: every run is VOID | |

`--use-operator-credentials` skips the eval user: every arm runs as you, with your
own environment, `HOME`, kiro-cli sign-in and the working `claude` on your `PATH`.
It first checks `kiro-cli whoami` and `claude -p "say ok"` and stops
if either fails. The agents can then reach your credentials; the CLI says so before
it starts and every run record carries `credentials`. Each run still gets its own
work directory and Crew data home.

Exit codes: 0 every scored run passed, 1 a run failed or none was scored, 2
usage, 3 preflight refused.

## The eval user

Each run gets its own directory under `--eval-home` with `HOME`,
`KIROCREW_HOME`, `KIRO_HOME` and `TMPDIR` inside it, and an env holding only
`PATH`, the locale, `PYTHONPATH` and the arm's sign-in. Crew arms run with the
`strict` sandbox tier and no unsandboxed fallback. After the final hidden check
the runner kills whatever the arm left running, then deletes the directory once
its output is copied to `--out`.

A swapped `HOME` alone is not a fence, so the run refuses to start when the
user running it holds any of the credential paths the sandbox hides (`~/.aws`,
`~/.ssh`, `~/.netrc`, `~/.git-credentials`, `~/.docker/config.json`, a Claude Code
login and more; `~/.config/gh` and `~/.kube` too), or can list another login
user's home. Run it as a dedicated OS user. Linux and macOS only.

The suite is fingerprinted when the batch starts and after every run. A run
that changed any case, check or `lib/` file is VOID and the batch stops.

## Scoring

The seed asks the agent only to commit on `trunk`; no arm pushes. Before each
check (the 60 s samples and the final one) the runner fetches the repo's `trunk`
into the local origin, and the check judges that origin. Each run directory keeps
the arm's whole stdout and, for a Crew arm, every chat transcript and
background-agent record under `crew/`.

A run is **VOID** (rerun, never counted) when the harness could not start, the
arm is not built, or its output names this suite's path. Otherwise it is
**FAILED** on any ask of a person, on passing the wall or spend cap, on any
other non-done ending, or when the hidden check fails; else **PASSED**. Each run
writes `runs/<run id>/result.json` (verdict, reason, asks, wall, cost, idle
after done, the check's parts, the resolved Crew agent); the batch writes
`eval_<ts>.md` and `eval_<ts>.json` with one row per harness and tier.

## Case layout

```
cases/<tier>/<ID>-<slug>/
  case.json        id, version, kind, tier, seed ({repo} placeholder), timeout_minutes,
                   spend_cap {credits, usd}, expected_wall_minutes, tools, check_parts,
                   provenance (source PRs, issues, month merged), canary
  CASE.md          where it comes from, how it is built, the traps
  build.sh         build.sh REPO ORIGIN: the base as one commit on `trunk`, with a local bare origin
  base/            the files the task repo starts with
  ref/             the same files after the merged change
  hidden/check.py  check.py ORIGIN: judges origin `trunk`; prints {"pass", "parts", "counters", "notes"}
lib/               build_from_base.sh, hidden.py (the shared check driver)
```

The branch is `trunk`, not `main`: Kiro Crew refuses a push to a protected
branch name even to a local origin, so a Crew arm could never finish a case on
`main`.

## Adding a case

Take it from a merged public PR. Write the seed from the issue as filed, or
from the bug as a user sees it; never from the diff, PR text or commits. Put
the hidden tests' feed builders, fixtures and required limits (a length cap,
an event cap) in `check.py` itself, so editing the visible tests or the
module's constants cannot change what it measures. Then run `--check`: the case
must fail on `base/` and pass on `ref/`.

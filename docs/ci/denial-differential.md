# The denial-differential gate

`Denial Differential` (`.github/workflows/denial-differential.yml`) fails a PR
that makes a deny rule refuse an operation the product depends on.

## Why it exists

A security fix is almost always a rule made stricter, and "stricter" has no upper
bound a reviewer can see. The author reads the pattern and the attack it now
catches. Nobody reads the set of ordinary commands that pattern *also* newly
matches — a read-only `gh` query, a feature-branch push, a chat start, an
installed cron. So the failure mode of a security fix is not a missed
vulnerability: it is a gate that quietly starts refusing legitimate work, and the
symptom surfaces days later as an agent that stopped working, reporting a pattern
instead of a cause.

That question is a bad fit for a model review, which would have to simulate a
17,000-line matcher over a corpus of commands nobody wrote down. It is a good fit
for a deterministic before/after classification.

## How it answers

`scripts/deny_diff.py` takes a base ref, a head ref, and a corpus of golden
paths. For each `shell` row whose platform matches the runner, it classifies the
command **twice** — once with the deny composite as it exists at the base ref,
once as it exists at the head ref — and diffs the two verdicts:

| Base | Head | Verdict |
|---|---|---|
| allowed | refused | **regression** — fails the job |
| refused | allowed | loosening — reported, informational |
| same | same | unchanged — counted only |

A loosening does not fail, because loosening is what a revert or a
false-positive fix looks like.

### "Refused" means the whole composite, not just the rule catalog

A shell command at `hooks.on_tool_call` is refused by four checks, and this gate
runs all four in the same order, reporting which one decided:

| Tier | Check | Lives in |
|---|---|---|
| `sensitive-path` | `is_sensitive_path` — the path fence | `security/paths.py` |
| `sensitive-bash` | `is_sensitive_bash_command` — scan ceiling, IMDS reach, env-credential detector | `security/paths.py` |
| `exfil` | `audit_bash_exfiltration` — egress and reverse-shell shapes | `security/exfil.py` |
| `deny-rules` | `is_denied` — the rule catalog and the argv-structural floors | `security/__init__.py` |

Measuring only the last one would be the specific way this gate could ship a
meaningless green: its trigger paths cover `paths.py` and `exfil.py`, so a
tightening there would run it, come back empty, and leave the green badge
standing as evidence the question had been asked. The tier is reported because
"the path fence refused it" and "a catalog rule matched it" need different fixes.

The list is **pinned, not asserted**: `test/test_deny_diff.py` reads the checks
out of the hooks gate's own source and compares them with the script's declared
tier table, so adding a fifth check over there reds this gate instead of silently
escaping it.

Every check is called with its default enabled set, which fails closed to every
built-in rule enabled — the strictest posture an operator can be running, and the
only one that needs no config on the runner.

### A PR that ADDS a deny check

That is the gate's primary use case, and it needs one rule to work at all. The
child is always this script at head, so it iterates head's tier table against
whichever tree it is pointed at — and the base tree of a check-adding PR has no
such function. Treating that as an error would exit 2 on exactly the tightening
the gate exists to measure.

So a tier absent at **base** is skipped: a check that did not exist there refused
nothing there, which is the truth, and the new check's refusals at head then
surface as regressions — the answer the reviewer wanted. The report names the added
tiers, because a whole tier's worth of rows appearing at once otherwise looks like
the differential misfiring.

A tier absent at **head** is the opposite: coverage silently lost. That stays an
exit-2 error.

### Why the verdicts can be trusted

Both sides are the **real** code: each ref is materialized with `git archive`
into its own directory, and a child process classifies against it with
`PYTHONPATH` pointed there. Three properties keep that honest:

- **The parent never imports the product.** The composite is the thing under
  test, so importing it in the harness would pin the comparison to one side.
- **Each child proves which tree answered.** It re-reports the file
  `kiro_crew.security` resolved to and exits 2 when that sits outside the
  checkout it was given. Without the check, an installed copy of the package
  shadowing the path would serve *both* refs from one tree and every differential
  would come back empty — a false green with no symptom.
- **Each child is hermetic.** Every `KIROCREW_*` variable is stripped and
  `KIROCREW_HOME` is repointed at a throwaway directory, so the verdict depends
  on the checkout alone and the best-effort audit writes never reach a real
  security log.

Exit codes: `0` no regressions, `1` regressions, `2` corpus or ref error. A `2`
fails the job — a differential that could not run is not a pass.

### What a green does not cover

The gate calls the four `security.*` checks directly, so it measures the **rules**,
not `hooks.py`'s own composition of them: how the targets are built, the
`raw_params` application of the path fence, the context-derived enabled set. A
tightening implemented inside `hooks.py` itself runs this gate — `hooks.py` is in
its trigger paths — and comes back empty, because none of the four functions
changed.

The tier pin narrows this but does not close it: it catches a check *added* to the
gate, not a behavioural change in how the gate assembles what it checks. Closing it
properly means driving commands through the gate's own target construction, which is
async and needs a session context — a larger change than this gate, and worth doing
separately if hooks-internal tightenings turn out to be a real source of false
denials. Until then: a green here means "no golden path is newly refused by the four
rule checks", and that is narrower than "no golden path is newly refused".

## The corpus

The corpus is `src/kiro_crew/builtin_skills/security-conductor/golden-paths.json`, the
security-conductor's committed golden-paths seed, whose rows each name an operation
that is legitimate **by decision** plus the reason it is:

```json
{ "kind": "shell", "surface": "gh-read",
  "command_or_flow": "gh pr view 9332 --json state",
  "platform": "any", "reason": "Read-only PR status query." }
```

That file is also what the security-conductor's `verify_fix.py` reads, so the
fixer's acceptance gate and this one judge a change against **one** corpus. A
second corpus was the earlier arrangement — a small fixture under `scripts/` that
this gate classified while the seed only fed `verify_fix.py` — and two corpora can
drift into disagreeing about what a golden path is. The fixture is retired; the
rows it alone carried were folded into the seed by the same change that repointed
this gate, so the handover withdrew nothing.

`surface` groups rows for a reader and is ignored by the gate.

`kind` is `shell`, `flow` or `cron`; `platform` is `any`, `posix` or `windows`.
Only `shell` rows are classified here — a flow and a cron have no single command
line to hand a matcher — and the other two are reported as skipped rather than
dropped. They stay in the file on purpose: the corpus is a contract document as
well as a gate input, and one that listed only shell rows would read as "these are
all the golden paths", which is false. Reporting them as skipped is the honest form,
and it also means a corpus that is mostly unclassifiable says so instead of
reporting a confident zero. A row that spells its command under any other key is a malformed
corpus (exit 2), not a tolerated variant: silently classifying nothing is the
false green this gate exists to prevent.

### It is read from the base ref, not the head checkout

The corpus **is** the contract the change is judged against, so the change must
not be able to edit it. A head-owned corpus would let the same PR that tightens a
rule delete the row that rule breaks — removing it from *both* sides of the
differential and passing.

Two consequences worth knowing. A row **added** by the PR is not classified,
because the base corpus does not have it — the pinned corpus test (below) is what
covers a newly-added row, since it reads the head file. And when neither corpus
exists at the base ref (the gate or the corpus is being introduced) the head file
is used and the report says so: with no base contract there is no row a PR could
withdraw to escape one.

The reason a row belongs in the corpus is the reason a reviewer needs when the gate
goes red, so a row without one is not much use.

The corpus path stays a **named line** in the workflow rather than a lookup, which
is what made adopting the seed an explicit PR instead of an automatic preference
for whichever file exists. A gate that changed contracts the moment another file
appeared would hand coverage over with nobody's diff showing the handover, and rows
the old corpus carried could drop out silently. The script itself takes any corpus
path, so a future move is a one-line change plus the same obligation: show that the
new file covers the rows the old one carried.

## Why a three-platform matrix

The rules read argv **shape** and the path fence reads real paths, and both
differ per platform: Windows has its own tokenizer and separators, and the
fence's home-directory ordering is macOS-specific (`Library/Application
Support`, `/Volumes/<share>`). A Linux-only gate would pass a tightening that
breaks every operator on another OS. That is the same class of defect the
[Cross-Platform Portability](ci-and-reviews.md) gate exists for and cannot see,
because that one reads added lines rather than behaviour.

Legs do not `fail-fast`: one platform's regression is not evidence about
another's, and a cancelled leg hides rows only that runner can produce.

## When it goes red

Read the job summary. Each listed row is an operation the corpus records as
legitimate that this change newly refuses, with the reason it is legitimate, the
tier that refused it, and the refusal text. There are exactly two answers.

**Narrow the rule.** The usual one: the tightening is catching more than it meant
to, and the row is the evidence of what.

**Withdraw the row, in its own pull request, first.** When the row should no
longer be a golden path, remove it from the corpus in a PR that changes no rule.
This gate is green on that PR — nothing is newly refused — and once it lands, this
PR's base no longer carries the row.

There is deliberately **no approval label**. Unlike the [Cross-Platform
Portability](ci-and-reviews.md) gate, whose finding is a line of code with nothing
to edit but the code, this gate's finding is a row in a file that is already
reviewable — so the base-owned corpus is itself the escape, and it is a better
one. A withdrawal in its own PR is reviewed on its own merits, with the removed
row and its stated reason as the whole diff, instead of riding along inside a
security change behind a label. It also leaves no waiver mechanism to keep honest:
a label that survives a push, or a scoping comment anyone can author, is a bypass
of the gate rather than a use of it.

## Its relationship to the pinned corpus test

`test/test_deny_diff.py` also pins every corpus row as allowed at `HEAD`, and
that test runs in the ordinary suite on Linux and Windows for every PR. The two
are complementary, and the differential earns its place on attribution: the
pinned test says "a corpus row is refused", the differential says "*this change*
newly refuses it". When `main` drifts into refusing a row, the pinned test goes
red on every subsequent PR and blames whichever one happens to be open, while the
differential reads base-red plus head-red as unchanged and stays green. It also
reports loosenings — how a false-positive fix demonstrates it fixed something. In
the other direction the pinned test covers what the differential deliberately
cannot: a row the PR *adds*, which the base-owned corpus does not contain.

## Running it locally

```
python3 scripts/deny_diff.py --base origin/main --head HEAD \
    --corpus src/kiro_crew/builtin_skills/security-conductor/golden-paths.json
```

Add `--json` for machine-readable rows, or `--platform posix|windows` to classify
another platform's rows than the host's. Stdlib only, no install required.

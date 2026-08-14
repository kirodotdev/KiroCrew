---
name: module-simplification
description: "Refactor the Kiro Crew backend for readability ONE MODULE AT A TIME (one module = one branch = one commit = one PR), safely and in parallel across sessions. Carries the candidate classes worth sweeping, the three prohibitions that make a mechanical sweep safe (never delete an in-function `from X import Y`, never hoist a lazy import on the gateway boot path, never reshape a keystone security file), how to tell a real regression from this host's environmental test noise, and how several sessions share the work without colliding. Use when simplifying, tidying, de-duplicating, or flattening existing Kiro Crew backend code — not for feature work."
triggers: kirocrew module simplification, kirocrew simplify module, kirocrew refactor module, kirocrew readability sweep, kirocrew dead imports, kirocrew chained ternary, kirocrew simplify backend, kirocrew source refactor
repo_scope: src/kiro_crew
---

# Module-by-module simplification

> **Scope guard: this skill applies ONLY when the working directory is the Kiro
> Crew source repository (or a worktree of it).** Its rules are conventions of
> this repo and are wrong elsewhere. Read
> [`kirocrew-worktree-dev`](../kirocrew-worktree-dev/SKILL.md) first — every rule
> there (worktree mandate, build gate, single commit) still applies. This skill
> adds only what is specific to *behavior-preserving* refactoring.

The whole point is **behavior preservation**. You are changing how code reads, never
what it does. A simplification that alters behavior is not a simplification, it is an
undocumented bug fix riding in a cleanup PR — the worst place to put one.

## Rule 0 — One module, one branch, one commit, one PR

```bash
git worktree add ../kirocrew-wt-simplify-<module> -b refactor/simplify-<module> origin/main
```

Never batch modules. A 48-file mixed-class diff costs a reviewer far more than
three narrow ones, and when a reviewer asks for one subtraction you re-run the
whole gate cycle. One module also makes the parallel model in Rule 5 work.

Name the branch **exactly** `refactor/simplify-<module>` — Rule 5 uses that
pattern as the cross-session lock.

## Rule 1 — What is in scope

Run the bundled detector for the two mechanically decidable classes:

```bash
SKILL_DIR="${KIROCREW_HOME:-$HOME/.kiro/crew}/skills/kirocrew-dev/module-simplification"
python3 "$SKILL_DIR/scripts/scan_module.py"                      # queue: per-module counts
python3 "$SKILL_DIR/scripts/scan_module.py" --module <module>     # site-level detail
```

It reports exactly two classes, and both are safe by construction:

| Class | What it is | Why deleting/rewriting is safe |
|---|---|---|
| `shadow-import` | in-function `import X` where `X` is already imported at module scope in the same file | the inner statement re-binds the same `sys.modules` singleton, so it is a strict no-op |
| `chained-ternary` | `a if c1 else b if c2 else d` (an `IfExp` whose `body`/`orelse` is an `IfExp`) | rewrite to `if`/`elif`/`else` preserving precedence, order and short-circuiting |

The detector already excludes every false-positive class that has bitten this
sweep before: `TYPE_CHECKING`-only module bindings (deleting the inner import
would be a runtime `NameError`), `try/except ImportError` optional-dependency
guards, `# circular import` markers **including one written inside a
parenthesized form on the imported name's own line**, and ternaries that merely
*contain* a ternary as a sub-expression of their result
(`(a if c else b).method() if x else []` — that reads left-to-right and is out of
scope). Trust its exclusions; do not re-derive them by eye.

**A zero from the detector does not mean there is no work.** These two classes are
finite and get consumed — one sweep took the whole tree from 98 actionable sites to
0. When the scan comes back empty, the module's remaining simplification is entirely
in the judgment classes below, which no detector decides for you. Re-run the scan
anyway at the start of each module: the classes re-accumulate as new code lands, and
Rule 6 explains why a rebase alone can put sites back.

**Judgment classes** are also in scope, but only for the module you are already in,
capped at a handful per file, and only when you are certain:

- provably dead code — an unreachable branch, an unused local, a name nothing reads
- a duplicated condition, or a branch that cannot differ from its sibling
- logic re-implementing an existing helper — route it through the helper, after
  verifying the semantics match **exactly** (a redaction helper that applies the
  same two calls in the same order is a real dedup; one that reorders them is not)
- nesting past three levels that flattens cleanly via an early return or `continue`
- **comment hygiene**, per `AGENTS.md`: state current behavior in present tense,
  drop task-log citations (PR/CR numbers, review-round markers, commit SHAs,
  incident dates, milestone tags). Only in files you are already editing for a
  structural reason, so comment churn rides along with substantive change instead
  of becoming its own 200-file sweep.

A comment you rewrite must be **true of the current code**. A stale comment flipped
to confident present tense misleads the next reader worse than the stale one did —
verify each claim against the adjacent lines before restating it.

Two things look like this work and are not: chasing a coverage number, and
"while I'm here" fixes. Both belong in their own PR.

## Rule 2 — The three prohibitions

These are not style preferences. Each one has already produced a defect here.

### 2a — NEVER delete an in-function `from X import Y`

An in-function `from X import Y` resolves `X.Y` at **call** time. A test that
substitutes the attribute on the source module — `monkeypatch.setattr(mod, "Y", …)`
or `patch("X.Y")` — is therefore observed by the function. The module-level binding
captured the original object at import time and is **not**.

Deleting one silently redirects the call to the real object. Measured: dropping a
`KiroCrewConfig` shadow in `dashboard/handlers/memory.py` made the handler read the
operator's real config instead of the test's mock, and only one assertion in the
suite happened to notice.

The asymmetry is what settles it. The gain is one dead-looking line; the loss is a
test that keeps passing while verifying nothing — and several of these symbols are
security controls (`is_sensitive_path`, `redact_credentials`,
`redact_exfiltration_urls`, `sel`). Proving a deletion safe means enumerating every
patch idiom across a 26k-test suite: dotted-string, module-object variable, module
alias, and direct attribute assignment. One miss is invisible.

So the detector never reports them, and neither do you.

**A `# noqa: F811` on such an import is evidence somebody kept the shadow
deliberately.** It is the opposite of a signal that the line is dead.

### 2b — NEVER hoist a lazy import

Deleting a shadow is safe because the module already loads at module scope.
*Hoisting* a genuinely lazy import is a different act, and on the gateway boot path
it violates the **blocking** AUTOSDE rule `no-new-work-on-gateway-boot-path`, which
forbids eager import of an optional or feature-flagged subsystem. The detector flags
those files `boot-path`, reading the rule's own `file-patterns` out of the checkout's
`AUTOSDE.yaml` rather than carrying a copy — so widening that rule cannot leave the
detector stale, and it exits nonzero if the rule is renamed. If a site is not in the
detector's output, leave it.

### 2c — NEVER reshape a keystone security file, and apply that consistently

**The detector owns the keystone set** — it is `KEYSTONE_RELATIVE` in
`scan_module.py`, which validates every entry against the checkout and exits nonzero
if one no longer exists. Read it from there rather than from a list in prose; a
second copy here is how the set goes stale. Files it covers are flagged `keystone`,
and their chained ternaries are printed as `EXCLUDED` and not counted as work.

That validation is one-directional, so note the gap: it catches a keystone file that
was renamed or deleted, but **a newly added security-critical module is not detected
and will be offered as ordinary work.** If you add one — a new matcher, guard, audit
emitter, or policy evaluator — add it to `KEYSTONE_RELATIVE` in the same change.
Nothing else will.

Deleting a shadow-import in such a file is fine. Restructuring a sensitive-path
matcher, a denied-command rule, a deny-by-default guard, an audit emit, a redaction
call, or the governance intersection is not — even to "simplify" it. A verbose
explicit guard stays verbose.

**Consistency is the part that gets missed.** If you exclude one keystone file
because "the value feeds only an audit label", that criterion covers *every*
keystone audit label. Two reviewers caught exactly this drift on one PR: one hunk
excluded in `safety_override.py` while the equivalent `Decision.layer` hunk in
`platform/governance.py` landed. Decide the criterion once, then grep for every
file it reaches before you push.

## Rule 3 — The loop for one module

1. **Scan** — `scan_module.py --module <module>`. Note `boot-path` / `keystone` flags.
2. **Read before editing.** Open enough of each file to understand the surrounding
   logic. Pattern-matching on a line number is how a `paused` hoist turns into an
   `UnboundLocalError`: assigning a name inside a nested function makes it local for
   that whole function, so a closure read elsewhere in it breaks. Check the
   enclosing scope for the name you are about to bind.
3. **Line numbers go stale the moment you edit.** Re-locate every subsequent site
   in that file by its **text**, never by a number the scan printed earlier.
4. **Prove each rewrite equivalent** before moving on: branch precedence; whether
   the new form evaluates a call, property or subscript the original skipped (a
   hoisted `dict.get` is unobservable, a hoisted property or anything that can raise
   is not); `is not None` versus truthiness, which flips on `0`, `""`, `[]`, `False`;
   whether a statement moved into or out of a `try`/`except`/`finally` or across an
   `await`.
5. **mypy infers a conditionally-assigned local from its FIRST assignment.** A chain
   assigning `str` then `None` needs an explicit annotation
   (`lock_reason: str | None`) or mypy rejects the `None`.
6. **Gate** — Rule 4.
7. **Review, then PR** — `prepare-pr` owns the commit→green loop.

## Rule 4 — Gating, and attributing a test failure to the right cause

`black --check` is **not** a CI gate here (deliberately disabled pending a bulk
format pass), and flake8 ignores `E501`/`W503`. Do not reformat unrelated lines and
do not report line length.

Build the mypy/pytest venv on **Python 3.12** to match CI. A 3.14 venv invents a
`asyncio.PidfdChildWatcher` error in `cli.py` (removed in 3.14), and never install
`faiss` — it makes mypy stricter than CI, which sees a missing import.

**Never conclude anything from the full suite's failure COUNT alone — on either
side.** A count can be inflated by the machine, and a machine that inflates it will
also hide a real regression inside the noise. Two causes seen on a development host,
both of which red tests no diff touched:

- **No usable OS-level sandbox.** `unshare(CLONE_NEWUSER)` returning `EINVAL` makes
  anything that spawns a sandboxed subprocess raise `SandboxUnavailableError`.
- **Memory pressure under `-n auto`.** Subagent spawns get refused for want of
  several GB, reddening most of `test_subagent*` at once.

On one such host `origin/main` itself failed 86 while a branch failed 123 — neither
number said anything about the branch. Your host may be perfectly healthy and every
failure real; that is exactly why the count is not the test. Attribute, don't assume:

```bash
# 1. Re-run just the failing files, in isolation, with nothing else heavy running.
python -m pytest -q -p no:cacheprovider --override-ini="addopts=-n 4 --dist loadgroup" <files>

# 2. If any still fail, build a PRISTINE baseline with its OWN venv and diff the
#    failure SETS, not the counts. A shared venv is editable-installed against your
#    branch worktree and would silently test the wrong source.
git worktree add ../kirocrew-wt-baseline --detach origin/main
```

Everything environmental passes in isolation; a real regression still fails. That
is exactly how one genuine regression was separated from 64 branch-only failures.
CI is the authoritative suite signal — it has a working sandbox.

**Check exit codes, never piped output.** `cmd | tail` makes `$?` tail's status, and
a `grep -c` that finds nothing exits 1 and will short-circuit a `&&` chain, skipping
the gate you thought ran. Redirect to a file and test `$?` on its own line.

Two gate gotchas that cost a round each:

- **`verify_vendor_manifest.py` fails after any test run**, because importing the
  vendored tree leaves `__pycache__` under `src/kiro_crew/_vendor/`. It is gitignored
  so `git status` looks clean. Clear it before pushing:
  `find src/kiro_crew/_vendor -name __pycache__ -type d -exec rm -rf {} +`
- **A backend-only diff narrows CI.** Touching nothing under `website/`,
  `.github/` or `scripts/` makes the `changes` job report `only_backend=true`, so
  the frontend suites cannot newly fail and you need not run them locally. Say so
  in the PR body rather than leaving it unexplained.

For the PR body: a backend-only sweep has no rendered delta, so it takes the
`<!-- no-visual-delta -->` marker **plus** the required one-line justification.

## Rule 5 — Several sessions at once

The work parallelizes well because module file sets are disjoint. What does *not*
parallelize is the push, so be precise about the mechanism.

**The remote branch is the lock.** No claim file, no stale-lock cleanup, works
across machines:

```bash
# See what is already claimed.
git ls-remote --heads origin 'refactor/simplify-*'

# Claim it, before editing anything. This is a CREATE-ONLY push: the empty lease
# value asserts the remote ref does not exist yet, so exactly one session wins.
git push --force-with-lease=refs/heads/refactor/simplify-<module>: -u origin refactor/simplify-<module>
```

A rejection (`! [rejected] ... (stale info)`) means another session got there first —
take the next module off the queue.

**Do not claim with a plain `git push -u`.** Two sessions that create the branch at
the same commit — which is what a fresh worktree off `origin/main` gives you — both
see the push succeed, so both believe they own the module and both start editing. The
empty-lease form is what makes the claim exclusive rather than advisory.

- **One session owns one module, start to merge.** Do not hand a half-finished
  module to another session; the base will have moved and the second session cannot
  tell your edits from the rebase's.
- **Bound the in-flight count to about 2–4 PRs.** This repo already carries 200+
  open PRs. A dozen simultaneous sweep PRs rebase into each other, and every rebase
  can *add* new sites (Rule 6), so throughput goes down as concurrency goes up.
- **Order the queue ascending by `actionable`.** Land the small modules first and
  build reviewer trust before touching `dashboard/` and `slack/`, which hold most of
  the sites and most of the risk.

**Do NOT drive this from a cron job or a heartbeat task.** Both are structurally
incapable of it and both fail while reporting success. A cron has no owning chat
slot, so its tool calls land on a deny-by-default approval path and time out after
180 seconds — and a denied tool inside a completed turn still records
`last_status: ok`, so the registry looks healthy while nothing is pushed. Heartbeat
runs under a strict name allowlist with no shell and no `git push`, so it cannot
amend a commit at all. Use real interactive sessions, one per module; see
[`babysit`](../babysit/SKILL.md) for the in-session monitoring loop that *can* push.

## Rule 6 — The base moves under you, and it adds work

`main` here can absorb dozens of commits while one PR is open — one measured PR
rebased three times, once across 40 commits. Two consequences:

- **Re-scan after every rebase.** A rebase does not just relocate your hunks, it can
  introduce brand-new sites. One chained ternary appeared in `dashboard/state.py`
  from a commit merged while the branch was open, which made a "the AST pass
  enumerated everything" claim false through no fault of the pass.
- **Re-run the gates after every rebase**, and let `push_guard.py` refuse the push
  when `HEAD~1` is no longer `origin/main`. That refusal is the guard working.

## Why these rules exist

- Deleting an in-function `from X import Y` → a test's mock stops being observed →
  a security control's test passes while verifying nothing (Rule 2a).
- Hoisting a lazy import on the boot path → every user pays it on every launch, and
  a blocking AUTOSDE rule rejects the diff (Rule 2b).
- Reshaping a keystone guard for style → a reviewer must diff a security evaluator
  to confirm a no-op; excluding one keystone file but not its twin gets caught and
  costs a round (Rule 2c).
- Trusting a stale line number → an edit lands in the wrong place, or a hoisted
  local shadows a closure variable and raises `UnboundLocalError` (Rule 3).
- Reading the full suite's failure count as signal → hours spent on failures
  `origin/main` also has (Rule 4).
- Batching modules → an unreviewable diff, and one requested subtraction re-runs the
  whole gate cycle (Rule 0).
- Driving the loop from cron → 100+ runs, zero pushes, a green-looking job registry
  (Rule 5).

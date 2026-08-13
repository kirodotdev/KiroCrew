# Auto-Improvement — user manual

An opt-in built-in app that finds and fixes real defects in a GitHub repository, then
opens them as **draft pull requests** for you to review.

Its central idea: **measure before you change, and never let the agent grade its own
work.** The agent proposes; deterministic Python decides. That is why a run can honestly
report "I found nothing" — and why a fix that reaches you has already been proved.

---

## 1. Before you start

You need:

* **`git`** and an authenticated **`gh`** (`gh auth status`). An unauthenticated `gh`
  only fails at the moment a PR is drafted, which is the worst time to discover it — so
  the app checks up front and refuses to start.
* **A GitHub repository with a Python test suite.** The suite is the app's measuring
  instrument, so this is a hard requirement, not a nicety.
* **`ruff`** (optional). Improves defect discovery; the app degrades cleanly without it.

The app is **disabled by default** (`defaultEnabled: false`). Enable it from the Apps
page, then open **Auto-Improve** in the left nav.

### Is my repository a good target?

The gate runs your suite several times per candidate (build → lint → collect → RED twice
→ GREEN → suite-stays-green). Suite runtime is therefore the dominant cost.

| Suite | Verdict |
|---|---|
| Under ~2 min | Ideal. |
| 2–10 min | Workable; expect fewer candidates per hour. |
| Very large monorepo | Narrow the blast radius with `editAllowlist` (§7) so the gate runs only the relevant tests. Without that the whole-suite steps can exceed their timeout, which the app must read as "regressed". |
| No tests | Not a target. There is nothing to measure and nothing to prove a fix against. |

---

## 2. Connect a repository

1. Paste an `https://github.com/<owner>/<repo>` URL and click **Connect**.
2. Pick a **base branch**.

The app clones into its own scratch directory and immediately **disables push** on that
clone (both the fetch and push URLs are neutralized). It then re-checks this before every
run and refuses to start if the check does not hold.

Two refusals you may hit, both deliberate:

* **"Only github.com URLs are supported"** — the host list is an allowlist, not a
  denylist.
* **"Existing clone … has origin X, which does not match Y — refusing to reuse it"** —
  usually because `gh` switched between HTTPS and SSH. Delete or move the old scratch
  clone and reconnect. The app will not silently reuse a clone it cannot vouch for.

Changing repositories clears `branch` and `scopeDiffBase`, because a branch belongs to the
repository it came from.

---

## 3. Calibrate the ruler (do this first)

Click **Calibrate** before your first run.

Calibration measures your suite repeatedly to learn two things:

* **A baseline** — how long the suite takes.
* **A noise band** — how much that number moves on its own, computed as
  `max(2σ, floor)`.

The noise band is what makes a performance claim meaningful: a change is only a "win" if
it beats the band, not just the last measurement. It then runs a **canary** — a change
whose direction is known — to check the ruler can actually detect a win.

If the canary does not clear the band, the perf run **halts**: the ruler was not proven on
this target, and a "win" measured by an unproven ruler is exactly the unmeasured change this
app exists to refuse. The bug track is unaffected — it skips ruler pre-flight entirely,
because its RED→GREEN regression gate is the verdict and it has no noise band.

> A repository whose suite runs in about the time its own collection takes cannot prove a
> perf win this way. Point `benchmarkCommand` at a real workload instead, use the app for
> the bug track only, or set `canaryAdvisory` to warn-and-continue if you accept that the
> resulting perf numbers come from an instrument that was never proven on this repo.

---

## 4. Run a loop

Click **Run**. Each cycle:

1. **Discover** — the agent reads your code and names candidate defects, pinned to
   file, line, and symbol.
2. **Propose** — candidates are authored in isolated git worktrees, one per candidate.
3. **Gate** — build/import, no new lint findings, the reproducing test collects, then it
   must **fail on your unmodified code (twice, to catch flakes)**, **pass with the fix**,
   and **leave the rest of the suite green**.
4. **Keep** — only a real transition is accepted.
5. **Draft a PR** (or commit — §6).

A run ends on any of: the cycle cap, the time budget, the cost ceiling, **quiescence**
(3 consecutive cycles with no keep — "this region is mined out"), or **Stop**. Stop lands
between candidates, not mid-measurement, so it can take up to one gate cycle.

The activity feed shows the agent's own reasoning as it works. When a candidate is
rejected, that feed plus the finding's detail panel is where the "why" lives.

---

## 5. Read the findings

Click any finding to expand its evidence:

* **Defect** — what is wrong, in one sentence.
* **Hypothesis** — the concrete input and the wrong output.
* **Gate** — which stages passed (RED / GREEN / STAYGREEN / build / lint).
* **Reproducing test**, **diff**, **base commit**, **fingerprint**.

### What the statuses mean

| Status | Meaning | Retries? |
|---|---|---|
| `seen` | Discovered, not yet attempted. Usually the run ended first. | yes |
| `no_defect` | The agent investigated and found nothing real. **This is a success, not a failure** — an honest "no" beats an invented fix. | after 24 h |
| `failed_gate` | Did not build, added a lint finding, or its test would not collect. | sticky |
| `failed_verify` | The fix broke something else, or could not be proved. The anti-regression guard did its job. | after 24 h |
| `discarded_noise` | A perf change that did not beat the noise band. | sticky |
| `duplicate` | Same fingerprint already resolved; not re-filed. | — |
| `error` | A harness failure, not a verdict on the code. | after 24 h |
| `filed` | A draft PR exists. **Review it.** | — |
| `committed` | Pushed to your branch (autocommit mode). Click **View commit**. | — |

Findings are keyed by a **content fingerprint** and scoped per repository *and* branch, so
switching either gives you a separate set, and the same defect is never filed twice.

`Forget` makes a finding retryable; `Purge` also removes its artifacts.

---

## 6. Draft PRs vs autocommit

**Draft PR (default).** A verified fix is pushed to a generated
`auto-improvement/<kind>-<fingerprint>` branch and opened as a **draft**. The app never
marks a PR ready, never merges, and never enables auto-merge — those stay yours.

**Autocommit** (`directCommit`). A verified fix is committed straight to your base branch.
Use it on a feature branch you own. Guards that still apply:

* A **non-overridable protected-branch denylist** (`main`, `master`, `develop`,  <!-- wokeignore:rule=master -->
  `release/*`, …). A hand-edited config cannot widen it.
* If the branch moved while the run was working, the push is retried once after a rebase.
  A conflict aborts, and it never force-pushes — losing a race is a signal, not something
  to overwrite.

`autoPublish` (off by default) will mark a *fully green* draft ready-for-review. It never
merges. The gate is strict: open draft, verdict exactly READY, zero failing checks **and
at least one check actually run**, no unresolved comments.

---

## 7. Settings that matter

| Key | Default | Why you would change it |
|---|---|---|
| `maxCycles` | 25 | Deliberately generous. Let **time** and quiescence end a run — a low cycle cap leaves discovered findings at `seen`, never tried. |
| `maxHours` | 2.0 | The real bound on a run. |
| `maxCostUsd` | 5.0 | Ceiling on agent spend. |
| `quiesceAfter` | 3 | No-keep cycles before declaring the region mined out. |
| `editAllowlist` | *(whole repo)* | **The blast-radius control.** Glob-confine edits to a subtree. Also focuses discovery *and* the gate's suite on that region — the single most useful setting on a large repo. |
| `directCommit` | off | Autocommit instead of draft PRs (§6). |
| `proposerWide` / `proposerDeep` | 1 / 1 | Candidates authored per cycle. Each is a real agent call. |
| `scopeDiffBase` | unset | Restrict attention to what a branch changed. |
| `canaryAdvisory` | off | A failed canary halts a perf run. On downgrades it to a warning — accepting perf numbers from an unproven ruler. |

Some keys are deliberately **not** settable through the config API — `clone` and
`target_url` decide which repository the agent is turned loose on, so they move only
through Connect. Rejected keys are echoed back rather than silently dropped.

---

## 8. Troubleshooting

**"the clone's push is not disabled — re-run repository setup."** The safety invariant
failed. Reconnect the repository.

**Every candidate fails `failed_gate: test_invalid`.** Its reproducing test would not
collect. Most often the test depends on something only the *fix* introduces (a new import,
for example), so on your unmodified code it errors instead of failing cleanly — which
cannot prove the bug is real.

**Every candidate fails `failed_verify: suite is NOT green but reported no identifiable
failing test.`** The suite did not finish — usually a timeout on a very large repo. Narrow
`editAllowlist`.

**Nothing is ever kept, and findings sit at `seen`.** The run is ending before it can
attempt them. Raise `maxHours`; `maxCycles` is already generous.

**Discovery returns nothing repeatedly.** The region may genuinely be clean — the app
deduplicates by fingerprint, so a subtree it has already worked gets quieter over time.
Point `editAllowlist` somewhere new.

**A PR's CI goes red after the run finished.** The app periodically re-drives filed PRs
whose checks fail, bounded by a concurrency cap. Over-cap findings are deferred, never
dropped.

---

## 9. What it will not do

Stated plainly, because these are guarantees:

* Never pushes to a protected branch.
* Never merges, marks a PR ready (unless you enable `autoPublish`), or enables auto-merge.
* Never edits your tests to make a metric look better — the tests are the measuring
  instrument, and a candidate that deletes tests is rejected by name.
* Never reports an estimate as a measurement.
* Never keeps a fix on the agent's word. Every accepted change passed a deterministic
  gate, and a perf win is independently reproduced before a PR is drafted.

---

## See also

* [`../../../../../../docs/system-specs/modules/auto-improvement.md`](../../../../../../docs/system-specs/modules/auto-improvement.md)
  — how it works internally (routes, storage, safety controls).
* [`../../../../../../docs/system-specs/modules/auto-improvement-test-plan.md`](../../../../../../docs/system-specs/modules/auto-improvement-test-plan.md)
  — how it is verified.
* [`PORT_PLAN.md`](PORT_PLAN.md) — historical record of the port.

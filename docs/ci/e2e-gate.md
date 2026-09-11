# The browser E2E gate

```bash
python setup.py test_e2e
```

One command is the whole offline browser gate. It boots a real gateway wired to a
packaged fake model backend, then shells the in-tree Playwright suite at it. No
model, no credentials, no network, no cost.

The member-creation scenarios require a supported OS sandbox even with the fake
model backend. The Linux CI job enables unprivileged user namespaces and requires
`unshare --mount --map-root-user true` to succeed before the suite. A failed
precondition fails the job; the browser fixtures do not bypass private-memory
admission or skip these scenarios.
For the disposable gateway only (`KIROCREW_E2E_EPHEMERAL=1`), authenticated browser
setup enables `agent.sandbox=auto` through the owner API and reapplies the same
`agent.acp_backend` value. That field's existing refresh rebuilds the provider
factory that captured the minimal fixture's sandbox-off setting at startup.
Both writes must succeed before scenarios run; the model backend remains the
packaged fake executable and the shared minimal seed is unchanged.

`setup.py::E2eTestCommand` is the entry point (registered under `cmdclass` as
`test_e2e`). It runs exactly two pytest files:

| File | What it covers |
|---|---|
| `test/test_e2e_smoke.py` | Gateway boot and HTTP-level smoke checks. |
| `test/test_playwright_e2e.py` | The dashboard browser suite, folded in so one command is the whole gate. |

The HTTP smoke turns send the nonempty agent identity returned by slot creation.
They test the configured binding's real ACP round trip without substituting a
template name for its alias. Failed smoke POSTs report the HTTP status and a
bounded machine code, keeping response prose and credentials out of diagnostics.

## What the command sets up

`E2eTestCommand.run()` builds the child pytest invocation itself, so the
environment is not something a caller has to remember:

- `KIROCREW_E2E=1` lifts the `skipif` on both files. Neither runs in a bare
  `pytest` invocation, which is deliberate: the browser leg takes minutes per
  interpreter, far too slow for the per-commit gate.
- `KIROCREW_STRICT_ON_LOOP_PERSIST=1` turns the on-loop session-JSONL persistence
  discipline into an enforced invariant for the duration of the run. The harness
  gateway inherits this env, so any raw on-loop `ConversationLog._locked` entry
  that skipped the `*_off_loop` helpers raises `OnLoopPersistError` and fails the
  gate instead of silently losing transcript data under real contention.
- `-o addopts=` clears the `[tool:pytest]` defaults from `setup.cfg` (`-n auto`,
  `--dist loadgroup`, `--max-worker-restart=2`, `--timeout=120`). xdist would spawn
  one gateway per worker, and coverage of a subprocess gateway measures nothing, so
  the E2E run is **serial** and uninstrumented by construction. This is the one
  place an `addopts` wipe is correct: it runs two files, not a large selection, so
  the loadgroup invariant that a broad override must preserve does not apply. See
  [../system-specs/common/testing-conventions.md](../system-specs/common/testing-conventions.md).
- `--timeout=1800` replaces the 120s unit-test cap. The browser leg runs several
  minutes per interpreter leg, and with `retries: 2` under box contention a
  retry-heavy run can exceed a shorter cap. A generic pytest timeout kills the run
  and hides which specs actually failed, so the cap is set well above the
  expected worst case. Smoke tests finish in seconds and pay nothing for it.
- `-p no:cacheprovider` keeps the run from writing a pytest cache.

## How the browser leg is wired

`test_playwright_e2e.py::test_dashboard_playwright_suite` does five things in
order:

1. Resolves the in-tree `website/` directory (a sibling of `test/`), its
   Playwright CLI at `website/node_modules/.bin/playwright`, and a **concrete**
   Node >= 18 binary. Node resolution deliberately skips mise shims: a shim is
   cwd-sensitive and the website dir often pins an older Node, so the test scans
   real installs and prepends the winning bin dir to `PATH` for the child.
2. Points `KIROCREW_KIRO_BIN` at `kiro_crew.testing.fake_acp_backend`. That is
   the env var `kiro_cli.py` reads to override the agent binary, so the harness
   gateway spawns the fake instead of a real `kiro-cli`. The fake speaks the
   minimal ACP subset the client drives (`initialize`, `session/new`,
   `session/set_mode`, `session/set_model`, `session/prompt`) and switches
   behavior on bracket markers in the prompt (`[[TOOL]]`, `[[PERMISSION]]`,
   `[[GATED]]`, `[[SLOW]]`, `[[SLOW_NOACK]]`, `[[ERROR]]`), which is what makes
   agent-driven specs deterministic offline.
3. Boots a real gateway with `spawn_feature_gateway(fixture="minimal",
   approval="reads")`, on an isolated temporary `KIROCREW_HOME` seeded
   atomically with gateway startup.
4. Exports the harness env into the Playwright child: `PLAYWRIGHT_BASE_URL`
   (the gateway's port), `PLAYWRIGHT_TOKEN`, `PLAYWRIGHT_RUN_AGENT_SPECS=1`,
   `KIROCREW_E2E_EPHEMERAL=1`, `CI=1`, and `PLAYWRIGHT_JSON_OUTPUT_NAME`.
5. Runs `playwright test --reporter=html,json` with `cwd=website`. A CLI
   `--reporter` replaces the config value, so both are named: `html` keeps the CI
   artifact the config asks for, `json` supplies the machine-readable counts the
   darkening floor below reads.

`KIROCREW_KIRO_BIN` is restored (or removed) in a `finally` block, so the test
cannot leak a fake backend into a later test in the same interpreter.

### The gateway must already be running: `webServer` is not configured

`website/playwright.config.ts` sets `webServer: undefined`. Playwright starts no
server of its own, so a bare `npx playwright test` against a machine with no
gateway on `baseURL` fails on every spec. `test_e2e` is the supported way to run
the suite because it owns the gateway lifecycle.

Config facts worth knowing before you touch a spec:

| Setting | Value | Why |
|---|---|---|
| `testDir` | `./playwright` | Specs live at `website/playwright/*.spec.ts`. |
| `baseURL` | `process.env.PLAYWRIGHT_BASE_URL` or `http://localhost:5476` | 5476 is the default dashboard port, so an ad-hoc local run against a normal gateway works. |
| `locale` | `en-US` | Most specs assert English prose. The app resolves language from `navigator.languages` when nothing is stored, and the harness storage state carries no `mc-lang`, so a `zh-*` runner would render the zh-CN catalog and fail those assertions. Pinning makes that an explicit dependency. |
| `workers` | 1 under `CI` | The harness sets `CI=1`, so the browser leg is serial. |
| `retries` | 2 under `CI` | Absorbs gateway-load timeout flakes. |
| `timeout` | 30s per test | Assertion (`expect`/`poll`) timeout stays at Playwright's 5s default so a genuine slowdown surfaces instead of passing inside a wide window. |
| `grepInvert` | excludes `@needs-agent` unless `PLAYWRIGHT_RUN_AGENT_SPECS` | The default run is the credential-less green set. The harness wires the fake backend, so it opts the agent specs back in. `@needs-live-agent` stays excluded either way and currently tags nothing. |
| browser | Playwright's own bundled Chromium | This fork vends no browser binary; CI installs it with `npx playwright install chromium`, restored from an `actions/cache` entry keyed on the exact `@playwright/test` version. `--with-deps` is deliberately NOT used — see [what CI does](#what-ci-does-around-the-command). |

### Auth flow

Playwright runs two projects. The `setup` project (`playwright/auth.setup.ts`)
navigates once to `/?token=<PLAYWRIGHT_TOKEN>`, lets the gateway exchange the
token for a session cookie, sets the `mc-onboarded` localStorage flag so the
first-run theme overlay cannot intercept clicks, and persists the whole storage
state. The `chromium` project declares `dependencies: ['setup']` and loads that
state, so raw tokens never appear in test-level traces or videos.

The state path is `PLAYWRIGHT_STORAGE_STATE` or `playwright/.auth/state.json`,
and both writer and reader honor the same override. That matters for concurrency:
cookies are bound to one gateway's port and token, so two runs against separate
ephemeral gateways sharing the default file would have the last writer win and
the losers see "session expired".

When no token is supplied the setup project still writes an empty storage state,
because `storageState` must resolve to an existing file or every spec fails with
ENOENT.

## `KIROCREW_E2E_REQUIRE=1`: why a graceful skip needs a marker

The environment the browser leg needs (an in-tree `website/`, its installed
Playwright CLI, a Node >= 18) is not present in a python-only checkout. So
`_unresolved()` has two behaviors:

- **Marker unset** (ad-hoc local or dev run): `pytest.skip`. A contributor
  without the frontend toolchain installed still gets a useful smoke run.
- **`KIROCREW_E2E_REQUIRE` set** (the CI gate): `pytest.fail`. A skip counts as
  a pass, so without this the required gate would go green having run **zero**
  browser specs, which is exactly the dead-suite drift the fold exists to catch.

`.github/workflows/ci.yml`'s `e2e` job sets `KIROCREW_E2E_REQUIRE: "1"`. Set it
on any job you expect to actually exercise the browser.

## The darkening floor

An exit code cannot tell "all specs passed" from "the specs were never
collected". `grepInvert` excludes by tag, and an excluded spec is never collected
and never reported as a skip, so a mis-tagged suite reports green while a third
of it does not run. Every dark spec that was later re-enabled had also rotted:
stale selectors for UI that had moved, because nothing exercised them.

So `_assert_suite_not_darkened()` reads Playwright's JSON report and asserts two
numbers, **even when the run failed** (a red run plus a collapsed count points at
darkening rather than at the reported failure):

- `MIN_EXECUTED_SPECS` is a floor on `expected + flaky`. Both mean "ran and
  ultimately passed"; counting only `expected` would trip the floor whenever CI's
  retries absorb a flake. **Raise it when you add specs.** Only lower it with a
  written reason in the commit body, because a drop means specs stopped running.
- `MAX_SKIPPED_SPECS` is 0. A skip is a silent pass, so a spec should seed its
  preconditions in a fixture rather than skip when they are absent.

A missing or unparseable report is a hard `pytest.fail`, not a pass for lack of
evidence. The floor helper has its own unit tests in the same file, deliberately
**ungated** so they run in the default pytest pass: an unverified guard against
silent darkening is no guard.

## What CI does around the command

`ci.yml`'s `e2e` job (`E2E (stub ACP backend, offline)`) installs the backend
with `--group dev`, runs `npm ci` and `npm run build` in `website/`, stages
`website/dist` into `src/kiro_crew/static/dist` so the specs render the real
bundled dashboard rather than a 404, installs Chromium, runs the i18n render-time
gate (which reuses that Chromium install), and finally runs `python setup.py
test_e2e`.

### The browser install is budgeted, and installs no apt packages

The job's ceiling is `timeout-minutes: 25`, and the browser install is the step
that historically consumed it. It carries three constraints, all in service of
leaving the specs enough of that budget to actually run:

- **`~/.cache/ms-playwright` is cached**, keyed on the exact `@playwright/test`
  version read out of `website/package-lock.json`. The key has no restore-key
  prefix on purpose: a near-miss would hand the job a Chromium revision that
  `@playwright/test` does not expect.
- **`--with-deps` is not used.** It runs `apt-get update` first, and when the
  runner's default mirror answers `Ign:` apt falls back and stalls — measured at
  23, 15 and 12 minutes. It also buys nothing this gate asserts on: every shared
  library Chromium needs is already on the `ubuntu-latest` image, and the only
  packages it newly installs are 9 CJK/Thai/Cyrillic font packages. There is no
  pixel comparison anywhere under `website/`, `locale` is pinned to `en-US`, and
  the render gate reads `textContent` rather than measuring geometry. A spec that
  asserts glyph **metrics** for a non-Latin script would need those fonts back —
  as its own bounded, non-fatal step, not by restoring `--with-deps`.
- **`timeout-minutes: 6` on the step.** The download is ~7s and a cache hit is a
  no-op, so anything near the cap is a stalled mirror or CDN. Failing there
  reports the real cause while the job still has budget, instead of the job
  timing out having run zero specs.

### A red run uploads the specs' own failure record

`website/playwright/voice-recovery.spec.ts` contributes three untagged tests to
the executed-spec floor. Desktop and touch cases photograph blocked read-aloud
recovery and its open menu, checking labels, the preserved draft and viewport
fit. The closed-menu frames are taken without hovering or focusing the reply,
so they prove that recovery controls remain visible. A separate desktop case
photographs the failed-playback notice, follows its settings link, and checks
and photographs the highlighted Text-to-speech provider field.

These tests reuse the suite's authenticated page/browser fixtures and production
dashboard, with fixture API responses and injected `voice-error` events; they
neither record the microphone nor prove audible speech. After E2E, the
`voice-recovery-evidence` artifact retains six PNGs on a successful run plus
per-attempt provenance: checkout, PR-head, source-file, build, served-document
and frame hashes, viewport and browser version. Playwright owns retries and
timeouts; each attempt writes its own output directory, including partial
evidence if a later assertion fails.

`website/playwright/member-memory.spec.ts` also captures the real dashboard
with synthetic member and memory data in ten focused scenarios. The `member-memory-ui-evidence` artifact
retains its PNGs and focused-spec WebM recordings on successful and failed attempts: desktop/mobile records,
copy selection, record details, proposals, restore confirmation and pending
restore, a working legacy V1 member and explicit private-memory opt-in, plus V1/V2 bulk
selection and edit/forget previews. These are screenshots
of the test's interaction states, not evidence of a live provider response or a
Crew delegation. The artifact belongs to its GitHub Actions run and checkout;
partial screenshots from a failed attempt are not a completed scenario. The
upload includes only the named media and `member-memory-evidence.json`, never
the browser authentication state. Each attempt's directory contains
`member-memory-walkthrough.webm` and that JSON manifest with test title, status,
retry, exact CI checkout SHA, run ID and run attempt. Recording is enabled only
inside this memory spec, including successful tests; the global video setting
and authentication setup are unchanged. The first walkthrough shows the member
header/avatar, copy selection, correction preview/save, current toolbar, a
separate empty member and forgetting. The recovery walkthrough shows backup,
restore confirmation, pending recovery across reload and cancellation while the
active records stay unchanged. It does not activate a restore by restarting the
gateway. It also imports an episode through the owner API, corrects its explicitly
linked fact, and restores the resulting replaced experience through the real
recovery API. The restored episode keeps its ID, text, source and creation time;
the active facts and observed Global/peer records remain unchanged by restoration.
The legacy walkthrough captures Set up private memory and a disabled Manage
memory action with its visible unsaved-work reason. A separate scenario reads
intentional unavailable and mismatched bindings seeded only in the disposable
gateway's configuration, verifies a healthy member can still be created, and
performs a real identity-list Retry without claiming it repairs those bindings.
No healthy peer store is implied by the deliberately mismatched declaration;
the attempt manifest records that fixture limitation. These seven added capture
points are authored and pending CI execution. Only a completed CI run can supply these recordings; source authoring
alone is not rendered evidence.
Its retention is seven days, and it does not fail when setup produced no images.

When the job fails, a final `if: failure()` step uploads
`website/test-results/` and `website/playwright-report/` as the
`e2e-playwright-failures` artifact (7-day retention). `test-results/` holds one
directory per failed attempt: `error-context.md` (the ARIA snapshot of the page
at the failing assertion — the thing that says whether a locator matched the
wrong row or no row), the `on-first-retry` trace, and any screenshot. The html
report next to it is the one `--reporter=html` writes.

The job's log alone is not enough to triage a spec failure: it names
`error-context.md` and prints nothing from it. #8526 (a ghost transcript from the
previous session rendering for a few hundred ms after the first send) was
narrowed for hours from that one line before a local run produced the snapshot.
Download the artifact first; bisect second.

The app-detail scenario also captures the compact Design Critique description
at desktop and 390px widths. The separate `gallery-copy-ui-evidence` artifact
retains those PNGs for seven days. Capture code alone is not rendered evidence;
the current run must reach and pass that scenario before its images are used.

`if-no-files-found: ignore`, deliberately: a run that fails before the specs
start (a stalled browser install) has neither directory, and the upload must not
turn that into a second, misleading failure.

Related: [i18n-gates.md](i18n-gates.md) for the render-time gate that shares this
job, and [ci-and-reviews.md](ci-and-reviews.md) for where `e2e` sits among the
other PR gates.

The private member memory specs use the real gateway and database. They cover
explicit V1-to-member copying with provenance, correction/reload/forgetting,
cross-member isolation, persisted/cancellable backup staging, and the empty
member's exact conversation binding across reload. Desktop and
390px captures accompany the first flow. Their write guard requires
`KIROCREW_E2E_EPHEMERAL=1`, which the isolated gateway harness sets; it must never
be set for an operator gateway. The strict reporter enforces the executed-test floor and refuses skips or flaky retries. The current run must pass the preceding i18n render gate before these browser scenarios count as executed evidence.

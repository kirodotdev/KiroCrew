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

## The distribution layer: install the artifact, then boot it

The browser gate above and the backend shards both run against a SOURCE tree. A
whole class of failure is invisible to both, because it lives in packaging
metadata that a package manager or an installer interprets rather than in code we
run: a dependency name that does not exist in the target distro, a registry
registration that never lands, a prune that drops a module the packaged
interpreter imports at boot. Each of those produces an artifact that builds green
and then refuses to install or refuses to start.

Two legs cover it, and neither costs a PR any minutes: both live in
`workflow_call` workflows reached from `nightly.yml` and `release.yml`.

| Leg | Job | Script | What only a real install shows |
| --- | --- | --- | --- |
| Linux | `build-desktop.yml` -> `Smoke-install Linux packages (deb + rpm)` | `scripts/smoke-linux-packages.sh` | dependency names resolve in Ubuntu 24.04 and Amazon Linux 2023, the `.desktop` entry's `StartupWMClass` equals Electron's app_id, the maintainer scripts place and remove `/usr/bin/<exe>`, and the beacon stamp names THIS format |
| Windows | `build-windows.yml` -> `Smoke-install Windows installer (x64)` | `scripts/smoke-windows-install.ps1` | the uninstall registration and its `InstallLocation`, the install-root ownership boundary, where the Start Menu shortcut POINTS, that the bundled CLI runs, that the installed gateway answers `/api/health`, and that a silent uninstall removes both the registration and the tree |

The Windows leg is gated on the build job's `artifact_uploaded` output rather
than on `needs` alone: publish runs build Windows under `continue-on-error`
(`soft_fail`), and a job that failed under it still reads as success to its
dependents, so without the gate a packaging or signing failure would run the
smoke install against an artifact that was never uploaded and redden the very
release run `soft_fail` keeps green. The output is set by the step after the
upload, so it exists only when there is an artifact to consume.

Both scripts DERIVE every identity from the artifact rather than naming it. The
nightly channel deliberately ships different ones so it can sit beside stable:
`packaging/build-desktop.sh` overrides `productName`, `extraMetadata.name`,
`deb.packageName`, `linux.executableName` and `nsis.guid` for a `-nightly.`
version, which moves the install directory, the launcher name, the registry key
and the shortcut name together. Hardcoding stable's spelling fails the gate on
every nightly build, and because a failed job inside a reusable workflow fails
the CALLER's job, that would silently skip a whole platform's publication. The
Linux script reads the package's own declared name and its desktop entry's
filename; the Windows script diffs the uninstall registry around the install and
reads the registration that appeared.

Reading the repository's `website/electron/package.json` would be just as wrong
on Windows as hardcoding: those channel overrides are electron-builder CLI flags,
so the file on disk still says `KiroCrew` while the artifact says otherwise.

### What the Windows smoke does NOT assert

There is no `PATH` edit to assert. The `nsis` block in
`website/electron/package.json` declares no PATH handling and
`website/electron/build/installer.nsh` touches only shortcuts and the
electron-updater cache, so a desktop install puts no `kirocrew` on `PATH`. The
bundled CLI is exercised at its packaged path
(`resources\backend-dist\kirocrew-backend\bin\kirocrew.cmd`) as its own new
process instead, which is the path
[windows-install.md](../guides/windows-install.md) describes and the one the
managed-server invocation resolves.

### `build.yml`'s installer job boots the gateway it installed, on every PR

`build.yml`'s `build-windows-installer` job compiles an NSIS installer on every
qualifying PR, installs it silently, and runs
`.github/scripts/test-windows-installer.ps1` with NO `-SkipGatewayValidation`.
The script starts the just-installed bundled interpreter against an isolated data
home and requires `/api/ready` within 30 seconds, so an artifact that installs
but cannot boot fails at review time.

Its backend payload is a real python-build-standalone runtime carrying the wheel
`build-wheel` produced (the job `needs` it, so the bundled bytes are the ones
users install). The job repeats the same assembly
`packaging/build-desktop.sh`'s `build_backend_windows` performs -- PBS runtime,
`pip install`, the relocatable `bin/kirocrew.cmd` shim, a self-containment check
under `PYTHONNOUSERSITE=1`, then `packaging/precompile_windows.py` for the
measured gateway import closure -- minus the voice extras, which add a
pywhispercpp and numpy download for a code path a gateway boot never reaches.

`KIROCREW_KIRO_BIN` points at a `.cmd` shim running
`kiro_crew.testing.fake_acp_backend` out of the INSTALLED payload through the
INSTALLED interpreter, so readiness needs no model, no network and no sign-in.
`KIROCREW_SKIP_MODEL_DOWNLOAD=1` keeps the embedding model out of a 30-second
ceiling.

Two ceilings became load-bearing with that change and were not before. The
install-duration ceiling (120 s) previously measured the extraction of a 40-byte
batch file, so it proved nothing about a real install; it now measures one.
`MinStartupPycs` is passed as 750 rather than the script's 1000 default, because
the default describes the full release bundle and this job omits the voice
extras: the core closure of `kiro_crew.cli_server` measures about 990 sources, so
750 leaves headroom for the win32 closure differing while still catching what the
assertion exists for, which is bytecode filtered out of the artifact or a
launcher redirecting imports into an empty user cache. Both land near zero.

Before this the job staged a two-line `@echo off` batch file as its entire
backend payload and therefore had to pass `-SkipGatewayValidation`, since there
was no interpreter for the gateway leg to launch. The whole class of defect that
leaves an installable-but-unbootable artifact had no PR gate at all.

`build-windows.yml`'s nightly smoke job remains the broader one: it exercises the
SIGNED installer, the Start Menu shortcut's target, the bundled CLI and a silent
uninstall, none of which the PR lane covers.

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

## The cross-OS gateway boot matrix

Everything above is `ubuntu-latest`. `test/e2e/test_gateway_boot_matrix.py` is the
one asset that boots a real gateway on **macOS and Windows too**, and `ci.yml`'s
`e2e-boot-matrix` job is what runs it: `strategy.matrix.os` of `ubuntu-latest`,
`macos-15` and `windows-latest`, `fail-fast: false`, `needs: [await-fast-gate]`,
20 minutes.

### Why it exists

Before it, no job on either of those runners started a gateway at all: the whole
E2E surface is gated on `KIROCREW_E2E`, which only `setup.py test_e2e` sets, and
only the Linux `e2e` job runs that. That is one of the two holes
[#8117](https://github.com/kirodotdev/KiroCrew/pull/8117) fell through, reverted
in
[56f67aa43](https://github.com/kirodotdev/KiroCrew/commit/56f67aa43f00f9484c346a8d1669b39102a63c78).
It added a settings-file probe to `sandbox.wrap_argv`'s Windows delegation
branch, so on a fresh Windows host -- where that file does not exist -- the Kiro
ACP spawn stopped delegating to Kiro CLI's own sandbox, fell through to the
no-backend fail-closed path, and the gateway never became usable. The unit test
that pinned that branch, `test/test_sandbox_argv.py`, is in
`test/windows-collect-ignore.txt`, and the PR changed its mock to hardcode the
one answer a fresh Windows host cannot give. No second unit test closes that;
only a real boot on the real platform does.

### What it asserts

Seven tests, each on its own gateway and its own scratch `KIROCREW_HOME`.
`KIROCREW_KIRO_BIN` comes from `harness.fake_acp_backend_launcher`: the fake
backend's own `.py` on POSIX (exec'd through its shebang), and a generated
`kiro-backend.cmd` shim on Windows, because `CreateProcess` refuses a `.py` path.
The first Windows run of this module is why that helper exists: the gateway
booted, answered `/api/health`, resolved the `acp` provider, and then never
completed a turn, because the spawn of the `.py` path failed silently. A failed
first request or a missing reply reports `GatewayHandle.diagnostics()` (exit
status, stderr tail, stdout tail after READY) in the assertion, since on macOS
and Windows that tail is the only evidence a maintainer without that OS gets.

| Test | What it pins |
|---|---|
| `test_gateway_boots_and_answers_health` | `KIROCREW_READY:` then an unauthenticated `GET /api/health` 200. |
| `test_resolved_provider_is_acp` | The provider resolves to `acp`, so the `KIROCREW_KIRO_BIN` seam fires. |
| `test_prompt_returns_the_fake_backend_reply` | One session create plus one prompt returns the fake backend's reply. `/api/health` can answer while the ACP spawn is refused, so this is the load-bearing one. |
| `test_tool_marker_prompt_completes_the_turn` | A `[[TOOL]]` prompt still completes its turn. |
| `test_seeded_sandbox_mode_boots_and_runs_a_turn[minimal]` | `agent.sandbox: "off"` boots and serves. |
| `test_seeded_sandbox_mode_boots_and_runs_a_turn[rich]` | The shipped `auto` default boots and serves. **This is the #8117 pin.** |
| `test_shutdown_leaves_no_gateway_child_alive` | Teardown reaps the tree: the pid is gone (via `platform_compat.pid_exists`, never `os.kill(pid, 0)`) and the port refuses connections. |

The tier is expressed as a SEED FIXTURE rather than a post-boot config write,
because `agent.sandbox` is read at boot: `minimal` states `"off"` and `rich`
omits the key, so it resolves to the shipped default, which is the tier a fresh
install runs. The test asserts the fixture still says so, so editing either
fixture fails there instead of quietly collapsing the matrix to one tier tested
twice.

Under `auto`, the turn expectation off Windows is DERIVED from the product's own
backend probe rather than assumed. A host with a real backend (macOS seatbelt,
Linux user namespaces) must complete the turn; a host that genuinely has none
must FAIL CLOSED with a named sandbox refusal and stay healthy. `ubuntu-latest`
is that second host: its unprivileged user namespaces are AppArmor-restricted,
which is why `backend-test-sandbox` has to clear a sysctl to get one. On Windows
the expectation is unconditionally the first, so a #8117-style regression cannot
hide in the fail-closed branch.

### `KIROCREW_E2E_MATRIX_REQUIRE=1`: the second marker

Same mechanism as `KIROCREW_E2E_REQUIRE` above, for a different module. An unmet
PRECONDITION (the packaged fake ACP backend missing, `kiro_crew.testing` not
importable) is a graceful `pytest.skip` on a local run and a `pytest.fail` on the
job. Set it wherever you expect gateways to actually boot.

### The job's own honesty checks

- **`KIROCREW_HARNESS_READY_TIMEOUT` per OS**: 60 on Ubuntu, 90 on macOS, 180 on
  Windows. It lives in the job env, not the test, so a slow runner is retunable
  without a code change. Windows needs the widest window: subprocess spawn and
  filesystem latency there are measurably slower, the conditions
  [#9172](https://github.com/kirodotdev/KiroCrew/pull/9172) addressed when a slow
  disk killed the gateway.
- **`-n0` with `--timeout=420`**: the module spawns a real process per test, and
  under xdist a block takes the worker with it, which on Windows aborts the run.
  The cap sits above the widest readiness window plus the per-turn reply ceiling,
  so a stuck turn fails by name.
- **A canary grep for `7 passed`**, copied from the macOS peer-identity canary.
  `pytest` exits 0 on a fully skipped module, so the exit code cannot tell seven
  booted gateways from a module that was never collected. Raise the number when
  you add a test to that file.
- **`shell: bash` on every leg**, so one command text serves all three; the
  Windows default is pwsh, where `tee` and `grep` are not these tools.

`pr-readiness.yml` needs no entry: it resolves lanes by WORKFLOW FILE
(`ci.yml` -> `CI`), never by job name, so every job inside `ci.yml` is already
part of the required `CI` verdict.

## The pod scenario suite (nightly, not a PR gate)

A second E2E lane, orthogonal to the browser gate above. `test/e2e/scenarios/`
boots ONE real service-managed pod through the shipped `kirocrew pod` verbs and
drives five user-visible flows against it: a setting saved across a gateway
restart, a cron firing, one agent turn with a tool call, the host service
definition rendering inside a pod's environment, and the built wheel installing
into a clean venv. The recipes are in
[../guides/worktree-verification-recipes.md](../guides/worktree-verification-recipes.md).

Plain pytest, not pytest-bdd or Robot Framework. This repo's isolation, timeout
and sharding story is already pytest-shaped, and a second framework would need a
second isolation story rather than inheriting this one.

### Gating

Same shape as `KIROCREW_E2E_REQUIRE` above, and for the same reason.

- `KIROCREW_E2E_SCENARIOS` unset: every scenario skips. The suite boots a real
  pod, which is minutes and a service manager away from a bare `pytest`.
- `KIROCREW_E2E_SCENARIOS_REQUIRE=1`: every precondition skip becomes a FAILURE.
  A skip counts as a pass, so without this the job would report green having run
  zero scenarios.

`KIROCREW_E2E_SCENARIOS_REAL_AGENT=1` opts the agent turn onto the host's
signed-in `kiro-cli` instead of the packaged fake backend, and is REFUSED when no
`kiro-cli` is on PATH rather than being quietly served by the fake.

### The `pod-scenarios` job

Lives in `.github/workflows/nightly.yml`, matrix `[ubuntu-latest, macos-15]` with
`fail-fast: false` and `timeout-minutes: 40`. It is not a `needs:` of any publish
lane, so a scenario failure never holds up a nightly release and a release
failure never hides a scenario result. `workflow_dispatch` on the workflow makes
it runnable on a branch.

Steps, in order: build the checkout's `.venv` (a pod boots the CHECKOUT's own
`kirocrew`, and the suite refuses to fall back to a global one), `npm ci` plus
`npm run build` in `website/` staged into `src/kiro_crew/static/dist` (a pod
refuses to come up without a bundle), bring up a service manager, run the suite,
upload the pod logs on failure.

**The Linux leg has to CREATE its `systemd --user` session.** A hosted ubuntu
runner has no login session, so there is no per-user manager and no session bus,
and every pod verb refuses through `pod/runtime.py`'s `require_systemd`. The job
runs `sudo loginctl enable-linger "$USER"`, which is the exact remedy that
refusal prints. It then exports `XDG_RUNTIME_DIR=/run/user/<uid>` and
`DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/<uid>/bus` into `$GITHUB_ENV`,
because `systemctl --user` locates the manager through those two and a non-login
shell inherits neither. Linger creates the runtime directory asynchronously, so
the step polls for the bus socket rather than sleeping a fixed amount.

That step then PROVES the session in the log with `systemctl --user --version`,
`is-system-running`, and a `show-environment` that fails the job when the manager
cannot be reached. Without the proof a broken session degrades into six skipped
scenarios, and the REQUIRE marker would be the only thing between that and a
green nightly. A systemd-capable container (the pattern `docker-smoke.yml` uses)
is the fallback if a future runner image cannot linger; it is not needed today.

macOS needs no equivalent. The pod's launchd backend uses the per-user launchd
domain, which a runner session already has, so that leg only prints
`launchctl print user/<uid>` to keep the two logs readable side by side.

### The canary

Copied from `ci.yml`'s macOS peer-identity step: run by path with `-v -n0`, tee
to `pod-scenarios.log`, then `grep -qE '6 passed'` and fail the step otherwise.
An exit code cannot tell "every scenario passed" from "every scenario was never
collected", and a precondition-gated suite degrades into exactly that. **Raise
the expected count when you add a scenario.**

On failure the job uploads `pod-scenarios.log` plus the pod plane's artifact and
log files as `pod-scenarios-logs-<os>` (7 days, `if-no-files-found: ignore`). A
pod's boot refusal is only fully legible in its own journal or log files; the job
log carries just the tail `pod up` chose to print.

### Windows is a matrix add, not a rewrite

No scenario body contains a platform test. Only the pod fixture asks whether this
host can run pods, and it asks the pod's own `runtime.require_backend()`, which
dispatches systemd on Linux, launchd on macOS and Task Scheduler on Windows. That
last one now EXISTS, so the gate finds a backend on windows-latest and the add is
one more entry in `strategy.matrix.os` plus a service-manager step beside the two
above. What holds it back is a validated run rather than a missing backend, and
`test/test_pod_scenario_matrix.py` asserts that reason so this section cannot
quietly become false: delete its `PENDING_VALIDATION` entry and the test requires
the matrix entry. Deferred with it: whatever the Windows service manager needs to
make a pod's private API socket reachable, since `pod api` has no TCP fallback by
design.

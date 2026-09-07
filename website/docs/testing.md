# Frontend testing

Three test layers cover the dashboard. Pick the cheapest one that can actually
observe the thing you changed.

| Layer | Runner | Environment | Lives in |
|---|---|---|---|
| Unit and integration | vitest | `happy-dom`, network mocked by MSW | `integration/**/*.test.tsx`, `src/**/*.test.tsx` |
| Browser end-to-end | Playwright | real Chromium against a real gateway | `playwright/*.spec.ts` |
| Desktop shell | node:test | Node, no DOM | `electron/test/` |
| Component stories | Storybook | real Chromium, no gateway, every shipped theme | `src/**/*.stories.tsx`, config in `.storybook/` |

## Commands

```bash
npm run test              # test:website + test:electron (a jscpd pretest runs first)
npm run test:website      # vitest run --coverage
npm run test:integration  # vitest run integration/   (the MSW suite only)
npm run test:watch        # vitest, watch mode
npm run test:electron     # the Electron node:test suite
npm run test:playwright   # playwright test --headed --workers=1
npm run test:playwright:headless
npm run storybook         # component stories on http://127.0.0.1:6006 (loopback only)
npm run build-storybook   # static build into storybook-static/ (gitignored)
npx tsc -b                # the real type check
```

One trap worth knowing before you trust a green run:

- **`npm test` is wider than it looks.** It runs the Electron suite as well as the
  website suite, and the `pretest` hook runs a jscpd duplication check first, so
  `npm test` can fail on copy-paste before a single test executes.

## Choosing a layer

Reach for **vitest + MSW** by default: it is the fastest loop, and mocking at the
network boundary lets a test drive real components through real state. Use it for
component behavior, hooks, reducers, rendering, and anything you can assert from the
DOM.

Reach for **Playwright** only when the thing under test cannot exist without a real
browser and a real backend: navigation across routes, WebSocket lifecycle, iframe
and cross-origin behavior, file downloads, or a flow whose bug only appears once
real latency is involved. Every Playwright spec costs orders of magnitude more wall
clock than a vitest test, so a spec that could have been a vitest test is a
regression in suite speed.

Reach for the **Electron suite** for main-process code: window and menu wiring,
remote-host token resolution, and the launcher.

Reach for a **story** when the question is how a shared primitive LOOKS or behaves
in isolation — a variant matrix, a controlled component driven by hand, a Framer
Motion transition, or the same component under all shipped themes. Stories run in
a real browser with the real `src/index.css`, so they show what `happy-dom` cannot
(layout, animation, token resolution); the `theme` toolbar entry paints
`data-theme` + `data-mode` exactly as `applyTheme` in `useTheme.tsx` does, one
entry per theme × mode. They are not a test layer on their own yet: nothing in CI
renders them, so a story is a review surface and a place to reproduce a visual
bug, not proof of anything. A story file is development-only — it is excluded from
coverage, jscpd, and the hardcoded-string gate the same way a test file is, and
the production bundle never imports it.

Stories live in `src/stories/` (one file per component, `title:
'Primitives/<Name>'`) rather than beside `ui.tsx`, because that file is a barrel of
two dozen primitives and one story file per barrel would collide on title. Seven
primitives have a story; the rest do not, and nothing requires one yet. Whether
every shared primitive must carry a story is decided by the change that makes CI
render them — a requirement the tree does not meet has no gate behind it, so it is
not stated here until it can be enforced.

## MSW mocking

The vitest run loads `integration/setup.ts`, which installs the MSW server from
`integration/mocks/server.ts`. Handlers there define the gateway's HTTP surface, so
a component under test talks to a realistic API without a gateway running.

When a test fails with an unhandled request, the fix is almost always a missing
handler rather than a change to the component: add the endpoint to the mock server.

## What a `setupFiles` entry costs

`isolate: true` clears each worker's module registry between files, so vitest
re-fetches `integration/setup.ts` and its whole module graph **once per test
FILE** — ~1,400 of them. The total can exceed the cost of running the tests.
Reaching all 14 locale catalogs from it cost more in setup than the whole suite
spent running tests; owning them elsewhere is where the numbers below come from.

Measured back to back on one host at `maxWorkers: 3`, same `node_modules`, both
runs green:

| | all 14 in the setup graph | English only |
|---|---|---|
| wall | 1166.5s | **822.2s** (1.42x, -29.5%) |
| setup | 1363.5s | **371.6s** (3.67x less) |
| tests | 873.5s | 817.8s |
| setup as a share of the tests phase | 1.56x | 0.45x |

Quote the ratio rather than the wall clock: the absolute number moves with
`maxWorkers` and host load, and an earlier pair on a quieter host read 17m06s ->
11m30s for the same change.

**The cost is the per-module round trip, not the bytes.** Parsing all 14 catalogs
measures 32 ms and compiling them 1.5 ms, against ~690 ms per file of measured
saving — what is expensive is 14 vite-node module fetches crossing the fork IPC
channel, repeated per file. So the lever is always *fewer modules in the graph*;
making the same modules cheaper to parse buys nothing, which is why Vite's
`json.stringify` (on by default above 10 KB) does not help. Count modules, not
kilobytes, when you judge a setup import.

The test path is also heavier than the production bundle: `en-XA.json` is 1.33 MiB
and DEV-only, and `import.meta.env.DEV` is true under vitest, so it loads here and
is dropped from a release build.

That is why `src/i18n/index.ts` imports **English only** (726,947 bytes, 5.9% of the
12,242,932 authored bytes), `src/i18n/catalogs.ts` owns every catalog import, and
`src/i18n/all.ts` is the entry that registers them. Three rules hold that split in
place:

- **No non-English catalog import in `src/i18n/index.ts`** — that is the module
  `integration/setup.ts` and ~600 components import.
- **No all-catalogs import in `integration/setup.ts`**: neither `src/i18n/all` nor
  `src/i18n/catalogs`, and not transitively through a helper it pulls in.
- **A page entry point imports `initI18n` from `src/i18n/all`**, never from
  `src/i18n/index`. Both export the same signature so `tsc` accepts either, but
  through the English-only module the dashboard renders English to a user who
  picked Japanese: i18next falls back, nothing throws, no key renders raw.

`src/test/i18nAllLanguagesEntry.test.ts` gates all three by scanning the static
import graph, because none of them changes a test result on its own.

A test that needs a language other than English imports `src/i18n/all`; a test that
audits the whole catalog set imports `CATALOGS` from `src/i18n/catalogs`. Either way
a single file pays the load. None of this defers a load — `t()` is synchronous on
every path — it only settles which module owns the import.

Weigh anything else you put in the setup graph the same way: multiply it by the
file count first.

## Playwright: how it actually runs

The config is `playwright.config.ts`. The full table of its choices — including
`locale: 'en-US'`, which is a real dependency (most specs assert English prose,
the harness storage state carries no `mc-lang`, so a `zh-*` runner renders the
zh-CN catalog and fails them) — is in
[../../docs/ci/e2e-gate.md](../../docs/ci/e2e-gate.md#the-gateway-must-already-be-running-webserver-is-not-configured).

The one thing to know locally: **Playwright starts nothing.** `webServer` is
`undefined`, so a gateway must already be listening on `baseURL`
(`http://localhost:5476` unless `PLAYWRIGHT_BASE_URL` overrides it), or every spec
fails on connection refused.

**In CI these specs run through the backend gate, not through npm.**
`python setup.py test_e2e` boots a real gateway wired to a packaged fake ACP
backend and shells this suite against it, entirely offline. That is the harness to
match when you are debugging a CI-only failure.

## CI gates

- **jscpd** duplication check: copy-pasted code fails the build.
- Coverage is emitted as cobertura XML from `test:website`.
- `npx tsc -b` and eslint run as their own blocking steps.
- Coverage runs cap fork workers (`maxWorkers` in `vite.config.ts`) with a
  3072 MB old-space ceiling per worker. The cap leaves room for the Vitest
  coordinator, coverage maps, happy-dom state, and the operating system on a
  standard hosted runner; without it one fork per core exceeds host RAM and the
  kernel OOM-kills a worker, which surfaces as "Worker exited unexpectedly"
  even though every test passed.

Backend-side test determinism and suite-speed rules (they apply to the same CI run)
are in
[../../docs/system-specs/common/testing-conventions.md](../../docs/system-specs/common/testing-conventions.md).
The short version holds here too: never fix a flake with a rerun, a longer timeout,
or a weakened assertion. Poll for the condition you actually care about.

## Determinism: establish the state you assert on

Every CI-only failure this suite has produced so far reduces to one mistake: **the
test asserted against a state it did not establish**, and got away with it locally
because the component happened to be slower than the assertion. The shard runs four
workers under coverage, so "happened to be" stops holding. Two concrete shapes to
recognize — both have shipped as red shards.

**A mounted element is not a settled state.** `findBy*` proves a node rendered, not
that the async work behind it finished. A component that renders against a fallback
prop mounts *before* the effect that sets the real value, so its query has not even
been issued yet:

```tsx
// WRONG: the editor renders against `mainFile` before the open-main effect sets
// `currentFile`, so the read query is still disabled — `mockClear` clears nothing
// and the mount-time read lands afterwards, credited to the click.
await screen.findByLabelText('editor')
api.readFile.mockClear()
await user.click(fileRow)
expect(api.readFile).not.toHaveBeenCalled()

// RIGHT: wait for the thing the assertion is actually about.
await screen.findByLabelText('editor')
await waitFor(() => expect(api.readFile).toHaveBeenCalled())
api.readFile.mockClear()
```

Put that wait in the file's shared `…Ready()` helper rather than in the one test
that tripped over it: the barrier is wrong for every test using it, and the next
one to notice will be another red shard.

**A default DOM value the component overwrites is not a fixture.** If production
code writes `scrollTop`, `value`, or `open` on a timer or in an effect, a test that
relies on the initial value is racing it — and losing the race is silent, because
the state just reads as "already correct" and the branch under test never runs:

```tsx
// WRONG: the panel re-pins the scroller on 50/150/300ms timers after history
// lands. Once one has fired, this scroll reads as "already at the bottom" and the
// pill never renders — a `findByRole` timeout with no hint why.
fireEvent.scroll(scroller)

// STILL WRONG: a plain write is itself racing the same timers — one that runs
// after it puts the value right back.
scroller.scrollTop = 0
fireEvent.scroll(scroller)

// RIGHT: park it with an own accessor, so every read reports the parked value
// and the component's own writes are swallowed. The setter must exist: a
// getter-only property makes the component's strict-mode write throw instead.
Object.defineProperty(scroller, 'scrollTop', {
  configurable: true, get: () => 500, set: () => {},
})
fireEvent.scroll(scroller)
```

**Reproduce before you fix.** Both examples above were confirmed by *forcing* the
race locally — an `await new Promise(r => setTimeout(r, 400))` before the assertion,
or a `mockImplementation` that resolves on a timer — which turns a CI-only flake
into a deterministic local failure. Keep the forced delay in place while you verify
the fix, then remove it: a fix that only passes once the delay is gone has not been
shown to fix anything.

### What five full runs under load found

Five back-to-back `vitest run --coverage` passes on a Windows host that was also
running the backend suite (so every fork was starved) turned up 27 intermittent
cases and one deterministic Windows failure. They fall into eight shapes, and each
one is a rule:

- **A `React.lazy` boundary races the 1000ms default.** `findByTitle('Copy patch')`
  waits for `PierrePatch`'s header, which only exists once the
  `import('./PierreImpl')` chunk resolves and commits; under load that took longer
  than a second in 3 of 5 runs. The same for `PierreWorkspaceTree`'s `@pierre/trees`
  chunk. When the element you query sits behind a dynamic import, pass an explicit
  timeout (`{ timeout: 5000 }`) **and say which lazy boundary it is waiting for** in a
  comment, so the next reader knows the wait is a chunk load and not a guess.
- **Expensive engines built per test hit the 15s `testTimeout`.**
  `approvalOneShotDecisionRule.test.ts` constructed a new `ESLint` instance — which
  re-parses `eslint.config.js` and the whole plugin graph — inside `lint()`, for 17
  snippets. Build stateless engines once at module scope.
- **Fake filesystems and source scans must be Windows-neutral.** Two Electron suites
  failed on Windows every run: `crash-collector.test.js` keyed a fake `fs` by
  `path.join(...)` (backslashes) but passed the bare literal `"/reports"` to the code
  under test, so the prefix match read back empty; `window-lifecycle.test.js`
  scanned module source for `"}\n"` and a `core.autocrlf` checkout gave it `"}\r\n"`.
  Build fixture keys with `path.normalize`, and normalise `\r\n` before regex-scanning
  a source file. Run `npm test` on a Windows checkout before calling an Electron test
  done — CI's Electron job is Linux-only, so nothing else will.
- **`mkdtempSync` without an `after()` is a leak on every run.** `petOverlay.test.js`
  created a `cc-pet-test-*` user-data dir per `stubElectron()` call and its
  `restore()` never removed it; `store-rename-migration.test.js` did the same with
  `kc-store-*`. Together they left 64 directories in the real temp dir per run. Track
  every temp dir the file creates and remove them in one top-level `after()` (or in
  the helper's own `restore()`), with `fs.rmSync(dir, { recursive: true, force: true })`.
- **A wait must resolve on something only the settled state can produce.** The topbar
  metrics capsule paints `MEM —` as its *loading* placeholder and again as its
  "no valid reading" glyph, so `findByText(/MEM —/)` resolved on the pre-frame render
  and the synchronous `getByText(/CPU 25%/)` that followed found nothing. Wait for a
  reading only the loaded frame can carry (`CPU 25%`), then read the dash off that
  same frame. The same shape hides behind `findByTestId('share-dialog')` followed by
  `expect(dialog.contains(document.activeElement))`: the focus trap moves focus in a
  passive effect one tick after the commit the query resolved on, so "holds focus" is
  a `waitFor` condition, not a property of the first frame the dialog is in the DOM.
- **A real async chain behind the 1000ms default needs a named ceiling, not a longer
  guess.** The Privacy dialog mounts only after the import chapter's scan query
  resolves, an effect fires its auto-complete mutation and `onSuccess` flips parent
  state; the skills review body sits behind two chained queries (list, then detail
  once the `?review=` latch expands the row); the second error hand-off is not even
  attempted until the first has polled up to 300 x 10ms for its seed. Each is up to
  several seconds of legitimate work that a default `findBy*` / `waitFor` was asserting
  on before the code had reached it. Pass `{ timeout: 5000 }` **and name the chain**
  in a comment — that is what separates a bounded wait from a sleep. Never add a
  forced delay to production code to "prove" it and leave the probe behind.
- **Fake timers go on after the render settles, and off in `afterEach`.** The
  push-to-talk discard test arms a real 500ms timer on keydown, so under load the
  timer could win the race against the very next keyup. `vi.useFakeTimers()` fixes
  that — but installed *before* `renderAndWaitForInput`, its `waitFor` never advances
  and the test times out, and a timeout skips the `finally` that would have restored
  real timers, so every later test in the file inherits them and hangs the same way
  (15 of 22 red from one edit). Install fake timers after the last real-timer wait,
  and restore them in an `afterEach` that runs whether or not the test threw.
- **A synchronous simulation is CPU time, and a file-wide ceiling does not know
  that.** `MissionControlSceneCoverage`'s errand tests step the scene frame by frame
  through its own 1800+-frame idle timer with a full canvas draw per frame — ~8s of
  pure CPU on an idle laptop and past 15s the moment the host is shared. Nothing in
  them waits on a timer or a promise, so no `waitFor` change helps: give exactly
  those tests an explicit `it(name, { timeout }, fn)` ceiling, say in a comment why
  the work is real and how it is bounded (the frame budgets), and leave the file-wide
  `testTimeout` alone for everything else.

## Manual procedures

A few flows are deliberately not automated. They are documented rather than
scripted because the cost of automating them exceeds the value, and a deterministic
test already covers the underlying logic.

### Cron notification to chat navigation

The cron timer polls on a fixed interval, so an end-to-end assertion would have to
wait out a real cron fire (tens of seconds per case) for a UI behavior that is
already covered deterministically by
`integration/CronNotificationButtons.integration.test.tsx`. Verify by hand when you
change the notification buttons or the slot-linking logic:

1. Start a gateway and open the dashboard.
2. Add a one-shot cron job that produces output, and wait for it to fire.
3. From the notification, confirm **View last result** opens the result.
4. Repeat with a recurring job and confirm **Continue session** resumes the linked
   slot on subsequent fires.
5. Repeat with a non-persistent job and confirm it always offers **View last
   result** rather than a session to continue.
6. Confirm the linked slot still holds its earlier context.
7. Remove the test jobs.

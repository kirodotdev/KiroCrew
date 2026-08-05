# Frontend testing

Three test layers cover the dashboard. Pick the cheapest one that can actually
observe the thing you changed.

| Layer | Runner | Environment | Lives in |
|---|---|---|---|
| Unit and integration | vitest | `happy-dom`, network mocked by MSW | `integration/**/*.test.tsx`, `src/**/*.test.tsx` |
| Browser end-to-end | Playwright | real Chromium against a real gateway | `playwright/*.spec.ts` |
| Desktop shell | node:test | Node, no DOM | `electron/test/` |

## Commands

```bash
npm run test              # test:website + test:electron (a jscpd pretest runs first)
npm run test:website      # vitest run --coverage
npm run test:integration  # vitest run integration/   (the MSW suite only)
npm run test:watch        # vitest, watch mode
npm run test:electron     # the Electron node:test suite
npm run test:playwright   # playwright test --headed --workers=1
npm run test:playwright:headless
npx tsc -b                # the real type check
```

Two traps worth knowing before you trust a green run:

- **`npm run typecheck` checks ZERO files.** It runs `tsc --noEmit`, and the root
  `tsconfig.json` sets `"files": []` with project references, so nothing is checked
  and it always passes. Use `npx tsc -b`, which is what `npm run build` and CI run.
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

## MSW mocking

The vitest run loads `integration/setup.ts`, which installs the MSW server from
`integration/mocks/server.ts`. Handlers there define the gateway's HTTP surface, so
a component under test talks to a realistic API without a gateway running.

When a test fails with an unhandled request, the fix is almost always a missing
handler rather than a change to the component: add the endpoint to the mock server.

## Playwright: how it actually runs

The config is `playwright.config.ts`, and several of its choices surprise people:

- `testDir` is `./playwright`, and specs are `*.spec.ts` there.
- `baseURL` defaults to `http://localhost:5476`, overridable with
  `PLAYWRIGHT_BASE_URL`.
- **`webServer` is `undefined`.** Playwright starts nothing. A gateway must already
  be listening, or every spec fails on connection refused.
- Authentication is a setup project: it exchanges `PLAYWRIGHT_TOKEN` for a session
  cookie and saves it to `playwright/.auth/state.json`, which the other projects
  reuse as `storageState`.
- Specs that need a live model are tagged and **excluded by default** via
  `grepInvert`; set `PLAYWRIGHT_RUN_AGENT_SPECS=1` to include them. This keeps the
  default run credential-free and deterministic.
- CI pins `workers: 1`; local runs parallelize.

**In CI these specs run through the backend gate, not through npm.**
`python setup.py test_e2e` boots a real gateway wired to a packaged fake ACP
backend and shells this suite against it, entirely offline. That is the harness to
match when you are debugging a CI-only failure: see
[../../docs/ci/e2e-gate.md](../../docs/ci/e2e-gate.md).

## CI gates

- **jscpd** duplication check: copy-pasted code fails the build.
- Coverage is emitted as cobertura XML from `test:website`.
- `npx tsc -b` and eslint run as their own blocking steps.

Backend-side test determinism and suite-speed rules (they apply to the same CI run)
are in
[../../docs/system-specs/common/testing-conventions.md](../../docs/system-specs/common/testing-conventions.md).
The short version holds here too: never fix a flake with a rerun, a longer timeout,
or a weakened assertion. Poll for the condition you actually care about.

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

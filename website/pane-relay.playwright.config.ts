import { defineConfig, devices } from '@playwright/test'

// Dedicated config for the kc-46d84a incident fixture. It lives under website/
// so Node resolves @playwright/test from website/node_modules, and its testDir
// is playwright-fixtures/ (NOT the shared ./playwright suite) so the bespoke-
// topology spec is never collected by the credential-less :5476 gate. The
// topology harness (test/e2e/test_instance_pane_relay_e2e.py) passes the hub URL
// via env and invokes this config explicitly.
//
// `website/package.json` is `"type": "module"`, so this config loads as ESM:
// no `__dirname`. `testDir` is resolved relative to this file by Playwright.
export default defineConfig({
  testDir: 'playwright-fixtures',
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  workers: 1,
  retries: 0,
  reporter: [
    ['list'],
    ['json', { outputFile: process.env.PANE_JSON_REPORT || 'pane-report.json' }],
  ],
  timeout: 60_000,
  use: {
    baseURL: process.env.PANE_HUB_URL,
    // The hub serves a self-signed cert; the incident is about origin/mixed
    // content, not cert trust, so accept the cert and let the browser's real
    // mixed-content and CSP enforcement do its job.
    ignoreHTTPSErrors: true,
    ...devices['Desktop Chrome'],
    // The harness resolves a concrete Chromium/headless-shell binary and passes
    // it here, so the fixture does not depend on Playwright's pinned-revision
    // download resolving on the host.
    launchOptions: process.env.PANE_CHROMIUM_EXECUTABLE
      ? { executablePath: process.env.PANE_CHROMIUM_EXECUTABLE }
      : {},
    trace: 'retain-on-failure',
    video: process.env.PANE_VIDEO === '1' ? 'on' : 'off',
    navigationTimeout: 20_000,
    actionTimeout: 15_000,
  },
})

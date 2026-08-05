import { defineConfig, devices } from '@playwright/test'

export default defineConfig({
  testDir: './playwright',
  fullyParallel: true, // Enable parallel execution
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined, // Use default workers (parallel) except in CI
  reporter: process.env.CI ? 'html' : 'list', // 'list' shows test names as they run
  timeout: 30000, // 30 second timeout per test
  // Assertion (expect/poll) timeout stays at Playwright's 5s default globally.
  // Only the load-sensitive session-tags-folders "Folders inside columns (deep)"
  // suite needs headroom under box-CPU contention, and it applies { timeout: 15000 }
  // on its own polls -- keeping the global default tight so a genuine future
  // slowdown in any other spec still surfaces fast instead of silently passing
  // within a 3x window.
  // Gating: by default exclude @needs-agent (chat, fork — need a model/agent
  // turn the credential-less CI gateway lacks). The default run is therefore
  // the credential-less green set. PLAYWRIGHT_RUN_AGENT_SPECS=1 (set
  // by a harness that wires a fake ACP backend) re-includes the @needs-agent
  // specs. @needs-live-agent currently tags nothing: the last holder was the
  // budget-expiry soft-stop, which the fake does model ([[SLOW_NOACK]] withholds
  // the cancel ack), so it moved to @needs-agent. The tag stays wired as the
  // seam for a spec that genuinely needs real model semantics.
  grepInvert: process.env.PLAYWRIGHT_RUN_AGENT_SPECS
    ? /@needs-live-agent/
    : /@needs-agent|@needs-live-agent/,
  use: {
    baseURL: process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5476',
    // Pin the browser locale. Most specs assert English prose, and the app
    // resolves its language from `navigator.languages` when no explicit choice
    // is stored (src/i18n/detect.ts precedence: config `dashboard.language`
    // mirrored to localStorage `mc-lang`, then the browser tags, then `en`).
    // The harness storage state carries no `mc-lang`, so the browser tags
    // decide, and a zh-* runner would render the zh-CN catalog and fail those
    // assertions. Declaring en-US here makes that an explicit dependency
    // instead of an accident of the runner's environment.
    locale: 'en-US',
    trace: 'on-first-retry',
    video: process.env.PLAYWRIGHT_VIDEO === '1' ? 'on' : 'off',
    navigationTimeout: 10000, // 10 second navigation timeout
    actionTimeout: 10000, // 10 second action timeout
  },

  projects: [
    // Setup project: exchanges PLAYWRIGHT_TOKEN for a session cookie and
    // persists it to .auth/state.json. Keeps raw tokens out of test traces.
    { name: 'setup', testMatch: /auth\.setup\.ts/ },
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        storageState: process.env.PLAYWRIGHT_STORAGE_STATE || 'playwright/.auth/state.json',
      },
      dependencies: ['setup'],
    },
  ],

  // Note: Make sure kirocrew gateway is running on port 5476 before running tests
  // Run: kirocrew gateway
  webServer: undefined,
})

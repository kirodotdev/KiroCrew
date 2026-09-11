import { test as setup, expect } from '@playwright/test'

/**
 * Auth setup: exchanges PLAYWRIGHT_TOKEN for a session cookie once, then
 * persists storage state to .auth/state.json. All test projects reuse the
 * saved state so tokens never appear in test-level traces or videos.
 */

// Honour the same override playwright.config.ts reads for `storageState`, so a
// caller can point writer and reader at one non-default path. Without this the
// writer always clobbered the default file, which makes concurrent runs against
// separate ephemeral gateways race: each run's cookies are bound to its own
// port + token, so the last writer wins and the losers see "session expired".
const STATE_PATH = process.env.PLAYWRIGHT_STORAGE_STATE || 'playwright/.auth/state.json'

setup('authenticate', async ({ page }) => {
  const token = process.env.PLAYWRIGHT_TOKEN
  if (!token) {
    // Unauthenticated gateway. Still persist an empty storage state so the
    // `storageState` path in playwright.config.ts always resolves — otherwise
    // every test fails with ENOENT when running without a token.
    await page.context().storageState({ path: STATE_PATH })
    return
  }
  await page.goto(`/?token=${encodeURIComponent(token)}`, { waitUntil: 'domcontentloaded', timeout: 30000 })
  await page.waitForLoadState('load', { timeout: 30000 })
  if (process.env.KIROCREW_E2E_EPHEMERAL === '1') {
    const configResponse = await page.request.get('/api/config/kirocrew')
    expect(configResponse.ok(), await configResponse.text()).toBeTruthy()
    const config = await configResponse.json()
    const backend = config.agent.acp_backend
    expect(typeof backend).toBe('string')
    // The disposable minimal fixture starts with sandbox off. Enable real
    // isolation, then reapply its backend through the owner API: that field's
    // normal refresh rebuilds the factory which captured sandbox at startup.
    // The backend stays the same and no private admission check is bypassed.
    for (const change of [
      { path: 'agent.sandbox', value: 'auto' },
      { path: 'agent.acp_backend', value: backend },
    ]) {
      const response = await page.request.patch('/api/config/kirocrew', { data: change })
      expect(response.ok(), await response.text()).toBeTruthy()
    }
  }
  // Dismiss the first-run theme-onboarding overlay. App.tsx gates the "Choose
  // your look" modal on the `mc-onboarded` localStorage flag; a fresh browser
  // context has no flag, so the modal would overlay the shell and intercept
  // every spec's interactions. Persisting it into storageState here lets all
  // test projects inherit it (the ephemeral gateway port makes a committed
  // state.json localStorage entry useless across runs, so it must be set live).
  await page.evaluate(() => window.localStorage.setItem('mc-onboarded', '1'))
  await page.context().storageState({ path: STATE_PATH })
})

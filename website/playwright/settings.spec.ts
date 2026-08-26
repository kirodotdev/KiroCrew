import { test, expect } from '@playwright/test'
import { MAX_RECENT_TINT_COUNT } from '../src/utils/recencyTint'

// The stepper's upper bound comes from the source of truth rather than a copy,
// so raising or lowering it there cannot leave this spec asserting a stale
// bound. Playwright transpiles specs with its own pipeline, so importing across
// the playwright/ and src/ boundary resolves even though src/ is a separate
// tsconfig project. recencyTint.ts is a dependency-free module of pure helpers,
// so this pulls in no component or DOM code.
const MAX_TINT = MAX_RECENT_TINT_COUNT

/**
 * Settings page (/settings) — route-level Playwright coverage.
 *
 * Covers:
 *  (a) Page load with tab strip rendering
 *  (b) Tab navigation + ?tab= query-param round-trip
 *  (c) Config mutation round-trip via the "Highlight recent sessions" stepper
 *      (dashboard.recent_tint_count — server-persisted, cosmetic, test-safe)
 *  (d) Redirect contracts: /overview → /settings?tab=overview,
 *      /instances → /settings?tab=instances
 */
test.describe('Settings Page', () => {
  test('loads and renders the tab strip with known tabs', async ({ page }) => {
    await page.goto('/settings', { waitUntil: 'domcontentloaded' })

    // The page title rendered by SidePanelLayout inside the nav sidebar
    await expect(page.getByText('Settings').first()).toBeVisible({ timeout: 10000 })

    // Verify a selection of real tab labels exist as clickable buttons
    await expect(page.getByRole('button', { name: 'Overview', exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Display', exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Chat', exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Remote Crew', exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'About', exact: true })).toBeVisible()
  })

  test('defaults to the overview tab when no ?tab= param', async ({ page }) => {
    await page.goto('/settings', { waitUntil: 'domcontentloaded' })
    // Overview is the first tab and renders when no param is present.
    // The content area heading should show "Overview"
    await expect(page.getByRole('heading', { name: 'Overview' }).or(
      page.locator('div').filter({ hasText: /^Overview$/ }).first()
    )).toBeVisible({ timeout: 10000 })
    // URL should NOT have ?tab=overview (SidePanelLayout strips the default)
    expect(page.url()).not.toContain('tab=')
  })

  test('navigating between tabs updates the ?tab= query param', async ({ page }) => {
    await page.goto('/settings', { waitUntil: 'domcontentloaded' })

    // Click Display tab button
    const displayTab = page.getByRole('button', { name: 'Display', exact: true })
    await expect(displayTab).toBeVisible({ timeout: 10000 })
    await displayTab.click()

    // URL should update to ?tab=display
    await expect(page).toHaveURL(/[?&]tab=display/)

    // Click About tab button
    await page.getByRole('button', { name: 'About', exact: true }).click()
    await expect(page).toHaveURL(/[?&]tab=about/)
  })

  test('direct navigation to ?tab=chat renders the Chat panel', async ({ page }) => {
    await page.goto('/settings?tab=chat', { waitUntil: 'domcontentloaded' })

    // The Chat panel should render its content — check for a known setting label
    // ChatPanel contains the "Timestamps" toggle and other settings
    await expect(page.locator('[data-setting-label]').first()).toBeVisible({ timeout: 10000 })
    // URL should still have ?tab=chat
    expect(page.url()).toContain('tab=chat')
  })

  test('config mutation round-trip: recent_tint_count via Display stepper', async ({ page, request }) => {
    // Read the current value via API
    const before = await (await request.get('/api/config/kirocrew')).json()
    const originalCount: number = before?.dashboard?.recent_tint_count ?? 0

    // Navigate to the Display tab
    await page.goto('/settings?tab=display', { waitUntil: 'domcontentloaded' })

    // Find the "Highlight recent sessions" stepper
    const field = page.locator('[data-setting-label="Highlight recent sessions"]')
    await expect(field).toBeVisible({ timeout: 10000 })

    // Step in whichever direction is actually available. clampTintCount
    // (recencyTint.ts:21) floors the value into [0, MAX_RECENT_TINT_COUNT],
    // and DisplayPanel passes no `disabled` prop, so at the maximum the
    // Increase button is still clickable but the persisted value stays at the
    // bound -- an assertion of originalCount + 1 would then poll for a value
    // one past the bound until it times out. The harness always starts at the 0
    // default so increment is the normal path, but a developer running this
    // against a live gateway may sit at either bound.
    const atMax = originalCount >= MAX_TINT
    const delta = atMax ? -1 : 1
    const btn = field.getByRole('button', { name: atMax ? 'Decrease' : 'Increase' })
    await btn.click()

    // Read back via API to verify server-side persistence. expect.poll owns the
    // wait for the mutation to reach the server -- no fixed sleep needed.
    await expect.poll(async () => {
      const cfg = await (await request.get('/api/config/kirocrew')).json()
      return cfg?.dashboard?.recent_tint_count
    }, { timeout: 5000 }).toBe(originalCount + delta)

    // Restore original value to not pollute other tests
    await request.fetch('/api/config/kirocrew', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      data: JSON.stringify({ path: 'dashboard.recent_tint_count', value: originalCount }),
    })
  })

  test('/overview redirects to /settings?tab=overview', async ({ page }) => {
    await page.goto('/overview', { waitUntil: 'domcontentloaded' })
    // React Router Navigate with replace — URL should land on /settings
    // (overview is default so ?tab=overview may or may not be stripped)
    await page.waitForURL('**/settings**', { timeout: 10000 })
    // The Overview panel content should render (health status)
    await expect(page.getByText(/All systems running|Connecting|Reconnecting/)).toBeVisible({ timeout: 10000 })
  })

  test('/instances redirects to /settings?tab=instances', async ({ page }) => {
    await page.goto('/instances', { waitUntil: 'domcontentloaded' })
    await page.waitForURL('**/settings?tab=instances', { timeout: 10000 })
    // Remote Crew panel should render — check for the tab being active
    await expect(page.getByRole('button', { name: 'Remote Crew', exact: true })).toBeVisible({ timeout: 5000 })
  })
})

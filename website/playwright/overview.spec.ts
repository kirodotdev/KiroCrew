import { test, expect } from '@playwright/test'

// Overview after the mission-control rewrite: no sub-tab bar. The page is a
// health hero + stat tiles + summary cards whose "View details" actions drill
// into the Usage and WakaTime reports (URL-backed ?view=). Memory is its own
// Settings section now, one entry below Overview in the Settings list.
test.describe('Overview Page E2E Tests', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/overview', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(1000)
  })

  test('shows the mission-control landing (hero, tiles, summary cards)', async ({ page }) => {
    await expect(page.getByText(/All systems running|Connecting…|Reconnecting…/)).toBeVisible({ timeout: 10000 })
    await expect(page.getByText('Uptime')).toBeVisible()
    // Old sub-tab bar is gone.
    await expect(page.getByRole('button', { name: 'KiroCrew Config', exact: true })).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Import/Export', exact: true })).toHaveCount(0)
    // Two summary cards (Usage, WakaTime) expose the same drill-in verb; the
    // Memory card left with the memory page, which has its own Settings entry.
    await expect(page.getByRole('button', { name: 'View details' })).toHaveCount(2)
    await expect(page.getByRole('heading', { name: /^Memory/, level: 3 })).toHaveCount(0)
  })

  test('opens the Memory page from Settings and back to Overview', async ({ page }) => {
    const sections = page.getByRole('navigation').filter({ has: page.getByRole('button', { name: 'Overview', exact: true }) })
    await sections.getByRole('button', { name: 'Memory', exact: true }).click()
    await expect(page).toHaveURL(url => url.pathname === '/settings/memory')
    await expect(page.getByRole('tablist', { name: 'Memory for Shared · Global Memory V1', exact: true })).toBeVisible({ timeout: 5000 })
    await expect(page.getByRole('combobox', { name: 'Memory store', exact: true })).toContainText('Global memory')
    await sections.getByRole('button', { name: 'Overview', exact: true }).click()
    await expect(page.getByText(/All systems running|Connecting…|Reconnecting…/)).toBeVisible({ timeout: 5000 })
  })

  test('an old Overview memory link lands on the Memory page', async ({ page }) => {
    await page.goto('/settings/overview?view=memory', { waitUntil: 'domcontentloaded' })
    await expect(page).toHaveURL(url => url.pathname === '/settings/memory' && !url.searchParams.has('view'))
    await expect(page.getByRole('tablist', { name: 'Memory for Shared · Global Memory V1', exact: true })).toBeVisible({ timeout: 5000 })
  })

  test('Memory page exposes the manual summarize action', async ({ page }) => {
    await page.goto('/settings/memory', { waitUntil: 'domcontentloaded' })
    await page.getByRole('tab', { name: 'Settings', exact: true }).click()
    await expect(page.getByText(/^Memory Settings/i)).toBeVisible({ timeout: 5000 })
    const summarize = page.getByRole('button', { name: /summarize now/i })
    await expect(summarize).toBeVisible({ timeout: 5000 })
    await expect(summarize).toBeEnabled()
  })

  test('drills into WakaTime and back', async ({ page }) => {
    // WakaTime card renders second (index 1).
    await page.getByRole('button', { name: 'View details' }).nth(1).click()
    await expect(page.getByRole('button', { name: 'Back to Overview' })).toBeVisible({ timeout: 5000 })
    await page.getByRole('button', { name: 'Back to Overview' }).click()
    await expect(page.getByText(/All systems running|Connecting…|Reconnecting…/)).toBeVisible({ timeout: 5000 })
  })

  test('drills into Usage and back', async ({ page }) => {
    await page.getByRole('button', { name: 'View details' }).nth(0).click()
    // The drill-in chrome (back affordance) is the reliable marker — the
    // Usage report body varies with provider billing capabilities.
    await expect(page.getByRole('button', { name: 'Back to Overview' })).toBeVisible({ timeout: 5000 })
    await page.getByRole('button', { name: 'Back to Overview' }).click()
    await expect(page.getByText(/All systems running|Connecting…|Reconnecting…/)).toBeVisible({ timeout: 5000 })
  })
})

import { test, expect } from '@playwright/test'

// Overview after the mission-control rewrite: no sub-tab bar. The page is a
// health hero + stat tiles + two summary cards whose "View details" actions
// drill into the Usage report and the Memory browser (URL-backed ?view=).
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
    // Both summary cards expose the same drill-in verb.
    await expect(page.getByRole('button', { name: 'View details' })).toHaveCount(2)
  })

  test('drills into the Memory browser and back', async ({ page }) => {
    // Usage card renders first, Memory second.
    await page.getByRole('button', { name: 'View details' }).nth(1).click()
    await expect(page.getByRole('heading', { name: /memory settings/i })).toBeVisible({ timeout: 5000 })
    await page.getByRole('button', { name: 'Back to Overview' }).click()
    await expect(page.getByText(/All systems running|Connecting…|Reconnecting…/)).toBeVisible({ timeout: 5000 })
  })

  test('Memory browser exposes the manual summarize action', async ({ page }) => {
    await page.getByRole('button', { name: 'View details' }).nth(1).click()
    const summarize = page.getByRole('button', { name: /summarize now/i })
    await expect(summarize).toBeVisible({ timeout: 5000 })
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

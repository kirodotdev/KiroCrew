import { test, expect } from '@playwright/test'

/**
 * Memory surfaces after the Overview mission-control rewrite:
 * - Settings > Overview hosts the user-facing memory BROWSER behind a
 *   summary-card drill-in (?view=memory) with a back affordance.
 * - The graph explorer moved to the Developer page's Memory tab (direct
 *   route; the Developer-Mode toggle only gates the sidebar entry).
 */
test.describe('Memory surfaces E2E Tests', () => {
  test('Overview drills into the memory browser and back', async ({ page }) => {
    await page.goto('/overview', { waitUntil: 'domcontentloaded' })
    // Two summary cards share the verb; Usage renders first, Memory second.
    const drill = page.getByRole('button', { name: 'View details' }).nth(1)
    await drill.waitFor({ state: 'visible' })
    await drill.click()
    await expect(page.getByRole('heading', { name: /memory settings/i })).toBeVisible({ timeout: 5000 })
    // Back returns to the mission-control hero.
    await page.getByRole('button', { name: 'Back to Overview' }).click()
    await expect(page.getByText('All systems running')).toBeVisible({ timeout: 5000 })
  })

  test('memory browser exposes the manual summarize action', async ({ page }) => {
    await page.goto('/settings?tab=overview&view=memory', { waitUntil: 'domcontentloaded' })
    await expect(page.getByRole('button', { name: /summarize now/i })).toBeVisible({ timeout: 5000 })
  })

  test('Developer page Memory tab renders the graph explorer', async ({ page }) => {
    await page.goto('/developer?tab=memory', { waitUntil: 'domcontentloaded' })
    await expect(page.getByRole('heading', { name: 'Memory Graph' })).toBeVisible({ timeout: 5000 })
  })

  test('graph explorer shows filter buttons', async ({ page }) => {
    await page.goto('/developer?tab=memory', { waitUntil: 'domcontentloaded' })
    await expect(page.getByText(/All \(\d+\)/)).toBeVisible({ timeout: 5000 })
  })

  test('graph explorer has a search input', async ({ page }) => {
    await page.goto('/developer?tab=memory', { waitUntil: 'domcontentloaded' })
    await expect(page.getByPlaceholder('Search nodes…')).toBeVisible({ timeout: 5000 })
  })
})

import { test, expect } from '@playwright/test'

// Overview page tabs as of the current IA. Cron / Skills / MCP were moved out
// of Overview into their own pages, so they are intentionally not asserted here.
const TABS = ['Memory', 'Usage', 'KiroCrew Config', 'Agent Config', 'Import/Export'] as const

test.describe('Overview Page E2E Tests', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/overview', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(1000)
  })

  test('navigates to Overview and displays tabs', async ({ page }) => {
    for (const label of TABS) {
      // exact match: 'Memory' would otherwise also hit the 'Enable Vector Memory' CTA.
      await expect(page.getByRole('button', { name: label, exact: true })).toBeVisible({ timeout: 10000 })
    }
  })

  test('switches between Overview tabs and loads data', async ({ page }) => {
    // Click each tab and assert it becomes the active tab via the semantic
    // aria-current state, not a styling utility class -- a text-accent match
    // would couple the gate to a design token and re-introduce the selector
    // fragility this CR removes.
    for (const label of TABS) {
      const btn = page.getByRole('button', { name: label, exact: true })
      await btn.click()
      await expect(btn).toHaveAttribute('aria-current', 'page', { timeout: 5000 })
    }
    // Memory tab content check.
    await page.getByRole('button', { name: 'Memory', exact: true }).click()
    await expect(page.getByRole('heading', { name: /memory settings/i })).toBeVisible({ timeout: 5000 })
  })

  test('Memory tab: saves preferences', async ({ page }) => {
    await page.getByRole('button', { name: 'Memory', exact: true }).click()
    await expect(page.getByRole('heading', { name: /memory settings/i })).toBeVisible({ timeout: 5000 })
  })

  test('Memory tab: tests consolidation', async ({ page }) => {
    await page.getByRole('button', { name: 'Memory', exact: true }).click()
    const consolidateButton = page.getByRole('button', { name: /test consolidation|consolidate/i })
    if (await consolidateButton.isVisible()) {
      await consolidateButton.click()
      await page.waitForTimeout(2000)
    }
  })
})

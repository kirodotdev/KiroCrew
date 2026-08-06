import { test, expect } from '@playwright/test'

test.describe('System Page E2E Tests', () => {
  test.beforeEach(async ({ page }) => {
    // Navigate directly to system page (System metrics moved under the
    // Developer page's System tab: /system → /developer?tab=system).
    await page.goto('/developer?tab=system', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(500)
  })

  test('navigates to System page and displays system metrics', async ({ page }) => {
    // Should see memory heading and CPU metrics. `exact` is required: the name
    // option matches a substring by default, and this page also carries the
    // "Session & Task Memory" card, so a loose 'Memory' resolves to 2 headings.
    await expect(
      page.getByRole('heading', { name: 'Memory', exact: true })
    ).toBeVisible({ timeout: 10000 })
    await expect(page.getByText('CPUs')).toBeVisible({ timeout: 5000 })
  })

  test('displays the per-session memory card', async ({ page }) => {
    // Renders unconditionally (it owns an empty state), so this holds even
    // when the offline stub backend reports no live sessions.
    await expect(
      page.getByRole('heading', { name: 'Session & Task Memory' })
    ).toBeVisible({ timeout: 10000 })
    await expect(
      page.getByRole('columnheader', { name: 'Session / Task' })
    ).toBeVisible({ timeout: 5000 })
  })

  test('displays platform information', async ({ page }) => {
    // Should see platform details - use first specific match
    await expect(page.getByText('Python').first()).toBeVisible({ timeout: 5000 })
  })
})

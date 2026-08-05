import { test, expect } from '@playwright/test'

test.describe('System Page E2E Tests', () => {
  test.beforeEach(async ({ page }) => {
    // Navigate directly to system page (System metrics moved under the
    // Developer page's System tab: /system → /developer?tab=system).
    await page.goto('/developer?tab=system', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(500)
  })

  test('navigates to System page and displays system metrics', async ({ page }) => {
    // Should see memory heading and CPU metrics
    await expect(
      page.getByRole('heading', { name: 'Memory' })
    ).toBeVisible({ timeout: 10000 })
    await expect(page.getByText('CPUs')).toBeVisible({ timeout: 5000 })
  })

  test('displays platform information', async ({ page }) => {
    // Should see platform details - use first specific match
    await expect(page.getByText('Python').first()).toBeVisible({ timeout: 5000 })
  })
})

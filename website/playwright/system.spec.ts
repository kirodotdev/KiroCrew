import { test, expect } from '@playwright/test'

// The System page is a task manager with three planes. These assertions
// deliberately target only chrome that renders with NO data — the tab rail,
// column headers, the Group-by control, the resource rail — because the offline
// stub backend reports no live sessions. Asserting on a row would make the suite
// depend on the stub having spawned something, which it never does.
test.describe('System Page E2E Tests', () => {
  test.beforeEach(async ({ page }) => {
    // System metrics live under the Developer page's System tab.
    await page.goto('/developer?tab=system', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(500)
  })

  test('offers the three System planes as a tab rail', async ({ page }) => {
    const rail = page.getByRole('tablist', { name: /System planes/i })
    await expect(rail).toBeVisible({ timeout: 10000 })
    for (const name of ['Sessions', 'Performance', 'Services']) {
      await expect(rail.getByRole('tab', { name, exact: true })).toBeVisible({ timeout: 5000 })
    }
    // Exactly one plane is current — a rail reporting two selected tabs would
    // mean the underline and the panel disagree about where the user is.
    await expect(rail.getByRole('tab', { selected: true })).toHaveCount(1)
  })

  test('lands on the Sessions plane with its table and Group-by control', async ({ page }) => {
    await expect(
      page.getByRole('columnheader', { name: 'Session / Task' })
    ).toBeVisible({ timeout: 10000 })
    // Every resource is a COLUMN here rather than a view mode, so the two that
    // the mockup puts on first paint must both be present at once.
    await expect(page.getByRole('columnheader', { name: 'Memory' })).toBeVisible({ timeout: 5000 })
    await expect(page.getByRole('columnheader', { name: 'CPU' })).toBeVisible({ timeout: 5000 })
    await expect(page.getByText('Group by')).toBeVisible({ timeout: 5000 })
  })

  test('shows machine and platform detail on the Performance plane', async ({ page }) => {
    await page.getByRole('tab', { name: 'Performance', exact: true }).click()
    // The machine-identity strip is where the static facts moved when the flat
    // card grid was replaced; Python is one of them.
    await expect(page.getByText('Python').first()).toBeVisible({ timeout: 10000 })
    await expect(page.getByRole('navigation', { name: /Resources/i })).toBeVisible({ timeout: 5000 })
  })

  test('shows the gateway process on the Services plane', async ({ page }) => {
    await page.getByRole('tab', { name: 'Services', exact: true }).click()
    await expect(page.getByText('PID').first()).toBeVisible({ timeout: 10000 })
  })
})

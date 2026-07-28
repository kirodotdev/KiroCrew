import { test, expect } from '@playwright/test'

const HARNESS_GATEWAY = !!process.env.KIROCREW_E2E_EPHEMERAL

test.describe('Notifications Page', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/notifications', { waitUntil: 'domcontentloaded' })
    // Wait for the page subtitle to render -- unique to this page
    await expect(page.getByText('All agent activity, cron results, webhooks, and approvals')).toBeVisible({ timeout: 10000 })
  })

  test('renders page header and subtitle', async ({ page }) => {
    // The page header title -- use the subtitle as the unique anchor since
    // "Notifications" also appears in the topbar bell button text
    // PageHeader (ui.tsx:257) renders its title as a plain <div>, not a heading, and
    // "Notifications" also appears in the topbar bell button -- so the subtitle is the
    // only unique, non-class-coupled anchor for "the header rendered".
    await expect(page.getByText('All agent activity, cron results, webhooks, and approvals')).toBeVisible()
  })

  test('displays stat cards with zero counts on empty fixture', async ({ page }) => {
    // The stat cards show Total, Unread, Cron, Hooks, Heartbeat -- all 0 on minimal fixture
    const totalCard = page.locator('.stat-accent').filter({ hasText: 'Total' })
    await expect(totalCard.locator('.text-2xl')).toContainText('0')
    const unreadCard = page.locator('.stat-accent').filter({ hasText: 'Unread' })
    await expect(unreadCard.locator('.text-2xl')).toContainText('0')
    const cronCard = page.locator('.stat-accent').filter({ hasText: 'Cron' })
    await expect(cronCard.locator('.text-2xl')).toContainText('0')
  })

  test('shows empty state message when no notifications', async ({ page }) => {
    await expect(page.getByText('No notifications')).toBeVisible()
    await expect(page.getByText('Activity will appear here')).toBeVisible()
  })

  test('renders category filter chips with correct labels', async ({ page }) => {
    const filterGroup = page.getByRole('group', { name: 'Filter notifications by kind' })
    await expect(filterGroup).toBeVisible()

    const chipLabels = ['All', 'Cron', 'Hooks', 'Heartbeat', 'Agent', 'Approval', 'Subagent', 'Tasks']
    for (const label of chipLabels) {
      await expect(filterGroup.getByRole('button', { name: label, exact: true })).toBeVisible()
    }
  })

  test('toggling a category chip changes its pressed state', async ({ page }) => {
    const filterGroup = page.getByRole('group', { name: 'Filter notifications by kind' })
    const cronChip = filterGroup.getByRole('button', { name: 'Cron' })

    // Initially all chips are active (aria-pressed=true)
    await expect(cronChip).toHaveAttribute('aria-pressed', 'true')

    // Click to deactivate
    await cronChip.click()
    await expect(cronChip).toHaveAttribute('aria-pressed', 'false')

    // Click again to re-activate
    await cronChip.click()
    await expect(cronChip).toHaveAttribute('aria-pressed', 'true')
  })

  test('clicking All chip deselects all categories and shows appropriate empty state', async ({ page }) => {
    const filterGroup = page.getByRole('group', { name: 'Filter notifications by kind' })
    const allChip = filterGroup.getByRole('button', { name: 'All' })

    // "All" acts as a toggle: when all are on, clicking clears all
    await allChip.click()

    // Now the empty state should mention categories
    await expect(page.getByText('No categories selected')).toBeVisible()
  })

  test('GET /api/notifications returns correct structure for empty state', async ({ request }) => {
    const resp = await request.get('/api/notifications')
    expect(resp.ok()).toBeTruthy()
    const body = await resp.json()
    expect(body).toHaveProperty('notifications')
    expect(body).toHaveProperty('unread')
    expect(body.notifications).toEqual([])
    expect(body.unread).toBe(0)
  })

  test('search input filters and shows appropriate empty message', async ({ page }) => {
    const searchInput = page.getByRole('textbox', { name: 'Search…' })
    await expect(searchInput).toBeVisible()

    // Type a search term -- with no notifications, the empty state text changes
    await searchInput.fill('nonexistent')
    await expect(page.getByText('Try a different search')).toBeVisible()

    // Clear the search -- empty state should revert
    await searchInput.fill('')
    await expect(page.getByText('Activity will appear here')).toBeVisible()
  })

  // /api/notifications/clear is global: it deletes EVERY notification, not just
  // ones this spec created, and the endpoint offers no way to scope it. Gated on
  // the explicit ephemeral-harness marker, same contract as session-tags-e2e.
  // test/test_playwright_e2e.py sets KIROCREW_E2E_EPHEMERAL for the throwaway
  // tmp-home gateway it spawns, so this still runs in CI. Token presence is NOT
  // a safe signal -- it is also the normal state for a real token-protected
  // gateway, so a developer pointing this suite at their live gateway to debug a
  // failure must never lose their notification history.
  test('POST /api/notifications/clear round-trips correctly on empty state', async ({ request }) => {
    test.skip(
      !HARNESS_GATEWAY,
      'destructive notification wipe requires the ephemeral harness gateway (KIROCREW_E2E_EPHEMERAL)',
    )
    // Clear on empty is idempotent -- verifies the full HTTP round-trip
    const resp = await request.post('/api/notifications/clear')
    expect(resp.ok()).toBeTruthy()
    const body = await resp.json()
    expect(body.ok).toBe(true)

    // Verify state is still empty after the clear
    const listResp = await (await request.get('/api/notifications')).json()
    expect(listResp.notifications).toEqual([])
    expect(listResp.unread).toBe(0)
  })
})

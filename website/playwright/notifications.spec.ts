import { test, expect } from '@playwright/test'

const HARNESS_GATEWAY = !!process.env.KIROCREW_E2E_EPHEMERAL

test.describe('Notifications Page', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/notifications', { waitUntil: 'domcontentloaded' })
    // Structural mount gate, no copy pinned.
    await expect(page.getByTestId('page-header')).toBeVisible({ timeout: 10000 })
  })

  test('renders page header and subtitle', async ({ page }) => {
    // data-testid="page-title" disambiguates the header from the topbar bell button,
    // which also renders the word "Notifications".
    await expect(page.getByTestId('page-title')).toHaveText('Notifications')
    await expect(page.getByTestId('page-subtitle')).toBeVisible()
  })

  test('displays stat cards with zero counts on empty fixture', async ({ page }) => {
    // The stat cards show Total, Unread, Cron, Hooks, Heartbeat -- all 0 on minimal fixture
    const totalCard = page.getByTestId('stat-card').filter({ hasText: 'Total' })
    await expect(totalCard.getByTestId('stat-card-value')).toContainText('0')
    const unreadCard = page.getByTestId('stat-card').filter({ hasText: 'Unread' })
    await expect(unreadCard.getByTestId('stat-card-value')).toContainText('0')
    const cronCard = page.getByTestId('stat-card').filter({ hasText: 'Cron' })
    await expect(cronCard.getByTestId('stat-card-value')).toContainText('0')
  })

  test('shows empty state message when no notifications', async ({ page }) => {
    await expect(page.getByTestId('notification-feed-empty-title')).toHaveText('No notifications')
    // The subtitle distinguishes WHICH empty state rendered (search miss vs
    // genuinely empty), so the text is load-bearing -- but matched loosely and
    // scoped to the element rather than searched for across the page.
    await expect(page.getByTestId('notification-feed-empty-subtitle')).toHaveText(/activity will appear here/i)
  })

  test('renders no per-kind filter controls', async ({ page }) => {
    // The feed deliberately has no kind filter: free-text search is the only
    // narrowing mechanism. Pinned here because the chips previously hid
    // unknown-kind notifications whenever any one of them was deselected.
    await expect(page.getByRole('group', { name: 'Filter notifications by kind' })).toHaveCount(0)
    // 'All' is deliberately NOT in this list: it is still the label of the
    // mark-all-as-read button in the search row (rendered when unread > 0), so
    // asserting its absence would pin this test to the fixture's unread count.
    // The stat cards labelled Cron/Hooks/Heartbeat are plain divs (StatCard only
    // takes role="button" when given an onClick), so they cannot match either.
    for (const label of ['Cron', 'Hooks', 'Heartbeat', 'Agent', 'Approval', 'Subagent', 'Tasks']) {
      await expect(page.getByRole('button', { name: label, exact: true })).toHaveCount(0)
    }
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
    await expect(page.getByTestId('notification-feed-empty-subtitle')).toHaveText(/try a different search/i)

    // Clear the search -- empty state should revert
    await searchInput.fill('')
    await expect(page.getByTestId('notification-feed-empty-subtitle')).toHaveText(/activity will appear here/i)
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

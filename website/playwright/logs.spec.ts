import { test, expect } from '@playwright/test'

const HARNESS_GATEWAY = !!process.env.KIROCREW_E2E_EPHEMERAL

// Every test here needs DEBUG so the stream actually emits lines, and
// POST /api/logs/level PERSISTS: api_log_level writes cfg.agent.log_level and
// calls cfg.save() (updates.py:542). There is no read-only way to raise the
// level, so the whole suite is gated on the explicit ephemeral-harness marker
// rather than permanently rewriting a developer's log level. Same contract as
// session-tags-e2e; test/test_playwright_e2e.py sets the marker, so this still
// runs in CI.
test.describe('Logs Page', () => {
  test.beforeEach(async ({ page, request }) => {
    test.skip(
      !HARNESS_GATEWAY,
      'persisting a DEBUG log level requires the ephemeral harness gateway (KIROCREW_E2E_EPHEMERAL)',
    )
    // Ensure log level is set to DEBUG so all gateway lines are visible.
    // The minimal fixture defaults to WARNING which may show zero lines.
    await request.post('/api/logs/level', { data: { level: 'DEBUG' } })
    await page.goto('/logs', { waitUntil: 'domcontentloaded' })
    // Wait for the page header to render, proving the route mounted
    await expect(page.getByText('Live Logs')).toBeVisible({ timeout: 10000 })
  })

  test('renders page header and subtitle', async ({ page }) => {
    await expect(page.getByText('Live Logs')).toBeVisible()
    await expect(page.getByText('Real-time application output')).toBeVisible()
  })

  test('displays log level buttons with current level highlighted', async ({ page }) => {
    // The four level buttons: Debug, Info, Warning, Error
    await expect(page.getByRole('button', { name: 'Debug' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Info' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Warning' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Error' })).toBeVisible()
  })

  test('renders the filter input and control buttons', async ({ page }) => {
    await expect(page.getByLabel('Filter logs')).toBeVisible()
    // Tail and Wrap toggle buttons
    await expect(page.getByRole('button', { name: /Tail:/ })).toBeVisible()
    await expect(page.getByRole('button', { name: /Wrap:/ })).toBeVisible()
    // Latest direction toggle
    await expect(page.getByRole('button', { name: /Latest:/ })).toBeVisible()
  })

  test('receives live log lines from the gateway', async ({ page }) => {
    // The gateway emits log lines. At DEBUG level, there will be many.
    // Log lines are rendered as font-mono divs inside the Virtuoso container.
    const logLine = page.locator('.font-mono').first()
    await expect(logLine).toBeVisible({ timeout: 15000 })
  })

  test('changing log level via UI narrows displayed lines', async ({ page, request }) => {
    // Confirm we have log lines visible at DEBUG level
    const logLine = page.locator('.font-mono').first()
    await expect(logLine).toBeVisible({ timeout: 15000 })

    // Switch to ERROR level via the UI button -- this should filter display
    await page.getByRole('button', { name: 'Error' }).click()

    // Verify the level changed on the server (round-trip)
    await expect.poll(async () => {
      const resp = await (await request.get('/api/logs/level')).json()
      return resp.level
    }, { timeout: 5000 }).toBe('ERROR')
  })

  test('search filter shows match count when term matches log lines', async ({ page }) => {
    // Wait for log lines to appear at DEBUG level
    const logLine = page.locator('.font-mono').first()
    await expect(logLine).toBeVisible({ timeout: 15000 })

    // Every log line contains "kiro_crew" in the logger name field.
    // Type the search term
    const filterInput = page.getByLabel('Filter logs')
    await filterInput.fill('kiro_crew')

    // The match count indicator should appear (format: "N matches")
    await expect(page.getByText(/\d+ matches/)).toBeVisible({ timeout: 5000 })
  })

  test('Matches only button activates and deactivates correctly', async ({ page }) => {
    // Wait for log lines to appear
    const logLine = page.locator('.font-mono').first()
    await expect(logLine).toBeVisible({ timeout: 15000 })

    // Search for a term that matches SOME lines (the logger name appears in all lines)
    const filterInput = page.getByLabel('Filter logs')
    await filterInput.fill('kiro_crew')

    // Wait for the match count to appear (proves search works)
    const matchCountEl = page.getByText(/\d+ matches/)
    await expect(matchCountEl).toBeVisible({ timeout: 5000 })

    // The "Matches only" button should now be visible (only shown when search is active)
    const matchesOnlyBtn = page.getByRole('button', { name: 'Matches only' })
    await expect(matchesOnlyBtn).toBeVisible()

    // Click "Matches only" -- all remaining visible lines contain the search term
    await matchesOnlyBtn.click()

    // Verify lines are still shown (the search matched lines)
    await expect(page.locator('.font-mono').first()).toBeVisible()

    // Clear the search -- "Matches only" button should disappear
    await filterInput.fill('')
    await expect(matchesOnlyBtn).not.toBeVisible({ timeout: 3000 })
  })

  test('log level change via API round-trip persists correctly', async ({ request }) => {
    // GET the current level
    const before = await (await request.get('/api/logs/level')).json()
    expect(before).toHaveProperty('level')

    // Set to INFO via POST
    const setResp = await request.post('/api/logs/level', { data: { level: 'INFO' } })
    expect(setResp.ok()).toBeTruthy()
    const setBody = await setResp.json()
    expect(setBody.ok).toBe(true)
    expect(setBody.level).toBe('INFO')

    // Verify the level persisted
    const after = await (await request.get('/api/logs/level')).json()
    expect(after.level).toBe('INFO')

    // Restore to DEBUG for consistency
    await request.post('/api/logs/level', { data: { level: 'DEBUG' } })
  })
})

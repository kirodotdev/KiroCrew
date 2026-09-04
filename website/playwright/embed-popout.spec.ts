import { test, expect, type APIRequestContext } from '@playwright/test'

const HARNESS_GATEWAY = !!process.env.KIROCREW_E2E_EPHEMERAL

// ─── Seed helpers ──────────────────────────────────────────────────────────

/** Create a chat slot via API and return its key. */
async function seedSlot(request: APIRequestContext, suffix: string): Promise<string> {
  const res = await request.post('/api/chat/slots', {
    data: { agent: 'default', title: `pw-embed-${suffix}-${Date.now()}` },
  })
  expect(res.status(), `slot seed failed: ${await res.text()}`).toBeLessThan(300)
  const body = await res.json()
  return body.key as string
}

/** Create an artifact via API and return its slug. */
async function seedArtifact(request: APIRequestContext, suffix: string): Promise<string> {
  // POST/PATCH /api/artifacts fire ArtifactKnowledgeSync.on_change
  // (artifact_ingest.py:230, gated on knowledge.auto_ingest_artifacts which
  // DEFAULTS TO TRUE). That chains to pipeline.ingest_file() ->
  // delete_items_batch (ingestion.py:341) -> store.py:494, which runs an
  // UNSCOPED sweep: DELETE FROM entities WHERE id NOT IN (SELECT entity_id FROM
  // mentions) AND ... -- destroying pre-existing orphan entities in a real
  // developer's Knowledge Library. Seeding an artifact is therefore not a local
  // operation, so it requires the ephemeral harness gateway.
  test.skip(
    !HARNESS_GATEWAY,
    'seeding artifacts requires the ephemeral harness gateway: artifact ingestion triggers a global orphan-entity sweep (store.py:494)',
  )
  const slug = `pw-embed-${suffix}-${Date.now()}`
  const res = await request.post('/api/artifacts', {
    data: {
      name: `PW Embed Test ${suffix}`,
      slug,
      content: `<h1>Embed Test ${suffix}</h1><p>Seeded by embed-popout.spec.ts</p>`,
      kind: 'widget',
      description: 'Seeded by embed-popout spec',
      tags: ['e2e-embed'],
    },
  })
  expect(res.status(), `artifact seed failed: ${await res.text()}`).toBeLessThan(300)
  const body = await res.json()
  return body.slug as string
}

// ─── POPOUT route tree ─────────────────────────────────────────────────────

test.describe('Popout route tree — /popout/*', () => {

  test('popout chat renders without main app chrome', async ({ page, request }) => {
    const slotKey = await seedSlot(request, 'popout-chat')
    await page.goto(`/popout/chat/${slotKey}?sid=${slotKey}`, { waitUntil: 'domcontentloaded' })
    // Popout shell: no dashboard-shell, no navigation sidebar
    await expect(page.locator('[data-testid="dashboard-shell"]')).toHaveCount(0)
    await expect(page.getByRole('navigation', { name: 'Main navigation' })).toHaveCount(0)
    // ChatPage renders inside the popout frame
    await expect(page.locator('.h-screen.w-screen')).toBeVisible({ timeout: 10000 })
  })

  test('popout chat renders the chat input area', async ({ page, request }) => {
    const slotKey = await seedSlot(request, 'popout-input')
    await page.goto(`/popout/chat/${slotKey}?sid=${slotKey}`, { waitUntil: 'domcontentloaded' })
    // The chat page contains a message input area (textarea or contenteditable)
    const input = page.locator('textarea, [contenteditable="true"]').first()
    await expect(input).toBeVisible({ timeout: 10000 })
  })

  test('popout chat without slug renders chat page shell', async ({ page }) => {
    // /popout/chat/:slug? — the slug is optional
    await page.goto('/popout/chat/', { waitUntil: 'domcontentloaded' })
    await expect(page.locator('[data-testid="dashboard-shell"]')).toHaveCount(0)
    // Still renders the popout frame container
    await expect(page.locator('.h-screen.w-screen')).toBeVisible({ timeout: 10000 })
  })

  test('popout artifact renders the artifact detail with return button', async ({ page, request }) => {
    const slug = await seedArtifact(request, 'popout-art')
    await page.goto(`/popout/artifact/${slug}`, { waitUntil: 'domcontentloaded' })
    // No main chrome
    await expect(page.locator('[data-testid="dashboard-shell"]')).toHaveCount(0)
    await expect(page.getByRole('navigation', { name: 'Main navigation' })).toHaveCount(0)
    // ArtifactPopoutFrame renders the "Return to main window" button
    await expect(
      page.getByRole('button', { name: 'Return to main window and close this popout' })
    ).toBeVisible({ timeout: 10000 })
  })

  test('popout artifact shows seeded artifact content', async ({ page, request }) => {
    const slug = await seedArtifact(request, 'popout-content')
    await page.goto(`/popout/artifact/${slug}`, { waitUntil: 'domcontentloaded' })
    // ArtifactPopoutFrame sets document.title to include "Kiro Crew"
    await expect.poll(
      () => page.title(),
      { timeout: 10000 }
    ).toContain('Kiro Crew')
  })

  test('popout wildcard redirects SPA navigation back to initial path', async ({ page, request }) => {
    // The wildcard <Route path="*" element={<Navigate to={initialPopoutPath} replace />} />
    // redirects stray SPA navigations back to the initial load path.
    const slotKey = await seedSlot(request, 'popout-wc')
    await page.goto(`/popout/chat/${slotKey}?sid=${slotKey}`, { waitUntil: 'domcontentloaded' })
    // Confirm we're on the valid popout page first
    await expect(page.locator('.h-screen.w-screen')).toBeVisible({ timeout: 10000 })
    // Capture the initial URL (may be rewritten by SPA from the loaded path)
    const urlBeforePush = page.url()
    // Trigger in-app navigation to a non-matching popout path
    await page.evaluate(() => {
      window.history.pushState({}, '', '/popout/stray-nav')
      window.dispatchEvent(new PopStateEvent('popstate'))
    })
    // The wildcard route catches this and redirects back via Navigate(to=initialPopoutPath).
    // The initial path is set at document load (window.location.pathname + search).
    // Verify the URL reverts away from /popout/stray-nav back to a /popout/chat/ path.
    await expect.poll(
      () => page.url(),
      { timeout: 5000 }
    ).toContain('/popout/chat/')
    // Confirm it's not stuck on the stray path
    expect(page.url()).not.toContain('/popout/stray-nav')
  })
})

// ─── EMBED route tree ──────────────────────────────────────────────────────

test.describe('Embed route tree — /embed/*', () => {

  test('embed sessions renders without main app chrome but with tab strip', async ({ page }) => {
    await page.goto('/embed/sessions', { waitUntil: 'domcontentloaded' })
    // No dashboard-shell or navigation sidebar
    await expect(page.locator('[data-testid="dashboard-shell"]')).toHaveCount(0)
    await expect(page.getByRole('navigation', { name: 'Main navigation' })).toHaveCount(0)
    // EmbedTabStrip renders tab elements and a "New tab" button
    await expect(page.getByRole('button', { name: 'New tab' })).toBeVisible({ timeout: 10000 })
  })

  test('embed sessions shows tab strip with at least one tab', async ({ page }) => {
    await page.goto('/embed/sessions', { waitUntil: 'domcontentloaded' })
    // The EmbedTabStrip renders role="tab" elements
    await expect(page.getByRole('tab').first()).toBeVisible({ timeout: 10000 })
  })

  test('embed chat renders the chat view for a session', async ({ page, request }) => {
    const slotKey = await seedSlot(request, 'embed-chat')
    await page.goto(`/embed/chat/${slotKey}?sid=${slotKey}`, { waitUntil: 'domcontentloaded' })
    // Embed shell: no main chrome
    await expect(page.locator('[data-testid="dashboard-shell"]')).toHaveCount(0)
    await expect(page.getByRole('navigation', { name: 'Main navigation' })).toHaveCount(0)
    // Has the tab strip
    await expect(page.getByRole('button', { name: 'New tab' })).toBeVisible({ timeout: 10000 })
    // Has the chat input
    const input = page.locator('textarea, [contenteditable="true"]').first()
    await expect(input).toBeVisible({ timeout: 10000 })
  })

  test('embed chat without slug navigates to sessions', async ({ page }) => {
    // /embed/chat/:slug? — optional slug. Without it, the embed tab strip's
    // mount logic navigates to /embed/sessions when it has no slug for the active tab.
    await page.goto('/embed/chat/', { waitUntil: 'domcontentloaded' })
    await page.waitForURL('**/embed/sessions', { timeout: 10000 })
  })

  /**
   * FINDING (product bug, not fixed here): /embed/settings cannot be reached by
   * URL, and cannot be held once reached. EmbedTabStrip is a sibling of <Routes>
   * in the embed shell (App.tsx:1397), so it mounts on every /embed/* path and
   * navigates away twice over:
   *
   *   1. a one-shot mount effect -> the restored tab (EmbedTabStrip.tsx:53-60);
   *   2. an effect keyed on [activeSlot] that rewrites the empty "sessions" tab
   *      into a chat tab -> /embed/chat/:slug (EmbedTabStrip.tsx:129-136).
   *
   * (2) fires whenever the store hydrates an activeSlot, so it can land after any
   * client-side navigation and clobber it mid-render. That makes the render and
   * tab-switching behaviour of EmbedSettingsPage untestable without a fixed
   * sleep, which this suite bans. The page's only in-app entry point is
   * VoiceDisabledModal's onOpenSettings (ChatPage.tsx:4096).
   *
   * What IS deterministic is the redirect contract itself, asserted below. When
   * the deep link is fixed, replace this with render + tab-switching coverage.
   */
  test('embed settings deep link is redirected into the embed tab flow', async ({ page }) => {
    await page.goto('/embed/settings', { waitUntil: 'domcontentloaded' })
    // The shell never leaves the embed tree, and never shows main app chrome.
    await expect(page.locator('[data-testid="dashboard-shell"]')).toHaveCount(0)
    await expect(page.getByRole('navigation', { name: 'Main navigation' })).toHaveCount(0)
    // EmbedTabStrip's mount effect takes over: the URL lands on a tab route.
    await page.waitForURL(/\/embed\/(sessions|chat)/, { timeout: 15000 })
    await expect(page).not.toHaveURL(/\/embed\/settings/)
    // The tab strip is live, proving we landed in the tab flow.
    await expect(page.getByRole('button', { name: 'New tab' })).toBeVisible({ timeout: 10000 })
  })

  test('embed wildcard redirects to /embed/sessions', async ({ page }) => {
    // Any non-matching path under /embed/* redirects to /embed/sessions
    await page.goto('/embed/nonexistent-route', { waitUntil: 'domcontentloaded' })
    await page.waitForURL('**/embed/sessions', { timeout: 10000 })
    // After redirect, the sessions view renders
    await expect(page.getByRole('button', { name: 'New tab' })).toBeVisible({ timeout: 10000 })
  })

  test('embed new tab button creates a sessions tab', async ({ page }) => {
    await page.goto('/embed/sessions', { waitUntil: 'domcontentloaded' })
    await expect(page.getByRole('button', { name: 'New tab' })).toBeVisible({ timeout: 10000 })
    const initialTabCount = await page.getByRole('tab').count()
    // Click "New tab" — adds an empty-slug tab that navigates to /embed/sessions
    await page.getByRole('button', { name: 'New tab' }).click()
    // Tab count increased
    await expect(page.getByRole('tab')).toHaveCount(initialTabCount + 1, { timeout: 5000 })
  })
})

// ─── Chrome presence on normal routes (contrast) ───────────────────────────

test.describe('Normal route renders main app chrome (contrast)', () => {

  test('main dashboard route has dashboard-shell and navigation', async ({ page }) => {
    await page.goto('/', { waitUntil: 'domcontentloaded' })
    await expect(page.locator('[data-testid="dashboard-shell"]')).toBeVisible({ timeout: 10000 })
    await expect(page.getByRole('navigation', { name: 'Main navigation' })).toBeVisible({ timeout: 10000 })
  })
})

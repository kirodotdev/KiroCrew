import { test, expect } from '@playwright/test'

/**
 * Builtin App Route resolution e2e tests.
 *
 * Covers the /:builtinApp catch-all route (App.tsx ~line 2017) which resolves
 * paths against BUILTIN_COMPONENT_REGISTRY (builtinRegistry.ts). Each registered
 * route renders its lazy-loaded page component; unrecognised paths redirect to /chat.
 *
 * React Router v6 ranks static paths above parameterised ones, so explicitly
 * declared routes (/settings, /apps, /artifacts, etc.) still win over the catch-all.
 */

test.describe('Builtin App Route resolution', () => {

  // ── Route resolution contracts ───────────────────────────────────────────

  test('unrecognised path redirects to /chat', async ({ page }) => {
    await page.goto('/totally-unknown-xyz-route', { waitUntil: 'domcontentloaded' })
    await expect(page).toHaveURL(/\/chat/)
  })

  test('static route /settings is NOT caught by the catch-all', async ({ page }) => {
    await page.goto('/settings', { waitUntil: 'domcontentloaded' })
    // Settings page renders — confirm we are NOT redirected to /chat
    await expect(page).toHaveURL(/\/settings/)
    // Settings page heading (inside #main-content, not the nav label)
    await expect(page.locator('#main-content').getByText('Settings', { exact: true })).toBeVisible({ timeout: 10000 })
  })

  test('static route /artifacts is NOT caught by the catch-all', async ({ page }) => {
    await page.goto('/artifacts', { waitUntil: 'domcontentloaded' })
    await expect(page).toHaveURL(/\/artifacts/)
    // Artifacts page should render without redirect. Scoped with .first():
    // 'Artifacts' also appears as the nav rail label.
    await expect(page.locator('text=Artifacts').first()).toBeVisible({ timeout: 10000 })
  })

  // ── /worlds — WorldsPage ─────────────────────────────────────────────────

  test('/worlds renders Agent Worlds page', async ({ page }) => {
    await page.goto('/worlds', { waitUntil: 'domcontentloaded' })
    await expect(page).toHaveURL(/\/worlds/)
    await expect(page.getByText('Agent Worlds')).toBeVisible({ timeout: 10000 })
    await expect(page.getByTestId('collapse-panel')).toBeVisible({ timeout: 10000 })
  })

  // ── /channels — ChannelPage ───────────────────────────────────────────────

  test('/channels renders Channels page with empty state', async ({ page }) => {
    await page.goto('/channels', { waitUntil: 'domcontentloaded' })
    await expect(page).toHaveURL(/\/channels/)
    // PageHeader (ui.tsx:257) renders its title as a plain <div>, not a heading,
    // so getByRole('heading') cannot match it and a bare getByText('Channels')
    // resolves to 3 elements (nav label + header + sidebar) once earlier tests
    // populate the sidebar -- a strict-mode violation that only reproduces when
    // the whole file runs. Assert the unique subtitle instead.
    await expect(page.getByText('Multi-agent collaboration spaces')).toBeVisible({ timeout: 10000 })
    // Minimal fixture has no channels — assert the empty state
    await expect(page.getByText('No channels yet')).toBeVisible({ timeout: 10000 })
  })

  // ── /auto-research — ResearchLabPage ──────────────────────────────────────

  test('/auto-research renders Research Lab page', async ({ page }) => {
    await page.goto('/auto-research', { waitUntil: 'domcontentloaded' })
    await expect(page).toHaveURL(/\/auto-research/)
    await expect(page.getByRole('heading', { name: 'Research Lab' })).toBeVisible({ timeout: 10000 })
    // Confirms the description text is present (unconditional render)
    await expect(page.getByText('Research-only.')).toBeVisible({ timeout: 10000 })
  })

  // ── /code-review-sage — CodeReviewSagePage ────────────────────────────────

  test('/code-review-sage renders Code Review Sage page', async ({ page }) => {
    await page.goto('/code-review-sage', { waitUntil: 'domcontentloaded' })
    await expect(page).toHaveURL(/\/code-review-sage/)
    await expect(page.getByText('Code Review Sage')).toBeVisible({ timeout: 10000 })
    // The old page led with a hero tagline ("Self-evolving deep reviewer…").
    // The shell replaced it: the rail's identity row carries the name, and the
    // space the tagline occupied belongs to the report. Assert the section nav
    // instead — it renders unconditionally, before any repo or run is loaded,
    // which is what this spec is checking (the route resolves and mounts).
    await expect(page.getByRole('navigation', { name: 'Sections' })).toBeVisible({ timeout: 10000 })
  })

  // ── /workflows — WorkflowsPage ───────────────────────────────────────────

  test('/workflows renders Workflows page', async ({ page }) => {
    await page.goto('/workflows', { waitUntil: 'domcontentloaded' })
    await expect(page).toHaveURL(/\/workflows/)
    await expect(page.getByText('Workflows', { exact: true })).toBeVisible({ timeout: 10000 })
    await expect(page.getByText(/Author, run, and watch dynamic workflows/)).toBeVisible({ timeout: 10000 })
  })

  // ── /dev-fleet — DevFleetPage ─────────────────────────────────────────────

  test('/dev-fleet renders Dev Fleet page', async ({ page }) => {
    await page.goto('/dev-fleet', { waitUntil: 'domcontentloaded' })
    await expect(page).toHaveURL(/\/dev-fleet/)
    await expect(page.getByText('Dev Fleet')).toBeVisible({ timeout: 10000 })
  })

  // ── /issue-radar — IssueRadarPage ─────────────────────────────────────────

  test('/issue-radar renders Issue Radar welcome (no repos on fixture)', async ({ page }) => {
    await page.goto('/issue-radar', { waitUntil: 'domcontentloaded' })
    await expect(page).toHaveURL(/\/issue-radar/)
    await expect(page.getByText('Welcome to Issue Radar')).toBeVisible({ timeout: 10000 })
  })

  // ── /file-explorer — FileExplorerPage ─────────────────────────────────────

  test('/file-explorer renders File Explorer page', async ({ page }) => {
    await page.goto('/file-explorer', { waitUntil: 'domcontentloaded' })
    await expect(page).toHaveURL(/\/file-explorer/)
    // FileExplorerPage always renders the mc-fe-root container
    await expect(page.locator('.mc-fe-root')).toBeVisible({ timeout: 10000 })
  })
})

import { test, expect } from '@playwright/test'

/**
 * Redirect contract tests — routes that Navigate.replace to other pages.
 *
 * Covered here:
 *  /agents      → /capabilities (Navigate replace)
 *  /mc-agents   → /capabilities (Navigate replace)
 *  /tasks       → /projects (TasksRedirect, preserves search params)
 *  /orchestrated/:slug? → /chat/:slug? (OrchestratedRedirect, preserves slug + search)
 *
 * NOT covered (handled by sibling specs):
 *  /overview    → /settings?tab=overview  (settings.spec.ts)
 *  /instances   → /settings?tab=instances (settings.spec.ts)
 *  /artifacts/deploy → /deploy            (artifacts.spec.ts)
 */

test.describe('Redirect contracts', () => {
  test('/agents redirects to /capabilities and renders Agent Capabilities', async ({ page }) => {
    await page.goto('/agents', { waitUntil: 'domcontentloaded' })
    // React Router Navigate replace — URL should land on /capabilities
    await page.waitForURL('**/capabilities', { timeout: 10000 })
    // Verify the destination page actually rendered (not a blank error page)
    await expect(page.locator('text=Agent Capabilities')).toBeVisible({ timeout: 10000 })
    // Verify the default tab content (the Crews roster) is present. The primary
    // action's testid is the landmark: it survives restyling of the roster,
    // unlike the StatCard label this used to read.
    await expect(page.getByTestId('new-crew')).toBeVisible({ timeout: 5000 })
  })

  test('/mc-agents redirects to /capabilities and renders Agent Capabilities', async ({ page }) => {
    await page.goto('/mc-agents', { waitUntil: 'domcontentloaded' })
    await page.waitForURL('**/capabilities', { timeout: 10000 })
    await expect(page.locator('text=Agent Capabilities')).toBeVisible({ timeout: 10000 })
    await expect(page.getByTestId('new-crew')).toBeVisible({ timeout: 5000 })
  })

  test('/tasks redirects to /projects and renders Task Runner', async ({ page }) => {
    await page.goto('/tasks', { waitUntil: 'domcontentloaded' })
    // TasksRedirect: Navigate to="/projects" + search params
    await page.waitForURL('**/projects', { timeout: 10000 })
    // ProjectsPage names the app in its run rail (no page-title heading)
    await expect(page.locator('text=Task Runner').first()).toBeVisible({ timeout: 10000 })
  })

  test('/tasks preserves query params through redirect', async ({ page }) => {
    await page.goto('/tasks?run=abc123', { waitUntil: 'domcontentloaded' })
    await page.waitForURL('**/projects?run=abc123', { timeout: 10000 })
    await expect(page.locator('text=Task Runner').first()).toBeVisible({ timeout: 10000 })
  })

  test('/orchestrated redirects to /chat and renders chat UI', async ({ page }) => {
    await page.goto('/orchestrated', { waitUntil: 'domcontentloaded' })
    // OrchestratedRedirect: Navigate to="/chat" + optional slug + search
    await page.waitForURL('**/chat', { timeout: 10000 })
    // Verify the chat page actually rendered — the ChatInput wrapper is present
    await expect(page.locator('[data-testid="input-wrapper"]')).toBeVisible({ timeout: 10000 })
  })

  test('/orchestrated/:slug redirects to /chat/:slug', async ({ page }) => {
    await page.goto('/orchestrated/my-session', { waitUntil: 'domcontentloaded' })
    await page.waitForURL('**/chat/my-session', { timeout: 10000 })
    await expect(page.locator('[data-testid="input-wrapper"]')).toBeVisible({ timeout: 10000 })
  })

  test('/orchestrated preserves search params through redirect', async ({ page }) => {
    await page.goto('/orchestrated?intent=hello', { waitUntil: 'domcontentloaded' })
    await page.waitForURL('**/chat?intent=hello', { timeout: 10000 })
    await expect(page.locator('[data-testid="input-wrapper"]')).toBeVisible({ timeout: 10000 })
  })
})

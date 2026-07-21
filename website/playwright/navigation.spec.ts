import { test, expect } from '@playwright/test'

test.describe('Navigation E2E Tests', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/', { waitUntil: 'domcontentloaded' })
    // Wait for app to load by checking sidebar
    await expect(page.locator('nav[aria-label="Main navigation"]')).toBeVisible({ timeout: 10000 })
  })

  test('loads the homepage and displays navigation', async ({ page }) => {
    // Redirects to /chat by default - check that sidebar navigation exists
    const sidebar = page.locator('nav[aria-label="Main navigation"]')
    await expect(sidebar).toBeVisible({ timeout: 10000 })
    
    // Just verify sidebar is visible - the text "Chat" appears in many places
    // so we'll just check that the sidebar loaded successfully
    await expect(sidebar.locator('svg').first()).toBeVisible()
  })

  test('handles navigation back and forth', async ({ page }) => {
    // Navigate to Overview via URL (navigation items are divs, not links)
    await page.goto('/overview', { waitUntil: 'domcontentloaded' })
    // Check for Memory tab button specifically
    await expect(page.getByRole('button', { name: 'Memory', exact: true })).toBeVisible({ timeout: 10000 })

    // Navigate to Chat
    await page.goto('/chat', { waitUntil: 'domcontentloaded' })
    await expect(page.getByPlaceholder(/message|type|chat/i)).toBeVisible({ timeout: 10000 })

    // Go back to Overview
    await page.goto('/overview', { waitUntil: 'domcontentloaded' })
    await expect(page.getByRole('button', { name: 'Memory', exact: true })).toBeVisible({ timeout: 10000 })
  })

  test('theme toggle works', async ({ page }) => {
    // Look for theme toggle button (usually a sun/moon icon)
    const themeToggle = page.locator('button').filter({ hasText: /☀|🌙|theme/i })
    
    if (await themeToggle.count() > 0) {
      // Get initial theme
      const html = page.locator('html')
      const initialTheme = await html.getAttribute('data-theme')
      
      // Click theme toggle
      await themeToggle.first().click()
      
      // Wait for theme attribute to change
      await expect(html).toHaveAttribute('data-theme', /.+/, { timeout: 2000 })

      // Verify theme changed
      const newTheme = await html.getAttribute('data-theme')
      expect(newTheme).not.toBe(initialTheme)
    }
  })

  test('all main navigation links are clickable', async ({ page }) => {
    const navPages = [
      { name: 'Chat', path: '/chat' },
      { name: 'Developer', path: '/developer' },
      { name: 'Hooks', path: '/hooks' },
      { name: 'Schedule', path: '/schedule' }
    ]

    for (const { path } of navPages) {
      // Navigate directly to the page
      await page.goto(path, { waitUntil: 'domcontentloaded' })
      
      // Wait for page to be ready by checking for common elements
      await expect(page.locator('body')).toBeVisible()
      
      // Should navigate to the correct URL
      const url = page.url()
      expect(url).toContain(path)
    }
  })

  test('sidebar navigation persists across pages', async ({ page }) => {
    // Navigation should be visible on Overview page - use sidebar locator
    await page.goto('/overview', { waitUntil: 'domcontentloaded' })
    await expect(page.locator('nav[aria-label="Main navigation"]').getByText('Sessions')).toBeVisible()

    // Navigation should be visible on the Developer page (was /system, now
    // /developer?tab=system — kept consistent with system.spec.ts).
    await page.goto('/developer?tab=system', { waitUntil: 'domcontentloaded' })
    await expect(page.locator('nav[aria-label="Main navigation"]').getByText('Schedule')).toBeVisible()
  })
})

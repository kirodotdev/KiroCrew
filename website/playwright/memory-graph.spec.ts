import { test, expect, type Page } from '@playwright/test'

// The Memory page's Graph tab loads after the page: wait for its data read.
async function openGraph(page: Page) {
  const graphRead = page.waitForResponse(response => new URL(response.url()).pathname === '/api/memory/graph')
  await page.goto('/settings/memory?focus=graph', { waitUntil: 'domcontentloaded' })
  await graphRead
}
test.describe('Memory surfaces E2E Tests', () => {
  test('the Graph tab renders the graph explorer', async ({ page }) => {
    await openGraph(page)
    await expect(page.getByRole('heading', { name: 'Memory Graph' })).toBeVisible({ timeout: 5000 })
  })

  test('graph explorer shows filter buttons', async ({ page }) => {
    await openGraph(page)
    await expect(page.getByText(/All \(\d+\)/)).toBeVisible({ timeout: 5000 })
  })

  test('graph explorer has a search input', async ({ page }) => {
    await openGraph(page)
    await expect(page.getByPlaceholder('Search nodes…')).toBeVisible({ timeout: 5000 })
  })
})

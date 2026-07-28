import { test, expect } from '@playwright/test'

/**
 * /capabilities — Agent Capabilities page.
 * SidePanelLayout with 6 tabs: Agents, Agent Templates, Integrations (MCP),
 * Skills, Hooks, Prompts. Default tab is "agents" (KiroCrewAgentsPage).
 *
 * Covers: page load + heading, tab navigation with content change assertion,
 * the agents table read + a create/delete round-trip mutation.
 */

test.describe('Capabilities Page — /capabilities', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/capabilities', { waitUntil: 'domcontentloaded' })
    // Wait for the SidePanelLayout page title (in the nav panel, scoped to avoid ambiguity)
    await expect(page.locator('#main-content .text-lg.font-bold').first()).toBeVisible({ timeout: 10000 })
  })

  test('renders the page title and default Agents tab heading', async ({ page }) => {
    // SidePanelLayout nav title "Agent Capabilities" — scoped inside main-content
    await expect(page.locator('#main-content .text-lg.font-bold').first()).toHaveText('Agent Capabilities')
    // Default tab description from the content area header
    await expect(page.locator('#main-content').getByText('Manage agent → workspace → memory store bindings')).toBeVisible({ timeout: 5000 })
  })

  test('shows all 6 tab buttons in the side nav', async ({ page }) => {
    // Tab buttons inside the nav panel — look inside #main-content nav
    const nav = page.locator('#main-content nav')
    const tabs = ['Agents', 'Agent Templates', 'Integrations (MCP)', 'Skills', 'Hooks', 'Prompts']
    for (const label of tabs) {
      await expect(nav.getByRole('button', { name: label, exact: true })).toBeVisible({ timeout: 5000 })
    }
  })

  test('agents tab shows the stats cards and agents table', async ({ page }) => {
    // StatCard "Total Agents" rendered by KiroCrewAgentsPage
    await expect(page.locator('#main-content').getByText('Total Agents')).toBeVisible({ timeout: 5000 })
    // StatCard "Default" showing the default agent name
    await expect(page.locator('#main-content').getByText('Default').first()).toBeVisible()
    // The agents table has a header row with "Name" column
    await expect(page.locator('#main-content th').filter({ hasText: 'Name' }).first()).toBeVisible()
    // The minimal fixture seeds at least one agent (the "kirocrew" default)
    await expect(page.locator('#main-content td').filter({ hasText: 'kirocrew' }).first()).toBeVisible({ timeout: 5000 })
  })

  test('switching to Skills tab renders skills content', async ({ page }) => {
    // Click the Skills tab button in the side nav
    await page.locator('#main-content nav').getByRole('button', { name: 'Skills', exact: true }).click()
    // URL should update with ?tab=skills
    await page.waitForURL('**/capabilities?tab=skills', { timeout: 5000 })
    // Skills tab content renders "Filter skills…" search input
    await expect(page.getByPlaceholder('Filter skills…')).toBeVisible({ timeout: 10000 })
  })

  test('switching to Hooks tab renders hooks content', async ({ page }) => {
    await page.locator('#main-content nav').getByRole('button', { name: 'Hooks', exact: true }).click()
    await page.waitForURL('**/capabilities?tab=hooks', { timeout: 5000 })
    // HooksPage shows the "+ New Hook" button
    await expect(page.getByRole('button', { name: /\+ new hook/i })).toBeVisible({ timeout: 10000 })
  })

  test('switching to Agent Templates tab renders installed agents', async ({ page }) => {
    await page.locator('#main-content nav').getByRole('button', { name: 'Agent Templates', exact: true }).click()
    await page.waitForURL('**/capabilities?tab=templates', { timeout: 5000 })
    // AgentsPage renders "Installed Agents" heading text
    await expect(page.locator('#main-content').getByText('Installed Agents')).toBeVisible({ timeout: 10000 })
  })

  test('create and delete agent round-trip via API', async ({ page, request }) => {
    // Use the API directly to create an agent, verify it appears in the table,
    // then delete it and verify removal. This is a full mutation round-trip.
    const agentName = `pw-cap-${Date.now()}`

    // Create agent via API
    const createRes = await request.post('/api/agents', {
      data: { name: agentName, kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default' },
    })
    expect(createRes.ok()).toBeTruthy()

    // Refresh the page to see the new agent
    await page.goto('/capabilities', { waitUntil: 'domcontentloaded' })
    await expect(page.locator('#main-content .text-lg.font-bold').first()).toBeVisible({ timeout: 10000 })

    // Verify the created agent appears in the table
    await expect(page.locator('#main-content td').filter({ hasText: agentName })).toBeVisible({ timeout: 10000 })

    // Delete agent via API
    const deleteRes = await request.delete(`/api/agents/${encodeURIComponent(agentName)}`)
    expect(deleteRes.ok()).toBeTruthy()

    // Refresh and verify the agent is gone
    await page.goto('/capabilities', { waitUntil: 'domcontentloaded' })
    await expect(page.locator('#main-content .text-lg.font-bold').first()).toBeVisible({ timeout: 10000 })
    await expect(page.locator('#main-content td').filter({ hasText: agentName })).not.toBeVisible({ timeout: 5000 })
  })
})

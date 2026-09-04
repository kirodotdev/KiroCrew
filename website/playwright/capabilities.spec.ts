import { test, expect } from '@playwright/test'

/**
 * /capabilities — Agent Capabilities page.
 * SidePanelLayout with 7 tabs: Agents, Agent Templates, Connections,
 * Skills, Steering, Hooks, Prompts. Default tab is "crews" (KiroCrewAgentsPage).
 *
 * Covers: page load + heading, tab navigation with content change assertion,
 * the crew roster read + a create/delete round-trip mutation through the
 * roster's editor sheet.
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
    // Prose deliberately: on /capabilities this string is a TAB DESCRIPTION
    // (CapabilitiesPage.tsx:16), not a PageHeader subtitle, so there is no
    // page-subtitle testid on this route. KiroCrewAgentsPage renders the same
    // string as a real PageHeader subtitle, hence the #main-content scope.
    await expect(page.locator('#main-content').getByText('Agents you chat with', { exact: false })).toBeVisible({ timeout: 5000 })
  })

  test('shows all 7 tab buttons in the side nav', async ({ page }) => {
    // Tab buttons inside the nav panel — look inside #main-content nav
    const nav = page.locator('#main-content nav')
    const tabs = ['Agents', 'Agent Templates', 'Connections', 'Skills', 'Steering', 'Hooks', 'Prompts']
    for (const label of tabs) {
      await expect(nav.getByRole('button', { name: label, exact: true })).toBeVisible({ timeout: 5000 })
    }
  })

  test('crews tab renders the crew roster as cards', async ({ page }) => {
    // The roster is a card grid with a side editor sheet — no StatCard row and
    // no table any more, so everything here keys off a testid or an accessible
    // name rather than a tag, which restyling cannot invalidate.
    await expect(page.getByTestId('new-crew')).toBeVisible({ timeout: 5000 })

    const cards = page.getByTestId('crew-card')
    await expect(cards.first()).toBeVisible({ timeout: 5000 })

    // The minimal fixture seeds one crew bound to the "kirocrew" agent template.
    // The crew's own NAME is "default" there (config/loader.py seeds
    // agents["default"] when config.json has no agents section), so the template
    // value is what identifies it — the same string the retired assertion
    // matched, which was a table cell in the Agent Template column, not a name.
    const seeded = cards.filter({ hasText: 'kirocrew' }).first()
    await expect(seeded).toBeVisible({ timeout: 5000 })

    // Every card labels the four bindings the table used to carry as columns.
    for (const label of ['Agent Template', 'Workspace', 'Memory Store', 'Model']) {
      await expect(seeded.getByText(label, { exact: true })).toBeVisible()
    }

    // The trailing dashed tile is the roster's second entry point into the
    // create sheet, so it is part of the contract rather than decoration.
    await expect(page.getByRole('button', { name: 'Create a new agent', exact: true })).toBeVisible()
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

  test('create and delete crew round-trip via the editor panel', async ({ page, request }) => {
    // Both mutations now live in the side sheet: creation is New crew → dialog →
    // Create, deletion is card → dialog → Delete crew. Driving them through the
    // UI is the round-trip now, since neither control exists on the page itself.
    const agentName = `pw-cap-${Date.now()}`
    const card = page.getByRole('button', { name: `Edit agent ${agentName}`, exact: true })

    try {
      await page.getByTestId('new-crew').click()
      const createSheet = page.getByRole('dialog', { name: 'Create a new agent' })
      await expect(createSheet).toBeVisible({ timeout: 5000 })

      // The Name field's label is a <span>, not a <label for>, so the input has
      // no accessible name — the placeholder is its stable handle. Workspace and
      // memory store keep their defaults; the Agent Template does NOT have one and
      // must be chosen, because pre-filling it made a new crew a silent alias for
      // the default agent, so Create now refuses until it is set.
      await createSheet.getByPlaceholder('e.g. oncall').fill(agentName)
      await createSheet.getByRole('combobox', { name: 'Agent Template' }).click()
      await page.getByRole('option', { name: 'kirocrew', exact: true }).click()
      await createSheet.getByRole('button', { name: 'Create', exact: true }).click()

      // A successful create closes the sheet and refetches the roster.
      await expect(createSheet).toBeHidden({ timeout: 10000 })
      await expect(card).toBeVisible({ timeout: 10000 })

      // Delete through the same panel — the danger zone only renders for a crew
      // that is not the default, which a freshly created one never is.
      await card.click()
      const editSheet = page.getByRole('dialog', { name: `Edit agent ${agentName}` })
      await expect(editSheet).toBeVisible({ timeout: 5000 })
      // The editor is a rail plus one pane, so removal lives on its own pane and
      // the button is not mounted until that pane is showing. This is the click a
      // user makes; without it the button below is simply absent.
      await editSheet.getByTestId('crew-rail-danger').click()
      await editSheet.getByRole('button', { name: 'Delete agent', exact: true }).click()
      // Delete is a two-step confirm: the first press only arms it, so without
      // this second press the sheet never closes and the delete never happens.
      await editSheet.getByTestId('confirm-delete-crew').click()

      await expect(editSheet).toBeHidden({ timeout: 10000 })
      await expect(card).toHaveCount(0, { timeout: 10000 })
    } finally {
      // Best-effort cleanup: a failure part-way through (or a CI retry) must not
      // leave the crew behind in the gateway's config for the next run. A 404
      // here is the expected outcome of the happy path.
      await request.delete(`/api/agents/${encodeURIComponent(agentName)}`).catch(() => {})
    }
  })
})

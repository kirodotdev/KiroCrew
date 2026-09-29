import { test, expect } from '@playwright/test'

/**
 * /capabilities — Customize page (formerly Agent Capabilities).
 * SidePanelLayout with 6 tabs: Agents, Connections,
 * Skills, Steering, Hooks, Prompts. Default tab is "crews" (KiroCrewAgentsPage).
 *
 * Covers: page load + heading, tab navigation with content change assertion,
 * the crew roster read + a create/delete round-trip mutation through the
 * roster's editor sheet.
 */

test.describe('Capabilities Page — /capabilities', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/capabilities', { waitUntil: 'domcontentloaded' })
    // Wait for the SidePanelLayout nav-column title
    await expect(page.getByTestId('side-panel-nav-title')).toBeVisible({ timeout: 10000 })
  })

  test('renders the page title and default Crewmates tab heading', async ({ page }) => {
    // SidePanelLayout nav title "Customize" — scoped inside main-content
    await expect(page.getByTestId('side-panel-nav-title')).toHaveText('Customize')
    // Default tab description from the content area header
    // Prose deliberately: on /capabilities this string is a TAB DESCRIPTION
    // (CapabilitiesPage.tsx:16), not a PageHeader subtitle, so there is no
    // page-subtitle testid on this route. KiroCrewAgentsPage renders the same
    // string as a real PageHeader subtitle, hence the #main-content scope.
    await expect(page.locator('#main-content').getByText('Your AI teammates', { exact: false })).toBeVisible({ timeout: 5000 })
  })

  test('shows all 6 tab buttons in the side nav', async ({ page }) => {
    // Tab buttons inside the nav panel — look inside #main-content nav
    const nav = page.locator('#main-content nav')
    const tabs = ['Crewmates', 'Connections', 'Skills', 'Steering', 'Hooks', 'Prompts']
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
    // matched, which was a table cell in the Built from column, not a name.
    const seeded = cards.filter({ hasText: 'kirocrew' }).first()
    await expect(seeded).toBeVisible({ timeout: 5000 })

    // Every card labels the four bindings the table used to carry as columns.
    for (const label of ['Built from', 'Workspace', 'Memory Store', 'Model']) {
      await expect(seeded.getByText(label, { exact: true })).toBeVisible()
    }

    // The trailing dashed tile is the roster's second entry point into the
    // create dialog, so it is part of the contract rather than decoration.
    await expect(page.getByLabel('New crewmate', { exact: true })).toBeVisible()
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

  test('create and delete crew round-trip via the editor panel', async ({ page, request }) => {
    // Creation now opens the simple New crewmate dialog (the same one the
    // Crewmates page uses); deletion is still card → editor → Delete crewmate.
    // Config mode: a create stays on this roster, it does not navigate away.
    const agentName = `pw-cap-${Date.now()}`
    const card = page.getByRole('button', { name: `Edit crewmate ${agentName}`, exact: true })

    try {
      await page.getByTestId('new-crew').click()
      const createSheet = page.getByRole('dialog', { name: 'New crewmate' })
      await expect(createSheet).toBeVisible({ timeout: 5000 })

      // Name by placeholder (its label is a <span>, not a <label for>). "Built
      // from" defaults to the built-in kirocrew template, so no pick is needed;
      // everything else sits behind the Advanced fold and keeps its defaults.
      await createSheet.getByPlaceholder('e.g. Radar or Dr. Eggbot').fill(agentName)
      await createSheet.getByRole('button', { name: 'Create crewmate', exact: true }).click()

      // A successful create closes the dialog and refetches the roster; the page
      // stays on /capabilities (config mode: no navigation).
      await expect(createSheet).toBeHidden({ timeout: 10000 })
      await expect(page).toHaveURL(/\/capabilities/, { timeout: 5000 })
      await expect(card).toBeVisible({ timeout: 10000 })

      // Delete through the same panel — the danger zone only renders for a crew
      // that is not the default, which a freshly created one never is.
      await card.click()
      const editSheet = page.getByRole('dialog', { name: `Edit crewmate ${agentName}` })
      await expect(editSheet).toBeVisible({ timeout: 5000 })
      // The editor is a rail plus one pane, so removal lives on its own pane and
      // the button is not mounted until that pane is showing. This is the click a
      // user makes; without it the button below is simply absent.
      await editSheet.getByTestId('crew-rail-danger').click()
      await editSheet.getByRole('button', { name: 'Delete crewmate', exact: true }).click()
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

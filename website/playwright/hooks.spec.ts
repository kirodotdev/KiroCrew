import { test as base, expect, type APIRequestContext, type Page, type Response } from '@playwright/test'
import { randomUUID } from 'crypto'
import { pickFromDropdown } from './helpers/dropdown'

interface HookRow {
  id: string
  name: string
  event: string
  command: string
  enabled: boolean
}

/** Seeded and tracked hooks a test owns; teardown deletes exactly these ids. */
interface OwnHooks {
  seed(fields?: Partial<Omit<HookRow, 'id'>>): Promise<HookRow>
  track(id: string): void
  read(id: string): Promise<HookRow | undefined>
}

/** A name no other test, worker or repeat can collide with. */
const uniqueName = (label: string) => `Playwright_${label}_${randomUUID().slice(0, 8)}`

async function listHooks(request: APIRequestContext): Promise<HookRow[]> {
  const res = await request.get('/api/hooks')
  expect(res.status(), await res.text()).toBe(200)
  return (await res.json()).hooks as HookRow[]
}

/**
 * Every test owns the hooks it acts on. The page lists whatever the gateway
 * holds, sorted by name, so acting on the first row would act on a hook another
 * test, worker or person created. Teardown deletes ONLY the ids this test
 * created, never a name sweep (the knowledge.spec.ts rule), so pointing the
 * suite at a live gateway cannot touch anyone else's hooks.
 */
const test = base.extend<{ ownHooks: OwnHooks }>({
  ownHooks: async ({ request }, use) => {
    const ids = new Set<string>()
    await use({
      async seed(fields = {}) {
        // Off by default: a seeded hook never fires on another spec's prompt,
        // and Test runs a hook whether or not it is enabled.
        const res = await request.post('/api/hooks', {
          data: {
            name: uniqueName('Seed'),
            event: 'UserPromptSubmit',
            command: 'echo "E2E test"',
            enabled: false,
            ...fields,
          },
        })
        expect(res.status(), await res.text()).toBe(200)
        const { hook } = await res.json()
        ids.add(hook.id)
        return hook as HookRow
      },
      track(id) {
        ids.add(id)
      },
      async read(id) {
        return (await listHooks(request)).find(h => h.id === id)
      },
    })
    // Every id is attempted before anything is asserted, so one refused
    // delete cannot leave the rest behind. 404: the test deleted its own hook.
    const refused: string[] = []
    for (const id of ids) {
      const res = await request.delete(`/api/hooks/${encodeURIComponent(id)}`)
      if (![200, 404].includes(res.status())) refused.push(`${id}: ${res.status()} ${await res.text()}`)
    }
    expect(refused, 'hooks this test created and could not delete').toEqual([])
  },
})

/** The row whose Name cell is exactly *name* (a substring match could pick a sibling). */
const rowFor = (page: Page, name: string) =>
  page.getByRole('row').filter({ has: page.getByRole('cell', { name, exact: true }) })

/** The response to *method* on exactly *pathname*, armed before the action that sends it. */
const responseTo = (page: Page, method: string, pathname: string): Promise<Response> =>
  page.waitForResponse(r => r.request().method() === method && new URL(r.url()).pathname === pathname)

const newHookButton = (page: Page) => page.getByRole('button', { name: '+ New Hook', exact: true })

/**
 * Open the page and wait until its list has loaded: HooksPage renders only a
 * loading line until GET /api/hooks settles, so the toolbar button is the ready
 * signal. Seed BEFORE calling this, so that first GET already holds the row.
 */
async function gotoHooks(page: Page) {
  await page.goto('/hooks', { waitUntil: 'domcontentloaded' })
  await expect(newHookButton(page)).toBeVisible({ timeout: 10000 })
}

test.describe('Hooks Page E2E Tests', () => {
  test('navigates to Hooks page and displays interface', async ({ page }) => {
    await gotoHooks(page)
  })

  test('displays existing hooks', async ({ page, ownHooks }) => {
    const hook = await ownHooks.seed({ name: uniqueName('Display'), command: 'echo "display test"' })
    await gotoHooks(page)

    const row = rowFor(page, hook.name)
    await expect(row).toBeVisible()
    await expect(row).toContainText('UserPromptSubmit')
    await expect(row).toContainText('echo "display test"')
  })

  test('creates a new hook', async ({ page, ownHooks }) => {
    await gotoHooks(page)
    await newHookButton(page).click()

    const nameInput = page.getByPlaceholder('Hook name', { exact: true })
    await expect(nameInput).toBeVisible()
    const name = uniqueName('Create')
    await nameInput.fill(name)
    await page.getByPlaceholder("echo 'hook fired'").fill('echo "E2E test"')

    const created = responseTo(page, 'POST', '/api/hooks')
    await page.getByRole('button', { name: 'Save', exact: true }).click()
    const res = await created
    expect(res.status(), await res.text()).toBe(200)
    const { hook } = await res.json()
    // Tracked before anything else can fail, so teardown still removes it.
    ownHooks.track(hook.id)
    expect(hook).toMatchObject({ name, command: 'echo "E2E test"' })

    await expect(nameInput).toBeHidden()
    await expect(rowFor(page, name)).toBeVisible()
  })

  test('cancels hook creation', async ({ page }) => {
    await gotoHooks(page)
    await newHookButton(page).click()

    await expect(page.getByPlaceholder(/hook name/i)).toBeVisible({ timeout: 3000 })

    // Click cancel
    await page.getByRole('button', { name: /cancel/i }).click()

    // Form should close
    await expect(page.getByPlaceholder(/hook name/i)).not.toBeVisible({ timeout: 3000 })
    await expect(newHookButton(page)).toBeVisible()
  })

  test('edits an existing hook', async ({ page, ownHooks }) => {
    const hook = await ownHooks.seed({ name: uniqueName('Edit'), command: 'echo "edit test"' })
    await gotoHooks(page)

    // Open the edit form for our row via the ⋯ overflow menu and save an update.
    await rowFor(page, hook.name).getByRole('button', { name: 'More actions', exact: true }).click()
    await page.getByRole('menuitem', { name: 'Edit', exact: true }).click()
    const nameInput = page.getByPlaceholder('Hook name', { exact: true })
    // The form is OUR hook's, not whichever row the menu happened to open.
    await expect(nameInput).toHaveValue(hook.name)
    const updatedName = uniqueName('Edited')
    await nameInput.fill(updatedName)

    const updated = responseTo(page, 'PUT', `/api/hooks/${hook.id}`)
    await page.getByRole('button', { name: 'Save', exact: true }).click()
    const res = await updated
    expect(res.status(), await res.text()).toBe(200)

    await expect(nameInput).toBeHidden()
    await expect(rowFor(page, updatedName)).toBeVisible()
    await expect(rowFor(page, hook.name)).toHaveCount(0)
    expect((await ownHooks.read(hook.id))?.name).toBe(updatedName)
  })

  test('toggles hook enabled state', async ({ page, ownHooks }) => {
    const hook = await ownHooks.seed({ name: uniqueName('Toggle') })
    await gotoHooks(page)
    const row = rowFor(page, hook.name)

    // The switch's name flips only once the toggle succeeded and the list was
    // refetched, so each label is the server-confirmed state, checked again
    // through the API. Both directions, so the hook ends where it started.
    await row.getByRole('button', { name: 'Enable hook', exact: true }).click()
    await expect(row.getByRole('button', { name: 'Disable hook', exact: true })).toBeVisible()
    expect((await ownHooks.read(hook.id))?.enabled).toBe(true)

    await row.getByRole('button', { name: 'Disable hook', exact: true }).click()
    await expect(row.getByRole('button', { name: 'Enable hook', exact: true })).toBeVisible()
    expect((await ownHooks.read(hook.id))?.enabled).toBe(false)
  })

  test('tests hook execution', async ({ page, ownHooks }) => {
    const marker = `pw_hook_test_${randomUUID().slice(0, 8)}`
    const hook = await ownHooks.seed({ name: uniqueName('Run'), command: `echo ${marker}` })
    await gotoHooks(page)

    const ran = responseTo(page, 'POST', `/api/hooks/${hook.id}/test`)
    await rowFor(page, hook.name).getByRole('button', { name: 'Test', exact: true }).click()
    const res = await ran
    expect(res.status(), await res.text()).toBe(200)
    const { result } = await res.json()
    expect(result.exit_code, JSON.stringify(result)).toBe(0)
    expect(result.stdout).toContain(marker)

    // Should show test results for THIS hook, with its output.
    await expect(page.getByText(`Test Result: ${hook.name}`, { exact: true })).toBeVisible({ timeout: 5000 })
    await expect(page.locator('pre').filter({ hasText: marker })).toBeVisible()
  })

  test('deletes a hook', async ({ page, ownHooks }) => {
    const hook = await ownHooks.seed({ name: uniqueName('Delete') })
    await gotoHooks(page)
    const row = rowFor(page, hook.name)

    // Delete arms on the first click (the label becomes "Delete?") and
    // deletes on the second, with no confirm dialog — fail the test if one
    // opens.
    let dialogOpened = false
    page.on('dialog', dialog => { dialogOpened = true; void dialog.dismiss() })

    await row.getByRole('button', { name: 'Delete', exact: true }).click()
    const confirm = row.getByRole('button', { name: 'Delete?', exact: true })
    await expect(confirm).toBeVisible()
    const deleted = responseTo(page, 'DELETE', `/api/hooks/${hook.id}`)
    await confirm.click()
    const res = await deleted
    expect(res.status(), await res.text()).toBe(200)

    await expect(row).toHaveCount(0)
    expect(await ownHooks.read(hook.id)).toBeUndefined()
    expect(dialogOpened).toBe(false)
  })

  test('changes event type', async ({ page }) => {
    await gotoHooks(page)
    await newHookButton(page).click()

    await expect(page.getByPlaceholder(/hook name/i)).toBeVisible({ timeout: 3000 })

    // Find and change event select
    await pickFromDropdown(page, 'Event', 'PreToolUse')

    // Matcher placeholder should change to tool filter placeholder
    await expect(page.getByPlaceholder(/tool filter.*fs_write/i)).toBeVisible({ timeout: 2000 })
  })

  test('updates timeout value', async ({ page }) => {
    await gotoHooks(page)
    await newHookButton(page).click()

    await expect(page.getByPlaceholder(/hook name/i)).toBeVisible({ timeout: 3000 })

    // Find timeout input
    const timeoutInput = page.locator('input[type="number"]')
    await timeoutInput.fill('60')

    // Verify value
    await expect(timeoutInput).toHaveValue('60')
  })
})

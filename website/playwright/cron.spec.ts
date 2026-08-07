import { test, expect } from '@playwright/test'
import { pickFromDropdown } from './helpers/dropdown'

// Cron management moved from an Overview tab to the standalone /schedule page
// (SchedulePage). The create/edit form is a slide-out panel opened via "Add
// Job" rendering JobForm in vertical layout: Name is a labelled <input> and
// Message a <textarea> (no placeholders), so anchor on their helper spans.
// Submit is "Create"; pause/resume/delete are per-row panel actions
// (delete via an arm→Confirm state machine on the same in-row button).

type Page = import('@playwright/test').Page

// Vertical-layout fields have no placeholders — anchor on their unique helper text.
const nameField = (page: Page) => page.locator('span:has-text("A short label for this job") ~ input')
const msgField = (page: Page) => page.locator('span:has-text("The prompt or task sent to the agent") ~ textarea')
// The schedule-kind dropdown, addressed by its accessible name. Its rows read
// "Every interval" / "Weekly schedule" / "Cron expression".
const pickSchedMode = (page: Page, label: string) => pickFromDropdown(page, 'Schedule', label)

const openForm = async (page: Page) => {
  await page.getByRole('button', { name: /add job/i }).click()
  await expect(nameField(page)).toBeVisible({ timeout: 5000 })
}

test.describe('Schedule (Cron) Page E2E Tests', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/schedule', { waitUntil: 'domcontentloaded' })
    await expect(page.getByRole('table')).toBeVisible({ timeout: 10000 })
  })

  // Failure-path cleanup. Each test deletes its own job inline on the pass path,
  // but a mid-test failure (plausible under the load this CR hardens against)
  // aborts before that delete and would leave an armed job firing real agent
  // turns on a personal :5476 gateway. Sweep any Playwright_*-named job by API so
  // cleanup runs even when a test body throws -- restores the failure-path
  // coverage the removed afterAll used to provide, without its cross-test coupling.
  test.afterEach(async ({ request }) => {
    // Best-effort teardown. GET /api/crons returns { jobs: [...] } (a wrapped
    // object, not a bare array like /api/chat/folders). Never let a cleanup
    // hiccup fail an otherwise-passing test.
    try {
      const body = await (await request.get('/api/crons')).json()
      const jobs = Array.isArray(body) ? body : (body?.jobs ?? [])
      for (const j of jobs) {
        if (typeof j?.name === 'string' && j.name.startsWith('Playwright_')) {
          await request.delete(`/api/crons/${j.id}`)
        }
      }
    } catch {
      // teardown is best-effort -- ignore cleanup errors
    }
  })

  test('displays existing cron jobs', async ({ page }) => {
    await expect(page.getByRole('columnheader', { name: 'Name' })).toBeVisible()
    await expect(page.getByRole('columnheader', { name: 'Schedule' })).toBeVisible()
    await expect(page.getByRole('columnheader', { name: 'Message' })).toBeVisible()
  })

  test('creates new cron job with interval schedule', async ({ page }) => {
    // Unique name (avoids strict-mode collisions across retries) + cleanup at
    // the end: without it the created every-2-hours job stays armed on the
    // target gateway and, on the documented local workflow (personal gateway
    // on 5476), would actually fire real agent turns on schedule.
    const jobName = `Playwright_Test_Job_${Date.now()}`
    await openForm(page)
    await nameField(page).fill(jobName)
    await msgField(page).fill('Run E2E tests every hour')
    await page.locator('input[type="number"]').first().fill('2')
    await pickFromDropdown(page, 'Every interval', 'hours')
    await page.getByRole('button', { name: /^create$/i }).click()
    const row = page.getByRole('row').filter({ hasText: jobName })
    await expect(row).toBeVisible({ timeout: 5000 })
    await expect(page.getByText('Run E2E tests every hour').first()).toBeVisible()
    // Cleanup: row-scoped arm→Confirm delete (see 'deletes a cron job').
    await row.getByRole('button', { name: /^delete$/i }).click()
    await row.getByRole('button', { name: /^confirm$/i }).click()
    await expect(page.getByRole('cell', { name: jobName })).toHaveCount(0, { timeout: 5000 })
  })

  test('creates cron job with weekly schedule', async ({ page }) => {
    const jobName = `Playwright_Weekly_Report_${Date.now()}`
    await openForm(page)
    await nameField(page).fill(jobName)
    await msgField(page).fill('Generate weekly metrics')
    await pickSchedMode(page, 'Weekly schedule')
    await page.getByRole('button', { name: /^mon$/i }).click()
    await page.getByRole('button', { name: /^fri$/i }).click()
    await page.locator('input[type="time"]').fill('09:00')
    await page.getByRole('button', { name: /^create$/i }).click()
    const row = page.getByRole('row').filter({ hasText: jobName })
    await expect(row).toBeVisible({ timeout: 5000 })
    // Cleanup: don't leave a live Mon/Fri 09:00 job armed on the gateway.
    await row.getByRole('button', { name: /^delete$/i }).click()
    await row.getByRole('button', { name: /^confirm$/i }).click()
    await expect(page.getByRole('cell', { name: jobName })).toHaveCount(0, { timeout: 5000 })
  })

  test('pauses and resumes a cron job', async ({ page }) => {
    // Self-contained: create a uniquely-named job, then pause/resume/delete
    // it by name. Targeting a fixture row by index (row.nth(1)) is fragile —
    // it assumes the fixture seeds jobs AND that ordering is stable across the
    // pause→reopen cycle; a re-sort would flip a different job and leak state.
    const jobName = `Playwright_Toggle_${Date.now()}`
    await openForm(page)
    await nameField(page).fill(jobName)
    await msgField(page).fill('Job created for pause/resume test')
    await page.locator('input[type="number"]').first().fill('1')
    await page.getByRole('button', { name: /^create$/i }).click()
    const row = page.getByRole('row').filter({ hasText: jobName })
    await expect(row).toBeVisible({ timeout: 5000 })

    // Pause/Resume moved OUT of the row and into its ⋯ overflow menu, so the
    // actions column fits the table width (six row buttons did not). The menu is
    // scoped to this job's row -- one ⋯ per row, so an unscoped page-level
    // locator is ambiguous (strict-mode violation once >1 job exists). The menu
    // itself renders in a portal at the document root, hence page-level item
    // locators after opening it.
    const pauseVia = async (label: RegExp) => {
      await row.getByRole('button', { name: /^actions$/i }).click()
      await page.getByRole('menuitem', { name: label }).click()
    }
    await pauseVia(/^pause$/i)
    await expect(row.getByRole('button', { name: /^run$/i })).toBeDisabled({ timeout: 5000 })
    await pauseVia(/^resume$/i)
    await expect(row.getByRole('button', { name: /^run$/i })).toBeEnabled({ timeout: 5000 })

    // Cleanup: row-scoped arm→Confirm delete.
    await row.getByRole('button', { name: /^delete$/i }).click()
    await row.getByRole('button', { name: /^confirm$/i }).click()
    await expect(page.getByRole('cell', { name: jobName })).toHaveCount(0, { timeout: 5000 })
  })

  test('deletes a cron job', async ({ page }) => {
    // Unique name: the seeded gateway already ships several jobs, and retries
    // re-run against the same shared home, so a fixed name collides (multiple
    // matching cells/rows -> strict-mode violation).
    const jobName = `Playwright_Delete_${Date.now()}`
    await openForm(page)
    await nameField(page).fill(jobName)
    await msgField(page).fill('Job created for deletion test')
    await page.locator('input[type="number"]').first().fill('1')
    await page.getByRole('button', { name: /^create$/i }).click()
    const row = page.getByRole('row').filter({ hasText: jobName })
    await expect(row).toBeVisible({ timeout: 5000 })

    // Delete is a two-click arm→confirm on the same in-row button: the first
    // click re-labels "Delete"→"Confirm", the second commits (see SchedulePage
    // confirmDeleteId state machine). Scope both clicks to this job's row.
    await row.getByRole('button', { name: /^delete$/i }).click()
    await row.getByRole('button', { name: /^confirm$/i }).click()
    await expect(page.getByRole('cell', { name: jobName })).toHaveCount(0, { timeout: 5000 })
  })

  test('filters cron jobs by search term', async ({ page }) => {
    await page.getByPlaceholder(/filter jobs/i).fill('test')
    await expect(page.getByRole('table')).toBeVisible()
  })

  test('validates required fields', async ({ page }) => {
    await openForm(page)
    await page.getByRole('button', { name: /^create$/i }).click()
    // Empty form → name checked first (message/agent/model/approval are only
    // required for the agent-message kind, not script/command crons).
    await expect(page.getByText(/name is required/i)).toBeVisible({ timeout: 3000 })
  })

  test('validates weekly mode requires day selection', async ({ page }) => {
    await openForm(page)
    await nameField(page).fill('Test')
    await msgField(page).fill('Test task')
    await pickSchedMode(page, 'Weekly schedule')
    await page.getByRole('button', { name: /^create$/i }).click()
    await expect(page.getByText(/select at least one day/i)).toBeVisible({ timeout: 3000 })
  })
})

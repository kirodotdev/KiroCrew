/**
 * Real-browser proof for the AgentSelector-inside-Radix-Dialog interaction
 * (#6358) that the happy-dom unit tests cannot exercise faithfully — the same
 * harness limit documented in verify-crews-dialog-select.mjs: Radix commits
 * its layer interplay through `ReactDOM.flushSync` dispatches that land inside
 * Testing Library's event batch, so the popup never opens under fireEvent.
 *
 * Drives the REAL built SPA (website/dist) behind the shared `serveDist`
 * server with every /api/** call answered from fixtures, against BOTH dialogs
 * that host the picker (#8963 — the edit dialog used to have no guard at all):
 *
 *   create: open /schedule -> Add job (Radix MODAL dialog) -> drive the picker
 *   edit:   reload -> click the fixture job's row (same JobDetailDialog,
 *           titled by the job's name) -> drive the same picker again
 *
 * The drill per dialog: open the agent picker -> click a non-default agent ->
 * assert the value committed AND the dialog stayed open -> reopen and assert
 * the keyboard path (filter input focused, ArrowDown roves to an option,
 * Enter on a narrowed filter commits) -> reopen and assert a real wheel event
 * scrolls the overflowing list -> Escape dismisses the popup, not the dialog.
 *
 * With the pre-fix build (bare createPortal to document.body) the option click
 * times out on Playwright's hit-test: react-remove-scroll's
 * `pointer-events: none` on the body swallows it — run with EXPECT=broken to
 * capture that state as the "before" evidence instead of failing. Broken mode
 * drives BOTH dialogs too: a regression back to a bare portal breaks the edit
 * dialog exactly like the create dialog, and an edit pass that cannot fail
 * would be worse than no edit pass at all.
 *
 * Usage: EXPECT=fixed|broken node scripts/verify-agent-selector-dialog.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi, logPageProblems } from './lib/stub-dashboard-api.mjs'

const EXPECT = process.env.EXPECT || 'fixed'
const OUT = process.argv[2] || '/tmp/agent-selector-6358-shots'
mkdirSync(OUT, { recursive: true })

const AGENTS = [
  { name: 'kirocrew', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', description: 'Default crew', source: 'builtin' },
  { name: 'oncall', kiro_agent: 'oncall-agent', workspace: 'oncall', memory_store: 'oncall-kb', description: 'Oncall crew', source: 'kirocrew' },
  { name: 'research', kiro_agent: 'kirocrew', workspace: 'research', memory_store: 'research-mem', description: 'Research crew', source: 'kirocrew' },
  // Filler rows so the list overflows its max-h-[280px] — the wheel-scroll
  // assertion below is vacuous on a list that fits.
  ...Array.from({ length: 9 }, (_, i) => ({
    name: `crew-${String(i + 1).padStart(2, '0')}`,
    kiro_agent: 'kirocrew',
    workspace: 'default',
    memory_store: 'default',
    description: `Filler crew ${i + 1}`,
    source: 'kirocrew',
  })),
]

// One persisted job so the schedule table has a row to click: the EDIT dialog
// is reachable only through an existing job (SchedulePage's `openDetail` on
// the row) — exactly the leg an empty cron fixture left undriven. A plain
// message job (no `script`/`command`) keeps the agent picker rendered, and
// `agent: 'kirocrew'` keeps the non-default selections below meaningful —
// driveAgentPicker asserts that invariant before relying on it. `every_secs`
// is the shape JobForm's parseJobDefaults actually recognises (the same shape
// verify-timezone-search.mjs seeds), so the edit dialog opens on a coherent
// "every 24 hours" interval instead of an empty cron-expression box.
const JOB = {
  id: 'job-01', name: 'Nightly digest', message: 'Summarize the day',
  enabled: true, schedule: 'every 24 hours', every_secs: 86400,
  last_status: 'ok', agent: 'kirocrew',
}

/** Open the create-job MODAL dialog from the schedule toolbar. The seeded
 *  fixture job removes the empty state, so "Add Job" is the only reachable
 *  entry point (the "Create your first job" arm renders only on an empty
 *  table). */
async function openCreateDialog(page, base) {
  await page.goto(base + '/schedule', { waitUntil: 'domcontentloaded' })
  await page.getByRole('button', { name: /Add Job/i }).first().waitFor({ timeout: 15000 })
  await page.getByRole('button', { name: /Add Job/i }).first().click()
  const dialog = page.getByRole('dialog', { name: 'New job' })
  await dialog.waitFor({ timeout: 10000 })
  return dialog
}

/** Open the EDIT dialog by clicking the fixture job's row (same JobDetailDialog,
 *  titled by the job's name, Details tab active by default). */
async function openEditDialog(page, base) {
  await page.goto(base + '/schedule', { waitUntil: 'domcontentloaded' })
  await page.getByText(JOB.name, { exact: true }).first().waitFor({ timeout: 15000 })
  await page.getByText(JOB.name, { exact: true }).first().click()
  const dialog = page.getByRole('dialog', { name: JOB.name })
  await dialog.waitFor({ timeout: 10000 })
  return dialog
}

/**
 * THE interaction under test, identical for both host dialogs: the agent
 * picker opened and committed from inside a Radix MODAL dialog. `shot`
 * prefixes the screenshots and error messages so the create and edit passes
 * stay distinguishable.
 */
async function driveAgentPicker(page, dialog, shot) {
  const trigger = dialog.getByRole('button', { name: 'Switch agent' })

  // The commit assertions below are after-only, so they are vacuous if the
  // trigger already reads the agent we are about to pick — which the EDIT
  // dialog seeds data-driven from the fixture's `agent`. Guard the invariant
  // instead of trusting the fixture comment (same shape as the vacuous-wheel
  // guard further down).
  const before = await trigger.textContent()
  if (before?.includes('oncall') || before?.includes('research')) {
    throw new Error(`[${shot}] trigger already reads "${before}" — the commit assertions are vacuous; keep the fixture on a different agent`)
  }

  await trigger.click()
  const listbox = page.getByRole('listbox', { name: 'Agent list' })
  await listbox.waitFor({ timeout: 5000 })

  await page.screenshot({ path: join(OUT, `${shot}-dropdown-open.png`) })

  await page.getByRole('option', { name: /oncall/ }).click({ timeout: 5000 })

  // The selection must commit…
  const committed = await trigger.textContent()
  if (!committed?.includes('oncall')) {
    throw new Error(`[${shot}] agent selection did not commit: trigger reads "${committed}"`)
  }
  // …and Radix's DismissableLayer must treat it as INSIDE the dialog's layer
  // stack: an outside-interaction would have closed the whole dialog.
  if (!(await dialog.count())) {
    throw new Error(`[${shot}] selecting an agent closed the job dialog underneath`)
  }
  await page.screenshot({ path: join(OUT, `${shot}-selection-committed.png`) })

  // Keyboard path: reopen — the filter input must take focus (the dialog's
  // FocusScope used to reclaim it), ArrowDown must rove to an option, and
  // Enter on a narrowed filter must commit.
  await trigger.click()
  await listbox.waitFor({ timeout: 5000 })
  const input = page.getByLabel('Filter agents')
  if (!(await input.evaluate(el => el === document.activeElement))) {
    throw new Error(`[${shot}] filter input did not take focus inside the modal dialog`)
  }
  await page.keyboard.press('ArrowDown')
  const onOption = await page.evaluate(() => document.activeElement?.getAttribute('role') === 'option')
  if (!onOption) throw new Error(`[${shot}] ArrowDown did not move focus to an option (keyboard still dead)`)
  await page.keyboard.press('ArrowUp')
  await input.pressSequentially('res')
  await page.screenshot({ path: join(OUT, `${shot}-keyboard-filter.png`) })
  await page.keyboard.press('Enter')
  const kbCommitted = await trigger.textContent()
  if (!kbCommitted?.includes('research')) {
    throw new Error(`[${shot}] keyboard selection did not commit: trigger reads "${kbCommitted}"`)
  }
  if (!(await dialog.count())) {
    throw new Error(`[${shot}] keyboard selection closed the job dialog underneath`)
  }

  // Escape must dismiss only the popup on reopen, never the dialog.
  await trigger.click()
  await listbox.waitFor({ timeout: 5000 })

  // The option list must also SCROLL inside the modal: the popover portals
  // outside DialogContent, so it sits in neither react-remove-scroll's lock
  // container nor its shards — react-remove-scroll cancels wheel events it
  // does not recognise, so drive a REAL wheel over the list and assert it
  // moved (with a long roster this is a third way the picker could be
  // "unusable inside dialogs").
  const scrollable = await listbox.evaluate(el => el.scrollHeight > el.clientHeight)
  if (!scrollable) {
    throw new Error(`[${shot}] fixture roster does not overflow the list — the wheel assertion is vacuous`)
  }
  await listbox.hover()
  await page.mouse.wheel(0, 120)
  await page.waitForTimeout(200)
  const scrolled = await listbox.evaluate(el => el.scrollTop)
  if (scrolled <= 0) {
    throw new Error(`[${shot}] wheel over the agent list did not scroll it inside the modal dialog`)
  }

  await page.keyboard.press('Escape')
  await listbox.waitFor({ state: 'detached', timeout: 5000 })
  if (!(await dialog.count())) {
    throw new Error(`[${shot}] Escape on the agent popup also closed the job dialog underneath`)
  }
}

/**
 * The broken-build assertion, identical for both host dialogs: the popup
 * renders but sits under the modal's pointer-events cut, so the click on an
 * option never lands. Playwright's hit-test surfaces exactly that — the
 * timeout IS the defect.
 */
async function confirmClickThrough(page, dialog, shot) {
  const trigger = dialog.getByRole('button', { name: 'Switch agent' })
  await trigger.click()
  const listbox = page.getByRole('listbox', { name: 'Agent list' })
  await listbox.waitFor({ timeout: 5000 })
  await page.screenshot({ path: join(OUT, `${shot}-dropdown-open.png`) })

  let clickLanded = true
  try {
    await page.getByRole('option', { name: /oncall/ }).click({ timeout: 3000 })
  } catch {
    clickLanded = false
  }
  if (clickLanded) {
    const committed = await trigger.textContent()
    if (committed?.includes('oncall')) {
      throw new Error(`[${shot}] EXPECT=broken but the option click committed — is this the fixed build?`)
    }
  }
  await page.screenshot({ path: join(OUT, `${shot}-click-through.png`) })
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()

try {
  const context = await browser.newContext({ viewport: { width: 1400, height: 950 }, deviceScaleFactor: 1 })
  const page = await context.newPage()
  logPageProblems(page)

  const errors = []
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()) })
  page.on('pageerror', e => errors.push(`pageerror: ${e.message}`))

  await stubDashboardApi(page, {
    extra: async (path, route) => {
      if (path === '/api/agents') {
        await json(route, { agents: AGENTS, default_agent: 'kirocrew' })
        return true
      }
      if (path === '/api/crons') {
        await json(route, { jobs: [JOB] })
        return true
      }
      return false
    },
  })
  await page.addInitScript(() => localStorage.setItem('mc-lang', 'en'))

  if (EXPECT === 'broken') {
    const createDialog = await openCreateDialog(page, base)
    await confirmClickThrough(page, createDialog, 'before-create')
    // Fresh page state between passes: the broken build's popup teardown is
    // exactly what cannot be trusted, so never dismiss our way back to the list.
    const editDialog = await openEditDialog(page, base)
    await confirmClickThrough(page, editDialog, 'before-edit')
    console.log('OK (broken build confirmed): option click does not land / does not commit in either dialog')
  } else {
    const createDialog = await openCreateDialog(page, base)
    await driveAgentPicker(page, createDialog, 'after-create')
    const editDialog = await openEditDialog(page, base)
    await driveAgentPicker(page, editDialog, 'after-edit')
    console.log('OK: select-in-dialog commits (mouse + keyboard) in create AND edit dialogs, dialog survives, Escape scoped')
  }

  await context.close()

  const real = errors.filter(e => !/favicon|Failed to load resource/i.test(e))
  if (real.length) {
    console.error('CONSOLE ERRORS:\n' + real.join('\n'))
    process.exit(1)
  }
} finally {
  await browser.close()
  srv.close()
}

/**
 * Screenshot harness for #4297: the hooks-table Actions cell collapsed from
 * three peer buttons (Test / Edit / Delete) to the CronRowActions overflow
 * shape — Test in the row, Edit in the ⋯ menu, Delete as a row-level
 * arm→Confirm button (a menu that closes on select cannot host the armed
 * state, so Delete stays out of the overflow).
 *
 * Four frames, matching the PR's committed evidence set:
 *   hooks-desktop-1280-after.png  — the collapsed row at rest, 1280×800
 *   hooks-mobile-390-after.png    — the same at the AUTOSDE phone baseline
 *   hooks-menu-open-after.png     — the ⋯ overflow open (Test + Edit, no Delete)
 *   hooks-armed-delete-after.png  — Delete armed ("Delete?"), nothing deleted
 *
 * Runs the REAL built SPA (website/dist) with every /api/** call answered from
 * fixtures — gateway-free. Same technique as capture-armed-delete-touch.mjs.
 * Labels are read from the CATALOG, so a key rename breaks the capture loudly
 * instead of silently screenshotting the wrong element.
 *
 * Usage: node scripts/capture-hooks-actions-overflow.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/hooks-actions-overflow-4297'

mkdirSync(OUT, { recursive: true })

const LOCALES = fileURLToPath(new URL('../src/i18n/locales/', import.meta.url))
const manual = JSON.parse(readFileSync(LOCALES + 'en.manual.json', 'utf-8'))
const hp = manual.pages.hooksPage
const ARMED_LABEL = hp.confirm_delete_hook          // "Delete?"
const TRIGGER_LABEL = hp.more_actions               // "More actions"
if (!ARMED_LABEL) throw new Error('catalog key confirm_delete_hook missing — renamed?')
if (!TRIGGER_LABEL) throw new Error('catalog key more_actions missing — renamed?')

const now = Math.floor(Date.now() / 1000)
const HOOKS = [
  {
    id: 'hk-1', name: 'log prompts', event: 'UserPromptSubmit', matcher: '', matcher_mode: 'glob',
    command: 'echo prompt >> /tmp/log.txt', skills: [], timeout: 30, enabled: true,
    last_run: now - 3600, last_status: 'ok', run_count: 42,
  },
  {
    id: 'hk-2', name: 'guard writes', event: 'PreToolUse', matcher: 'fs_write', matcher_mode: 'glob',
    command: '~/.kiro/hooks/guard-writes.sh', skills: [], timeout: 10, enabled: true,
    last_run: now - 300, last_status: 'ok', run_count: 187,
  },
  {
    id: 'hk-3', name: 'notify done', event: 'Stop', matcher: '', matcher_mode: 'glob',
    command: 'notify-send "turn finished"', skills: [], timeout: 30, enabled: false,
    last_run: now - 86400, last_status: 'error', run_count: 9,
  },
  {
    id: 'hk-4', name: 'backup transcripts', event: 'PostToolUse', matcher: 'fs_write', matcher_mode: 'glob',
    command: 'rsync -a ~/.kiro/history/ ~/backups/history/', skills: [], timeout: 120, enabled: true,
    last_run: 0, last_status: '', run_count: 0,
  },
]

const stub = page => stubDashboardApi(page, {
  extra: async (path, route) => {
    if (path === '/api/hooks') { await json(route, { hooks: HOOKS }); return true }
    return false
  },
})

async function openHooks(page, base) {
  await page.goto(base + '/hooks', { waitUntil: 'domcontentloaded' })
  await page.getByRole('table').waitFor({ timeout: 15000 })
  await page.getByText('guard writes').first().waitFor()
  await page.waitForTimeout(400)
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()

  // Desktop frames: rest, menu open, armed delete.
  const desktop = await browser.newContext({ viewport: { width: 1280, height: 800 } })
  const dPage = await desktop.newPage()
  logPageProblems(dPage)
  await stub(dPage)
  await openHooks(dPage, base)
  await dPage.screenshot({ path: `${OUT}/hooks-desktop-1280-after.png` })

  const row = dPage.getByRole('row').filter({ hasText: 'guard writes' })
  await row.getByRole('button', { name: TRIGGER_LABEL, exact: true }).click()
  // The overflow is a complete account of the hook's actions except Delete,
  // which stays a row-level button so its armed state survives the menu close.
  await dPage.getByRole('menuitem', { name: 'Edit', exact: true }).waitFor({ timeout: 5000 })
  const deleteItems = await dPage.getByRole('menuitem', { name: 'Delete', exact: true }).count()
  if (deleteItems !== 0) throw new Error('Delete found inside the overflow menu — arm→Confirm cannot live there')
  await dPage.screenshot({ path: `${OUT}/hooks-menu-open-after.png` })
  await dPage.keyboard.press('Escape')

  await row.getByRole('button', { name: 'Delete', exact: true }).click()
  // The visible armed label IS the accessible name (deliberately no
  // aria-label — WCAG 2.5.3 Label in Name); assert it so the capture cannot
  // silently photograph a regression.
  const armed = row.getByRole('button', { name: ARMED_LABEL, exact: true })
  await armed.waitFor({ timeout: 5000 })
  const aria = await armed.getAttribute('aria-label')
  if (aria !== null) throw new Error(`armed button carries aria-label ${JSON.stringify(aria)} — overrides the visible label (WCAG 2.5.3)`)
  await dPage.screenshot({ path: `${OUT}/hooks-armed-delete-after.png` })
  await desktop.close()

  // Phone frame at the AUTOSDE narrow-viewport baseline.
  const phone = await browser.newContext({
    viewport: { width: 390, height: 844 },
    deviceScaleFactor: 2, // phone-density; frames stay ~780px wide, well under the 2000px read ceiling
    hasTouch: true,
  })
  const pPage = await phone.newPage()
  logPageProblems(pPage)
  await stub(pPage)
  await openHooks(pPage, base)
  // Scroll the table's scroller fully right so the frame shows the Actions
  // column — the 390px payoff this evidence set exists to prove. The before
  // frame is captured at the same scroll position (UX review: opposite scroll
  // positions made the payoff unverifiable from the pixels).
  await pPage.getByRole('table').evaluate(t => {
    let n = t.parentElement
    while (n && n.scrollWidth <= n.clientWidth) n = n.parentElement
    if (!n) throw new Error('no horizontally scrollable ancestor found for the hooks table')
    n.scrollLeft = n.scrollWidth
  })
  await pPage.waitForTimeout(300)
  await pPage.screenshot({ path: `${OUT}/hooks-mobile-390-after.png` })
  await phone.close()

  await browser.close()
  srv.close()
  console.log(`wrote 4 frames to ${OUT}`)
}

main().catch(err => { console.error(err); process.exit(1) })

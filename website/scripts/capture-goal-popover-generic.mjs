/**
 * Screenshot harness for the composer's GENERIC goal-loop popover.
 *
 * Opens the real built SPA (website/dist) against the shared API stub, clicks
 * the composer's "Set a goal" trigger and photographs the popover that opens.
 * It asserts, not just photographs: the popover must carry the generic goal
 * textarea and must NOT carry a pull-request URL field, so a regression back to
 * a pull-request-only form fails this script instead of shipping quietly.
 *
 * Usage: node scripts/capture-goal-popover-generic.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/goal-popover-generic'
const SLOT = 'chat-goal'
const PROJECT = '/home/user/workspace/notes'

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Keep the nightly build green',
  running: false,
  last_message: 'Ready when you are.',
  messages: 1,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  folder_id: '',
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: false,
  has_more: false,
  total: 1,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'assistant', ts: Date.now() / 1000 - 30, content: 'Ready when you are.' },
  ],
}

let failed = false
function check(name, ok, detail) {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
  return ok
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1400, height: 900 }, deviceScaleFactor: 2 })

  const extra = async (path, route) => {
    if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
    if (path.startsWith('/api/autonudge')) { await json(route, { loops: [] }); return true }
    return false
  }

  for (const theme of ['dark', 'light']) {
    const page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, { folders: [], slots, theme, extra })
    await page.addInitScript(slot => { localStorage.setItem('mc-active-slot', slot) }, SLOT)
    await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2000)

    const trigger = page.getByRole('button', { name: 'Set a goal' }).first()
    await trigger.waitFor({ timeout: 10_000 })
    await trigger.click()
    await page.waitForTimeout(600)

    const goalField = await page.getByRole('textbox', { name: /goal/i }).count()
    const prField = await page.getByText(/pull request url/i).count()
    const legacyCta = await page.getByText(/legacy goal loop/i).count()
    const ok = check(
      `01-generic-goal-popover-${theme}`,
      goalField >= 1 && prField === 0 && legacyCta === 0,
      `goalField=${goalField} prUrlField=${prField} legacyCta=${legacyCta}`,
    )
    if (ok) await page.screenshot({ path: `${OUT}/01-generic-goal-popover-${theme}.png` })
    await page.close()
  }

  await browser.close()
  srv.close()
  process.exit(failed ? 1 : 0)
}

main().catch(err => { console.error(err); process.exit(2) })

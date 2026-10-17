/**
 * Screenshot harness, and behaviour check, for the SYSTEM page's Sessions table over the
 * crew whose conductor was closed: the second surface the one backend join feeds.
 *
 * Two frames over the same payload through the real `SessionsTab`:
 *   before -- each worker carries a null key, which is what the table received before
 *             this change: three top-level rows, each saying it was created by a
 *             session that is not running.
 *   after  -- each worker resolves to the lead with `ancestor`. All three nest under
 *             the lead's row, and the citation on each still names the closed
 *             conductor rather than the lead it now hangs from.
 *
 * The second assertion is the one `SessionsTab.creatorOf` exists for: `parent.key` is
 * the ancestor on such a row, so naming it there would credit the creating to a session
 * that did not do it.
 *
 * Serves the capture page from the DEV server
 * (`/capture/sessions-table-closed-conductor.html`); the page answers
 * `/api/sessions/memory` itself and every other boot fixture comes from the shared stub.
 *
 * Usage: node scripts/capture-sessions-table-closed-conductor.mjs [devBase] [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'
import { stubDashboardApi, logPageProblems } from './lib/stub-dashboard-api.mjs'

const BASE = process.argv[2] || 'http://127.0.0.1:6181'
const OUT = process.argv[3] || '../temp-screenshots/sessions-table-closed-conductor'
const LEAD = 'Remote crew lane: drive the three PRs to green'
const CONDUCTOR = 'chat-2552'
const CREW = [
  'I1: MicroVM boot path and the lease',
  'I2: Fargate task definition',
  'I3: token handoff to the remote crew',
]

mkdirSync(OUT, { recursive: true })

let failed = false
const check = (label, ok, detail) => {
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${label}${detail ? ` — ${detail}` : ''}`)
  if (!ok) failed = true
}

const browser = await chromium.launch({ executablePath: chromiumExecutable() })
const context = await browser.newContext({ viewport: { width: 1180, height: 620 }, deviceScaleFactor: 2 })
const page = await context.newPage()
page.on('pageerror', e => { console.log(`FAIL pageerror — ${e.message}`); failed = true })
await stubDashboardApi(page, {
  theme: 'dark',
  folders: [],
  extra: async (path, route) => {
    if (path.startsWith('/api/')) return false
    await route.continue()
    return true
  },
})
logPageProblems(page)

/**
 * Each row's name-cell text and its nesting depth, in table order.
 *
 * Depth is read from the guide rails the name cell draws, one per level
 * (`data-depth-guide`), rather than from the cell's padding: the padding is a computed
 * pixel figure and the rails are the row's own statement of how deep it sits.
 */
const rows = () => page.$$eval('tbody tr', trs => trs.map(tr => ({
  text: (tr.querySelector('td')?.textContent ?? '').trim(),
  indent: tr.querySelectorAll('[data-depth-guide]').length,
})))
const rowOf = async name => (await rows()).find(r => r.text.startsWith(name))

async function load(query) {
  await page.goto(`${BASE}/capture/sessions-table-closed-conductor.html?theme=dark${query}`)
  await page.waitForSelector('[data-capture-ready]')
  await page.waitForSelector(`text=${CREW[0]}`)
  await page.waitForTimeout(600)
}

// ── before: a closed creator leaves every worker at the top level ────────────
await load('')
console.log('before:', (await rows()).map(r => `${r.indent}:${r.text.slice(0, 24)}`).join(' | '))
const leadBefore = await rowOf(LEAD)
check('before: the lead is a top-level row', leadBefore?.indent === 0, `indent=${leadBefore?.indent}`)
for (const w of CREW) {
  const r = await rowOf(w)
  check(`before: ${w.slice(0, 12)} sits at the lead's level`, r?.indent === leadBefore?.indent,
    `indent=${r?.indent}`)
  check(`before: ${w.slice(0, 12)} says its creator is not running`,
    (r?.text ?? '').includes(CONDUCTOR), `text=${r?.text?.slice(0, 90)}`)
}
await page.screenshot({ path: `${OUT}/before-workers-top-level-in-the-table.png` })

// ── after: the walk reaches the lead, which is this change ───────────────────
await load('&reparent=1')
console.log('after: ', (await rows()).map(r => `${r.indent}:${r.text.slice(0, 24)}`).join(' | '))
const leadAfter = await rowOf(LEAD)
check('after: the lead is still a top-level row', leadAfter?.indent === 0, `indent=${leadAfter?.indent}`)
for (const w of CREW) {
  const r = await rowOf(w)
  check(`after: ${w.slice(0, 12)} is indented under the lead`, (r?.indent ?? 0) > (leadAfter?.indent ?? 0),
    `lead=${leadAfter?.indent} worker=${r?.indent}`)
  // The citation stays, and it must name the CLOSED CONDUCTOR -- never the lead the
  // row now hangs from, whose expander already names itself one line up.
  check(`after: ${w.slice(0, 12)} still says its creator closed`,
    (r?.text ?? '').includes(`Created by ${CONDUCTOR} (closed)`), `text=${r?.text?.slice(0, 100)}`)
  check(`after: ${w.slice(0, 12)} does not credit the lead with opening it`,
    !(r?.text ?? '').includes('Created by Remote crew lane'), `text=${r?.text?.slice(0, 100)}`)
}
check('after: the lead can be collapsed, so the table knows the crew is under it',
  !!(await page.$('button[aria-label*="Collapse sessions under Remote crew lane"]')))
await page.screenshot({ path: `${OUT}/after-workers-nested-under-the-lead-in-the-table.png` })

await browser.close()
console.log(failed ? 'RESULT: FAIL' : 'RESULT: ok')
process.exit(failed ? 1 : 0)

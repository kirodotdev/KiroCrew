/**
 * Screenshot harness for the Switch All Sessions panel's peer-bound outcome.
 *
 * What is under test is a DISTINCTION, so several frames are needed to show it:
 *   1. a switch that skipped peer-bound sessions reports them as STATUS text and
 *      holds the panel open — nothing failed, so no ErrorNotice appears,
 *   2. a switch that both failed somewhere AND skipped peer-bound sessions shows
 *      the two side by side, neither masking the other,
 *   3. a failure with no peer-bound skip: the control frame, on the path this
 *      panel already had before the notice existed,
 *   4. every eligible session reported peer-bound: the count reaches zero and the
 *      submit is disabled at "Switch 0 sessions", the boundary the coverage test
 *      pins and the outcome the notice alone has to explain.
 *
 * Runs the REAL built SPA (website/dist) behind an in-process static server with
 * every /api/** call answered from fixtures, so no gateway and no kiro-cli are
 * needed.
 *
 * Usage: node scripts/capture-peer-bound-bulk-switch.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/peer-bound-bulk-switch'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

// model: '' on every row so picking `auto` counts all three as affected and the
// submit button reads "Switch 3 sessions" — the label the user acts on.
const slots = ['Release notes draft', 'Flaky test triage', 'Docs audit'].map((title, i) => ({
  key: `k-${i + 1}`,
  title,
  messages: 3,
  running: false,
  agent: 'kirocrew',
  model: '',
  created: '2026-09-10T01:00:00Z',
  last_ts: new Date(Date.parse('2026-09-13T18:00:00Z') - i * 3600_000).toISOString(),
  folder_id: '',
}))

// Two successive POSTs, two outcomes. The endpoint's own contract: 200 with
// per-slot buckets, so a partial outcome is a success body and not an error.
// `skipped_remote` carries the SLOT KEYS the endpoint left alone (it appends
// `name` from `state._slots`), which is what lets the panel subtract them from
// the "Switch N" label after the response — so the fixture must name real rows.
const RESPONSES = [
  { ok: true, model: 'auto', switched: ['k-1', 'k-2'], skipped_running: [], skipped_remote: ['k-3'], unchanged: [], failed: [] },
  { ok: true, model: 'auto', switched: ['k-1'], skipped_running: [], skipped_remote: ['k-2', 'k-3'], unchanged: [], failed: ['k-1'] },
  // Third outcome is the PRE-EXISTING path: a failure with no peer-bound skip,
  // i.e. exactly what this panel rendered before the notice existed. It is the
  // control frame for whether the button-row squeeze is new or old.
  { ok: true, model: 'auto', switched: ['k-1'], skipped_running: [], skipped_remote: [], unchanged: [], failed: ['k-2'] },
  // Fourth outcome is the boundary the coverage test pins ("disables Switch when
  // every eligible session was reported peer-bound"): the server reports every
  // eligible slot as peer-bound, so the subtraction takes the count to zero and
  // the submit button is disabled at "Switch 0 sessions" with the notice as the
  // only explanation on screen. The disabled-at-zero guard itself predates this
  // change; what is new is reaching it because the peer owns every session.
  { ok: true, model: 'auto', switched: [], skipped_running: [], skipped_remote: ['k-1', 'k-2', 'k-3'], unchanged: [], failed: [] },
]
let posted = 0

const switchButton = page => page.getByRole('button', { name: /^Switch \d+ sessions?$/ })

async function shotPanel(page, path) {
  // Screenshot the panel itself rather than a hand-measured clip, so the frame
  // stays correct if the panel moves. The heading's parent IS the panel div.
  const panel = page.getByText('Switch All Sessions').locator('..')
  const box = await panel.boundingBox()
  if (!box) throw new Error('panel not found on screen')
  const pad = 10
  await page.screenshot({
    path,
    clip: { x: Math.max(0, box.x - pad), y: Math.max(0, box.y - pad), width: box.width + pad * 2, height: box.height + pad * 2 },
  })
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 })
  const page = await context.newPage()

  await stubDashboardApi(page, {
    slots,
    extra: async (path, route) => {
      // Must return TRUTHY after fulfilling: the shared stub awaits this hook and
      // falls through to its own fulfill on a falsy result, which throws
      // "Route is already handled!" — and `json()` resolves to undefined.
      if (path === '/api/chat/slots/model') {
        await json(route, RESPONSES[Math.min(posted++, RESPONSES.length - 1)])
        return true
      }
      return false
    },
  })
  logPageProblems(page)

  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)

  await page.getByRole('button', { name: 'More options' }).first().click()
  await page.getByText('Switch all to model…').click()
  await page.waitForTimeout(500)
  await page.getByRole('option', { name: /auto/i }).first().click()
  await page.waitForTimeout(300)

  const label0 = await switchButton(page).textContent()
  console.log('BEFORE submit button:', label0)
  if (label0 !== 'Switch 3 sessions') throw new Error(`expected "Switch 3 sessions" before the first submit, got ${JSON.stringify(label0)}`)

  await switchButton(page).click()
  await page.waitForSelector('[data-testid="bulk-model-notice"]')
  await page.waitForTimeout(400)
  const notice1 = await page.locator('[data-testid="bulk-model-notice"]').textContent()
  const label1 = await switchButton(page).textContent()
  console.log('FRAME 1 notice:', notice1)
  console.log('FRAME 1 button:', label1)
  console.log('FRAME 1 error present:', await page.locator('[data-testid="bulk-model-error"]').count())
  // 3 eligible − 1 reported peer-bound = 2: the label and the notice must agree.
  if (!/^1 session runs on another of your machines and kept its model/.test(notice1)) throw new Error(`frame 1 notice: ${notice1}`)
  if (label1 !== 'Switch 2 sessions') throw new Error(`frame 1 button should read "Switch 2 sessions", got ${JSON.stringify(label1)}`)
  await shotPanel(page, `${OUT}/${PREFIX}-skipped-only.png`)

  await switchButton(page).click()
  await page.waitForSelector('[data-testid="bulk-model-error"]')
  await page.waitForTimeout(400)
  const notice2 = await page.locator('[data-testid="bulk-model-notice"]').textContent()
  const label2 = await switchButton(page).textContent()
  console.log('FRAME 2 notice:', notice2)
  console.log('FRAME 2 button:', label2)
  console.log('FRAME 2 error:', await page.locator('[data-testid="bulk-model-error"]').textContent())
  // 3 eligible − 2 reported peer-bound = 1.
  if (!/^2 sessions run on your other machines and kept their models/.test(notice2)) throw new Error(`frame 2 notice: ${notice2}`)
  if (label2 !== 'Switch 1 session') throw new Error(`frame 2 button should read "Switch 1 session", got ${JSON.stringify(label2)}`)
  await shotPanel(page, `${OUT}/${PREFIX}-failed-and-skipped.png`)

  await switchButton(page).click()
  await page.waitForTimeout(900)
  console.log('FRAME 3 notice count:', await page.locator('[data-testid="bulk-model-notice"]').count())
  console.log('FRAME 3 button:', await switchButton(page).textContent())
  console.log('FRAME 3 error:', await page.locator('[data-testid="bulk-model-error"]').textContent())
  await shotPanel(page, `${OUT}/${PREFIX}-failed-only-control.png`)

  await switchButton(page).click()
  await page.waitForSelector('[data-testid="bulk-model-notice"]')
  await page.waitForTimeout(400)
  const notice4 = await page.locator('[data-testid="bulk-model-notice"]').textContent()
  const label4 = await switchButton(page).textContent()
  const disabled4 = await switchButton(page).isDisabled()
  console.log('FRAME 4 notice:', notice4)
  console.log('FRAME 4 button:', label4, 'disabled:', disabled4)
  console.log('FRAME 4 error present:', await page.locator('[data-testid="bulk-model-error"]').count())
  // 3 eligible − 3 reported peer-bound = 0, so the submit is disabled and the
  // notice is the only thing on screen accounting for the three untouched rows.
  if (!/^3 sessions run on your other machines and kept their models/.test(notice4)) throw new Error(`frame 4 notice: ${notice4}`)
  if (label4 !== 'Switch 0 sessions') throw new Error(`frame 4 button should read "Switch 0 sessions", got ${JSON.stringify(label4)}`)
  if (!disabled4) throw new Error('frame 4 submit button should be disabled at a zero count')
  await shotPanel(page, `${OUT}/${PREFIX}-all-peer-bound.png`)

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })

/**
 * Screenshot harness for the unreadable-send-receipt contract.
 *
 * Runs the REAL built SPA (website/dist) behind the shared `serveDist` server
 * and answers every /api/** call from fixtures through the shared
 * `stubDashboardApi` helper. No gateway, no dashboard auth, no kiro-cli.
 *
 * The scene-specific stub is one route: `POST /api/chat` answers **HTTP 200**
 * with a body that stops mid-JSON. Nothing here fabricates a status the backend
 * does not produce -- the status is the ordinary acceptance the handler really
 * returns, and the truncated body is what the wire delivers when a response is
 * cut short (a dropped connection mid-transfer, a proxy that terminates the
 * stream). That pair is the whole point: the request WAS accepted, so the turn
 * may be running, and only the receipt is unreadable.
 *
 * Two shots are taken against the same stub, so the delta is the code and not
 * the fixture:
 *   - `before-*` on the pre-fix build: a red "Send failed" row, and the payload
 *     back in the composer inviting a retry that duplicates a delivered turn.
 *   - `after-*` on the fixed build: no error row, composer left clear.
 *
 * Usage: node scripts/capture-send-receipt-unknown.mjs <outDir> <before|after>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/send-receipt-unknown-shots'
const PHASE = process.argv[3] === 'before' ? 'before' : 'after'

mkdirSync(OUT, { recursive: true })

const SLOT = 'chat-1'
const MESSAGE = 'ship the release notes'

/** A 200 whose JSON stops mid-object — what a cut-short response looks like. */
const TRUNCATED_RECEIPT = '{"ok": true, "run_'

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1500, height: 950 }, deviceScaleFactor: 2 })
const page = await context.newPage()

/** Each branch AWAITS its fulfil then returns true; falsy means "not handled". */
const extra = async (path, route) => {
  if (path === '/api/chat') {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: TRUNCATED_RECEIPT,
    })
    return true
  }
  return false
}

await stubDashboardApi(page, {
  slots: [{ key: SLOT, messages: 0, running: false, agent: 'kirocrew', mode: '' }],
  extra,
  // Pin the locale: without it the SPA picks one from the environment and the
  // shot comes out in whatever language the runner happens to negotiate.
  localStorageEntries: { 'mc-active-slot': SLOT, 'mc-lang': 'en' },
})

await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(2500)

const composer = page.getByLabel('Message input').first()
await composer.waitFor({ state: 'visible', timeout: 10000 })
await composer.fill(MESSAGE)
await composer.press('Enter')

// The send is bounded by a 10s abort, and the branch under test settles as soon
// as the receipt is read. Waiting past the optimistic render is enough for both
// phases: the pre-fix build appends its error row synchronously with the read.
await page.waitForTimeout(3000)

const out = join(OUT, `${PHASE}-01-unreadable-receipt.png`)
await page.screenshot({ path: out })
console.log('wrote', out)

// Report what the two states actually differ on, so the harness is falsifiable
// rather than "trust the pixels": the error row, and the composer's contents.
const errorRows = await page.getByTestId('error-card').count()
const composerText = await composer.inputValue()
console.log(JSON.stringify({ phase: PHASE, errorRows, composerText }))

await browser.close()
srv.close()

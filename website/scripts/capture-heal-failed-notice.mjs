/**
 * Screenshot harness for the HEAL-FAILED notice (PR #15029).
 *
 * After an allow or revoke, the open chat re-reads its transcript so a revoked
 * link stops being clickable. When that re-read fails, the chat says so with a
 * Retry instead of leaving the outdated rows on screen silently. This harness
 * loads a chat in the REAL built SPA (website/dist), then fires the same
 * `mc:redaction-hosts-changed` event a redaction card's Allow/Undo fires, with
 * the transcript endpoint now failing, and photographs the notice -- then clicks
 * Retry with the endpoint still failing and photographs the repeat-failure
 * wording, then clicks Retry with it healthy again and photographs the
 * recovered chat.
 *
 * Usage: node scripts/capture-heal-failed-notice.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/15029-heal-failed-notice'
const SLOT = 'chat-heal'

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Where did the review link go?',
  running: false,
  last_message: 'The review link is in the summary above.',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: '',
  folder_id: '',
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  redaction_gen: 'g-1',
  messages: [
    { role: 'user', ts: Date.now() / 1000 - 600, content: 'Can you link me the review for the export fix?', meta: { mid: 'm-1' } },
    { role: 'assistant', ts: Date.now() / 1000 - 30, content: 'The review is up; the link is in the summary above.', meta: { mid: 'm-2' } },
  ],
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1400, height: 900 }, deviceScaleFactor: 1 })
  let failing = false
  // While set, a slot read waits on it: the Retry is photographed in flight.
  let held = null
  const extra = async (path, route) => {
    if (path.startsWith('/api/chat/slots/')) {
      if (failing) { await json(route, { error: 'gateway unavailable' }, 503); return true }
      if (held) await held
      await json(route, detail)
      return true
    }
    return false
  }
  for (const theme of ['dark', 'light']) {
    failing = false
    const page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, { slots, theme, extra })
    await page.addInitScript(slot => { localStorage.setItem('mc-active-slot', slot) }, SLOT)
    await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2500)
    failing = true
    await page.evaluate(slot => {
      window.dispatchEvent(new CustomEvent('mc:redaction-hosts-changed', { detail: { slot, gen: 'g-2' } }))
    }, SLOT)
    await page.getByTestId('heal-error').waitFor({ timeout: 10000 })
    await page.waitForTimeout(400)
    await page.screenshot({ path: `${OUT}/heal-failed-${theme}.png` })
    // A Retry that fails again says so in words, not only by re-mounting.
    await page.getByTestId('heal-retry').click()
    await page.getByText(/Still couldn.t refresh this session/).waitFor({ timeout: 10000 })
    await page.waitForTimeout(400)
    await page.screenshot({ path: `${OUT}/heal-failed-again-${theme}.png` })
    failing = false
    let release
    held = new Promise(r => { release = r })
    await page.getByTestId('heal-retry').click()
    await page.getByText('Retrying…').waitFor({ timeout: 10000 })
    await page.waitForTimeout(300)
    await page.screenshot({ path: `${OUT}/heal-retrying-${theme}.png` })
    held = null
    release()
    await page.getByTestId('heal-error').waitFor({ state: 'detached', timeout: 10000 })
    await page.waitForTimeout(400)
    await page.screenshot({ path: `${OUT}/heal-retried-${theme}.png` })
    await page.close()
  }
  await browser.close()
  srv.close()
  console.log(`wrote ${OUT}`)
}

main().catch(err => { console.error(err); process.exit(1) })

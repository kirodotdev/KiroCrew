/**
 * Screenshot harness for a CANCELLED QUEUED MESSAGE restoring its attachment
 * after a reload, against the REAL built SPA (website/dist), gateway-free.
 *
 * The scene: a slot is mid-turn and holds one queued message whose text names
 * a bare-upload document with a SPACE in its path
 * (`[attached_file 1] /Users/me/Desktop/My Report.pdf`). The tab is opened
 * fresh, so there is no send-time stash for the entry; the only restore source
 * is what the server put on the slot-detail `queue[]` item. The harness clicks
 * the card's Cancel and photographs the composer.
 *
 *  - `after`: the item carries `meta.files`, so the composer gets the typed
 *    text back with the document re-staged as a chip.
 *  - `before`: the item carries only `content` (what an older gateway sends),
 *    so the marker line sits in the composer verbatim and no chip appears.
 *
 * ASSERTS as well as photographs: the `after` frame must show the chip and a
 * composer value equal to the typed text; the `before` frame must show the
 * marker text and no chip. Exits non-zero otherwise.
 *
 * Usage: node scripts/capture-queue-cancel-restore-meta.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/queue-cancel-restore-meta'
const SLOT = 'chat-queue-meta'
const SPACED = '/Users/me/Desktop/My Report.pdf'
const TYPED = 'summarize this for the team'
const WIRE = `${TYPED}\n[attached_file 1] ${SPACED}`

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Quarterly report review',
  running: true,
  last_message: 'Reading the draft now.',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: '',
  folder_id: '',
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

/** Slot detail with ONE queued entry; `withMeta` decides whether the server
 *  echoed the entry's attachment list on the item. */
const detail = (withMeta) => ({
  running: true,
  has_more: false,
  total: 2,
  project: '',
  queue: [{ id: 'q-1', content: WIRE, ...(withMeta ? { meta: { files: [SPACED] } } : {}) }],
  messages: [
    { role: 'user', ts: Date.now() / 1000 - 90, content: 'Start with the executive summary.' },
    { role: 'streaming', ts: Date.now() / 1000 - 5, content: 'Reading the draft now.' },
  ],
})

async function scene(context, base, label, withMeta) {
  const page = await context.newPage()
  logPageProblems(page)
  const extra = async (path, route) => {
    if (/^\/api\/chat\/slots\/[^/]+\/queue\/[^/]+$/.test(path)) {
      // The server's cancel reply; the client restores from the row it
      // already holds, so the reply is not what is under test.
      await json(route, { ok: true, content: WIRE })
      return true
    }
    if (path.startsWith('/api/chat/slots/')) { await json(route, detail(withMeta)); return true }
    return false
  }
  await stubDashboardApi(page, { slots, theme: 'light', extra })
  await page.addInitScript(slot => { localStorage.setItem('mc-active-slot', slot) }, SLOT)
  await page.goto(`${base}/chat/${SLOT}`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('textarea[data-composer-typo]')
  const cancel = page.getByRole('button', { name: 'Cancel queued message' }).first()
  await cancel.waitFor({ timeout: 15000 })
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/${label}-1-queued-card.png`, clip: await bandClip(page) })

  await cancel.click()
  await page.waitForTimeout(700)
  const dialogs = await page.locator('[role="dialog"]').count()
  if (dialogs) throw new Error(`${label}: an unexpected dialog is open over the frame`)
  await page.screenshot({ path: `${OUT}/${label}-2-composer-after-cancel.png`, clip: await bandClip(page) })

  const value = await page.locator('textarea[data-composer-typo]').inputValue()
  const chip = await page.getByTestId('preview-strip').locator(`[title="${SPACED}"], span:text-is("My Report.pdf")`).count()
  console.log(`  ${label}: composer=${JSON.stringify(value)} chips=${chip}`)
  await page.close()
  return { value, chip }
}

/** The lower band of the page: transcript tail, queue card, composer. */
async function bandClip(page) {
  const vp = page.viewportSize()
  return { x: 0, y: Math.max(0, vp.height - 420), width: vp.width, height: 420 }
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 2 })

  const after = await scene(context, base, 'after', true)
  const before = await scene(context, base, 'before', false)

  if (after.value !== TYPED) throw new Error(`after: composer should hold the typed text, saw ${JSON.stringify(after.value)}`)
  if (after.chip < 1) throw new Error('after: the spaced document should be re-staged as a chip')
  if (before.value !== WIRE) throw new Error(`before: composer should hold the marker verbatim, saw ${JSON.stringify(before.value)}`)
  if (before.chip !== 0) throw new Error('before: no chip is expected without the list')
  console.log('OK: after restores text + chip; before leaves the marker verbatim')

  await browser.close()
  srv.close()
}

main().catch((e) => { console.error(e); process.exit(1) })

/**
 * Screenshot probe: a long paste stays full text when the reader asks for it.
 *
 * Runs the REAL built SPA (website/dist) behind a tiny in-process static server
 * and answers every /api/** call from fixtures via Playwright route interception
 * (gateway-free - no kiro-cli, no live backend). The paste is delivered through
 * a real clipboard event on the composer, so the frame shows what the composer
 * does with it rather than a hand-assembled DOM.
 *
 * Frames written, per run:
 *   <prefix>-01-composer   the composer right after a 40-line paste
 *   <prefix>-02-setting    the Settings > Chat > Composer row (after only)
 *
 * The point is the delta, so run it twice - the setting is opt-in, so `after`
 * seeds it and `before` leaves the default:
 *   node scripts/capture-show-full-pastes.mjs ../temp-screenshots/show-full-pastes before
 *   node scripts/capture-show-full-pastes.mjs ../temp-screenshots/show-full-pastes after
 *
 * Usage: node scripts/capture-show-full-pastes.mjs [outDir] [prefix] [on|off]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/show-full-pastes'
const PREFIX = process.argv[3] || 'after'
const OPT_IN = (process.argv[4] || (PREFIX === 'after' ? 'on' : 'off')) === 'on'

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: 's1', title: 'Worker - paste a config and talk about it', messages: 4,
  running: false, agent: 'kirocrew', created: '2026-09-19T01:00:00Z',
  last_ts: '2026-09-19T21:14:00Z',
}]

// The payload is the shape that motivates the change: a config a reader pastes
// in order to edit one line of it before sending.
const PASTE = [
  '[gateway]',
  'host = "127.0.0.1"',
  'port = 5476',
  'workers = 4',
  '',
  '[gateway.tls]',
  'enabled = false',
  'cert = "/etc/kirocrew/server.pem"',
  'key = "/etc/kirocrew/server.key"',
  '',
  '[memory]',
  'store = "global-v1"',
  'consolidate_every_minutes = 30',
  'retention_days = 180',
  '',
  '[memory.embeddings]',
  'model = "text-embedding-3-small"',
  'batch_size = 64',
  'timeout_secs = 20',
  '',
  '[crons]',
  'timezone = "America/Los_Angeles"',
  'jitter = true',
].join('\n')

const config = OPT_IN ? { showFullPastes: true } : {}

async function openChat(context) {
  const page = await context.newPage()
  await stubDashboardApi(page, {
    slots,
    localStorageEntries: Object.keys(config).length
      ? { 'mc-chat-config': JSON.stringify(config) }
      : null,
  })
  logPageProblems(page)
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)
  return page
}

/**
 * Paste `text` into the composer through the REAL clipboard.
 *
 * A hand-dispatched ClipboardEvent is not enough here: when the composer
 * deliberately does NOT preventDefault (the opted-in path leaves a clean paste
 * to the browser), a synthetic event has no native insert behind it and the
 * frame comes back empty. A genuine Ctrl+V exercises the same branch the reader
 * does. Needs the clipboard permissions the context grants.
 */
async function pasteInto(page, locator, text) {
  await locator.click()
  await page.evaluate(value => navigator.clipboard.writeText(value), text)
  await page.keyboard.press('ControlOrMeta+v')
  await page.waitForTimeout(800)
}

let base

async function main() {
  const served = await serveDist()
  base = served.base
  const browser = await chromium.launch()
  const shared = {
    viewport: { width: 1400, height: 1000 },
    deviceScaleFactor: 2,
    // A real Ctrl+V needs a readable clipboard; see pasteInto.
    permissions: ['clipboard-read', 'clipboard-write'],
  }

  // 01 - the composer after the paste. A full-text paste is many lines tall, so
  // the clip is anchored to the composer's own box rather than a fixed height.
  const chatContext = await browser.newContext(shared)
  const chat = await openChat(chatContext)
  const composer = chat.getByRole('textbox', { name: /message input/i }).first()
  await pasteInto(chat, composer, PASTE)
  // A full-text paste leaves the caret at the end, so the input is scrolled to
  // its bottom. The frame is about what arrived, so show the top of it.
  await composer.evaluate(el => { el.scrollTop = 0 })
  await chat.waitForTimeout(200)
  const box = await composer.boundingBox()
  const cy = box ? Math.max(0, box.y - 28) : 600
  const ch = box ? Math.min(1000 - cy, box.height + 150) : 420
  await chat.screenshot({
    path: `${OUT}/${PREFIX}-01-composer.png`,
    clip: { x: 420, y: cy, width: 960, height: ch },
  })
  console.log('wrote', `${OUT}/${PREFIX}-01-composer.png`)
  await chatContext.close()

  // 02 - the Settings row itself. Only the `after` run needs it: the row is the
  // same control in both states and the toggle position is the whole delta.
  if (OPT_IN) {
    const settingsContext = await browser.newContext(shared)
    const settings = await settingsContext.newPage()
    await stubDashboardApi(settings, { slots, localStorageEntries: { 'mc-chat-config': JSON.stringify(config) } })
    logPageProblems(settings)
    await settings.goto(base + '/settings?tab=chat', { waitUntil: 'domcontentloaded' })
    await settings.waitForTimeout(2600)
    const row = settings.getByText('Show Pasted Text in Full').first()
    await row.scrollIntoViewIfNeeded()
    await settings.waitForTimeout(400)
    const rowBox = await row.boundingBox()
    const ry = rowBox ? Math.max(0, rowBox.y - 120) : 300
    // Out to the viewport edge: the toggle sits at the panel's right margin and
    // the switch position IS the evidence, so a narrower clip cuts the point.
    await settings.screenshot({
      path: `${OUT}/${PREFIX}-02-setting.png`,
      clip: { x: 340, y: ry, width: 1060, height: Math.min(1000 - ry, 360) },
    })
    console.log('wrote', `${OUT}/${PREFIX}-02-setting.png`)
    await settingsContext.close()
  }

  await browser.close()
  served.srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })

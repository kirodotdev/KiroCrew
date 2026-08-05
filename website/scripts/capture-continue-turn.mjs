/**
 * Screenshot harness for the Continue affordance on an interrupted turn.
 *
 * Runs the REAL built SPA (website/dist) against a static file server with every
 * /api/** call and the /api/ws websocket intercepted by Playwright and answered
 * from fixtures. No gateway, no kiro-cli, no live backend — only the network is
 * stubbed, so the error card, the composer button morph and the placeholder are
 * exercised exactly as they run in production.
 *
 * A turn can end WITHOUT the assistant handing the floor back, and the two shapes
 * look nothing alike in the transcript:
 *
 *   silent   the gateway restarted during an app update, so the turn's task died
 *            with the process and NOTHING was ever appended. The transcript just
 *            stops on the user's message and the UI said nothing at all.
 *   errored  the connection dropped mid-stream, so an error row landed. Its copy
 *            already read "please retry" — with nothing to click.
 *
 * Frames:
 *   01-silent-before    stopped on a user row, composer send button dead (main)
 *   02-silent-after     same transcript, send button is now Continue + placeholder
 *   03-errored-before   actionless red error div (main)
 *   04-errored-after    error card with a Continue action
 *   05-typed-reverts    typing restores Send, so the button never means two things
 *
 * The `-before` frames are produced by passing `--before`, which withholds the
 * fixtures' interrupted shape; run the same script on origin/main for a true
 * side-by-side.
 *
 * Usage: node scripts/capture-continue-turn.mjs <baseUrl> <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { json, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const BASE = process.argv[2] || 'http://127.0.0.1:6802'
const OUT = process.argv[3] || '../temp-screenshots/continue-turn'
const SLOT = 'chat-continue'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

const now = () => Date.now() / 1000

const slots = [{
  key: SLOT,
  title: 'Wire the diagnostics collector into /logs',
  running: false,
  last_message: 'Wire the diagnostics collector into the /logs page',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

/** Interrupted with NO output at all — the gateway-restart shape. */
const silent = {
  running: false,
  has_more: false,
  total: 1,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: now() - 900, content: 'Wire the diagnostics collector into the /logs page.' },
  ],
}

/** Interrupted AFTER streaming began — the dropped-connection shape. */
const errored = {
  running: false,
  has_more: false,
  total: 3,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: now() - 900, content: 'Wire the diagnostics collector into the /logs page.' },
    {
      role: 'assistant',
      ts: now() - 300,
      content: 'Reading `crash_dump_store.py` first — it already has the newest-dump helpers, so the endpoint can wrap those instead of re-walking the directory.',
    },
    { role: 'error', ts: now() - 280, content: '⟳ Connection lost — please retry.', cls: 'msg msg-err' },
  ],
}

/** A settled conversation: nothing to continue, so no affordance appears. */
const settled = {
  ...errored,
  total: 2,
  messages: errored.messages.slice(0, 2),
}

const scene = { detail: silent, theme: 'dark' }

async function main() {
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1500, height: 950 },
    // Dense 12–13px type; a 1x shot renders it soft on GitHub.
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()

  await page.routeWebSocket(/\/api\/ws/, () => {})

  await stubDashboardApi(page, {
    slots,
    theme: scene.theme,
    // Slot detail carries the whole scenario, and `theme` is read at boot, so
    // both are served from `scene` rather than captured at setup time.
    extra: async (path, route) => {
      if (path.startsWith('/api/chat/slots/')) { json(route, scene.detail); return true }
      if (path === '/api/theme/boot') { json(route, { mode: scene.theme, theme: '' }); return true }
      if (path === '/api/recent-projects') { json(route, { dirs: [PROJECT] }); return true }
      if (path === '/api/chat/nav/resolve-links') { json(route, { summaries: [] }); return true }
      return false
    },
  })

  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))
  page.on('console', msg => {
    if (msg.type() === 'error') console.log('CONSOLE:', msg.text().slice(0, 300))
  })

  async function load(detail, theme = 'dark') {
    scene.detail = detail
    scene.theme = theme
    await page.addInitScript(t => {
      // The composer persists drafts, so a wipe keeps one scenario's typed text
      // from bleeding into the next and misreading as that scenario's state.
      localStorage.clear()
      localStorage.setItem('mc-theme', t)
      localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-active-slot', 'chat-continue')
    }, theme)
    await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2500)
  }

  async function shot(name) {
    await page.screenshot({ path: `${OUT}/${name}.png` })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  /**
   * Tight crop across the transcript tail + composer — the whole story.
   *
   * The top edge is anchored to the LAST transcript row rather than a fixed
   * offset: a silent interruption leaves a single short message, and a fixed
   * offset would crop several hundred pixels of empty pane above it.
   */
  async function band(name) {
    const composer = page.locator('textarea').first()
    const box = await composer.boundingBox()
    if (!box) return shot(name)
    let top = Math.max(0, box.y - 330)
    const rows = page.locator('[data-testid="error-card"], .message-bubble')
    const count = await rows.count()
    if (count) {
      const first = await rows.nth(0).boundingBox()
      if (first) top = Math.max(0, Math.min(top, first.y - 12))
      // Never crop the tail row out: keep the deepest row in frame.
      const last = await rows.nth(count - 1).boundingBox()
      if (last) top = Math.min(top, Math.max(0, last.y - 12))
    }
    await page.screenshot({
      path: `${OUT}/${name}.png`,
      clip: { x: box.x - 30, y: top, width: Math.min(1180, box.width + 60), height: box.y + box.height + 60 - top },
    })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  // ---- silent interruption: composer is the only surface that can speak -----
  await load(silent)
  await band('02-silent-after')
  await shot('02-silent-after-full')

  // ---- errored interruption: card grows an action, composer offers it too ---
  await load(errored)
  await band('04-errored-after')
  await shot('04-errored-after-full')

  // ---- typing reverts the morph, so the control never carries two meanings --
  await load(errored)
  await page.locator('textarea').first().fill('actually, do the /logs page first')
  await page.waitForTimeout(400)
  await band('05-typed-reverts')

  // ---- settled conversation: no affordance anywhere -------------------------
  await load(settled)
  await band('06-settled-no-affordance')

  // ---- light theme parity on the busiest frame -----------------------------
  await load(errored, 'light')
  await band('07-errored-after-light')

  await browser.close()
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})

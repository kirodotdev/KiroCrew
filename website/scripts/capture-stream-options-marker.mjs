/**
 * Screenshot + video harness for the streamed `[OPTIONS:]` marker.
 *
 * Runs the REAL built SPA (website/dist) against a static file server with every
 * /api/** call answered from fixtures and the /api/ws websocket bound by
 * Playwright, so the turn is driven exactly the way the backend drives it: a
 * sequence of `chat_chunk` frames (the token stream) followed by the terminal
 * `chat_message`. No gateway, no auth, no real session.
 *
 * That fidelity is the point. The defect only appears while the marker line is
 * PARTIALLY arrived — `[OPTIONS: Open the PR | Sho` — which no static fixture can
 * express, because the marker in a finished message is already stripped by
 * OPTION_MARKER_RE. Streaming it chunk by chunk is the only way to photograph the
 * window, and the pause before the mid-marker shot lets useSmoothStream's ~0.4s
 * reveal lag drain so the shot shows what a reader actually sees.
 *
 * Run it twice — once against a dist built from the base ref, once from the fix —
 * and the pair is the before/after evidence.
 *
 * Usage: node scripts/capture-stream-options-marker.mjs <outDir> [label]
 */
import { chromium } from 'playwright'
import { mkdirSync, renameSync } from 'node:fs'
import { join } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/stream-options-marker'
const LABEL = process.argv[3] || 'after'
const SLOT = 'chat-stream-options'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Hide the half-streamed follow-up marker',
  running: true,
  last_message: 'Renamed the hook and reran the suite.',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: true,
  has_more: false,
  total: 1,
  queue: [],
  project: PROJECT,
  messages: [{
    role: 'user',
    ts: Date.now() / 1000 - 20,
    content: 'Rename the hook and rerun the suite.',
  }],
}

/** The turn, split the way a model streams it: prose, then the marker line. */
const PROSE = 'Renamed the hook to `useStreamReveal` across 6 call sites and reran the suite — 9,833 tests green, no snapshot churn.'
const MARKER = '\n\n[OPTIONS: Open the PR | Show me the diff | Skip it]'

/** Cut a string into ~n-char chunks, the granularity a real delta arrives at. */
const chunks = (s, n) => s.match(new RegExp(`[\\s\\S]{1,${n}}`, 'g')) ?? []

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1200, height: 760 },
    deviceScaleFactor: 2,
    recordVideo: { dir: OUT, size: { width: 1200, height: 760 } },
  })
  const page = await context.newPage()

  let wsServer = null
  await page.routeWebSocket(/\/api\/ws/, ws => { wsServer = ws })

  const fixedApi = makeFixedApi(PROJECT)
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/chat/slots') return json(route, slots)
    if (path.startsWith('/api/chat/slots/')) return json(route, detail)
    return handleBootRoute(route, path, { project: PROJECT, theme: 'dark', fixedApi })
  })

  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))
  page.on('console', msg => {
    if (msg.type() === 'error') console.log('CONSOLE:', msg.text().slice(0, 300))
  })

  await page.addInitScript(() => {
    localStorage.clear()
    localStorage.setItem('mc-theme', 'dark')
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-active-slot', 'chat-stream-options')
  })
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2500)
  if (!wsServer) throw new Error('websocket route never bound')

  let seq = 0
  /** One `chat_chunk` frame — the same shape useWebSocket's handler consumes. */
  const push = async (content, settle = 90) => {
    wsServer.send(JSON.stringify({ type: 'chat_chunk', data: { slot: SLOT, content, seq: seq++ } }))
    await page.waitForTimeout(settle)
  }
  const shot = async name => {
    await page.screenshot({ path: `${OUT}/${LABEL}-${name}.png` })
    console.log('wrote', `${OUT}/${LABEL}-${name}.png`)
  }

  // 1. Prose only — the baseline both builds agree on.
  for (const c of chunks(PROSE, 12)) await push(c)
  await page.waitForTimeout(1200) // let the smooth reveal catch up to the live edge
  await shot('01-prose')

  // 2. The marker, stopping one character short of its closing bracket. This is
  //    the window OPTION_MARKER_RE cannot match, so it is where the raw marker
  //    used to type itself out as prose.
  for (const c of chunks(MARKER.slice(0, -1), 8)) await push(c)
  await page.waitForTimeout(1400)
  await shot('02-marker-half-arrived')

  // 3. Closing bracket + the terminal message: the turn ends and the pills mount.
  await push(MARKER.slice(-1))
  wsServer.send(JSON.stringify({
    type: 'chat_message',
    data: { slot: SLOT, role: 'assistant', content: PROSE + MARKER, ts: new Date().toISOString() },
  }))
  wsServer.send(JSON.stringify({ type: 'slots', data: { slots: [{ ...slots[0], running: false }] } }))
  await page.waitForTimeout(2000)
  await shot('03-pills')

  // Ask Playwright for the recording's own path rather than scanning the out-dir:
  // it names the file by a random id, and a directory scan cannot tell this run's
  // recording from a previous label's finished `<label>-stream.webm` sitting beside
  // it — so the earlier run's evidence would get renamed under this run's label.
  // `video()` is bound to the page, so it is right no matter what else is in there.
  // The path resolves only after the context closes, which is why this runs last.
  const video = page.video()
  await context.close()
  await browser.close()
  srv.close()

  if (video) {
    const src = await video.path()
    const dest = join(OUT, `${LABEL}-stream.webm`)
    if (src !== dest) renameSync(src, dest)
    console.log('wrote', dest)
  }
}

main().catch(err => { console.error(err); process.exit(1) })

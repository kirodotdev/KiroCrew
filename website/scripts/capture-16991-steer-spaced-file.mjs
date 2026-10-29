/**
 * Screenshot harness for #16991: steering a running turn on the Chat page with
 * an attached file whose path contains spaces.
 *
 * Runs the REAL built SPA (website/dist, or the dist passed as argv[4]) with
 * every /api/** call answered from fixtures and the /api/ws websocket bound by
 * Playwright. The slot is running; the file is staged in the composer through
 * the per-slot file-draft store (sessionStorage `mc-chat-file-drafts`, the same
 * record a picked file lands in), a line of text is typed, and Enter steers.
 * The `/api/chat` POST is answered `{ ok, steered }` and the gateway's
 * `steer_push` echo is sent back carrying exactly the `meta.files` the POST
 * carried (the gateway echoes the client's attachment lists, nothing more).
 *
 * Shot 01: the optimistic steer bubble right after Enter.
 * Shot 02: the same bubble after the server echo reconciles it.
 * Before the fix the chip is cut at the first space; after it shows the path.
 *
 * Usage: node scripts/capture-16991-steer-spaced-file.mjs <outDir> [label] [distDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/16991-steer-spaced-file'
const LABEL = process.argv[3] || 'after'
const DIST = process.argv[4] ? resolve(process.argv[4]) : undefined
const SLOT = 'chat-steer-spaced-file'
const PROJECT = '/home/user/workspace/KiroCrew'
const FILE = '/tmp/My Project/notes v2.md'

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Summarize the release notes',
  running: true,
  last_message: 'Reading the changelog…',
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
  total: 2,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: Date.now() / 1000 - 40, content: 'Summarize the release notes for v2.' },
    { role: 'assistant', ts: Date.now() / 1000 - 30, content: 'Reading the changelog now.' },
  ],
}

async function main() {
  const { srv, base } = await serveDist(DIST)
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1200, height: 760 }, deviceScaleFactor: 2 })
  const page = await context.newPage()

  let wsServer = null
  await page.routeWebSocket(/\/api\/ws/, ws => { wsServer = ws })

  let posted = null
  const fixedApi = makeFixedApi(PROJECT)
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/chat' && route.request().method() === 'POST') {
      posted = route.request().postDataJSON()
      return json(route, { ok: true, steered: true })
    }
    if (path === '/api/chat/slots') return json(route, slots)
    if (path.startsWith('/api/chat/slots/')) return json(route, detail)
    return handleBootRoute(route, path, { project: PROJECT, theme: 'dark', fixedApi })
  })
  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))

  await page.addInitScript(([slot, file]) => {
    localStorage.clear()
    sessionStorage.clear()
    localStorage.setItem('mc-theme', 'dark')
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-active-slot', slot)
    sessionStorage.setItem('mc-chat-file-drafts', JSON.stringify({ [slot]: [file] }))
  }, [SLOT, FILE])
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2500)
  if (!wsServer) throw new Error('websocket route never bound')

  const shot = async name => {
    await page.screenshot({ path: `${OUT}/${LABEL}-${name}.png` })
    console.log('wrote', `${OUT}/${LABEL}-${name}.png`)
  }

  const box = page.locator('textarea').first()
  await box.click()
  await box.fill('Use these notes too')
  await page.waitForTimeout(300)
  await box.press('Enter')
  await page.waitForTimeout(400)
  if (!posted) throw new Error('steer POST never sent')
  console.log('POST meta:', JSON.stringify(posted.meta ?? null), 'steer:', JSON.stringify(posted.steer))
  await shot('01-optimistic')

  // The gateway's steer_push: content is the wire text, meta is the client's
  // own attachment lists (absent when the POST carried none).
  const files = posted.meta?.files
  wsServer.send(JSON.stringify({
    type: 'steer_push',
    data: {
      slot: SLOT,
      content: posted.message,
      ts: new Date().toISOString(),
      steerState: 'written',
      mid: 'm-16991-steer',
      ...(posted.meta?.sendId ? { sendId: posted.meta.sendId } : {}),
      ...(files?.length ? { meta: { files } } : {}),
    },
  }))
  await page.waitForTimeout(1200)
  await shot('02-echo')

  await context.close()
  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })

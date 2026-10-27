/**
 * Screenshot harness for #15926: option chips from an earlier segment while the
 * same turn keeps running.
 *
 * Runs the REAL built SPA (website/dist) with every /api/** call answered from
 * fixtures and the /api/ws websocket bound by Playwright, so the turn is driven
 * the way the backend drives it: `chat_chunk` frames for a segment that ends in
 * an `[OPTIONS:]` marker, a `chat_segment` that finalizes it, then a `tool_call`
 * plus its `chat_message` row while the slot stays running.
 *
 * Shot 01: the segment just finalized, nothing after it -- chips show on both
 * builds (a pick can still be steered into the turn).
 * Shot 02: a tool row follows -- the old build keeps the stale chips, the fix
 * hides them.
 *
 * Run it twice (base dist, fix dist); the pair is the before/after evidence.
 *
 * Usage: node scripts/capture-15926-chips-mid-turn.mjs <outDir> [label]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/15926-chips-mid-turn'
const LABEL = process.argv[3] || 'after'
const SLOT = 'chat-chips-mid-turn'
const PROJECT = '/home/user/workspace/KiroCrew'
const TOOL_CALL_ID = 'tc-15926-read'

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Check the review comments',
  running: true,
  last_message: 'Please log in again.',
  messages: 1,
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
  messages: [{ role: 'user', ts: Date.now() / 1000 - 20, content: 'Check the review comments on the PR.' }],
}

const SEGMENT = 'The GitHub token expired. Please log in again, then tell me to continue.\n\n[OPTIONS: Done, check now | Wait]'

const chunks = (s, n) => s.match(new RegExp(`[\\s\\S]{1,${n}}`, 'g')) ?? []

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1200, height: 760 }, deviceScaleFactor: 2 })
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

  await page.addInitScript(slot => {
    localStorage.clear()
    localStorage.setItem('mc-theme', 'dark')
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-active-slot', slot)
  }, SLOT)
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2500)
  if (!wsServer) throw new Error('websocket route never bound')

  const send = frame => wsServer.send(JSON.stringify(frame))
  const shot = async name => {
    await page.screenshot({ path: `${OUT}/${LABEL}-${name}.png` })
    console.log('wrote', `${OUT}/${LABEL}-${name}.png`)
  }

  // 1. The segment streams in and is finalized; the turn keeps running.
  let seq = 0
  for (const c of chunks(SEGMENT, 12)) {
    send({ type: 'chat_chunk', data: { slot: SLOT, content: c, seq: seq++ } })
    await page.waitForTimeout(60)
  }
  await page.waitForTimeout(800)
  send({ type: 'chat_segment', data: { slot: SLOT, content: SEGMENT } })
  await page.waitForTimeout(1800)
  await shot('01-segment-finalized')

  // 2. The agent moves on: a tool call, broadcast as the backend does.
  send({
    type: 'tool_call',
    data: {
      slot: SLOT, tool: 'read', kind: 'read', purpose: 'Read the PR review comments',
      input_preview: 'gh pr view --comments', tool_call_id: TOOL_CALL_ID, auto: true, is_shell: false,
    },
  })
  send({
    type: 'chat_message',
    data: { slot: SLOT, role: 'tool', content: '🔧 read', ts: new Date().toISOString(), meta: { tool_call_id: TOOL_CALL_ID } },
  })
  await page.waitForTimeout(1800)
  await shot('02-tool-row-follows')

  await context.close()
  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })

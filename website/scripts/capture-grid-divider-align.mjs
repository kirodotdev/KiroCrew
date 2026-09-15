/**
 * Screenshot harness for the split-view divider alignment fix: the 2px
 * divider bar between session panes previously ran the panes' full height
 * (`h-full`), overshooting past the rounded-lg (8px) corners of the adjacent
 * ChatPane cards. After the fix the bar is inset 8px at each end so it spans
 * exactly the straight segment of the pane borders.
 *
 * Renders the REAL SPA (built dist) with all /api/** answered from fixtures,
 * seeds a persisted 2-pane split, hovers the divider (accent state), and
 * captures a full view plus a zoomed crop of the divider's bottom end where
 * the misalignment is visible.
 *
 * Usage: node scripts/capture-grid-divider-align.mjs <outDir> <tag>
 */
import { chromium } from 'playwright'
import { prepareSplitChatPage } from './lib/prepare-split-chat-page.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { mkdirSync } from 'node:fs'

const OUT = process.argv[2] || '../temp-screenshots/grid-divider-align'
const TAG = process.argv[3] || 'shot'

mkdirSync(OUT, { recursive: true })

const now = Date.now() / 1000

const slots = [
  { key: 'pane-a', title: 'Design notes', running: false, last_message: 'Working through phase one…', messages: 2, agent: 'kirocrew', memory_mode: 'persistent', modified: Math.floor(now) },
  { key: 'pane-b', title: 'Release checklist', running: false, last_message: 'Summarized the layout options.', messages: 2, agent: 'kirocrew', memory_mode: 'persistent', modified: Math.floor(now) - 60 },
]

const detailA = {
  running: false, has_more: false, total: 2, queue: [],
  messages: [
    { role: 'user', ts: now - 300, content: 'Compare the two layout options.', cls: 'msg msg-user' },
    { role: 'assistant', ts: now - 240, content: 'Option A keeps the sidebar fixed; option B collapses it under 900px.', cls: 'msg msg-assistant' },
  ],
}
const detailB = {
  running: false, has_more: false, total: 2, queue: [],
  messages: [
    { role: 'user', ts: now - 120, content: 'Run the deployment checklist for the new release.', cls: 'msg msg-user' },
    { role: 'assistant', ts: now - 60, content: 'Starting phase one: build verification.', cls: 'msg msg-assistant' },
  ],
}

const splitLayouts = {
  'pane-a': {
    type: 'split', id: 'seed-split', dir: 'col',
    children: [
      { type: 'leaf', id: 'seed-a', kind: 'session', slot: 'pane-a' },
      { type: 'leaf', id: 'seed-b', kind: 'session', slot: 'pane-b' },
    ],
    sizes: [0.5, 0.5],
  },
}

const json = (route, body) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })

const FIXTURES = {
  '/api/chat/slots': slots,
  '/api/kiro-prerequisite': {
    platform: 'linux', installed: true, authenticated: true, ready: true,
    initial_setup_complete: true, can_auto_install: false, can_login: false,
    repair_required: false, docs_url: '', setup_allowed: false,
    operation: { kind: '', status: 'idle', message: '', detail: '', url: '', error: '' },
  },
  '/api/dashboard/config': { session_grid: true },
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 })
  const page = await prepareSplitChatPage(ctx, { base, fixtures: FIXTURES, detailA, detailB, splitLayouts, json })

  const divider = page.locator('[data-divider-index="0"]')
  await divider.waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForTimeout(800)

  // Hover the divider midpoint so the bar shows its accent (drag) color.
  const box = await divider.boundingBox()
  console.log('divider box:', JSON.stringify(box))
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
  await page.waitForTimeout(400)

  await page.screenshot({ path: `${OUT}/${TAG}-full.png` })
  // Zoomed crop of the divider's BOTTOM end + the two pane corners beside it.
  const cx = box.x + box.width / 2
  const bottomY = box.y + box.height
  await page.screenshot({ path: `${OUT}/${TAG}-bottom-zoom.png`, clip: { x: cx - 60, y: bottomY - 80, width: 120, height: 90 } })
  // Report the rendered bar geometry so before/after is verifiable in text too.
  const barBox = await divider.locator('div').first().boundingBox()
  console.log('bar box:', JSON.stringify(barBox), '| divider bottom:', bottomY, '| bar bottom:', barBox.y + barBox.height)

  await browser.close()
  srv.close()
}

main().catch(e => { console.error(e); process.exit(1) })

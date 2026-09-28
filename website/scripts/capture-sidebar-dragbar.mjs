/**
 * Screenshot harness for the sidebar (session list) resize bar alignment fix:
 * the 2px accent bar on the sidebar's right edge previously ran the card's
 * full height (`h-full`), overshooting past the rounded-xl (12px) corners of
 * the sidebar card. After the fix the bar is inset 12px at each end so its
 * hover/drag accent state spans exactly the straight segment of the border.
 *
 * Renders the REAL SPA (built dist) with all /api/** answered from fixtures,
 * hovers the sidebar resize handle, and captures the sidebar's top-right
 * corner zoomed plus a full view.
 *
 * Usage: node scripts/capture-sidebar-dragbar.mjs <outDir> <tag>
 */
import { chromium } from 'playwright'
import { prepareSplitChatPage } from './lib/prepare-split-chat-page.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { mkdirSync } from 'node:fs'

const OUT = process.argv[2] || '../temp-screenshots/sidebar-dragbar'
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

const json = (route, body) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })

const FIXTURES = {
  '/api/chat/slots': slots,
  '/api/kiro-prerequisite': {
    platform: 'linux', installed: true, authenticated: true, ready: true,
    initial_setup_complete: true, can_auto_install: false, can_login: false,
    repair_required: false, docs_url: '', setup_allowed: false,
    operation: { kind: '', status: 'idle', message: '', detail: '', url: '', error: '' },
  },
  '/api/dashboard/config': {},
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 })
  // No split layout — plain single chat page with the session-list sidebar.
  const page = await prepareSplitChatPage(ctx, { base, fixtures: FIXTURES, detailA, detailB: detailA, splitLayouts: {}, json })

  const handle = page.locator('.sidebar-resize-handle')
  await handle.waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForTimeout(800)

  // Hover mid-height so the bar shows its accent color.
  const box = await handle.boundingBox()
  console.log('handle box:', JSON.stringify(box))
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
  await page.waitForTimeout(400)

  await page.screenshot({ path: `${OUT}/${TAG}-full.png` })
  // Zoomed crop of the sidebar card's TOP-RIGHT corner + the bar's top end.
  const cx = box.x + box.width / 2
  await page.screenshot({ path: `${OUT}/${TAG}-top-zoom.png`, clip: { x: cx - 90, y: box.y - 10, width: 130, height: 100 } })
  // And the BOTTOM end.
  await page.screenshot({ path: `${OUT}/${TAG}-bottom-zoom.png`, clip: { x: cx - 90, y: box.y + box.height - 90, width: 130, height: 100 } })
  const barBox = await handle.locator('div').first().boundingBox()
  console.log('bar box:', JSON.stringify(barBox), '| handle top/bottom:', box.y, box.y + box.height, '| bar top/bottom:', barBox.y, barBox.y + barBox.height)

  await browser.close()
  srv.close()
}

main().catch(e => { console.error(e); process.exit(1) })

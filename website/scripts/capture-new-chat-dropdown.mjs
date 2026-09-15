/**
 * Screenshot harness for the "New chat" entry in the sidebar's split
 * create-button caret menu.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server and answers every /api/** call from fixtures via Playwright route
 * interception (gateway-free — no kiro-cli, no live backend). Seeds two
 * folders so the "New chat in folder" submenu row renders (it is gated on
 * `folders.length > 0`), then clicks the caret (aria-label "More create
 * options") and shoots the menu OPEN.
 *
 * Two frames per run: a full-window shot for context and a tight crop around
 * the open menu so the 13px item labels are legible on GitHub.
 *
 * Usage: node scripts/capture-new-chat-dropdown.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/new-chat-dropdown'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

const folders = [
  { id: 'f1', name: 'Kiro', icon: '🚀', order: 0, collapsed: false },
  { id: 'f2', name: 'Design', icon: '🎨', order: 1, collapsed: true },
]

const slot = (key, title, folder_id, last_ts, mode = '') => ({
  key, title, messages: 6, running: false, agent: 'kirocrew', mode,
  created: '2026-08-01T01:00:00Z', last_ts, folder_id,
})

const slots = [
  slot('s1', 'New chat in caret menu', 'f1', '2026-08-04T20:00:00Z'),
  slot('s2', 'Per-app trust grants', 'f1', '2026-08-04T18:30:00Z'),
  slot('s3', 'Skill pending notification', 'f2', '2026-08-03T12:00:00Z'),
  slot('s4', 'Windows NSIS target', '', '2026-08-04T21:00:00Z', 'orchestrator'),
  slot('s5', 'GHCR anonymous pull', '', '2026-08-04T14:00:00Z'),
]

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1400, height: 900 },
    deviceScaleFactor: 2, // 12-13px menu type renders soft at 1x on GitHub
  })
  const page = await context.newPage()

  await stubDashboardApi(page, { folders, slots })
  logPageProblems(page)

  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)

  const caret = page.locator('button[aria-label="More create options"]')
  await caret.first().waitFor({ state: 'visible', timeout: 15000 })
  await caret.first().click()

  // Radix portals the content to <body>; wait for the menu, not the trigger.
  const menu = page.locator('[role="menu"]')
  await menu.first().waitFor({ state: 'visible', timeout: 10000 })
  await page.waitForTimeout(500) // let the open animation settle

  console.log('MENU ITEMS', JSON.stringify(
    (await menu.first().locator('[role="menuitem"]').allInnerTexts()).map(s => s.trim()),
  ))

  await page.screenshot({ path: `${OUT}/${PREFIX}-01-menu-open-full.png` })
  console.log('wrote', `${OUT}/${PREFIX}-01-menu-open-full.png`)

  // Tight crop: union of the create-button and the portaled menu, padded.
  const trigBox = await page.locator('[data-create-menu]').first().boundingBox()
  const menuBox = await menu.first().boundingBox()
  const pad = 18
  const x0 = Math.max(0, Math.min(trigBox.x, menuBox.x) - pad)
  const y0 = Math.max(0, trigBox.y - pad)
  const x1 = Math.min(1400, Math.max(trigBox.x + trigBox.width, menuBox.x + menuBox.width) + pad)
  const y1 = Math.min(900, menuBox.y + menuBox.height + pad)
  await page.screenshot({
    path: `${OUT}/${PREFIX}-02-menu-open-crop.png`,
    clip: { x: x0, y: y0, width: x1 - x0, height: y1 - y0 },
  })
  console.log('wrote', `${OUT}/${PREFIX}-02-menu-open-crop.png`)

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })

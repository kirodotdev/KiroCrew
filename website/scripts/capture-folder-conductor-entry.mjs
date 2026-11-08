/**
 * Screenshot probe: the folder conductor entry on a sidebar folder row.
 *
 * Runs the REAL built SPA (website/dist) behind a tiny in-process static server
 * and answers every /api/** call from fixtures via Playwright route interception
 * (gateway-free - no kiro-cli, no live backend). The fixture is one folder whose
 * lead chat opened the two other chats filed in it, so the folder resolves a
 * conductor and draws the entry.
 *
 * Frames written, per run:
 *   <prefix>-01-row    the folder row hovered on its chat button (cluster + label)
 *   <prefix>-02-menu   the folder's ... menu, conductor entry first
 *
 * Run it against this branch (after) and against origin/main (before):
 *   node scripts/capture-folder-conductor-entry.mjs ../temp-screenshots/folder-conductor-entry after
 *
 * Usage: node scripts/capture-folder-conductor-entry.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/folder-conductor-entry'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

const FOLDER = 'wave2'
const folders = [{ id: FOLDER, name: 'wave2', order: 0, collapsed: false }]
const LEAD = 'chat-lead'
const slots = [
  { key: LEAD, title: 'Wave 2 rollout', messages: 18, running: false, agent: 'kirocrew',
    created: '2026-10-28T01:00:00Z', last_ts: '2026-10-28T09:10:00Z', folder_id: FOLDER },
  { key: 'chat-w1', title: 'Fargate task definition', messages: 6, running: false, agent: 'kirocrew',
    created: '2026-10-28T02:00:00Z', last_ts: '2026-10-28T09:05:00Z', folder_id: FOLDER,
    parent: { slot: LEAD, key: LEAD } },
  { key: 'chat-w2', title: 'ALB health checks', messages: 4, running: false, agent: 'kirocrew',
    created: '2026-10-28T03:00:00Z', last_ts: '2026-10-28T09:00:00Z', folder_id: FOLDER,
    parent: { slot: LEAD, key: LEAD } },
]

async function main() {
  const served = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1400, height: 900 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  await stubDashboardApi(page, { folders, slots })
  logPageProblems(page)
  await page.goto(served.base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)

  const row = page.locator(`[data-folder-row="${FOLDER}"]`).first()
  const rowBox = await row.boundingBox()
  const x = rowBox ? Math.max(0, rowBox.x - 16) : 0
  const y = rowBox ? Math.max(0, rowBox.y - 12) : 100
  const clip = { x, y, width: rowBox ? rowBox.width + 32 : 380, height: 150 }

  // Hover the chat button itself so the in-page label shows beside the cluster.
  await row.hover()
  const chatButton = page.locator(`[data-testid="folder-conductor-chat-row-${FOLDER}"]`)
  if (await chatButton.count()) await chatButton.first().hover()
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/${PREFIX}-01-row.png`, clip })
  console.log('wrote', `${OUT}/${PREFIX}-01-row.png`)

  await row.hover()
  await page.locator(`[data-testid="folder-menu-${FOLDER}"]`).first().click()
  await page.waitForTimeout(500)
  // Widen the frame to take in the whole menu, which opens to the row's right.
  const menuBox = await page.locator('[role="menu"]').first().boundingBox()
  const menuClip = menuBox
    ? { x, y, width: Math.max(clip.width, menuBox.x + menuBox.width + 12 - x), height: Math.min(menuBox.y + 230 - y, 420) }
    : { ...clip, height: 330 }
  await page.screenshot({ path: `${OUT}/${PREFIX}-02-menu.png`, clip: menuClip })
  console.log('wrote', `${OUT}/${PREFIX}-02-menu.png`)

  await browser.close()
  served.srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })

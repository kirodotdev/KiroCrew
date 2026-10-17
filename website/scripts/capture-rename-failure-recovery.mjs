/**
 * Screenshot harness for the sidebar rename failure recovery fix (#10151):
 * a server-refused rename used to leave the optimistic title in the sidebar
 * forever, because the .catch recovered with an invalidateQueries on a plain
 * ['chat-slots'] key no query is registered on. The fix dispatches
 * fetchSlots(), so the row snaps back to the server title.
 *
 * Renders the REAL SPA (built dist) with all /api/** answered from fixtures,
 * drives the inline rename (double-click, type, Enter), answers the title
 * PATCH with a delayed 500, and captures three frames: the rename editor,
 * the optimistic title while the PATCH is in flight, and the row snapped
 * back to the server title after the recovery refetch.
 *
 * Usage: node scripts/capture-rename-failure-recovery.mjs <outDir> <tag>
 */
import { chromium } from 'playwright'
import { prepareSplitChatPage } from './lib/prepare-split-chat-page.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { mkdirSync } from 'node:fs'

const OUT = process.argv[2] || '../temp-screenshots/10151-rename-recovery'
const TAG = process.argv[3] || 'shot'

mkdirSync(OUT, { recursive: true })

const now = Date.now() / 1000

const SERVER_TITLE = 'Weekly sync notes'
const DRAFT_TITLE = 'Renamed but refused'

const slots = [
  { key: 'pane-a', title: SERVER_TITLE, running: false, last_message: 'Agenda drafted.', messages: 2, agent: 'kirocrew', memory_mode: 'persistent', modified: Math.floor(now) },
  { key: 'pane-b', title: 'Release checklist', running: false, last_message: 'Summarized the layout options.', messages: 2, agent: 'kirocrew', memory_mode: 'persistent', modified: Math.floor(now) - 60 },
]

const detailA = {
  running: false, has_more: false, total: 2, queue: [],
  messages: [
    { role: 'user', ts: now - 300, content: 'Draft the agenda for the weekly sync.', cls: 'msg msg-user' },
    { role: 'assistant', ts: now - 240, content: 'Agenda drafted: status round, blockers, next milestones.', cls: 'msg msg-assistant' },
  ],
}

const json = (route, body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

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
  // The title PATCH is refused after a delay, so the optimistic frame is
  // capturable before the recovery refetch lands.
  const pre = async (path, route) => {
    if (/^\/api\/chat\/slots\/[^/]+\/title$/.test(path) && route.request().method() === 'PATCH') {
      await new Promise(resolve => setTimeout(resolve, 1500))
      await json(route, { error: 'rename refused' }, 500)
      return true
    }
    return false
  }
  const page = await prepareSplitChatPage(ctx, { base, fixtures: FIXTURES, detailA, detailB: detailA, splitLayouts: {}, json, pre })

  const row = page.locator('[data-slot-key="pane-a"] .session-row')
  await row.waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForTimeout(800)

  // Open the inline rename editor and type the draft.
  await row.locator('[data-session-title]').dblclick()
  const editor = row.locator('textarea')
  await editor.waitFor({ state: 'visible', timeout: 5000 })
  await editor.fill(DRAFT_TITLE)
  await page.screenshot({ path: `${OUT}/${TAG}-1-editing.png` })

  // Enter commits: the optimistic title renders while the PATCH is in flight.
  await page.keyboard.press('Enter')
  await row.getByText(DRAFT_TITLE).waitFor({ state: 'visible', timeout: 5000 })
  await page.screenshot({ path: `${OUT}/${TAG}-2-optimistic.png` })

  // The PATCH 500 lands, the .catch dispatches fetchSlots(), and the row
  // snaps back to the server truth. This frame is the fix: before it, the
  // refused draft stayed on screen indefinitely.
  await row.getByText(SERVER_TITLE).waitFor({ state: 'visible', timeout: 10000 })
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/${TAG}-3-recovered.png` })
  console.log('recovered title visible:', await row.getByText(SERVER_TITLE).isVisible(), '| draft still visible:', await row.getByText(DRAFT_TITLE).isVisible().catch(() => false))

  await browser.close()
  srv.close()
}

main().catch(e => { console.error(e); process.exit(1) })

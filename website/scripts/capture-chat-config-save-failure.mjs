/**
 * Screenshot probe: the chat-setting save-failure banner (issue #15236, UX Review).
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server and answers every /api/** call from fixtures (gateway-free). To force
 * the failure the UX reviewer named, an init script makes `localStorage.setItem`
 * throw a QuotaExceededError for the `mc-chat-config` blob and its dirty marker,
 * so `safeSetItem` returns false and `saveChatConfig` rolls back. We then flip a
 * toggle BELOW the fold ("Show thinking inline") so the frame shows the fix the
 * review asked for: the toggle snaps back AND the ErrorNotice banner is scrolled
 * into view near the top with its next-step copy visible.
 *
 * Usage: node scripts/capture-chat-config-save-failure.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/chat-config-per-field-merge'
mkdirSync(OUT, { recursive: true })

const slots = [{
  key: 's1', title: 'Worker', messages: 2, running: false, agent: 'kirocrew',
  created: '2026-09-19T01:00:00Z', last_ts: '2026-09-19T21:14:00Z',
}]

// Make the chat-config write (and its dirty marker) fail the way near-full
// storage does, so `safeSetItem` returns false and the save rolls back. Scoped
// to the two keys the save touches so unrelated writes still work and the SPA
// boots normally.
const FAIL_WRITES = `
  (() => {
    const real = Storage.prototype.setItem;
    Storage.prototype.setItem = function (k, v) {
      if (k === 'mc-chat-config' || k === 'mc-chat-config-dirty') {
        throw new DOMException('quota', 'QuotaExceededError');
      }
      return real.call(this, k, v);
    };
  })();
`

async function main() {
  const served = await serveDist()
  const base = served.base
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1400, height: 1000 },
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()
  await page.addInitScript(FAIL_WRITES)
  // Fulfill the model/agent/resolved endpoints so mcQ / agentsQ / resolvedQ
  // succeed — otherwise this panel raises its own, UNRELATED "Failed to load
  // config" banner from a harness fixture gap, cluttering the evidence frame.
  const extra = async (path, route) => {
    if (path === '/api/agents/resolved-model') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) })
      return true
    }
    if (path === '/api/models') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ models: [] }) })
      return true
    }
    return false
  }
  await stubDashboardApi(page, { slots, extra })
  logPageProblems(page)
  await page.goto(base + '/settings?tab=chat', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)

  // Flip a toggle below the fold. "Show thinking inline" lives lower in the
  // Transcript group, so the snap-back alone would be off-screen from the
  // top-anchored banner — exactly the case the review flagged.
  const toggle = page.getByText('Show thinking inline', { exact: false }).first()
  await toggle.scrollIntoViewIfNeeded()
  await page.waitForTimeout(300)
  await toggle.click()
  // Let setChat run, the save fail, and the effect scroll the banner into view.
  await page.waitForTimeout(1200)

  // Clip 1: the save-failure banner with its next-step copy, scrolled to the
  // top of the panel by the fix. (A second "Failed to load config" banner below
  // it is an UNRELATED gateway-free-harness fixture limitation, not the fix —
  // so this clip is bounded to the save-failure banner alone.)
  const banner = page.getByText("Couldn't save this chat setting", { exact: false }).first()
  const bb = await banner.boundingBox()
  const by = bb ? Math.max(0, bb.y - 16) : 130
  const bh = bb ? Math.ceil(bb.height + 32) : 90
  await page.screenshot({
    path: `${OUT}/chat-config-save-failure-banner.png`,
    clip: { x: 480, y: by, width: 920, height: bh },
  })
  console.log('wrote', `${OUT}/chat-config-save-failure-banner.png`)

  // Clip 2: the "Show Thinking Inline" row, reverted to OFF after the failed
  // save — the un-persisted value was NOT left shown.
  const row = page.getByText('Show Thinking Inline', { exact: false }).first()
  await row.scrollIntoViewIfNeeded()
  await page.waitForTimeout(300)
  const rb = await row.boundingBox()
  const ry = rb ? Math.max(0, rb.y - 20) : 700
  await page.screenshot({
    path: `${OUT}/chat-config-save-failure-toggle-reverted.png`,
    clip: { x: 700, y: ry, width: 700, height: 80 },
  })
  console.log('wrote', `${OUT}/chat-config-save-failure-toggle-reverted.png`)

  // Clip 3: the FULL settings panel with the banner scrolled into view at the
  // top and the Transcript list below it — the "one full-panel screenshot" the
  // UX review asked for, so the banner and the controls are shown together.
  await page.evaluate(() => window.scrollTo({ top: 0 }))
  await page.waitForTimeout(300)
  await page.screenshot({ path: `${OUT}/chat-config-save-failure-full-panel.png`, fullPage: false })
  console.log('wrote', `${OUT}/chat-config-save-failure-full-panel.png`)

  await context.close()

  // ── Sidebar board/list toggle save-failure (UX evidence gap 1) ────────────
  // Open /chat, force the same save failure, flip the board-view toggle in the
  // sidebar header menu, and capture the resulting notice: it is UNTITLED (not
  // the lane-seed title) and has NO "Try again" button (which would seed lanes,
  // not retry the save).
  const sidebarCtx = await browser.newContext({ viewport: { width: 1500, height: 950 }, deviceScaleFactor: 2 })
  const sb = await sidebarCtx.newPage()
  await sb.addInitScript(FAIL_WRITES)
  await stubDashboardApi(sb, {
    slots,
    extra: async (path, route) => {
      if (path === '/api/chat/tags') { await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }); return true }
      if (path === '/api/chat/tag-columns') { await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }); return true }
      return false
    },
  })
  logPageProblems(sb)
  await sb.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await sb.waitForSelector('[data-slot-key]', { timeout: 10000 })
  await sb.waitForTimeout(600)
  // Open the sidebar header "More options" menu that holds the board-view item
  // and click "Switch to board view". There are several "More options" menus
  // (per-row + header); find the one whose menu actually contains the item.
  const moreBtns = sb.getByRole('button', { name: /more options/i })
  const n = await moreBtns.count()
  let opened = false
  for (let i = 0; i < n; i++) {
    try { await moreBtns.nth(i).click({ timeout: 2000 }) } catch { continue }
    await sb.waitForTimeout(250)
    if (await sb.getByText('Switch to board view', { exact: false }).count() > 0) { opened = true; break }
    await sb.keyboard.press('Escape'); await sb.waitForTimeout(150)
  }
  if (!opened) throw new Error('could not find the sidebar menu with "Switch to board view"')
  await sb.getByText('Switch to board view', { exact: false }).first().click()
  await sb.waitForTimeout(800)
  const sbBanner = sb.getByTestId('lane-seed-error')
  const sbb = await sbBanner.boundingBox()
  if (sbb) {
    await sb.screenshot({
      path: `${OUT}/sidebar-board-toggle-save-failure.png`,
      clip: { x: Math.max(0, sbb.x - 8), y: Math.max(0, sbb.y - 8), width: Math.min(1500 - Math.max(0, sbb.x - 8), sbb.width + 16), height: sbb.height + 60 },
    })
  } else {
    await sb.screenshot({ path: `${OUT}/sidebar-board-toggle-save-failure.png`, fullPage: false })
  }
  console.log('wrote', `${OUT}/sidebar-board-toggle-save-failure.png`)
  await sidebarCtx.close()

  await browser.close()
  served.srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })

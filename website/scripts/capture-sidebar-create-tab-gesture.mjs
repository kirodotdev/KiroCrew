/**
 * Capture + regression harness for #10575: the sidebar folder create entries'
 * "open as a background tab" gestures (list-view folder "+", empty-folder row,
 * board-view column folder "+").
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback server with
 * /api/** answered by the shared fixture stub. Photographs the flow AND asserts
 * what matters, exiting non-zero on failure:
 *   1. Ctrl-click on the list-view folder "+" creates the session and opens it
 *      as a BACKGROUND tab: the tab strip appears with both keys and the origin
 *      session keeps the active tab.
 *   2. A plain click creates and ACTIVATES (no background tab).
 *   3. Board view: Ctrl-click on the column folder "+" does the same.
 *
 * Usage: node scripts/capture-sidebar-create-tab-gesture.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/sidebar-create-tab-gesture'
const VIEW = { width: 1400, height: 900 }
const FID = 'falpha'
const ORIGIN = 'chat-origin'
const BLOCKED = '11111111-1111-1111-1111-111111111111'
const COL_A = 'col-aaaa'
// The tab modifier is platform-split (isOpenInTabModifierClick): Cmd on macOS,
// Ctrl elsewhere. Chromium reports the host OS, so derive from process.platform.
const MOD = process.platform === 'darwin' ? 'Meta' : 'Control'

mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)
const folders = [{ id: FID, name: 'Alpha', order: 0, collapsed: false }]
const slots = [{
  key: ORIGIN, title: 'Origin session', running: false, last_message: '',
  messages: 3, agent: 'kirocrew', memory_mode: 'persistent', project: '',
  modified: now, tags: [], source_links: [], source_links_total: 0,
}]
const tags = [{ id: BLOCKED, name: 'Blocked', color: '#e11', order: 0, status: true }]
const columns = [{ id: COL_A, name: 'Planned', tag_ids: [BLOCKED], mode: 'any', order: 0 }]

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch({ args: ['--no-sandbox'] })
  const results = []
  const record = (name, pass, note = '') => {
    results.push({ name, pass, note })
    console.log(`${pass ? 'PASS' : 'FAIL'}  ${name}${note ? ` -- ${note}` : ''}`)
  }

  let createSeq = 0
  const extra = async (path, route) => {
    const method = route.request().method()
    if (path === '/api/chat/slots' && method === 'POST') {
      createSeq += 1
      const key = `chat-new-${createSeq}`
      await json(route, {
        key, title: '', running: false, messages: 0,
        agent: 'kirocrew', folder_id: FID, modified: now, tags: [],
      })
      return true
    }
    if (path === '/api/chat/tags') return json(route, tags), true
    if (path === '/api/chat/tag-columns') return json(route, columns), true
    if (/^\/api\/chat\/slots\/[^/]+\/column$/.test(path)) return json(route, { ok: true }), true
    if (/^\/api\/chat\/slots\/[^/]+\/(folder|drop)$/.test(path)) return json(route, { ok: true }), true
    return false
  }

  async function boot(boardView) {
    const context = await browser.newContext({ viewport: VIEW, deviceScaleFactor: 2 })
    const page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, {
      folders, slots, extra,
      localStorageEntries: {
        'mc-chat-config': JSON.stringify({ tagColumnsEnabled: boardView, confirmCloseSession: false, defaultAutopilot: false }),
        // One 220px lane + padding: keep the whole strip in frame.
        ...(boardView ? { 'mc-sidebar-width': '480' } : {}),
      },
    })
    await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
    if (boardView) await page.waitForSelector('[data-testid="column-strip"]', { timeout: 12000 })
    await page.waitForTimeout(800)
    return { context, page }
  }

  const shot = (page, name) => page.screenshot({ path: `${OUT}/${name}.png` })

  // Origin tab must keep the active state after a background create.
  async function originStaysActive(page) {
    const originTab = page.locator(`[data-testid="session-tab-${ORIGIN}"]`)
    const v = (await originTab.getAttribute('aria-selected'))
      ?? (await originTab.getAttribute('data-active'))
      ?? ((await originTab.getAttribute('class'))?.includes('active') ? 'true' : null)
    return v === 'true' || v === 'active'
  }

  // ---- list view: folder "+" modifier-click opens a background tab ----
  {
    const { context, page } = await boot(false)
    const plus = page.locator(`[data-testid="folder-new-chat-${FID}"]`)
    await plus.waitFor({ timeout: 12000 })
    await shot(page, '01-list-before')

    await plus.click({ modifiers: [MOD] })
    const strip = page.locator('[data-testid="session-tab-strip"]')
    await strip.waitFor({ timeout: 12000 })
    await page.waitForTimeout(400)
    await shot(page, '02-list-after-ctrl-click-plus')

    const originTab = page.locator(`[data-testid="session-tab-${ORIGIN}"]`)
    const newTab = page.locator('[data-testid="session-tab-chat-new-1"]')
    record('list "+": tab strip shows origin + new tab', (await originTab.count()) === 1 && (await newTab.count()) === 1)
    record('list "+": origin stays the active tab', await originStaysActive(page))
    await context.close()
  }

  // ---- list view: plain click creates and ACTIVATES (no background tab) ----
  {
    createSeq = 0
    const { context, page } = await boot(false)
    const plus = page.locator(`[data-testid="folder-new-chat-${FID}"]`)
    await plus.waitFor({ timeout: 12000 })
    await plus.click()
    await page.waitForTimeout(800)
    // One create POST must have fired -- "no tab strip" alone would also pass
    // for a click that created nothing.
    record('list "+": plain click fires exactly one create', createSeq === 1, `createSeq=${createSeq}`)
    const strip = page.locator('[data-testid="session-tab-strip"]')
    record('list "+": plain click opens no background tab strip', (await strip.count()) === 0)
    await shot(page, '03-list-after-plain-click-plus')
    await context.close()
  }

  // ---- list view: empty-folder row modifier-click opens a background tab ----
  {
    createSeq = 0
    const { context, page } = await boot(false)
    const row = page.locator(`[data-testid="folder-empty-new-chat-${FID}"]`)
    await row.waitFor({ timeout: 12000 })
    await row.click({ modifiers: [MOD] })
    await page.locator('[data-testid="session-tab-strip"]').waitFor({ timeout: 12000 })
    await page.waitForTimeout(400)
    await shot(page, '06-list-empty-row-after-ctrl-click')
    record('list empty row: background tab + origin stays active',
      (await page.locator('[data-testid="session-tab-chat-new-1"]').count()) === 1 && await originStaysActive(page))
    await context.close()
  }

  // ---- board view: column folder "+" modifier-click opens a background tab ----
  {
    createSeq = 0
    const { context, page } = await boot(true)
    const plus = page.locator(`[data-testid="col-${COL_A}-folder-${FID}-new-chat"]`)
    await plus.waitFor({ timeout: 12000 })
    await shot(page, '04-board-before')

    await plus.click({ modifiers: [MOD] })
    const strip = page.locator('[data-testid="session-tab-strip"]')
    await strip.waitFor({ timeout: 12000 })
    await page.waitForTimeout(400)
    await shot(page, '05-board-after-ctrl-click-plus')
    record('board "+": tab strip appears with the new background tab',
      (await page.locator('[data-testid="session-tab-chat-new-1"]').count()) === 1)
    record('board "+": origin stays the active tab', await originStaysActive(page))
    await context.close()
  }

  // ---- board view: column empty-folder row modifier-click ----
  {
    createSeq = 0
    const { context, page } = await boot(true)
    const row = page.locator(`[data-testid="col-${COL_A}-folder-${FID}-empty-new-chat"]`)
    await row.waitFor({ state: 'attached', timeout: 12000 })
    await row.click({ modifiers: [MOD] })
    await page.locator('[data-testid="session-tab-strip"]').waitFor({ timeout: 12000 })
    await page.waitForTimeout(400)
    await shot(page, '07-board-empty-row-after-ctrl-click')
    record('board empty row: background tab + origin stays active',
      (await page.locator('[data-testid="session-tab-chat-new-1"]').count()) === 1 && await originStaysActive(page))
    await context.close()
  }

  await browser.close()
  srv.close()
  const failed = results.filter(r => !r.pass)
  if (failed.length) {
    console.error(`\n${failed.length} assertion(s) failed`)
    process.exit(1)
  }
  console.log(`\nAll ${results.length} assertions passed; screenshots in ${OUT}`)
}

main().catch(err => { console.error(err); process.exit(1) })

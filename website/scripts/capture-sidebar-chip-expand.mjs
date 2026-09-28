/**
 * Screenshot harness for "the session row's +N chip expands the full link list".
 *
 * A still cannot carry this change: the "+2" chip looked identical before, when
 * it was inert text. The evidence is the sequence — collapsed strip, then every
 * link revealed with a collapse control at the end, then collapsed again — plus
 * the assertion that the revealed chips are ones the slots payload never carried
 * (they can only have come from GET /api/chat/slots/{slot}/source-links).
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures (gateway-free — no
 * kiro-cli, no dashboard token, no provider CLI). Only the network and the
 * localStorage seed are stubbed; the client code under test is unmodified.
 *
 * Usage: node scripts/capture-sidebar-chip-expand.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/sidebar-chip-expand'
const ACTIVE = 'chat-a'
const BUSY = 'chat-b'
const REPO = 'https://github.com/kirodotdev/KiroCrew'
const ROW = 'Sweep the native selects'

mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)

/** What the slots payload carries: three changes + one issue, the per-kind budget. */
const budgeted = [
  { provider: 'github', number: 648, url: `${REPO}/pull/648`, state: 'open', ci: 'running', kind: 'change' },
  { provider: 'github', number: 641, url: `${REPO}/pull/641`, state: 'open', ci: 'passed', kind: 'change' },
  { provider: 'github', number: 620, url: `${REPO}/pull/620`, state: 'merged', kind: 'change' },
  { provider: 'github', number: 701, url: `${REPO}/issues/701`, kind: 'issue' },
]
/** What the unbudgeted read adds: the two links behind "+2". */
const hidden = [
  { provider: 'github', number: 612, url: `${REPO}/pull/612`, state: 'merged', kind: 'change' },
  { provider: 'github', number: 688, url: `${REPO}/issues/688`, kind: 'issue' },
]
const full = [budgeted[0], budgeted[1], budgeted[2], hidden[0], budgeted[3], hidden[1]]

const slots = [
  {
    key: ACTIVE, title: 'Draft the release notes', running: false, messages: 4,
    agent: 'kirocrew', modified: now, last_ts: '2026-08-09T00:10:00Z', folder_id: '',
    last_message: 'Grouped the entries by area.',
  },
  {
    key: BUSY, title: ROW, running: false, messages: 24,
    agent: 'kirocrew', modified: now - 1800, last_ts: '2026-08-09T00:00:00Z', folder_id: '',
    last_message: 'Rebased and pushed; 47 checks green.',
    source_links: budgeted,
    source_links_total: 6,
  },
]

const detail = { running: false, has_more: false, total: 0, queue: [], messages: [] }

/**
 * Routes this harness owns on top of the shared boot fixtures.
 *
 * The source-links branch has to come FIRST: the detail branch matches every
 * path under /api/chat/slots/ and would answer the expand with a transcript.
 *
 * Each branch returns an explicit `true` — the stub treats a falsy return as
 * "not handled", and `json()` resolves to undefined, so `return json(...)` alone
 * double-fulfils ("Route is already handled!").
 */
const extra = (path, route) => {
  if (path === `/api/chat/slots/${BUSY}/source-links`) return json(route, { links: full, total: 6 }), true
  if (path.startsWith('/api/chat/slots/')) return json(route, detail), true
  return false
}

const rowLocator = page => page.locator('.session-row', { hasText: ROW }).first()
const chipLabels = page => rowLocator(page).locator('a[title^="Open "]')
  .evaluateAll(els => els.map(e => e.textContent.trim()))

async function cropRow(page, path) {
  // Clear hover AND focus off the row first: it reveals an action popup (⋯ /
  // duplicate / close) on either, and that popup sits ON TOP of the chip strip.
  // After a click the cursor is still inside the row, and the expand deliberately
  // moves focus to the control that replaced it — both of which would put the
  // popup over the chips this shot exists to show. Focus continuity is asserted
  // in ChatSidebar.sourceChipExpand.test.tsx instead.
  await page.mouse.move(1500, 850)
  await page.evaluate(() => document.activeElement instanceof HTMLElement && document.activeElement.blur())
  await page.waitForTimeout(250)
  const box = await rowLocator(page).evaluate(el => {
    const r = el.getBoundingClientRect()
    return { x: r.x, y: r.y, width: r.width, height: r.height }
  })
  await page.screenshot({
    path,
    clip: {
      x: Math.max(0, box.x - 8),
      y: Math.max(0, box.y - 8),
      width: box.width + 16,
      height: box.height + 16,
    },
  })
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1600, height: 900 },
    // The story is a 10px chip, illegible in a 1x full-window shot.
    deviceScaleFactor: 2,
  })
  // The overflow control is a button, not a link — if it ever regressed into an
  // anchor it would open a tab, and the harness would still screenshot a valid
  // page. Collect popups so the assertion below can say so out loud.
  const popups = []
  let page = null

  /** A FRESH page per theme: stubDashboardApi installs one `**\/api\/**`
   *  handler and bakes the theme into /api/theme/boot. */
  async function load(theme) {
    if (page) await page.close()
    page = await context.newPage()
    logPageProblems(page)
    page.on('popup', p => popups.push(`${theme}: ${p.url()}`))
    await stubDashboardApi(page, { slots, theme, extra })
    await page.addInitScript(slot => {
      localStorage.setItem('mc-active-slot', slot)
      localStorage.setItem('mc-privacy-notice-v1', '1')
      localStorage.setItem('mc-sidebar-pinned', 'true')
    }, ACTIVE)
    await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2600)
  }

  try {
    for (const theme of ['dark', 'light']) {
      await load(theme)

      const overflow = rowLocator(page).getByTestId('session-source-overflow')
      await overflow.waitFor({ state: 'visible', timeout: 15000 })

      // 1. Collapsed: the budgeted four chips and an inert-looking "+2".
      const before = await chipLabels(page)
      if (before.join(',') !== '#648,#641,#620,#701') {
        throw new Error(`unexpected collapsed strip: ${JSON.stringify(before)}`)
      }
      if ((await overflow.textContent()).trim() !== '+2') {
        throw new Error(`overflow chip does not read +2: ${await overflow.textContent()}`)
      }
      await cropRow(page, `${OUT}/02-collapsed-crop-${theme}.png`)
      await page.screenshot({ path: `${OUT}/01-collapsed-${theme}.png` })

      // 2. One click reveals everything, including two links the slots payload
      //    never carried, and swaps "+2" for a collapse control.
      await overflow.click()
      await rowLocator(page).getByTestId('session-source-collapse')
        .waitFor({ state: 'visible', timeout: 15000 })
      const expanded = await chipLabels(page)
      if (expanded.join(',') !== '#648,#641,#620,#612,#701,#688') {
        throw new Error(`unexpected expanded strip: ${JSON.stringify(expanded)}`)
      }
      if (await rowLocator(page).getByTestId('session-source-overflow').count() !== 0) {
        throw new Error('the +N chip survived a full expand')
      }
      await cropRow(page, `${OUT}/04-expanded-crop-${theme}.png`)
      await page.screenshot({ path: `${OUT}/03-expanded-${theme}.png` })

      // 3. The collapse control returns the row to the budgeted strip.
      await rowLocator(page).getByTestId('session-source-collapse').click()
      await overflow.waitFor({ state: 'visible', timeout: 15000 })
      const after = await chipLabels(page)
      if (after.join(',') !== before.join(',')) {
        throw new Error(`collapse did not restore the strip: ${JSON.stringify(after)}`)
      }
      await cropRow(page, `${OUT}/05-collapsed-again-crop-${theme}.png`)

      console.log(`${theme}: +2 -> ${expanded.length} chips -> collapsed back to ${after.length}`)
    }

    if (popups.length) {
      throw new Error(`a chip control opened a browser tab: ${popups.join('; ')}`)
    }
    console.log(`\nOK — screenshots in ${OUT}`)
  } finally {
    await browser.close()
    srv.close()
  }
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})

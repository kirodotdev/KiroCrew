/**
 * Screenshot harness for "a session-list chip stops claiming a conflicted pull
 * request is ready".
 *
 * The evidence has to be a COMPARISON, not one chip: the change is a glyph
 * swap, so a lone shot of a warning triangle proves nothing about when it
 * appears. Each row below therefore puts the new glyph next to the chips whose
 * rendering must NOT move — a clean passing PR, a pending one, a merged one —
 * and the second row pins the precedence that a still image can otherwise only
 * assert: with a failed rollup AND a conflict live, the chip shows the failure.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures (gateway-free — no
 * kiro-cli, no dashboard token, no provider CLI). Only the network and the
 * localStorage seed are stubbed; the client code under test is unmodified.
 *
 * Usage: node scripts/capture-sidebar-chip-conflict.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/sidebar-chip-conflict'
const ACTIVE = 'chat-a'
const REPO = 'https://github.com/kirodotdev/KiroCrew'
const pr = n => `${REPO}/pull/${n}`

mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)

/**
 * Chip counts stay at three per row: that is the serialized budget a real slot
 * ships (`_SERIALIZED_SOURCE_LINKS_PER_SLOT`), so a row crowded with six chips
 * would be a shape production never renders.
 */
const slots = [
  {
    key: ACTIVE, title: 'Draft the release notes', running: false, messages: 4,
    agent: 'kirocrew', modified: now, last_ts: '2026-08-15T00:10:00Z', folder_id: '',
    last_message: 'Grouped the entries by area.',
  },
  {
    // The case this change is about: #843's checks all pass AND its branch
    // conflicts with main, where #844 differs only in being mergeable.
    key: 'chat-b', title: 'Merge-conflict glyph on session chips', running: false, messages: 9,
    agent: 'kirocrew', modified: now - 600, last_ts: '2026-08-15T00:00:00Z', folder_id: '',
    last_message: 'All checks pass, but the branch needs a rebase.',
    source_links: [
      { provider: 'github', number: 843, url: pr(843), state: 'open', ci: 'passed', kind: 'change', mergeable: 'conflicting', mergeStateStatus: 'dirty' },
      { provider: 'github', number: 844, url: pr(844), state: 'open', ci: 'passed', kind: 'change', mergeable: 'mergeable', mergeStateStatus: 'clean' },
      { provider: 'github', number: 845, url: pr(845), state: 'open', ci: 'running', kind: 'change', mergeable: 'mergeable', mergeStateStatus: 'clean' },
    ],
    source_links_total: 3,
  },
  {
    // Precedence row. #846 carries BOTH blockers and must show the failure;
    // #847 proves a conflict still outranks a rollup that has not finished;
    // #848 is terminal, where the providers stop answering the merge pair at
    // all and the lifecycle glyph is the only meaningful signal.
    key: 'chat-c', title: 'Sweep the native selects', running: false, messages: 24,
    agent: 'kirocrew', modified: now - 1800, last_ts: '2026-08-14T23:30:00Z', folder_id: '',
    last_message: 'Two of these need a rebase before they can land.',
    source_links: [
      { provider: 'github', number: 846, url: pr(846), state: 'open', ci: 'failed', kind: 'change', mergeable: 'conflicting', mergeStateStatus: 'dirty' },
      { provider: 'github', number: 847, url: pr(847), state: 'open', ci: 'running', kind: 'change', mergeable: 'conflicting', mergeStateStatus: 'dirty' },
      { provider: 'github', number: 848, url: pr(848), state: 'merged', ci: 'passed', kind: 'change', mergeable: 'conflicting', mergeStateStatus: 'dirty' },
    ],
    source_links_total: 3,
  },
]

const detail = { running: false, has_more: false, total: 0, queue: [], messages: [] }

/**
 * Routes this harness owns on top of the shared boot fixtures.
 *
 * Each branch returns an explicit `true`: the stub treats a falsy return as "not
 * handled" and fulfils the route itself, and `json()` resolves to undefined, so
 * `return json(...)` alone double-fulfils ("Route is already handled!").
 */
const extra = (path, route) => {
  if (path.startsWith('/api/chat/slots/')) return json(route, detail), true
  return false
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1600, height: 900 },
    // The whole story is a 10px glyph, illegible in a 1x full-window shot.
    deviceScaleFactor: 2,
  })

  let page = null
  /**
   * A FRESH page per theme. stubDashboardApi installs one `**\/api\/**` handler
   * and bakes the theme into /api/theme/boot, so calling it twice on one page
   * throws "Route is already handled!" and the theme could not change anyway.
   */
  async function load(theme) {
    if (page) await page.close()
    page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, { slots, theme, extra })
    // Registered after the shared stub, which clears localStorage in its own
    // init script.
    await page.addInitScript(slot => {
      localStorage.setItem('mc-active-slot', slot)
      localStorage.setItem('mc-privacy-notice-v1', '1')
      localStorage.setItem('mc-sidebar-pinned', 'true')
    }, ACTIVE)
    await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2600)
  }

  /** Crop to the two chip-bearing rows, which is where the whole change lives. */
  async function shotRows(theme) {
    const first = page.locator('a[title^="Open ' + pr(843) + '"]').first()
    const last = page.locator('a[title^="Open ' + pr(848) + '"]').first()
    await first.waitFor({ state: 'visible', timeout: 15000 })
    await last.waitFor({ state: 'visible', timeout: 15000 })
    const box = await first.evaluate((el, lastSel) => {
      const top = el.closest('.session-row').getBoundingClientRect()
      const bottom = document.querySelector(lastSel).closest('.session-row').getBoundingClientRect()
      return { x: top.x, y: top.y, width: top.width, height: bottom.bottom - top.y }
    }, 'a[title^="Open ' + pr(848) + '"]')
    await page.screenshot({
      path: `${OUT}/01-chip-rows-${theme}.png`,
      clip: {
        x: Math.max(0, box.x - 8), y: Math.max(0, box.y - 10),
        width: box.width + 16, height: box.height + 20,
      },
    })
    await page.screenshot({ path: `${OUT}/02-sidebar-${theme}.png` })
  }

  const missing = []
  for (const theme of ['dark', 'light']) {
    await load(theme)
    await shotRows(theme)

    // A still cannot prove WHICH glyph a chip carries, so assert the labels the
    // change is about. Without this the harness would happily ship a screenshot
    // of the old rendering.
    const label = async (n, expected) => {
      const chip = page.locator('a[title^="Open ' + pr(n) + '"]').first()
      const got = await chip.locator('[aria-label]').evaluateAll(els => els.map(e => e.getAttribute('aria-label')))
      if (!got.includes(expected)) missing.push(`${theme}: #${n} has [${got.join(', ')}], expected "${expected}"`)
    }
    await label(843, 'Merge conflicts')
    await label(844, 'Checks passed')
    await label(845, 'Checks running')
    await label(846, 'Checks failed')
    await label(847, 'Merge conflicts')
    await label(848, 'Merged')
  }

  await browser.close()
  srv.close()
  if (missing.length) {
    console.error('FAILED — chip glyphs are not what this change claims:\n' + missing.join('\n'))
    process.exit(1)
  }
  console.log(`Wrote chip-conflict screenshots to ${OUT}`)
}

main()

/**
 * Screenshot harness for sidebar source-link chip project qualification (#9782).
 *
 * Demonstrates the fix: when a session's source links span more than one project
 * and two projects share the same issue/PR number, the chips now display a
 * project-qualified label (e.g., `group-a/svc #1`) instead of the ambiguous
 * bare `#1` that both rendered before the fix.
 *
 * Scene A — multi-project: one session with `group-a/svc#1` and `group-b/svc#1`.
 *   Chips must read `group-a/svc #1` and `group-b/svc #1` (distinct, qualified).
 * Scene B — single project: one session with `group-a/svc#1` and `group-a/svc#2`.
 *   Chips must read bare `#1` and `#2` (no qualifier prefix, short label preserved).
 *
 * Both scenes are captured in light and dark themes.
 *
 * Usage: node scripts/capture-9782-sidebar-chip-qualifier.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/9782-sidebar-chip-qualifier'
const ACTIVE = 'chat-active'

mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)

/** Two different GitHub projects, both with issue #1. */
const URL_A1 = 'https://github.com/group-a/svc/issues/1'
const URL_B1 = 'https://github.com/group-b/svc/issues/1'
/** Same project, two different issues. */
const URL_A1_PR = 'https://github.com/group-a/svc/pull/1'
const URL_A2_PR = 'https://github.com/group-a/svc/pull/2'

const active = {
  key: ACTIVE, title: 'Draft the release notes', running: false, messages: 2,
  agent: 'kirocrew', modified: now, last_ts: new Date().toISOString(), folder_id: '',
  last_message: 'Noted.',
  source_links: [], source_links_total: 0,
}

/** Scene A: two projects with the same issue #1. */
const multiProjectSlots = [
  active,
  {
    key: 'chat-multi', title: 'Service split work', running: false, messages: 5,
    agent: 'kirocrew', modified: now - 300, last_ts: new Date(Date.now() - 300_000).toISOString(), folder_id: '',
    last_message: 'Both issues filed.',
    source_links: [
      { provider: 'github', number: 1, label: '#1', url: URL_A1, state: 'open', kind: 'issue' },
      { provider: 'github', number: 1, label: '#1', url: URL_B1, state: 'open', kind: 'issue' },
    ],
    source_links_total: 2,
  },
]

/** Scene B: one project, two PRs with distinct numbers. */
const singleProjectSlots = [
  active,
  {
    key: 'chat-single', title: 'Refactor the handlers', running: false, messages: 3,
    agent: 'kirocrew', modified: now - 600, last_ts: new Date(Date.now() - 600_000).toISOString(), folder_id: '',
    last_message: 'Both PRs open.',
    source_links: [
      { provider: 'github', number: 1, label: '#1', url: URL_A1_PR, state: 'open', ci: 'passed', kind: 'change' },
      { provider: 'github', number: 2, label: '#2', url: URL_A2_PR, state: 'open', ci: 'running', kind: 'change' },
    ],
    source_links_total: 2,
  },
]

const detail = { running: false, has_more: false, total: 0, queue: [], messages: [] }
const extra = (path, route) => {
  if (path.startsWith('/api/chat/slots/')) return json(route, detail), true
  return false
}

/** Session rows that carry source-link chips. */
const chipRows = page => page.locator('.session-row').filter({ has: page.locator('a[title*="github.com"]') })

async function cropRows(page, path) {
  await page.mouse.move(1500, 850)
  await page.evaluate(() => { if (document.activeElement instanceof HTMLElement) document.activeElement.blur() })
  await page.waitForTimeout(300)
  const boxes = await chipRows(page).evaluateAll(els => els.map(el => {
    const r = el.getBoundingClientRect()
    return { x: r.x, y: r.y, right: r.right, bottom: r.bottom }
  }))
  if (!boxes.length) throw new Error('No chip rows found — chips did not render')
  const pad = 8
  const x = Math.max(0, Math.floor(Math.min(...boxes.map(b => b.x)) - pad))
  const y = Math.max(0, Math.floor(Math.min(...boxes.map(b => b.y)) - pad))
  const right = Math.ceil(Math.max(...boxes.map(b => b.right)) + pad)
  const bottom = Math.ceil(Math.max(...boxes.map(b => b.bottom)) + pad)
  await page.screenshot({ path, clip: { x, y, width: right - x, height: bottom - y } })
}

/** Read the visible chip text content from the sidebar rows. */
async function chipTexts(page) {
  return chipRows(page).evaluateAll(els =>
    els.map(el => [...el.querySelectorAll('a[title*="github.com"]')].map(a => a.textContent?.trim() ?? ''))
  )
}

function assert(label, got, want) {
  if (JSON.stringify(got) !== JSON.stringify(want)) {
    throw new Error(`${label}:\n  got  ${JSON.stringify(got)}\n  want ${JSON.stringify(want)}`)
  }
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1600, height: 900 }, deviceScaleFactor: 2 })
  let page = null

  async function load(theme, slots) {
    if (page) await page.close()
    page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, { slots, theme, extra })
    await page.addInitScript(slot => {
      localStorage.setItem('mc-active-slot', slot)
      localStorage.setItem('mc-privacy-notice-v1', '1')
      localStorage.setItem('mc-sidebar-pinned', 'true')
      localStorage.setItem('mc-session-stale-collapse-ms', '0')
    }, ACTIVE)
    await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2500)
    // Wait for at least one chip to appear.
    await page.locator('a[title*="github.com"]').first().waitFor({ state: 'visible', timeout: 15_000 })
  }

  try {
    for (const theme of ['dark', 'light']) {
      // Scene A: multi-project — chips should be qualified.
      await load(theme, multiProjectSlots)
      const multiChips = await chipTexts(page)
      assert(`${theme} multi-project chips`, multiChips, [
        ['group-a/svc #1', 'group-b/svc #1'],
      ])
      await cropRows(page, join(OUT, `01-multi-project-${theme}.png`))
      console.log(`${theme} multi-project OK: ${JSON.stringify(multiChips)}`)

      // Scene B: single project — chips should keep bare labels.
      await load(theme, singleProjectSlots)
      const singleChips = await chipTexts(page)
      assert(`${theme} single-project chips`, singleChips, [
        ['#1', '#2'],
      ])
      await cropRows(page, join(OUT, `02-single-project-${theme}.png`))
      console.log(`${theme} single-project OK: ${JSON.stringify(singleChips)}`)
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

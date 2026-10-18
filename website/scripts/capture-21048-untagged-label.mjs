/**
 * #21048 — the board column-filter popover's "include untagged" checkbox is
 * mode-aware. On a column with NO tag filter it reads "Untagged only" (ticking
 * it narrows the column to untagged sessions, matching the "Untagged" header);
 * on a tag-filtered column it reads "Include untagged sessions" (additive).
 * Captures both popover states gateway-free from the built dist.
 *
 * Usage: node scripts/capture-21048-untagged-label.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/21048-untagged-label'
mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)
/** One ordinary, fully-shaped slot so the sidebar providers boot cleanly. */
const slot = {
  key: 'chat-active', title: 'A session', running: false, messages: 1,
  agent: 'kirocrew', modified: now, last_ts: new Date().toISOString(), folder_id: '',
  last_message: 'Hi.', source_links: [], source_links_total: 0, tags: [],
}
const EMPTY_COL = { id: 'c-empty', name: '', tag_ids: [], mode: 'any', order: 0, include_untagged: true }
const TAGGED_COL = { id: 'c-tagged', name: 'Planned', tag_ids: ['t-planned'], mode: 'any', order: 1, include_untagged: true }
const TAGS = [{ id: 't-planned', name: 'Planned', color: '#888888' }]

const detail = { running: false, has_more: false, total: 0, queue: [], messages: [] }
const extra = (path, route) => {
  if (path === '/api/chat/tag-columns') return json(route, [EMPTY_COL, TAGGED_COL]), true
  if (path === '/api/chat/tags') return json(route, TAGS), true
  if (path.startsWith('/api/chat/slots/')) return json(route, detail), true
  return false
}

async function shoot(page, colId, expectLabel, file) {
  // The edit (tag) button is opacity-0 until its column is hovered.
  await page.locator(`[data-testid="column-${colId}"]`).hover()
  await page.locator(`[data-testid="column-edit-${colId}"]`).click({ force: true })
  const label = page.getByText(expectLabel, { exact: true })
  await label.waitFor({ state: 'visible', timeout: 5000 })
  // Assert-then-shoot: the label text must really be in the DOM before the PNG.
  if (!(await label.isVisible())) throw new Error(`label "${expectLabel}" not visible for ${colId}`)
  await page.screenshot({ path: join(OUT, file) })
  console.log(`wrote ${file} (asserted "${expectLabel}")`)
  await page.keyboard.press('Escape')
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1600, height: 900 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, { slots: [slot], theme: 'dark', extra })
  await page.addInitScript(() => {
    localStorage.setItem('mc-active-slot', 'chat-active')
    localStorage.setItem('mc-privacy-notice-v1', '1')
    localStorage.setItem('mc-sidebar-pinned', 'true')
    localStorage.setItem('mc-session-stale-collapse-ms', '0')
    localStorage.setItem('mc-chat-config', JSON.stringify({ tagColumnsEnabled: true }))
  })
  try {
    await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2500)
    await page.locator(`[data-testid="column-${EMPTY_COL.id}"]`).waitFor({ state: 'visible', timeout: 15_000 })
    await shoot(page, EMPTY_COL.id, 'Untagged only', '01-empty-tag-untagged-only.png')
    await shoot(page, TAGGED_COL.id, 'Include untagged sessions', '02-tagged-include-untagged.png')
    console.log(`\nOK — screenshots in ${OUT}`)
  } finally {
    await browser.close()
    srv.close()
  }
}

main().catch(err => { console.error(err); process.exit(1) })

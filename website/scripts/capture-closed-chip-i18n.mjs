/**
 * Screenshot harness for "the closed source chip renders a translated label"
 * (issue #8220).
 *
 * The defect was the raw wire value: the chip printed `link.state` ("closed")
 * verbatim, untranslated in every locale and CSS-capitalized, while its merged
 * sibling four lines above already went through the catalog. The evidence has
 * to show the SAME chip in two locales, because a lone English shot of "Closed"
 * is indistinguishable from the raw value with `capitalize` applied — only the
 * non-English frame proves the string now comes from the catalog.
 *
 * Frame 1 is a closed chip next to its merged and open siblings in English;
 * frame 2 is the same three rows in Chinese, where the closed label reads
 * 已关闭 instead of the raw "closed".
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures: no gateway, no
 * dashboard token, no provider CLI.
 *
 * Usage: node scripts/capture-closed-chip-i18n.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/closed-chip-i18n'
const ACTIVE = 'chat-a'
const pr = n => `https://github.com/kirodotdev/KiroCrew/pull/${n}`

mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)

const SLOTS = [
  {
    key: ACTIVE, title: 'Retire the legacy importer', running: false, messages: 6,
    agent: 'kirocrew', modified: now, last_ts: '2026-09-04T00:10:00Z', folder_id: '',
    last_message: 'Superseded by the streaming path.',
    // The case under test: a terminal closed pull request.
    source_links: [
      { provider: 'github', number: 993, url: pr(993), state: 'closed', kind: 'change' },
    ],
    source_links_total: 1,
  },
  {
    key: 'chat-b', title: 'Ship the compaction fix', running: false, messages: 12,
    agent: 'kirocrew', modified: now - 600, last_ts: '2026-09-04T00:00:00Z', folder_id: '',
    last_message: 'Merged after the second review round.',
    // The sibling precedent the fix mirrors: merged renders a translated glyph.
    source_links: [
      { provider: 'github', number: 994, url: pr(994), state: 'merged', ci: 'passed', kind: 'change' },
    ],
    source_links_total: 1,
  },
  {
    key: 'chat-c', title: 'Wire the audit worker', running: false, messages: 3,
    agent: 'kirocrew', modified: now - 1800, last_ts: '2026-09-03T23:30:00Z', folder_id: '',
    last_message: 'Checks are green, awaiting review.',
    // Live control: an open chip keeps its CI glyph and shows no lifecycle text.
    source_links: [
      { provider: 'github', number: 995, url: pr(995), state: 'open', ci: 'passed', kind: 'change', mergeable: 'mergeable', mergeStateStatus: 'clean' },
    ],
    source_links_total: 1,
  },
]

const detail = { running: false, has_more: false, total: 0, queue: [], messages: [] }

const extra = async (path, route) => {
  if (path === '/api/chat/slots') return await json(route, SLOTS), true
  if (path.startsWith('/api/chat/slots/')) return await json(route, detail), true
  return false
}

/**
 * Element-screenshot the session card that carries the chip under test. The
 * card is the whole region the change lives in, so no hand-built clip
 * arithmetic is needed (and none is duplicated from sibling harnesses).
 */
async function shotRows(page, name) {
  const row = page.getByText('Retire the legacy importer', { exact: true }).first()
  await row.waitFor({ state: 'visible', timeout: 20000 })
  const card = row.locator('xpath=ancestor::*[contains(concat(" ", normalize-space(@class), " "), " session-row ")]')
  const out = join(OUT, name)
  await card.screenshot({ path: out })
  console.log('wrote', out)
}

async function captureLocale(browser, base, lang, expectLabel, name) {
  const context = await browser.newContext({
    viewport: { width: 1600, height: 900 },
    // The chip label is ~10px, illegible in a 1x window shot.
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    theme: 'dark',
    extra,
    localStorageEntries: {
      'mc-lang': lang,
      'mc-active-slot': ACTIVE,
      'mc-privacy-notice-v1': '1',
      'mc-sidebar-pinned': 'true',
    },
  })
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  // The assertion the frame exists for: the catalog string rendered, not the
  // raw wire value. Waiting on the label also replaces a fixed sleep.
  const label = page.getByText(expectLabel, { exact: true }).first()
  await label.waitFor({ state: 'visible', timeout: 20000 })
  // Brief settle so the timestamp/glyph queries beside the label finish too.
  await page.waitForTimeout(600)
  await shotRows(page, name)
  await context.close()
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  await captureLocale(browser, base, 'en', 'Closed', '01-closed-chip-en.png')
  await captureLocale(browser, base, 'zh-CN', '已关闭', '02-closed-chip-zh-CN.png')
  await browser.close()
  srv.close()
}

await main()

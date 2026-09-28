/**
 * Screenshot harness for the sidebar's channel mark after a disconnect.
 *
 * Runs the REAL built SPA behind the shared in-process static server with every
 * /api/** answered from fixtures (gateway-free). Four rows, two of them
 * disconnected, so the frame shows the mark's whole truth table:
 *
 *   Slack-born, connected          -> Slack mark
 *   Slack-born, DISCONNECTED       -> no mark      (the defect: it stayed before)
 *   Dashboard, connected to Slack  -> Slack mark
 *   Dashboard, DISCONNECTED        -> no mark
 *
 * Asserts as well as shoots: the two disconnected rows must carry no channel
 * mark. Pass `--expect-stale` to invert the Slack-born assertion and capture
 * the BEFORE frame from a base-branch dist (`--dist <dir>`).
 *
 * Usage: node capture-sidebar-connected-glyph.mjs <outDir> <prefix> --dist <dir> [--expect-stale]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const positional = process.argv.slice(2).filter((a, i, all) => !a.startsWith('--') && all[i - 1] !== '--dist')
const OUT = positional[0]
const PREFIX = positional[1] || 'after'
const distIdx = process.argv.indexOf('--dist')
const DIST = distIdx > -1 ? process.argv[distIdx + 1] : undefined
const EXPECT_STALE = process.argv.includes('--expect-stale')
if (!OUT || !DIST) throw new Error('usage: <outDir> <prefix> --dist <dir>')
mkdirSync(OUT, { recursive: true })

const link = (channel, direction, paused) => ({
  channel, label: 'Slack', target: 'DM · redacted', direction, live: true, paused,
})
const base = { messages: 3, running: false, agent: 'kirocrew', created: '2026-09-14T17:00:00Z', folder_id: '' }
const ago = (min) => new Date(Date.now() - min * 60_000).toISOString()

// Slack-born keys are `slack_<ts>`: the row must NOT read that prefix for a mark.
const SLOTS = [
  { key: 'slack_1789201630.608199', title: 'Quick Bug Board burndown', ...base, last_ts: ago(3),
    links: [link('slack', 'origin', false)], slack_linked: true },
  { key: 'slack_1789205912.388979', title: 'PR #9628 babysit', ...base, last_ts: ago(9),
    links: [link('slack', 'origin', true)], slack_linked: true },
  { key: 'chat-350-1789188549', title: 'Sidebar Slack glyph bug', ...base, last_ts: ago(14),
    links: [link('slack', 'out', false)], slack_linked: true },
  { key: 'chat-351-1789188560', title: 'Asana Target Release research', ...base, last_ts: ago(22),
    links: [link('slack', 'out', true)], slack_linked: true },
  { key: 'chat-352-1789188570', title: 'Release notes draft', ...base, last_ts: ago(40) },
]

async function main() {
  const { srv, base: origin } = await serveDist(DIST)
  const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
  const browser = await chromium.launch({ env: browserEnv })
  const context = await browser.newContext({ viewport: { width: 1280, height: 720 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  await stubDashboardApi(page, { folders: [], slots: SLOTS })
  logPageProblems(page)
  page.on('pageerror', e => console.log('PAGEERROR', e.message))

  await page.goto(`${origin}/chat?sid=chat-352-1789188570`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('[aria-label="Chat messages"]', { timeout: 20_000 })
  for (const s of SLOTS) await page.locator(`[data-session-row="${s.key}"]`).first().waitFor({ state: 'visible', timeout: 15_000 })
  await page.mouse.move(1000, 600)
  await page.waitForTimeout(400)

  // Per-row truth: which channel marks (brand <img>) the row wears.
  const marks = async (key) => page.locator(`[data-session-row="${key}"] img`).evaluateAll(
    imgs => imgs.map(i => i.getAttribute('src') || '').filter(s => /slack|discord/i.test(s)).length,
  )
  const report = {}
  for (const s of SLOTS) report[s.title] = await marks(s.key)
  console.log(JSON.stringify(report, null, 2))

  const slackBornDisconnected = report['PR #9628 babysit']
  const dashboardDisconnected = report['Asana Target Release research']
  if (EXPECT_STALE) {
    if (slackBornDisconnected === 0) throw new Error('expected the STALE frame: Slack-born disconnected row still marked')
  } else {
    if (slackBornDisconnected !== 0) throw new Error(`Slack-born disconnected row still carries ${slackBornDisconnected} mark(s)`)
  }
  if (dashboardDisconnected !== 0) throw new Error('dashboard disconnected row carries a mark')
  if (report['Quick Bug Board burndown'] !== 1 || report['Sidebar Slack glyph bug'] !== 1) throw new Error('a connected row lost its mark')
  if (report['Release notes draft'] !== 0) throw new Error('plain dashboard row carries a mark')

  // The sidebar column, tightly framed on the session list.
  const sidebar = page.locator('[data-session-row]').first().locator('xpath=ancestor::aside[1]')
  const target = (await sidebar.count()) ? sidebar : page.locator('[data-session-row]').first().locator('xpath=ancestor::*[contains(@class,"overflow")][1]')
  await target.screenshot({ path: `${OUT}/${PREFIX}-sidebar.png` })
  console.log(`wrote ${OUT}/${PREFIX}-sidebar.png`)

  await browser.close()
  srv.close()
}

main().catch(e => { console.error(e); process.exit(1) })

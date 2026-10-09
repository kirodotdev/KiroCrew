/**
 * Screenshot harness for #11081: the chat session menu's MCP panel names a
 * server that started but gave this session no tools -- the trace of a
 * tool-name clash, where kiro-cli keeps one server's tools and drops the
 * other's whole set without a word.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server, every /api/** call answered from fixtures via Playwright route
 * interception -- gateway-free, no kiro-cli.
 *
 * The slot's `mcp_report` is what the gateway publishes on this branch for the
 * live repro behind the issue: two servers (`slack-alpha`, `slack-beta`) that
 * publish identical tool names, both `running`, with `/tools` listing only
 * `slack-alpha`'s. `main` publishes the same report without `no_tools`, so its
 * panel shows two healthy "Started" rings and nothing else (scene 1).
 *
 * Scenes (dark, plus the after-scene in light):
 *   1-before   main's shape: both rings say Started, no warning.
 *   2-after    this branch: slack-beta carries a warning mark on its name and
 *              the warning line, with the next step, under its row.
 *   3-extra    a no-tools server the configured list does not draw (declared
 *              only in the agent spec) is named in a line under the list.
 *
 * Usage: node scripts/capture-mcp-no-tools-warning.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/mcp-no-tools-warning'
mkdirSync(OUT, { recursive: true })

const SERVERS = [
  { name: 'kirocrew-core', enabled: true },
  { name: 'slack-alpha', enabled: true },
  { name: 'slack-beta', enabled: true },
]
const REPORT_BEFORE = {
  configured: [],
  unresolved_refs: [],
  ready: ['kirocrew-core', 'slack-alpha', 'slack-beta'],
  failed: [],
  awaiting_auth: [],
  failures: {},
}
const REPORT_AFTER = { ...REPORT_BEFORE, no_tools: ['slack-beta'] }
const WARNING = 'Started, but this session got none of its tools.'
const EXTRA = 'Started, but this session got none of their tools'
// A server declared only by the agent spec is absent from the configured list
// the panel draws, which is the usual clash case: named in the line under it.
const REPORT_EXTRA = {
  ...REPORT_BEFORE,
  ready: [...REPORT_BEFORE.ready, 'slack-gamma'],
  no_tools: ['slack-gamma'],
}

const slotWith = report => [{
  key: 's1', title: 'Two Slack workspaces', messages: 2, running: false, agent: 'kirocrew',
  created: '2026-10-08T09:00:00Z', last_ts: '2026-10-08T09:05:00Z', mcp_report: report,
}]

async function scene(browser, base, theme, name, report, expectWarning, text = WARNING) {
  const context = await browser.newContext({ viewport: { width: 1100, height: 760 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  await stubDashboardApi(page, {
    theme,
    slots: slotWith(report),
    extra: async (path, route) => {
      if (path === '/api/mcp/active') { await json(route, SERVERS); return true }
      return false
    },
  })
  logPageProblems(page)
  await page.goto(base + '/chat?slot=s1', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)
  await page.getByRole('button', { name: 'Session options' }).first().click()
  const sub = page.getByRole('menuitem', { name: /MCP servers/i }).first()
  await sub.waitFor({ state: 'visible', timeout: 5000 })
  await sub.hover()
  await sub.press('ArrowRight').catch(() => {})
  await page.getByText('slack-beta').first().waitFor({ state: 'visible', timeout: 5000 })
  // The submenu caps its own height, so bring the last row (and its warning)
  // into view the way a reader scrolling the panel would.
  await page.getByText('slack-beta').first().scrollIntoViewIfNeeded()
  const tail = page.getByText(text, { exact: false })
  if (await tail.count()) await tail.first().scrollIntoViewIfNeeded()
  await page.waitForTimeout(400)
  const shown = await page.getByText(text, { exact: false }).count()
  if (expectWarning !== shown > 0) throw new Error(`${name}: warning shown=${shown}, expected ${expectWarning}`)
  const menu = page.locator('[role="menu"]').last()
  const box = await menu.boundingBox()
  const clip = box
    ? { x: Math.max(0, box.x - 260), y: Math.max(0, box.y - 20), width: Math.min(1100, box.width + 290), height: box.height + 40 }
    : undefined
  await page.screenshot({ path: `${OUT}/${name}-${theme}.png`, clip })
  console.log(`wrote ${OUT}/${name}-${theme}.png`)
  await context.close()
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()
try {
  await scene(browser, base, 'dark', '1-before', REPORT_BEFORE, false)
  await scene(browser, base, 'dark', '2-after', REPORT_AFTER, true)
  await scene(browser, base, 'light', '2-after', REPORT_AFTER, true)
  await scene(browser, base, 'dark', '3-extra', REPORT_EXTRA, true, EXTRA)
} finally {
  await browser.close()
  srv.close()
}

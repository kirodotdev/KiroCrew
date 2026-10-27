/**
 * Screenshot harness for PR #24701: the chat session menu's MCP panel gives an
 * "Awaiting authorization" server a Sign in action. On KAS such a server's
 * tools are hidden from the model, so this row is where a user who never named
 * the server can start its sign-in; the Authorize card then lands in the chat.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server, every /api/** call answered from fixtures via Playwright route
 * interception -- gateway-free, no kiro-cli.
 *
 * Scenes, dark and light:
 *   1-row       figma-mcp awaits authorization; its row carries Sign in.
 *   2-starting  after the click: the button is disabled and says so while the
 *               start request runs (held open by the stub).
 *
 * Usage: node scripts/capture-mcp-awaiting-sign-in.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/mcp-awaiting-sign-in'
mkdirSync(OUT, { recursive: true })

const SERVERS = [
  { name: 'kirocrew-core', enabled: true },
  { name: 'figma-mcp', enabled: true },
  { name: 'slack-mcp', enabled: true },
]
const REPORT = {
  configured: [],
  unresolved_refs: [],
  ready: ['kirocrew-core', 'slack-mcp'],
  failed: [],
  awaiting_auth: ['figma-mcp'],
  failures: {},
}
const slots = [{
  key: 's1', title: 'Design review', messages: 2, running: false, agent: 'kirocrew',
  created: '2026-10-28T09:00:00Z', last_ts: '2026-10-28T09:05:00Z', mcp_report: REPORT,
}]

async function scene(browser, base, theme, name, click) {
  const context = await browser.newContext({ viewport: { width: 1100, height: 760 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  let signInRequests = 0
  await stubDashboardApi(page, {
    theme,
    slots,
    extra: async (path, route) => {
      if (path === '/api/mcp/active') { await json(route, SERVERS); return true }
      if (path === '/api/chat/slots/s1/mcp/sign-in') {
        // Held open so the starting state stays on screen.
        signInRequests += 1
        return true
      }
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
  const button = page.getByRole('button', { name: 'Sign in to figma-mcp' })
  await button.waitFor({ state: 'visible', timeout: 5000 })
  await button.scrollIntoViewIfNeeded()
  if (click) {
    await button.click()
    await page.getByText('Starting sign-in…').waitFor({ state: 'visible', timeout: 5000 })
    if (signInRequests !== 1) throw new Error(`${name}: ${signInRequests} sign-in requests, expected 1`)
  }
  await page.waitForTimeout(400)
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
  for (const theme of ['light', 'dark']) {
    await scene(browser, base, theme, '1-row', false)
    await scene(browser, base, theme, '2-starting', true)
  }
} finally {
  await browser.close()
  srv.close()
}

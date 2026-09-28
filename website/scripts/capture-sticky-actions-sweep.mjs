/**
 * Screenshot harness for #4299: the trailing Actions columns in the MCP servers
 * table (Capabilities → Connections → MCP Servers) and the webhook tokens table
 * (/webhooks), captured at a phone viewport where both tables overflow their
 * scroller. Before the fix the last column sits past the scroll edge behind a
 * hidden scrollbar; after it, the cells pin `sticky right-0` on an opaque base
 * with a measured seam + fade cue.
 *
 * Runs the REAL built SPA (website/dist) with every /api/** call answered from
 * fixtures — gateway-free. Same technique as capture-armed-delete-touch.mjs.
 *
 * Usage: node scripts/capture-sticky-actions-sweep.mjs [outDir] [suffix]
 *   suffix names the build being photographed, e.g. `before` / `after`.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/sticky-actions'
const SUFFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)

const MCP_SERVERS = [
  {
    name: 'builder-mcp', command: 'npx -y @amzn/builder-mcp-server --stdio', status: 'connected',
    tools: ['ReadInternalWebsites', 'InternalCodeSearch', 'GetPipelineDetails'],
    source: 'kirocrew', enabled: true, kirocrewManaged: true,
    presence: { kirocrew: true, kiroGlobal: false },
  },
  {
    name: 'playwright', command: 'npx @playwright/mcp@latest', status: 'connected',
    tools: ['browser_navigate', 'browser_click', 'browser_snapshot'],
    source: 'kirocrew', enabled: true, kirocrewManaged: true,
    presence: { kirocrew: true, kiroGlobal: true },
  },
  {
    name: 'notion', command: '', url: 'https://mcp.notion.com/mcp', status: 'error',
    error: 'authorization required', tools: [],
    source: 'kirocrew', enabled: true, kirocrewManaged: true,
    presence: { kirocrew: true, kiroGlobal: false },
  },
]

const WEBHOOKS_VIEW = {
  enabled: true,
  switch_on: true,
  has_tokens: true,
  url: 'http://127.0.0.1:6776/api/hooks/agent',
  slots: { in_use: 0, max: 2 },
  limits: {
    session_key_prefix: 'hook:', message_max: 16000, timeout_default: 300,
    timeout_max: 900, max_concurrent: 2, signature_window_seconds: 300,
  },
  tokens: [
    { id: 'tok-1', label: 'review-bot', display_prefix: 'kc_whk_4f2b', last4: '9d1c', created_at: now - 86400 * 12, last_used_at: now - 3600, require_signature: true, legacy: false },
    { id: 'tok-2', label: 'ci-pipeline', display_prefix: 'kc_whk_a81e', last4: '02f7', created_at: now - 86400 * 3, last_used_at: null, require_signature: false, legacy: false },
    { id: 'tok-3', label: 'pager-bridge', display_prefix: 'kc_whk_77c0', last4: '5b44', created_at: now - 3600 * 5, last_used_at: now - 60, require_signature: true, legacy: false },
  ],
  contexts: [],
  runs: [],
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 390, height: 844 },
    deviceScaleFactor: 2,
    hasTouch: true,
  })
  const page = await context.newPage()
  logPageProblems(page)

  await stubDashboardApi(page, {
    extra: async (path, route) => {
      if (path === '/api/mcp' || path === '/api/mcp/probe') { await json(route, MCP_SERVERS); return true }
      if (path === '/api/webhooks') { await json(route, WEBHOOKS_VIEW); return true }
      return false
    },
  })

  // ── MCP servers table (Capabilities → Connections → MCP Servers) ──
  await page.goto(base + '/capabilities?tab=mcp', { waitUntil: 'domcontentloaded' })
  const mcpTab = page.locator('#main-content').getByRole('tab', { name: /MCP Servers/i })
  await mcpTab.waitFor({ timeout: 15000 })
  await mcpTab.click()
  await page.getByText('builder-mcp').first().waitFor({ timeout: 15000 })
  await page.waitForTimeout(400)
  // Show mid-scroll so the capture proves whether the Actions column stays
  // reachable while other columns scroll (the defect is invisible at rest).
  await page.locator('#connections-mcp-panel .overflow-x-auto').first().evaluate(el => { el.scrollLeft = 120 })
  await page.waitForTimeout(300)
  await page.screenshot({ path: `${OUT}/mcp-${SUFFIX}-390.png` })

  // ── Webhook tokens table ──
  await page.goto(base + '/webhooks', { waitUntil: 'domcontentloaded' })
  const tokenLabel = page.getByText('review-bot').first()
  await tokenLabel.waitFor({ timeout: 15000 })
  await page.waitForTimeout(400)
  // The tokens Section sits below the setup banners — bring the table to the top
  // of the pane so the capture shows it.
  const tokensTable = page.locator('table').filter({ hasText: 'review-bot' }).first()
  await tokensTable.evaluate(el => el.scrollIntoView({ block: 'start' }))
  const tokensScroller = tokensTable.locator('..')
  await tokensScroller.evaluate(el => { el.scrollLeft = 120 })
  await page.waitForTimeout(300)
  await page.screenshot({ path: `${OUT}/webhooks-${SUFFIX}-390.png` })

  await browser.close()
  srv.close()
  console.log(`wrote ${OUT}/mcp-${SUFFIX}-390.png and ${OUT}/webhooks-${SUFFIX}-390.png`)
}

main().catch(err => { console.error(err); process.exit(1) })

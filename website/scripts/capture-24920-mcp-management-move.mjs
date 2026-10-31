/**
 * Screenshot harness for "Move MCP Management from Developer to Customize → MCP"
 * (issue #24920).
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures: no gateway, no token.
 *
 * Frames (per theme):
 *   after-capabilities-mcp-servers-<theme>.png  Customize → MCP, MCP Servers
 *       sub-tab (/capabilities?tab=mcp&view=mcp-servers), scrolled to the new
 *       MCP Management section below the servers list — reached with no
 *       Developer Mode.
 *   after-developer-tabs-<theme>.png  the Developer page rail
 *       (/developer?tab=logs): no "MCP Management" tab any more.
 *
 * The "before" (MCP Management on the Developer page's mcp-pool tab) is captured
 * from a build of origin/main in a sibling worktree; see the PR body.
 *
 * Usage (in website/): node scripts/capture-24920-mcp-management-move.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'

import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = resolve(process.argv[2] || '../temp-screenshots/24920-mcp-management-move')
mkdirSync(OUT, { recursive: true })

const GATEWAY_STATUS = { enabled: true, stub: ['filesystem'], stub_count: 1, running: true, ping_ok: true, supported: true }
const GATEWAY_SERVERS = {
  servers: [
    { name: 'filesystem', stub: true, can_stub: true, in_allowlist: true, entry_poolable: false, agents: ['kirocrew'], transport: 'stdio', denylisted: false },
    { name: 'git', stub: false, can_stub: true, in_allowlist: false, entry_poolable: false, agents: ['kirocrew'], transport: 'stdio', denylisted: false },
  ],
}

let failures = 0
const check = (label, ok) => { console.log(`${ok ? 'PASS' : 'FAIL'} ${label}`); if (!ok) failures++ }

const mcpExtra = async (path, route) => {
  if (path.startsWith('/api/mcp-gateway/servers')) { await json(route, GATEWAY_SERVERS); return true }
  if (path.startsWith('/api/mcp-gateway/status')) { await json(route, GATEWAY_STATUS); return true }
  if (path === '/api/mcp' || path.startsWith('/api/mcp?')) { await json(route, []); return true }
  if (path.startsWith('/api/mcp/scopes')) { await json(route, { scopes: [] }); return true }
  if (path.startsWith('/api/connections/status')) { await json(route, { connections: [] }); return true }
  if (path.startsWith('/api/acp-backends')) { await json(route, { backends: [] }); return true }
  return false
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()
try {
  for (const theme of ['dark', 'light']) {
    // ── Customize → MCP · MCP Servers: list + MCP Management section ─────────
    {
      const context = await browser.newContext({ viewport: { width: 1280, height: 1000 }, deviceScaleFactor: 2 })
      const page = await context.newPage()
      const errors = []
      page.on('pageerror', e => errors.push(String(e)))
      logPageProblems(page)
      await stubDashboardApi(page, {
        theme,
        extra: mcpExtra,
        localStorageEntries: { 'mc-lang': 'en', 'mc-privacy-notice-v1': '1' },
      })
      await page.goto(base + '/capabilities?tab=mcp&view=mcp-servers#mcp-management', { waitUntil: 'domcontentloaded' })
      await page.getByRole('heading', { name: 'MCP Management' }).waitFor({ timeout: 30000 })
      // Unscrolled viewport capture: land at the top of
      // the MCP Servers sub-tab (no `#mcp-management` fragment), so the sub-tab
      // bar and the servers list ABOVE MCP Management are in frame and the
      // section's place relative to the list it acts on is visible.
      await page.goto(base + '/capabilities?tab=mcp&view=mcp-servers', { waitUntil: 'domcontentloaded' })
      await page.getByRole('heading', { name: 'MCP Management' }).waitFor({ timeout: 30000 })
      await page.evaluate(() => window.scrollTo(0, 0))
      await page.waitForTimeout(300)
      await page.screenshot({ path: resolve(OUT, `after-capabilities-mcp-servers-top-${theme}.png`), fullPage: false })
      // Then the scrolled full section, as the link's fragment lands it.
      await page.goto(base + '/capabilities?tab=mcp&view=mcp-servers#mcp-management', { waitUntil: 'domcontentloaded' })
      await page.getByRole('heading', { name: 'MCP Management' }).waitFor({ timeout: 30000 })
      await page.locator('#mcp-management').scrollIntoViewIfNeeded()
      await page.waitForTimeout(600)
      const heading = await page.getByRole('heading', { name: 'MCP Management' }).count()
      check(`[${theme}] MCP Management section renders in the MCP Servers sub-tab: ${heading > 0}`, heading > 0)
      check(`[${theme}] no page errors (capabilities)`, errors.length === 0)
      await page.screenshot({ path: resolve(OUT, `after-capabilities-mcp-servers-${theme}.png`), fullPage: true })
      await context.close()
    }

    // ── Settings → Developer: no MCP Management page; its old link redirects ─
    {
      const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 2 })
      const page = await context.newPage()
      const errors = []
      page.on('pageerror', e => errors.push(String(e)))
      logPageProblems(page)
      await stubDashboardApi(page, {
        theme,
        extra: mcpExtra,
        localStorageEntries: { 'mc-lang': 'en', 'mc-privacy-notice-v1': '1' },
      })
      await page.goto(base + '/settings/developer/logs', { waitUntil: 'domcontentloaded' })
      await page.getByText('Logs').first().waitFor({ timeout: 30000 })
      await page.waitForTimeout(400)
      const mcpTab = await page.getByRole('option', { name: 'MCP Management' }).count()
      check(`[${theme}] Settings → Developer rail has NO MCP Management page: ${mcpTab === 0}`, mcpTab === 0)
      await page.screenshot({ path: resolve(OUT, `after-developer-tabs-${theme}.png`) })
      await page.goto(base + '/settings/developer/mcp-pool', { waitUntil: 'domcontentloaded' })
      await page.waitForURL(/\/capabilities\?tab=mcp&view=mcp-servers#mcp-management$/, { timeout: 15000 })
      check(`[${theme}] /settings/developer/mcp-pool redirects to the MCP Servers sub-tab`, true)
      check(`[${theme}] no page errors (developer)`, errors.length === 0)
      await context.close()
    }
  }
} finally {
  await browser.close()
  srv.close()
}
if (failures) { console.error(`${failures} check(s) failed`); process.exit(1) }
console.log(`wrote ${OUT}`)

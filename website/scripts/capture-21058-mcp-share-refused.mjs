/**
 * Screenshot harness for #21058: a server set to share whose launch the gateway
 * refused is named on Capabilities > Connections > MCP Servers, with a link to
 * Developer > MCP Management, where it is approved.
 *
 * Serves the REAL built SPA (website/dist) and answers /api/** from the shared
 * stub map. The scene-specific stubs are `GET /api/mcp-gateway/status`
 * (sharing on, two servers set to share, both refused for the two reasons the
 * gateway records) and `GET /api/mcp` (the two servers in the list).
 *
 * Shoots the notice in dark and light, then follows the link and shoots the page
 * it lands on with Developer Mode OFF, to show the approval is reachable. A third
 * scene answers the status read with a 503 and shoots the error notice.
 *
 * Usage: node scripts/capture-21058-mcp-share-refused.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { chromiumExecutable } from './lib/chromium-executable.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/21058-mcp-share-refused'
const PREFIX = process.argv[3] || 'after'

const server = name => ({
  name, command: name, status: 'ok', source: 'agent', enabled: true, tools: [],
})

const STATUS = {
  enabled: true,
  stub: ['alpha-mcp', 'beta-mcp'],
  stub_count: 2,
  running: true,
  ping_ok: true,
  supported: true,
  share_refused: { 'alpha-mcp': 'added_outside_dashboard', 'beta-mcp': 'changed_needs_reapproval' },
  launch_refused: {
    'alpha-mcp': { reason: 'added_outside_dashboard', commands: [['alpha-mcp']], envs: [[]], complete: true, expected_launch: 'a:b' },
    'beta-mcp': { reason: 'changed_needs_reapproval', commands: [['beta-mcp']], envs: [[]], complete: true, expected_launch: 'c:d' },
  },
}

const MANAGED = ['alpha-mcp', 'beta-mcp'].map(name => ({
  name, stub: true, can_stub: true, in_allowlist: true, entry_poolable: false,
  agents: ['kirocrew'], transport: 'stdio', denylisted: false,
}))

mkdirSync(OUT, { recursive: true })

async function main() {
  if (!process.env.SKIP_BUILD) {
    console.log('building dist (SKIP_BUILD=1 to reuse)…')
    execFileSync('npm', ['run', 'build'], { stdio: 'inherit', shell: process.platform === 'win32' })
  }

  const { srv, base } = await serveDist()
  const browser = await chromium.launch({ executablePath: chromiumExecutable() })

  for (const [theme, scene] of [['dark', 'refused'], ['light', 'refused'], ['dark', 'failed']]) {
    const context = await browser.newContext({ viewport: { width: 1300, height: 800 }, deviceScaleFactor: 2 })
    const page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, {
      slots: [],
      // `return json(...), true` -- the comma marks the request handled.
      extra: async (path, route) => {
        if (path === '/api/theme/boot') return json(route, { mode: theme, theme: '' }), true
        if (path === '/api/mcp' || path === '/api/mcp/probe') return json(route, [server('alpha-mcp'), server('beta-mcp')]), true
        // The shell's harness gate reads `backends`; the catch-all `[]` has none.
        if (path === '/api/acp-backends') return json(route, { backends: [] }), true
        if (path === '/api/mcp-gateway/status') {
          return (scene === 'failed' ? json(route, { error: 'unavailable' }, 503) : json(route, STATUS)), true
        }
        if (path === '/api/mcp-gateway/servers') return json(route, { servers: MANAGED }), true
        if (path.startsWith('/api/chat/slots/')) {
          return json(route, { running: false, has_more: false, total: 0, queue: [], messages: [] }), true
        }
        return false
      },
    })
    await page.addInitScript(t => {
      localStorage.setItem('mc-theme-mode', t)
      localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-lang', 'en')
      // Developer Mode OFF: the Developer page is not in the side rail.
      localStorage.removeItem('mc-dev-mode')
    }, theme)

    await page.goto(`${base}/capabilities?tab=mcp&view=mcp-servers`, { waitUntil: 'domcontentloaded' })
    const notice = page.getByTestId(scene === 'failed' ? 'mcp-share-status-error' : 'mcp-share-refused')
    await notice.waitFor({ state: 'visible', timeout: 15000 })
    const out = `${OUT}/${PREFIX}-${scene === 'failed' ? 'status-failed' : 'notice'}-${theme}.png`
    await page.screenshot({ path: out })
    console.log('wrote', out)

    if (theme === 'dark' && scene === 'refused') {
      await page.getByRole('link', { name: 'Review in MCP Management' }).click()
      await page.waitForURL(/\/developer\?tab=mcp-pool/, { timeout: 15000 })
      await page.getByText('alpha-mcp').first().waitFor({ state: 'visible', timeout: 15000 })
      const landed = `${OUT}/${PREFIX}-mcp-management-devmode-off-${theme}.png`
      await page.screenshot({ path: landed })
      console.log('wrote', landed)
    }
    await context.close()
  }

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })

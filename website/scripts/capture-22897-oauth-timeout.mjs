/**
 * Screenshot harness for issue #22897: an ended MCP sign-in attempt retires its
 * Authorize card. Gateway-free: the built SPA behind the shared static server,
 * every /api/** call answered from fixtures.
 *
 * One transcript, shaped exactly as `_expire_ended_mcp_oauth_banners` and
 * `_emit_mcp_oauth_request` leave it: the first lennysdata attempt ended (no
 * oauth_url, expired + expired_reason=attempt_ended), then the next offer minted a
 * fresh, live banner. Shot in light and dark.
 *
 * Usage: node scripts/capture-22897-oauth-timeout.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/22897-oauth-timeout'
const SLOT = 'chat-1'
const PROJECT = '/home/user/workspace/KiroCrew'
mkdirSync(OUT, { recursive: true })
const iso = s => new Date(Date.now() - s * 1000).toISOString()

const slots = [{
  key: SLOT, title: 'Agent SDK workflows', running: false,
  last_message: 'Checking the page now.', messages: 4, agent: 'kirocrew',
  memory_mode: 'persistent', project: PROJECT,
  modified: Math.floor(Date.now() / 1000), source_links: [], source_links_total: 0,
}]

const detail = {
  running: false, has_more: false, total: 4, queue: [], project: PROJECT,
  messages: [
    { role: 'user', cls: '', ts: iso(300), content: 'Are workflows in the Agent SDK?' },
    {
      role: 'mcp_oauth', cls: 'msg msg-info', ts: iso(240),
      content: '\u231B lennysdata sign-in ended before it was completed.',
      meta: {
        server_name: 'lennysdata', child: 'c-1', expired: true,
        expired_reason: 'attempt_ended', mid: 'm-1',
      },
    },
    {
      role: 'mcp_oauth', cls: 'msg msg-info', ts: iso(60),
      content: '\u{1F510} lennysdata requires authentication.',
      meta: {
        server_name: 'lennysdata', child: 'c-1', mid: 'm-2',
        oauth_url: 'https://www.lennysdata.com/oauth/authorize?response_type=code&client_id=abc&redirect_uri=http%3A%2F%2Flocalhost%3A54857%2Foauth%2Fcallback',
      },
    },
    { role: 'assistant', cls: '', ts: iso(50), content: 'Checking the page now.' },
  ],
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  for (const scheme of ['light', 'dark']) {
    const context = await browser.newContext({
      viewport: { width: 1280, height: 820 }, deviceScaleFactor: 2, colorScheme: scheme,
    })
    const page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, {
      slots,
      theme: scheme,
      extra: async (path, route) => {
        if (path.startsWith('/api/chat/slots/')) return json(route, detail), true
        return false
      },
    })
    await page.addInitScript(s => localStorage.setItem('mc-active-slot-chat', s), SLOT)
    await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
    const main = page.locator('#main-content')
    await main.getByText('Checking the page now.').first().waitFor({ state: 'visible', timeout: 20000 })
    await main.getByText(/ended before it was completed/).first().waitFor({ state: 'visible', timeout: 20000 })
    const links = await main.getByRole('link', { name: /Authorize lennysdata/i }).count()
    if (links !== 1) throw new Error(`${scheme}: expected exactly one live Authorize link, saw ${links}`)
    await page.waitForTimeout(500)
    const path = `${OUT}/timed-out-then-fresh-${scheme}.png`
    await page.screenshot({ path })
    console.log('wrote', path)
    await context.close()
  }
  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })

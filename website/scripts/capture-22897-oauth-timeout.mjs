/**
 * Screenshot harness for issue #22897: a replaced or ended MCP sign-in card is
 * not drawn at all. Gateway-free: the built SPA behind the shared static server,
 * every /api/** call answered from fixtures.
 *
 * Two transcripts, shaped exactly as `_expire_ended_mcp_oauth_banners`,
 * `_supersede_open_mcp_oauth_banners` and `_emit_mcp_oauth_request` leave them:
 *
 *   - `replaced`: the first lennysdata attempt ended (expired, attempt_ended), a
 *     second card was replaced (superseded), then the next offer minted a live
 *     card. Only that live card may show.
 *   - `ended`: the only attempt ended and nothing was offered since. No card shows.
 *
 * The DOM is asserted before each frame, since a screenshot cannot prove absence.
 * Shot in light and dark.
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

const LIVE_URL =
  'https://www.lennysdata.com/oauth/authorize?response_type=code&client_id=abc&redirect_uri=http%3A%2F%2Flocalhost%3A54857%2Foauth%2Fcallback'

const ended = (ts, mid) => ({
  role: 'mcp_oauth', cls: 'msg msg-info', ts: iso(ts),
  content: '\u231B lennysdata sign-in ended before it was completed.',
  meta: { server_name: 'lennysdata', child: 'c-1', expired: true, expired_reason: 'attempt_ended', mid },
})
const replaced = (ts, mid) => ({
  role: 'mcp_oauth', cls: 'msg msg-info', ts: iso(ts),
  content: '\u21BB lennysdata sign-in is no longer active \u2014 a newer request replaced it.',
  meta: { server_name: 'lennysdata', child: 'c-1', superseded: true, mid },
})
const live = (ts, mid) => ({
  role: 'mcp_oauth', cls: 'msg msg-info', ts: iso(ts),
  content: '\u{1F510} lennysdata requires authentication.',
  meta: { server_name: 'lennysdata', child: 'c-1', mid, oauth_url: LIVE_URL },
})
const user = (ts, content) => ({ role: 'user', cls: '', ts: iso(ts), content })
const assistant = (ts, content) => ({ role: 'assistant', cls: '', ts: iso(ts), content })

const SCENES = {
  replaced: {
    links: 1,
    messages: [
      user(300, 'Are workflows in the Agent SDK?'),
      ended(240, 'm-1'),
      replaced(120, 'm-2'),
      live(60, 'm-3'),
      assistant(50, 'Checking the page now.'),
    ],
  },
  ended: {
    links: 0,
    messages: [
      user(300, 'Are workflows in the Agent SDK?'),
      ended(240, 'm-1'),
      assistant(50, 'Checking the page now.'),
    ],
  },
  // A late success on a hidden ended row (the backend flips it to completed), beside
  // another server's card whose hosting process died: that grey card still renders.
  'late-success': {
    links: 0,
    visible: [/lennysdata authenticated|authenticated\./i, /the session that started it ended/i],
    messages: [
      user(300, 'Are workflows in the Agent SDK?'),
      {
        role: 'mcp_oauth', cls: 'msg msg-info', ts: iso(240),
        content: '\u{1F513} lennysdata authenticated.',
        meta: { server_name: 'lennysdata', child: 'c-1', completed: true, mid: 'm-1' },
      },
      {
        role: 'mcp_oauth', cls: 'msg msg-info', ts: iso(120),
        content: '\u{1F510} figma requires authentication.',
        meta: { server_name: 'figma', child: 'c-0', expired: true, mid: 'm-2' },
      },
      assistant(50, 'Checking the page now.'),
    ],
  },
}

const slotRow = total => [{
  key: SLOT, title: 'Agent SDK workflows', running: false,
  last_message: 'Checking the page now.', messages: total, agent: 'kirocrew',
  memory_mode: 'persistent', project: PROJECT,
  modified: Math.floor(Date.now() / 1000), source_links: [], source_links_total: 0,
}]

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  for (const [scene, { links, messages, visible = [] }] of Object.entries(SCENES)) {
    const detail = { running: false, has_more: false, total: messages.length, queue: [], project: PROJECT, messages }
    for (const scheme of ['light', 'dark']) {
      const context = await browser.newContext({
        viewport: { width: 1280, height: 820 }, deviceScaleFactor: 2, colorScheme: scheme,
      })
      const page = await context.newPage()
      logPageProblems(page)
      await stubDashboardApi(page, {
        slots: slotRow(messages.length),
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
      await page.waitForTimeout(500)
      const seen = await main.getByRole('link', { name: /Authorize lennysdata/i }).count()
      if (seen !== links) throw new Error(`${scene}/${scheme}: expected ${links} live Authorize link(s), saw ${seen}`)
      for (const gone of [/ended before it was completed/, /newer request/]) {
        if (await main.getByText(gone).count()) throw new Error(`${scene}/${scheme}: a retired card rendered (${gone})`)
      }
      for (const shown of visible) {
        if (!(await main.getByText(shown).count())) throw new Error(`${scene}/${scheme}: expected ${shown} to render`)
      }
      const path = `${OUT}/${scene}-${scheme}.png`
      await page.screenshot({ path })
      console.log('wrote', path)
      await context.close()
    }
  }
  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })

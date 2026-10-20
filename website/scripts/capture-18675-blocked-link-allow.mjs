/**
 * Screenshot harness for issue #18675: after Allow on a blocked-link card, the
 * card must reflect it across the whole card, not just the one entry pressed.
 *
 * One reply links the same host twice (two paths, both over the long-query
 * threshold), so the card lists two entries. The harness opens the card and
 * shoots it twice per theme:
 *  - before: the card is titled for the host's BLOCKED links and offers ONE
 *    Allow for the host (no per-entry Allow button);
 *  - after:  Allow was pressed — the title now names the host as ALLOWED, the
 *    Allow button is gone, and one "Allowed · Undo" line stands for the host.
 *
 * Runs the REAL built SPA behind the shared loopback static server with every
 * /api/** call answered from fixtures (gateway-free). `DIST` selects the build.
 *
 * Usage: DIST=<dist dir> node scripts/capture-18675-blocked-link-allow.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/18675-blocked-link-allow'
const ACTIVE = 'chat-a'
const HOST = 'reviews.corp.example'
const TS = '2026-10-20T04:00:00Z'

mkdirSync(OUT, { recursive: true })
const now = Math.floor(Date.now() / 1000)

const blockedLinks = [1, 2].map(n => ({
  domain: HOST,
  rule: 'exfil_query_length',
  path: `/r${n}`,
  query_chars: 290,
  url: `https://${HOST}/r${n}?filter=${'x'.repeat(280)}`,
  url_withheld: null,
}))

const slots = [
  {
    key: ACTIVE, title: 'Pull the two review dashboards', running: false, messages: 2,
    agent: 'kirocrew', modified: now, last_ts: TS, folder_id: '',
    last_message: 'Both links were blocked.',
  },
]

const detail = {
  running: false, has_more: false, total: 2, queue: [],
  messages: [
    { role: 'user', content: 'Open the two review dashboards.', ts: now - 60 },
    {
      role: 'assistant',
      content: `Here they are: [REDACTED: suspicious URL to ${HOST}] and [REDACTED: suspicious URL to ${HOST}].`,
      ts: TS,
      meta: { blocked_links: blockedLinks },
    },
  ],
}

const extra = (path, route) => {
  if (path.startsWith('/api/chat/slots/')) return json(route, detail), true
  if (path === '/api/redaction/allowed-hosts' && route.request().method() === 'POST') {
    return json(route, { ok: true, workspace: 'default' }), true
  }
  if (path.startsWith('/api/redaction/allowed-hosts')) return json(route, { workspaces: {} }), true
  return false
}

async function shoot(page, name) {
  const card = page.getByTestId('blocked-link-card').first()
  await card.waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForTimeout(500)
  // Honesty: assert the on-screen card state before every shot.
  await card.screenshot({ path: `${OUT}/${name}.png` })
  return card.textContent()
}

async function run(theme) {
  const { srv, base } = await serveDist(process.env.DIST || undefined)
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 980, height: 1100 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, { slots, theme, extra })
  await page.addInitScript(slot => {
    localStorage.setItem('mc-active-slot', slot)
    localStorage.setItem('mc-privacy-notice-v1', '1')
    localStorage.setItem('mc-sidebar-pinned', 'true')
  }, ACTIVE)
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)

  // Open the card.
  await page.getByTestId('blocked-link-inspect').first().click()
  const beforeText = await shoot(page, `${theme}-before-blocked`)

  // The Allow confirmation (card-level), with its workspace-scope explanation.
  await page.getByTestId('blocked-link-allow').first().click()
  await page.getByTestId('blocked-link-allow-confirm').first().waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForTimeout(400)
  const confirmText = await shoot(page, `${theme}-confirm`)

  // Allow the host.
  await page.getByTestId('blocked-link-allow-confirmed').first().click()
  await page.getByTestId('blocked-link-undo').first().waitFor({ state: 'visible', timeout: 15000 })
  const afterText = await shoot(page, `${theme}-after-allowed`)

  const report = {
    theme,
    before_has_allow: /Allow for this host/i.test(beforeText),
    before_title_blocked: /Blocked links to this host/i.test(beforeText),
    confirm_explains_scope: /this workspace only/i.test(confirmText),
    after_has_undo: /Undo/i.test(afterText),
    after_title_allowed: /This host is allowed in this workspace/i.test(afterText),
    after_still_has_allow_button: await page.getByTestId('blocked-link-allow').count(),
  }
  await browser.close()
  srv.close()
  return report
}

async function main() {
  const out = []
  for (const theme of ['light', 'dark']) out.push(await run(theme))
  console.log(JSON.stringify(out, null, 1))
}

main().catch(err => { console.error(err); process.exit(1) })

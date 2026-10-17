/**
 * Screenshot harness for the Blocked link chip's label.
 *
 * Runs the REAL built SPA (website/dist) behind the shared static server and
 * answers every /api/** call from fixtures. One assistant reply carries the two
 * cases from the issue in its `meta.blocked_links`:
 *   - one placeholder with no record of its own (a presigned link split at its
 *     query) beside one record of another address on the same host;
 *   - a forged chip (an entity-encoded bracket renders to placeholder text)
 *     beside the one real record of its host.
 * The chip must name the host alone in both: no path, no query length.
 *
 * ASSERTS before shooting: three chips, every `blocked-link-target` equals its
 * host, no `blocked-link-query` exists, the presigned link's query tail is not
 * shown beside its chip, and the lone record's card lists `/other-a` under its
 * own address below a title that never says "this link" -- a stale dist fails
 * the run instead of producing a lying screenshot.
 *   01 dark  -> the reply with the three chips, the forged chip's card open;
 *               then `-withheld-card` with the presigned-link chip's card open
 *   02 light -> same two frames, light theme
 *
 * Usage: node scripts/capture-blocked-link-chip-host-only.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/blocked-link-chip-host-only'
const SLOT = 'chat-blocked-link-chip'
const PROJECT = '/home/user/workspace/KiroCrew'
// Prove the wire shape the dashboard renders, not a copy: the one-record case is
// the backend fixture the unit test renders.
const oneOne = JSON.parse(readFileSync(fileURLToPath(new URL('../src/test/fixtures/blockedLinkOneOne.json', import.meta.url)), 'utf8'))
const FORGED_HOST = 'h.example-sink.net'

mkdirSync(OUT, { recursive: true })

const now = () => Date.now() / 1000

const slots = [{
  key: SLOT,
  title: 'Where did the links go?',
  running: false,
  last_message: 'Two of the links were blocked',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: now() - 900, content: 'Share the report link and the sink URL.' },
    {
      role: 'assistant',
      ts: now() - 60,
      content: `${oneOne.content}\n\nReal: [REDACTED: suspicious URL to ${FORGED_HOST}]\n\nOther: &#91;REDACTED: suspicious URL to ${FORGED_HOST}&#93;`,
      meta: {
        blocked_links: [
          ...oneOne.blocked_links,
          { domain: FORGED_HOST, rule: 'exfil_query_length', path: '/real', query_chars: 290, url: `https://${FORGED_HOST}/real?q=x`, url_withheld: null },
        ],
      },
    },
  ],
}

async function capture(browser, base, theme, name) {
  const context = await browser.newContext({ viewport: { width: 1500, height: 950 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  await page.routeWebSocket(/\/api\/ws/, () => {})
  await stubDashboardApi(page, {
    slots,
    theme,
    extra: async (path, route) => {
      if (path.startsWith('/api/chat/slots/')) { json(route, detail); return true }
      if (path === '/api/recent-projects') { json(route, { dirs: [PROJECT] }); return true }
      if (path === '/api/chat/nav/resolve-links') { json(route, { summaries: [] }); return true }
      return false
    },
  })
  logPageProblems(page)

  await page.addInitScript((t, slot) => {
    localStorage.clear()
    localStorage.setItem('mc-theme', t)
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-active-slot', slot)
  }, theme, SLOT)
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })

  await page.waitForSelector('[data-testid="blocked-link-chip"]', { timeout: 15000 })
  const chips = page.locator('[data-testid="blocked-link-chip"]')
  const count = await chips.count()
  if (count !== 3) throw new Error(`expected 3 chips, got ${count}`)
  const targets = await page.locator('[data-testid="blocked-link-target"]').allTextContents()
  const hosts = [oneOne.blocked_links[0].domain, FORGED_HOST, FORGED_HOST]
  if (JSON.stringify(targets) !== JSON.stringify(hosts)) throw new Error(`chip targets ${JSON.stringify(targets)} != hosts ${JSON.stringify(hosts)}`)
  if (await page.locator('[data-testid="blocked-link-query"]').count() !== 0) throw new Error('a chip still shows a query length')
  if ((await page.locator('main').textContent())?.includes('X-Amz-Date')) throw new Error('the presigned query tail still runs on beside its chip')

  // Open the forged chip's card: it lists the one real record under its own
  // address, which the chip beside it never claimed.
  const inspect = page.locator('[data-testid="blocked-link-inspect"]')
  await inspect.nth(2).click()
  await page.waitForSelector('[data-testid="blocked-link-card"]', { timeout: 5000 })
  await page.waitForTimeout(500)
  await page.screenshot({ path: `${OUT}/${name}.png` })
  console.log('wrote', `${OUT}/${name}.png`)

  // Close it and open the presigned-link chip's card: the lone record is listed
  // under its own address, its full address is withheld, and the title never
  // presents it as the clicked link.
  await inspect.nth(2).click()
  await page.waitForSelector('[data-testid="blocked-link-card"]', { state: 'detached', timeout: 5000 })
  await inspect.nth(0).click()
  await page.waitForSelector('[data-testid="blocked-link-card"]', { timeout: 5000 })
  const lone = page.locator('[data-testid="blocked-link-card"]')
  if (await lone.locator('[data-testid="blocked-link-entry-target"]').textContent() !== `${oneOne.blocked_links[0].domain}/other-a`) throw new Error('the lone record is not listed under its own address')
  if (/this link/i.test((await lone.textContent()) ?? '')) throw new Error('the lone record card still says "this link"')
  if (await page.locator('[data-testid="blocked-link-review"]').count() !== 0) throw new Error('a withheld address offers a full-URL view')
  await page.waitForTimeout(500)
  await page.screenshot({ path: `${OUT}/${name}-withheld-card.png` })
  console.log('wrote', `${OUT}/${name}-withheld-card.png`)
  await context.close()
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  await capture(browser, base, 'dark', '01-blocked-link-chip-host-only-dark')
  await capture(browser, base, 'light', '02-blocked-link-chip-host-only-light')
  await browser.close()
  srv.close()
}

main().catch(e => { console.error(e); process.exit(1) })

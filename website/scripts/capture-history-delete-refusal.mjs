/**
 * Screenshot harness for the history-delete refusal notice.
 *
 * `DELETE /api/sessions/{key}` answers 409 `{ ok: false, error, code }` when the
 * row's cron ownership cannot be established. The dashboard keeps the row in
 * the Older Sessions pane -- nothing was deleted -- and renders an ErrorNotice
 * above the composer whose sentence is chosen from `code`, never from the
 * English `error` string. Runs the REAL built SPA (website/dist) with every
 * /api/** call answered from fixtures -- gateway-free, same technique as
 * capture-older-sessions-hint.mjs.
 *
 * Usage: node scripts/capture-history-delete-refusal.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/history-delete-refusal'
mkdirSync(OUT, { recursive: true })

const LOCALES = fileURLToPath(new URL('../src/i18n/locales/', import.meta.url))
const en = JSON.parse(readFileSync(LOCALES + 'en.json', 'utf-8'))
const DELETE_LABEL = en.pages.chatSidebar.delete_history_session
if (!DELETE_LABEL) throw new Error('catalog key delete_history_session missing -- renamed?')

const NOW = Date.now()
const hoursAgo = h => new Date(NOW - h * 3600_000).toISOString()
const AGENT = 'kiro'
const folders = [{ id: 'kiro', name: 'Kiro', order: 1, collapsed: false }]
const slot = (key, title, folder_id, ageHours) => ({
  key, title, messages: 4, running: false, agent: AGENT,
  created: hoursAgo(ageHours + 2), last_turn_ts: hoursAgo(ageHours), folder_id,
})
const slots = [slot('s-k1', 'Settings path navigation', 'kiro', 1), slot('s-r1', 'Fix sidebar drag bug', '', 2)]

const REFUSED_KEY = 'dashboard:h-1'
const REFUSED_TITLE = 'Nightly report cron owner'
const history = [
  { key: REFUSED_KEY, title: REFUSED_TITLE, modified: (NOW - 3 * 86400_000) / 1000, agent: AGENT, messages: 12 },
  { key: 'dashboard:h-2', title: 'Theme pack debugging', modified: (NOW - 5 * 86400_000) / 1000, agent: AGENT, messages: 7 },
]

// The gateway's refusal, verbatim in shape: 409 with the machine-readable code.
const REFUSAL = {
  ok: false,
  code: 'cron_ownership_unknown',
  error: 'the cron jobs this session owns could not be determined, so deleting it would leave them owned by nobody.',
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1400, height: 900 }, deviceScaleFactor: 2 })

  async function load(theme) {
    const page = await context.newPage()
    let deletes = 0
    await stubDashboardApi(page, {
      folders, slots, theme,
      extra: async (path, route) => {
        if (path === '/api/sessions' && route.request().method() === 'GET') {
          await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ sessions: history, has_more: false, total: history.length }) })
          return true
        }
        if (path.startsWith('/api/sessions/') && route.request().method() === 'DELETE') {
          deletes += 1
          await route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify(REFUSAL) })
          return true
        }
        return false
      },
    })
    logPageProblems(page)
    page.on('dialog', d => d.accept())
    await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2600)
    // Open the Older Sessions pane so the history row is on screen.
    await page.getByTestId('older-sessions-hint-root').click()
    await page.getByText(REFUSED_TITLE).first().waitFor({ timeout: 15000 })
    await page.waitForTimeout(400)
    return { page, deleted: () => deletes }
  }

  for (const theme of ['dark', 'light']) {
    const { page, deleted } = await load(theme)
    const title = page.getByText(REFUSED_TITLE).first()
    await title.hover()
    await page.getByRole('button', { name: DELETE_LABEL, exact: true }).first().click({ force: true })
    // The refusal: row still listed, notice rendered from the code.
    const notice = page.getByTestId('undeletable-history-error')
    await notice.waitFor({ timeout: 15000 })
    if (deleted() !== 1) throw new Error('expected exactly one DELETE, saw ' + deleted())
    if ((await page.getByText(REFUSED_TITLE).count()) === 0) throw new Error('refused row vanished from the sidebar')
    const text = await notice.innerText()
    if (!text.includes(REFUSED_TITLE)) throw new Error('notice does not name the row: ' + text)
    await page.waitForTimeout(400)
    await page.screenshot({ path: OUT + '/01-refusal-notice-' + theme + '.png' })
    await notice.screenshot({ path: OUT + '/02-notice-close-up-' + theme + '.png' })
    console.log(theme, 'notice:', text)
    await page.close()
  }

  await browser.close()
  srv.close()
}

main().catch((e) => { console.error(e); process.exit(1) })

/**
 * Screenshot harness for the sidebar search / renamed-title fix.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server with all /api/** answered from fixtures (gateway-free).
 *
 * Shape that reaches the change: a query at or above SEARCH_MIN_CHARS makes the
 * sidebar switch to backend-ranked results, and the backend here returns an
 * EMPTY set — the state a just-renamed session sits in until the search
 * refreshes. Before the fix that session was hidden; now the local title match
 * ORs it back in.
 *
 * The same pair also shows the OR's scope: `agent-only` matches on its agent
 * name and not its title, so it must NOT be appended. Its absence from frame 2
 * is the scoping, and its presence in frame 1 proves it exists to be excluded.
 *
 * Usage: node scripts/capture-search-rename-refresh.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/search-rename-refresh'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

const QUERY = 'quarterly review'
const ACTIVE = 'chat-active'

const slots = [
  {
    key: ACTIVE, title: 'Deployment log', messages: 6, running: false,
    agent: 'kirocrew', created: '2026-08-01T09:00:00Z', last_ts: '2026-08-13T10:00:00Z', folder_id: '',
  },
  {
    // The renamed session. Its title matches the query; the backend does not
    // know that yet, so only the local title match can surface it.
    key: 'chat-renamed', title: 'Quarterly review notes', messages: 42, running: false,
    agent: 'kirocrew', created: '2026-07-02T09:00:00Z', last_ts: '2026-08-13T09:40:00Z', folder_id: '',
  },
  {
    // Matches the query on AGENT only. The backend deliberately excluded it, so
    // the scoped OR must leave it excluded too.
    key: 'chat-agent-only', title: 'Unrelated title', messages: 11, running: false,
    agent: 'quarterly-review-bot', created: '2026-07-20T09:00:00Z', last_ts: '2026-08-13T09:20:00Z', folder_id: '',
  },
  {
    key: 'chat-decoy', title: 'Pipeline triage', messages: 8, running: false,
    agent: 'oncall', created: '2026-08-05T09:00:00Z', last_ts: '2026-08-13T09:00:00Z', folder_id: '',
  },
]

const messages = [
  { role: 'user', content: 'Where does the deployment stand?', ts: '2026-08-13T09:58:00Z', meta: { mid: 'm-1' } },
  { role: 'assistant', content: 'Staging is green; production is waiting on the sign-off.', ts: '2026-08-13T09:58:30Z', meta: { mid: 'm-2' } },
]

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1280, height: 820 }, deviceScaleFactor: 2 })
  const page = await context.newPage()

  let searchCalls = 0

  await stubDashboardApi(page, {
    folders: [], slots,
    extra: async (path, route) => {
      if (path === '/api/sessions/search') {
        // Empty on purpose: the backend has not re-indexed the rename yet.
        searchCalls += 1
        await json(route, { sessions: [] })
        return true
      }
      if (path === '/api/sessions') { await json(route, { sessions: [], has_more: false }); return true }
      if (path === `/api/chat/slots/${ACTIVE}`) {
        await json(route, { messages, has_more: false, total: messages.length })
        return true
      }
      if (path === '/api/chat/pins') { await json(route, { pins: [] }); return true }
      return false
    },
  })
  logPageProblems(page)

  await page.goto(`${base}/chat/${ACTIVE}`, { waitUntil: 'domcontentloaded' })
  const search = page.getByPlaceholder(/search sessions/i)
  await search.waitFor({ timeout: 20_000 })
  await page.waitForTimeout(1200)

  // Frame 1 — no query. Every fixture session is listed, which is what makes
  // frame 2's exclusions meaningful rather than merely absent.
  const sidebar = page.locator('.sidebar').first()
  await sidebar.screenshot({ path: `${OUT}/${PREFIX}-1-no-query.png` })

  await search.fill(QUERY)
  // Debounce is 250ms; wait past it plus the fetch and the re-render.
  await page.waitForTimeout(1800)
  if (searchCalls === 0) throw new Error('backend search never fired — the frame would not show the ranked path')

  const renamed = page.getByText('Quarterly review notes')
  await renamed.waitFor({ state: 'visible', timeout: 10_000 })
  // Negative control on the fixture itself: if the agent-only row were visible
  // here the OR would still be unscoped, and this capture would be evidence of
  // the bug rather than of the fix.
  if (await page.getByText('Unrelated title').count()) {
    throw new Error('agent-only session is still listed — the OR is not scoped to title')
  }
  if (await page.getByText('Pipeline triage').count()) {
    throw new Error('non-matching session listed — the search path is not active')
  }

  await sidebar.screenshot({ path: `${OUT}/${PREFIX}-2-query-empty-backend.png` })

  console.log(`searchCalls=${searchCalls}`)
  await browser.close()
  srv.close()
  console.log(`wrote ${OUT}/${PREFIX}-{1-no-query,2-query-empty-backend}.png`)
}

main().catch(err => { console.error(err); process.exit(1) })

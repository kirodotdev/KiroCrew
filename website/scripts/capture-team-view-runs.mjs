/**
 * Screenshot for the team view's run counts and week feed after a routed
 * delegation: radar's log holds a select_crew decision and the spawn run it
 * led to, so the strip must count the run once and the feed must name it
 * "Delegated run", never "Chat".
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6831 --strictPort   # in another shell
 *   node scripts/capture-team-view-runs.mjs http://127.0.0.1:6831 ../temp-screenshots/spawn-member-activity
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { MEMBERS } from './lib/members-fixtures.mjs'

const BASE = process.argv[2] || 'http://127.0.0.1:6831'
const OUT = process.argv[3] || '../temp-screenshots/spawn-member-activity'
mkdirSync(OUT, { recursive: true })

const now = Date.now() / 1000
const ACTIVITY = {
  radar: [
    { ts: now - 60, via: 'spawn', project: '' },
    { ts: now - 90, via: 'select_crew', project: '' },
    { ts: now - 600, via: 'spawn', project: '/home/me/crew/workspace' },
  ],
}

const browser = await chromium.launch()
let failed = false
function check(name, ok, detail) {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
}

const page = await browser.newPage({ viewport: { width: 1280, height: 820 }, deviceScaleFactor: 1 })
const json = (route, body) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
await page.route(u => new URL(u).pathname.startsWith('/api/'), route => {
  const path = new URL(route.request().url()).pathname
  if (path === '/api/members') return json(route, { members: MEMBERS, default_agent: 'kirocrew' })
  if (path === '/api/teams') return json(route, { teams: [{ id: 'abc123abc123', name: 'Triage', members: ['radar', 'fixer'] }] })
  const activity = path.match(/^\/api\/members\/([^/]+)\/activity$/)
  if (activity) {
    const slug = decodeURIComponent(activity[1])
    return json(route, { slug, member: slug, capped: false, entries: ACTIVITY[slug] ?? [] })
  }
  const thread = path.match(/^\/api\/members\/([^/]+)\/thread$/)
  if (thread) {
    const slug = decodeURIComponent(thread[1])
    return json(route, { slot_key: `member-${slug}`, slug, member: slug, created: false })
  }
  if (/^\/api\/chat\/slots\/[^/]+$/.test(path)) return json(route, { key: path.split('/').pop(), running: false, messages: [] })
  const isList = /commands|skills|agents|sessions|files|history|models|artifacts|folders/.test(path)
  return route.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
})
// The roster rides a dropdown at this width; a deep link opens the team as its header click does.
await page.goto(`${BASE}/capture/members-page.html?theme=dark&route=${encodeURIComponent('/members?team=abc123abc123')}`)
await page.waitForSelector('[data-capture-root]')
const view = page.getByTestId('team-view')
await view.waitFor()
const radar = view.getByTestId('team-status-counts').first()
await radar.waitFor()
await page.waitForFunction(() => /Runs: 2 today/.test(document.querySelector('[data-testid="team-status-counts"]')?.textContent || ''), null, { timeout: 10_000 }).catch(() => {})
const counts = (await radar.textContent()) || ''
check('strip counts the run once', /Runs: 2 today · 2 this week/.test(counts), JSON.stringify(counts))
const rows = await view.getByTestId('team-week-row').allTextContents()
check('feed names delegated runs', rows.filter(r => r.includes('Delegated run')).length === 2, JSON.stringify(rows))
check('feed shows no Chat for a run', !rows.some(r => /Chat/.test(r)), JSON.stringify(rows))
await page.screenshot({ path: `${OUT}/after.png` })
await browser.close()
process.exit(failed ? 1 : 0)

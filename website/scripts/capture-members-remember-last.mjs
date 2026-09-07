/**
 * Screenshots for "open the last member on arrival, mirror it in the URL"
 * (Crew Members page). Drives website/capture/members-page.html with
 * `?nav=1`, which mounts the REAL MembersPage behind a caption bar that
 * prints the router's live URL and offers a history pop. Every frame asserts
 * its state before writing, so a frame cannot document the wrong state:
 *   01-arrival          desktop, /members with nothing remembered: the first
 *                       row (most recently active) is open on arrival, URL
 *                       says so, no "Pick a member" column
 *   02-gone-notice      desktop, deep link to a member that is gone: the
 *                       fallback thread is open and the warn-toned status
 *                       above it names the swap
 *   03-gone-roster      390px, the same dead link: back on the roster with
 *                       the notice above the list, no thread opened
 *   04a/04b-back        desktop: arrive from another page, switch members
 *                       twice, press Back ONCE -> the other page (switching
 *                       replaced, it did not stack)
 *   05a/05b-mobile-back 390px: tap a member (pushed), header back pops it ->
 *                       roster, URL back to /members
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6831 --strictPort   # in another shell
 *   node scripts/capture-members-remember-last.mjs http://127.0.0.1:6831 ../temp-screenshots/members-remember-last
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6831'
const OUT = process.argv[3] || '../temp-screenshots/members-remember-last'
mkdirSync(OUT, { recursive: true })

/** Four members, most-recently-active first so "the first row" is radar. */
const member = (name, kiro_agent, extra = {}) => ({
  name,
  slug: name,
  bound: false,
  slot_key: '',
  running: false,
  kiro_agent,
  workspace: 'default',
  memory_store: 'default',
  model: '',
  ...extra,
})
const MEMBERS = [
  member('radar', 'kirocrew-autofix', { bound: true, slot_key: 'member-radar', running: true, workspace: 'autofix', last_active_ts: 1000, last_message: 'Six new issues: four covered by open PRs.' }),
  member('scout', 'kirocrew-research', { memory_store: 'scout-own', model: 'claude-opus-5' }),
  member('fixer', 'kirocrew', { bound: true, slot_key: 'member-fixer', last_active_ts: 900, last_message: 'Two PRs opened for the queue.' }),
  member('scribe', 'kirocrew-lite', { workspace: 'docs' }),
]

const browser = await chromium.launch()
const failures = []
const check = (name, ok, detail) => {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failures.push(name)
}

const urlText = (page) => page.locator('[data-capture-url-text]').textContent()

async function newPage(route, viewport = { width: 1280, height: 820 }) {
  const page = await browser.newPage({ viewport, deviceScaleFactor: 1 })
  // Gateway-free: answer every REAL API call the mounted page (and the
  // ChatPane it hosts) makes. Predicate on the pathname — a glob would also
  // swallow vite-served source modules and break boot.
  await page.route((u) => new URL(u).pathname.startsWith('/api/'), (r) => {
    const path = new URL(r.request().url()).pathname
    const json = (body) => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
    if (path === '/api/members') return json({ members: MEMBERS, default_agent: 'kirocrew' })
    if (path === '/api/crons') return json({ jobs: [] })
    if (path === '/api/webhooks') return json({ tokens: [] })
    if (path === '/api/agents') return json({ agents: [], default_agent: 'kirocrew' })
    const thread = path.match(/^\/api\/members\/([^/]+)\/thread$/)
    if (thread) {
      const slug = decodeURIComponent(thread[1])
      return json({ slot_key: `member-${slug}`, slug, member: slug, created: false })
    }
    if (/^\/api\/members\/[^/]+\/activity$/.test(path)) return json({ slug: '', member: '', capped: false, entries: [] })
    const slot = path.match(/^\/api\/chat\/slots\/member-([^/]+)$/)
    if (slot) {
      const name = decodeURIComponent(slot[1])
      return json({
        key: `member-${name}`,
        title: name,
        running: false,
        messages: [
          { role: 'user', content: `What are you on, ${name}?`, ts: '2026-08-27T01:00:00Z' },
          { role: 'assistant', content: 'Six new issues: four covered by open PRs, one routed to needs-human, one queued as auto-fixable.', ts: '2026-08-27T01:00:05Z' },
        ],
      })
    }
    const isList = /commands|skills|agents|sessions|files|history|models|artifacts|folders/.test(path)
    return r.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
  })
  await page.goto(`${BASE}/capture/members-page.html?theme=dark&nav=1&route=${encodeURIComponent(route)}`)
  await page.waitForSelector('[data-capture-root]')
  return page
}

// 01 — arrival: nothing remembered, no member named -> first row opens.
{
  const page = await newPage('/members')
  await page.getByText('What are you on, radar?').waitFor()
  const url = await urlText(page)
  check('01-arrival URL names the open member', url === '/members?member=radar', `url=${url}`)
  const empty = await page.getByText(/Pick a member/i).count()
  check('01-arrival no empty column', empty === 0, `pick-a-member=${empty}`)
  const current = await page.locator('[data-testid="member-roster"] [aria-current="true"]').textContent()
  check('01-arrival row marked current', /radar/.test(current || ''), `current=${(current || '').trim().slice(0, 20)}`)
  await page.screenshot({ path: `${OUT}/01-arrival-dark.png` })
  await page.close()
}

// 02 — a link to a member that is gone: fallback opens, and SAYS so.
{
  const page = await newPage('/members?member=ghost')
  await page.getByTestId('member-gone-notice').waitFor()
  const notice = await page.getByTestId('member-gone-notice').textContent()
  check('02-gone-notice leads with the swap', /^Showing radar/.test((notice || '').trim()), `notice=${(notice || '').trim()}`)
  check('02-gone-notice names the gone member', /“ghost” is no longer on the roster/.test(notice || ''), 'quoted name present')
  const cls = await page.getByTestId('member-gone-notice').getAttribute('class')
  check('02-gone-notice warn tone', /text-warn/.test(cls || ''), `class=${cls}`)
  const url = await urlText(page)
  check('02-gone-notice URL rewritten', url === '/members?member=radar', `url=${url}`)
  await page.getByText('What are you on, radar?').waitFor()
  await page.screenshot({ path: `${OUT}/02-gone-notice-dark.png` })
  await page.close()
}

// 03 — the same dead link below md: the roster is the answer, with the notice above it.
{
  const page = await newPage('/members?member=ghost', { width: 390, height: 780 })
  await page.getByTestId('member-gone-roster-notice').waitFor()
  const notice = await page.getByTestId('member-gone-roster-notice').textContent()
  check('03-gone-roster notice', /“ghost” is no longer on the roster/.test(notice || ''), `notice=${(notice || '').trim()}`)
  const url = await urlText(page)
  check('03-gone-roster URL cleared', url === '/members', `url=${url}`)
  const thread = await page.getByTestId('member-back').count()
  check('03-gone-roster no thread opened', thread === 0, `thread-headers=${thread}`)
  await page.screenshot({ path: `${OUT}/03-gone-roster-mobile-390.png` })
  await page.close()
}

// 04 — switching replaces: arrive from another page, walk two members, one Back leaves.
{
  const page = await newPage('/elsewhere')
  await page.locator('[data-capture-go-members]').click()
  await page.getByText('What are you on, radar?').waitFor()
  await page.locator('[data-testid="member-roster"]').getByText('scout', { exact: true }).click()
  await page.getByText('What are you on, scout?').waitFor()
  await page.locator('[data-testid="member-roster"]').getByText('fixer', { exact: true }).click()
  await page.getByText('What are you on, fixer?').waitFor()
  let url = await urlText(page)
  check('04a-back URL after two switches', url === '/members?member=fixer', `url=${url}`)
  await page.screenshot({ path: `${OUT}/04a-two-switches-dark.png` })
  await page.locator('[data-capture-back]').click()
  await page.locator('[data-capture-elsewhere]').waitFor()
  url = await urlText(page)
  check('04b-back one press leaves the page', url === '/elsewhere', `url=${url}`)
  await page.screenshot({ path: `${OUT}/04b-one-back-elsewhere-dark.png` })
  await page.close()
}

// 05 — below md: the roster->thread step is pushed; the header back pops it.
{
  const page = await newPage('/members', { width: 390, height: 780 })
  await page.locator('[data-testid="member-roster"]').getByText('scout', { exact: true }).waitFor()
  let url = await urlText(page)
  check('05-mobile no auto-open', url === '/members', `url=${url}`)
  await page.locator('[data-testid="member-roster"]').getByText('scout', { exact: true }).click()
  await page.getByTestId('member-back').waitFor()
  url = await urlText(page)
  check('05a-mobile tap opens thread', url === '/members?member=scout', `url=${url}`)
  await page.screenshot({ path: `${OUT}/05a-mobile-thread-390.png` })
  await page.getByTestId('member-back').click()
  await page.locator('[data-testid="member-roster"]').getByText('scout', { exact: true }).waitFor()
  url = await urlText(page)
  check('05b-mobile header back pops to roster', url === '/members', `url=${url}`)
  await page.screenshot({ path: `${OUT}/05b-mobile-back-roster-390.png` })
  await page.close()
}

await browser.close()
if (failures.length) {
  console.error(`CAPTURE FAILED: ${failures.length} frame(s) did not match their asserted state: ${failures.join(', ')}`)
  process.exit(1)
}
console.log('all frames verified')

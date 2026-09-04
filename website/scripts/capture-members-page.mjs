/**
 * Screenshots for the Crew Members page (per-member pinned DM threads).
 *
 * Drives the isolated capture entry (website/capture/members-page.html),
 * which mounts the REAL MembersPage. Every frame asserts its state before
 * writing, so a frame cannot document the wrong state:
 *   01-roster        four-member roster, live presence dot on the working one
 *   02-thread        clicking the REAL row opened the pinned thread: header
 *                    pin chip + detail drawer with the shared-memory note
 *   03-mobile        390px viewport, list column
 *   04-roster-light  light theme parity
 *   06-driving       drawer "Driving sessions": radar's workers by
 *                    created_by, status dots, fold/expand, empty state (fixer)
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6831 --strictPort   # in another shell
 *   node scripts/capture-members-page.mjs http://127.0.0.1:6831 ../temp-screenshots/crew-members
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6831'
const OUT = process.argv[3] || '../temp-screenshots/crew-members'
mkdirSync(OUT, { recursive: true })

const MEMBERS = [
  { name: 'radar', slug: 'radar', bound: true, slot_key: 'member-radar', running: true, kiro_agent: 'kirocrew-autofix', workspace: 'autofix', memory_store: 'default', model: '', last_active_ts: 1000, last_message: 'Six new issues: four covered by open PRs.' },
  { name: 'scout', slug: 'scout', bound: false, slot_key: '', running: false, kiro_agent: 'kirocrew-research', workspace: 'default', memory_store: 'default', model: 'claude-opus-5' },
  { name: 'fixer', slug: 'fixer', bound: true, slot_key: 'member-fixer', running: false, kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', model: '', last_active_ts: 900, last_message: 'Two PRs opened for the queue.' },
  { name: 'scribe', slug: 'scribe', bound: false, slot_key: '', running: false, kiro_agent: 'kirocrew-lite', workspace: 'docs', memory_store: 'default', model: '' },
]

const browser = await chromium.launch()
let failed = false

function check(name, ok, detail) {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
  return ok
}

async function newPage(theme, viewport = { width: 1280, height: 820 }) {
  const page = await browser.newPage({ viewport, deviceScaleFactor: 1 })
  // Gateway-free: answer every REAL API call the mounted page (and the
  // ChatPane it hosts) makes. Predicate on the pathname — a glob would also
  // swallow vite-served source modules and break boot. Array-shaped
  // endpoints must answer [] ({} crashes their .map consumers).
  await page.route(u => new URL(u).pathname.startsWith('/api/'), route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/members') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ members: MEMBERS, default_agent: 'kirocrew' }) })
    }
    if (path === '/api/crons') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ jobs: [
        { id: 'j1', name: 'nightly-triage', message: '', enabled: true, schedule: '0 2 * * *', last_status: 'ok', agent: 'radar' },
        { id: 'j2', name: 'queue-scan', message: '', enabled: false, schedule: '*/15 * * * *', last_status: 'ok', agent: 'radar' },
      ] }) })
    }
    if (path === '/api/webhooks') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ tokens: [
        { id: 'w1', label: 'ci-callback', display_prefix: 'kc_whk_4f2b', last4: 'a9c1', created_at: 0, last_used_at: null, require_signature: true, agent: 'radar', enabled: true },
      ] }) })
    }
    if (path === '/api/agents') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ agents: [], default_agent: 'kirocrew' }) })
    }
    const thread = path.match(/^\/api\/members\/([^/]+)\/thread$/)
    if (thread) {
      const slug = decodeURIComponent(thread[1])
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ slot_key: `member-${slug}`, slug, member: slug, created: false }) })
    }
    if (/^\/api\/members\/[^/]+\/activity$/.test(path)) {
      const now = Date.now() / 1000
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
        slug: 'radar',
        member: 'radar',
        capped: false,
        entries: [
          { ts: now - 180, via: 'chat', project: '' },
          { ts: now - 2700, via: 'select_crew', project: 'demo-app' },
          { ts: now - 7200, via: 'chat', project: '' },
          { ts: now - 86400, via: 'chat', project: 'demo-app' },
        ],
      }) })
    }
    if (/^\/api\/chat\/slots\/[^/]+$/.test(path)) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
        key: 'member-radar', title: 'radar', running: false, messages: [
          { role: 'user', content: 'What did you triage tonight?', ts: '2026-08-27T01:00:00Z' },
          { role: 'assistant', content: 'Six new issues: four covered by open PRs, one routed to needs-human, one queued as auto-fixable.', ts: '2026-08-27T01:00:05Z' },
        ],
      }) })
    }
    const isList = /commands|skills|agents|sessions|files|history|models|artifacts|folders/.test(path)
    return route.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
  })
  await page.goto(`${BASE}/capture/members-page.html?theme=${theme}`)
  await page.waitForSelector('[data-capture-root]')
  await page.getByText('radar', { exact: true }).first().waitFor()
  return page
}

// 01 — roster at rest (dark)
{
  const page = await newPage('dark')
  const rows = await page.getByRole('listitem').count()
  check('01-roster rows', rows >= 4, `listitems=${rows}`)
  const statusLabels = await page.getByText(/^(Working|Idle)$/).count()
  check('01-roster no status labels', statusLabels === 0, `status-labels=${statusLabels} (presence rides the avatar dot)`)
  const preview = await page.getByText('Six new issues: four covered by open PRs.').count()
  check('01-roster last-message preview', preview >= 1, `preview-rows=${preview}`)
  const search = await page.getByTestId('member-search').count()
  check('01-roster search box', search === 1, `search-inputs=${search}`)
  const dots = await page.getByTestId('member-presence-dot').count()
  check('01-roster presence dot only when running', dots === 1, `dots=${dots} (radar runs; idle rows show none)`)
  await page.screenshot({ path: `${OUT}/01-roster-dark.png` })
  await page.close()
}

// 02 — thread open + drawer (dark)
{
  const page = await newPage('dark')
  await page.getByText('radar', { exact: true }).first().click()
  // No pin chip: the pin is an invariant of every member thread, so the
  // header no longer announces it. The drawer toggle is the one header action.
  await page.getByTestId('member-drawer').waitFor()
  const chipGone = !(await page.getByTestId('member-pin-chip').isVisible())
  check('02-thread no pin chip', chipGone, 'header carries no pin chip')
  const drawer = await page.getByTestId('member-drawer').textContent()
  check('02-thread drawer config', /kirocrew-autofix/.test(drawer || ''), 'agent template shown')
  check('02-thread shared-memory note', /share one memory/i.test(drawer || ''), 'disclosure present')
  // Activity additions: honest counters + the recorded timeline. Four fixture
  // entries; the routed one carries its project label.
  await page.getByTestId('member-activity').waitFor()
  const activityRows = await page.getByTestId('member-activity').locator('li').count()
  check('02-thread activity timeline', activityRows === 4, `entries=${activityRows}`)
  const stats = await page.getByTestId('member-stats').textContent()
  check('02-thread stat cards', /Today/.test(stats || '') && /Past 7 days/.test(stats || ''), 'both honest counters labeled')
  const status = await page.getByTestId('member-drawer-status').textContent()
  check('02-thread drawer status', /Working/.test(status || ''), `status=${(status || '').trim()}`)
  // Wake sources: two schedules (one paused) + one webhook bound to radar.
  await page.getByTestId('member-wake-sources').waitFor()
  const wakeRows = await page.getByTestId('member-wake-sources').locator('li').count()
  const wakeText = await page.getByTestId('member-wake-sources').textContent()
  check('02-thread wake sources', wakeRows === 3, `rows=${wakeRows}`)
  check('02-thread wake content', /nightly-triage/.test(wakeText || '') && /ci-callback/.test(wakeText || '') && /paused/.test(wakeText || ''), 'schedule + webhook + paused marker present')
  await page.screenshot({ path: `${OUT}/02-thread-drawer-dark.png` })
  await page.close()
}

// 03 — narrow viewport: roster first, then the single-pane switch. The
// selected state is the page's core interaction, so the narrow capture must
// exercise it: pick a member, prove the thread took over (back button
// visible, roster hidden), and keep the frame.
{
  const page = await newPage('dark', { width: 390, height: 780 })
  const rows = await page.getByRole('listitem').count()
  check('03-mobile rows', rows >= 4, `listitems=${rows}`)
  await page.screenshot({ path: `${OUT}/03-mobile-390.png` })
  await page.getByText('radar', { exact: true }).first().click()
  await page.getByTestId('member-back').waitFor()
  const backVisible = await page.getByTestId('member-back').isVisible()
  // The pin chip and the header Edit button no longer exist at ANY width:
  // the pin is an invariant (nothing to announce) and Edit lives in the
  // drawer as a secondary action. Assert their absence stayed absent.
  const editHidden = !(await page.getByTestId('member-edit-jump').isVisible())
  const chipHidden = !(await page.getByTestId('member-pin-chip').isVisible())
  check(
    '03b-mobile selected single-pane',
    backVisible && editHidden && chipHidden,
    'back visible; no header edit, no pin chip (drawer carries edit)',
  )
  await page.screenshot({ path: `${OUT}/03b-mobile-390-thread.png` })
  await page.close()
}

// 04 — light theme parity
{
  const page = await newPage('light')
  await page.getByText('radar', { exact: true }).first().click()
  await page.getByTestId('member-drawer').waitFor()
  await page.screenshot({ path: `${OUT}/04-thread-light.png` })
  await page.close()
}

// 06 — "Driving sessions": the worker sessions this member opened and steers,
// read off the live slots frame by `created_by`. The entry seeds seven radar
// workers (one per status the dot distinguishes, plus overflow) and one
// scribe worker that must stay out. Folded at five, Show all expands; a row
// is a jump into that session (MemoryRouter here, so we assert the intent by
// the row being a button, not by navigation).
{
  const page = await newPage('dark')
  await page.getByText('radar', { exact: true }).first().click()
  await page.getByTestId('member-driving-sessions').waitFor()
  const rows = page.getByTestId('member-driving-row')
  check('06-driving folded at five', (await rows.count()) === 5, `rows=${await rows.count()}`)
  const statuses = await rows.evaluateAll((els) => els.map((e) => e.getAttribute('data-status')))
  check(
    '06-driving status order newest-first',
    statuses.join(',') === 'permission,running,question,idle,idle',
    `statuses=${statuses.join(',')}`,
  )
  const text = await page.getByTestId('member-driving-sessions').textContent()
  check('06-driving excludes other members', !/release notes/.test(text || ''), 'scribe worker absent')
  check('06-driving titles present', /sidebar drop/.test(text || '') && /seven fresh/.test(text || ''), 'worker titles rendered')
  const toggle = page.getByTestId('member-driving-toggle')
  check('06-driving toggle label', /Show all \(7\)/.test((await toggle.textContent()) || ''), `toggle=${await toggle.textContent()}`)
  await page.screenshot({ path: `${OUT}/06-driving-sessions-dark.png` })
  await toggle.click()
  check('06b-driving expanded', (await rows.count()) === 7, `rows=${await rows.count()}`)
  check('06b-driving toggle collapses', /Show less/.test((await toggle.textContent()) || ''), 'toggle flipped')
  await page.screenshot({ path: `${OUT}/06b-driving-sessions-expanded-dark.png` })
  await page.close()
}

// 06c — the empty state: a member with no workers reads one honest sentence.
{
  const page = await newPage('dark')
  await page.getByText('fixer', { exact: true }).first().click()
  await page.getByTestId('member-drawer').waitFor()
  await page.getByTestId('member-driving-empty').waitFor()
  const empty = await page.getByTestId('member-driving-empty').textContent()
  check('06c-driving empty state', /Not driving any sessions/.test(empty || ''), `empty=${(empty || '').trim()}`)
  await page.screenshot({ path: `${OUT}/06c-driving-sessions-empty-dark.png` })
  await page.close()
}

// 05 — wide viewport: the DM transcript carries the main chat's reading
// measure — the user's Content width setting resolved through CONTENT_WIDTH;
// a fresh profile is the compact default, 800px. Only visible on a wide
// column, hence the 1920px frame; the assertion reads the computed style so
// a silently dropped prop cannot pass.
{
  const page = await newPage('dark', { width: 1920, height: 900 })
  await page.getByText('radar', { exact: true }).first().click()
  await page.getByTestId('chat-pane-stub').waitFor().catch(() => {})
  await page.getByText('What did you triage tonight?').waitFor()
  const cap = await page.evaluate(() => {
    const row = document.querySelector('[data-chat-pane] .mx-auto.w-full')
    return row ? getComputedStyle(row).maxWidth : ''
  })
  check('05-wide transcript reading width', cap === '800px', `maxWidth=${cap}`)
  // Both halves of the measure: the composer follows the setting's input
  // width too (compact = 816px), not its 900px CSS-var fallback.
  const inputCap = await page.evaluate(() => {
    const el = document.querySelector('[data-chat-pane] .input-area')
    return el ? getComputedStyle(el).maxWidth : ''
  })
  check('05-wide composer input width', inputCap === '816px', `maxWidth=${inputCap}`)
  await page.screenshot({ path: `${OUT}/05-thread-wide-1920.png` })
  await page.close()
}

await browser.close()
if (failed) {
  console.error('CAPTURE FAILED: at least one frame did not match its asserted state')
  process.exit(1)
}
console.log('all frames verified')

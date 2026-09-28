/**
 * Screenshot for the Crew Members roster "Stopped" indicator.
 *
 * The roster subtitle is the last CONVERSATIONAL line — the stop card's JSON
 * is skipped — so a thread the user has stopped reads as ongoing work
 * ("Running the analysis now."). The server flags such a row with
 * `last_message_stopped`, and the page renders a LOCALIZED "Stopped" chip
 * beside the preview (the word is never sent from the server, whose preview is
 * computed without the client's locale). The server flag is false once a newer
 * real message lands.
 *
 * Drives the isolated capture entry (website/capture/members-page.html), which
 * mounts the REAL MembersPage. The frame asserts its state before writing, so
 * it cannot document the wrong thing:
 *   01-stopped-chip  the just-stopped row shows the chip beside its preview;
 *                    the sibling row (a later real message) shows none — one
 *                    chip on the whole roster.
 *
 * The roster rows here are inline, not the shared MEMBERS fixture: this frame
 * needs the `last_message_stopped` flag on exactly one row, and the other
 * capture scripts' frames must keep photographing the same unflagged crew.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6831 --strictPort   # in another shell
 *   node scripts/capture-members-stopped-indicator.mjs http://127.0.0.1:6831 ../temp-screenshots/9708-members-stopped
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6831'
const OUT = process.argv[3] || '../temp-screenshots/9708-members-stopped'
mkdirSync(OUT, { recursive: true })

// Two members: one whose newest event is a Stop press (flag set, preview reads
// as ongoing work), one whose newest event is a real reply (no flag).
const MEMBERS = [
  {
    name: 'analyst', slug: 'analyst', bound: true, slot_key: 'member-analyst',
    running: false, kiro_agent: 'kirocrew', workspace: 'default',
    memory_store: 'default', model: '', last_active_ts: 1000,
    last_message: 'Running the analysis now.', last_message_stopped: true,
  },
  {
    name: 'scribe', slug: 'scribe', bound: true, slot_key: 'member-scribe',
    running: false, kiro_agent: 'kirocrew', workspace: 'docs',
    memory_store: 'default', model: '', last_active_ts: 900,
    last_message: 'Draft pushed — ready for review.',
  },
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
  await page.route(u => new URL(u).pathname.startsWith('/api/'), route => {
    const path = new URL(route.request().url()).pathname
    const json = (body) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
    if (path === '/api/members') return json({ members: MEMBERS, default_agent: 'kirocrew' })
    if (path === '/api/crons') return json({ jobs: [] })
    if (path === '/api/webhooks') return json({ tokens: [] })
    if (path === '/api/agents') return json({ agents: [], default_agent: 'kirocrew' })
    const thread = path.match(/^\/api\/members\/([^/]+)\/thread$/)
    if (thread) {
      const slug = decodeURIComponent(thread[1])
      return json({ slot_key: `member-${slug}`, slug, member: slug, created: false })
    }
    if (/^\/api\/members\/[^/]+\/activity$/.test(path)) return json({ slug: 'analyst', member: 'analyst', capped: false, entries: [] })
    if (/^\/api\/chat\/slots\/[^/]+$/.test(path)) return json({ key: 'member-analyst', title: 'analyst', running: false, messages: [] })
    const isList = /commands|skills|agents$|sessions|files|history|models|artifacts|folders|slots$/.test(path)
    return route.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
  })
  await page.goto(`${BASE}/capture/members-page.html?theme=${theme}`)
  await page.waitForSelector('[data-capture-root]')
  await page.getByText('analyst', { exact: true }).first().waitFor()
  return page
}

// 01 — the roster with one just-stopped row
{
  const page = await newPage('dark')
  const chips = page.getByTestId('member-stopped-indicator')
  // Exactly one chip: the just-stopped member's. The sibling row (a later real
  // message) shows none — the flag cleared for it.
  check('01-stopped one chip on the roster', (await chips.count()) === 1, `chips=${await chips.count()}`)
  check('01-stopped chip text localized', /Stopped/.test((await chips.first().textContent()) || ''), `text=${(await chips.first().textContent()) || ''}`)
  // The conversational preview still shows beside the chip (the chip does not
  // replace it — it marks it).
  const preview = await page.getByText('Running the analysis now.').count()
  check('01-stopped preview still shown', preview >= 1, `preview-rows=${preview}`)
  // The unflagged member's preview is plain, no chip.
  const other = await page.getByText('Draft pushed — ready for review.').count()
  check('01-stopped sibling preview plain', other >= 1, `sibling-rows=${other}`)
  await page.screenshot({ path: `${OUT}/01-stopped-chip-dark.png` })
  await page.close()
}

// 02 — light theme parity
{
  const page = await newPage('light')
  const chips = page.getByTestId('member-stopped-indicator')
  check('02-stopped one chip (light)', (await chips.count()) === 1, `chips=${await chips.count()}`)
  await page.screenshot({ path: `${OUT}/02-stopped-chip-light.png` })
  await page.close()
}

await browser.close()
if (failed) {
  console.error('CAPTURE FAILED: at least one frame did not match its asserted state')
  process.exit(1)
}
console.log('all frames verified')

/**
 * Screenshots + computed-layout diagnostics for the empty Members DM thread
 * placeholder alignment at 768x1024 vs 1440x900, on the REAL MembersPage.
 *
 * Drives website/capture/members-empty-placeholder.html. Gateway-free: answers
 * GET /api/members (a one-member roster whose slot_key matches the seeded empty
 * slot) and POST /api/members/{slug}/thread (confirms that slot_key). For each
 * viewport it waits for the real "Session ready…" placeholder, then reads the
 * real computed geometry: the placeholder text's own rect (via a Range) and the
 * thread column's rect, so it measures where the glyphs SIT — jsdom cannot.
 *
 * Screenshots are the PR evidence (never committed).
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6833 --strictPort   # in another shell
 *   node scripts/capture-members-empty-placeholder.mjs http://127.0.0.1:6833 ../temp-screenshots/members-empty-placeholder before
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6833'
const OUT = process.argv[3] || '../temp-screenshots/members-empty-placeholder'
const LABEL = process.argv[4] || 'after'
mkdirSync(OUT, { recursive: true })

const PLACEHOLDER = 'Session ready. Type a message to start.'
const MEMBER = 'Oncall'
const SLUG = 'oncall'
const SLOT = 'member-oncall-slot'

const ROSTER = { members: [
  { name: MEMBER, slug: SLUG, slot_key: SLOT, running: false, last_active_ts: 0, source: 'kirocrew' },
] }

const browser = await chromium.launch()
let failed = false
function check(name, ok, detail) {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
}

const VIEWPORTS = [
  { name: '768x1024', width: 768, height: 1024 },
  { name: '1440x900', width: 1440, height: 900 },
]

for (const theme of ['dark']) {
  for (const vp of VIEWPORTS) {
    const page = await browser.newPage({ viewport: { width: vp.width, height: vp.height }, deviceScaleFactor: 1 })
    await page.route(u => new URL(u).pathname.startsWith('/api/'), route => {
      const req = route.request()
      const path = new URL(req.url()).pathname
      if (path === '/api/members' && req.method() === 'GET') {
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(ROSTER) })
      }
      if (path === `/api/members/${SLUG}/thread` && req.method() === 'POST') {
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ slot_key: SLOT, slug: SLUG, member: MEMBER }) })
      }
      // Endpoints that return a bare array vs a shaped object. The Members
      // side panel (docked beside at 1440) reads several; give each the shape
      // its consumer iterates so the wide layout does not crash.
      if (/\/activity(\?|$)/.test(path)) {
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ slug: SLUG, member: MEMBER, entries: [], capped: false }) })
      }
      if (/\/webhooks(\?|$)/.test(path)) {
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ tokens: [] }) })
      }
      const isList = /commands|skills|agents|sessions|files|history|models|artifacts|folders|crons|jobs/.test(path)
      return route.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
    })
    await page.goto(`${BASE}/capture/members-empty-placeholder.html?theme=${theme}`)
    await page.waitForSelector('[data-capture-root]')
    const ph = page.getByText(PLACEHOLDER)
    await ph.waitFor({ timeout: 15000 })

    const geo = await ph.evaluate((el) => {
      const div = el
      const header = document.querySelector('[data-testid="member-thread-header"]')
      // The thread column is the <section> that holds the header + pane.
      const col = header ? header.closest('section') || header.parentElement : null
      const scroller = div.closest('.chat-container')
      const colBox = (col || div.parentElement).getBoundingClientRect()
      const scBox = scroller ? scroller.getBoundingClientRect() : null
      const cs = getComputedStyle(div)
      const range = document.createRange(); range.selectNodeContents(div)
      const textBox = range.getBoundingClientRect()
      const divBox = div.getBoundingClientRect()
      return {
        textAlign: cs.textAlign,
        maxWidth: cs.maxWidth,
        divWidth: Math.round(divBox.width),
        divLeft: Math.round(divBox.left),
        colWidth: Math.round(colBox.width),
        colLeft: Math.round(colBox.left),
        scWidth: scBox ? Math.round(scBox.width) : null,
        scLeft: scBox ? Math.round(scBox.left) : null,
        textLeftFromColLeft: Math.round(textBox.left - colBox.left),
        textCenterFromColLeft: Math.round((textBox.left + textBox.width / 2) - colBox.left),
        colCenter: Math.round(colBox.width / 2),
        // center of the placeholder's OWN block (scroller) — the reference the
        // eye uses when there are no messages
        scCenterFromColLeft: scBox ? Math.round((scBox.left + scBox.width / 2) - colBox.left) : null,
      }
    })
    const deltaVsCol = geo.textCenterFromColLeft - geo.colCenter
    const deltaVsScroller = geo.scCenterFromColLeft != null ? geo.textCenterFromColLeft - geo.scCenterFromColLeft : null
    console.log(
      `[${vp.name}] textAlign=${geo.textAlign} maxWidth=${geo.maxWidth} divWidth=${geo.divWidth} ` +
      `scWidth=${geo.scWidth} colWidth=${geo.colWidth} textLeftFromColLeft=${geo.textLeftFromColLeft} ` +
      `textCenterFromColLeft=${geo.textCenterFromColLeft} colCenter=${geo.colCenter} ` +
      `deltaVsCol=${deltaVsCol} deltaVsScroller=${deltaVsScroller}`,
    )

    const shot = `${OUT}/${LABEL}-${vp.name}-${theme}.png`
    await page.screenshot({ path: shot })
    console.log(`  wrote ${shot}`)
    await page.close()
  }
}

await browser.close()
if (failed) { console.error('CAPTURE FAILED'); process.exit(1) }
console.log('done')

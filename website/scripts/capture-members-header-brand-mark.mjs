/**
 * Screenshot harness for the Crew Members page header glyphs: the page icon is
 * the same two-ghost brand mark the nav rail draws for `/members`
 * (`components/CrewMemberMark.tsx`), and both "Add member" entries (header
 * button, empty-roster call to action) carry a bare Lucide `Plus`.
 *
 * Drives website/capture/members-page.html on a vite dev server, gateway-free:
 * GET /api/members is answered with a one-member roster (header frames) or an
 * empty one (call-to-action frames). Every frame is preceded by DOM checks —
 * the mark is present by its test id, each add entry's <svg> carries
 * `lucide-plus` and not `lucide-user-plus` — so a screenshot is only written
 * for the state it claims to show.
 *
 * Frames, per theme (dark, light), at deviceScaleFactor 2:
 *   01-header-<theme>        the roster header: mark + title + "+" button
 *   02-empty-cta-<theme>     the empty roster with its "Add member" CTA
 *   03-header-crop-<theme>   the header row alone, so the 15px glyphs are legible
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6834 --strictPort   # in another shell
 *   node scripts/capture-members-header-brand-mark.mjs http://127.0.0.1:6834 ../temp-screenshots/members-header-brand-mark
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

const BASE = process.argv[2] || 'http://127.0.0.1:6834'
const OUT = process.argv[3] || '../temp-screenshots/members-header-brand-mark'
mkdirSync(OUT, { recursive: true })

const ROSTER = { members: [
  { name: 'Oncall', slug: 'oncall', slot_key: 'member-oncall', running: false, last_active_ts: 0, source: 'kirocrew' },
], default_agent: 'kirocrew' }
const EMPTY = { members: [], default_agent: 'kirocrew' }

let failed = false
function check(name, ok, detail = '') {
  console.log(`${ok ? 'ok   ' : 'FAIL '} ${name}${detail ? ` -- ${detail}` : ''}`)
  if (!ok) failed = true
}

function stub(page, roster) {
  return page.route((u) => new URL(u).pathname.startsWith('/api/'), (route) => {
    const req = route.request()
    const path = new URL(req.url()).pathname
    if (path === '/api/members' && req.method() === 'GET') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(roster) })
    }
    if (/\/thread$/.test(path) && req.method() === 'POST') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ slot_key: 'member-oncall', slug: 'oncall', member: 'Oncall' }) })
    }
    if (/\/activity(\?|$)/.test(path)) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ slug: 'oncall', member: 'Oncall', entries: [], capped: false }) })
    }
    if (/\/webhooks(\?|$)/.test(path)) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ tokens: [] }) })
    }
    if (path === '/api/agents') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ agents: [], default_agent: 'kirocrew' }) })
    }
    const isList = /commands|skills|sessions|files|history|models|artifacts|folders|crons|jobs/.test(path)
    return route.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
  })
}

/** The add entry's glyph, pinned by Lucide's rendered class: `lucide-plus`
 *  and not `lucide-user-plus`. */
async function checkPlus(page, theme, testId) {
  const cls = await page.getByTestId(testId).locator('svg').first().getAttribute('class')
  check(`[${theme}] ${testId} draws Lucide Plus`, /\blucide-plus\b/.test(cls || '') && !/user-plus/.test(cls || ''), cls || '(no svg)')
}

async function shoot(browser, theme) {
  // Header + one-row roster.
  let page = await browser.newPage({ viewport: { width: 1280, height: 820 }, deviceScaleFactor: 2 })
  await stub(page, ROSTER)
  await page.goto(`${BASE}/capture/members-page.html?theme=${theme}`)
  await page.waitForSelector('[data-capture-root]')
  const roster = page.getByTestId('member-roster')
  await roster.getByText('Oncall').first().waitFor({ state: 'visible', timeout: 20000 })
  const mark = roster.getByTestId('crew-member-mark')
  check(`[${theme}] the header draws the two-ghost brand mark`, (await mark.count()) === 1)
  // The mark is a CSS mask over currentColor; it tints with the header's
  // muted ink, so it must have resolved a mask image (else it is invisible).
  const masked = await mark.evaluate((el) => {
    const cs = getComputedStyle(el)
    const img = cs.maskImage || cs.webkitMaskImage
    return img && img !== 'none' && parseFloat(cs.width) > 0
  })
  check(`[${theme}] the mark resolved its mask and has a box`, masked)
  check(`[${theme}] no Lucide Users glyph remains in the header`, (await roster.locator('svg.lucide-users').count()) === 0)
  await checkPlus(page, theme, 'member-add')
  await page.waitForTimeout(300)
  await page.screenshot({ path: join(OUT, `01-header-${theme}.png`) })
  const headerBox = await roster.locator('h1').first().evaluate((h) => {
    const r = h.parentElement.parentElement.getBoundingClientRect()
    return { x: r.x, y: r.y, width: r.width, height: r.height }
  })
  await page.screenshot({ path: join(OUT, `03-header-crop-${theme}.png`), clip: headerBox })
  await page.close()

  // Empty roster: the call to action.
  page = await browser.newPage({ viewport: { width: 1280, height: 820 }, deviceScaleFactor: 2 })
  await stub(page, EMPTY)
  await page.goto(`${BASE}/capture/members-page.html?theme=${theme}`)
  await page.waitForSelector('[data-capture-root]')
  await page.getByTestId('member-empty-cta').waitFor({ state: 'visible', timeout: 20000 })
  await checkPlus(page, theme, 'member-empty-cta')
  await checkPlus(page, theme, 'member-add')
  check(`[${theme}/empty] the header still draws the brand mark`, (await page.getByTestId('member-roster').getByTestId('crew-member-mark').count()) === 1)
  await page.waitForTimeout(300)
  await page.screenshot({ path: join(OUT, `02-empty-cta-${theme}.png`) })
  await page.close()
}

const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined })
try {
  for (const theme of ['dark', 'light']) await shoot(browser, theme)
} finally {
  await browser.close()
}
if (failed) { console.error('CAPTURE FAILED'); process.exit(1) }
console.log('wrote', OUT)

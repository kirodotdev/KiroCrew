/**
 * Screenshots for ChatPane's shared transcript scroll chrome, captured on the
 * Crew Members page (the widest ChatPane host and the surface the fix was
 * reported against). Every frame asserts its state before writing:
 *   01-bottom-dark   long thread auto-pinned to the bottom on open (follow),
 *                    top edge fade present under the header
 *   02-pill-dark     scrolled up: jump-to-bottom pill visible, bottom fade
 *                    overlays the scroller above the composer
 *   03-jumped-dark   pill click landed back at the true bottom, pill gone
 *   04-pill-light    light theme parity of the scrolled-up state
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6831 --strictPort   # in another shell
 *   node scripts/capture-chatpane-scroll-chrome.mjs http://127.0.0.1:6831 ../temp-screenshots/chatpane-scroll-chrome
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6831'
const OUT = process.argv[3] || '../temp-screenshots/chatpane-scroll-chrome'
mkdirSync(OUT, { recursive: true })

const MEMBERS = [
  { name: 'radar', slug: 'radar', bound: true, slot_key: 'member-radar', running: true, kiro_agent: 'kirocrew-autofix', workspace: 'autofix', memory_store: 'default', model: '', last_active_ts: 1000, last_message: 'Six new issues: four covered by open PRs.' },
  { name: 'fixer', slug: 'fixer', bound: true, slot_key: 'member-fixer', running: false, kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', model: '', last_active_ts: 900, last_message: 'Two PRs opened for the queue.' },
]

// A transcript tall enough to scroll at 820px: alternating turns with a few
// multi-line answers, so the fades and the pill have real content to prove
// themselves against.
const LONG_THREAD = Array.from({ length: 18 }, (_, i) => ([
  { role: 'user', content: `Status check #${i + 1}: anything new in the queue?`, ts: `2026-08-27T01:${String(10 + i).padStart(2, '0')}:00Z` },
  { role: 'assistant', content: `Sweep ${i + 1} done.\n\n- two issues triaged as duplicates\n- one PR moved to review-ready\n- CI green on the retry`, ts: `2026-08-27T01:${String(10 + i).padStart(2, '0')}:30Z` },
])).flat()

const browser = await chromium.launch()
let failed = false

function check(name, ok, detail) {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
  return ok
}

const SCROLLER = '[data-chat-pane] .chat-container'

async function geom(page) {
  return page.evaluate((sel) => {
    const el = document.querySelector(sel)
    if (!el) return null
    return { top: el.scrollTop, height: el.scrollHeight, client: el.clientHeight }
  }, SCROLLER)
}

async function newPage(theme, viewport = { width: 1280, height: 820 }) {
  const page = await browser.newPage({ viewport, deviceScaleFactor: 1 })
  await page.route(u => new URL(u).pathname.startsWith('/api/'), route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/members') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ members: MEMBERS, default_agent: 'kirocrew' }) })
    }
    const thread = path.match(/^\/api\/members\/([^/]+)\/thread$/)
    if (thread) {
      const slug = decodeURIComponent(thread[1])
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ slot_key: `member-${slug}`, slug, member: slug, created: false }) })
    }
    if (/^\/api\/members\/[^/]+\/activity$/.test(path)) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ slug: 'radar', member: 'radar', capped: false, entries: [] }) })
    }
    if (/^\/api\/chat\/slots\/[^/]+$/.test(path)) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ key: 'member-radar', title: 'radar', running: false, messages: LONG_THREAD }) })
    }
    if (path === '/api/crons') return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ jobs: [] }) })
    if (path === '/api/webhooks') return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ tokens: [] }) })
    if (path === '/api/agents') return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ agents: [], default_agent: 'kirocrew' }) })
    const isList = /commands|skills|agents|sessions|files|history|models|artifacts|folders/.test(path)
    return route.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
  })
  await page.goto(`${BASE}/capture/members-page.html?theme=${theme}`)
  await page.waitForSelector('[data-capture-root]')
  await page.getByText('radar', { exact: true }).first().click()
  await page.getByText('Status check #18: anything new in the queue?').waitFor()
  // Give the RO-driven initial pin a frame to land before reading geometry.
  await page.waitForTimeout(150)
  return page
}

// 01 — long thread auto-pinned to the bottom on open (dark)
{
  const page = await newPage('dark')
  const g = await geom(page)
  check('01 scroller present', !!g, JSON.stringify(g))
  check('01 auto-pinned to bottom', !!g && g.height > g.client && Math.abs(g.top - (g.height - g.client)) <= 2,
    `top=${g?.top} bottom=${g ? g.height - g.client : '-'} (content ${g?.height}px in ${g?.client}px viewport)`)
  const fades = await page.evaluate(() => ({
    top: !!document.querySelector('[data-chat-pane] .bg-gradient-to-b.from-bg'),
    bottom: !!document.querySelector('[data-chat-pane] .bg-gradient-to-t.from-bg'),
  }))
  check('01 edge fades mounted', fades.top && fades.bottom, JSON.stringify(fades))
  const pill = await page.getByLabel('Scroll to bottom').count()
  check('01 no pill at the bottom', pill === 0, `pills=${pill}`)
  await page.screenshot({ path: `${OUT}/01-bottom-dark.png` })
  await page.close()
}

// 02 — scrolled up: pill appears (dark)
{
  const page = await newPage('dark')
  await page.evaluate((sel) => {
    const el = document.querySelector(sel)
    el.scrollTop = Math.max(0, el.scrollTop - 500)
    el.dispatchEvent(new Event('scroll'))
  }, SCROLLER)
  await page.getByLabel('Scroll to bottom').waitFor()
  check('02 pill visible after scroll-up', true, 'jump-to-bottom rendered')
  await page.screenshot({ path: `${OUT}/02-pill-dark.png` })

  // 03 — pill click lands back at the true bottom and the pill leaves.
  await page.getByLabel('Scroll to bottom').click()
  await page.waitForTimeout(100)
  const g = await geom(page)
  check('03 jumped to bottom', !!g && Math.abs(g.top - (g.height - g.client)) <= 2, `top=${g?.top} bottom=${g ? g.height - g.client : '-'}`)
  const pillGone = await page.getByLabel('Scroll to bottom').count()
  check('03 pill gone at bottom', pillGone === 0, `pills=${pillGone}`)
  await page.screenshot({ path: `${OUT}/03-jumped-dark.png` })
  await page.close()
}

// 04 — light theme parity of the scrolled-up state
{
  const page = await newPage('light')
  await page.evaluate((sel) => {
    const el = document.querySelector(sel)
    el.scrollTop = Math.max(0, el.scrollTop - 500)
    el.dispatchEvent(new Event('scroll'))
  }, SCROLLER)
  await page.getByLabel('Scroll to bottom').waitFor()
  await page.screenshot({ path: `${OUT}/04-pill-light.png` })
  await page.close()
}

await browser.close()
if (failed) {
  console.error('CAPTURE FAILED: at least one frame did not match its asserted state')
  process.exit(1)
}
console.log('all frames verified')

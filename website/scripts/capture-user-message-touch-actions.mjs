/**
 * Screenshot harness for PR #2016: user-message action buttons on touch
 * devices (hover:none) become always-visible 40px touch targets.
 *
 * Runs the REAL SPA with every /api/** call and the /api/ws websocket
 * intercepted by Playwright and answered from fixtures — no gateway. Two
 * captures:
 *
 *   1. mobile-touch-40px: 390x844 viewport with `(hover: none)` +
 *      `(pointer: coarse)` emulated via CDP. The action footer is visible
 *      without any hover, and each button is measured at >= 40px square
 *      (20px icon + 10px padding per side). Measurements are asserted in
 *      the script and burned into the capture log.
 *   2. desktop-hover-unchanged: 1440x900 hover-capable viewport. The footer
 *      stays reveal-on-hover and the icons stay compact (14px), proving no
 *      desktop regression.
 *
 * Usage: node scripts/capture-user-message-touch-actions.mjs <baseUrl> <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:3000'
const OUT = process.argv[3] || '../temp-screenshots/user-message-touch-actions'

mkdirSync(OUT, { recursive: true })

const now = Date.now() / 1000
const messages = [
  { role: 'user', ts: now - 120, content: 'Can you summarize the deployment checklist for the new release?' },
  { role: 'assistant', ts: now - 60, content: 'Sure — the checklist has three phases: build verification, staged rollout, and post-deploy monitoring. Each phase gates the next.' },
  { role: 'user', ts: now - 30, content: 'Great, start with phase one please.' },
]

const slots = [{
  key: 'demo-session', title: 'Deployment checklist', running: false,
  last_message: 'Great, start with phase one please.', messages: 3,
  agent: 'kirocrew', memory_mode: 'persistent', modified: Math.floor(now),
}]
const details = { running: false, has_more: false, total: 3, queue: [], messages }

const json = (route, body) => route.fulfill({
  status: 200, contentType: 'application/json', body: JSON.stringify(body),
})

// Fixture answers, table-driven: exact path → payload. Anything not listed
// falls through to the objectish/array heuristic at the bottom.
const FIXTURES = {
  '/api/chat/slots': slots,
  '/api/kiro-prerequisite': {
    platform: 'linux', installed: true, authenticated: true, ready: true,
    initial_setup_complete: true, can_auto_install: false, can_login: false,
    repair_required: false, docs_url: '', setup_allowed: false,
    operation: { kind: '', status: 'idle', message: '', detail: '', url: '', error: '' },
  },
  '/api/status': { sessions: 1, crons: 0, lessons: 0, uptime: 120, version: 'dev' },
  '/api/notifications': { notifications: [], unread: 0 },
  '/api/auth/me': { user: 'owner', app: '' },
  '/api/dashboard/branding': { bot_name: 'Kiro', avatar: '' },
  '/api/models': { models: [], default: 'auto' },
  '/api/themes': { themes: [], installed: [] },
  '/api/theme/boot': { mode: 'dark', theme: '' },
  '/api/chat/nav/resolve-links': { summaries: [] },
}

async function preparePage(context, { touch }) {
  const page = await context.newPage()
  await page.routeWebSocket(/\/api\/ws/, () => {})
  await page.route(url => url.pathname.startsWith('/api/'), async route => {
    const path = new URL(route.request().url()).pathname
    if (path in FIXTURES) return json(route, FIXTURES[path])
    if (/^\/api\/chat\/slots\/[^/]+/.test(path)) return json(route, details)
    if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
    const objectish = /(config|tips|voice|autonudge|branding|status|usage-summary)/.test(path)
    return json(route, objectish ? {} : [])
  })
  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 200)))
  await page.addInitScript(() => {
    localStorage.setItem('mc-theme', 'dark')
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-active-slot', 'demo-session')
  })
  if (touch) {
    const cdp = await context.newCDPSession(page)
    await cdp.send('Emulation.setEmulatedMedia', {
      features: [
        { name: 'hover', value: 'none' },
        { name: 'any-hover', value: 'none' },
        { name: 'pointer', value: 'coarse' },
        { name: 'any-pointer', value: 'coarse' },
      ],
    })
  }
  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(3000)
  return page
}

// This PR changes the USER message footer specifically (AssistantMessage
// already carries the same hover:none overrides on main), so anchor the
// measurement to the message root that contains the user's text.
const USER_TEXT = 'Great, start with phase one please.'
const measureFooter = page => page.evaluate(text => {
  const all = [...document.querySelectorAll('main *')]
  const holder = all.filter(e => e.childElementCount === 0 && e.textContent.trim() === text).pop()
  if (!holder) return null
  // Walk up to the message group root, then find its action footer row.
  let root = holder
  while (root && !(root.className && String(root.className).includes('group/msg'))) root = root.parentElement
  if (!root) return null
  const btns = [...root.querySelectorAll('button[aria-label]')].filter(b => b.querySelector('svg'))
  const rows = new Map()
  for (const b of btns) {
    const row = b.parentElement
    if (!rows.has(row)) rows.set(row, [])
    rows.get(row).push(b)
  }
  let best = null
  for (const [row, list] of rows) if (!best || list.length > best.list.length) best = { row, list }
  if (!best) return null
  const st = getComputedStyle(best.row)
  return {
    rowOpacity: st.opacity,
    buttons: best.list.map(b => {
      const r = b.getBoundingClientRect()
      const svg = b.querySelector('svg')?.getBoundingClientRect()
      return {
        label: b.getAttribute('aria-label'),
        w: Math.round(r.width), h: Math.round(r.height),
        icon: svg ? Math.round(svg.width) : null,
      }
    }),
  }
}, USER_TEXT)

async function main() {
  const browser = await chromium.launch()

  // ---- 1. Mobile touch viewport (hover: none) ----
  const mobileCtx = await browser.newContext({
    viewport: { width: 390, height: 844 }, deviceScaleFactor: 2,
    hasTouch: true, isMobile: true,
  })
  const mobile = await preparePage(mobileCtx, { touch: true })
  const hoverNone = await mobile.evaluate(() => matchMedia('(hover: none)').matches)
  console.log('mobile (hover: none) matches:', hoverNone)
  const mMeasure = await measureFooter(mobile)
  console.log('mobile footer:', JSON.stringify(mMeasure))
  if (!mMeasure || mMeasure.rowOpacity !== '1') throw new Error('mobile footer not visible without hover')
  for (const b of mMeasure.buttons) {
    if (b.w < 40 || b.h < 40) throw new Error(`touch target below 40px: ${JSON.stringify(b)}`)
    if (b.icon !== 20) throw new Error(`mobile icon not 20px: ${JSON.stringify(b)}`)
  }
  await mobile.screenshot({ path: `${OUT}/1-mobile-touch-40px-targets.png` })
  await mobileCtx.close()

  // ---- 2. Desktop hover viewport (no regression) ----
  const desktopCtx = await browser.newContext({
    viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2,
  })
  const desktop = await preparePage(desktopCtx, { touch: false })
  const before = await measureFooter(desktop)
  console.log('desktop footer before hover:', JSON.stringify(before))
  if (!before || before.rowOpacity === '1') throw new Error('desktop footer visible without hover (regression)')
  // Hover the user message to reveal its footer (300ms delay + 300ms fade).
  await desktop.getByText(USER_TEXT, { exact: true }).last().hover()
  await desktop.waitForTimeout(1200)
  const after = await measureFooter(desktop)
  console.log('desktop footer on hover:', JSON.stringify(after))
  if (!after) throw new Error('desktop footer not found')
  if (after.rowOpacity !== '1') throw new Error('desktop footer did not reveal on hover')
  for (const b of after.buttons) {
    if (b.icon !== 14) throw new Error(`desktop icon changed from 14px: ${JSON.stringify(b)}`)
  }
  await desktop.screenshot({ path: `${OUT}/2-desktop-hover-unchanged.png` })
  await desktopCtx.close()

  await browser.close()
  console.log('done →', OUT)
}

main().catch(err => { console.error(err); process.exit(1) })

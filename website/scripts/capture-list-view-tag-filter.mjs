/**
 * Screenshot harness: the sidebar's tag filter.
 *
 * Tags were reachable only through board view, whose columns scroll sideways on a
 * phone. This captures the filter menu's Tags section and the filtered result, so
 * "the list view can now be narrowed by tag" is a picture rather than a claim.
 *
 * Runs the REAL SPA with every /api/** call answered from fixtures (no gateway),
 * the same way scripts/capture-sage-rail-bar.mjs does.
 *
 * Three captures at 1400x900 (the width at which the sessions sidebar is
 * expanded; at phone width it collapses behind a toggle):
 *   1. before-menu — the filter menu as it is on the base branch (Filter + Sort
 *      + Folders, no Tags section). Produced by withholding the tag vocabulary,
 *      which is the same condition the base branch renders under.
 *   2. after-menu  — the same menu carrying the Tags section, one checkbox row
 *      per tag with its session count.
 *   3. after-filtered — Blocked selected: the list shows only Blocked sessions
 *      and the aggregate clear chip sits in its own row above the filter chips.
 *
 * Output filenames are the ones committed under
 * `temp-screenshots/list-view-tag-filter/`, so re-running this after a change
 * lands on the reviewed files instead of a parallel set someone copies by hand.
 *
 * Usage: node scripts/capture-list-view-tag-filter.mjs <baseUrl> <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:7824'
const OUT = process.argv[3] || '../temp-screenshots/list-view-tag-filter'

mkdirSync(OUT, { recursive: true })

const TAGS = [
  { id: 't-blocked', name: 'Blocked', color: '#e0526b', order: 0, status: true },
  { id: 't-review', name: 'In review', color: '#4f8ff7', order: 1, status: false },
  { id: 't-idea', name: 'Idea', color: '#3fb27f', order: 2, status: false },
]

const iso = minutesAgo => new Date(Date.now() - minutesAgo * 60_000).toISOString()

const SLOTS = [
  { key: 'chat-1', title: 'Tag filter for the session list', agent: 'meshclaw', running: false, messages: 24, tags: ['t-blocked'], last_ts: iso(4), created: iso(200) },
  { key: 'chat-2', title: 'Screenshot evidence gate', agent: 'meshclaw', running: false, messages: 11, tags: ['t-blocked'], last_ts: iso(18), created: iso(300) },
  { key: 'chat-3', title: 'Catalog parity for new keys', agent: 'meshclaw', running: false, messages: 7, tags: ['t-review'], last_ts: iso(40), created: iso(400) },
  { key: 'chat-4', title: 'Pseudolocale regeneration', agent: 'meshclaw', running: false, messages: 3, tags: ['t-idea'], last_ts: iso(90), created: iso(500) },
  { key: 'chat-5', title: 'Untagged scratch session', agent: 'meshclaw', running: false, messages: 2, tags: [], last_ts: iso(140), created: iso(600) },
]

const json = (route, body) => route.fulfill({
  status: 200, contentType: 'application/json', body: JSON.stringify(body),
})

/** @param withTags false reproduces the base branch: no tag vocabulary, so the
 *  filter menu has no Tags section to render. */
async function preparePage(context, { withTags }) {
  const page = await context.newPage()
  await page.routeWebSocket(/\/api\/ws/, () => {})
  await page.route(url => url.pathname.startsWith('/api/'), async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/chat/tags') return json(route, withTags ? TAGS : [])
    if (path === '/api/chat/slots') return json(route, SLOTS)
    if (path === '/api/chat/folders') return json(route, [])
    if (path === '/api/chat/tag-columns') return json(route, [])
    // Without this the app renders its "Install Kiro CLI" prerequisite gate
    // instead of the chat UI, and the sidebar never mounts at all.
    if (path === '/api/kiro-prerequisite') return json(route, {
      platform: 'linux', installed: true, authenticated: true, ready: true,
      initial_setup_complete: true, can_auto_install: false, can_login: false,
      repair_required: false, docs_url: '', setup_allowed: false,
      operation: { kind: '', status: 'idle', message: '', detail: '', url: '', error: '' },
    })
    if (path === '/api/status') return json(route, { sessions: SLOTS.length, crons: 0, lessons: 0, uptime: 120, version: 'dev' })
    if (path === '/api/auth/me') return json(route, { user: 'owner', app: '' })
    if (path === '/api/dashboard/branding') return json(route, { bot_name: 'Kiro', avatar: '' })
    if (path === '/api/theme/boot') return json(route, { mode: 'light', theme: '' })
    if (path === '/api/notifications') return json(route, { notifications: [], unread: 0 })
    if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
    if (path.startsWith('/api/apps')) return json(route, { apps: [], installed: [] })
    const objectish = /(config|tips|voice|autonudge|branding|status|usage-summary|prerequisite)/.test(path)
    return json(route, objectish ? {} : [])
  })
  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 240)))
  await page.addInitScript(() => {
    localStorage.setItem('mc-theme', 'light')
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('kc-onboarded', '1')
    localStorage.removeItem('mc-session-tag-filter')
  })
  await page.goto(`${BASE}/chat`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(3500)
  return page
}

/** Open the sidebar's sort/filter dropdown. */
async function openFilterMenu(page) {
  const trigger = page.getByLabel('Sort and filter sessions').first()
  await trigger.waitFor({ timeout: 15_000 })
  await trigger.click()
  await page.waitForTimeout(600)
}

const shot = (page, name) => page.screenshot({ path: `${OUT}/${name}.png`, fullPage: false })

// A mise-managed node exports its own `lib/node` on LD_LIBRARY_PATH inside the
// node process, and the browser child inherits it — so chromium resolves
// libstdc++ there and dies on a missing GLIBCXX/CXXABI that the system copy has.
// Point the browser at the system path; harmless when node is not mise-managed.
/** Fail the run when a frame does not contain what it is meant to evidence.
 *  Logging alone let a regressed re-run overwrite the reviewed PNGs and still
 *  exit 0, so the committed screenshots could go stale without any signal. */
function assertCount(label, actual, expected) {
  const ok = actual === expected
  console.log(`${label}: ${actual} (expect ${expected}) ${ok ? 'OK' : 'MISMATCH'}`)
  if (!ok) throw new Error(`${label}: expected ${expected}, got ${actual}`)
}

const browser = await chromium.launch({
  env: { ...process.env, LD_LIBRARY_PATH: '/usr/lib64' },
})
try {
  // 1. Base-branch menu: no tag vocabulary, so no Tags section.
  {
    const context = await browser.newContext({ viewport: { width: 1400, height: 900 }, deviceScaleFactor: 2 })
    const page = await preparePage(context, { withTags: false })
    await openFilterMenu(page)
    const hasTagRow = await page.locator('[data-testid^="tag-filter-"]').count()
    assertCount('before-menu: tag rows', hasTagRow, 0)
    await shot(page, '1-before-menu')
    await context.close()
  }

  // 2 + 3. With tags: the Tags section, then Blocked selected.
  {
    const context = await browser.newContext({ viewport: { width: 1400, height: 900 }, deviceScaleFactor: 2 })
    const page = await preparePage(context, { withTags: true })
    await openFilterMenu(page)
    const rows = await page.locator('[data-testid^="tag-filter-t-"]').count()
    assertCount('after-menu: tag rows', rows, TAGS.length)
    await shot(page, '2-after-menu')

    await page.getByTestId('tag-filter-t-blocked').click()
    await page.waitForTimeout(700)
    await page.keyboard.press('Escape')
    await page.waitForTimeout(500)
    const chips = await page.getByTestId('tag-filter-chip').count()
    const visible = await page.evaluate(() =>
      [...document.querySelectorAll('[data-testid^="slot-row-"], [data-slot-key]')].length)
    assertCount('after-filtered: aggregate chips', chips, 1)
    // Two of the five fixture sessions carry Blocked; the rest must be filtered out.
    assertCount('after-filtered: slot rows', visible, 2)
    await shot(page, '3-after-filtered')
    await context.close()
  }
} finally {
  await browser.close()
}
console.log('captures written to', OUT)

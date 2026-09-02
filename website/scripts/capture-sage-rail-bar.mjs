/**
 * Screenshot + measurement harness: Code Review Sage's rail at phone width.
 *
 * The collapsed rail becomes a bar across the top of the app while narrow. On
 * `main` that bar carries ONE icon, so from a report there is no visible route to
 * Learning or Settings and no labelled way back to the list. This captures the
 * bar and measures it, so the claim "the nav row survives the collapse and the
 * row still fits" is a measurement rather than an assertion about class names.
 *
 * Runs the REAL SPA with every /api/** call answered from fixtures (no gateway),
 * the same way scripts/capture-user-message-touch-actions.mjs does.
 *
 * Two captures:
 *   1. detail-390: 390x844, a review open in the detail pane. Asserts the bar
 *      holds a labelled back control plus all three section-nav buttons, that
 *      none of them overflows the viewport, and that each nav button clears 44px.
 *   2. list-390: the same viewport with the rail expanded (its full-width list
 *      state), asserting the identity row is the SAME row — one nav, two shapes.
 *
 * Output filenames are the ones committed under
 * `temp-screenshots/sage-rail-bar-nav/`, so re-running this after a change lands
 * on the reviewed files instead of a parallel set someone has to copy by hand —
 * that hand step is how the committed frames once drifted from HEAD. The
 * `1-before-*` frame is this same script's first capture taken against the
 * pre-fix code and renamed; it is not reproducible from this checkout.
 *
 * Usage: node scripts/capture-sage-rail-bar.mjs <baseUrl> <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:7823'
const OUT = process.argv[3] || '../temp-screenshots/sage-rail-bar'

mkdirSync(OUT, { recursive: true })

const SAGE = '/api/apps/code-review-sage'
const RUN_ID = 'run-demo-1'
const PR_URL = 'https://github.com/example-org/example-service/pull/128'
const CHANGE_ID = 'example-org_example-service_128'

const pr = {
  url: PR_URL,
  number: 128,
  title: 'Cache the resolved config instead of re-reading it per request',
  head_sha: 'a1b2c3d4e5f60718293a4b5c6d7e8f9012345678',
  author: 'example-dev',
  updated_at: new Date(Date.now() - 3600_000).toISOString(),
  draft: false,
  change_id: CHANGE_ID,
  reviewed: true,
  reviewed_stale: false,
  reviewed_at: new Date(Date.now() - 1800_000).toISOString(),
}

const run = {
  run_id: RUN_ID,
  repo: 'example-org/example-service',
  changes: [PR_URL],
  change_ids: [CHANGE_ID],
  status: 'done',
  started_at: new Date(Date.now() - 2400_000).toISOString(),
  finished_at: new Date(Date.now() - 1800_000).toISOString(),
  summary: { red: 0, yellow: 2, green: 1 },
  report_slug: null,
}

const report = {
  run_id: RUN_ID,
  status: 'done',
  ready: true,
  bands: { red: 0, yellow: 1, green: 0 },
  total: 1,
  generated_at: new Date(Date.now() - 1800_000).toISOString(),
  report_slug: null,
  rows: [{
    change_id: CHANGE_ID,
    url: PR_URL,
    title: pr.title,
    band: 'yellow',
    band_reason: 'blast=SMALL + 1x yellow',
    findings: [{
      dimension: 'Correctness',
      severity: 'yellow',
      file: 'src/config/resolve.ts',
      line: 42,
      headline: 'The cache is never invalidated when the file changes on disk.',
      observation: 'The resolved config is memoised on first read and the entry has no'
        + ' expiry or file-watch, so a later edit is not picked up.',
      consequence: 'An operator who edits the file and reloads sees the old value and'
        + ' concludes the setting does nothing.',
      suggestion: 'Key the cache on the file mtime, or drop the entry on the watcher'
        + ' event the loader already receives.',
    }],
  }],
}

const FIXTURES = {
  [`${SAGE}/runs`]: { runs: [run], pool: null, reviewer: { model: 'auto', effort: 'high' } },
  [`${SAGE}/runs/${RUN_ID}`]: { run },
  [`${SAGE}/runs/${RUN_ID}/report`]: report,
  [`${SAGE}/repos`]: { repos: [{ owner: 'example-org', repo: 'example-service' }] },
  [`${SAGE}/repo-prs`]: { repo: 'example-org/example-service', prs: [pr], count: 1 },
  [`${SAGE}/settings`]: {
    settings: { model: null, effort: 'high', active_namespaces: ['default'], max_concurrent: 2 },
    models: [], efforts: ['low', 'high'], namespaces: ['default'], reviewer: null,
  },
  [`${SAGE}/namespaces`]: { namespaces: [{ name: 'default', count: 0 }] },
  [`${SAGE}/learnings`]: { namespace: 'default', learnings: [] },
  '/api/kiro-prerequisite': {
    platform: 'linux', installed: true, authenticated: true, ready: true,
    initial_setup_complete: true, can_auto_install: false, can_login: false,
    repair_required: false, docs_url: '', setup_allowed: false,
    operation: { kind: '', status: 'idle', message: '', detail: '', url: '', error: '' },
  },
  '/api/status': { sessions: 0, crons: 0, lessons: 0, uptime: 120, version: 'dev' },
  '/api/notifications': { notifications: [], unread: 0 },
  '/api/auth/me': { user: 'owner', app: '' },
  '/api/dashboard/branding': { bot_name: 'Kiro', avatar: '' },
  '/api/models': { models: [], default: 'auto' },
  '/api/themes': { themes: [], installed: [] },
  '/api/theme/boot': { mode: 'light', theme: '' },
  '/api/chat/slots': [],
}

const json = (route, body) => route.fulfill({
  status: 200, contentType: 'application/json', body: JSON.stringify(body),
})

/** The Sage UI state the app restores on mount — this is how the harness lands
 *  directly on a review instead of driving the list first (the list is a
 *  virtualised pane, and clicking through it would make the capture depend on
 *  row geometry rather than on the bar under test). */
const uiState = {
  v: 1,
  state: {
    mainView: 'reviews',
    listTab: 'pulls',
    activeRepo: { owner: 'example-org', repo: 'example-service' },
    selectedRunId: RUN_ID,
    selectedPr: pr,
    detailTab: null,
  },
}

async function preparePage(context, { seedSelection = true } = {}) {
  const page = await context.newPage()
  await page.routeWebSocket(/\/api\/ws/, () => {})
  await page.route(url => url.pathname.startsWith('/api/'), async route => {
    const path = new URL(route.request().url()).pathname
    if (path in FIXTURES) return json(route, FIXTURES[path])
    if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
    if (path.startsWith('/api/apps')) return json(route, { apps: [], installed: [] })
    const objectish = /(config|tips|voice|autonudge|branding|status|usage-summary)/.test(path)
    return json(route, objectish ? {} : [])
  })
  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 240)))
  // Init scripts re-run on EVERY navigation, so the seeded selection cannot be
  // cleared by a later removeItem + reload — the capture that needs an unselected
  // app gets its own context with the seed withheld instead.
  await page.addInitScript(({ state, seed }) => {
    localStorage.setItem('mc-theme', 'light')
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('kc-onboarded', '1')
    if (seed) localStorage.setItem('kc:code-review-sage:ui-state', JSON.stringify(state))
  }, { state: uiState, seed: seedSelection })
  await page.goto(`${BASE}/code-review-sage`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(3500)
  return page
}

/** The bar (or the rail's own header) as measurements: which controls it holds,
 *  where each one sits, and whether any of them leaves the viewport. Found from
 *  the section nav's own landmark, so it does not depend on class names. */
const measureNav = page => page.evaluate(() => {
  const nav = document.querySelector('nav[aria-label]:not([aria-label="Main navigation"])')
  if (!nav) return null
  const row = nav.parentElement
  const bar = row?.parentElement
  const rect = el => {
    const r = el.getBoundingClientRect()
    return {
      x: Math.round(r.x), right: Math.round(r.right),
      w: Math.round(r.width), h: Math.round(r.height),
    }
  }
  const buttons = [...row.querySelectorAll('button')].map(b => ({
    label: b.getAttribute('aria-label') || b.textContent.trim(),
    inNav: nav.contains(b),
    ...rect(b),
  }))
  return {
    viewport: window.innerWidth,
    barTop: bar ? Math.round(bar.getBoundingClientRect().top) : null,
    rowWidth: Math.round(row.getBoundingClientRect().width),
    rowScrollWidth: row.scrollWidth,
    navButtons: buttons.filter(b => b.inNav).length,
    overflowing: buttons.filter(b => b.right > window.innerWidth + 1).map(b => b.label),
    buttons,
  }
})

async function main() {
  // This host's node sets LD_LIBRARY_PATH to its own bundled lib dir, and a
  // browser child inheriting it picks up that older libstdc++ instead of the
  // system one and dies before it opens a page. Clear it for the child only.
  const browser = await chromium.launch({
    env: { ...process.env, LD_LIBRARY_PATH: '' },
  })
  const context = await browser.newContext({
    viewport: { width: 390, height: 844 }, deviceScaleFactor: 2,
    hasTouch: true, isMobile: true,
  })
  const page = await preparePage(context)

  // ---- 1. Detail view: the bar is up, the report owns the viewport ----
  await page.screenshot({ path: `${OUT}/2-after-detail-bar-390.png` })
  console.log('navs on page:', JSON.stringify(await page.evaluate(() => [
    ...document.querySelectorAll('nav')].map(n => n.getAttribute('aria-label')))))
  const bar = await measureNav(page)
  console.log('bar:', JSON.stringify(bar, null, 1))
  if (!bar) throw new Error('no section nav found on the collapsed bar')
  if (bar.navButtons !== 3) throw new Error(`expected 3 section-nav buttons, got ${bar.navButtons}`)
  if (bar.overflowing.length) {
    throw new Error(`controls past the viewport edge: ${bar.overflowing.join(', ')}`)
  }
  if (bar.rowScrollWidth > bar.rowWidth + 1) {
    throw new Error(`bar row overflows: ${bar.rowScrollWidth} > ${bar.rowWidth}`)
  }
  const back = bar.buttons.find(b => !b.inNav && b.label && !/sidebar/i.test(b.label))
  if (!back) throw new Error('no back-to-list control on the bar')
  if (back.h < 44) throw new Error(`back control is ${back.h}px tall, below the 44px touch target`)
  for (const b of bar.buttons.filter(x => x.inNav)) {
    if (b.h < 44 || b.w < 44) throw new Error(`nav button below 44px: ${JSON.stringify(b)}`)
  }

  // ---- 2. Back to the list: the same nav, in the rail's own shape ----
  await page.getByRole('button', { name: back.label }).first().click()
  await page.waitForTimeout(600)
  const rail = await measureNav(page)
  console.log('rail:', JSON.stringify(rail, null, 1))
  if (!rail || rail.navButtons !== 3) throw new Error('the nav did not survive reopening the rail')
  if (rail.overflowing.length) {
    throw new Error(`rail header overflows: ${rail.overflowing.join(', ')}`)
  }
  // The rail is full-screen while narrow, so its close control is the one way
  // out of it and is sized for touch rather than for the desktop rail.
  const close = rail.buttons.find(b => /sidebar/i.test(b.label || ''))
  if (!close) throw new Error('no collapse control on the full-width rail')
  if (close.h < 44 || close.w < 44) {
    throw new Error(`collapse control below 44px: ${JSON.stringify(close)}`)
  }
  await page.screenshot({ path: `${OUT}/3-after-list-rail-390.png` })

  // ---- 3. Nothing selected: the empty state names a list the bar hides ----
  // The app's first-run mobile screen. Its copy asks the user to select from a
  // list that is on screen on a desktop and behind the bar here, so it carries
  // the same back-to-list control.
  const emptyCtx = await browser.newContext({
    viewport: { width: 390, height: 844 }, deviceScaleFactor: 2,
    hasTouch: true, isMobile: true,
  })
  const emptyPage = await preparePage(emptyCtx, { seedSelection: false })
  const empty = await measureNav(emptyPage)
  if (!empty || empty.navButtons !== 3) throw new Error('the bar lost its nav on the empty state')
  const routes = await emptyPage.evaluate(() => {
    const main = document.querySelector('main')
    if (!main) return null
    const btns = [...main.querySelectorAll('button')]
      .map(b => (b.textContent || '').trim()).filter(Boolean)
    return { mainButtons: btns }
  })
  console.log('empty state:', JSON.stringify(routes))
  if (!routes || routes.mainButtons.length === 0) {
    throw new Error('empty state offers no control at all — the list is unreachable from it')
  }
  await emptyPage.screenshot({ path: `${OUT}/4-after-empty-state-390.png` })
  await emptyCtx.close()

  await context.close()
  await browser.close()
  console.log('done →', OUT)
}

main().catch(err => { console.error(err); process.exit(1) })

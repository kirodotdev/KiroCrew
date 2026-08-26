/**
 * Screenshot harness: the onboarding importer's "detected but unreadable" states.
 *
 * Making the scan degrade instead of fail (a crashing reader now costs one source
 * rather than the whole import page) created two surfaces a diff cannot show, and
 * one of them was a regression I had to fix: with zero readable sources the flow
 * used to auto-complete and close, so a reader bug silently skipped first-run
 * import entirely. These frames are the evidence that the failure is now visible
 * and has a way out.
 *
 * Runs the REAL SPA with every /api/** call answered from fixtures (no gateway),
 * the same way scripts/capture-list-view-tag-filter.mjs does.
 *
 * Two captures at 1100x820:
 *   1. sole-source-unreadable — the only detected agent could not be read. Names
 *      the source, and offers BOTH "Try again" and "Skip import": retry alone
 *      dead-ends, because `source_unreadable` is a persistent reader failure and
 *      this state suppresses auto-complete, so the dialog would re-offer forever.
 *   2. partial-unreadable-stage1 — one readable agent (Codex) plus one unreadable
 *      (Claude Code). The picker still works, and the unreadable source is named
 *      inline instead of silently vanishing from the one screen where the user
 *      decides what to import.
 *
 * Output filenames are the ones committed under
 * `temp-screenshots/onboarding-import-unreadable/`, so re-running this after a
 * change lands on the reviewed files instead of a parallel set.
 *
 * Usage: node scripts/capture-onboarding-import-unreadable.mjs <baseUrl> <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { handleBootRoute } from './lib/boot-api.mjs'

const BASE = process.argv[2] || 'http://127.0.0.1:7824'
const OUT = process.argv[3] || '../temp-screenshots/onboarding-import-unreadable'

mkdirSync(OUT, { recursive: true })

/** Nothing readable: the reader for the only detected agent raised. */
const SOLE_UNREADABLE = {
  sources: [{ id: 'claude_code', name: 'Claude Code', detected: false, categories: [] }],
  skipped: [{ source: 'Claude Code', category: '', reason: 'source_unreadable' }],
  merge_only: true,
}

/** One readable, one not — the case where the failure used to leave no trace. */
const PARTIAL_UNREADABLE = {
  sources: [
    {
      id: 'codex',
      name: 'Codex',
      detected: true,
      categories: [
        { id: 'instructions', label: 'Instructions', count: 2 },
        { id: 'mcp_servers', label: 'MCP servers', count: 3 },
      ],
    },
  ],
  skipped: [{ source: 'Claude Code', category: '', reason: 'source_unreadable' }],
  merge_only: true,
}

const json = (route, body) => route.fulfill({
  status: 200, contentType: 'application/json', body: JSON.stringify(body),
})

async function preparePage(context, scan) {
  const page = await context.newPage()
  await page.routeWebSocket(/\/api\/ws/, () => {})
  await page.route(url => url.pathname.startsWith('/api/'), async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/onboarding/import/scan') return json(route, scan)
    // The importer only mounts while onboarding is unfinished.
    if (path === '/api/onboarding/import/state') return json(route, { completed: false })
    // A non-empty session list on purpose: with zero slots the sidebar's
    // reveal-flash path dereferences null and the app's error boundary replaces
    // the whole UI, taking the importer with it.
    if (path === '/api/chat/slots') return json(route, [{
      key: 'chat-1',
      title: 'First run',
      agent: 'kirocrew',
      running: false,
      messages: 0,
      tags: [],
      last_ts: new Date().toISOString(),
      created: new Date().toISOString(),
    }])
    if (path === '/api/chat/tags') return json(route, [])
    if (path === '/api/chat/folders') return json(route, [])
    if (path.startsWith('/api/apps')) return json(route, { apps: [], installed: [] })
    // Everything else — including the prerequisite gate, without which the app
    // renders "Install Kiro CLI" instead of the chat UI and the importer never
    // mounts — comes from the shared boot arm.
    return handleBootRoute(route, path, { project: '/tmp/demo', theme: 'light' })
  })
  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 240)))
  await page.addInitScript(() => {
    localStorage.setItem('mc-theme', 'light')
    // Deliberately NOT setting `mc-onboarded` / `mc-import-onboarded`: the theme
    // hook seeds `importOnboarded` from EITHER flag, so setting them marks first
    // run finished and the importer never opens. With both clear, App opens the
    // import flow first and the tour only after it completes — which is the real
    // first-run order and the surface under test.
    localStorage.removeItem('mc-onboarded')
    localStorage.removeItem('mc-import-onboarded')
  })
  await page.goto(`${BASE}/chat`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(3500)
  return page
}

const shot = (page, name) => page.screenshot({ path: `${OUT}/${name}.png`, fullPage: false })

/** Fail the run when a frame does not contain what it is meant to evidence.
 *  Logging alone would let a regressed re-run overwrite the reviewed PNGs and
 *  still exit 0, so the committed screenshots could go stale with no signal.
 *
 *  `count() > 0` is NOT sufficient on its own — a dev-server error overlay sits
 *  ON TOP of a fully-rendered page, so every locator still matches while the
 *  frame is unusable. `assertNoOverlay` below is the other half of the check. */
async function assertVisible(page, label, locator) {
  const count = await locator.count()
  console.log(`${label}: ${count} match(es) ${count > 0 ? 'OK' : 'MISSING'}`)
  if (count === 0) throw new Error(`${label}: expected the frame to show this, found nothing`)
}

/** Refuse to screenshot a frame the dev server has painted an error over.
 *
 *  Vite renders unresolved imports as a full-bleed `<vite-error-overlay>`, which
 *  is invisible to text locators but covers the UI in the PNG. A worktree whose
 *  `node_modules` predates a dependency `main` has since added hits this on an
 *  import unrelated to this surface, and without this guard the run happily
 *  overwrote both reviewed frames with pictures of a stack trace. */
async function assertNoOverlay(page) {
  const overlay = await page.locator('vite-error-overlay').count()
  if (overlay > 0) {
    const text = await page.locator('vite-error-overlay').innerText().catch(() => '')
    throw new Error(
      'dev-server error overlay is covering the page, so the frame would be '
      + `unusable. Fix the environment first (usually a stale node_modules):\n${text.slice(0, 400)}`,
    )
  }
}

// A mise-managed node exports its own `lib/node` on LD_LIBRARY_PATH inside the
// node process, and the browser child inherits it — so chromium resolves
// libstdc++ there and dies on a missing GLIBCXX/CXXABI that the system copy has.
// Point the browser at the system path; harmless when node is not mise-managed.
const browser = await chromium.launch({
  env: { ...process.env, LD_LIBRARY_PATH: '/usr/lib64' },
})
try {
  // 1. Nothing readable. Must NOT say "no supported setup found", and must offer
  //    an exit that is not "forfeit the whole tour".
  {
    const context = await browser.newContext({ viewport: { width: 1100, height: 820 }, deviceScaleFactor: 2 })
    const page = await preparePage(context, SOLE_UNREADABLE)
    await assertNoOverlay(page)
    await assertVisible(page, 'sole: names the source', page.getByText(/could not read the files/i))
    await assertVisible(page, 'sole: retry offered', page.getByRole('button', { name: /try again/i }))
    await assertVisible(page, 'sole: escape offered', page.getByRole('button', { name: /skip import/i }))
    await shot(page, '1-sole-source-unreadable')
    await context.close()
  }

  // 2. Partial failure. The picker still works AND the unreadable source is named.
  {
    const context = await browser.newContext({ viewport: { width: 1100, height: 820 }, deviceScaleFactor: 2 })
    const page = await preparePage(context, PARTIAL_UNREADABLE)
    await assertNoOverlay(page)
    await assertVisible(page, 'partial: unreadable named at stage 1', page.getByText(/could not read Claude Code/i))
    await assertVisible(page, 'partial: readable source still offered', page.getByText('Codex'))
    await shot(page, '2-partial-unreadable-stage1')
    await context.close()
  }
  console.log(`\nwrote 2 frames to ${OUT}`)
} finally {
  await browser.close()
}

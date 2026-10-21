/**
 * Screenshot harness for the offline gate on session rename (#10920).
 *
 * Serves the REAL built SPA (website/dist) against the shared fixture router,
 * so no gateway and no credential are involved. The gateway link is the only
 * thing staged, through the one seam that decides it: `dashboard.connected`
 * flips when the `/api/ws` socket opens (useWebSocket.ts). `stubDashboardApi`
 * installs a no-op socket route, which the client sees as OPEN, so a scene
 * registers its own route AFTER it and closes the socket to go offline.
 *
 * Four scenes cover both hosts and both sinks the gate guards:
 *
 * - `sidebar-offline`: double-click a row title offline. The row's own neutral
 *   notice appears under the title it refused.
 * - `header-offline`: click the chat header title offline. The notice appears
 *   beside the title instead of an editor opening.
 * - `header-draft-kept`: open the header editor WHILE CONNECTED, type a name,
 *   drop the socket, then commit. The editor and the typed draft survive, which
 *   is the draft-loss path the commit re-check closes.
 * - `sidebar-connected`: the control. Connected, the same double-click opens the
 *   editor and no notice renders, so the gate is not simply always-on.
 *
 * A `title` tooltip is OS chrome that Chromium never paints into a frame, so
 * each scene asserts its contract beside the shot: the notice text, and that
 * the element is inside the viewport rather than merely in the DOM.
 *
 * Usage, from website/: node scripts/capture-10920-offline-rename.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/10920-offline-rename'
const SLOT = 'chat-release-notes'
const NOTICE = 'Gateway offline — reconnect to rename sessions'
const VIEWPORT = { width: 1500, height: 900 }

// A mise-managed node exports its own lib/node on LD_LIBRARY_PATH, whose
// libstdc++ predates the one Chromium's bundled libs need, so the browser
// fails to launch at all. Same strip as the sibling harnesses.
delete process.env.LD_LIBRARY_PATH

mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)
const slots = [
  {
    key: SLOT, title: 'Draft the release notes', running: false, messages: 2,
    agent: 'kirocrew', modified: now, last_ts: new Date().toISOString(), folder_id: '',
    last_message: 'Noted.', source_links: [], source_links_total: 0,
  },
  {
    key: 'chat-triage', title: 'Triage the overnight pages', running: false, messages: 5,
    agent: 'kirocrew', modified: now - 600, last_ts: new Date(Date.now() - 600_000).toISOString(),
    folder_id: '', last_message: 'Two left.', source_links: [], source_links_total: 0,
  },
]

const detail = { running: false, has_more: false, total: 0, queue: [], messages: [] }
const extra = (path, route) => {
  if (path.startsWith('/api/chat/slots/')) return json(route, detail), true
  return false
}

let failures = 0
function check(label, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want)
  if (!ok) failures++
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${label}: got ${JSON.stringify(got)}${ok ? '' : ` want ${JSON.stringify(want)}`}`)
}

/** A rect wholly inside the viewport: DOM presence is not frame presence. */
const inFrame = (page, selector) => page.evaluate(sel => {
  const el = document.querySelector(sel)
  if (!el) return 'absent'
  const r = el.getBoundingClientRect()
  if (r.width === 0 || r.height === 0) return 'zero-size'
  return r.top >= 0 && r.left >= 0 && r.bottom <= window.innerHeight && r.right <= window.innerWidth
    ? 'in-frame' : 'clipped'
}, selector)

const text = (page, selector) => page.evaluate(sel => {
  const el = document.querySelector(sel)
  return el ? (el.textContent || '').replace(/\s+/g, ' ').trim() : null
}, selector)

/** Polled: the restore is deferred to a frame, so a single read can sample too early. */
async function focusedTag(page, within) {
  try {
    await page.waitForFunction(sel => {
      const el = document.activeElement
      if (!el || el === document.body) return false
      return sel ? !!el.closest(sel) : true
    }, within, { timeout: 5000 })
  } catch { /* fall through and report what it actually is */ }
  return page.evaluate(sel => {
    const el = document.activeElement
    if (!el || el === document.body) return 'body'
    return el.tagName + (sel ? (el.closest(sel) ? ' in-row' : ' elsewhere') : '')
  }, within)
}

async function load(context, { offline, ready }) {
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, { slots, theme: 'dark', extra })
  // Registered after the stub, so it is consulted first. The stub's own no-op
  // route reads as an OPEN socket, which is the connected state.
  const sock = { live: null, dropped: false }
  await page.routeWebSocket(/\/api\/ws/, ws => {
    if (offline || sock.dropped) ws.close()
    else sock.live = ws
  })
  await page.addInitScript(slot => {
    localStorage.setItem('mc-active-slot', slot)
    localStorage.setItem('mc-privacy-notice-v1', '1')
    localStorage.setItem('mc-sidebar-pinned', 'true')
    localStorage.setItem('mc-session-stale-collapse-ms', '0')
    localStorage.setItem('mc-onboarded', '1')
  }, SLOT)
  await page.goto(`${base}/chat`, { waitUntil: 'domcontentloaded' })
  await page.locator(ready || `[data-ui-location="chat.session-title"]`).first().waitFor({ state: 'visible', timeout: 20_000 })
  await page.waitForTimeout(1200)
  return { page, sock }
}

let base = ''

async function sidebarOffline(context) {
  const { page } = await load(context, { offline: true })
  const row = page.locator('.session-row').filter({ hasText: 'Draft the release notes' }).first()
  const rowTitle = row.locator('[data-session-title]').first()
  check('sidebar-offline title announces disabled', await rowTitle.getAttribute('aria-disabled'), 'true')
  // The full name stays reachable: `offlineProps` is spread BEFORE `title`, so
  // its own tooltip does not replace the only place a truncated name is shown.
  check('sidebar-offline title keeps its full-name tooltip',
    await rowTitle.getAttribute('title'), 'Draft the release notes')
  // `force`, because aria-disabled is advisory: the row's onDoubleClick still
  // fires in a real browser, where Playwright's actionability check refuses.
  await rowTitle.dblclick({ force: true })
  const notice = `[data-testid="session-rename-offline-${SLOT}"]`
  await page.locator(notice).waitFor({ state: 'visible', timeout: 10_000 })
  check('sidebar-offline notice text', await text(page, notice), NOTICE)
  check('sidebar-offline notice in frame', await inFrame(page, notice), 'in-frame')
  check('sidebar-offline opened no editor', await row.locator('textarea').count(), 0)
  // The row's action cluster is `absolute ... top-1/2` and appears on hover, so
  // the notice adding a second line puts it under those buttons. Hovered
  // explicitly, so the frame and the assertion both describe that state.
  await row.hover()
  await page.waitForTimeout(250)
  check('sidebar-offline notice clear of hover actions', await page.evaluate(sel => {
    const notice = document.querySelector(sel)
    const menu = notice?.closest('.session-row')?.querySelector('[data-ui-location="sessions.row-menu"]')
    if (!notice || !menu) return 'missing'
    // The TEXT, not the element's box: right padding moves the text and leaves
    // the padded box's own right edge where it was. A Range over the contents
    // measures the glyphs, so it reads the same whatever wraps them.
    const range = document.createRange()
    range.selectNodeContents(notice)
    const n = range.getBoundingClientRect(), m = menu.getBoundingClientRect()
    if (n.width === 0 || m.width === 0) return 'zero-size'
    if (getComputedStyle(menu.parentElement).opacity === '0') return 'actions-hidden'
    const overlaps = n.right > m.left && n.bottom > m.top && n.top < m.bottom
    return overlaps ? `overlap ${Math.round(n.right - m.left)}px` : 'clear'
  }, notice), 'clear')
  await page.screenshot({ path: `${OUT}/after-sidebar-offline.png` })
  console.log(`wrote ${OUT}/after-sidebar-offline.png`)
  await page.close()
}

async function headerOffline(context) {
  const { page } = await load(context, { offline: true })
  const trigger = page.locator('[data-ui-location="chat.session-title"]').first()
  check('header-offline trigger announces disabled', await trigger.getAttribute('aria-disabled'), 'true')
  await trigger.click({ force: true })
  const notice = '[data-testid="session-title-offline"]'
  await page.locator(notice).waitFor({ state: 'visible', timeout: 10_000 })
  check('header-offline notice text', await text(page, notice), NOTICE)
  check('header-offline notice in frame', await inFrame(page, notice), 'in-frame')
  check('header-offline opened no editor', await page.locator('input.session-header-title').count(), 0)
  await page.screenshot({ path: `${OUT}/after-header-offline.png` })
  console.log(`wrote ${OUT}/after-header-offline.png`)
  await page.close()
}

async function headerDraftKept(context) {
  const { page, sock } = await load(context, { offline: false })
  const trigger = page.locator('[data-ui-location="chat.session-title"]').first()
  check('header-draft-kept starts connected', await trigger.getAttribute('aria-disabled'), 'false')
  await trigger.click()
  const input = page.locator('input.session-header-title').first()
  await input.waitFor({ state: 'visible', timeout: 10_000 })
  await input.fill('Release notes, final pass')
  sock.dropped = true
  sock.live?.close()
  await page.waitForTimeout(1500)
  await input.press('Enter')
  const notice = '[data-testid="session-title-offline"]'
  await page.locator(notice).waitFor({ state: 'visible', timeout: 10_000 })
  check('header-draft-kept editor still open', await input.isVisible(), true)
  check('header-draft-kept draft survived', await input.inputValue(), 'Release notes, final pass')
  check('header-draft-kept notice text', await text(page, notice), NOTICE)
  check('header-draft-kept notice in frame', await inFrame(page, notice), 'in-frame')
  check('header-draft-kept focus is not on body', await focusedTag(page, null), 'INPUT')
  await page.screenshot({ path: `${OUT}/after-header-draft-kept.png` })
  console.log(`wrote ${OUT}/after-header-draft-kept.png`)
  await page.close()
}

async function sidebarDraftKept(context) {
  const { page, sock } = await load(context, { offline: false })
  // By slot attribute, not by title text: once the editor opens, the title text
  // is an input value and a `hasText` filter stops matching the row.
  const row = page.locator(`[data-slot-key="${SLOT}"]`).first()
  await row.locator('[data-session-title]').first().dblclick({ force: true })
  const editor = row.locator('textarea').first()
  await editor.waitFor({ state: 'visible', timeout: 10_000 })
  await editor.fill('Release notes, final pass')
  sock.dropped = true
  sock.live?.close()
  await page.waitForTimeout(1500)
  // Enter commits by blurring the textarea, so this is the path that drops
  // focus to body unless the refusal puts it back.
  await editor.press('Enter')
  const notice = `[data-testid="session-rename-offline-${SLOT}"]`
  await page.locator(notice).waitFor({ state: 'visible', timeout: 10_000 })
  check('sidebar-draft-kept editor still open', await editor.isVisible(), true)
  check('sidebar-draft-kept draft survived', await editor.inputValue(), 'Release notes, final pass')
  check('sidebar-draft-kept notice text', await text(page, notice), NOTICE)
  check('sidebar-draft-kept focus is not on body', await focusedTag(page, '.session-row'), 'TEXTAREA in-row')
  // A SECOND refused Enter: the refusal value is unchanged by now, so anything
  // keyed on it alone cannot fire again and this one would strand focus. Sent
  // from the keyboard, since focus is already back on the textarea and the row
  // refuses a programmatic fill while offline.
  await page.keyboard.press('Enter')
  check('sidebar-draft-kept focus survives a repeat refusal',
    await focusedTag(page, '.session-row'), 'TEXTAREA in-row')
  check('sidebar-draft-kept draft survived the repeat',
    await editor.inputValue(), 'Release notes, final pass')
  await page.screenshot({ path: `${OUT}/after-sidebar-draft-kept.png` })
  console.log(`wrote ${OUT}/after-sidebar-draft-kept.png`)
  await page.close()
}

async function headerNarrowDraftKept(browser) {
  // A separate, PHONE-SIZED context: the defect this covers only appears where
  // the notice and the editor compete for one narrow row, and jsdom computes no
  // layout at all, so the editor's width has to be measured in a real browser.
  const context = await browser.newContext({ viewport: { width: 360, height: 780 }, deviceScaleFactor: 2 })
  const { page, sock } = await load(context, { offline: false, ready: '[data-testid="mobile-topbar-title"]' })
  // The narrow header is MobileTopBar: rename lives in the session menu behind
  // the title, not on the title itself.
  await page.locator('[data-testid="mobile-topbar-title"] button').first().click({ force: true })
  await page.getByRole('menuitem', { name: /rename/i }).first().click({ force: true })
  const input = page.locator('input[class*="font-semibold"]').first()
  await input.waitFor({ state: 'visible', timeout: 10_000 })
  const before = (await input.boundingBox()).width
  await input.fill('Release notes, final pass')
  sock.dropped = true
  sock.live?.close()
  await page.waitForTimeout(1500)
  await input.press('Enter')
  const notice = '[data-testid="session-title-offline"]'
  await page.locator(notice).waitFor({ state: 'visible', timeout: 10_000 })
  const after = (await input.boundingBox()).width
  check('header-narrow notice text', await text(page, notice), NOTICE)
  check('header-narrow draft survived', await input.inputValue(), 'Release notes, final pass')
  // The contract: the notice must not take the editor's width. Border plus
  // padding alone is about 12px, which is what a collapsed input measures.
  check('header-narrow editor keeps a usable width', after >= 100, true)
  // On its own line the notice must still fit the viewport, or the reason is
  // clipped instead of squeezing the editor.
  check('header-narrow notice fits the viewport', await page.evaluate(() => {
    const el = document.querySelector('[data-testid="session-title-offline"]')
    const r = el.getBoundingClientRect()
    return r.right <= window.innerWidth + 1 ? 'fits' : `overflows by ${Math.round(r.right - window.innerWidth)}px`
  }), 'fits')
  // The banner is `absolute top-full` INSIDE the header, so it starts at the
  // header's bottom edge: any line the header cannot fit lands under it. The
  // phone topbar row grows, which is what keeps these two apart.
  check('header-narrow notice clears the offline banner', await page.evaluate(() => {
    const notice = document.querySelector('[data-testid="session-title-offline"]')
    const strip = document.querySelector('[data-testid="mobile-offline-strip"]')
    if (!notice || !strip) return 'missing'
    const n = notice.getBoundingClientRect(), s = strip.getBoundingClientRect()
    if (n.height === 0 || s.height === 0) return 'zero-size'
    const over = n.right > s.left && n.left < s.right && n.bottom > s.top && n.top < s.bottom
    return over ? `overlaps by ${Math.round(n.bottom - s.top)}px` : 'clear'
  }), 'clear')
  console.log(`  editor width ${Math.round(before)}px before, ${Math.round(after)}px with the notice showing`)
  await page.screenshot({ path: `${OUT}/after-header-narrow.png` })
  console.log(`wrote ${OUT}/after-header-narrow.png`)
  // The phone's ONLY rename entry is this menu: MobileTopBar renders it until
  // Rename fires and only then mounts the title editor, so the editor's own
  // open-time gate never sees this path. Driven here while already offline,
  // which the draft-kept run above cannot cover: it has to start connected to
  // type a draft.
  await page.keyboard.press('Escape')
  await page.locator('input[class*="font-semibold"]').waitFor({ state: 'detached', timeout: 10_000 })
  await page.locator('[data-testid="mobile-topbar-title"] button').first().click({ force: true })
  const renameItem = page.getByRole('menuitem', { name: /rename/i }).first()
  await renameItem.waitFor({ state: 'visible', timeout: 10_000 })
  check('header-narrow menu Rename is dimmed offline', await renameItem.getAttribute('aria-disabled'), 'true')
  check('header-narrow menu Rename names the reason', (await renameItem.getAttribute('title')) ?? '', NOTICE)
  await renameItem.click({ force: true })
  await page.waitForTimeout(400)
  check('header-narrow menu opens no editor while offline',
    await page.locator('input[class*="font-semibold"]').count(), 0)
  await context.close()
}

async function sidebarConnected(context) {
  const { page } = await load(context, { offline: false })
  const row = page.locator('.session-row').filter({ hasText: 'Draft the release notes' }).first()
  const rowTitle = row.locator('[data-session-title]').first()
  check('sidebar-connected title not disabled', await rowTitle.getAttribute('aria-disabled'), 'false')
  await rowTitle.dblclick({ force: true })
  const editor = row.locator('textarea').first()
  await editor.waitFor({ state: 'visible', timeout: 10_000 })
  check('sidebar-connected editor opened', await editor.isVisible(), true)
  check('sidebar-connected no notice', await page.locator('[data-testid^="session-rename-offline-"]').count(), 0)
  await page.screenshot({ path: `${OUT}/after-sidebar-connected.png` })
  console.log(`wrote ${OUT}/after-sidebar-connected.png`)
  await page.close()
}

async function main() {
  const served = await serveDist()
  base = served.base
  const browser = await chromium.launch({ executablePath: chromiumExecutable() })
  const context = await browser.newContext({ viewport: VIEWPORT, deviceScaleFactor: 2 })
  try {
    await sidebarOffline(context)
    await headerOffline(context)
    await headerDraftKept(context)
    await sidebarDraftKept(context)
    await sidebarConnected(context)
    await headerNarrowDraftKept(browser)
  } finally {
    await context.close()
    await browser.close()
    served.srv.close()
  }
  console.log(failures ? `FAILED: ${failures} assertion(s)` : 'all assertions passed')
  if (failures) process.exit(1)
}

main().catch(err => { console.error(err); process.exit(1) })

/**
 * New-chat composer on a phone: "Refresh suggestions" must not paint over the
 * composer's Send button (#mobile-refresh-send-overlap).
 *
 * The welcome hero scrolls UNDER the floating composer dock when it overflows,
 * which it does on a phone once the software keyboard takes ~40% of the height.
 * The refresh row carried `relative z-20` (to clear a hovered card that grows
 * over it on sm+), and the dock deliberately has no z-index, so that z-20 lifted
 * the row above the composer: the label and icon landed on the mic, sparkle and
 * Send controls and caught their taps.
 *
 * Real built SPA behind the shared static server, /api/** from fixtures.
 * Keyboard-open is emulated the way Chromium itself handles the page's
 * `interactive-widget=resizes-content` hint: the layout viewport shrinks by the
 * keyboard's height (336px at 390x844, 300px at 360x740, the iPhone and small
 * Android figures). The proof is a hit test, not the picture: at the centre of
 * every composer control, `elementFromPoint` must return that control, and the
 * refresh button's box must not intersect the composer box while the dock covers it.
 *
 * Usage: node scripts/capture-welcome-refresh-send.mjs [outDir] [prefix] [distDir]
 *   prefix 'before' expects the overlap (run it against a dist built from main);
 *   'after' (default) expects none.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist, DEFAULT_DIST } from './lib/serve-dist.mjs'
import { stubDashboardApi, logPageProblems, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/welcome-refresh-send'
const PREFIX = process.argv[3] || 'after'
const DIST = process.argv[4] || DEFAULT_DIST
mkdirSync(OUT, { recursive: true })

const SLOT = 'chat-welcome'
const slots = [{
  key: SLOT, title: 'New chat', running: false, last_message: '', messages: 0,
  agent: 'kirocrew', memory_mode: 'persistent', project: '', folder_id: '',
  modified: Math.floor(Date.now() / 1000), source_links: [], source_links_total: 0,
}]
const detail = { running: false, has_more: false, total: 0, queue: [], project: '', messages: [] }
const SUGGESTIONS = [
  { text: 'Review the open pull request for remaining review findings', kind: 'review' },
  { text: 'Install the CAD toolkit and test the free native key', kind: 'ops' },
  { text: "Investigate the dashboard's running-agent count", kind: 'research' },
  { text: 'Explore gentler RGB lighting for agent activity', kind: 'research' },
  { text: 'Summarize yesterday\'s on-call tickets', kind: 'tasks' },
  { text: 'Draft the design doc for the queue reorder', kind: 'write' },
]

const SCENES = [
  // [name, width, height, keyboardPx, typed]
  ['390-kbd-open-typed', 390, 844, 336, 'Test foo'],
  ['390-kbd-open-empty', 390, 844, 336, ''],
  ['390-kbd-closed-typed', 390, 844, 0, 'Test foo'],
  ['360-kbd-open-typed', 360, 740, 300, 'Test foo'],
  ['360-kbd-closed-empty', 360, 740, 0, ''],
  ['desktop-1400-typed', 1400, 900, 0, 'Test foo'],
]

/**
 * Hit-test the composer's own controls against the refresh button, after
 * scrolling the welcome hero to `pos`:
 *   'rest' -- as loaded (scrollTop 0);
 *   'send' -- the refresh row's centre on the Send button's centre, the report's
 *             state (iOS reaches it with the page scroll that reveals the focused
 *             composer; anyone reaches it with their own scroll). Clamped by the
 *             scroller, so a hero that fits cannot get there and says so;
 *   'end'  -- scrolled all the way down, where the row must sit clear of the dock.
 */
const measure = (page, pos) => page.evaluate(where => {
  const dock = document.querySelector('[data-testid="composer-dock-root"]')
  const refresh = [...document.querySelectorAll('button')].find(b => b.textContent?.trim() === 'Refresh suggestions')
  const send = dock && [...dock.querySelectorAll('button')].find(b => (b.getAttribute('aria-label') || '') === 'Send')
  const input = dock?.querySelector('textarea[data-composer-input]')
  if (!dock || !refresh || !send || !input) return { error: `missing ${!dock ? 'dock' : !refresh ? 'refresh' : !send ? 'send' : 'input'}` }
  const hero = refresh.closest('.overflow-y-auto')
  if (hero) {
    if (where === 'rest') hero.scrollTop = 0
    if (where === 'end') hero.scrollTop = hero.scrollHeight
    if (where === 'send') {
      hero.scrollTop = 0
      hero.scrollTop += (refresh.getBoundingClientRect().top + refresh.offsetHeight / 2) - (send.getBoundingClientRect().top + send.offsetHeight / 2)
    }
  }
  const r = refresh.getBoundingClientRect()
  const s = send.getBoundingClientRect()
  // The top of what the dock shows: the memory-mode chip above the composer
  // when it renders, else the composer's own card.
  const chip = dock.querySelector('[data-testid="composer-memory-chip"]')
  const card = chip || input.closest('.rounded-2xl, .rounded-xl, .rounded-3xl') || input
  const composerTop = card.getBoundingClientRect().top
  const onSend = r.left < s.right && r.right > s.left && r.top < s.bottom && r.bottom > s.top
  const misses = []
  for (const el of dock.querySelectorAll('button')) {
    const b = el.getBoundingClientRect()
    if (b.width < 2 || b.height < 2) continue
    const hit = document.elementFromPoint(b.left + b.width / 2, b.top + b.height / 2)
    if (!hit || !(el === hit || el.contains(hit))) {
      misses.push({ control: el.getAttribute('aria-label') || el.textContent?.trim().slice(0, 30) || el.tagName, hitRefresh: !!hit && refresh.contains(hit) })
    }
  }
  const rc = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)
  return {
    pos: where, scrollTop: hero ? Math.round(hero.scrollTop) : null,
    refresh: { top: Math.round(r.top), bottom: Math.round(r.bottom) },
    composerTop: Math.round(composerTop),
    onSend,
    refreshClear: r.bottom <= composerTop && !!rc && refresh.contains(rc),
    overlap: misses.some(m => m.hitRefresh),
    misses,
  }
}, pos)

async function main() {
  const { srv, base } = await serveDist(DIST)
  const browser = await chromium.launch()
  let failed = false
  for (const [name, w, h, kb, typed] of SCENES) {
    // Per scene, so one failing scene still lets every later one assert.
    let sceneFailed = false
    const bad = (msg) => { console.log(`BAD ${msg}`); sceneFailed = true; failed = true }
    const mobile = w < 640
    const context = await browser.newContext({ viewport: { width: w, height: h }, deviceScaleFactor: 2, isMobile: mobile, hasTouch: mobile })
    const page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, {
      slots,
      localStorageEntries: { 'mc-active-slot': SLOT },
      extra: async (path, route) => {
        if (path === '/api/suggestions') { await json(route, { suggestions: SUGGESTIONS, generated_at: Date.now() / 1000, stale: false }); return true }
        if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
        return false
      },
    })
    await page.goto(`${base}/chat/${SLOT}`, { waitUntil: 'domcontentloaded' })
    const input = page.locator('[data-testid="composer-dock-root"] textarea[data-composer-input]')
    await input.waitFor({ timeout: 20_000 })
    await page.getByRole('button', { name: 'Refresh suggestions' }).waitFor({ timeout: 20_000 })
    await input.focus()
    if (typed) await page.keyboard.type(typed)
    // The software keyboard: shrink the layout viewport by its height.
    if (kb) await page.setViewportSize({ width: w, height: h - kb })
    await page.waitForTimeout(900)
    const states = {}
    for (const pos of ['rest', 'send', 'end']) {
      states[pos] = await measure(page, pos)
      await page.waitForTimeout(200)
      await page.screenshot({ path: `${OUT}/${PREFIX}-${name}-${pos}.png` })
      console.log(`    ${PREFIX} ${name} ${JSON.stringify(states[pos])}`)
      if (states[pos].error) bad(`${name}: ${states[pos].error}`)
    }
    if (sceneFailed) { await context.close(); continue }
    const anyOverlap = Object.values(states).some(m => m.overlap)
    if (PREFIX === 'before') {
      // Every keyboard-open phone scene must reproduce, or it proves nothing.
      if (mobile && kb && !anyOverlap) bad(`${name}: expected the Refresh row to catch a composer tap on main`)
    } else {
      for (const m of Object.values(states)) if (m.misses.length) bad(`${name}/${m.pos}: composer controls covered: ${JSON.stringify(m.misses)}`)
      if (!states.end.refreshClear) bad(`${name}: scrolled to the end, Refresh suggestions is still under the composer`)
    }
    console.log(`${sceneFailed ? 'BAD' : 'ok '} ${PREFIX} ${name}`)
    await context.close()
  }
  await browser.close()
  srv.close()
  if (failed) { console.error(`FAIL: ${PREFIX} expectations not met`); process.exit(1) }
  console.log(`PASS: ${PREFIX}`)
}

main().catch(err => { console.error(err); process.exit(1) })

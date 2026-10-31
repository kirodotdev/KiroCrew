/**
 * Screenshot harness for #14765: a nav-arrow Back onto a session CLOSED since it
 * was visited.
 *
 * Runs the REAL built SPA (website/dist) gateway-free, with /api/** answered
 * from the shared fixtures and /api/ws bound by Playwright, and drives the
 * GENUINE flow the bug is filed on:
 *
 *   1. Boot /chat with three live sessions (Alpha, Beta, Gamma).
 *   2. Switch Alpha -> Beta -> Gamma by clicking the real session rows; each
 *      switch pushes a `?sid=` history entry, so Back retraces them.
 *   3. Close Beta: push the server's own `slot_patch` frame
 *      (`{ removed: ['<beta>'], slots: [alpha, gamma] }`), which is what sets
 *      `dashboard.durablyRemoved` — the exact signal a real close produces.
 *   4. Click the real top-left Back arrow. The POP lands on Beta's dead `?sid=`;
 *      the controller announces via `announceSlotGone` and self-heals the URL.
 *
 * The notice that renders is the REAL ChatPaneNotices `switch-slot-gone` banner
 * against the live i18n catalog and theme tokens — no HTML fixture, no
 * hand-set Redux. Captured in dark and light.
 *
 * Run once on the fix dist (after-*) and once on a base dist (before-*); the
 * pair is the before/after evidence. Reuses dist when SKIP_BUILD=1.
 *
 * Usage: node scripts/capture-14765-gone-older-sessions.mjs [outDir] [label]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { chromiumExecutable } from './lib/chromium-executable.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { installApiFixtures, logPageFailures } from './lib/api-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/14765-gone-older-sessions'
const LABEL = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

const SLOTS = [
  { key: 'chat-1-100', title: 'Alpha', messages: 2, running: false, agent: 'kirocrew', mode: '' },
  { key: 'chat-2-200', title: 'Beta', messages: 2, running: false, agent: 'kirocrew', mode: '' },
  { key: 'chat-3-300', title: 'Gamma', messages: 2, running: false, agent: 'kirocrew', mode: '' },
]
const BETA = 'chat-2-200'
const AFTER_CLOSE = SLOTS.filter(s => s.key !== BETA)

async function shootTheme(browser, base, theme) {
  const context = await browser.newContext({ viewport: { width: 1360, height: 860 }, deviceScaleFactor: 2, colorScheme: theme })
  const page = await context.newPage()
  logPageFailures(page)

  // installApiFixtures registers its own no-op /api/ws handler; register ours
  // AFTER it so Playwright's last route wins and we capture the socket.
  //
  // `/api/theme/boot` is overridden per theme: the shared fixture hardcodes
  // `mode: 'dark'`, and the SPA's boot writes that server mode into the
  // `mc-theme` localStorage key — overwriting any value an initScript seeded,
  // which is why a plain `localStorage` seed shot light pixels as dark. Driving
  // the boot mode here is the authoritative knob.
  await installApiFixtures(page, {
    '/api/chat/slots': SLOTS,
    '/api/theme/boot': { mode: theme, theme: '' },
  })
  let wsServer = null
  await page.routeWebSocket(/\/api\/ws/, ws => { wsServer = ws })

  // Per-slot detail GET is empty; the slots list comes from installApiFixtures.
  await page.route('**/api/chat/slots/**', route =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ messages: [], has_more: false, total: 0 }) }))

  await page.addInitScript(theme => {
    localStorage.clear()
    localStorage.setItem('mc-theme', theme)
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-lang', 'en')
  }, theme)

  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2200)
  // Fail loudly if the resolved theme is not what we asked for — a wrong-theme
  // capture (the #14765 UX review's "Light shows dark pixels") is a false pass.
  // `data-theme` is a `<color-theme>-<mode>` composite (e.g. "kiro-dark"), so
  // match on the mode suffix rather than the bare word.
  const applied = await page.evaluate(() => document.documentElement.dataset.theme)
  if (!applied || !applied.endsWith(`-${theme}`)) throw new Error(`theme mismatch: requested ${theme}, document resolved data-theme="${applied}"`)
  if (!wsServer) throw new Error('websocket route never bound')
  const sendPatch = frame => wsServer.send(JSON.stringify({ type: 'slot_patch', data: frame }))

  // Switch Alpha -> Beta -> Gamma via the real session rows. Each click is a
  // genuine session switch that pushes a `?sid=` history entry.
  for (const key of ['chat-1-100', BETA, 'chat-3-300']) {
    await page.locator(`[data-slot-key="${key}"]`).first().click()
    await page.waitForFunction(k => location.search.includes(`sid=${k}`), key, { timeout: 8000 })
    await page.waitForTimeout(350)
  }

  // Close Beta: the server-confirmed `removed` frame (what a real close emits).
  sendPatch({ removed: [BETA], slots: AFTER_CLOSE })
  await page.waitForTimeout(600)

  // Click the REAL Back arrow. POP lands on Beta's dead `?sid=`; the controller
  // announces the gone notice and self-heals the URL back to Gamma.
  await page.getByRole('button', { name: 'Back' }).first().click()
  await page.getByTestId('switch-slot-gone').waitFor({ state: 'visible', timeout: 8000 })
  await page.waitForTimeout(500)

  const out = `${OUT}/${LABEL}-gone-notice-${theme}.png`
  await page.screenshot({ path: out })
  // The Older Sessions footer action is the after-only affordance; log its
  // presence so a false pass (notice shown, action missing) is visible.
  const hasAction = await page.getByTestId('switch-slot-gone-older-sessions').count()
  const noticeText = (await page.getByTestId('switch-slot-gone').innerText()).replace(/\s+/g, ' ').trim()
  console.log('wrote', out, `| older-sessions action: ${hasAction} | copy: "${noticeText}"`)
  await context.close()
}

async function main() {
  if (!process.env.SKIP_BUILD) {
    console.log('building dist (SKIP_BUILD=1 to reuse)…')
    execFileSync('npm', ['run', 'build'], { stdio: 'inherit', shell: process.platform === 'win32' })
  }
  const { srv, base } = await serveDist()
  const browser = await chromium.launch({ executablePath: chromiumExecutable() })
  for (const theme of ['dark', 'light']) await shootTheme(browser, base, theme)
  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })

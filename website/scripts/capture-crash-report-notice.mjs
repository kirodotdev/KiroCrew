/**
 * Capture harness for the crash-report notice banner.
 *
 * Runs the REAL built SPA (website/dist) behind the shared gateway-free static
 * server + API stub, so the banner renders exactly as it does in production with
 * only the network replaced.
 *
 * ## Why the desktop bridge is injected rather than exercised
 *
 * The banner renders only when `window.crashReportsAPI` exists, which in the
 * product is installed by electron/preload.js. A browser-based harness has no
 * preload, so the bridge is injected with the same shape and the same two
 * methods. That is not a way around the render condition — it IS the render
 * condition: the component's only input is `get()`'s `newCount`, and what the
 * screenshots have to show is what the user sees for a given count, not how the
 * channel was wired.
 *
 * Reaching the condition for real would mean packaging the app, crashing its
 * main process and relaunching, which cannot produce a deterministic frame and
 * would put a machine-specific crash on the record instead of the banner.
 *
 * Usage: node scripts/capture-crash-report-notice.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, stubDashboardApi, logPageProblems } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/crash-report-notice'
const SLOT = 'chat-crash-notice'

mkdirSync(OUT, { recursive: true })

/**
 * One session row, because `/` lands on the chat page and an EMPTY slot list
 * error-boundaries the whole app shell out — leaving a harness that still exits 0
 * and still writes a PNG of the error boundary. The banner sits above the routes,
 * so this row is only here to keep the shell alive around it.
 */
const slots = [{
  key: SLOT,
  title: 'Why did the app close by itself?',
  running: false,
  last_message: 'Reading the crash ledger.',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: '/home/user/workspace/KiroCrew',
  folder_id: '',
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const { srv, base } = await serveDist()
const browser = await chromium.launch()

/**
 * One themed page with the bridge installed, reporting `newCount` crashes.
 *
 * A fresh context per scene rather than a reload: dismissal is in-memory and
 * per-session by design, so the dismissed frame has to come from a page that was
 * never reset, while each banner frame must start undismissed.
 */
async function openScene({ theme, newCount, viewport = { width: 1280, height: 900 }, revealOk = true }) {
  const ctx = await browser.newContext({
    viewport,
    deviceScaleFactor: 2,
    colorScheme: theme === 'light' ? 'light' : 'dark',
  })
  const page = await ctx.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    theme,
    slots,
    localStorageEntries: { 'mc-active-slot': SLOT },
    extra: async (path, route) => {
      // The LIST endpoint is the stub's; only the per-slot detail belongs here.
      if (path.startsWith('/api/chat/slots/')) {
        await json(route, {
          running: false, has_more: false, total: 0, queue: [], messages: [],
          project: '/home/user/workspace/KiroCrew',
        })
        return true
      }
      return false
    },
  })

  // Stands in for electron/preload.js. Registered as its own init script rather
  // than through `localStorageEntries` because this is a window property, not a
  // storage key, so the stub's own storage clear cannot race it. `reveal`
  // resolves without opening a file manager: the harness has no desktop, and the
  // frame is identical either way — the component discards the result.
  //
  // `get()` resolves the count and NOTHING else, matching the real summary: the
  // main process stopped sending `lastCrashAt` and `hasLog` once nothing read
  // them, and a stub that still offered them would quietly re-legitimise fields
  // the boundary no longer carries.
  //
  // `reveal` resolves `{ ok: revealOk }`: `true` for the frames where the result
  // is discarded and the frame is identical either way, `false` for the failure
  // scene, where the component surfaces the fallback through ErrorNotice and
  // that IS the delta the frame exists to show.
  await page.addInitScript(({ count, revealOk }) => {
    window.crashReportsAPI = {
      get: () => Promise.resolve({ newCount: count }),
      reveal: () => Promise.resolve({ ok: revealOk }),
    }
  }, { count: newCount, revealOk })

  await page.goto(`${base}/`, { waitUntil: 'domcontentloaded' })
  return { ctx, page }
}

const banner = page =>
  page.getByRole('status').filter({ hasText: 'closed unexpectedly' }).first()

/** Wait for the banner itself, never a bare timeout, then shoot just that region. */
async function shootBanner(page, name) {
  const el = banner(page)
  await el.waitFor({ state: 'visible', timeout: 20_000 })
  // `animate-rise` translates the element on entry; shooting mid-transform gives
  // an offset or clipped frame, so let it settle rather than racing it.
  await page.waitForTimeout(800)
  await el.screenshot({ path: `${OUT}/${name}` })
  return el
}

// Scene 1 — dark, two crashes. The count is the whole payload the renderer gets.
{
  const { ctx, page } = await openScene({ theme: 'dark', newCount: 2 })
  await shootBanner(page, 'crash-notice-dark.png')
  await ctx.close()
}

// Scene 2 — light, one crash. Same component; the warn tokens have to stay
// legible against the light canvas too.
{
  const { ctx, page } = await openScene({ theme: 'light', newCount: 1 })
  await shootBanner(page, 'crash-notice-light.png')
  await ctx.close()
}

// Scene 3 — in place, then dismissed. Shows the claim the component makes: the
// banner is a report, so dismissing it removes it for the rest of the session.
// Both frames are full-page rather than clipped: the evidence is the banner's
// position within the shell, and then the ABSENCE of that region. (A clipped
// banner here would be byte-identical to scene 1 and carry nothing new.)
{
  const { ctx, page } = await openScene({ theme: 'dark', newCount: 2 })
  const el = banner(page)
  await el.waitFor({ state: 'visible', timeout: 20_000 })
  await page.waitForTimeout(800)
  await page.screenshot({ path: `${OUT}/crash-notice-dashboard.png` })
  await el.getByRole('button', { name: /dismiss/i }).click()
  await el.waitFor({ state: 'detached', timeout: 20_000 })
  await page.screenshot({ path: `${OUT}/crash-notice-dismissed.png` })
  await ctx.close()
}

// Scenes 4 and 5 — the two widths `narrow-viewport-required` names. Both are
// captured, not just the smaller one, because the rule is about a RANGE and the
// banner changes shape between them: at 390 and 320 the reveal button has
// wrapped to its own full-width line, and at `md` it is back inline. Shooting
// only 320 would leave the claim "it also works at 390" unevidenced, and
// shooting only the desktop frame is what let the crushed-text layout ship in
// the first place. The German catalog is deliberately not used here — English is
// the shortest of the twelve, so a frame that fits at 320 in English is the
// weaker of the two claims and the layout has to hold regardless.
for (const width of [390, 320]) {
  const { ctx, page } = await openScene({
    theme: 'dark',
    newCount: 2,
    viewport: { width, height: 720 },
  })
  await shootBanner(page, `crash-notice-narrow-${width}.png`)
  await ctx.close()
}

// Scene 6 — the reveal action FAILED. The banner's one control used to swallow
// a rejection or an `{ ok: false }`, leaving a dead button and a user told to
// attach files they now cannot find. It now reports the failure through the
// shared ErrorNotice with the log's fallback path — the errors-use-error-notice
// contract. Click the control, wait for the error line, then shoot the banner
// carrying it.
{
  const { ctx, page } = await openScene({ theme: 'dark', newCount: 2, revealOk: false })
  const el = banner(page)
  await el.waitFor({ state: 'visible', timeout: 20_000 })
  await page.waitForTimeout(800)
  await el.getByRole('button', { name: /show diagnostics/i }).click()
  await page.getByTestId('crash-notice-reveal-error').waitFor({ state: 'visible', timeout: 20_000 })
  // Let the click's state update paint before shooting.
  await page.waitForTimeout(300)
  await el.screenshot({ path: `${OUT}/crash-notice-reveal-error.png` })
  await ctx.close()
}

await browser.close()
srv.close()
console.log(`wrote 7 frames to ${OUT}`)

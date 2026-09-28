/**
 * Evidence harness: a tap on the notification sheet's empty area dismisses it.
 *
 * Drives the REAL built SPA gateway-free (serveDist + stubDashboardApi) with
 * three seeded notifications, opens the sheet from the top-bar bell, and taps
 * the transparent strip BELOW the last card — the press the sheet used to
 * swallow because the strip sits inside the popover's box. Two scenes:
 *
 *   phone    390x844, touch, `isMobile` — the sheet spans the whole viewport
 *            under the top bar, so before the fix nothing but the bell closed it
 *   desktop  1280x800, mouse — the empty strip inside the 400px column, which
 *            used to differ from the identical-looking strip left of the column
 *
 * Frames per scene (prefixed with --label):
 *   01-open        sheet open, a ring marks the point about to be tapped
 *   02-after-tap   what the tap left behind — asserted, not assumed: with
 *                  `--expect closed` the sheet must be gone, with
 *                  `--expect open` it must still be there (the unfixed build)
 *   03-card-tap    a tap on a card keeps the sheet open (both builds)
 *
 * `--crash` photographs the OTHER thing the sheet can show instead: its
 * `ErrorBoundary` fallback. One malformed row (a `title` that is an object, so
 * rendering it throws) is enough to crash the feed inside the boundary, and the
 * frame proves the fallback offers the agent hand-off as well as the inbox link.
 * Both actions are asserted before the shot. This scene replaces the tap scenes
 * rather than joining them: nothing is tapped, and the sheet never opens
 * normally.
 *
 * Usage: node scripts/capture-notification-sheet-background-dismiss.mjs <outDir>
 *          [--dist <dir>] [--expect open|closed] [--label <prefix>] [--crash]
 * Run it once against the base build with `--expect open --label before` and
 * once against this branch's build with `--expect closed --label after`.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist, DEFAULT_DIST } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const argv = process.argv.slice(2)
// Declared up front so a boolean flag never swallows the token after it:
// `--crash --label after` used to set crash='--label' and drop the label, and
// `--dist` swallowed the same way silently reverted to the default build.
const VALUE_FLAGS = new Set(['dist', 'expect', 'label'])
const BOOLEAN_FLAGS = new Set(['crash'])
const flags = {}
const positional = []
for (let i = 0; i < argv.length; i++) {
  const arg = argv[i]
  if (!arg.startsWith('--')) { positional.push(arg); continue }
  const name = arg.slice(2)
  if (BOOLEAN_FLAGS.has(name)) { flags[name] = true; continue }
  if (!VALUE_FLAGS.has(name)) throw new Error(`unknown flag ${arg}`)
  const value = argv[i + 1]
  if (value === undefined || value.startsWith('--')) throw new Error(`${arg} needs a value`)
  flags[name] = value
  i++
}
const OUT = positional[0] || '../temp-screenshots/notification-sheet-background-dismiss'
const DIST = flags.dist || DEFAULT_DIST
const EXPECT = flags.expect || 'closed'
const LABEL = flags.label || EXPECT
const CRASH = 'crash' in flags
if (!['open', 'closed'].includes(EXPECT)) throw new Error(`--expect must be open|closed, got ${EXPECT}`)
mkdirSync(OUT, { recursive: true })

// Whole-run budget, checked between steps; every wait below is <= 15 s.
const DEADLINE = Date.now() + 240_000
const checkBudget = step => { if (Date.now() > DEADLINE) throw new Error(`deadline passed before ${step}`) }

// Three unread rows: enough to show the sheet is populated, few enough to
// leave an empty strip below the last card at 844px.
const NOTES = [1, 2, 3].map(i => ({
  kind: 'cron', source: 'system', channel: 'system.cron', priority: 'default',
  title: `Nightly digest ${i} ready`, body: 'Summary written to the outbox.',
  ts: `2026-09-24T0${i}:15:00.000000+00:00`, acked: false,
}))

const MATERIAL = '.notif-material, [data-notif-row], button, a, input, textarea, select, [role="button"]'

const { srv, base } = await serveDist(DIST)
const browser = await chromium.launch()

/**
 * A booted page with the seeded feed answered and the bell ready to press.
 *
 * Shared by both scenes because the boot is the fiddly part: the badge proves
 * the seeded rows reached the store before the sheet opens, and the first load
 * ends with a hop from /chat to the new slot's own URL — the popover closes
 * itself on any route change, so the bell must not be pressed until that hop
 * has landed.
 */
async function bootPage(context, notes) {
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    extra: async (path, route) => {
      if (path === '/api/notifications') { await json(route, { notifications: notes, unread: notes.length }); return true }
      // First load creates a chat slot; give it a keyed one so the palette's
      // recents provider has a key to normalize.
      if (path === '/api/chat/slots' && route.request().method() === 'POST') {
        await json(route, { key: 'chat-1', name: 'chat-1', title: 'New Session…', messages: [], running: false })
        return true
      }
      return false
    },
  })
  await page.goto(base + '/')

  const bell = page.locator('button[aria-label="Notifications"]')
  await bell.waitFor({ state: 'visible', timeout: 15000 })
  await bell.locator('span[aria-hidden="true"]').filter({ hasText: String(notes.length) }).waitFor({ timeout: 15000 })
  await page.waitForURL(url => /\/chat\/./.test(new URL(url).pathname), { timeout: 15000 })
  await page.waitForTimeout(500)
  return { page, bell }
}

async function scene(name, contextOptions, press) {
  checkBudget(`${name}: start`)
  const context = await browser.newContext(contextOptions)
  const { page, bell } = await bootPage(context, NOTES)

  const sheet = page.locator('[data-nc-phase]')
  const openSheet = async () => {
    await press.activate(bell)
    await page.locator('[data-nc-phase="open"]').waitFor({ timeout: 15000 })
    await page.locator('[data-notif-row]').nth(NOTES.length - 1).waitFor({ timeout: 15000 })
    // Let the entrance settle so the frame and the hit test see the sheet at rest.
    await page.waitForFunction(() => {
      const el = document.querySelector('[data-nc-phase="open"]')
      if (!el) return false
      const t = getComputedStyle(el).transform
      return t === 'none' || t === 'matrix(1, 0, 0, 1, 0, 0)'
    }, null, { timeout: 5000 })
  }
  await openSheet()
  checkBudget(`${name}: sheet open`)

  // The rows' scroll container is what a press below the last card lands on.
  // Found by computed overflow rather than by test id so the SAME harness runs
  // against the base build too.
  const list = await page.evaluate(() => {
    let el = document.querySelector('[data-notif-row]')
    while (el && !/auto|scroll/.test(getComputedStyle(el).overflowY)) el = el.parentElement
    if (!el) return null
    const r = el.getBoundingClientRect()
    return { x: r.x, y: r.y, width: r.width, height: r.height }
  })
  const lastRow = await page.locator('[data-notif-row]').last().boundingBox()
  if (!list || !lastRow) throw new Error(`${name}: list or last row has no box`)
  const x = Math.round(list.x + list.width / 2)
  const y = Math.round(Math.min(lastRow.y + lastRow.height + 48, list.y + list.height - 12))
  if (y <= lastRow.y + lastRow.height + 8) throw new Error(`${name}: no empty strip below the last card (y=${y})`)
  // The point must be the sheet's own background: inside the popover, on no card or control.
  const hit = await page.evaluate(([px, py, sel]) => {
    const el = document.elementFromPoint(px, py)
    return { tag: el?.tagName, inSheet: !!el?.closest('[data-nc-phase]'), onMaterial: !!el?.closest(sel) }
  }, [x, y, MATERIAL])
  if (!hit.inSheet || hit.onMaterial) throw new Error(`${name}: tap point is not the sheet background: ${JSON.stringify(hit)}`)
  console.log(`${name}: tap point (${x}, ${y}) lands on <${hit.tag}> inside the sheet, on no material`)

  // A ring at the tap point, pointer-events none so it never takes the press itself.
  await page.evaluate(([px, py]) => {
    const m = document.createElement('div')
    m.id = 'evidence-tap-marker'
    m.style.cssText = `position:fixed;left:${px - 14}px;top:${py - 14}px;width:28px;height:28px;border-radius:50%;border:3px solid #ff3b30;box-shadow:0 0 0 2px #fff;pointer-events:none;z-index:2147483647`
    document.body.appendChild(m)
  }, [x, y])
  await page.screenshot({ path: `${OUT}/${LABEL}-${name}-01-open.png` })

  // Snapshot what sits under the strip before the tap: the tap must END on
  // the sheet, so nothing beneath may change. A dismissal that fires at
  // pointerdown makes the leaving sheet pointer-transparent and the same tap's
  // click lands on the page beneath — here a suggestion chip, which fills the
  // composer.
  const composerState = () => page.evaluate(() => JSON.stringify([...document.querySelectorAll('textarea')].map(t => t.value)))
  const composerBefore = await composerState()

  await press.at(page, x, y)
  if (EXPECT === 'closed') {
    await sheet.waitFor({ state: 'detached', timeout: 5000 })
    const expanded = await bell.getAttribute('aria-expanded')
    if (expanded !== 'false') throw new Error(`${name}: sheet gone but bell reports aria-expanded=${expanded}`)
    console.log(`${name}: sheet dismissed by the background tap`)
  } else {
    await page.waitForTimeout(800)
    const phase = await sheet.getAttribute('data-nc-phase')
    if (phase !== 'open') throw new Error(`${name}: expected the unfixed sheet to stay open, phase=${phase}`)
    console.log(`${name}: sheet still open after the background tap (the reported behaviour)`)
  }
  const composerAfter = await composerState()
  if (composerAfter !== composerBefore) throw new Error(`${name}: the tap reached the page beneath — composer went from ${composerBefore} to ${composerAfter}`)
  console.log(`${name}: nothing beneath took the tap`)
  await page.screenshot({ path: `${OUT}/${LABEL}-${name}-02-after-tap.png` })
  checkBudget(`${name}: after tap`)

  // A tap on a card must keep the sheet on either build.
  await page.evaluate(() => document.getElementById('evidence-tap-marker')?.remove())
  if (EXPECT === 'closed') await openSheet()
  const firstRow = await page.locator('[data-notif-row]').first().boundingBox()
  if (!firstRow) throw new Error(`${name}: first row has no box`)
  await press.at(page, Math.round(firstRow.x + firstRow.width * 0.6), Math.round(firstRow.y + firstRow.height / 2))
  await page.waitForTimeout(800)
  const after = await sheet.getAttribute('data-nc-phase')
  if (after !== 'open') throw new Error(`${name}: a card tap must keep the sheet open, phase=${after}`)
  await page.screenshot({ path: `${OUT}/${LABEL}-${name}-03-card-tap.png` })
  console.log(`${name}: card tap kept the sheet open`)
  await context.close()
}

/**
 * The sheet's crash fallback, and that it is not a dead end.
 *
 * The malformed row below is the cheapest real render throw there is: React
 * refuses to render an object as a child, so the row that tries to print this
 * `title` throws inside the sheet's own `ErrorBoundary` (scope
 * `notifications-bell`) — the shell, the bell and its badge are all unaffected,
 * which is exactly the blast radius the boundary exists to produce.
 */
async function crashScene() {
  checkBudget('crash: start')
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 } })
  const poisoned = [{
    kind: 'cron', source: 'system', channel: 'system.cron', priority: 'default',
    title: { broken: 'a title that is not a string' }, body: 'Summary written to the outbox.',
    ts: '2026-09-24T01:15:00.000000+00:00', acked: false,
  }]
  const { page, bell } = await bootPage(context, poisoned)

  await bell.click()
  const fallback = page.locator('[data-nc-material]').filter({ hasText: 'Notifications failed to load' })
  await fallback.waitFor({ timeout: 15000 })
  // Both actions, asserted before the shot: the hand-off this round adds, and
  // the inbox link the panel already had.
  await fallback.getByRole('button', { name: 'Ask the agent' }).waitFor({ timeout: 5000 })
  // ...and the line under it that says where the press takes the reader.
  await fallback.getByText('Leaves this page and opens a new chat with this error\'s report already filled in, ready for you to send').waitFor({ timeout: 5000 })
  await fallback.getByRole('button', { name: 'Open the full inbox' }).waitFor({ timeout: 5000 })
  // The crash is contained: the bell and its badge still render beside it.
  await bell.locator('span[aria-hidden="true"]').waitFor({ timeout: 5000 })
  console.log('crash: the fallback offers the agent hand-off and the inbox link')

  await page.screenshot({ path: `${OUT}/${LABEL}-crash-01-fallback.png` })
  await context.close()
}

if (CRASH) {
  await crashScene()
} else {
  await scene('phone', { viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, hasTouch: true, isMobile: true }, {
    activate: bell => bell.tap(),
    at: (page, x, y) => page.touchscreen.tap(x, y),
  })
  await scene('desktop', { viewport: { width: 1280, height: 800 } }, {
    activate: bell => bell.click(),
    at: (page, x, y) => page.mouse.click(x, y),
  })
}

await browser.close()
srv.close()
console.log(`OK — ${LABEL} frames written to ${OUT}`)

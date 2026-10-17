/**
 * Screenshot harness for issue #21335: the goal-loop "Set a goal" popover's
 * warn banner ("This goal loop invokes the agent every cycle and can run
 * without a limit.") rendered near-black over its warn-tinted fill in the dark
 * themes -- unreadable dark-on-dark.
 *
 * What it has to prove: the banner's ink was `text-warn-fg` (= `--warn-fg`,
 * black in the dark palettes, meant for a SOLID `--warn` fill) sitting on
 * `bg-warn-subtle` (a ~12% amber wash over near-black). The fix swaps it to
 * `text-warn-text` -- the app's existing warn-hued ink for a warn-tinted fill,
 * the same token the info box two lines below already uses as `text-info-text`.
 * A contrast ratio in a table argues about the pixel; this shows it.
 *
 * It renders the REAL built SPA popover gateway-free (serveDist +
 * stubDashboardApi, no gateway/token), opens the composer's "Set a goal"
 * automation popover -- the default view, which is exactly where the warn
 * banner lives -- asserts the banner text is present, then clips to the
 * panel's bounding box so the frame is honest.
 *
 * BEFORE/AFTER from ONE build: the AFTER frame is the fix as it ships; the
 * BEFORE frame appends one stylesheet rule that re-points `text-warn-text` back
 * to `--warn-fg` under the dark mode, reproducing exactly what the banner
 * painted before this change. Same build, same component, same fixture; the
 * only difference between the pair is that one declaration. This is the same
 * call capture-warn-text-contrast.mjs / capture-scrollbar-thumb-contrast.mjs
 * made for the same "a token swap inside stylesheet rules" shape.
 *
 * Themes: the product default dark palette (where the bug bit) and the default
 * light palette (as the control -- light was already readable, and the fix must
 * keep it so).
 *
 * Gateway-free: needs `npm run build` first so website/dist exists.
 *
 * Usage: node scripts/capture-21335-goal-warn-contrast.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'
import { goalPopoverSlots, goalPopoverDetail } from './lib/goal-popover-fixture.mjs'

const OUT = process.argv[2] || '../temp-screenshots/goal-warn-contrast'
const SLOT = 'chat-monitor'
const PROJECT = '/home/user/workspace/notes'

mkdirSync(OUT, { recursive: true })

const slots = goalPopoverSlots(SLOT, PROJECT)
const detail = goalPopoverDetail(PROJECT)

/* The one declaration that reproduces the bug: before the fix the banner used
   `text-warn-fg`, so warn ink on this wash resolved to `--warn-fg` (black in
   the dark palettes). Re-pointing `--warn-text` to `--warn-fg` under the dark
   mode makes the shipped `text-warn-text` class paint what `text-warn-fg` did,
   without rebuilding or checking out an old ref. */
const BEFORE_CSS = `[data-mode="dark"]{--warn-text:var(--warn-fg)}`

const { srv, base } = await serveDist()
const browser = await chromium.launch()

const extra = async (path, route) => {
  if (path === `/api/autonudge/slot/${SLOT}`) { await json(route, { enabled: true, loop: null }); return true }
  if (path === `/api/monitors/slot/${SLOT}`) { await json(route, { enabled: true, monitor: null }); return true }
  if (path === '/api/monitors') { await json(route, { enabled: true, monitors: [] }); return true }
  if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
  return false
}

const results = []

async function shoot(name, theme, before) {
  const context = await browser.newContext({
    viewport: { width: 1500, height: 950 },
    deviceScaleFactor: 2,
    colorScheme: theme === 'light' ? 'light' : 'dark',
    locale: 'en-US',
  })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, { slots, theme, extra, localStorageEntries: { 'mc-lang': 'en' } })
  await page.addInitScript(slot => localStorage.setItem('mc-active-slot', slot), SLOT)
  /* The before frame's extra rule, appended after the built stylesheet so it
     wins on source order -- added at document-start so it is present before the
     popover paints. */
  if (before) {
    await page.addInitScript(css => {
      const s = document.createElement('style')
      s.textContent = css
      document.addEventListener('DOMContentLoaded', () => document.head.appendChild(s))
      // Also append immediately in case the head already exists.
      if (document.head) document.head.appendChild(s)
    }, BEFORE_CSS)
  }
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2500)

  const trigger = page.getByRole('button', { name: 'Set a goal' }).first()
  const found = await trigger.count().then(n => n > 0).catch(() => false)
  if (!found) {
    results.push({ name, ok: false, why: 'composer trigger "Set a goal" not found' })
    await page.screenshot({ path: `${OUT}/${name}-MISSING.png` })
    await context.close()
    return
  }
  await trigger.click()
  await page.waitForTimeout(700)

  const panel = page.locator('[data-side]').first()
  const box = await panel.boundingBox().catch(() => null)
  if (!box) {
    results.push({ name, ok: false, why: 'popover panel did not render' })
    await page.screenshot({ path: `${OUT}/${name}-MISSING.png` })
    await context.close()
    return
  }

  /* ASSERT-THEN-SHOOT: the warn banner this issue is about must be in the frame.
     Addressed by its own i18n text so the frame cannot silently photograph a
     popover that never rendered the banner. */
  const bannerText = /invokes the agent every cycle and can run without a limit/i
  const banner = page.getByText(bannerText).first()
  const bannerVisible = await banner.isVisible().catch(() => false)
  if (!bannerVisible) {
    results.push({ name, ok: false, why: 'warn banner text not visible in popover' })
    await page.screenshot({ path: `${OUT}/${name}-MISSING.png` })
    await context.close()
    return
  }
  await banner.scrollIntoViewIfNeeded().catch(() => {})

  await page.screenshot({
    path: `${OUT}/${name}.png`,
    clip: {
      x: Math.max(0, box.x - 12),
      y: Math.max(0, box.y - 12),
      width: box.width + 24,
      height: box.height + 24,
    },
  })
  console.log('wrote', `${OUT}/${name}.png`)
  results.push({ name, ok: true, bannerVisible: true })
  await context.close()
}

// dark is where the bug bit (before vs after differ); light is the control.
await shoot('01-dark-before', 'dark', true)
await shoot('02-dark-after', 'dark', false)
await shoot('03-light-before', 'light', true)
await shoot('04-light-after', 'light', false)

await browser.close()
srv.close()

console.log('--- assertions (the warn banner must be visible in every frame) ---')
for (const r of results) console.log(JSON.stringify(r))
if (!results.every(r => r.ok)) {
  console.error('FAIL: a frame did not render the warn banner')
  process.exit(1)
}
console.log('OK')

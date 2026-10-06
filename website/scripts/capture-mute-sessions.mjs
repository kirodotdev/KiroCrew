/**
 * Screenshots of the control this change adds, for Design/UX evidence:
 *
 *   kebab-unmuted / kebab-muted  -- the session kebab menu's new
 *                                   "Mute sessions this one opens" row, in both
 *                                   label states, rendered from the REAL
 *                                   SessionActionsMenu inside the REAL Radix
 *                                   DropdownMenu (capture/mute-sessions-kebab-item).
 *   kebab-error                  -- the same row with its failed-toggle
 *                                   ErrorNotice, from the REAL store error state.
 *
 * House pattern of scripts/capture-path-chips.mjs: a vite server serves the
 * isolated capture entries (the REAL components mounted against the real store,
 * theme tokens and live i18n), Playwright drives loopback, every /api answered
 * by the capture entry's own stub/seed. Gateway-free, no kiro-cli, no token.
 * The client code under test is unmodified.
 *
 * Each frame asserts the expected label text before shooting, so this can never
 * quietly emit a frame of the wrong row/state. Frames carry a blank-floor and
 * max-edge guard (same thresholds as the house harness).
 *
 * Usage (from website/):
 *   npx vite --host 127.0.0.1 --port 6815 --strictPort   # in another shell
 *   node scripts/capture-mute-sessions.mjs http://127.0.0.1:6815 ../temp-screenshots/13395
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6815'
const OUT = process.argv[3] || '../temp-screenshots/13395'
const MAX_EDGE = 2000
const MIN_MBPP = 15
mkdirSync(OUT, { recursive: true })

function pngSize(path) {
  const b = readFileSync(path)
  return { w: b.readUInt32BE(16), h: b.readUInt32BE(20) }
}

const wrote = []
function record(file, note) {
  const { w, h } = pngSize(file)
  const bytes = readFileSync(file).length
  const mbpp = Math.round((bytes * 1000) / (w * h))
  const over = w > MAX_EDGE || h > MAX_EDGE
  const blank = mbpp < MIN_MBPP
  console.log(`wrote ${file}  ${w}x${h}  ${bytes}B  ${mbpp} mB/px${over ? '  OVER' : ''}${blank ? '  BLANK' : ''}  ${note}`)
  wrote.push({ file, over, blank })
  if (blank) throw new Error(`frame ${file}: ${mbpp} mB/px below ${MIN_MBPP} blank floor`)
  if (over) throw new Error(`frame ${file}: over ${MAX_EDGE}px`)
}

const run = async () => {
  const browser = await chromium.launch(
    process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
      ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE }
      : undefined,
  )

  // ── Kebab menu row, both label states + the failed-toggle notice ────────────
  for (const theme of ['dark', 'light']) {
    for (const [scene, expect] of [
      ['unmuted', 'Mute sessions this one opens'],
      ['muted', 'Unmute sessions this one opens'],
      ['error', 'Mute sessions this one opens'],
    ]) {
      const ctx = await browser.newContext({ viewport: { width: 520, height: 640 }, deviceScaleFactor: 2, colorScheme: theme })
      const page = await ctx.newPage()
      const errors = []
      page.on('pageerror', e => errors.push(e.message))
      await page.goto(`${BASE}/capture/mute-sessions-kebab-item.html?scene=${scene}&theme=${theme}`, { waitUntil: 'networkidle' })
      // The menu is forced open; wait for the asserted row to exist.
      const row = page.getByRole('menuitem', { name: expect })
      try {
        await row.waitFor({ state: 'visible', timeout: 15000 })
      } catch {
        throw new Error(`kebab/${theme}/${scene}: row "${expect}" never rendered${errors.length ? ` (${errors[0]})` : ''}`)
      }
      // The error scene must additionally show the failure notice.
      if (scene === 'error') {
        const notice = page.getByText("Couldn't change the mute setting", { exact: false })
        try {
          await notice.waitFor({ state: 'visible', timeout: 10000 })
        } catch {
          throw new Error(`kebab/${theme}/error: the mute-failure ErrorNotice never rendered${errors.length ? ` (${errors[0]})` : ''}`)
        }
      }
      // Shoot the open menu content (the portal), with a little margin.
      const menu = page.getByRole('menu').first()
      await page.waitForTimeout(300)
      const file = `${OUT}/kebab-${scene}-${theme}.png`
      await menu.screenshot({ path: file })
      record(file, `row="${expect}"${scene === 'error' ? ' + failure notice' : ''}`)
      await ctx.close()
    }
  }

  // ── Settings toggle: removed. The global "mute any session opened by another
  //    session" preference was cut (SHRINK) — only the per-row kebab control
  //    remains, captured above. No settings capture entry to shoot.

  console.log('\n── SUMMARY ─────────────────────────────')
  const bad = wrote.filter(w => w.over || w.blank)
  console.log(bad.length ? `FAIL ${bad.length}` : `all ${wrote.length} frames ok`)

  await browser.close()
  if (bad.length) process.exit(1)
}

run().catch(err => { console.error(err); process.exit(1) })

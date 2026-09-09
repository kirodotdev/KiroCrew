/**
 * Screenshots of the session-trust tier on an approval card whose COMMAND
 * scope the gateway could not prove.
 *
 * Drives website/capture/trust-all-without-command-scope.html, which mounts the
 * REAL ChatInput with the permission meta the gateway sends.
 *
 * Each frame ASSERTS its state before writing the file, so a frame cannot
 * silently document the wrong one:
 *   after  (default): one control, and it NAMES the grant ("Trust all tools for
 *     this session") because a single tier collapses onto the control itself.
 *     No menu, and no bare "Trust" verb.
 *   --before: no Trust control at all, which is what the card looked like when
 *     the session tier shared the command bit.
 *   --folded: a fully-proven READ-ONLY command. Four tiers inside the one
 *     dropdown and three controls on the row, which is the cap
 *     `max-two-buttons-per-row` grandfathers.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6841 --strictPort   # in another shell
 *   node scripts/capture-trust-all-without-command-scope.mjs http://127.0.0.1:6841 ../temp-screenshots/trust-all-without-command-scope [--before|--folded]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6841'
const OUT = process.argv[3] || '../temp-screenshots/trust-all-without-command-scope'
const BEFORE = process.argv.includes('--before')
const FOLDED = process.argv.includes('--folded')
mkdirSync(OUT, { recursive: true })

const SCENES = FOLDED
  ? [
    { name: 'trust-tiers-folded-dark', theme: 'dark' },
    { name: 'trust-tiers-folded-light', theme: 'light' },
  ]
  : [
    { name: 'trust-all-scopeless-dark', theme: 'dark' },
    { name: 'trust-all-scopeless-light', theme: 'light' },
  ]

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 900, height: 300 }, deviceScaleFactor: 2 })

let failed = false
for (const s of SCENES) {
  const scene = FOLDED ? 'folded' : BEFORE ? 'before' : 'after'
  await page.goto(`${BASE}/capture/trust-all-without-command-scope.html?theme=${s.theme}&scene=${scene}`)
  await page.waitForSelector('[data-capture-root]')

  const allowOnce = page.locator('[data-capture-root] button', { hasText: 'Allow once' })
  await allowOnce.first().waitFor()
  const trust = page.locator('[data-capture-root] button', { hasText: /^Trust/ })
  const trustCount = await trust.count()
  const trustLabel = trustCount ? (await trust.first().innerText()).trim() : ''
  // The action row's own button count is what `max-two-buttons-per-row` bounds.
  const rowButtons = await allowOnce.first().evaluate(
    el => el.parentElement?.querySelectorAll('button').length ?? 0,
  )

  // A control that opens a MENU is the multi-tier shape; a single tier collapses
  // onto the control itself, so only the folded scene has a menu to open.
  let items = []
  if (FOLDED) {
    await trust.first().click()
    await page.locator('[role="menuitem"]').first().waitFor()
    // The menu fades and scales in; a frame shot on the first paint documents
    // a half-transparent control that reads as a rendering bug.
    await page.waitForTimeout(400)
    items = (await page.locator('[role="menuitem"]').allInnerTexts()).map(t => t.trim())
  }

  const ok = FOLDED
    // Read-only AND fully proven: four tiers in one dropdown, three controls on
    // the row. A fourth row button is the violation this scene rules out.
    ? trustCount === 1 && trustLabel === 'Trust' && rowButtons === 3 && items.length === 4
      && items.some(t => t === 'Trust read-only commands')
      && items.some(t => /Trust all tools for this session/i.test(t))
    : BEFORE
      ? trustCount === 0 && rowButtons === 2
      // One tier, so no menu: the control names the scope it grants and the
      // bare verb is gone.
      : trustCount === 1 && rowButtons === 3
        && trustLabel === 'Trust all tools for this session'
        && (await page.locator('[role="menuitem"]').count()) === 0
  console.log(`${s.name} (${scene}): trustControls=${trustCount} label=${JSON.stringify(trustLabel)} rowButtons=${rowButtons} menu=${JSON.stringify(items)} ${ok ? 'OK' : 'MISMATCH'}`)
  if (!ok) { failed = true; continue }

  await page.screenshot({ path: `${OUT}/${s.name}${BEFORE ? '-before' : ''}.png` })
}

await browser.close()
process.exit(failed ? 1 : 0)

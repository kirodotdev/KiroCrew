/**
 * Screenshot harness for the left-nav community row's top border token.
 *
 * The row holding "Star us · Report issue" drew its divider with
 * `border-border-strong` (--border-strong) while every other separator in the
 * rail — including the hairline directly above it, under the brand mark — uses
 * `border-border` (--border). At the default kiro-dark values that is #4a464f
 * against #352f3d: one contrast step apart, and visible as a mismatched pair of
 * lines in the same card.
 *
 * The subject is a single border-top-color, so each theme is photographed twice
 * from the SAME built bundle: once as shipped (the fix), and once with that one
 * property forced back to var(--border-strong) to reconstruct the prior state.
 * Toggling the property is exact here precisely BECAUSE the change is nothing
 * else — no geometry moves, so a rebuild of the old bundle would differ in that
 * property alone. The clip is tight on the rail's lower half so both hairlines
 * land in frame and can be compared against each other, which is the whole
 * point: the defect is a relationship between two lines, not one line's value.
 *
 * Runs the REAL built SPA (website/dist) gateway-free behind the shared
 * stubDashboardApi fixtures. Expanded rail only — the row is hidden (max-height
 * folded) while the rail is collapsed, so there is no collapsed state to shoot.
 *
 * Usage: node scripts/capture-leftnav-footer-border.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/leftnav-footer-border'

const SCENES = [
  { name: 'dark', theme: 'dark', attr: 'kiro-dark' },
  { name: 'light', theme: 'light', attr: 'kiro-light' },
]

const slots = [
  { key: 's1', title: 'Rail footer divider', messages: 4, running: false, agent: 'kirocrew', mode: '', created: '2026-08-11T01:00:00Z', last_ts: '2026-08-11T04:00:00Z', folder_id: '' },
]

mkdirSync(OUT, { recursive: true })

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()

  try {
    for (const s of SCENES) {
      const context = await browser.newContext({
        viewport: { width: 1400, height: 940 },
        // Both dividers are near-black hairlines a step apart; a 1x shot loses
        // the difference to PNG banding, which would make the before frame and
        // the after frame look identical and prove nothing.
        deviceScaleFactor: 2,
      })
      const page = await context.newPage()
      await stubDashboardApi(page, { slots, theme: s.theme })
      logPageProblems(page)
      // Runs after the stub's own init script, so it survives its
      // localStorage.clear(). mc-nav '0' keeps the rail expanded — the row is
      // folded away at max-height:0 when collapsed.
      await page.addInitScript(attr => {
        localStorage.setItem('mc-color-theme', attr)
        localStorage.setItem('mc-privacy-notice-v1', '1')
        localStorage.setItem('mc-nav', '0')
      }, s.attr)

      await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
      // Assert on the applied palette rather than sleeping: a shot taken
      // pre-swap carries the wrong token values and the comparison is void.
      await page.waitForFunction(
        t => document.documentElement.getAttribute('data-theme') === t,
        s.attr, { timeout: 15000 })

      // Anchor on the link and walk to the nearest bordered ancestor: the row
      // carries no role or test id of its own. The hook is `border-t` — the
      // side utility, which this change leaves alone — never the colour token
      // being compared, so the locator cannot drift with the thing under test.
      const row = page.locator('a[href="https://github.com/kirodotdev/KiroCrew"]')
        .locator('xpath=ancestor::div[contains(@class,"border-t")][1]')
      await row.waitFor({ state: 'visible', timeout: 15000 })

      // The FULL rail column, not a crop around the row: the defect is that the
      // row's divider disagrees with the brand hairline at the top of the same
      // card, so a frame that excludes that hairline cannot show it at all.
      const clip = { x: 0, y: 0, width: 300, height: 940 }

      // Park the pointer away from the rail so no row is hover-lit.
      await page.mouse.move(1000, 380)
      await page.waitForTimeout(300)

      const shipped = await row.evaluate(el => getComputedStyle(el).borderTopColor)
      await page.screenshot({ path: `${OUT}/after-${s.name}.png`, clip })
      console.log(`wrote after-${s.name}.png  border-top-color=${shipped}`)

      // Reconstruct the prior state: the one property this change touched.
      await row.evaluate(el => { el.style.borderTopColor = 'var(--border-strong)' })
      await page.waitForTimeout(120)
      const prior = await row.evaluate(el => getComputedStyle(el).borderTopColor)
      await page.screenshot({ path: `${OUT}/before-${s.name}.png`, clip })
      console.log(`wrote before-${s.name}.png border-top-color=${prior}`)

      if (shipped === prior) {
        throw new Error(`${s.name}: before and after resolved to the same colour ` +
          `(${shipped}) — the comparison would be meaningless`)
      }

      await context.close()
    }
  } finally {
    await browser.close()
    srv.close()
  }
}

await main()

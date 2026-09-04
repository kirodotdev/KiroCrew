/**
 * Screenshot harness for the nav rail's Terminal row open-state highlight.
 *
 * The bug is only visible in a state a unit test cannot photograph: the row was
 * lit by `:hover` alone, so with the docked panel OPEN and the pointer moved
 * away the rail looked identical to the panel being shut. Every shot here
 * therefore parks the mouse in the content area BEFORE capturing — a shot taken
 * with the cursor still on the row would pass under the old code too.
 *
 * Per theme, two rail widths x two panel states:
 *   closed         — row unlit, panel hidden (the control case)
 *   open-unhovered — row lit while the pointer sits far away (the fix)
 * captured with the rail expanded (text + icon) and collapsed (icon only, where
 * the icon tint is the whole signal).
 *
 * Runs the REAL built SPA (website/dist) gateway-free behind the shared
 * stubDashboardApi fixtures. The PTY calls behind the panel go unanswered on
 * purpose — the subject is the RAIL, not the shell.
 *
 * Usage: node scripts/capture-terminal-nav-active.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/terminal-nav-active'

const SCENES = [
  { name: 'dark', theme: 'dark', attr: 'kiro-dark' },
  { name: 'light', theme: 'light', attr: 'kiro-light' },
]
const RAILS = [
  { name: 'expanded', collapsed: false, width: 560 },
  { name: 'collapsed', collapsed: true, width: 320 },
]

/** Where the pointer parks: content area, far from the rail, so :hover is
 *  definitively off in every shot. */
const AWAY = { x: 1000, y: 380 }

const slots = [
  { key: 's1', title: 'Docked panel rail state', messages: 4, running: false, agent: 'kirocrew', mode: '', created: '2026-08-05T01:00:00Z', last_ts: '2026-08-05T04:00:00Z', folder_id: '' },
]

mkdirSync(OUT, { recursive: true })

async function shoot(page, path, clip, row) {
  await page.mouse.move(AWAY.x, AWAY.y)
  // The panel animates in on a framer-motion height spring; settle it so the
  // rail is not photographed against a half-drawn panel.
  await page.waitForTimeout(700)
  await page.screenshot({ path, clip })
  console.log(`wrote ${path} (aria-pressed=${await row.getAttribute('aria-pressed')})`)
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()

  try {
    for (const rail of RAILS) {
      for (const s of SCENES) {
        const context = await browser.newContext({
          viewport: { width: 1400, height: 940 },
          // The lit state is a low-contrast tinted background (#352f3d on the
          // dark rail); a 1x shot loses it to PNG banding.
          deviceScaleFactor: 2,
        })
        const page = await context.newPage()
        await stubDashboardApi(page, { slots, theme: s.theme })
        logPageProblems(page)
        // Runs after the stub's own init script, so it survives its
        // localStorage.clear(). `mc-color-theme` picks the kiro palette;
        // `mc-nav` is the rail-collapsed flag App reads on mount.
        await page.addInitScript(([attr, collapsed]) => {
          localStorage.setItem('mc-color-theme', attr)
          localStorage.setItem('mc-privacy-notice-v1', '1')
          localStorage.setItem('mc-nav', collapsed ? '1' : '0')
          // A persisted open panel would make the "closed" control case a lie.
          localStorage.removeItem('mc-bottom-terminal')
        }, [s.attr, rail.collapsed])

        await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
        // The palette is applied by a boot effect, so assert on the attribute
        // rather than sleeping: a shot taken pre-swap is the wrong theme.
        await page.waitForFunction(
          t => document.documentElement.getAttribute('data-theme') === t,
          s.attr, { timeout: 15000 })

        // Intersected with .nav-item (which IS the role=button element, not a
        // wrapper): a bare role+name also matches session rows and chips whose
        // accessible name happens to contain the word. exact:true handles the
        // expanded rail (text label) and the collapsed one (aria-label) alike.
        const row = page.getByRole('button', { name: 'Terminal', exact: true })
          .and(page.locator('.nav-item'))
        await row.waitFor({ state: 'visible', timeout: 15000 })
        const clip = { x: 0, y: 0, width: rail.width, height: 940 }

        await shoot(page, `${OUT}/${rail.name}-closed-${s.name}.png`, clip, row)
        await row.click()
        await shoot(page, `${OUT}/${rail.name}-open-unhovered-${s.name}.png`, clip, row)

        await context.close()
      }
    }
  } finally {
    await browser.close()
    srv.close()
  }
}

await main()

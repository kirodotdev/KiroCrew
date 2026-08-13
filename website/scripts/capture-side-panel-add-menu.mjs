/**
 * Screenshot harness for the side panel's "+" tab menu after its move onto the
 * shared shadcn/Radix dropdown.
 *
 * The menu is what the change is: the hand-rolled popover rendered square,
 * full-bleed rows with no hover pill and its own outside-click listener, so the
 * evidence has to be the menu OPEN, in both themes, at a crop where the 13px
 * item labels and the rounded hover row are legible.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures (gateway-free — no
 * kiro-cli, no dashboard token). Only the network and the localStorage seed are
 * stubbed; the client code under test is unmodified.
 *
 * Usage: node scripts/capture-side-panel-add-menu.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/side-panel-add-menu'
const SLOT = 'side-panel-add-menu'

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Side panel + menu on the shared dropdown',
  running: false,
  messages: 2,
  agent: 'kirocrew',
  modified: Math.floor(Date.now() / 1000),
  last_ts: '2026-08-05T22:00:00Z',
  folder_id: '',
}]

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1400, height: 900 },
    deviceScaleFactor: 2, // 13px menu type renders soft at 1x
  })
  const page = await context.newPage()
  logPageProblems(page)

  for (const theme of ['light', 'dark']) {
    // Re-stub per theme: /api/theme/boot decides the mode, and the init script
    // that seeds localStorage is registered by the stub (it clears storage, so
    // the panel seed below must be registered AFTER it).
    await stubDashboardApi(page, { slots, theme })
    await page.addInitScript(slot => {
      localStorage.setItem('mc-active-slot', slot)
      // chatSlice rehydrates the panel's open state from this per-slot key.
      localStorage.setItem('mc-activity-open:' + slot, 'true')
      localStorage.setItem('mc-privacy-notice-v1', '1')
      // Seed the strip so the shot shows the menu beside real tabs.
      localStorage.setItem('mc-panel-tabs:' + slot, JSON.stringify({
        tabs: [
          { id: 'changes', kind: 'changes', title: 'Changes' },
          { id: 'files', kind: 'files', title: 'Files' },
          { id: 'artifacts', kind: 'artifacts', title: 'Artifacts' },
        ],
        activeId: 'files',
      }))
    }, SLOT)

    await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2600)

    const trigger = page.locator('button[aria-label="Open side panel tab"]')
    await trigger.first().waitFor({ state: 'visible', timeout: 15000 })
    // Measure the trigger BEFORE opening: the dropdown is modal, so Radix marks
    // the rest of the page aria-hidden while it is open and the strip's own
    // role queries find nothing.
    const trig = await trigger.first().boundingBox()
    await trigger.first().click()

    // Radix portals the content to <body>, so wait on the menu, not the trigger.
    const menu = page.locator('[role="menu"]')
    await menu.first().waitFor({ state: 'visible', timeout: 10000 })
    await page.waitForTimeout(500) // let the open animation settle
    // Hover an item so the rounded highlight pill — the visible half of the
    // change — is in frame.
    await menu.first().locator('[role="menuitem"]').nth(1).hover()
    await page.waitForTimeout(200)

    console.log('MENU ITEMS', JSON.stringify(
      (await menu.first().locator('[role="menuitem"]').allInnerTexts()).map(s => s.trim()),
    ))

    await page.screenshot({ path: `${OUT}/01-window-${theme}.png` })
    const box = await menu.first().boundingBox()
    // Crop from a little left of the menu down to the window's right edge: the
    // menu is right-aligned against the panel, so a width derived from the menu
    // alone clips its own rounded right corner off.
    const x = Math.max(0, box.x - 40)
    const y = Math.max(0, (trig?.y ?? box.y) - 20)
    await page.screenshot({
      path: `${OUT}/02-menu-${theme}.png`,
      clip: {
        x, y,
        width: 1400 - x,
        height: Math.min(900 - y, box.y + box.height - y + 24),
      },
    })
    console.log('wrote', `${OUT}/02-menu-${theme}.png`)
  }

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })

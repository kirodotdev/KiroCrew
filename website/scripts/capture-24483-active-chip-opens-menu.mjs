/**
 * Screenshot evidence for #24483: clicking the active instance chip opens the
 * crew switcher menu, the same as the caret.
 *
 * Runs the REAL built SPA behind the shared static server with the boot
 * fixtures answered by the shared stub -- no gateway, no auth. `/api/instances`
 * is overridden with two connected instances so the top-bar switcher renders
 * its leading active chip plus the caret.
 *
 * Captures three frames in dark theme:
 *   1-closed   switcher at rest (menu shut)
 *   2-open     after clicking the ACTIVE CHIP (the fix: this used to do nothing)
 *   3-caret    menu reopened via the caret, to show the caret still works
 *
 * Self-checks the Radix menu's open state via aria-expanded so a frame taken
 * before the menu mounts cannot pass as evidence.
 *
 * Usage: node scripts/capture-24483-active-chip-opens-menu.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/24483-active-chip-opens-menu'
mkdirSync(OUT, { recursive: true })

const sso = { state: 'ok', seconds_remaining: 72000, expires_at: null, reason: 'valid' }

/** Two connected instances so the switcher shows a leading active chip + caret. */
const INSTANCES = {
  active: true,
  warm_set_cap: 5,
  sso,
  instances: [
    {
      id: 'cd-1', name: 'Cloud One', ssh_host: 'cd-1-alias',
      remote_port: 7777, local_port: 7778, ttl: '20h', remote_bin: '',
      was_connected: true,
      status: { instance_id: 'cd-1', state: 'connected', local_port: 7778, remote_port: 7777 },
    },
    {
      id: 'cd-2', name: 'Cloud Two', ssh_host: 'cd-2-alias',
      remote_port: 7779, local_port: 7780, ttl: '20h', remote_bin: '',
      was_connected: true,
      status: { instance_id: 'cd-2', state: 'connected', local_port: 7780, remote_port: 7779 },
    },
  ],
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()

try {
  const context = await browser.newContext({
    viewport: { width: 1280, height: 900 },
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    theme: 'dark',
    extra: async (path, route) => {
      if (path.startsWith('/api/instances')) { await json(route, INSTANCES); return true }
      return false
    },
  })

  await page.goto(`${base}/apps`, { waitUntil: 'domcontentloaded' })

  // The active chip leads the switcher row; it is the connected instance's name.
  const activeChip = page.locator('.tb-crew-active-chip')
  await activeChip.waitFor({ timeout: 8000 })

  const clip = { x: 0, y: 0, width: 520, height: 120 }

  // 1. At rest: menu closed, chip reports aria-expanded=false.
  if ((await activeChip.getAttribute('aria-expanded')) !== 'false') {
    throw new Error('expected active chip aria-expanded=false at rest')
  }
  await page.screenshot({ path: `${OUT}/1-closed.png`, clip })
  console.log(`wrote ${OUT}/1-closed.png`)

  // 2. The fix: clicking the ACTIVE CHIP opens the menu.
  await activeChip.click()
  await page.getByRole('menu').first().waitFor({ timeout: 4000 })
  if ((await activeChip.getAttribute('aria-expanded')) !== 'true') {
    throw new Error('expected active chip aria-expanded=true after click')
  }
  await page.waitForTimeout(250) // let the menu settle open
  // Clip to the switcher + its open menu (top-left) so the frame shows only the
  // surface under test, not unrelated page content behind the dropdown.
  const menuClip = { x: 0, y: 0, width: 410, height: 320 }
  await page.screenshot({ path: `${OUT}/2-open.png`, clip: menuClip })
  console.log(`wrote ${OUT}/2-open.png`)

  // Close it again (click the chip once more), then show the caret still works.
  await activeChip.click()
  await page.waitForTimeout(150)
  const caret = page.getByRole('button', { name: /Switch crew/i })
  await caret.click()
  await page.getByRole('menu').first().waitFor({ timeout: 4000 })
  await page.waitForTimeout(250)
  await page.screenshot({ path: `${OUT}/3-caret.png`, clip: menuClip })
  console.log(`wrote ${OUT}/3-caret.png`)

  await context.close()
  console.log('OK: active chip opens the menu (aria-expanded false -> true); caret still opens it')
} finally {
  await browser.close()
  srv.close()
}

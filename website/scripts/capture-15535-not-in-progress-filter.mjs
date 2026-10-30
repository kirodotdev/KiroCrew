/**
 * Screenshot harness for the "Not in progress" sidebar filter.
 *
 * Shows the Filter menu with the new "Not in progress" named row and the
 * resulting chip when active, plus the "In progress" selected state, where the
 * "Not in progress" row stays in the menu unchecked. Captures desktop (1280px)
 * and mobile (390px) in light and dark.
 *
 * Usage: node --experimental-vm-modules scripts/capture-15535-not-in-progress-filter.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi, logPageProblems } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/capture-not-in-progress-filter'
mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)

const slots = [
  { key: 'c-running-1', title: 'Build the release bundle', running: true, messages: 4, agent: 'kirocrew', modified: now - 60, last_ts: new Date(Date.now() - 60_000).toISOString(), folder_id: '' },
  { key: 'c-running-2', title: 'Deploy to staging', running: true, messages: 2, agent: 'kirocrew', modified: now - 120, last_ts: new Date(Date.now() - 120_000).toISOString(), folder_id: '' },
  { key: 'c-idle-1', title: 'Review CR feedback', running: false, messages: 8, agent: 'kirocrew', modified: now - 300, last_ts: new Date(Date.now() - 300_000).toISOString(), folder_id: '' },
  { key: 'c-idle-2', title: 'On-call ticket triage', running: false, messages: 12, agent: 'oncall', modified: now - 600, last_ts: new Date(Date.now() - 600_000).toISOString(), folder_id: '' },
  { key: 'c-idle-3', title: 'Daily worklog', running: false, messages: 3, agent: 'kirocrew', modified: now - 900, last_ts: new Date(Date.now() - 900_000).toISOString(), folder_id: '' },
]

const FILTER_BTN = 'button[aria-label="Sort and filter sessions"]'
const MOBILE_SESSIONS_TOGGLE = '[data-testid="mobile-topbar-sessions-toggle"]'

async function capture(page, name) {
  await page.screenshot({ path: join(OUT, `${name}.png`) })
  console.log(`  → ${name}.png`)
}

async function openFilterMenu(page, isMobile) {
  // On mobile the sessions drawer starts closed; open it only if it is not
  // already expanded, so a repeated call does not toggle it closed.
  if (isMobile) {
    const toggle = page.locator(MOBILE_SESSIONS_TOGGLE)
    if (await toggle.count() > 0) {
      const expanded = await toggle.getAttribute('aria-expanded')
      if (expanded !== 'true') {
        await toggle.click()
        await page.waitForTimeout(400)
      }
    }
  }
  await page.click(FILTER_BTN)
  await page.waitForSelector('[role="menu"]', { timeout: 5_000 })
  await page.waitForTimeout(300)
}

async function run() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  console.log('Capturing screenshots for #15535 Not-in-progress filter…')

  for (const theme of ['dark', 'light']) {
    for (const vp of [
      { width: 1280, height: 800, label: 'desktop', mobile: false },
      { width: 390, height: 844, label: 'mobile', mobile: true },
    ]) {
      const page = await browser.newPage()
      await page.setViewportSize({ width: vp.width, height: vp.height })
      logPageProblems(page)

      await stubDashboardApi(page, {
        slots,
        theme,
        localStorageEntries: {
          'mc-sidebar-lane': 'flat',
          'mc-session-stale-collapse-ms': '0',
          'mc-sidebar-pinned': 'true',
          'mc-onboarded': '1',
        },
      })

      await page.goto(`${base}/chat`, { waitUntil: 'domcontentloaded' })

      // On mobile, open the sessions drawer so the sidebar renders.
      if (vp.mobile) {
        const toggle = page.locator(MOBILE_SESSIONS_TOGGLE)
        if (await toggle.count() > 0) {
          await toggle.click()
          await page.waitForTimeout(600)
        }
      }

      await page.waitForSelector('[data-testid^="session-row"]', { timeout: 15_000 })
        .catch(() => page.waitForTimeout(3000))
      await page.waitForTimeout(500)

      const tag = `${theme}-${vp.label}`

      // Scene 1: baseline — all sessions visible
      await capture(page, `1-baseline-${tag}`)

      // Scene 2: filter menu open — shows "Not in progress" row
      await openFilterMenu(page, vp.mobile)
      await capture(page, `2-filter-menu-open-${tag}`)

      // Scene 3: activate "Not in progress" — row gets Check, running sessions hidden
      const notRow = page.getByTestId('filter-not-running')
      if (await notRow.count() > 0) {
        await notRow.click({ force: true })
        await page.waitForTimeout(500)
        await capture(page, `3-not-in-progress-active-${tag}`)

        // Scene 4: close menu, "Hiding: In progress (N)" chip in context
        await page.keyboard.press('Escape')
        await page.waitForTimeout(300)
        await capture(page, `4-chip-in-context-${tag}`)

        // Scene 5: re-open menu and activate "In progress" show-only
        // This clears the hide and turns on show-only; the "Not in progress" row stays, unchecked
        await openFilterMenu(page, vp.mobile)
        const inProgressRow = page.getByRole('menuitem', { name: /^In progress/ })
        if (await inProgressRow.count() > 0) {
          await inProgressRow.click({ force: true })
          await page.waitForTimeout(500)
          // Scene 5: menu with "In progress" selected, "Not in progress" row still offered
          await capture(page, `5-in-progress-selected-${tag}`)
        }
      } else {
        console.warn(`  ! filter-not-running not found in ${tag}`)
      }

      await page.close()
    }
  }

  await browser.close()
  srv.close()
  console.log(`\nDone → ${OUT}`)
}

run().catch(err => { console.error(err); process.exit(1) })

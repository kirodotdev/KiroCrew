/**
 * Screenshot harness for the sessions filter menu's duration pickers at PHONE
 * width — the Recent window and the dormant-collapse threshold.
 *
 * Why a harness and not a still of one state: the defect is a POSITION. Both
 * pickers are nested Radix submenus, and Radix hardcodes a submenu to
 * `side="right"` while its popper shifts only on the cross axis — so at phone
 * width the flyout never moves along the axis that is short. It lands off one
 * screen edge (measured at 390px: 249px wide, 192px past the right edge,
 * `--radix-popper-available-width: 57px`; `flip`'s bestFit can pick the LEFT
 * edge instead, which is what the bug report showed). The evidence has to be the
 * menu actually driven open, with the numbers read off the live layout — so this
 * harness FAILS if any part of the open menu sits outside the viewport, which is
 * exactly the assertion a still image cannot make.
 *
 * It also captures the wide viewport, where the flyout is correct and must stay
 * untouched.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures (gateway-free). Run it
 * on the base commit's dist for the "before" pair.
 *
 * Usage: node scripts/capture-sidebar-duration-pickers-narrow.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/sidebar-duration-pickers-narrow'
const PHONE = 390
const DESKTOP = 1440

mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)
const slots = [
  {
    key: 'chat-a', title: 'Draft the release notes', running: false, messages: 4,
    agent: 'kirocrew', modified: now, last_ts: new Date().toISOString(), folder_id: '',
    last_message: 'Grouped the entries by area.',
  },
  {
    key: 'chat-b', title: 'Sweep the option pickers', running: false, messages: 6,
    agent: 'kirocrew', modified: now - 900, last_ts: new Date(Date.now() - 9e5).toISOString(), folder_id: '',
    last_message: 'All checks pass.',
  },
]

/** Every open menu surface, with how far it falls outside the viewport. */
const measureMenus = page => page.evaluate(() => [...document.querySelectorAll('[role="menu"]')].map(m => {
  const r = m.getBoundingClientRect()
  return {
    label: (m.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 28),
    width: +r.width.toFixed(1),
    offLeft: +Math.max(0, -r.x).toFixed(1),
    offRight: +Math.max(0, r.right - innerWidth).toFixed(1),
  }
}))

async function openFilterMenu(page) {
  const trigger = page.getByLabel('Sort and filter sessions')
  if (!(await trigger.isVisible().catch(() => false))) {
    // Phone: the sidebar is a drawer. The first toggle is the one behind the
    // closed drawer and is obscured, so try them newest-first.
    const toggles = page.getByLabel('Toggle sessions')
    for (let i = (await toggles.count()) - 1; i >= 0; i--) {
      await toggles.nth(i).click({ timeout: 3000 }).catch(() => {})
      await page.waitForTimeout(600)
      if (await trigger.isVisible().catch(() => false)) break
    }
  }
  await trigger.waitFor({ state: 'visible', timeout: 15000 })
  await trigger.click()
  await page.waitForTimeout(400)
  const recent = page.locator('[role="menuitem"]').filter({ hasText: 'Recent' }).first()
  await recent.waitFor({ state: 'visible', timeout: 10000 })
  return recent
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const problems = []

  async function shoot({ width, theme, tag }) {
    const context = await browser.newContext({
      viewport: { width, height: 780 },
      // The chips carry 11px text, illegible in a 1x shot.
      deviceScaleFactor: 2,
      hasTouch: width === PHONE,
      isMobile: width === PHONE,
    })
    const page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, { slots, theme })
    await page.addInitScript(() => {
      localStorage.setItem('mc-active-slot', 'chat-a')
      localStorage.setItem('mc-privacy-notice-v1', '1')
      localStorage.setItem('mc-onboarded', '1')
    })
    await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2600)

    const recent = await openFilterMenu(page)
    // Tapping the row opens the flyout on a wide viewport; on a phone the picker
    // is already inline and the tap only toggles the filter.
    await recent.click()
    await page.waitForTimeout(700)
    await page.screenshot({ path: `${OUT}/${tag}-01-recent-${theme}.png` })

    const menus = await measureMenus(page)
    console.log(`${tag} ${theme}: ${JSON.stringify(menus)}`)
    for (const m of menus) {
      if (m.offLeft > 0.5 || m.offRight > 0.5) {
        problems.push(`${tag} ${theme}: "${m.label}" is ${m.offLeft}px off the left / ${m.offRight}px off the right edge`)
      }
    }

    // The dormant-collapse picker sits below the fold on a phone.
    await page.evaluate(() => {
      const m = [...document.querySelectorAll('[role="menu"]')].find(x => x.scrollHeight > x.clientHeight)
      if (m) m.scrollTop = m.scrollHeight
    })
    await page.waitForTimeout(400)
    await page.screenshot({ path: `${OUT}/${tag}-02-dormant-${theme}.png` })

    await page.close()
    await context.close()
  }

  for (const theme of ['dark', 'light']) await shoot({ width: PHONE, theme, tag: 'phone' })
  await shoot({ width: DESKTOP, theme: 'dark', tag: 'desktop' })

  await browser.close()
  srv.close()

  if (problems.length) {
    throw new Error(`menu surfaces fell outside the viewport:\n  ${problems.join('\n  ')}`)
  }
  console.log('every open menu surface stayed inside the viewport')
}

main().catch(err => { console.error(err); process.exit(1) })

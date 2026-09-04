/**
 * Screenshot harness for the nav rail's per-row keyboard shortcut hint (#4370).
 *
 * The subject is a state a still frame can only prove if the frame is taken
 * honestly, so every shot here ASSERTS the state it claims before capturing:
 *
 *   idle    the badge is present in the DOM at opacity 0 (the control case -- a
 *           shot of a row that simply has no badge would look identical)
 *   hover   opacity 1 under the pointer
 *   focus   opacity 1 under KEYBOARD focus with the pointer parked far away.
 *           This is the shot that matters: a hover-only hint is unreachable
 *           without a pointer, so the harness refuses to write this frame unless
 *           the row genuinely matches :focus-visible.
 *
 * Collapsed rail has no text label and therefore no inline badge -- the chord
 * rides the row's existing hover/focus flyout instead, so that variant is
 * verified by the flyout's text rather than by an opacity.
 *
 * Runs the REAL built SPA (website/dist) gateway-free behind the shared
 * stubDashboardApi fixtures, the same technique as capture-terminal-nav-active.
 *
 * Usage: node scripts/capture-nav-shortcut-hint.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/nav-shortcut-hint'

/** Where the pointer parks: content area, far from the rail, so :hover is
 *  definitively off in the idle and focus shots. */
const AWAY = { x: 1000, y: 380 }

const slots = [
  { key: 's1', title: 'Nav shortcut hint', messages: 4, running: false, agent: 'kirocrew', mode: '', created: '2026-08-05T01:00:00Z', last_ts: '2026-08-05T04:00:00Z', folder_id: '' },
]

mkdirSync(OUT, { recursive: true })

/** Assert-then-shoot. A frame written without its assertion is not evidence. */
async function shoot(page, name, expect) {
  const state = await page.evaluate(() => {
    const badge = document.querySelector('[data-testid="nav-shortcut-chat"]')
    const row = document.querySelector('.nav-item[data-onboarding-nav="chat"]')
    const flyout = Array.from(document.querySelectorAll('body > div'))
      .map(d => (d.textContent || '').trim())
      .filter(t => t.includes('Alt'))
    return {
      badgePresent: !!badge,
      badgeText: badge ? (badge.textContent || '').trim() : null,
      badgeOpacity: badge ? getComputedStyle(badge).opacity : null,
      keyshortcuts: row ? row.getAttribute('aria-keyshortcuts') : null,
      focusVisible: !!row && row.matches(':focus-visible'),
      flyoutWithChord: flyout,
    }
  })
  for (const [k, v] of Object.entries(expect)) {
    const got = state[k]
    const ok = typeof v === 'function' ? v(got) : got === v
    if (!ok) throw new Error(`${name}: expected ${k}=${v} but measured ${JSON.stringify(got)} -- refusing to write a frame that does not show what it claims`)
  }
  const path = `${OUT}/${name}.png`
  await page.screenshot({ path })
  console.log(`wrote ${path} ${JSON.stringify(state)}`)
}

async function openRail(browser, base, collapsed) {
  const context = await browser.newContext({ viewport: { width: 1400, height: 940 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  await stubDashboardApi(page, { slots, theme: 'dark' })
  logPageProblems(page)
  await page.addInitScript(c => {
    localStorage.setItem('mc-color-theme', 'kiro-dark')
    localStorage.setItem('mc-privacy-notice-v1', '1')
    localStorage.setItem('mc-nav', c ? '1' : '0')
    // A disabled toggle would suppress every hint, making an empty rail look
    // like the feature is missing rather than switched off.
    localStorage.removeItem('mc-keyboard-shortcuts')
  }, collapsed)
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForFunction(
    t => document.documentElement.getAttribute('data-theme') === t,
    'kiro-dark', { timeout: 15000 })
  const row = page.locator('.nav-item[data-onboarding-nav="chat"]')
  await row.waitFor({ state: 'visible', timeout: 15000 })
  await page.mouse.move(AWAY.x, AWAY.y)
  await page.waitForTimeout(400)
  return { context, page, row }
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  try {
    // ---- Expanded rail: the inline badge ----
    {
      const { context, page, row } = await openRail(browser, base, false)

      await shoot(page, 'expanded-idle', {
        badgePresent: true, keyshortcuts: 'Alt+C', badgeText: 'Alt + C',
        // Present but invisible: the control case for the two shots below.
        badgeOpacity: o => Number(o) < 0.05,
      })

      await row.hover()
      await page.waitForTimeout(300)
      await shoot(page, 'expanded-hover', {
        badgeText: 'Alt + C', badgeOpacity: o => Number(o) > 0.95,
      })

      // Keyboard focus with the pointer parked away. A Tab press first so the
      // browser's focus modality is KEYBOARD -- programmatic focus alone does
      // not match :focus-visible, and a frame captured then would prove nothing.
      await page.mouse.move(AWAY.x, AWAY.y)
      await page.keyboard.press('Tab')
      await row.evaluate(el => el.focus())
      await page.waitForTimeout(300)
      await shoot(page, 'expanded-keyboard-focus', {
        focusVisible: true, badgeOpacity: o => Number(o) > 0.95, badgeText: 'Alt + C',
      })

      await context.close()
    }

    // ---- Collapsed rail: the chord rides the existing flyout ----
    {
      const { context, page, row } = await openRail(browser, base, true)

      await shoot(page, 'collapsed-idle', {
        // No inline badge exists at this width by construction.
        badgePresent: false, keyshortcuts: 'Alt+C',
        flyoutWithChord: f => f.length === 0,
      })

      await row.hover()
      await page.waitForTimeout(500)
      await shoot(page, 'collapsed-hover', {
        flyoutWithChord: f => f.some(t => t.includes('Alt + C')),
      })

      await context.close()
    }
  } finally {
    await browser.close()
    srv.close()
  }
}

await main()

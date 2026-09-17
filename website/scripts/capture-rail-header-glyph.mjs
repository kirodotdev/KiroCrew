/**
 * Screenshot harness for the nav-rail header's expand/collapse glyph when the
 * rail is COLLAPSED (icon-only).
 *
 * In that state the product logo is the button's only visible content — the
 * bot name is unmounted — so a failing avatar asset used to leave an invisible
 * control that still toggled the rail. The fix mounts RailHeaderGlyph: a
 * PanelLeft fallback fills the box until the logo's own `load` event, and an
 * `error` reverts to it.
 *
 * Three scenes from the SAME built bundle, all against the real SPA behind the
 * shared stubDashboardApi fixtures:
 *   logo-404-fallback           — collapsed rail, `/logo.png` aborted at the
 *                                 network layer, the img fires `error`, the
 *                                 PanelLeft fallback must fill the w-10 box.
 *   logo-loads-logo             — collapsed rail, the asset is served, the img
 *                                 fires `load`, the logo must be visible and
 *                                 the fallback gone.
 *   expanded-logo-404-fallback  — expanded rail, asset aborted: the w-7
 *                                 fallback sits beside the bot name (the
 *                                 state the old bug never made invisible, so
 *                                 the new glyph is shown there too).
 * Each frame is refused unless the DOM state it claims to show is measured.
 *
 * Usage: node scripts/capture-rail-header-glyph.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/rail-header-glyph'

const slots = [
  { key: 's1', title: 'Rail header glyph', messages: 4, running: false, agent: 'kirocrew', mode: '', created: '2026-08-11T01:00:00Z', last_ts: '2026-08-11T04:00:00Z', folder_id: '' },
]

mkdirSync(OUT, { recursive: true })

const FALLBACK = '[data-testid="rail-header-fallback"]'
// The rail header button: the one aria-expanded control whose content is the
// brand glyph (img or its fallback) -- other aria-expanded buttons exist.
const RAIL_BTN = `button[aria-expanded]:has(img[src="/logo.png"]), button[aria-expanded]:has(${FALLBACK})`

async function openRail(browser, base, { logo404, collapsed }) {
  const context = await browser.newContext({ viewport: { width: 1400, height: 940 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  if (logo404) await page.route('**/logo.png', route => route.abort('failed'))
  // Storage seeds go through the stub so they land AFTER its own clear; a
  // separately registered init script has no defined order against it.
  await stubDashboardApi(page, {
    slots,
    theme: 'dark',
    localStorageEntries: {
      'mc-color-theme': 'kiro-dark',
      'mc-privacy-notice-v1': '1',
      'mc-nav': collapsed ? '1' : '0',
    },
  })
  logPageProblems(page)
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForFunction(
    t => document.documentElement.getAttribute('data-theme') === t,
    'kiro-dark', { timeout: 15000 })
  await page.locator(RAIL_BTN).first().waitFor({ state: 'visible', timeout: 15000 })
  await page.mouse.move(1000, 600)
  await page.waitForTimeout(500)
  return { context, page }
}

async function measure(page) {
  return page.evaluate(({ FALLBACK, RAIL_BTN }) => {
    const btn = document.querySelector(RAIL_BTN)
    const fb = btn?.querySelector(FALLBACK)
    const img = btn?.querySelector('img')
    const visible = el => !!el && el.getClientRects().length > 0 && getComputedStyle(el).display !== 'none'
    return {
      collapsed: btn?.getAttribute('aria-expanded') === 'false',
      fallbackVisible: visible(fb),
      imgVisible: visible(img),
      imgComplete: img ? img.complete && img.naturalWidth > 0 : null,
      box: fb ? fb.className.match(/w-\d+ h-\d+/)?.[0] : img?.className.match(/w-\d+ h-\d+/)?.[0],
      brandText: !!btn?.textContent?.trim(),
    }
  }, { FALLBACK, RAIL_BTN })
}

async function shoot(page, name, expect, clip) {
  const state = await measure(page)
  for (const [k, v] of Object.entries(expect)) {
    if (state[k] !== v) throw new Error(`${name}: expected ${k}=${v} but measured ${JSON.stringify(state)} -- refusing to write a frame that does not show what it claims`)
  }
  const dialogs = await page.locator('[role="dialog"]').count()
  if (dialogs) throw new Error(`${name}: ${dialogs} dialog(s) open over the frame`)
  await page.screenshot({ path: `${OUT}/${name}.png`, clip })
  console.log(`wrote ${name}.png ${JSON.stringify(state)}`)
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  try {
    const COLLAPSED_CLIP = { x: 0, y: 0, width: 120, height: 160 }
    const EXPANDED_CLIP = { x: 0, y: 0, width: 300, height: 160 }
    const waitFallback = page => page.waitForFunction(([btn, fb]) => {
      const el = document.querySelector(btn)?.querySelector(fb)
      return !!el && el.getClientRects().length > 0
    }, [RAIL_BTN, FALLBACK], { timeout: 15000 })
    {
      const { context, page } = await openRail(browser, base, { logo404: true, collapsed: true })
      await waitFallback(page)
      await shoot(page, 'logo-404-fallback', { collapsed: true, fallbackVisible: true, imgVisible: false, box: 'w-10 h-10', brandText: false }, COLLAPSED_CLIP)
      await context.close()
    }
    {
      const { context, page } = await openRail(browser, base, { logo404: true, collapsed: false })
      await waitFallback(page)
      await shoot(page, 'expanded-logo-404-fallback', { collapsed: false, fallbackVisible: true, imgVisible: false, box: 'w-7 h-7', brandText: true }, EXPANDED_CLIP)
      await context.close()
    }
    {
      const { context, page } = await openRail(browser, base, { logo404: false, collapsed: true })
      await page.waitForFunction(btn => {
        const img = document.querySelector(btn)?.querySelector('img')
        return !!img && img.complete && img.naturalWidth > 0 && getComputedStyle(img).display !== 'none'
      }, RAIL_BTN, { timeout: 15000 })
      await shoot(page, 'logo-loads-logo', { collapsed: true, fallbackVisible: false, imgVisible: true, imgComplete: true, box: 'w-10 h-10', brandText: false }, COLLAPSED_CLIP)
      await context.close()
    }
  } finally {
    await browser.close()
    srv.close()
  }
}

await main()

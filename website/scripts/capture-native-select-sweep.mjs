/**
 * Screenshots for the native-<select> sweep.
 *
 * Captures the two surfaces the change is easiest to judge on:
 *   1. Settings > Display > Install theme — the site from the bug report. It was
 *      an OS-drawn menu; it is now the same themed popup as the Language field
 *      directly above it, so the two are shot together for comparison.
 *   2. Schedule > Calendar > Render in — 420 IANA zones through the new
 *      SearchableSelect, closed / open / filtered.
 *
 * Assertions, not just pixels: the run fails if a popup does not open, if the
 * zone list is short enough that a filter box would be pointless, or if the
 * console reports an error.
 *
 * Usage: npm run build && node scripts/capture-native-select-sweep.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/native-select-sweep-shots'

/** One cron job, enough to get the Schedule page past its empty state. */
const CRONS = {
  jobs: [{
    id: 'job-1', name: 'Nightly digest', message: 'summarise the day',
    schedule: 'every 24 hours', enabled: true, agent: 'kirocrew',
    timezone: 'UTC', cron_expr: '0 2 * * *',
    next_run: Math.floor(Date.now() / 1000) + 3600,
  }],
}

async function main() {
  mkdirSync(OUT, { recursive: true })
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } })

  const errors = []
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()) })
  page.on('pageerror', e => errors.push(`pageerror: ${e.message}`))

  await stubDashboardApi(page, {
    extra: async (path, route) => {
      if (path === '/api/crons') {
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(CRONS) })
        return true
      }
      return false
    },
  })

  // ── 1. Settings > Display > Install theme ────────────────────────────────
  await page.goto(base + '/settings?tab=display', { waitUntil: 'domcontentloaded' })
  const themeSource = page.getByRole('combobox', { name: 'Theme source' })
  await themeSource.waitFor({ timeout: 15000 })
  await themeSource.scrollIntoViewIfNeeded()
  await page.screenshot({ path: `${OUT}/1-install-theme-closed.png`, clip: await pad(themeSource, 40, 40) })

  // Clip computed BEFORE opening: once Radix opens the popup the trigger's
  // accessible name changes, so re-querying it here would time out.
  const themeClip = await pad(themeSource, 40, 150)
  await themeSource.click()
  const themeOpts = page.getByRole('option')
  await themeOpts.first().waitFor({ timeout: 10000 })
  if (await themeOpts.count() !== 2) {
    throw new Error(`expected 2 theme-source options, got ${await themeOpts.count()}`)
  }
  await page.screenshot({ path: `${OUT}/2-install-theme-open.png`, clip: themeClip })
  await page.keyboard.press('Escape')

  // ── 2. Schedule > Calendar > Render in ──────────────────────────────────
  await page.goto(base + '/schedule', { waitUntil: 'domcontentloaded' })
  await page.getByRole('button', { name: 'Calendar' }).click({ timeout: 15000 })
  const tz = page.getByRole('button', { name: 'Render timezone' })
  await tz.waitFor({ timeout: 15000 })
  await page.screenshot({ path: `${OUT}/3-timezone-closed.png`, clip: await pad(tz, 8, 48) })

  const tzOpenClip = await pad(tz, 8, 380)
  const tzFilteredClip = await pad(tz, 8, 200)
  await tz.click()
  const list = page.getByRole('listbox', { name: 'Render timezone' })
  await list.waitFor({ timeout: 10000 })
  const total = await list.getByRole('option').count()
  if (total < 50) throw new Error(`expected the full IANA list, got ${total} options`)
  await page.screenshot({ path: `${OUT}/4-timezone-open.png`, clip: tzOpenClip })

  await page.getByRole('textbox', { name: 'Search…' }).fill('shang')
  await page.waitForFunction(() => document.querySelectorAll('[role="option"]').length === 1, undefined, { timeout: 5000 })
  await page.screenshot({ path: `${OUT}/5-timezone-filtered.png`, clip: tzFilteredClip })

  await browser.close()
  srv.close()

  if (errors.length) {
    console.error('console errors:\n  ' + errors.join('\n  '))
    process.exit(1)
  }
  console.log(`OK: install-theme popup themed, ${total} zones listed, filtered to 1`)
  console.log(`screenshots in ${OUT}`)
}

/** Clip box anchored on an element, padded so a portalled popup lands in frame. */
async function pad(locator, top, bottom) {
  const b = await locator.boundingBox()
  return {
    x: Math.max(0, b.x - 24),
    y: Math.max(0, b.y - top),
    width: Math.min(1280 - Math.max(0, b.x - 24), b.width + 340),
    height: b.height + top + bottom,
  }
}

main().catch(e => { console.error(e); process.exit(1) })

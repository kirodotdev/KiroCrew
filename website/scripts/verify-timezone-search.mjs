/**
 * Real-browser proof + screenshots for the searchable timezone dropdown on the
 * Schedule page (SearchableSelect over Radix Popover).
 *
 * The unit tests cover filtering, keyboard and commit semantics in happy-dom.
 * What they cannot show is the thing the change is FOR: that the popup is
 * theme-drawn rather than an OS-drawn native <select> menu. So this script
 * captures the closed trigger, the open popup, and a filtered popup, and fails
 * loudly on any console error.
 *
 * Usage: npm run build && node scripts/verify-timezone-search.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/timezone-search-shots'

async function main() {
  mkdirSync(OUT, { recursive: true })
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const page = await browser.newPage({ viewport: { width: 1280, height: 820 } })

  const errors = []
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()) })
  page.on('pageerror', e => errors.push(`pageerror: ${e.message}`))

  // One job is enough to get past the empty state and render the Jobs card,
  // whose Calendar view hosts the timezone picker.
  await stubDashboardApi(page, {
    extra: async (path, route) => {
      if (path === '/api/crons') {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            jobs: [{
              id: 'job-1',
              name: 'Nightly digest',
              message: 'summarise the day',
              schedule: 'every 24 hours',
              enabled: true,
              agent: 'kirocrew',
              timezone: 'UTC',
              cron_expr: '0 2 * * *',
              next_run: Math.floor(Date.now() / 1000) + 3600,
            }],
          }),
        })
        return true
      }
      return false
    },
  })
  await page.goto(base + '/schedule', { waitUntil: 'domcontentloaded' })

  // The timezone picker lives in the Calendar view; the page opens on List.
  await page.getByRole('button', { name: 'Calendar' }).click({ timeout: 15000 })

  const trigger = page.getByRole('button', { name: 'Render timezone' })
  await trigger.waitFor({ timeout: 15000 })
  await page.screenshot({ path: `${OUT}/1-closed.png`, clip: await clipAround(trigger, 0, 60) })

  await trigger.click()
  const listbox = page.getByRole('listbox', { name: 'Render timezone' })
  await listbox.waitFor({ timeout: 10000 })
  const total = await listbox.getByRole('option').count()
  if (total < 50) throw new Error(`expected the full IANA list, got ${total} options`)
  await page.screenshot({ path: `${OUT}/2-open.png`, clip: await clipAround(trigger, 0, 380) })

  // The whole point of the change: narrow a 400-entry list by typing.
  await page.getByRole('textbox', { name: 'Search…' }).fill('shang')
  await page.waitForFunction(
    () => document.querySelectorAll('[role="option"]').length === 1,
    undefined,
    { timeout: 5000 },
  )
  await page.screenshot({ path: `${OUT}/3-filtered.png`, clip: await clipAround(trigger, 0, 200) })

  // Commit it and confirm the trigger reflects the new value.
  await listbox.getByRole('option').first().click()
  await page.waitForFunction(
    () => !document.querySelector('[role="listbox"]'),
    undefined,
    { timeout: 5000 },
  )
  const label = (await trigger.textContent()) ?? ''
  if (!label.includes('Asia/Shanghai')) {
    throw new Error(`trigger did not commit the picked zone, reads: ${label}`)
  }
  await page.screenshot({ path: `${OUT}/4-committed.png`, clip: await clipAround(trigger, 0, 60) })

  await browser.close()
  srv.close()

  if (errors.length) {
    console.error('console errors:\n  ' + errors.join('\n  '))
    process.exit(1)
  }
  console.log(`OK: ${total} zones listed, filtered to 1, committed Asia/Shanghai`)
  console.log(`screenshots in ${OUT}`)
}

/** Clip box anchored on an element, padded out so the portalled popup below it
 *  lands inside the frame. */
async function clipAround(locator, padTop, padBottom) {
  const b = await locator.boundingBox()
  return {
    x: Math.max(0, b.x - 24),
    y: Math.max(0, b.y - padTop - 8),
    width: Math.min(1280 - Math.max(0, b.x - 24), b.width + 320),
    height: b.height + padTop + padBottom,
  }
}

main().catch(e => { console.error(e); process.exit(1) })

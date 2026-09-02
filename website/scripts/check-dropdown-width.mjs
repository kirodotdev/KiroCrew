/**
 * Width-parity probe for the shared dropdowns.
 *
 * A Radix popup is positioned by the popper, not laid out inside the trigger, so
 * its width is whatever CSS says — and a `min-w` floor wider than the trigger
 * makes the panel visibly overhang. This walks every select-family dropdown on a
 * set of surfaces, opens it, and asserts panel width == trigger width.
 *
 * Command menus (`ui/dropdown-menu.tsx`) are deliberately NOT checked: their
 * trigger is often an icon button, so matching its width would crush the menu.
 *
 * Usage: npm run build && node scripts/check-dropdown-width.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/dropdown-width-shots'
/** Sub-pixel rounding from the popper's transform is not a mismatch. */
const TOLERANCE_PX = 1.5

const CRONS = {
  jobs: [{
    id: 'job-1', name: 'Nightly digest', message: 'summarise the day',
    schedule: 'every 24 hours', enabled: true, agent: 'kirocrew',
    timezone: 'UTC', cron_expr: '0 2 * * *',
    next_run: Math.floor(Date.now() / 1000) + 3600,
  }],
}

/** Surfaces to sweep. `prep` runs after navigation to reveal the controls. */
const SURFACES = [
  { id: 'settings-display', url: '/settings?tab=display' },
  { id: 'settings-chat', url: '/settings?tab=chat' },
  { id: 'settings-voice', url: '/settings?tab=voice' },
  { id: 'settings-overview', url: '/settings?tab=overview' },
  { id: 'settings-imports', url: '/settings?tab=imports' },
  { id: 'settings-instances', url: '/settings?tab=instances' },
  { id: 'artifacts', url: '/artifacts' },
  { id: 'knowledge', url: '/knowledge' },
  {
    id: 'hooks',
    url: '/hooks',
    // The event picker only exists inside the new-hook form.
    prep: async page => { await page.getByRole('button', { name: /\+ new hook/i }).click({ timeout: 15000 }) },
  },
  {
    id: 'schedule-calendar',
    url: '/schedule',
    prep: async page => { await page.getByRole('button', { name: 'Calendar' }).click({ timeout: 15000 }) },
  },
]

async function main() {
  mkdirSync(OUT, { recursive: true })
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } })

  await stubDashboardApi(page, {
    extra: async (path, route) => {
      if (path === '/api/crons') {
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(CRONS) })
        return true
      }
      return false
    },
  })

  const rows = []
  const bad = []

  for (const surface of SURFACES) {
    await page.goto(base + surface.url, { waitUntil: 'domcontentloaded' })
    try { if (surface.prep) await surface.prep(page) } catch { /* surface may not offer it */ }
    await page.waitForTimeout(600)

    // Both families: SimpleSelect/SettingsSelect expose Radix Select's
    // role=combobox trigger; SearchableSelect is a Popover trigger carrying
    // aria-haspopup="listbox". Missing the second would leave the component
    // whose width floor was widest completely unchecked.
    const triggers = page.locator('[role="combobox"], [aria-haspopup="listbox"]')
    const n = await triggers.count()
    for (let i = 0; i < n; i++) {
      const trigger = triggers.nth(i)
      if (!(await trigger.isVisible().catch(() => false))) continue
      // A disabled picker cannot be opened, so it has no panel to compare.
      if (!(await trigger.isEnabled().catch(() => false))) continue
      const name = (await trigger.getAttribute('aria-label')) || `#${i}`
      const tb = await trigger.boundingBox()
      if (!tb) continue

      await trigger.click({ timeout: 5000 }).catch(() => {})
      // The panel is whichever portalled surface just appeared.
      const panel = page.locator('[role="listbox"], [data-radix-select-viewport]').first()
      let pb = null
      try {
        await panel.waitFor({ timeout: 3000 })
        pb = await panel.evaluate(el => {
          // The viewport is inside the sized content element; measure the
          // positioned panel, not the scroll container.
          const box = el.closest('[data-radix-popper-content-wrapper]') || el.parentElement || el
          const r = box.getBoundingClientRect()
          return { width: r.width }
        })
      } catch { /* not an openable select */ }
      await page.keyboard.press('Escape')
      await page.waitForTimeout(120)
      if (!pb) continue

      const delta = Math.abs(pb.width - tb.width)
      const row = { surface: surface.id, name, trigger: +tb.width.toFixed(1), panel: +pb.width.toFixed(1), delta: +delta.toFixed(1) }
      rows.push(row)
      if (delta > TOLERANCE_PX) bad.push(row)
    }
  }

  await browser.close()
  srv.close()

  if (!rows.length) throw new Error('probe found no dropdowns — the sweep is vacuous, fix the selectors')
  for (const r of rows) {
    const flag = r.delta > TOLERANCE_PX ? '  <-- MISMATCH' : ''
    console.log(`${r.surface.padEnd(20)} ${String(r.name).padEnd(28)} trigger=${String(r.trigger).padStart(6)}  panel=${String(r.panel).padStart(6)}  d=${r.delta}${flag}`)
  }
  console.log(`\nchecked ${rows.length} dropdown(s) across ${SURFACES.length} surface(s)`)
  if (bad.length) {
    console.error(`\n${bad.length} popup(s) do not match their trigger width (tolerance ${TOLERANCE_PX}px).`)
    process.exit(1)
  }
  console.log('OK: every popup matches its trigger width')
}

main().catch(e => { console.error(e); process.exit(1) })

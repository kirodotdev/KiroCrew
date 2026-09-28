/**
 * Screen recording of the PINNED CREW CHIPS flow, for review evidence.
 *
 * A still frame cannot show that pinning is reversible, that the highlight
 * follows the active pane, or that an overflowing row drops chips WHOLE rather
 * than clipping one mid-word. This drives the real built SPA (website/dist) with
 * a stubbed API and records the whole sequence.
 *
 * It shares its fixtures and stub with `capture-crew-pin-chips.mjs`; that script
 * is the one that ASSERTS geometry. This one only films.
 *
 * Usage: npm run build && node scripts/record-crew-pin-chips.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, renameSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/crew-pin-chips'
const VIEWPORT = { width: 1280, height: 760 }
const TRIGGER = '[aria-label^="Switch crew"]'
const CHIP_ROW = '[data-testid="crew-chip-row"]'

const crew = (id, name, sshHost, port) => ({
  id,
  name,
  ssh_host: sshHost,
  remote_port: 7777,
  local_port: port,
  ttl: '20h',
  remote_bin: '',
  connection_method: 'ssh',
  ssm_target: '',
  ssm_run_as: '',
  aws_profile: '',
  aws_region: '',
  was_connected: false,
  status: { instance_id: id, state: 'connected', local_port: port, remote_port: 7777 },
})

const CREWS = [
  crew('devdesk', 'devdesk', 'dev-dsk-alias', 7801),
  crew('sandbox', 'sandbox', 'sandbox-alias', 7802),
  crew('prod', 'prod-us-east-1', 'prod-use1-alias', 7803),
  crew('staging', 'staging-eu-west-1', 'stg-euw1-alias', 7804),
]

const SSO = { state: 'ok', seconds_remaining: 72000, expires_at: null, reason: 'valid' }

/** See capture-crew-pin-chips.mjs — without a slot the command palette's recents
 *  provider maps a keyless placeholder and takes the shell down. */
const SLOTS = [{
  key: 'crew-pin-shot',
  title: 'Switching between crews',
  running: false,
  last_message: 'Pinned devdesk to the header.',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  folder_id: '',
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

/**
 * Dispatch a real pointer sequence at a selector, polled.
 *
 * Radix opens a dropdown on POINTERDOWN (so `el.click()` alone does nothing to
 * the trigger) and its rows act on click. Polling inside `waitForFunction` keeps
 * the lookup and the dispatch in one frame: the instances refetch hands the bar a
 * fresh array periodically, and a locator action would be waiting for a stability
 * that never arrives.
 */
async function press(page, selector, timeout = 15000) {
  await page.waitForFunction(sel => {
    const el = document.querySelector(sel)
    if (!(el instanceof HTMLElement)) return false
    const o = { bubbles: true, cancelable: true, button: 0, pointerId: 1, isPrimary: true }
    el.dispatchEvent(new PointerEvent('pointerdown', o))
    el.dispatchEvent(new PointerEvent('pointerup', o))
    el.click()
    return true
  }, selector, { timeout })
}

const chipCount = page =>
  page.evaluate(sel => document.querySelector(sel)?.children.length ?? 0, CHIP_ROW)

/** Press the chip whose label contains `label`. Chips are ordered by the crew
 *  list, not by pin order, so addressing them positionally goes stale as soon as
 *  the active crew changes and its chip leaves the row. */
async function pressChip(page, label) {
  await page.waitForFunction(([rowSel, want]) => {
    const row = document.querySelector(rowSel)
    if (!row) return false
    const el = [...row.children].find(c => (c.textContent || '').includes(want))
    if (!(el instanceof HTMLElement)) return false
    const o = { bubbles: true, cancelable: true, button: 0, pointerId: 1, isPrimary: true }
    el.dispatchEvent(new PointerEvent('pointerdown', o))
    el.dispatchEvent(new PointerEvent('pointerup', o))
    el.click()
    return true
  }, [CHIP_ROW, label], { timeout: 15000 })
}

async function main() {
  const { srv, base } = await serveDist()
  mkdirSync(OUT, { recursive: true })
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: VIEWPORT,
    recordVideo: { dir: OUT, size: VIEWPORT },
  })
  const page = await context.newPage()
  logPageProblems(page)
  await page.route(/127\.0\.0\.1:78\d\d/, route =>
    route.fulfill({ contentType: 'text/html', body: '<!doctype html><title>pane</title>' }),
  )
  await stubDashboardApi(page, {
    theme: 'dark',
    slots: SLOTS,
    extra: async (path, route) => {
      if (path === '/api/instances') {
        await json(route, { active: true, instances: CREWS, warm_set_cap: 5, sso: SSO })
        return true
      }
      const tunnel = /^\/api\/instances\/([^/]+)\/(connect|refresh-token)$/.exec(path)
      if (tunnel) {
        const id = decodeURIComponent(tunnel[1])
        const found = CREWS.find(c => c.id === id)
        await json(route, {
          ...(found ? found.status : { instance_id: id, state: 'connected' }),
          token: 'stub-token',
        })
        return true
      }
      if (path.startsWith('/api/instances/')) {
        await json(route, { ok: true })
        return true
      }
      return false
    },
  })

  await page.goto(`${base}/`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector(TRIGGER, { timeout: 20000 })
  // Settle: let the first instances poll land so the bar stops re-rendering
  // during the filmed interaction.
  await page.waitForTimeout(2500)

  const steps = []

  // 1. Open the dropdown — the default state, everything one click away.
  await press(page, TRIGGER)
  await page.waitForSelector('[data-testid="crew-pin-devdesk"]', { timeout: 10000 })
  await page.waitForTimeout(900)

  // 2. Pin Local plus two short-named crews. The menu stays open between them,
  //    so three pins are three clicks with no reopening. Local is pinned too:
  //    without a chip of its own there is no one-click way BACK from a remote
  //    pane, which is the whole point of the feature.
  await press(page, '[data-testid="crew-pin-__local__"]')
  await page.waitForTimeout(700)
  await press(page, '[data-testid="crew-pin-devdesk"]')
  await page.waitForTimeout(700)
  await press(page, '[data-testid="crew-pin-sandbox"]')
  await page.waitForTimeout(800)
  steps.push(['pinned three, active Local', await chipCount(page)])

  // 3. Close the menu: the two remote chips sit on the header. Local has no chip
  //    while it IS the active pane — the trigger already names it.
  await page.keyboard.press('Escape')
  await page.waitForTimeout(1200)

  // 4. Switch by clicking a chip. The trigger takes devdesk's name and Local,
  //    now inactive, takes a chip of its own.
  await pressChip(page, 'devdesk')
  await page.waitForTimeout(1500)
  steps.push(['switched to devdesk', await chipCount(page)])

  // 5. One click back to Local — the round trip that used to cost two dropdown
  //    openings.
  await pressChip(page, 'Local')
  await page.waitForTimeout(1500)
  steps.push(['back on Local', await chipCount(page)])

  // 6. Pin the two host-shaped names as well. Four chips no longer fit the
  //    budget, so the row drops the ones past it WHOLE rather than clipping one
  //    mid-word — and the centered search keeps its full width throughout.
  await press(page, TRIGGER)
  await page.waitForSelector('[data-testid="crew-pin-prod"]', { timeout: 10000 })
  await page.waitForTimeout(700)
  await press(page, '[data-testid="crew-pin-prod"]')
  await page.waitForTimeout(700)
  await press(page, '[data-testid="crew-pin-staging"]')
  await page.waitForTimeout(900)
  await page.keyboard.press('Escape')
  await page.waitForTimeout(1700)

  const geom = await page.evaluate(([rowSel]) => {
    const row = document.querySelector(rowSel)
    const kids = row ? [...row.children] : []
    const overlay = document.querySelector('[data-topbar-overlay]')
    const chevron = document.querySelector('[aria-label^="Switch crew"]')
    return {
      chips: kids.length,
      // Horizontal: the row is one nowrap line, so a chip is cut off once its
      // trailing edge passes the row's visible width.
      clipped: kids.filter(
        k => k.offsetLeft + k.offsetWidth > (row ? row.clientWidth : 0) + 1,
      ).length,
      // Must stay at the flex gap: the chevron sits against the last chip, which
      // is why the row clips rather than wraps.
      chevronGap:
        row && chevron
          ? Math.round(chevron.getBoundingClientRect().left - row.getBoundingClientRect().right)
          : null,
      overlayWidth: overlay ? Math.round(overlay.getBoundingClientRect().width) : 0,
    }
  }, [CHIP_ROW])
  steps.push([
    'overflowed', geom.chips, 'clipped', geom.clipped,
    'chevronGap', geom.chevronGap, 'overlay', geom.overlayWidth,
  ])

  // 7. Hold on the final frame so the last state is readable in the GIF.
  await page.waitForTimeout(1200)

  await page.close()
  await context.close()
  await browser.close()
  srv.close()

  // Playwright names the file by an internal id; give it a stable name.
  const webm = readdirSync(OUT).filter(f => f.endsWith('.webm')).sort().pop()
  if (!webm) throw new Error('playwright produced no video')
  const finalWebm = join(OUT, 'crew-pin-flow.webm')
  renameSync(join(OUT, webm), finalWebm)

  for (const s of steps) console.log('STEP', JSON.stringify(s))
  console.log('WEBM', finalWebm)

  if (geom.chips !== 4 || geom.clipped < 1) {
    console.error(`FAIL: expected an overflowing 4-chip row, got ${geom.chips} chips / ${geom.clipped} clipped`)
    process.exit(1)
  }
  if ((geom.chevronGap ?? 0) > 6) {
    console.error(`FAIL: the dropdown drifted ${geom.chevronGap}px from the last chip`)
    process.exit(1)
  }
  if (geom.overlayWidth < 240) {
    console.error(`FAIL: the search overlay ended at ${geom.overlayWidth}px — the row disturbed it`)
    process.exit(1)
  }
  console.log('OK')
}

main().catch(err => { console.error(err); process.exit(1) })

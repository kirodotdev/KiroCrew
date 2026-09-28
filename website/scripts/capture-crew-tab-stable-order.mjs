/**
 * Screenshot harness + behaviour check for the crew switcher's STABLE-ORDER
 * preference (`mc-crew-switcher-stable-order`).
 *
 * The switcher's default is to pull the crew ON SCREEN to a leading slot
 * (`tb-crew-active-chip`) and render the other pinned crews after it, so the row
 * reshuffles on every switch. A frequent switcher can opt into stable order: the
 * pinned crews then hold their configured order and the active one is only
 * highlighted in place. This harness photographs both, plus the dropdown toggle
 * that flips the preference.
 *
 * Runs against the REAL built SPA (website/dist) with a stubbed instances API —
 * the same approach as `capture-crew-pin-chips.mjs`, so the switcher measured
 * here is the one the header actually renders. Nothing in CI runs this file; the
 * ordering logic is unit-tested in `src/test/InstanceTabBar.test.tsx`.
 *
 * Usage: npm run build && node scripts/capture-crew-tab-stable-order.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/crew-tab-stable-order'

const VIEWPORT = { width: 1280, height: 760 }
const HEADER_CLIP = { x: 0, y: 0, width: VIEWPORT.width, height: 54 }

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

// Short names so all four chips (Local + three crews) fit at 1280px without
// clipping — clipping is `capture-crew-pin-chips`'s subject, not this one's.
const CREWS = [
  crew('devdesk', 'devdesk', 'dev-dsk-alias', 7801),
  crew('prod', 'prod', 'prod-alias', 7802),
  crew('staging', 'staging', 'stg-alias', 7803),
]

const SSO = { state: 'ok', seconds_remaining: 72000, expires_at: null, reason: 'valid' }

const SLOTS = [{
  key: 'stable-order-shot',
  title: 'Switching between crews',
  running: false,
  last_message: 'Pinned every crew to the header.',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  folder_id: '',
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const TRIGGER = '[aria-label^="Switch crew"]'
const CHIP_ROW = '[data-testid="crew-chip-row"]'
const ACTIVE_LEAD = '.tb-crew-active-chip'
const STABLE_TOGGLE = '[data-testid="crew-stable-order-toggle"]'
const PINNED_KEY = 'mc-crew-switcher-pinned'
const STABLE_KEY = 'mc-crew-switcher-stable-order'

// Local + all three crews pinned, so the row is fully populated and the active
// crew (prod, the middle one) has a real slot to hold under stable order.
const PINS = ['__local__', 'devdesk', 'prod', 'staging']
const ACTIVE = 'prod'

const results = []

async function main() {
  const { srv, base } = await serveDist()
  mkdirSync(OUT, { recursive: true })
  const browser = await chromium.launch()

  const extra = async (path, route) => {
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
  }

  /**
   * @param name       output file stem
   * @param stable     seed the stable-order preference on
   * @param openMenu   photograph the dropdown (where the toggle lives) instead of the header
   */
  async function scenario(name, { stable = false, openMenu = false } = {}) {
    const context = await browser.newContext({ viewport: VIEWPORT, deviceScaleFactor: 2 })
    const page = await context.newPage()
    logPageProblems(page)
    // A `connected` crew makes InstancesViewport mount a warm-pane iframe at its
    // forwarded port; nothing serves those here, so answer them a blank doc.
    await page.route(/127\.0\.0\.1:78\d\d/, route =>
      route.fulfill({ contentType: 'text/html', body: '<!doctype html><title>pane</title>' }),
    )
    await stubDashboardApi(page, { theme: 'dark', slots: SLOTS, extra })
    // Seed AFTER the stub (its init script clears localStorage first); the pin
    // and stable-order stores both read once at module import.
    await page.addInitScript(
      ([pinKey, pinVal, stableKey, stableVal]) => {
        localStorage.setItem(pinKey, pinVal)
        if (stableVal) localStorage.setItem(stableKey, stableVal)
      },
      [PINNED_KEY, JSON.stringify(PINS), STABLE_KEY, stable ? '1' : ''],
    )
    await page.goto(`${base}/`, { waitUntil: 'domcontentloaded' })
    await page.waitForSelector(TRIGGER, { timeout: 20000 })
    await page.waitForSelector(CHIP_ROW, { timeout: 10000 })

    // Two bars mount (the strip + the inline header bar); one is hidden via
    // display:none. Always drive the VISIBLE trigger, or a click can land on the
    // hidden one and time out.
    const trigger = page.locator(`${TRIGGER}:visible`).first()

    // Activate the middle crew by DRIVING the menu — the realistic path that also
    // proves selection works, rather than seeding activeId.
    await trigger.click()
    await page.waitForSelector('[role="menuitemradio"]', { timeout: 10000 })
    await page.click(`[role="menuitemradio"]:has-text("${ACTIVE}")`)
    await page.waitForTimeout(300)

    if (openMenu) {
      await trigger.click()
      await page.waitForSelector(STABLE_TOGGLE, { timeout: 10000 })
      await page.waitForTimeout(200)
      const menu = await page.evaluate((sel) => {
        const toggle = document.querySelector(sel)
        const content = toggle?.closest('[role="menu"]')
        const r = content?.getBoundingClientRect()
        return {
          toggleChecked: toggle?.getAttribute('aria-checked') ?? null,
          box: r
            ? { x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height) }
            : null,
        }
      }, STABLE_TOGGLE)
      results.push({ name, kind: 'menu', stable, toggleChecked: menu.toggleChecked })
      await page.screenshot({
        path: `${OUT}/${name}.png`,
        clip: menu.box
          ? { x: menu.box.x - 8, y: 0, width: menu.box.width + 16, height: menu.box.y + menu.box.height + 8 }
          : HEADER_CLIP,
      })
      await page.close()
      await context.close()
      return
    }

    const geom = await page.evaluate(
      ({ leadSel, rowSel, active }) => {
        const lead = document.querySelector(leadSel)
        const row = document.querySelector(rowSel)
        const chips = row ? [...row.children] : []
        const chipInfo = chips.map(c => ({
          text: (c.textContent || '').trim(),
          current: c.getAttribute('aria-current') === 'true',
        }))
        return {
          hasLead: !!lead,
          leadText: lead ? (lead.textContent || '').trim() : null,
          chipTexts: chipInfo.map(c => c.text),
          activeInRow: chipInfo.some(c => c.current && c.text.includes(active)),
        }
      },
      { leadSel: ACTIVE_LEAD, rowSel: CHIP_ROW, active: ACTIVE },
    )
    results.push({ name, kind: 'header', stable, ...geom })
    await page.screenshot({ path: `${OUT}/${name}.png`, clip: HEADER_CLIP })
    await page.close()
    await context.close()
  }

  // 1. Default (stable OFF): the active crew is pulled out to LEAD, and the row
  //    behind it reshuffles as you switch.
  await scenario('01-default-active-leads', { stable: false })
  // 2. Stable ON: the active crew holds its configured slot in the row and is only
  //    highlighted — the row never moves under a frequent switcher.
  await scenario('02-stable-order-in-place', { stable: true })
  // 3/4. The dropdown toggle that flips the preference, unchecked then checked.
  await scenario('03-menu-toggle-off', { stable: false, openMenu: true })
  await scenario('04-menu-toggle-on', { stable: true, openMenu: true })

  await browser.close()
  srv.close()

  for (const r of results) console.log(JSON.stringify(r))

  const byName = Object.fromEntries(results.map(r => [r.name, r]))

  // Default: the active crew leads and is NOT duplicated inside the row.
  const def = byName['01-default-active-leads']
  if (!def.hasLead || !def.leadText.includes(ACTIVE)) {
    console.error(`FAIL: default scenario should lead with the active crew (${ACTIVE}); got lead=${def.leadText}`)
    process.exit(1)
  }
  if (def.chipTexts.filter(t => t.includes(ACTIVE)).length !== 0) {
    console.error('FAIL: default scenario duplicated the active crew into the pinned row')
    process.exit(1)
  }

  // Stable: NO leading chip, the active crew sits in the row highlighted, and the
  // configured order is preserved.
  const st = byName['02-stable-order-in-place']
  if (st.hasLead) {
    console.error('FAIL: stable-order scenario still rendered a separate leading active chip')
    process.exit(1)
  }
  if (!st.activeInRow) {
    console.error('FAIL: stable-order scenario did not highlight the active crew inside the row')
    process.exit(1)
  }
  const stableActiveOrder = st.chipTexts.some(t => t.includes(ACTIVE))
  if (!stableActiveOrder || st.chipTexts.length < PINS.length) {
    console.error(`FAIL: stable-order row is missing pinned crews; got ${JSON.stringify(st.chipTexts)}`)
    process.exit(1)
  }

  // Menu toggle reflects the stored state.
  if (byName['03-menu-toggle-off'].toggleChecked !== 'false') {
    console.error('FAIL: dropdown toggle should read unchecked when stable order is off')
    process.exit(1)
  }
  if (byName['04-menu-toggle-on'].toggleChecked !== 'true') {
    console.error('FAIL: dropdown toggle should read checked when stable order is on')
    process.exit(1)
  }

  console.log('OK')
}

main().catch(err => { console.error(err); process.exit(1) })

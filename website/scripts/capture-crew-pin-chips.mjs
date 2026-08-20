/**
 * Screenshot harness + geometry check for PINNED CREW CHIPS in the top header.
 *
 * Photographs the states of the switcher (nothing pinned, short names pinned, and
 * a row that overflows and is clipped) and ASSERTS the invariant the design rests on: a pinned chip row never reaches
 * the centered top-bar search overlay, so the overlay keeps its full width and
 * never unmounts.
 *
 * A fourth scenario opens the menu instead, where the pin lives: one lit/unlit
 * pin per crew row. It asserts that each destination owns exactly one named pin
 * and that the lit ones are exactly the pinned crews.
 *
 * Crews are pinned by DRIVING THE UI, not by seeding localStorage: the dashboard
 * does not carry a pre-seeded value across the reload the store would need to
 * observe it, and clicking the real menu rows proves the interaction as a
 * side-effect instead of assuming it.
 *
 * Runs against the REAL built SPA (website/dist) with a stubbed API, so the
 * geometry measured here is what the header actually produces. Nothing in CI runs
 * this file; the CI-enforced half of the invariant is
 * `src/test/topbarLayout.test.ts`.
 *
 * Usage: npm run build && node scripts/capture-crew-pin-chips.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/crew-pin-chips'

/**
 * 1280px wide on purpose: an ordinary laptop width, and the one where a full
 * pinned row has to share the left grid track with the centered search column.
 */
const VIEWPORT = { width: 1280, height: 760 }

/** Just the header band — the rest of the shell is not what these prove. */
const HEADER_CLIP = { x: 0, y: 0, width: VIEWPORT.width, height: 54 }

const crew = (id, name, sshHost, port) => ({
  id,
  name,
  ssh_host: sshHost,
  remote_port: 7777,
  local_port: port,
  ttl: '20h',
  remote_bin: '',
  // The SSM-transport fields are not optional on the wire: the dashboard reads
  // them while deciding which lifecycle actions a row may offer, and omitting
  // them crashes the shell rather than degrading.
  connection_method: 'ssh',
  ssm_target: '',
  ssm_run_as: '',
  aws_profile: '',
  aws_region: '',
  // Deliberately false: `visibleInstanceTabs` admits a crew on a live
  // `status.state` alone, and setting the sticky-intent flag would make the
  // dashboard auto-connect and mount a cross-origin remote pane iframe — noise
  // this harness has no use for.
  was_connected: false,
  status: { instance_id: id, state: 'connected', local_port: port, remote_port: 7777 },
})

// A mix of short and host-shaped names, because the row's capacity is a pixel
// budget: the long ones are what make a four-crew row overflow where a
// four-short-name row would fit.
const CREWS = [
  crew('devdesk', 'devdesk', 'dev-dsk-alias', 7801),
  crew('prod', 'prod-us-east-1', 'prod-use1-alias', 7802),
  crew('staging', 'staging-eu-west-1', 'stg-euw1-alias', 7803),
  crew('sandbox', 'sandbox', 'sandbox-alias', 7804),
]

const SSO = { state: 'ok', seconds_remaining: 72000, expires_at: null, reason: 'valid' }

/**
 * One live session. Not decoration: with no slots the command palette's recents
 * provider maps over a placeholder that has no `key`, and `normalizeKey` takes
 * the whole shell down through its error boundary — which looks like a crash in
 * whatever is being photographed rather than a missing fixture.
 */
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

const TRIGGER = '[aria-label^="Switch crew"]'
const CHIP_ROW = '[data-testid="crew-chip-row"]'
const PIN_ITEM = '[data-testid^="crew-pin-"]'
const PINNED_KEY = 'mc-crew-switcher-pinned'

const results = []
/** Open-menu scenarios: the per-row pin toggles, which the header clip cannot see. */
const menuResults = []

async function main() {
  const { srv, base } = await serveDist()
  mkdirSync(OUT, { recursive: true })
  const browser = await chromium.launch()

  /**
   * Routes the shared stub does not know about. Each branch AWAITS `json()` then
   * returns `true`: the stub reads a falsy return as "not handled" and fulfils
   * the route itself, which would double-fulfil the request.
   *
   * The list route is matched EXACTLY. A `startsWith('/api/instances')` catch-all
   * also swallows `/{id}/connect`, answering it with the list payload — the
   * dashboard then reads `state` off a response that has none and the shell dies
   * inside its error boundary, with no page error to show for it.
   */
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
   * @param name    output file stem
   * @param pinIds  crews to pre-pin
   */
  async function scenario(name, pinIds, opts = {}) {    const context = await browser.newContext({ viewport: opts.viewport || VIEWPORT, deviceScaleFactor: 2 })
    const page = await context.newPage()
    logPageProblems(page)
    // A crew reported `connected` makes InstancesViewport mount a warm pane
    // iframe pointed at its forwarded port. Nothing serves those ports here, so
    // the iframe would load this same SPA cross-origin, trip on storage it is not
    // allowed to read, and take the shell down with it. Serve them a blank
    // document: the pane is not what these screenshots are about.
    await page.route(/127\.0\.0\.1:78\d\d/, route =>
      route.fulfill({ contentType: 'text/html', body: '<!doctype html><title>pane</title>' }),
    )
    await stubDashboardApi(page, { theme: 'dark', slots: SLOTS, extra })
    // Registered AFTER the stub, and this ordering is load-bearing: the stub's own
    // init script opens with `localStorage.clear()` to keep screenshots
    // deterministic, so anything seeded earlier — an earlier `addInitScript`, or
    // `storageState` — is wiped before the bundle reads it. Init scripts run in
    // registration order, so writing here lands after that clear. The pin store
    // reads storage once at module import, which is why the value has to be in
    // place before the first evaluation rather than set afterwards.
    await page.addInitScript(
      ([key, value]) => {
        localStorage.setItem(key, value)
      },
      [PINNED_KEY, JSON.stringify(pinIds)],
    )
    await page.goto(`${base}/`, { waitUntil: 'domcontentloaded' })

    // The switcher only mounts once the instances poll resolves.
    await page.waitForSelector(TRIGGER, { timeout: 20000 })
    if (pinIds.length) await page.waitForSelector(CHIP_ROW, { timeout: 10000 })


    await page.waitForTimeout(300)

    // The pin lives ON each crew's row in the open menu, so that state is only
    // photographable with the menu down — and the menu is portalled outside the
    // header, so it needs its own clip rather than HEADER_CLIP.
    if (opts.openMenu) {
      await page.click(TRIGGER)
      await page.waitForSelector(PIN_ITEM, { timeout: 10000 })
      await page.waitForTimeout(250)
      const menu = await page.evaluate(() => {
        const items = [...document.querySelectorAll('[data-testid^="crew-pin-"]')]
        const content = items[0]?.closest('[role="menu"]')
        const r = content?.getBoundingClientRect()
        return {
          pins: items.map(el => ({
            id: el.getAttribute('data-testid').replace('crew-pin-', ''),
            checked: el.getAttribute('aria-checked'),
            name: el.getAttribute('aria-label'),
            // Fill is the ONLY thing separating pinned from unpinned, so it is
            // read back rather than assumed: an outline pin on a pinned crew
            // reads as "not pinned" and invites a click that unpins it.
            filled: !!el.querySelector('svg')?.getAttribute('class')?.includes('fill-current'),
          })),
          // One switch target per crew: the old design listed every crew a second
          // time under a "Pin crews…" heading, which is what this replaces.
          destinations: content ? content.querySelectorAll('[role="menuitemradio"]').length : 0,
          // Same measure the geometry scenarios use: a chip whose trailing edge
          // passes the row's visible width is cut off, which is what puts a
          // pinned crew into the `noRoom` state this scenario exists to cover.
          chipsClipped: (() => {
            const row = document.querySelector('[data-testid="crew-chip-row"]')
            if (!row) return 0
            return [...row.children].filter(
              k => k.offsetLeft + k.offsetWidth > row.clientWidth + 1,
            ).length
          })(),
          box: r
            ? { x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height) }
            : null,
        }
      })
      menuResults.push({ name, expectedPinned: pinIds, requireClipped: !!opts.requireClipped, ...menu })
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

    // Geometry: does the switcher reach the centered search overlay?
    const geom = await page.evaluate(() => {
      const box = el => {
        if (!el) return null
        const r = el.getBoundingClientRect()
        return { left: Math.round(r.left), right: Math.round(r.right), width: Math.round(r.width) }
      }
      const row = document.querySelector('[data-testid="crew-chip-row"]')
      const chevron = document.querySelector('[aria-label^="Switch crew"]')
      const kids = row ? [...row.children] : []
      return {
        overlay: box(document.querySelector('[data-topbar-overlay]')),
        row: box(row),
        // The chevron TRAILS the chips, so it is the switcher's rightmost edge.
        trigger: box(chevron),
        chips: kids.length,
        // Clipping is horizontal: the row is one nowrap line, so a chip is cut off
        // once its trailing edge passes the row's visible width.
        chipsClipped: kids.filter(
          k => k.offsetLeft + k.offsetWidth > (row ? row.clientWidth : 0) + 1,
        ).length,
        // THE reason this layout clips instead of wrapping: the chevron has to sit
        // against the last visible chip. A wrapped row keeps its full ALLOCATED
        // width with the wrapped chips' space empty, pushing the chevron away by a
        // viewport-dependent gap. This is that gap, and it must stay at the flex
        // gap (4px).
        chevronGap:
          row && chevron
            ? Math.round(chevron.getBoundingClientRect().left - row.getBoundingClientRect().right)
            : null,
      }
    })

    const reach = Math.max(geom.trigger?.right ?? 0, geom.row?.right ?? 0)
    results.push({
      name,
      overlayPresent: !!geom.overlay,
      overlayWidth: geom.overlay?.width ?? 0,
      switcherReach: reach,
      overlayLeft: geom.overlay?.left ?? null,
      clearsOverlay: geom.overlay ? reach <= geom.overlay.left : null,
      chips: geom.chips,
      chipsClipped: geom.chipsClipped,
      chevronGap: geom.chevronGap,
    })

    await page.screenshot({
      path: `${OUT}/${name}.png`,
      // Clipped to the header band, at THIS scenario's width — a fixed 1280px clip
      // would overrun a narrow viewport and fail the capture outright.
      clip: { ...HEADER_CLIP, width: (opts.viewport || VIEWPORT).width },
    })
    await page.close()
    await context.close()
  }

  // 1. Nothing pinned — the default. No chip row at all, so a single-crew user
  //    pays no header width for the feature.
  await scenario('01-nothing-pinned', [])

  // 2. Two short names pinned — both fit, both one click away.
  await scenario('02-two-short-names-pinned', ['devdesk', 'sandbox'])

  // 3. All four pinned, two of them host-shaped — the row overflows and the chips
  //    that do not fit are cut at the row edge, marked by the fade. Narrow, because
  //    the chip row adapts to its own track: four host-shaped names fit at 1280px,
  //    where this scenario would photograph no cut and prove no fade.
  await scenario('03-overflow-clipped-with-fade', ['devdesk', 'prod', 'staging', 'sandbox'], {
    viewport: { width: 820, height: 760 },
  })

  // 4. The menu itself: every row carries its own pin, lit for the two pinned
  //    crews and unlit for the rest — one list of crews, not two.
  await scenario('04-menu-row-pins', ['devdesk', 'sandbox'], { openMenu: true })

  // 5. The same menu with the header row OVERFLOWING, so some pinned crews have
  //    no visible chip. That state must still render a FILLED pin: the only case
  //    where a pinned crew could be mistaken for an unpinned one, and the one
  //    jsdom cannot produce because it has no layout. Narrow on purpose — the chip
  //    row adapts to its own track, so four host-shaped names fit at 1280px.
  await scenario('05-menu-pins-with-clipped-chips', ['devdesk', 'prod', 'staging', 'sandbox'], {
    openMenu: true,
    viewport: { width: 820, height: 760 },
    requireClipped: true,
  })



  await browser.close()
  srv.close()

  console.log('--- geometry (the switcher must never reach the centered search overlay) ---')
  for (const r of results) console.log(JSON.stringify(r))
  console.log('--- open menu (one row per crew, each with its own pin state) ---')
  for (const r of menuResults) console.log(JSON.stringify(r))

  // Every destination owns exactly one pin, and it reads checked for exactly the
  // pinned crews. A screenshot of a pin that reports the wrong state would
  // document a feature that does not work.
  for (const m of menuResults) {
    const lit = m.pins.filter(p => p.checked === 'true').map(p => p.id).sort()
    const want = [...m.expectedPinned].sort()
    if (m.pins.length !== CREWS.length + 1 || m.destinations !== m.pins.length) {
      console.error(`FAIL: ${m.name} has ${m.pins.length} pins for ${m.destinations} destinations`)
      console.error('      (expected one pin per row, Local included, and no second crew list)')
      process.exit(1)
    }
    if (lit.join(',') !== want.join(',')) {
      console.error(`FAIL: ${m.name} lit ${lit.join(',') || '(none)'}, expected ${want.join(',')}`)
      process.exit(1)
    }
    if (m.pins.some(p => !p.name)) {
      console.error(`FAIL: ${m.name} has an unnamed pin — an icon-only control must say which crew it pins`)
      process.exit(1)
    }
    const unfilled = m.pins.filter(p => p.checked === 'true' && !p.filled).map(p => p.id)
    if (unfilled.length) {
      console.error(`FAIL: ${m.name} rendered pinned crew(s) ${unfilled.join(',')} WITHOUT fill.`)
      console.error('      An outline pin on a pinned crew reads as "not pinned" and invites an accidental unpin.')
      process.exit(1)
    }
    if (m.pins.some(p => p.checked === 'false' && p.filled)) {
      console.error(`FAIL: ${m.name} filled an UNPINNED pin — fill must mean pinned and nothing else`)
      process.exit(1)
    }
    if (m.requireClipped && m.chipsClipped === 0) {
      console.error(`FAIL: ${m.name} was meant to photograph pinned crews whose header chip is cut off,`)
      console.error('      but nothing clipped at this width, so the filled-while-clipped evidence is vacuous.')
      process.exit(1)
    }
  }

  const derived = results
  const bad = derived.filter(r => !r.overlayPresent || r.clearsOverlay !== true)
  const pinnedShots = derived.filter(r => r.name !== '01-nothing-pinned')

  if (bad.length) {
    console.error('FAIL: the derived bound let the switcher reach the search overlay:')
    for (const r of bad) console.error('  ' + JSON.stringify(r))
    process.exit(1)
  }
  if (pinnedShots.some(r => r.chips === 0)) {
    console.error('FAIL: a scenario pinned crews but rendered no chips — the UI path is broken,')
    console.error('      so these screenshots would document a feature that does not work.')
    process.exit(1)
  }
  // Clipping evidence may come from a header-only scenario OR from the open-menu
  // one: the chip row adapts to its own track, so four host-shaped names no longer
  // overflow at 1280px and only the narrow scenario reaches the clipped state.
  if (![...derived, ...menuResults].some(r => (r.chipsClipped ?? 0) > 0)) {
    console.error('FAIL: no scenario overflowed, so the clipping evidence is vacuous.')
    process.exit(1)
  }
  // The chip row is a flex sibling with a 4px gap, so anything materially larger
  // means the row is holding space it is not using — the wrapped-layout defect
  // this arrangement exists to avoid.
  const CHEVRON_GAP_MAX = 6
  const gappy = derived.filter(r => r.chips > 0 && (r.chevronGap ?? 0) > CHEVRON_GAP_MAX)
  if (gappy.length) {
    console.error(`FAIL: the dropdown drifted from the last chip (ceiling ${CHEVRON_GAP_MAX}px):`)
    for (const r of gappy) console.error('  ' + JSON.stringify(r))
    process.exit(1)
  }

  console.log('OK')
}

main().catch(err => { console.error(err); process.exit(1) })

/**
 * Before / after for "the pinned chip row adapts to its own track".
 *
 * The identity group's width is a pure function of the WINDOW: `.topbar`'s two
 * side tracks are both `minmax(0,1fr)` and each group carries
 * `container-type:inline-size`, which by design keeps its content out of the track
 * calculation (see scripts/measure-crew-chip-track.mjs for the numbers). So a
 * group short of room cannot borrow from the centred search — it has to absorb the
 * shortage internally. It now does that by letting every pinned chip give up name
 * width; before, the chips were `shrink-0` and whichever one landed on the clip
 * boundary was sliced through its unread badge.
 *
 * Runs against the REAL built SPA with a stubbed API. `before` is reproduced by
 * injecting `flex-shrink:0` back onto the chips, so both sides come from one build
 * and differ only in that one declaration.
 *
 * Assertions, per width:
 *  - before reproduces the defect: at least one chip is cut
 *  - after cuts strictly fewer chips, and every label still on screen is at least
 *    the 5ch floor wide (a name squeezed to one or two characters would not
 *    identify a crew, so past the floor the chip is cut instead)
 *  - THE SEARCH DOES NOT MOVE: same left edge, same width, still centred in the
 *    header's content box (what the three-track grid centres; the header's own
 *    padding is not symmetric, so window-relative would misreport it)
 *  - the dropdown trigger stays glued to the row (the gap is the flex gap)
 *
 * Usage: npm run build && node scripts/capture-crew-chip-shrink.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/crew-chip-shrink'
mkdirSync(OUT, { recursive: true })

/** The one declaration that separates the two states. */
const BEFORE_CSS = '.crew-chip-row > button{flex-shrink:0}'

const crew = (id, name, sshHost, port) => ({
  id, name, ssh_host: sshHost, remote_port: 7777, local_port: port, ttl: '20h',
  remote_bin: '', connection_method: 'ssh', ssm_target: '', ssm_run_as: '',
  aws_profile: '', aws_region: '', was_connected: false,
  status: { instance_id: id, state: 'connected', local_port: port, remote_port: 7777 },
})

const CREWS = [
  crew('devdesk', 'devdesk', 'dev-dsk-alias', 7801),
  crew('prod', 'prod-us-east-1', 'prod-use1-alias', 7802),
  crew('staging', 'staging-eu-west-1', 'stg-euw1-alias', 7803),
  crew('sandbox', 'sandbox', 'sandbox-alias', 7804),
]
const PINS = ['devdesk', 'prod', 'staging', 'sandbox']
const SSO = { state: 'ok', seconds_remaining: 72000, expires_at: null, reason: 'valid' }
const SLOTS = [{
  key: 'crew-chip-shrink', title: 'Pinned chips adapt to their track', running: false,
  last_message: '', messages: 2, agent: 'kirocrew', memory_mode: 'persistent',
  folder_id: '', modified: Math.floor(Date.now() / 1000), source_links: [], source_links_total: 0,
}]

/** 1280 is the reported case (49px short); 900 is a deeper shortage. */
const WIDTHS = [1280, 900]
const TRIGGER = '[aria-label^="Switch crew"]'
const CHIP_ROW = '[data-testid="crew-chip-row"]'
const PINNED_KEY = 'mc-crew-switcher-pinned'

let failures = 0
/** Widths where the fix strictly reduced the cut count, so the pair is not inert. */
const improved = []
const fail = (msg) => { console.error(`FAIL: ${msg}`); failures++ }
const near = (a, b, tol = 0.5) => Math.abs(a - b) <= tol

const extra = async (path, route) => {
  if (path === '/api/instances') {
    await json(route, { active: true, instances: CREWS, warm_set_cap: 5, sso: SSO })
    return true
  }
  const tunnel = /^\/api\/instances\/([^/]+)\/(connect|refresh-token)$/.exec(path)
  if (tunnel) {
    const found = CREWS.find((c) => c.id === decodeURIComponent(tunnel[1]))
    await json(route, { ...(found ? found.status : { state: 'connected' }), token: 'stub-token' })
    return true
  }
  if (path.startsWith('/api/instances/')) {
    await json(route, { ok: true })
    return true
  }
  return false
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()

async function shot(width, state) {
  const context = await browser.newContext({ viewport: { width, height: 300 } })
  const page = await context.newPage()
  await page.route(/127\.0\.0\.1:78\d\d/, (route) =>
    route.fulfill({ contentType: 'text/html', body: '<!doctype html><title>pane</title>' }))
  await stubDashboardApi(page, { theme: 'light', slots: SLOTS, extra })
  await page.addInitScript(([k, v]) => localStorage.setItem(k, v), [PINNED_KEY, JSON.stringify(PINS)])
  await page.goto(`${base}/`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector(TRIGGER, { timeout: 20000 })
  await page.waitForSelector(CHIP_ROW, { timeout: 10000 })
  if (state === 'before') await page.addStyleTag({ content: BEFORE_CSS })
  await page.waitForTimeout(250)

  const m = await page.evaluate(() => {
    const row = document.querySelector('[data-testid="crew-chip-row"]')
    const trig = document.querySelector('[aria-label^="Switch crew"]')
    const search = document.querySelector('[data-topbar-overlay]')
    const kids = [...row.children]
    const sb = search ? search.getBoundingClientRect() : null
    const rr = row.getBoundingClientRect()
    // The floor in real pixels, measured in the name's own font rather than
    // assumed: `ch` is the advance of the "0" glyph, which no constant can stand
    // in for across the theme's font choices.
    const probe = document.createElement('span')
    const nameEl = kids[0]?.querySelector('.tb-drop-crew-name')
    if (nameEl) {
      probe.style.cssText = 'position:absolute;visibility:hidden;width:5ch'
      probe.style.font = getComputedStyle(nameEl).font
      nameEl.parentElement.appendChild(probe)
    }
    const floorPx = nameEl ? probe.getBoundingClientRect().width : 0
    probe.remove()
    return {
      cut: kids.filter((k) => k.offsetLeft + k.offsetWidth > row.clientWidth + 1).length,
      chips: kids.length,
      // A chip whose name box has collapsed to nothing still shows its dot and
      // badge, so "on screen" is about the chip box, not the label.
      onScreen: kids.filter((k) => k.offsetLeft + k.offsetWidth <= row.clientWidth + 1).length,
      floorPx: +floorPx.toFixed(1),
      labels: kids.map((k) => k.querySelector('.tb-drop-crew-name')?.textContent ?? ''),
      labelWidths: kids.map((k) =>
        Math.round(k.querySelector('.tb-drop-crew-name')?.getBoundingClientRect().width ?? 0)),
      // Labels belonging to chips that are actually on screen — the only ones a
      // reader can be asked to identify a crew from.
      visibleLabelWidths: kids
        .filter((k) => k.offsetLeft + k.offsetWidth <= row.clientWidth + 1)
        .map((k) =>
          Math.round(k.querySelector('.tb-drop-crew-name')?.getBoundingClientRect().width ?? 0)),
      // The declared floor vs the measured one. The chip's floor is written in the
      // component as `calc(5ch + <fixed>px)` arithmetic over its own classes; if a
      // class changes and the arithmetic does not, the two diverge here.
      floors: kids.map((k) => {
        const n = k.querySelector('.tb-drop-crew-name')
        if (!n) return null
        const declared = parseFloat(getComputedStyle(k).minWidth)
        if (!Number.isFinite(declared) || declared === 0) return null
        // Every part of a chip except the name is `shrink-0`, so chip width minus
        // name width IS the fixed half, whatever state the chip is in.
        const fixed = k.getBoundingClientRect().width - n.getBoundingClientRect().width
        return { declared: +declared.toFixed(1), measuredFixed: +fixed.toFixed(1) }
      }).filter(Boolean),
      // A chip squeezed below its own content paints the name outside its border.
      // Count the chips whose label is wider than the chip's inner box.
      nameOverflows: kids.filter((k) => {
        const n = k.querySelector('.tb-drop-crew-name')
        if (!n) return false
        const cs = getComputedStyle(k)
        const inner = k.getBoundingClientRect().width
          - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight)
          - parseFloat(cs.borderLeftWidth) - parseFloat(cs.borderRightWidth)
        return n.getBoundingClientRect().width > inner + 0.5
      }).length,
      searchLeft: sb ? +sb.left.toFixed(1) : null,
      searchWidth: sb ? +sb.width.toFixed(1) : null,
      // Centred in the HEADER'S CONTENT BOX, which is what the three-track grid
      // actually centres -- not in the window. The header's own padding is not
      // symmetric (currently 8px left, 12px right), so a window-relative figure
      // reports a couple of px of offset that no change here owns.
      searchOffCentre: (() => {
        if (!sb) return null
        const h = document.querySelector('header')
        const hcs = getComputedStyle(h)
        const hr = h.getBoundingClientRect()
        const centre = ((hr.left + parseFloat(hcs.paddingLeft))
          + (hr.right - parseFloat(hcs.paddingRight))) / 2
        return +((sb.left + sb.width / 2) - centre).toFixed(1)
      })(),
      chevronGap: trig ? Math.round(trig.getBoundingClientRect().left - rr.right) : null,
      dataCut: row.getAttribute('data-cut'),
    }
  })
  await page.screenshot({ path: `${OUT}/${state}-${width}.png`, clip: { x: 0, y: 0, width, height: 46 } })
  await page.close()
  await context.close()
  return m
}

for (const width of WIDTHS) {
  const before = await shot(width, 'before')
  const after = await shot(width, 'after')
  console.log(`\n=== ${width}px, ${PINS.length} crews pinned ===`)
  for (const [name, m] of [['before', before], ['after', after]]) {
    console.log(`  ${name}: ${m.onScreen}/${m.chips} chips on screen, ${m.cut} cut`)
    console.log(`    labels ${JSON.stringify(m.labels)} widths ${JSON.stringify(m.labelWidths)}`)
    console.log(`    search left=${m.searchLeft} width=${m.searchWidth} offCentre=${m.searchOffCentre}` +
      `  chevronGap=${m.chevronGap}px`)
  }
  console.log(`  5ch floor measures ${after.floorPx}px; visible labels after: ` +
    `${JSON.stringify(after.visibleLabelWidths)}; names overflowing their chip: ` +
    `before ${before.nameOverflows}, after ${after.nameOverflows}`)

  if (after.nameOverflows) {
    fail(`${width}px: ${after.nameOverflows} name(s) paint outside their chip border — ` +
      `the chip was squeezed below its own content`)
  }
  // The floor is hand-written arithmetic over the chip's own classes; this is what
  // stops it drifting from them.
  for (const f of after.floors) {
    const impliedFixed = +(f.declared - after.floorPx).toFixed(1)
    if (Math.abs(impliedFixed - f.measuredFixed) > 1) {
      fail(`${width}px: a chip declares min-width ${f.declared}px = 5ch(${after.floorPx}) + ` +
        `${impliedFixed}px fixed, but its fixed parts measure ${f.measuredFixed}px — ` +
        `the floor arithmetic has drifted from the chip's classes`)
    }
  }

  console.log(`  declared vs measured floors: ${JSON.stringify(after.floors)}`)

  if (before.cut === 0) {
    fail(`${width}px: before shows no cut, so this pair does not reproduce the defect`)
  }
  if (after.cut > before.cut) {
    fail(`${width}px: after cuts ${after.cut}, MORE than before's ${before.cut}`)
  }
  if (after.cut < before.cut) improved.push(width)
  // The floor is the whole point of choosing 5ch over 0: a label still on screen
  // has to be wide enough to identify a crew. 1px of tolerance for subpixel
  // layout; a name shorter than the floor is legitimately narrower than it.
  const tooNarrow = after.visibleLabelWidths.filter((w, i) => {
    const label = after.labels[i] ?? ''
    return w > 0 && w < after.floorPx - 1 && label.length > 5
  })
  if (tooNarrow.length) {
    fail(`${width}px: ${tooNarrow.length} visible label(s) below the ${after.floorPx}px floor ` +
      `(${JSON.stringify(tooNarrow)}) — a two-character name identifies no crew`)
  }
  // The user's constraint, asserted rather than asserted-by-eye.
  if (!near(before.searchLeft, after.searchLeft) || !near(before.searchWidth, after.searchWidth)) {
    fail(`${width}px: the search moved (left ${before.searchLeft}->${after.searchLeft}, ` +
      `width ${before.searchWidth}->${after.searchWidth}); it must not change`)
  }
  if (!near(after.searchOffCentre, 0, 1)) {
    fail(`${width}px: the search is ${after.searchOffCentre}px off the header's content centre`)
  }
  if (after.chevronGap !== 4) {
    fail(`${width}px: the dropdown drifted off the row (gap ${after.chevronGap}px, expected the 4px flex gap)`)
  }
}

await browser.close()
srv.close()
// At the deepest widths a shortage bigger than the names can absorb still cuts, by
// design -- the floor is there to stop a two-character label. But if NO width got
// better, the change did nothing and these screenshots prove nothing.
if (!improved.length) {
  fail('no width reduced its cut count; the change is inert')
}
console.log(`\nwidths that strictly improved: ${JSON.stringify(improved)}`)
if (failures) {
  console.error(`\n${failures} assertion failure(s)`)
  process.exit(1)
}
console.log('\nALL GREEN')

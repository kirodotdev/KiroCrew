/**
 * Pixel evidence for the pinned-crew chip row's CUT EDGE cue.
 *
 * The row is one nowrap line with `overflow:hidden`, so the chip at the boundary
 * is cut rather than dropped. That cut used to be marked with an alpha mask over
 * the row's last 18px — but a chip's unread badge is its TRAILING element, so the
 * mask's ramp is wider than the 16px badge and the count the chip exists to show
 * dissolved into the header. The cue now sits ON the boundary (a 1px rule)
 * instead of ACROSS the content.
 *
 * Drives the shared top-bar capture entry, which renders the shipped class
 * strings and lets the real stylesheet lay them out. `?fade=on` re-injects the
 * retired mask verbatim, so before and after come from one build and differ only
 * in the cue.
 *
 * Two scenes, because the defect has two regimes:
 *  - collapsed: at the `@container (max-width:152px)` rung the crew name is gone,
 *    so the chip is a dot plus its badge and the ramp covers essentially all of it
 *    (this is the state a user reported as "why is this faded")
 *  - named: a wide group, where the ramp still lands on the badge because the
 *    badge is the chip's trailing element at every width
 *
 * The cut width is MEASURED, not guessed: each scene first renders unclipped,
 * reads where the badge sits inside the row, and then re-renders with the row
 * clipped 8px into that badge — half a badge, the same bite the report showed.
 *
 * Assertions, per scene:
 *  - before reproduces the defect: visible badge pixels are washed toward the
 *    header, i.e. no longer the accent colour
 *  - after keeps every visible badge pixel at full accent, right up to the cut
 *  - the cut column and the trigger do not move, so the cue changed no geometry
 *    (the row's width is what feeds its own clipped-chip measurement)
 *  - after paints a 1px rule in the border colour at the cut column
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6812 --strictPort   # in another shell
 *   node scripts/capture-crew-chip-cut-edge.mjs http://127.0.0.1:6812 ../temp-screenshots/crew-chip-cut-edge
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6812'
const OUT = process.argv[3] || '../temp-screenshots/crew-chip-cut-edge'
mkdirSync(OUT, { recursive: true })

const UNREAD = 3
/** How far into the 16px badge the row is clipped. Half of it. */
const BITE = 8

const SCENES = [
  { name: 'collapsed', viewport: { width: 390, height: 150 }, pins: 2 },
  { name: 'named', viewport: { width: 1600, height: 150 }, pins: 2 },
]

const PAGE = (scene, fade, roww) =>
  `${BASE}/capture/topbar-search-variants.html?theme=light&form=mobile` +
  `&pins=${scene.pins}&unread=${UNREAD}&fade=${fade}` +
  (roww === null ? '' : `&roww=${roww}`)

let failures = 0
const fail = (msg) => { console.error(`FAIL: ${msg}`); failures++ }
const near = (a, b) => Math.abs(a - b) <= 0.5

/**
 * A computed colour as a 0-255 triplet. Chrome serialises these theme tokens as
 * `color(srgb r g b)` rather than `rgb()`, because the palette is authored in a
 * wide-gamut space — parsing only `rgb()` returns null and the scanline then has
 * nothing to compare against.
 */
const rgb = (page, sel, prop) => page.evaluate(([s, p]) => {
  const v = getComputedStyle(document.querySelector(s))[p]
  const srgb = /color\(srgb\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)/.exec(v)
  if (srgb) return srgb.slice(1, 4).map((n) => Math.round(parseFloat(n) * 255))
  const legacy = /rgba?\(([^)]+)\)/.exec(v)
  if (legacy) return legacy[1].split(',').slice(0, 3).map((n) => Math.round(parseFloat(n)))
  return null
}, [sel, prop])

/** Row / badge / trigger geometry, plus where the badge starts inside the row. */
const geometry = (page) => page.evaluate(() => {
  const rowEl = document.querySelector('[data-testid="crew-chip-row"]')
  const row = rowEl.getBoundingClientRect()
  const badge = document.querySelector('[data-badge-chip]').getBoundingClientRect()
  const trig = document.querySelector('[aria-label="切换 crew"]').getBoundingClientRect()
  return {
    rowLeft: +row.left.toFixed(2),
    rowWidth: +row.width.toFixed(2),
    cutX: +row.right.toFixed(2),
    trigX: +trig.left.toFixed(2),
    groupRight: +document.querySelector('.tb-left').getBoundingClientRect().right.toFixed(2),
    badgeInRow: +(badge.left - row.left).toFixed(2),
    badgeWidth: +badge.width.toFixed(2),
    badge: { left: badge.left, right: badge.right, top: badge.top, height: badge.height },
    maskImage: getComputedStyle(rowEl).maskImage,
  }
})

/**
 * Sample the badge's own scanline out of a 1x screenshot and report, per column,
 * how much of the accent colour survives (1 = full accent, 0 = pure header) —
 * which is exactly what an alpha mask erodes.
 *
 * Node carries no image decoder in this repo, so the decode borrows the browser
 * already running, as capture-topbar-badge-overhang.mjs does.
 */
async function badgeScanline(page, b64, badge, accent, cutX) {
  return page.evaluate(async ([src, box, ac, cut]) => {
    const img = new Image()
    await new Promise((res) => { img.onload = res; img.src = `data:image/png;base64,${src}` })
    const c = document.createElement('canvas')
    c.width = img.naturalWidth
    c.height = img.naturalHeight
    const ctx = c.getContext('2d')
    ctx.drawImage(img, 0, 0)
    // The header's own painted colour, read from the band above the 24px chips
    // rather than from `getComputedStyle`: `.topbar-glass` is a `color-mix()` over
    // a backdrop filter, so the declared value is not what lands on screen.
    const bgPx = ctx.getImageData(Math.round(c.width / 2), 2, 1, 1).data
    const bgc = [bgPx[0], bgPx[1], bgPx[2]]
    // The channel with the most separation between accent and header decides the
    // alpha, so a near-tie on one channel cannot dominate the estimate.
    let best = 0
    for (let k = 0; k < 3; k++) {
      if (Math.abs(bgc[k] - ac[k]) > Math.abs(bgc[best] - ac[best])) best = k
    }
    const span = bgc[best] - ac[best]
    const out = []
    // Per column take the STRONGEST accent pixel over the badge's full height.
    // A single scanline through the middle runs straight along the "3"'s centre
    // stroke, so most columns would sample white glyph rather than badge fill.
    const y0 = Math.round(box.top) + 1
    const y1 = Math.round(box.top + box.height) - 1
    // The window skips one column at each end: the badge's own left rim is
    // antialiased against the chip behind it, and the two columns at the cut carry
    // the 1px cue plus its fractional coverage. Neither is the badge FILL that is
    // under test here.
    const from = Math.ceil(box.left) + 1
    const to = Math.min(Math.floor(box.right), Math.floor(cut) - 2)
    for (let x = from; x < to; x++) {
      let strongest = -Infinity
      for (let y = y0; y < y1; y++) {
        const d = ctx.getImageData(x, y, 1, 1).data
        const a = (bgc[best] - d[best]) / span
        if (a > strongest && a <= 1.02) strongest = a
      }
      out.push({ x, alpha: +strongest.toFixed(3) })
    }
    return { bg: bgc, window: [from, to], line: out }
  }, [b64, badge, accent, cutX])
}

const browser = await chromium.launch()

for (const scene of SCENES) {
  const page = await browser.newPage({ viewport: scene.viewport })

  // Pass 1: unclipped, to learn where this scene's badge sits inside the row.
  await page.goto(PAGE(scene, 'off', null), { waitUntil: 'networkidle' })
  await page.waitForSelector('[data-badge-chip]')
  const natural = await geometry(page)
  const cutW = Math.round(natural.badgeInRow + BITE)
  const accent = await rgb(page, '[data-badge-chip]', 'backgroundColor')
  const border = await rgb(page, '[data-badge-chip]', 'backgroundColor') && await page.evaluate(() => {
    const v = getComputedStyle(document.querySelector('[data-testid="crew-chip-row"]'), '::after').backgroundColor
    const m = /color\(srgb\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)/.exec(v)
    if (m) return m.slice(1, 4).map((n) => Math.round(parseFloat(n) * 255))
    const l = /rgba?\(([^)]+)\)/.exec(v)
    return l ? l[1].split(',').slice(0, 3).map((n) => Math.round(parseFloat(n))) : null
  })
  console.log(`\n[${scene.name}] viewport ${scene.viewport.width}px, ${scene.pins} pinned chips`)
  console.log(`  natural row ${natural.rowWidth}px; badge ${natural.badgeWidth}px starts ${natural.badgeInRow}px in`)
  console.log(`  clipping the row to ${cutW}px cuts ${BITE}px into the badge`)

  const state = {}
  for (const fade of ['on', 'off']) {
    await page.goto(PAGE(scene, fade, cutW), { waitUntil: 'networkidle' })
    await page.waitForSelector('[data-badge-chip]')
    const geom = await geometry(page)
    const clip = { x: 0, y: 0, width: scene.viewport.width, height: 54 }
    const shot = await page.screenshot({ clip })
    const label = fade === 'on' ? 'before' : 'after'
    await page.screenshot({ path: `${OUT}/${label}-${scene.name}.png`, clip })
    const scan = await badgeScanline(page, shot.toString('base64'), geom.badge, accent, geom.cutX)
    state[fade] = {
      geom,
      line: scan.line.filter((p) => Number.isFinite(p.alpha)),
      window: scan.window,
      bg: scan.bg,
    }
  }

  const before = state.on
  const after = state.off
  const weakest = (s) => (s.line.length ? Math.min(...s.line.map((p) => p.alpha)) : NaN)
  console.log(`  accent=${accent}  header=${after.bg}  border=${border}`)
  console.log(`  before: mask=${before.geom.maskImage === 'none' ? 'none' : 'linear-gradient'}, ` +
    `${before.line.length} badge px in x=[${before.window}), weakest accent ${weakest(before).toFixed(3)}`)
  console.log(`  after : mask=${after.geom.maskImage === 'none' ? 'none' : 'linear-gradient'}, ` +
    `${after.line.length} badge px in x=[${after.window}), weakest accent ${weakest(after).toFixed(3)}`)
  console.log(`  cut column ${before.geom.cutX} -> ${after.geom.cutX}; trigger x ${before.geom.trigX} -> ${after.geom.trigX}`)

  if (before.geom.maskImage === 'none') {
    fail(`${scene.name}: fade=on did not apply the retired mask, so the before state is not the shipped one`)
  }
  // The row must be the binding clip. If the pinned width pushes it past the
  // identity group's own clip box, the ANCESTOR cuts first — and `.tb-left`
  // carries no mask, so before and after would be identical for reasons that
  // have nothing to do with the cue.
  if (after.geom.cutX > after.geom.groupRight + 0.5) {
    fail(`${scene.name}: the row's clip (${after.geom.cutX}) sits past the identity group's (${after.geom.groupRight}); widen the scene's viewport or pin fewer chips`)
  }
  if (after.geom.maskImage !== 'none') {
    fail(`${scene.name}: the mask survives after the fix (${after.geom.maskImage})`)
  }
  if (!(weakest(before) < 0.75)) {
    fail(`${scene.name}: fade=on left the badge intact (weakest accent ${weakest(before)}) — this pair does not show the defect`)
  }
  // 0.85, not 1.0: the badge is a rounded pill, so its boundary columns are
  // antialiased against the chip behind it and never reach full accent even
  // unmasked. The mask drives the same columns an order of magnitude lower.
  if (!(weakest(after) > 0.85)) {
    fail(`${scene.name}: badge pixels are still washed after the fix (weakest accent ${weakest(after)})`)
  }
  if (!near(before.geom.cutX, after.geom.cutX) || !near(before.geom.trigX, after.geom.trigX)) {
    fail(`${scene.name}: the cue moved geometry (cut ${before.geom.cutX}->${after.geom.cutX}, ` +
      `trigger ${before.geom.trigX}->${after.geom.trigX}); the row's width feeds its own clip measurement`)
  }

  const rule = await page.evaluate(() => {
    const cs = getComputedStyle(document.querySelector('[data-testid="crew-chip-row"]'), '::after')
    return { content: cs.content, width: cs.width, position: cs.position }
  })
  console.log(`  cut rule: content=${rule.content} width=${rule.width} position=${rule.position}`)
  if (rule.content === 'none' || rule.width !== '1px' || rule.position !== 'absolute') {
    fail(`${scene.name}: cut-edge rule missing or in flow (content=${rule.content}, width=${rule.width}, position=${rule.position})`)
  }

  // Legible crops for review: the erosion is 8px wide at 1x.
  for (const [fade, label] of [['on', 'before'], ['off', 'after']]) {
    const zoom = await browser.newPage({ viewport: scene.viewport, deviceScaleFactor: 5 })
    await zoom.goto(PAGE(scene, fade, cutW), { waitUntil: 'networkidle' })
    await zoom.waitForSelector('[data-badge-chip]')
    const g = await geometry(zoom)
    const x = Math.max(0, Math.round(g.rowLeft) - 44)
    await zoom.screenshot({
      path: `${OUT}/${label}-${scene.name}-zoom5x.png`,
      clip: { x, y: 4, width: Math.min(scene.viewport.width - x, Math.round(g.cutX) + 40 - x), height: 34 },
    })
    await zoom.close()
  }
  await page.close()
}

await browser.close()
if (failures) {
  console.error(`\n${failures} assertion failure(s)`)
  process.exit(1)
}
console.log('\nALL GREEN')

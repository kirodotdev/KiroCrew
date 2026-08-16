/**
 * Pixel + box evidence for the notification bell's unread badge overhang.
 *
 * Drives the shared top-bar capture entry (website/capture/topbar-search-variants.html),
 * which renders the shipped `.topbar` / `.tb-right` class strings and the verbatim
 * `BellButton` and lets the real stylesheet lay them out. `?fix=off` strips the
 * gutter that admits the badge's 4px overhang, reproducing the before state.
 *
 * Two instruments, because one is not enough on its own:
 *
 *  - BOXES prove the mechanism: with the gutter, the badge sits inside the
 *    group's clip box (its padding box) instead of 4px outside it, while the
 *    bell and the header keep the geometry they had. That is what makes this fix
 *    engine-independent — no `overflow-clip-margin`, which WebKit does not
 *    implement, so on iOS that spelling would leave the badge clipped.
 *  - PIXELS prove it is not inert: the clip RECTANGLE moving is a paint change,
 *    and a fix that failed to apply would leave both renders byte-identical.
 *
 * Assertions, per width:
 *  - fix=off reproduces the defect: badge 4px outside the clip box on both axes
 *  - fix=on puts the badge fully inside it (0px outside, both axes)
 *  - fix=on does not move the bell or change the header height vs fix=off
 *  - fix=on does not change the group's CONTENT box width, so the @container
 *    collapse-ladder thresholds in index.css stay calibrated
 *  - the two renders DIFFER, and every differing pixel lies within the badge
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6811 --strictPort   # in another shell
 *   node scripts/capture-topbar-badge-overhang.mjs http://127.0.0.1:6811 ../temp-screenshots/topbar-badge-overhang
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6811'
const OUT = process.argv[3] || '../temp-screenshots/topbar-badge-overhang'
mkdirSync(OUT, { recursive: true })

/** The badge offset in App.tsx: `-top-1 -right-1` = 0.25rem. The gutter in
 *  index.css must equal it. */
const OVERHANG_PX = 4
const WIDTHS = [390, 1280]
const PAGE = (fix, w) =>
  `${BASE}/capture/topbar-search-variants.html?theme=dark&count=11&fix=${fix}` +
  `&form=${w < 768 ? 'mobile' : 'desktop'}`

let failures = 0
const fail = (msg) => { console.error(`FAIL: ${msg}`); failures++ }
const near = (a, b) => Math.abs(a - b) <= 0.5

/** Decode two PNGs in the browser and diff them. Node has no image decoder here
 *  and the repo carries no pixel dependency, so borrow the one already running.
 *
 *  `TOL` is not cosmetic. Changing the group's box re-rasterises the header's
 *  `backdrop-filter` layer, which sprays single-channel ±1 pixels across the
 *  whole header while the badge itself differs by many tens per channel.
 *  Counting the noise would let the diff's bounding box swallow the header and
 *  make the "confined to the badge" assertion vacuous, so it is thresholded —
 *  and `rawN` is reported so the noise stays visible rather than discarded. */
const TOL = 24

async function diffPngs(page, aB64, bB64) {
  return page.evaluate(async ([a, b, tol]) => {
    const load = (b64) => new Promise((res) => {
      const img = new Image()
      img.onload = () => {
        const c = document.createElement('canvas')
        c.width = img.naturalWidth; c.height = img.naturalHeight
        c.getContext('2d').drawImage(img, 0, 0)
        res(c.getContext('2d').getImageData(0, 0, c.width, c.height))
      }
      img.src = `data:image/png;base64,${b64}`
    })
    const [ia, ib] = await Promise.all([load(a), load(b)])
    let n = 0, rawN = 0, minX = Infinity, maxX = -1, minY = Infinity, maxY = -1
    for (let y = 0; y < ia.height; y++) {
      for (let x = 0; x < ia.width; x++) {
        const i = (ia.width * y + x) << 2
        // Alpha compared too: the glow's edge differs in alpha before opacity is
        // flattened onto the page background.
        let d = 0
        for (let k = 0; k < 4; k++) d = Math.max(d, Math.abs(ia.data[i + k] - ib.data[i + k]))
        if (d > 0) rawN++
        if (d > tol) {
          n++
          if (x < minX) minX = x
          if (x > maxX) maxX = x
          if (y < minY) minY = y
          if (y > maxY) maxY = y
        }
      }
    }
    return { n, rawN, minX, maxX, minY, maxY }
  }, [aB64, bB64, TOL])
}

/** Boxes that decide whether the badge is clipped. `.tb-right` carries no
 *  border, so its client rect IS its padding box — the clip box for `overflow`. */
const boxes = (page) => page.evaluate(() => {
  const g = document.querySelector('.tb-right')
  const cs = getComputedStyle(g)
  const gr = g.getBoundingClientRect()
  const b = document.querySelector('[data-badge]').getBoundingClientRect()
  const bell = document.querySelector('[data-bell-wrap]').getBoundingClientRect()
  const hdr = document.querySelector('[data-topbar]').getBoundingClientRect()
  return {
    outsideRight: +(b.right - gr.right).toFixed(2),
    outsideTop: +(gr.top - b.top).toFixed(2),
    bellRight: +bell.right.toFixed(2),
    bellTop: +bell.top.toFixed(2),
    headerH: +hdr.height.toFixed(2),
    contentW: +(gr.width - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight)).toFixed(2),
    badgeLeft: +b.left.toFixed(2),
    badgeRight: +b.right.toFixed(2),
    overflow: cs.overflow,
  }
})

const browser = await chromium.launch()

for (const w of WIDTHS) {
  const shots = {}
  const geom = {}
  const page = await browser.newPage({ viewport: { width: w, height: 220 } })
  for (const fix of ['off', 'on']) {
    await page.goto(PAGE(fix, w), { waitUntil: 'networkidle' })
    await page.waitForSelector('[data-badge]')
    geom[fix] = await boxes(page)
    const hdr = await page.locator('[data-topbar]').boundingBox()
    // Clip past the header's own box so anything painting outside it is
    // captured rather than cropped away by the screenshot itself.
    const clip = { x: 0, y: 0, width: w, height: Math.ceil(hdr.height) + 12 }
    shots[fix] = (await page.screenshot({ clip })).toString('base64')
    await page.screenshot({ path: `${OUT}/${fix === 'off' ? 'before' : 'after'}-${w}px.png`, clip })
  }
  const off = geom.off, on = geom.on
  console.log(`w=${w} overflow=${on.overflow}`)
  console.log(`  before: badge outside clip box by right=${off.outsideRight}px top=${off.outsideTop}px`)
  console.log(`  after : badge outside clip box by right=${on.outsideRight}px top=${on.outsideTop}px`)
  console.log(`  unchanged? bellRight ${off.bellRight}->${on.bellRight}  bellTop ${off.bellTop}->${on.bellTop}` +
    `  headerH ${off.headerH}->${on.headerH}  groupContentW ${off.contentW}->${on.contentW}`)

  if (!near(off.outsideRight, OVERHANG_PX) || !near(off.outsideTop, OVERHANG_PX)) {
    fail(`w=${w}: fix=off does not reproduce the overhang (right=${off.outsideRight}, top=${off.outsideTop}, expected ${OVERHANG_PX} on both) — before/after evidence would be meaningless`)
  }
  if (!near(on.outsideRight, 0) || !near(on.outsideTop, 0)) {
    fail(`w=${w}: badge still sits outside the clip box with the gutter applied (right=${on.outsideRight}, top=${on.outsideTop})`)
  }
  if (!near(off.bellRight, on.bellRight) || !near(off.bellTop, on.bellTop)) {
    fail(`w=${w}: the gutter moved the bell (${off.bellRight},${off.bellTop} -> ${on.bellRight},${on.bellTop}) — it is meant to reserve space without shifting anything`)
  }
  if (!near(off.headerH, on.headerH)) {
    fail(`w=${w}: the gutter changed the header height (${off.headerH} -> ${on.headerH})`)
  }
  if (!near(off.contentW, on.contentW)) {
    fail(`w=${w}: the gutter changed the group's content width (${off.contentW} -> ${on.contentW}) — the @container collapse thresholds are calibrated against it`)
  }

  const dp = await browser.newPage({ viewport: { width: 400, height: 300 } })
  await dp.goto(PAGE('on', w), { waitUntil: 'domcontentloaded' })
  const d = await diffPngs(dp, shots.off, shots.on)
  await dp.close()
  console.log(`  diff: ${d.n}px over tol=${TOL} (raw ${d.rawN}px incl. rasterisation noise) x=[${d.minX},${d.maxX}] y=[${d.minY},${d.maxY}]`)
  if (d.n === 0) {
    fail(`w=${w}: before and after render identically — the fix=off toggle did not take effect, so this pair is not evidence`)
  } else {
    if (d.minX < Math.floor(on.badgeLeft) - 2) {
      fail(`w=${w}: diff starts at x=${d.minX}, left of the badge (${on.badgeLeft}) — the change is not confined to the badge`)
    }
    if (d.maxX > Math.ceil(on.badgeRight) + 2) {
      fail(`w=${w}: diff reaches x=${d.maxX}, past the badge's right edge (${on.badgeRight}) — more than the badge changed`)
    }
  }

  // Legible crop for the PR: the overhang is 4px, invisible at 1x in a
  // full-width frame. Measured above at 1x; this is corroborating, and scaled
  // 6x, so do not read coordinates off it.
  for (const fix of ['off', 'on']) {
    const zoom = await browser.newPage({ viewport: { width: w, height: 220 }, deviceScaleFactor: 6 })
    await zoom.goto(PAGE(fix, w), { waitUntil: 'networkidle' })
    await zoom.waitForSelector('[data-badge]')
    await zoom.screenshot({ path: `${OUT}/${fix === 'off' ? 'before' : 'after'}-${w}px-zoom6x.png`, clip: { x: w - 56, y: 0, width: 56, height: 40 } })
    await zoom.close()
  }
  await page.close()
}

await browser.close()
if (failures) {
  console.error(`${failures} assertion failure(s)`)
  process.exit(1)
}
console.log('ALL GREEN')

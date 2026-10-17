/**
 * Recording harness: do the rows of the folder that RISES into view on a
 * collapse paint in the same frame as its header?
 *
 * Collapsing a tall folder from its pinned header closes it in one frame, and
 * the next folder comes up under the pinned header at once. Its header paints
 * on that frame. Its session rows should too: a lane that shows a folder header
 * over empty space for a few frames reads as a flash, and it happens on the
 * reduced-motion path as well, so it is not an effect of the close transition.
 *
 * Runs the REAL built SPA on the scene `lib/folder-collapse-scene.mjs` fixes
 * (shared with scripts/capture-sticky-folder-collapse.mjs): two folders of 40
 * sessions, the lane scrolled 400px into "alpha folder" so its header is
 * pinned, then a click on that header's collapse.
 * Every compositor frame across the commit is kept (screencast, not DOM: the
 * DOM reports rows present on frames where nothing has painted yet).
 *
 * Per frame it measures two boxes, taken from the settled DOM after the
 * collapse: the next folder's header, and the lane below it down to the first
 * three of its rows. A box has PAINTED when its pixels match the settled frame
 * (mean channel difference under PAINTED_MAX_DIFF). The verdict is the number of
 * frames on which the header has painted and the rows have not, and the
 * filmstrip shows those frames so the number can be checked by eye.
 *
 * Usage: node scripts/capture-collapse-rising-rows.mjs <baseUrl> <outDir> <label> [reduce] [light]
 * Writes <outDir>/<label>-filmstrip.png and <outDir>/<label>.json, prints one
 * verdict line, and exits 1 when any frame shows the header over blank rows.
 * `reduce` runs the page with prefers-reduced-motion; `light` uses the light
 * theme (the fixtures default to dark).
 */
import { chromium } from 'playwright'
import { mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { routeFolderCollapseScene } from './lib/folder-collapse-scene.mjs'

const BASE = process.argv[2] || 'http://127.0.0.1:7824'
const OUT = process.argv[3] || '../temp-screenshots/18121-collapse-rising-rows'
const LABEL = process.argv[4] || 'fix'
const flags = new Set(process.argv.slice(5))
const REDUCE = flags.has('reduce')
const THEME = flags.has('light') ? 'light' : 'dark'
mkdirSync(OUT, { recursive: true })

/** A box counts as painted like the settled frame below this mean per-channel
 *  difference (0..255). Anti-aliasing and the video encoder's own noise sit
 *  well under 4; a missing row of text sits well over 10. */
const PAINTED_MAX_DIFF = 6

const browser = await chromium.launch({ env: { ...process.env, LD_LIBRARY_PATH: '/usr/lib64' } })
try {
  const context = await browser.newContext({
    viewport: { width: 1400, height: 900 },
    reducedMotion: REDUCE ? 'reduce' : 'no-preference',
    colorScheme: THEME,
  })
  const page = await context.newPage()
  await routeFolderCollapseScene(page, { theme: THEME })
  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 240)))
  await page.addInitScript(theme => {
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('kc-onboarded', '1')
    localStorage.setItem('mc-theme', theme)
  }, THEME)
  await page.goto(`${BASE}/chat`, { waitUntil: 'domcontentloaded' })
  const lane = page.getByTestId('tree-view-lane')
  await lane.waitFor({ timeout: 20_000 })
  await page.getByText('beta session 39').first().waitFor({ state: 'attached', timeout: 20_000 })
  await lane.evaluate(el => { el.scrollTop = 400 })
  await page.waitForTimeout(1200)

  const cdp = await context.newCDPSession(page)
  const frames = []
  let recording = false
  cdp.on('Page.screencastFrame', async f => {
    if (recording) frames.push({ ms: f.metadata.timestamp * 1000, data: f.data })
    await cdp.send('Page.screencastFrameAck', { sessionId: f.sessionId }).catch(() => {})
  })
  await cdp.send('Page.startScreencast', { format: 'png', everyNthFrame: 1 })
  await page.waitForTimeout(300)
  recording = true
  await page.waitForTimeout(150)
  // Anchor on the commit (the header's aria-expanded flipping), not the click:
  // headless Chromium commits a few hundred ms after the click.
  await page.evaluate(() => {
    const btn = document.querySelector('[data-folder-drop="f-a"] > [data-folder-row] [aria-expanded]')
    new MutationObserver((_, obs) => {
      if (btn.getAttribute('aria-expanded') === 'false') { window.__committedAt = Date.now(); obs.disconnect() }
    }).observe(btn, { attributes: true })
  })
  await page.getByRole('button', { name: /collapse folder alpha folder/i }).click()
  await page.waitForFunction(() => window.__committedAt, null, { timeout: 10_000 })
  await page.waitForTimeout(800)
  recording = false
  await cdp.send('Page.stopScreencast')
  const committedAt = await page.evaluate(() => window.__committedAt)

  // The two boxes, from the settled layout: the risen folder's header, and the
  // lane under it down to the bottom of its third row.
  const boxes = await page.evaluate(() => {
    const r = el => { const b = el.getBoundingClientRect(); return { x: b.left, y: b.top, w: b.width, h: b.height } }
    const header = r(document.querySelector('[data-folder-drop="f-b"] > [data-folder-row]'))
    const rows = [...document.querySelectorAll('[data-folder-drop="f-b"] [data-session-row]')].slice(0, 3).map(r)
    if (rows.length === 0) throw new Error('no session rows under "beta folder" after the collapse')
    const bottom = Math.max(...rows.map(b => b.y + b.h))
    return { header, rows: { x: header.x, y: header.y + header.h, w: header.w, h: bottom - (header.y + header.h) } }
  })
  const laneBox = await lane.boundingBox()
  await context.close()

  // Measure every frame against the last one in a canvas page: the screencast
  // frames are PNGs already, so the browser decodes them.
  const tiles = frames.filter(f => f.ms >= committedAt - 60 && f.ms <= committedAt + 400)
  if (frames.length === 0 || tiles.length === 0) throw new Error(`screencast delivered ${frames.length} frame(s), ${tiles.length} across the commit: nothing to measure`)
  const meter = await browser.newPage({ viewport: { width: 1400, height: 900 } })
  const measured = await meter.evaluate(async ({ tiles, boxes, settled }) => {
    const load = data => new Promise(res => { const img = new Image(); img.onload = () => res(img); img.src = `data:image/png;base64,${data}` })
    const canvas = document.createElement('canvas')
    canvas.width = 1400; canvas.height = 900
    const ctx = canvas.getContext('2d', { willReadFrequently: true })
    const read = (img, box) => {
      ctx.clearRect(0, 0, canvas.width, canvas.height)
      ctx.drawImage(img, 0, 0)
      return ctx.getImageData(Math.round(box.x), Math.round(box.y), Math.round(box.w), Math.round(box.h)).data
    }
    const diff = (a, b) => {
      let sum = 0
      for (let i = 0; i < a.length; i += 4) sum += Math.abs(a[i] - b[i]) + Math.abs(a[i + 1] - b[i + 1]) + Math.abs(a[i + 2] - b[i + 2])
      return sum / (a.length / 4 * 3)
    }
    // Share of pixels that are not the box's dominant colour: text, borders,
    // glyphs. Near zero means the box is one flat colour, i.e. nothing drawn.
    const ink = a => {
      const counts = new Map()
      for (let i = 0; i < a.length; i += 4) { const k = (a[i] << 16) | (a[i + 1] << 8) | a[i + 2]; counts.set(k, (counts.get(k) || 0) + 1) }
      const [bg] = [...counts.entries()].sort((p, q) => q[1] - p[1])[0]
      const br = bg >> 16 & 255, bgc = bg >> 8 & 255, bb = bg & 255
      let n = 0
      for (let i = 0; i < a.length; i += 4) if (Math.abs(a[i] - br) + Math.abs(a[i + 1] - bgc) + Math.abs(a[i + 2] - bb) > 30) n++
      return n / (a.length / 4)
    }
    const crop = box => { const c = document.createElement('canvas'); c.width = Math.round(box.w); c.height = Math.round(box.h); c.getContext('2d').drawImage(canvas, Math.round(box.x), Math.round(box.y), c.width, c.height, 0, 0, c.width, c.height); return c.toDataURL('image/png').split(',')[1] }
    const ref = await load(settled)
    const refHeader = read(ref, boxes.header)
    const refRows = read(ref, boxes.rows)
    const out = []
    for (const t of tiles) {
      const img = await load(t.data)
      const rows = read(img, boxes.rows)
      out.push({ ms: t.ms, header: diff(read(img, boxes.header), refHeader), rows: diff(rows, refRows), rowsInk: ink(rows), crop: crop({ x: boxes.header.x, y: boxes.header.y, w: boxes.header.w, h: boxes.header.h + boxes.rows.h }) })
    }
    return out
  }, { tiles, boxes, settled: frames[frames.length - 1].data })
  await meter.close()

  const verdicts = measured.map(m => ({
    ms: Math.round(m.ms - committedAt),
    headerPainted: m.header < PAINTED_MAX_DIFF,
    rowsPainted: m.rows < PAINTED_MAX_DIFF,
    headerDiff: +m.header.toFixed(1),
    rowsDiff: +m.rows.toFixed(1),
    rowsInk: +m.rowsInk.toFixed(3),
  }))
  // The frames in question, cropped to the risen folder, so "blank" can be
  // checked against pixels and not only against the numbers.
  measured.forEach((m, i) => {
    const v = verdicts[i]
    if (v.headerPainted && !v.rowsPainted) writeFileSync(join(OUT, `${LABEL}-blank-${v.ms}ms.png`), Buffer.from(m.crop, 'base64'))
  })
  writeFileSync(join(OUT, `${LABEL}-settled.png`), Buffer.from(measured[measured.length - 1].crop, 'base64'))
  const blank = verdicts.filter(v => v.headerPainted && !v.rowsPainted)
  const firstHeader = verdicts.find(v => v.headerPainted)
  const firstRows = verdicts.find(v => v.rowsPainted && v.headerPainted)

  // Filmstrip of the measured frames, the top of the lane only, each labelled
  // with its time from the commit and the two verdicts.
  const cell = { x: Math.round(laneBox.x), y: Math.round(laneBox.y), w: Math.round(laneBox.width), h: 220 }
  const strip = await browser.newPage({ viewport: { width: 1600, height: 400 } })
  await strip.setContent(`<body style="margin:0;background:#555;display:flex;flex-wrap:wrap;gap:4px;width:${(cell.w + 4) * 6}px">${tiles.map((t, i) => {
    const v = verdicts[i]
    const tag = v.headerPainted && !v.rowsPainted ? 'HEADER, ROWS BLANK' : v.headerPainted ? 'header + rows' : 'before'
    return `
    <div style="width:${cell.w}px;height:${cell.h + 18}px;overflow:hidden;background:#000;font:12px monospace;color:${v.headerPainted && !v.rowsPainted ? '#f66' : '#ff0'}">
      <div style="height:18px;padding-left:4px">${v.ms} ms  ${tag}</div>
      <div style="width:${cell.w}px;height:${cell.h}px;overflow:hidden;position:relative">
        <img src="data:image/png;base64,${t.data}" style="position:absolute;left:${-cell.x}px;top:${-cell.y}px">
      </div></div>`
  }).join('')}</body>`)
  await strip.locator('body').screenshot({ path: join(OUT, `${LABEL}-filmstrip.png`) })
  await strip.close()

  const report = { label: LABEL, theme: THEME, reducedMotion: REDUCE, boxes, blankFrames: blank.length, firstHeaderMs: firstHeader?.ms ?? null, firstRowsMs: firstRows?.ms ?? null, frames: verdicts }
  writeFileSync(join(OUT, `${LABEL}.json`), JSON.stringify(report, null, 2))
  console.log(`${LABEL} (${THEME}${REDUCE ? ', reduced motion' : ''}): header painted at ${firstHeader?.ms} ms, rows at ${firstRows?.ms} ms, ${blank.length} frame(s) with the header over blank rows${blank.length ? ` (${blank.map(v => v.ms + ' ms').join(', ')})` : ''}`)
  console.log(`${LABEL}: ${tiles.length} frames -> ${join(OUT, `${LABEL}-filmstrip.png`)}`)
  process.exitCode = blank.length ? 1 : 0
} finally {
  await browser.close()
}

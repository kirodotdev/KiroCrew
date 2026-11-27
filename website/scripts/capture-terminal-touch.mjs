/**
 * Evidence for phone touch in the terminal, via capture/terminal-touch.html: a
 * REAL xterm with the touch hook, selection handles and soft key bar, fed a
 * fixture scrollback (no gateway).
 *
 * Each device (iPhone 14, Pixel 7 viewports, touch on) runs the same checks with
 * real touch input sent through the DevTools protocol, so the browser fires
 * genuine TouchEvents at xterm and at the hook:
 *
 *   long-press   a still press on a word selects that word, by character, and
 *                draws two handles.
 *   drag-handle  dragging the end handle right/down extends the selection.
 *   copy         the key bar's Copy puts exactly the selection on the clipboard.
 *   momentum     a flick keeps scrolling after the finger lifts.
 *   desktop      a mouse drag still selects (xterm's own path), with no handles.
 *
 * Every check prints PASS/FAIL with its measurement; the script exits 1 when
 * any check fails, so running it on the parent commit is the "before" proof.
 * Each device's run is also recorded as a video. Frames carry a corner label,
 * "Component render, not the live page".
 *
 * Chromium stands in for both devices (the gate installs Chromium only), with
 * each device's viewport, scale, user agent and touch support.
 *
 * Usage (from website/): node scripts/capture-terminal-touch.mjs [outDir] [viteBase]
 * With no viteBase it starts its own Vite dev server on 127.0.0.1 and stops it
 * at the end.
 */
import { chromium, devices } from 'playwright'
import { mkdirSync } from 'node:fs'

// A version-manager node can inject an older libstdc++ into LD_LIBRARY_PATH,
// which the system Chromium then fails to load against.
delete process.env.LD_LIBRARY_PATH

const OUT = process.argv[2] || '../temp-screenshots/terminal-touch'
let BASE = process.argv[3]
let vite = null
if (!BASE) {
  const { createServer } = await import('vite')
  vite = await createServer({ server: { host: '127.0.0.1', port: 0 }, logLevel: 'warn' })
  await vite.listen()
  BASE = vite.resolvedUrls.local[0].replace(/\/$/, '')
}
mkdirSync(OUT, { recursive: true })

const LABEL = 'Component render, not the live page'
const results = []
const check = (device, name, ok, detail) => {
  results.push({ device, name, ok, detail })
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${device.padEnd(10)} ${name.padEnd(12)} ${detail}`)
}

async function open(ctx) {
  const page = await ctx.newPage()
  await page.goto(`${BASE}/capture/terminal-touch.html?theme=dark`)
  await page.locator('[data-ready="1"]').waitFor({ timeout: 30000 })
  await page.evaluate(label => {
    const tag = document.createElement('div')
    tag.textContent = label
    tag.style.cssText = 'position:fixed;left:6px;top:2px;z-index:99;font:10px sans-serif;color:#fff;background:#c026d3;padding:1px 4px;border-radius:3px;pointer-events:none'
    document.body.append(tag)
  }, LABEL)
  await page.waitForTimeout(300)
  return page
}

/** Geometry of xterm's grid: one cell's size and the grid's origin. */
const grid = page => page.evaluate(() => {
  const t = window.__term
  const r = t.element.querySelector('.xterm-screen').getBoundingClientRect()
  return { left: r.left, top: r.top, cw: r.width / t.cols, ch: r.height / t.rows }
})
const cellCentre = (g, col, row) => ({ x: g.left + (col + 0.5) * g.cw, y: g.top + (row + 0.5) * g.ch })
const selection = page => page.evaluate(() => window.__term.getSelection())
const viewportY = page => page.evaluate(() => window.__term.buffer.active.viewportY)

async function touchDrag(cdp, page, from, to, steps, stepMs) {
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [from] })
  for (let i = 1; i <= steps; i++) {
    await page.waitForTimeout(stepMs)
    const p = { x: from.x + ((to.x - from.x) * i) / steps, y: from.y + ((to.y - from.y) * i) / steps }
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [p] })
  }
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] })
}

async function phone(browser, key, slug) {
  const { defaultBrowserType: _ignored, ...desc } = devices[key]
  const ctx = await browser.newContext({ ...desc, recordVideo: { dir: OUT, size: desc.viewport } })
  await ctx.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: BASE })
  const page = await open(ctx)
  const cdp = await ctx.newCDPSession(page)
  const g = await grid(page)

  // Long-press the short SHA at the start of the 10th visible row.
  const word = cellCentre(g, 3, 10)
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [word] })
  await page.waitForTimeout(800)
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] })
  await page.waitForTimeout(200)
  const picked = await selection(page)
  const handles = await page.locator('[data-terminal-touch-handle]').count()
  check(slug, 'long-press', picked.length === 7 && handles === 2, `selection=${JSON.stringify(picked)} handles=${handles}`)
  await page.screenshot({ path: `${OUT}/${slug}-1-long-press.png` })

  // Drag the end handle two rows down and to column 20.
  const end = page.locator('[data-terminal-touch-handle="end"]')
  if (await end.count()) {
    const b = await end.boundingBox()
    const from = { x: b.x + b.width / 2, y: b.y + b.height / 2 }
    const to = { x: from.x + 13 * g.cw, y: from.y + 2 * g.ch }
    await touchDrag(cdp, page, from, to, 12, 30)
    await page.waitForTimeout(200)
  }
  const dragged = await selection(page)
  check(slug, 'drag-handle', dragged.length > picked.length && dragged.startsWith(picked), `selection=${JSON.stringify(dragged)}`)
  await page.screenshot({ path: `${OUT}/${slug}-2-drag-handle.png` })

  // Copy from the key bar.
  await page.evaluate(() => navigator.clipboard.writeText(''))
  await page.getByRole('button', { name: 'Copy', exact: true }).tap()
  await page.waitForTimeout(300)
  const clip = await page.evaluate(() => navigator.clipboard.readText())
  check(slug, 'copy', dragged !== '' && clip === dragged, `clipboard=${JSON.stringify(clip)}`)
  await page.screenshot({ path: `${OUT}/${slug}-3-copied.png` })

  // A quick downward flick (into older history), then watch after the lift.
  const top = cellCentre(g, 20, 4)
  const bottom = cellCentre(g, 20, 16)
  await touchDrag(cdp, page, top, bottom, 6, 16)
  const atLift = await viewportY(page)
  await page.waitForTimeout(1200)
  const after = await viewportY(page)
  check(slug, 'momentum', atLift - after >= 3, `viewportY at lift=${atLift}, 1.2s later=${after} (moved ${atLift - after} rows after lift)`)
  await page.screenshot({ path: `${OUT}/${slug}-4-after-fling.png` })

  const video = page.video()
  await ctx.close()
  await video?.saveAs(`${OUT}/${slug}.webm`)
  await video?.delete()
}

async function desktop(browser) {
  const ctx = await browser.newContext({ viewport: { width: 900, height: 600 } })
  const page = await open(ctx)
  const g = await grid(page)
  const a = cellCentre(g, 0, 8)
  const b = cellCentre(g, 20, 9)
  await page.mouse.move(a.x, a.y)
  await page.mouse.down()
  await page.mouse.move(b.x, b.y, { steps: 8 })
  await page.mouse.up()
  await page.waitForTimeout(200)
  const sel = await selection(page)
  const handles = await page.locator('[data-terminal-touch-handle]').count()
  const keyBar = await page.locator('[data-testid="terminal-key-bar"]').count()
  check('desktop', 'mouse-drag', sel.length > 20 && handles === 0 && keyBar === 0, `selection length=${sel.length} handles=${handles} keyBar=${keyBar}`)
  await page.screenshot({ path: `${OUT}/desktop-mouse-select.png` })
  await ctx.close()
}

const browser = await chromium.launch()
await phone(browser, 'iPhone 14', 'iphone-14')
await phone(browser, 'Pixel 7', 'pixel-7')
await desktop(browser)
await browser.close()
await vite?.close()

const failed = results.filter(r => !r.ok)
console.log(failed.length ? `${failed.length} check(s) failed` : 'all checks passed')
process.exit(failed.length ? 1 : 0)

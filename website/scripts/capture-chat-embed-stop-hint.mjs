/**
 * Screenshots and measurements for ChatEmbed's stop hint strip beside the
 * working indicator.
 *
 * Drives website/capture/chat-embed-stop-hint.html: the REAL ChatEmbed under
 * the REAL AppApiProvider, against fixed fake slot data in which a turn runs
 * and its stop press is pending. The scroller ends in the working indicator
 * and the force-stop hint shows above the composer. Frames: 640px and 340px
 * wide, dark and light, framed (plus the frameless embed, measured only).
 *
 * Asserted, not assumed, per frame:
 *  - the working indicator is scrolled into view at the scroller's bottom;
 *  - the hint text starts at least MIN_GAP px below the indicator's visible
 *    bottom edge;
 *  - a framed embed draws a top border on the strip, and the composer row
 *    below it draws none, so one line separates the transcript from the
 *    composer block.
 *
 * It also measures the main chat (website/capture/chatpane-loader.html, the
 * REAL ChatPane with its working indicator) and prints those boxes as JSON,
 * so a run on main and a run on a change can be compared for an unchanged
 * main chat layout.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6811 --strictPort   # in another shell
 *   node scripts/capture-chat-embed-stop-hint.mjs http://127.0.0.1:6811 ../temp-screenshots/chat-embed-stop-hint
 *   (add --measure-only to print the numbers without asserting or capturing)
 */
import { chromium } from 'playwright'
import { mkdirSync, statSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6811'
const OUT = process.argv[3] || '../temp-screenshots/chat-embed-stop-hint'
const MEASURE_ONLY = process.argv.includes('--measure-only')
const MIN_GAP = 12
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
const pageErrors = []
let failures = 0
const fail = msg => { console.error(`FAIL: ${msg}`); failures++ }

/** Boxes of the working indicator, the hint strip and the composer row. */
function measureEmbed() {
  const root = document.querySelector('[data-capture-root]')
  const footer = root.querySelector('[data-testid="chat-footer"]')
  const hint = root.querySelector('[data-testid="chat-embed-stop-hint"]')
  if (!footer || !hint) return { missing: { footer: !footer, hint: !hint } }
  let scroller = footer.parentElement
  while (scroller && !/(auto|scroll)/.test(getComputedStyle(scroller).overflowY)) scroller = scroller.parentElement
  // The indicator's ink: the lowest painted descendant, not the footer's
  // own box, which carries its own padding.
  let inkBottom = 0
  for (const el of footer.querySelectorAll('*')) {
    const r = el.getBoundingClientRect()
    if (r.width > 0 && r.height > 0) inkBottom = Math.max(inkBottom, r.bottom)
  }
  const strip = hint.parentElement.closest('.shrink-0')
  const composerRow = root.querySelector('[aria-label="Chat message"]').closest('.shrink-0')
  const sr = scroller.getBoundingClientRect()
  const visibleBottom = Math.min(inkBottom, sr.bottom)
  const hintTop = hint.getBoundingClientRect().top
  const round = n => Math.round(n * 10) / 10
  return {
    scrollerBottom: round(sr.bottom),
    scrollerPaddingBottom: getComputedStyle(scroller).paddingBottom,
    indicatorInkBottom: round(inkBottom),
    indicatorInView: inkBottom <= sr.bottom + 1 && inkBottom > sr.top,
    stripTop: round(strip.getBoundingClientRect().top),
    stripBorderTop: getComputedStyle(strip).borderTopWidth,
    hintTop: round(hintTop),
    gap: round(hintTop - visibleBottom),
    composerRowBorderTop: getComputedStyle(composerRow).borderTopWidth,
  }
}

const results = {}
for (const frameless of [false, true]) {
  for (const width of [640, 340]) {
    for (const theme of ['dark', 'light']) {
      const name = `${frameless ? 'frameless' : 'framed'}-${width}-${theme}`
      const page = await browser.newPage({ viewport: { width: width + 64, height: 500 }, deviceScaleFactor: 1 })
      page.on('pageerror', e => pageErrors.push(String(e)))
      await page.goto(`${BASE}/capture/chat-embed-stop-hint.html?theme=${theme}&width=${width}${frameless ? '&frameless=1' : ''}`, { waitUntil: 'networkidle' })
      await page.getByTestId('chat-embed-stop-hint').waitFor()
      await page.getByTestId('chat-footer').waitFor()
      await page.evaluate(() => Promise.all(document.getAnimations().filter(a => a.effect?.getTiming().iterations !== Infinity).map(a => a.finished)))
      await page.waitForTimeout(400)
      const m = await page.evaluate(measureEmbed)
      results[name] = m
      console.log(`${name}: ${JSON.stringify(m)}`)
      if (!MEASURE_ONLY) {
        if (m.missing) fail(`${name} missing ${JSON.stringify(m.missing)}`)
        else {
          if (!m.indicatorInView) fail(`${name} working indicator is not at the scroller's bottom`)
          if (m.gap < MIN_GAP) fail(`${name} hint starts ${m.gap}px below the indicator (want >= ${MIN_GAP})`)
          if (!frameless && m.stripBorderTop !== '1px') fail(`${name} strip top border is ${m.stripBorderTop} (want 1px)`)
          if (!frameless && m.composerRowBorderTop !== '0px') fail(`${name} composer row also draws a top border (${m.composerRowBorderTop})`)
        }
        if (!frameless) {
          const file = `${OUT}/${name}.png`
          await page.locator('[data-capture-root]').screenshot({ path: file })
          const bytes = statSync(file).size
          if (bytes < 4000) fail(`${name} capture is only ${bytes} bytes`)
          console.log(`${name}: captured ${bytes} bytes`)
        }
      }
      await page.close()
    }
  }
}

// The main chat: the REAL ChatPane with its working indicator.
{
  const page = await browser.newPage({ viewport: { width: 860, height: 640 }, deviceScaleFactor: 1 })
  page.on('pageerror', e => pageErrors.push(String(e)))
  await page.route(u => new URL(u).pathname.startsWith('/api/'), route => {
    const path = new URL(route.request().url()).pathname
    const isList = /commands|skills|agents|sessions|files|history|models|artifacts|folders/.test(path)
    return route.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
  })
  await page.goto(`${BASE}/capture/chatpane-loader.html?theme=dark&state=tool_running`)
  await page.getByTestId('chat-footer').waitFor()
  await page.waitForTimeout(800)
  const main = await page.evaluate(() => {
    const box = el => { const r = el.getBoundingClientRect(); return [r.left, r.top, r.width, r.height].map(n => Math.round(n * 10) / 10) }
    const footer = document.querySelector('[data-testid="chat-footer"]')
    let scroller = footer.parentElement
    while (scroller && !/(auto|scroll)/.test(getComputedStyle(scroller).overflowY)) scroller = scroller.parentElement
    const input = document.querySelector('[data-capture-root] textarea, [data-capture-root] [contenteditable="true"]')
    return {
      footer: box(footer),
      scroller: box(scroller),
      scrollerPaddingBottom: getComputedStyle(scroller).paddingBottom,
      composerInput: input ? box(input) : null,
    }
  })
  results.mainChat = main
  console.log(`mainChat: ${JSON.stringify(main)}`)
  await page.close()
}

await browser.close()
if (pageErrors.length) { console.error('page errors:', pageErrors); failures++ }
if (failures) {
  console.error(`${failures} assertion failure(s)`)
  process.exit(1)
}
console.log(MEASURE_ONLY ? 'MEASURED' : 'ALL GREEN')

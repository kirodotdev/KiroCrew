/**
 * Screenshot + assertion runner for capture/tool-output-clamp.html.
 *
 * From website/:
 *   npx vite --host 127.0.0.1 --port 6841 --strictPort
 *   node scripts/capture-tool-output-clamp.mjs http://127.0.0.1:6841 \
 *     ../temp-screenshots/tool-output-clamp
 *
 * The assertion carries the evidence: a 120 000-character tool result goes
 * through the real `sseToolResult` reducer, which stores head + tail plus a
 * structural seam (`output_cut`), and the Output panel must render the
 * `…(N characters truncated — …)` marker at that seam exactly once, with whole
 * source lines on both sides of it, the head above, the tail below — and the
 * sentinel line from the dropped middle absent. A frame that merely looks like
 * a long output would prove nothing, so the probe reads the rendered text first.
 * The marker is the panel's own render, so the probe also checks that no
 * dialog sits over the frame before each shot.
 *
 * Frames `edit-<theme>.png` cover the other clamped surface (`?row=edit`): an
 * edit-kind row whose long-line create diff is clamped. The store must record
 * `input_cut`, the row must render the truncated SUMMARY chip (`≥+N · diff
 * truncated`) and no patch card, and the Input pane must show the marker.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6841'
const OUT = process.argv[3] || '../temp-screenshots/tool-output-clamp'
// `store.chatSlice.truncated_chars` in i18n/locales/en.manual.json, with the
// elided-character count interpolated in the locale's digit grouping
// (`84,080`). Anchored to the line so a source row that merely mentions the
// word cannot count as a marker.
const MARKER_RE = /^…\(([\d,]+) characters truncated — reopen the session to see the full output\)$/m
// Mirrors TOOL_OUTPUT_MAX_CHARS in store/chatSlice.ts; the marker line fits
// inside the 4 000-character slack the head + tail slices leave under it.
const CEILING = 64_000

mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
let failed = 0

function check(name, ok, detail) {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed++
}

for (const theme of ['dark', 'light']) {
  const ctx = await browser.newContext({
    viewport: { width: 900, height: 720 },
    deviceScaleFactor: 2,
    colorScheme: theme,
  })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))

  await page.goto(`${BASE}/capture/tool-output-clamp.html?theme=${theme}`, { waitUntil: 'networkidle' })
  await page.waitForSelector('[data-capture-root]', { timeout: 15000 })
  const pre = page.locator('[data-capture-root] pre').first()
  await pre.waitFor({ timeout: 15000 })
  // Reveal animation on the expanded details panel.
  await page.waitForTimeout(600)

  const probe = await pre.evaluate((el, [src, flags]) => {
    const text = el.textContent || ''
    const hits = [...text.matchAll(new RegExp(src, flags + 'g'))]
    const first = hits[0]
    const at = first?.index ?? -1
    const markerLen = first?.[0].length ?? 0
    // Rendered = head + "\n" + marker + "\n" + tail; read both source rows
    // that touch the marker from the rendered text itself.
    const lineBefore = at > 0 ? text.slice(text.lastIndexOf('\n', at - 2) + 1, at - 1) : ''
    const afterStart = at >= 0 ? at + markerLen + 1 : -1
    const lineAfter = afterStart >= 0 ? text.slice(afterStart, text.indexOf('\n', afterStart)) : ''
    return {
      length: text.length,
      markers: hits.length,
      count: first ? Number(first[1].replace(/,/g, '')) : -1,
      headLength: at > 0 ? at - 1 : -1,
      tailLength: at >= 0 ? text.length - (at + markerLen + 1) : -1,
      lineBefore,
      lineAfter,
      hasHead: text.includes('HEAD- case 00001 passed'),
      hasTail: text.includes('TAIL- case 02000 passed') && text.includes('exit status: 0'),
      hasSentinel: text.includes('ELIDED-SENTINEL'),
    }
  }, [MARKER_RE.source, MARKER_RE.flags])
  const rawLength = Number(await page.locator('[data-capture-root]').getAttribute('data-raw-length'))
  const ROW_RE = /^(HEAD|MID-|TAIL)- case \d{5} passed$/
  const elided = rawLength - probe.headLength - probe.tailLength

  check(`${theme} raw result is over the ceiling`, rawLength > CEILING, `raw=${rawLength}`)
  check(`${theme} rendered output is clamped`, probe.length <= CEILING, `rendered=${probe.length} ceiling=${CEILING}`)
  check(`${theme} marker appears exactly once`, probe.markers === 1, `markers=${probe.markers}`)
  check(`${theme} marker counts the elided characters`, probe.count === elided, `count=${probe.count} elided=${elided}`)
  check(`${theme} whole source line above the marker`, ROW_RE.test(probe.lineBefore), JSON.stringify(probe.lineBefore))
  check(`${theme} whole source line below the marker`, ROW_RE.test(probe.lineAfter), JSON.stringify(probe.lineAfter))
  check(`${theme} head survives`, probe.hasHead, '')
  check(`${theme} tail survives`, probe.hasTail, '')
  check(`${theme} elided middle is gone`, !probe.hasSentinel, '')
  check(`${theme} no page errors`, errors.length === 0, errors.join(' | ').slice(0, 200))
  // The marker lives in its own span inside the panel's <pre>; a frame with a
  // dialog over it would still pass the text probes above.
  const dialogs = await page.locator('[role="dialog"]').count()
  check(`${theme} no dialog over the frame`, dialogs === 0, `dialogs=${dialogs}`)
  const markerSpans = await page.locator('[data-capture-root] [data-testid="tool-payload-truncated"]').count()
  check(`${theme} marker is the panel's own span`, markerSpans === 1, `spans=${markerSpans}`)

  // Scroll the marker to the middle of the Output panel so the frame shows
  // head above, tail below, and the marker between them.
  await pre.evaluate((el, [src, flags]) => {
    const text = el.textContent || ''
    const at = text.search(new RegExp(src, flags))
    const range = document.createRange()
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT)
    let seen = 0
    let node = walker.nextNode()
    while (node) {
      const len = node.textContent?.length ?? 0
      if (seen + len > at) {
        range.setStart(node, at - seen)
        range.setEnd(node, at - seen)
        break
      }
      seen += len
      node = walker.nextNode()
    }
    const rect = range.getBoundingClientRect()
    const box = el.getBoundingClientRect()
    el.scrollTop += rect.top - box.top - box.height / 2
  }, [MARKER_RE.source, MARKER_RE.flags])
  await page.waitForTimeout(150)

  await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${theme}.png` })
  console.log(`wrote ${OUT}/${theme}.png`)
  await ctx.close()
}

// ---- edit row: clamped diff -> summary chip, never a complete-looking card
for (const theme of ['dark', 'light']) {
  const ctx = await browser.newContext({
    viewport: { width: 900, height: 720 },
    deviceScaleFactor: 2,
    colorScheme: theme,
  })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))

  await page.goto(`${BASE}/capture/tool-output-clamp.html?theme=${theme}&row=edit`, { waitUntil: 'networkidle' })
  const root = page.locator('[data-capture-root][data-row="edit"]')
  await root.waitFor({ timeout: 15000 })
  await page.locator('[data-capture-root] pre').first().waitFor({ timeout: 15000 })
  await page.waitForTimeout(600)

  const rawLength = Number(await root.getAttribute('data-raw-length'))
  const cutJson = await root.getAttribute('data-input-cut')
  const cut = cutJson ? JSON.parse(cutJson) : null
  const summaryChips = await page.locator('[data-testid="tool-diff-summary-chip"]').count()
  const cardChips = await page.locator('[data-testid="tool-diff-chip"]').count()
  const cardToggles = await page.locator('[data-diff-toggle]').count()
  const chipText = summaryChips ? await page.locator('[data-testid="tool-diff-summary-chip"]').first().textContent() : ''
  const paneText = await page.locator('[data-capture-root] pre').first().textContent()
  const markerSpans = await page.locator('[data-capture-root] [data-testid="tool-payload-truncated"]').count()
  const dialogs = await page.locator('[role="dialog"]').count()

  check(`edit ${theme} raw diff is over the ceiling`, rawLength > CEILING, `raw=${rawLength}`)
  // Rendered pane = stored text with the marker + one newline spliced in, and
  // the stored text = head + one seam newline + tail. So raw = head + count +
  // tail, i.e. the seam's count must be exactly what the raw diff lost.
  const markerLen = ((paneText || '').match(MARKER_RE) || [''])[0].length
  const storedLength = (paneText || '').length - markerLen - 1
  const headPlusTail = storedLength - 1
  check(`edit ${theme} store recorded input_cut`, !!cut && cut.at > 0 && cut.count === rawLength - headPlusTail, `cut=${JSON.stringify(cut)} raw=${rawLength} head+tail=${headPlusTail}`)
  check(`edit ${theme} row shows the summary chip`, summaryChips === 1, `summary=${summaryChips}`)
  check(`edit ${theme} chip is flagged truncated`, /diff truncated/.test(chipText || ''), JSON.stringify(chipText))
  check(`edit ${theme} no patch card is promoted`, cardChips === 0 && cardToggles === 0, `card=${cardChips} toggles=${cardToggles}`)
  check(`edit ${theme} Input pane shows the marker once`, markerSpans === 1 && MARKER_RE.test(paneText || ''), `spans=${markerSpans}`)
  check(`edit ${theme} elided middle is gone`, !(paneText || '').includes('ELIDED_SENTINEL'), '')
  check(`edit ${theme} no dialog over the frame`, dialogs === 0, `dialogs=${dialogs}`)
  check(`edit ${theme} no page errors`, errors.length === 0, errors.join(' | ').slice(0, 200))

  // Scroll the Input pane to the seam so the frame shows chip + marker.
  await page.locator('[data-capture-root] pre').first().evaluate((el, [src, flags]) => {
    const text = el.textContent || ''
    const at = text.search(new RegExp(src, flags))
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT)
    let seen = 0
    let node = walker.nextNode()
    while (node) {
      const len = node.textContent?.length ?? 0
      if (seen + len > at) {
        const range = document.createRange()
        range.setStart(node, at - seen)
        range.setEnd(node, at - seen)
        const rect = range.getBoundingClientRect()
        const box = el.getBoundingClientRect()
        el.scrollTop += rect.top - box.top - box.height / 2
        break
      }
      seen += len
      node = walker.nextNode()
    }
  }, [MARKER_RE.source, MARKER_RE.flags])
  await page.waitForTimeout(150)

  await root.screenshot({ path: `${OUT}/edit-${theme}.png` })
  console.log(`wrote ${OUT}/edit-${theme}.png`)
  await ctx.close()
}

await browser.close()
if (failed) {
  console.error(`CAPTURE FAILED: ${failed} assertion(s) did not hold`)
  process.exit(1)
}
console.log('all frames verified')

/**
 * Screenshot + assertion runner for capture/tool-output-clamp.html.
 *
 * From website/:
 *   npx vite --host 127.0.0.1 --port 6841 --strictPort
 *   node scripts/capture-tool-output-clamp.mjs http://127.0.0.1:6841 \
 *     ../temp-screenshots/tool-output-clamp
 *
 * The assertion carries the evidence: a 120 000-character tool result goes
 * through the real `sseToolResult` reducer, and the Output panel must show the
 * head, the `…(N characters truncated — …)` marker exactly once with whole
 * source lines on both sides of it, and the tail — with the sentinel line from
 * the dropped middle absent. A frame that merely looks like a long output
 * would prove nothing, so the probe reads the rendered text first.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6841'
const OUT = process.argv[3] || '../temp-screenshots/tool-output-clamp'
// `store.chatSlice.truncated_chars` in i18n/locales/en.manual.json, with the
// elided-character count interpolated. Anchored to the line so a source row
// that merely mentions the word cannot count as a marker.
const MARKER_RE = /^…\((\d+) characters truncated — full output on reload\)$/m
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
      count: first ? Number(first[1]) : -1,
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

await browser.close()
if (failed) {
  console.error(`CAPTURE FAILED: ${failed} assertion(s) did not hold`)
  process.exit(1)
}
console.log('all frames verified')

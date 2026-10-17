/**
 * #7819 — capture the chat selection toolbar on a reply that is still streaming.
 *
 * Drives the REAL desktop path against the real `AssistantMessage`: build a DOM
 * range inside the streaming bubble, fire the `mouseup` the toolbar debounces by
 * 50ms, then shoot.
 *
 * The script is deliberately TWO-SIDED so it can prove a delta rather than just
 * illustrate one state. `--expect absent` is what makes the "before" shot a
 * measurement: run it with the `!isStreaming` gate restored and it must find NO
 * toolbar; run it on this branch and it must find one. A one-sided capture would
 * pass on any tree that renders something.
 *
 * Usage: node scripts/capture-selection-toolbar-streaming.mjs <viteBase> <outFile> [--expect present|absent]
 */
import { chromium } from 'playwright'

const base = process.argv[2] || 'http://127.0.0.1:5199'
const out = process.argv[3] || '../temp-screenshots/selection-toolbar-streaming/toolbar-while-streaming.png'
const expectIdx = process.argv.indexOf('--expect')
const expect = expectIdx === -1 ? 'present' : process.argv[expectIdx + 1]
if (expect !== 'present' && expect !== 'absent') {
  console.error(`--expect must be present|absent, got ${expect}`)
  process.exit(2)
}

const b = await chromium.launch()
const p = await (await b.newContext({ viewport: { width: 780, height: 300 }, deviceScaleFactor: 2 })).newPage()
p.on('pageerror', (e) => console.error(`PAGE ERROR: ${e.message}`))
await p.goto(`${base}/capture/selection-toolbar-streaming.html?theme=dark`, { waitUntil: 'networkidle' })

// The bubble must be on screen before a range can be built inside it. This also
// fails loudly if the scene itself broke, so an absent toolbar can never be
// confused with an absent scene.
await p.locator('[data-testid="scene"] .msg-content').waitFor({ state: 'visible', timeout: 15_000 })

// Select a mid-paragraph span, then fire the desktop trigger. Anchoring on a
// substring rather than the whole node keeps the selection visibly inside the
// prose, which is what a reader would actually do mid-stream.
const NEEDLE = 'the session lease expires while a turn is still in flight'
const picked = await p.evaluate((needle) => {
  const root = document.querySelector('[data-testid="scene"] .msg-content')
  if (!root) return { ok: false, why: 'no bubble' }
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    const i = (n.textContent || '').indexOf(needle)
    if (i === -1) continue
    const range = document.createRange()
    range.setStart(n, i)
    range.setEnd(n, i + needle.length)
    const sel = window.getSelection()
    sel.removeAllRanges()
    sel.addRange(range)
    const r = range.getBoundingClientRect()
    const target = n.parentElement || root
    target.dispatchEvent(new MouseEvent('mouseup', {
      bubbles: true, clientX: Math.round(r.left + r.width / 2), clientY: Math.round(r.bottom),
    }))
    return { ok: true, text: sel.toString() }
  }
  return { ok: false, why: 'needle not found in the rendered prose' }
}, NEEDLE)
if (!picked.ok) { console.error(`selection failed: ${picked.why}`); await b.close(); process.exit(1) }

const quote = p.locator('button[title="Quote"], button:has-text("Quote")').first()
if (expect === 'present') {
  await quote.waitFor({ state: 'visible', timeout: 15_000 })
} else {
  // Give the 50ms debounce room to fire before concluding nothing appeared, so
  // "absent" means absent rather than merely early.
  await p.waitForTimeout(1_500)
  if (await quote.count() > 0 && await quote.isVisible()) {
    console.error('expected NO toolbar (gate in place) but one is visible')
    await b.close(); process.exit(1)
  }
}
// `animations: 'disabled'` matters for the PAIR, not for this shot alone: the
// streaming glow sweeps and the caret blinks, so two unfrozen shots differ in
// more than the toolbar and stop being a controlled comparison.
await p.screenshot({ path: out, animations: 'disabled' })
console.log(`captured ${out} — selected ${JSON.stringify(picked.text)}, toolbar ${expect} as asserted`)
await b.close()

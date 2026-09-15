/**
 * #7819 -- MEASURE whether a selection survives real token arrival, and record it.
 *
 * The capture scene's default frame freezes `isStreaming` without growing the
 * content, which is enough for a screenshot and cannot answer the question two
 * review lanes asked: does a held selection survive while tokens land? This
 * drives `?stream=1`, which appends words on a real timer so `useSmoothStream`
 * and `MarkdownRenderer` genuinely re-parse under the selection. The token
 * SOURCE is irrelevant to that mechanism -- what collapses a range is the DOM
 * being rewritten, not where the bytes came from.
 *
 * It prints measurements rather than asserting an outcome, because the honest
 * answer is a number. The one thing it does assert is its own CONTROL: if the
 * word count did not grow between the two reads, nothing was re-parsed and the
 * whole reading is void -- a probe that cannot tell "survived" from "never
 * tested" is worse than no probe.
 *
 * Two cases, because they are different claims:
 *   SETTLED -- a selection in a paragraph that was complete before the stream
 *              started. This is the case the feature exists for.
 *   TAIL    -- a selection inside the block still being written. Expected to be
 *              fragile; measured so the PR can state a number.
 *
 * Usage: node scripts/probe-selection-toolbar-token-stream.mjs <viteBase> [videoDir]
 */
import { chromium } from 'playwright'

const base = process.argv[2] || 'http://127.0.0.1:5199'
const videoDir = process.argv[3] || ''

const b = await chromium.launch()
const ctx = await b.newContext({
  viewport: { width: 780, height: 320 },
  deviceScaleFactor: 1,
  ...(videoDir ? { recordVideo: { dir: videoDir, size: { width: 780, height: 320 } } } : {}),
})
const p = await ctx.newPage()
p.on('pageerror', (e) => console.error(`PAGE ERROR: ${e.message}`))

const words = () => p.locator('[data-testid="scene"]').getAttribute('data-words').then(Number)
const toolbarVisible = async () => {
  const q = p.locator('button[title="Quote"], button:has-text("Quote")').first()
  return (await q.count()) > 0 && (await q.isVisible())
}

/** Select a substring of the paragraph at `pIdx` and fire the desktop trigger. */
async function selectIn(pIdx, needle) {
  return p.evaluate(({ pIdx, needle }) => {
    const root = document.querySelector('[data-testid="scene"] .msg-content')
    const paras = root ? root.querySelectorAll('p') : []
    const el = paras[pIdx]
    if (!el) return { ok: false, why: `no paragraph ${pIdx} (found ${paras.length})` }
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT)
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
      el.dispatchEvent(new MouseEvent('mouseup', {
        bubbles: true, clientX: Math.round(r.left + r.width / 2), clientY: Math.round(r.bottom),
      }))
      return { ok: true, text: sel.toString() }
    }
    return { ok: false, why: `needle not in paragraph ${pIdx}` }
  }, { pIdx, needle })
}

async function run(label, pIdx, needleArg, holdMs) {
  let needle = needleArg
  await p.goto(`${base}/capture/selection-toolbar-streaming.html?theme=dark&stream=1&interval=70`, { waitUntil: 'networkidle' })
  // The wait has to be on the RENDERED text, not on the word counter: the smooth
  // reveal lags the content, so `data-words` can be well ahead of what is on
  // screen. And for the settled case the paragraph must be COMPLETE -- waiting
  // only for the needle catches it mid-growth, which measures the tail case
  // while claiming to measure the settled one (this probe did exactly that once).
  if (pIdx === 0) {
    await p.waitForFunction(() => {
      const ps = document.querySelectorAll('[data-testid="scene"] .msg-content p')
      return ps.length >= 2 && (ps[0].textContent || '').trim().endsWith('agree.') && (ps[1].textContent || '').length > 20
    }, null, { timeout: 20_000 })
  } else {
    // Derive the tail needle from the paragraph's OWN live text instead of
    // hardcoding a phrase: the smooth reveal lags the appends, so any fixed
    // phrase is a guess about timing and this wait failed on exactly that.
    await p.waitForFunction(() => {
      const ps = document.querySelectorAll('[data-testid="scene"] .msg-content p')
      return ps.length >= 2 && (ps[1].textContent || '').trim().length > 25
    }, null, { timeout: 20_000 })
    needle = await p.evaluate(() => {
      // Take the needle from a SINGLE text node, not from `textContent`: the
      // streaming glow splits the tail across several nodes, so a slice of the
      // concatenated text can straddle a boundary and then match nothing.
      const ps = document.querySelectorAll('[data-testid="scene"] .msg-content p')
      const w = document.createTreeWalker(ps[1], NodeFilter.SHOW_TEXT)
      for (let n = w.nextNode(); n; n = w.nextNode()) {
        const t = (n.textContent || '').trim()
        if (t.length >= 14) return t.slice(0, 14)
      }
      return ''
    })
    if (!needle) return { label, ok: false, why: 'no single tail text node held 14+ chars (the glow fragments it)' }
  }

  const picked = await selectIn(pIdx, needle)
  if (!picked.ok) return { label, ok: false, why: picked.why }
  await p.waitForTimeout(120)                      // the toolbar's 50ms debounce
  const shownAt = await words()
  const shown = await toolbarVisible()

  // How long the browser keeps the highlight is a separate question from whether
  // the toolbar stays usable, so measure both.
  const selAlive = await p.evaluate(() => (window.getSelection()?.toString() || '').length)
  await p.waitForTimeout(holdMs)                   // hold through token arrival
  const selAfter = await p.evaluate(() => (window.getSelection()?.toString() || '').length)
  const heldAt = await words()
  const still = await toolbarVisible()

  // CONTROL: if nothing was appended, nothing re-parsed and the reading is void.
  const grew = heldAt > shownAt
  let quoted = null
  if (still) {
    await p.locator('button[title="Quote"], button:has-text("Quote")').first().click()
    quoted = await p.evaluate(() => window.__quoted ?? null)
  }
  return { label, ok: true, shown, still, shownAt, heldAt, grew, selAlive, selAfter,
           selected: picked.text, quoted, quoteMatches: quoted !== null && quoted === picked.text }
}

const results = []
results.push(await run('SETTLED (paragraph complete before the stream)', 0, 'independent clocks', 2600))
results.push(await run('TAIL (block still being written)', 1, 'reconnect storm', 2600))

console.log('')
for (const r of results) {
  if (!r.ok) { console.log(`${r.label}: PROBE FAILED -- ${r.why}`); continue }
  if (!r.grew) { console.log(`${r.label}: VOID -- word count did not grow (${r.shownAt} -> ${r.heldAt}); nothing re-parsed`); continue }
  console.log(`${r.label}:`)
  console.log(`  words ${r.shownAt} -> ${r.heldAt} (grew, so the subtree really re-parsed under the selection)`)
  console.log(`  toolbar shown on selection: ${r.shown}`)
  console.log(`  browser highlight length: ${r.selAlive} at selection -> ${r.selAfter} after the hold`)
  console.log(`  toolbar still shown after holding through token arrival: ${r.still}`)
  console.log(`  Quote payload identical to what was selected: ${r.quoteMatches} ${r.quoted === null ? '(not clicked -- toolbar gone)' : ''}`)
}
await ctx.close()
await b.close()
if (videoDir) console.log(`\nvideo written under ${videoDir}`)

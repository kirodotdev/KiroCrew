/**
 * Screenshot of the Comment pill appearing over a REAL triple-click selection of
 * the spec document's last paragraph (#7891), via the
 * capture/docview-multiclick-last-paragraph harness.
 *
 * The gesture is genuine Chromium input, so the browser makes its own selection,
 * including the boundary normalization past the scroll pane that used to clear
 * it. Two details make this a live regression proof rather than a portrait, both
 * established by probing the real geometry:
 *
 * 1. The harness puts a block AFTER the pane, so a normalized boundary point has
 *    somewhere outside the pane to land.
 * 2. The click targets a TWO-LETTER word. The pill is rendered only while `sel`
 *    is set, and the double-click stage of a triple-click sets it -- but only
 *    when the word clears `onSelectionSettled`'s 3-character floor. On a longer
 *    word the pill mounts inside the pane and then ABSORBS the normalization
 *    (the boundary lands in the pill's own overlay, still inside the pane), so
 *    the pre-fix component passes. Targeting a short word leaves the pane with a
 *    single child, which is the condition the defect needs.
 *
 * Verified against the pre-fix component: the pill wait TIMES OUT there, with
 * the probe reading `paneContainsCac: false` and the end normalized to a `<p>`
 * outside the pane.
 *
 * Usage: node scripts/capture-docview-multiclick.mjs <viteBase> <outDir>
 */
import { chromium } from 'playwright'

// Some dev hosts run a version-manager-built node that injects its own lib dir
// (with an older libstdc++) into LD_LIBRARY_PATH of the node process itself;
// Chromium inherits it and its system Mesa/LLVM then fail to load. The browser
// wants the system loader path, so drop the injection before launching.
delete process.env.LD_LIBRARY_PATH

const base = process.argv[2] || 'http://127.0.0.1:5199'
const outDir = process.argv[3] || '../temp-screenshots/selection-containment-7891'
const SHORT_WORD = 'to'

const b = await chromium.launch()
const p = await (await b.newContext({ viewport: { width: 820, height: 330 }, deviceScaleFactor: 2 })).newPage()

async function shoot(name, theme) {
  await p.goto(`${base}/capture/docview-multiclick-last-paragraph.html?theme=${theme}`, { waitUntil: 'networkidle' })
  await p.getByText('Triple-click this final paragraph').waitFor({ state: 'visible', timeout: 15_000 })

  // Centre of the short word, measured from a throwaway range over it, so the
  // click lands on it regardless of font metrics.
  const box = await p.evaluate((word) => {
    const paras = [...document.querySelectorAll('.overflow-y-auto p')]
    const text = paras[paras.length - 1].firstChild
    const i = text.data.indexOf(` ${word} `)
    if (i < 0) return null
    const r = document.createRange()
    r.setStart(text, i + 1)
    r.setEnd(text, i + 1 + word.length)
    const rect = r.getBoundingClientRect()
    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 }
  }, SHORT_WORD)
  if (!box) throw new Error(`the word "${SHORT_WORD}" is missing from the last paragraph`)

  await p.mouse.click(box.x, box.y, { clickCount: 3 })
  // Fails loudly against the pre-fix component instead of shooting an empty frame.
  await p.getByRole('button', { name: /comment/i }).waitFor({ state: 'visible', timeout: 5_000 })
  const out = `${outDir}/${name}.png`
  await p.screenshot({ path: out })
  console.log(`captured ${out}`)
}

await shoot('comment-pill-last-paragraph-dark', 'dark')
await shoot('comment-pill-last-paragraph-light', 'light')

await b.close()

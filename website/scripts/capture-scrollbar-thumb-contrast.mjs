/**
 * Screenshot harness for the scrollbar thumb contrast floor (issue #7128).
 *
 * What it has to prove: a thumb that a user can actually SEE against the surface
 * it scrolls over. A diff cannot show that, and a contrast number in a table is
 * an argument about a frame rather than the frame itself.
 *
 * The frames exercise the REAL built stylesheet (`website/dist/assets/src-*.css`,
 * the file `index.css` compiles into) against a minimal fixture rather than a
 * dashboard route. That is the honest scope and the same call
 * capture-focus-ring.mjs made for the same reason: the change is entirely a token
 * swap inside stylesheet rules, no component logic participates, and the thing
 * being compared is a painted colour. Driving a live route instead would add a
 * gateway, a login and a hunt for a scroll box whose overflow state the harness
 * does not control -- more moving parts between the reader and the pixel, not
 * fewer.
 *
 * The BEFORE frame is produced by re-declaring the same rules with the tokens
 * `main` carried (`--border`, `--border-strong`), appended after the stylesheet
 * so it wins on source order. Both frames therefore come from one build, one
 * fixture and one browser, and the ONLY difference between them is the pair of
 * declarations this PR changes -- which is exactly the difference the diff makes.
 *
 * Themes: the two extremes of the measured set plus the product default, so the
 * frames span the range rather than flattering it.
 *   monokai-dark  the tightest theme after the change (3.40:1, the worst case)
 *   kiro-dark     the product's default palette (1.28:1 -> 5.24:1)
 *   light         the default light palette (1.27:1 -> 4.83:1)
 *
 * Each frame holds all three thumb families the stylesheet paints:
 *   - a plain scroll box            -> the global base-layer thumb, always visible
 *   - a `.scrollbar-overlay` box    -> hover-revealed vertical thumb
 *   - a `.scroll-fade` box          -> hover-revealed horizontal thumb
 * The two hover-revealed boxes are held open with a forced `:hover` via CDP, so
 * the frame shows the state the rule paints rather than a transparent gutter.
 *
 * deviceScaleFactor is 3: the thumb is a 6px gutter, invisible at review size in
 * a 1x capture. Every output edge stays well under 2000px.
 *
 * Usage: node scripts/capture-scrollbar-thumb-contrast.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'

const OUT = process.argv[2] || '../temp-screenshots/scrollbar-thumb-contrast'
mkdirSync(OUT, { recursive: true })

const { srv, base } = await serveDist()

/** The compiled stylesheet index.css becomes, found by content hash. */
const cssHref = (() => {
  const assets = readdirSync(new URL('../dist/assets/', import.meta.url))
  const name = assets.find((f) => /^src-.*\.css$/.test(f))
  if (!name) throw new Error('no dist/assets/src-*.css -- run `npm run build` first')
  return `${base}/assets/${name}`
})()
console.log(`stylesheet: ${cssHref.slice(base.length)}`)

/** The tokens `main` carried, re-declared to win on source order. */
const BEFORE_CSS = `
  ::-webkit-scrollbar-thumb{background:var(--border)}
  ::-webkit-scrollbar-thumb:hover{background:var(--border-strong)}
  .scroll-fade:hover::-webkit-scrollbar-thumb{background:var(--border)}
  .scroll-fade:hover{scrollbar-color:var(--border) transparent}
  .scrollbar-overlay:hover{scrollbar-color:var(--border) transparent}
  .scrollbar-overlay:hover::-webkit-scrollbar-thumb{background:var(--border)}
  .scrollbar-overlay:hover::-webkit-scrollbar-thumb:hover{background:var(--border-strong)}
`

const LOREM = Array.from({ length: 14 }, (_, i) => `<div>scrollable line ${i + 1}</div>`).join('')
const WIDE = 'a-very-wide-token-that-forces-horizontal-overflow-'.repeat(4)

const fixture = (theme, before) => `<!doctype html>
<html lang="en" data-theme="${theme}"><head><meta charset="utf-8">
<link rel="stylesheet" href="${cssHref}">
<style>
  body{background:var(--bg);color:var(--text);font:400 12px/1.55 system-ui;
       margin:0;padding:18px;display:flex;flex-direction:column;gap:14px;width:440px}
  .cap{color:var(--muted);font-size:10px;letter-spacing:.02em}
  .panel{background:var(--bg-elevated);border:1px solid var(--border);border-radius:8px;
         padding:9px 11px;height:86px;overflow-y:auto}
  .code{background:var(--bg-elevated);border:1px solid var(--border);border-radius:8px;
        padding:9px 11px;overflow-x:auto;white-space:pre;font-family:ui-monospace,monospace}
</style>
${before ? `<style>${BEFORE_CSS}</style>` : ''}
</head><body>
  <div><div class="cap">plain scroll box -- global base-layer thumb (always visible)</div>
    <div class="panel" id="plain">${LOREM}</div></div>
  <div><div class="cap">.scrollbar-overlay -- vertical thumb, revealed on hover</div>
    <div class="panel scrollbar-overlay" id="overlay">${LOREM}</div></div>
  <div><div class="cap">.scroll-fade -- horizontal thumb, revealed on hover</div>
    <div class="code scroll-fade" id="fade">${WIDE}</div></div>
</body></html>`

// Playwright's headless Chromium ships `--hide-scrollbars` in its DEFAULT args,
// which suppresses the scrollbar entirely -- with it in place every frame here
// renders a scrolled box and no thumb, so before and after come out byte-identical
// and the harness silently proves nothing. Dropping that one default is the whole
// reason this launch is not a bare `chromium.launch()`.
const browser = await chromium.launch({ ignoreDefaultArgs: ['--hide-scrollbars'] })
const THEMES = ['monokai-dark', 'kiro-dark', 'light']

for (const theme of THEMES) {
  for (const before of [true, false]) {
    const context = await browser.newContext({
      viewport: { width: 476, height: 340 },
      deviceScaleFactor: 3,
      colorScheme: theme.endsWith('-light') || theme === 'light' ? 'light' : 'dark',
    })
    const page = await context.newPage()
    await page.setContent(fixture(theme, before), { waitUntil: 'load' })

    // Both hover-revealed boxes must be painted in ONE frame, so a real pointer
    // (one position, one element) cannot do it. forcePseudoState holds :hover on
    // both at once -- the same state the rule keys on, applied by the engine.
    const cdp = await context.newCDPSession(page)
    await cdp.send('DOM.enable')
    await cdp.send('CSS.enable')
    const { root } = await cdp.send('DOM.getDocument')
    for (const sel of ['#overlay', '#fade']) {
      const { nodeId } = await cdp.send('DOM.querySelector', { nodeId: root.nodeId, selector: sel })
      await cdp.send('CSS.forcePseudoState', { nodeId, forcedPseudoClasses: ['hover'] })
    }
    // Scroll each box off its start edge so the thumb sits mid-track and reads as
    // a thumb rather than as a rounded cap in the corner.
    await page.evaluate(() => {
      document.getElementById('plain').scrollTop = 40
      document.getElementById('overlay').scrollTop = 40
      document.getElementById('fade').scrollLeft = 120
    })
    await page.waitForTimeout(380)

    const name = `${theme}-${before ? 'before' : 'after'}.png`
    await page.screenshot({ path: `${OUT}/${name}` })
    console.log(`wrote ${name}`)
    await context.close()
  }
}

await browser.close()
srv.close()

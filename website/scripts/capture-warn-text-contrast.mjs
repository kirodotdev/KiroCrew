/**
 * Screenshot harness for warn text on warn tints in light palettes (issue #20071,
 * the non-glass sequel to #19920 / #20056).
 *
 * What it has to prove: that `text-warn-text` on `bg-warn-subtle` / `bg-warn/N`
 * is READABLE in a light palette where plain `text-warn` was not. A contrast
 * ratio in a table argues about the pixel; this shows it.
 *
 * The frames exercise the REAL built stylesheet (`website/dist/assets/src-*.css`,
 * the file `index.css` compiles into) against a fixture of the chip and notice
 * shapes the source pairs the tint with, instead of a dashboard route: the change
 * is a token swap inside stylesheet rules (`--warn-text`), no component logic
 * participates, and the thing compared is a painted colour. The same call
 * capture-focus-ring.mjs made for the same reason.
 *
 * The BEFORE frame re-declares `--warn-text` as plain `--warn` under
 * `[data-mode="light"]`, appended after the stylesheet so it wins on source
 * order -- which is exactly what every pairing painted before this change. Both
 * frames come from one build, one fixture and one browser; the ONLY difference
 * is the one declaration the PR adds.
 *
 * Themes: the worst measured palette, the one that sets the token's share, and
 * the product default light palette, plus the default dark palette as the
 * control that must not move.
 *   rosepine-light    1.9:1 before, the issue's own example, and the palette
 *                     that sets the 50% share (5.4:1 on `bg-warn/30`)
 *   everforest-light  a light palette with a warm, low-contrast surface
 *   light             the default light palette (4.2:1 before on warn-subtle)
 *   dark              dark palettes keep plain `--warn`: before == after
 *
 * Each frame holds the tints the source pairs with the text: `bg-warn-subtle`,
 * `bg-warn/10`, `bg-warn/15`, `bg-warn/20` (the chat-sidebar/boardLane.tsx hover) and
 * `bg-warn/30` (the VectorMemoryCard filter pill), as a chip row and a notice.
 * The utilities are painted through the built stylesheet's own classes, so the
 * fixture names the same class strings the components do.
 *
 * Usage: node scripts/capture-warn-text-contrast.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'

const OUT = process.argv[2] || '../temp-screenshots/warn-text-contrast'
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

/** What `main` painted: the token resolves to the plain hue in light mode too. */
const BEFORE_CSS = `[data-mode="light"]{--warn-text:var(--warn)}`

const TINTS = ['bg-warn-subtle', 'bg-warn/10', 'bg-warn/15', 'bg-warn/20', 'bg-warn/30']

const chip = (tint) =>
  `<span class="inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-medium ${tint} text-warn-text">${tint}</span>`

const fixture = (theme, before) => {
  const mode = theme.endsWith('-light') || theme === 'light' ? 'light' : 'dark'
  return `<!doctype html>
<html lang="en" data-theme="${theme}" data-mode="${mode}"><head><meta charset="utf-8">
<link rel="stylesheet" href="${cssHref}">
<style>
  body{background:var(--bg);color:var(--text);font:400 13px/1.5 system-ui;
       margin:0;padding:16px 18px;display:flex;flex-direction:column;gap:12px;width:444px}
  .cap{color:var(--muted);font-size:10px;letter-spacing:.02em}
  .row{display:flex;flex-wrap:wrap;gap:6px}
  .card{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:10px 12px}
</style>
${before ? `<style>${BEFORE_CSS}</style>` : ''}
</head><body>
  <div class="cap">${theme} -- ${before ? 'before: text-warn' : 'after: text-warn-text'} on each warn tint</div>
  <div class="row">${TINTS.map(chip).join('')}</div>
  <div class="flex items-start gap-2 px-3 py-2 rounded-md border border-warn/40 bg-warn-subtle text-[13px] text-warn-text">
    This skill fails validation. Fix the frontmatter and save again.
  </div>
  <div class="card">
    <div class="cap" style="margin-bottom:6px">on a card, as a status pill</div>
    <div class="row">
      <span class="inline-flex items-center px-2 py-px rounded-full text-[10.5px] font-semibold bg-warn-subtle text-warn-text">running</span>
      <span class="inline-flex items-center gap-1 px-1.5 py-[1px] rounded-full text-[11px] font-semibold bg-warn-subtle text-warn-text">needs update</span>
      <span class="px-2 py-0.5 rounded-full text-[12px] border bg-warn/30 text-warn-text border-warn/40">episode</span>
    </div>
  </div>
</body></html>`
}

const browser = await chromium.launch()
const THEMES = ['rosepine-light', 'everforest-light', 'light', 'dark']

for (const theme of THEMES) {
  for (const before of [true, false]) {
    const context = await browser.newContext({
      viewport: { width: 480, height: 230 },
      deviceScaleFactor: 2,
      colorScheme: theme.endsWith('-light') || theme === 'light' ? 'light' : 'dark',
    })
    const page = await context.newPage()
    await page.setContent(fixture(theme, before), { waitUntil: 'load' })
    await page.waitForTimeout(200)
    const name = `${theme}-${before ? 'before' : 'after'}.png`
    await page.screenshot({ path: `${OUT}/${name}` })
    console.log(`wrote ${name}`)
    await context.close()
  }
}

await browser.close()
srv.close()

/**
 * Screenshot harness for issue #21450: the Schedule writes badge and notices
 * painted warn-hued text with `text-warn-fg` over a `bg-warn-subtle` wash, which
 * composites to ~1.4:1 (dark-on-dark) in the dark palettes. The fix swaps the
 * ink to `text-warn-text` — the warn hue meant for a warn tint.
 *
 * Like capture-warn-text-contrast.mjs (the #20071 sibling), this exercises the
 * REAL built stylesheet against a fixture of the exact element shapes the source
 * pairs the tint with, instead of a dashboard route: the change is a token swap
 * and the thing compared is a painted colour, so no component logic participates.
 *
 * The BEFORE frame re-declares the three Schedule warn elements with
 * `text-warn-fg` (what main painted); the AFTER frame uses `text-warn-text`
 * (what the PR ships). Both frames come from one build, one fixture and one
 * browser; the only difference is the token.
 *
 * Default dark theme only — that is where the regression lives (the dark palette
 * makes `--warn-fg` black, invisible on the 12% tint).
 *
 * Usage: node scripts/capture-21450-schedule-warn-contrast.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'

const OUT = process.argv[2] || '../temp-screenshots/21450-schedule-warn-contrast'
mkdirSync(OUT, { recursive: true })

const { srv, base } = await serveDist()

const cssHref = (() => {
  const assets = readdirSync(new URL('../dist/assets/', import.meta.url))
  const name = assets.find((f) => /^src-.*\.css$/.test(f))
  if (!name) throw new Error('no dist/assets/src-*.css -- run `npm run build` first')
  return `${base}/assets/${name}`
})()
console.log(`stylesheet: ${cssHref.slice(base.length)}`)

/** The three Schedule warn surfaces, parameterised by the ink class. */
const fixture = (ink) => `<!doctype html>
<html lang="en" data-theme="dark" data-mode="dark"><head><meta charset="utf-8">
<link rel="stylesheet" href="${cssHref}">
<style>
  body{background:var(--bg);color:var(--text);font:400 13px/1.5 system-ui;
       margin:0;padding:18px;display:flex;flex-direction:column;gap:14px;width:420px}
  .cap{color:var(--muted);font-size:10px;letter-spacing:.02em}
  .card{background:var(--card);border:1px solid var(--border);border-radius:20px;padding:20px;display:flex;flex-direction:column;gap:8px;align-items:flex-start;width:220px}
</style>
</head><body>
  <div class="cap">dark theme -- ${ink === 'text-warn-fg' ? 'before: text-warn-fg (dark-on-dark)' : 'after: text-warn-text'}</div>

  <!-- writes badge (SchedulePage featured card + ScheduleTemplateGallery) -->
  <div class="card">
    <div class="text-[15px] font-semibold text-text-strong leading-snug">Issue Radar sweep</div>
    <div class="text-[13px] leading-[18px] text-muted">Triage new issues and open PRs.</div>
    <span class="flex items-center gap-2 mt-auto">
      <span class="text-[12px] text-muted/80 font-medium">Every day</span>
      <span class="inline-flex items-center gap-1 text-[11px] font-medium ${ink} bg-warn-subtle rounded-full px-2 py-0.5">
        Writes to your repos
      </span>
    </span>
  </div>

  <!-- schedule-writes-notice -->
  <div class="flex items-start gap-2 px-3 py-2 rounded-lg bg-warn-subtle text-[12.5px] ${ink}">
    <span>This template can write to your repositories. It is an advisory cue, not enforced policy.</span>
  </div>

  <!-- secrets-pending note -->
  <div class="flex flex-col gap-2 px-3 py-2.5 rounded-lg bg-warn-subtle ${ink}" style="width:100%">
    <div class="flex items-center gap-1.5 text-[13px] font-semibold">Agent requested secrets</div>
    <div class="text-[11px]">Approve to inject them into this job's environment.</div>
  </div>
</body></html>`

const browser = await chromium.launch()
for (const ink of ['text-warn-fg', 'text-warn-text']) {
  const context = await browser.newContext({
    viewport: { width: 456, height: 320 },
    deviceScaleFactor: 2,
    colorScheme: 'dark',
  })
  const page = await context.newPage()
  await page.setContent(fixture(ink), { waitUntil: 'load' })
  await page.waitForTimeout(200)
  const name = `${ink === 'text-warn-fg' ? 'before' : 'after'}.png`
  await page.screenshot({ path: `${OUT}/${name}`, fullPage: true })
  console.log(`wrote ${name}`)
  await context.close()
}

await browser.close()
srv.close()

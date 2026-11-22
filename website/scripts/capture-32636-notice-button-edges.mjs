/**
 * Screenshot harness for #32636 (batch: warn-tinted notices in components/ and
 * the vault item's delete confirm). A stock `Btn` is transparent with a faint
 * `--border` edge, so on a 10-12% warn or danger tint it has no visible edge
 * until hovered. The fix gives each button the `bg-bg-elevated` fill and the
 * `border-border-strong` edge #32711 and #36509 used.
 *
 * Like capture-21450-schedule-warn-contrast.mjs, this paints the REAL built
 * stylesheet onto a fixture of the exact container and button classes the
 * source uses, instead of scripting each flow to the state that shows the
 * notice: the change is two classes and the thing compared is a painted edge.
 * BEFORE uses the stock `Btn` classes (what main renders), AFTER the same
 * string as `twMerge` resolves it with the added pair. One build, one browser.
 *
 * For each theme it captures the notices at rest, then hovers the first button
 * and keyboard-focuses the second, so hover and focus are shown as well.
 *
 * Usage: node scripts/capture-32636-notice-button-edges.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'

const OUT = process.argv[2] || '../temp-screenshots/32636-notice-button-edges'
mkdirSync(OUT, { recursive: true })

const { srv, base } = await serveDist()

// Every stylesheet the built SPA itself loads, so the fixture paints with the
// same tokens and utilities the dashboard does.
const indexHtml = readFileSync(new URL('../dist/index.html', import.meta.url), 'utf8')
const cssLinks = [...indexHtml.matchAll(/<link[^>]+rel="stylesheet"[^>]*href="([^"]+)"/g)].map((m) => m[1])
if (!cssLinks.length) throw new Error('no stylesheet in dist/index.html -- run `npm run build` first')

// The stock Btn classes from components/ui.tsx (plain and danger variants).
const STOCK = 'busy-cue inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md border text-[13px] cursor-pointer font-body transition-all disabled:cursor-not-allowed'
const PLAIN = 'border-border bg-transparent text-text hover:border-border-strong hover:bg-bg-hover disabled:opacity-30'
const DANGER = 'busy-cue-danger border-border bg-transparent text-danger hover:text-danger-text hover:border-danger hover:bg-danger-subtle disabled:opacity-30'
const fixed = (s) => s.replace('border-border ', 'border-border-strong ').replace('bg-transparent', 'bg-bg-elevated')
const btn = (after, danger, label) => {
  const v = danger ? DANGER : PLAIN
  return `<button type="button" class="${STOCK} ${after ? fixed(v) : v}">${label}</button>`
}

const fixture = (mode, after) => `<!doctype html>
<html lang="en" data-theme="${mode}" data-mode="${mode}"><head><meta charset="utf-8">
${cssLinks.map((h) => `<link rel="stylesheet" href="${base}${h.startsWith('/') ? '' : '/'}${h}">`).join('\n')}
<style>
  body{background:var(--bg);color:var(--text);font:400 13px/1.5 system-ui;margin:0;padding:18px;
       display:flex;flex-direction:column;gap:14px;width:560px}
  .cap{color:var(--muted);font-size:11px}
</style>
</head><body>
  <div class="cap">${mode} theme -- ${after ? 'after: bg-bg-elevated + border-border-strong' : 'before: stock Btn'}</div>

  <!-- DirectDeployFlow: scan-blocked (non-credential) -->
  <div class="mt-2 flex flex-col gap-2 rounded border border-warn/30 bg-warn-subtle p-2.5">
    <div class="text-[12px] text-text">1 finding blocks this deploy: internal-hostname in build/app.js:3</div>
    <div class="flex gap-2">${btn(after, true, 'Deploy anyway')}${btn(after, false, 'Cancel')}</div>
  </div>

  <!-- CrashReportNotice -->
  <div class="bg-warn/10 border border-warn/30 rounded-lg p-3">
    <div class="text-[13px] font-medium text-text">The app closed unexpectedly</div>
    <div class="text-[13px] text-muted mt-1">Diagnostics for 2 crashes were saved on this computer.</div>
    <div class="mt-3 flex gap-2">${btn(after, false, 'Show diagnostics')}${btn(after, false, 'Report issue')}</div>
  </div>

  <!-- AgentImportFlow: resolvable conflicts -->
  <div class="rounded-lg border border-warn/30 bg-warn/10 p-3 text-sm">
    <p class="font-medium text-text-strong">2 items already exist with different content.</p>
    <div class="mt-3 flex flex-wrap gap-2">${btn(after, false, 'Keep both')}${btn(after, false, 'Replace mine')}</div>
  </div>

  <!-- SecretsPanel: vault item delete confirm -->
  <div class="flex flex-wrap items-center gap-2 rounded-lg border border-danger-subtle bg-danger-subtle px-3 py-2">
    <span class="text-[13px] text-danger">Permanently delete my-key?</span>
    ${btn(after, true, 'Delete')}${btn(after, false, 'Cancel')}
  </div>
</body></html>`

const browser = await chromium.launch()
const written = []
try {
  for (const mode of ['dark', 'light']) {
    for (const after of [false, true]) {
      const context = await browser.newContext({ viewport: { width: 596, height: 470 }, deviceScaleFactor: 2 })
      const page = await context.newPage()
      await page.setContent(fixture(mode, after), { waitUntil: 'load' })
      // Btn carries `transition-all`, so a frame taken at once catches it still
      // easing from the browser's default button face: wait for it to settle.
      // A transition replaced mid-flight rejects its `finished`, so settle on all
      // of them and repeat until none is running.
      const settled = () => page.evaluate(async () => {
        for (let anims = document.getAnimations(); anims.length; anims = document.getAnimations()) {
          await Promise.allSettled(anims.map((a) => a.finished))
        }
      })
      await settled()
      const tag = `${mode}-${after ? 'after' : 'before'}`
      await page.screenshot({ path: `${OUT}/rest-${tag}.png`, fullPage: true })
      written.push(`rest-${tag}.png`)
      // Hover the first button of the first notice, keyboard-focus the second.
      await page.locator('button').nth(0).hover()
      await page.locator('button').nth(0).focus()
      await page.keyboard.press('Tab')
      await settled()
      await page.screenshot({ path: `${OUT}/hover-focus-${tag}.png`, fullPage: true })
      written.push(`hover-focus-${tag}.png`)
      await context.close()
    }
  }
} finally {
  await browser.close()
  srv.close()
}
console.log(`wrote ${written.length} frames to ${OUT}:\n  ${written.join('\n  ')}`)

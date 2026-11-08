/**
 * Screenshot harness for the delayed, app-shaped AppHost loading fallback.
 * House pattern of website/capture/shoot-focus-mode-exit-notice.mjs: the REAL
 * built SPA (website/dist) behind the in-process loopback static server, every
 * /api/** answered from fixtures via Playwright route interception, and the
 * app's ESM bundle (`/apps/<name>/ui/index.mjs`) served by a route handler that
 * holds it for a chosen time. Nothing listens beyond 127.0.0.1 and no gateway
 * is involved.
 *
 * Frames, per theme (dark, light):
 *   layout-<layout>-<theme>   `ui.loading.layout` = page | rail | centered | split,
 *                             bundle held 5 s, shot once the placeholder is up.
 *   layout-<rail|split>-narrow-<theme>
 *                             the same at a 390px phone width: no rail, and
 *                             the split panes stacked.
 *   filmstrip-fast-<theme>    bundle held 120 ms: frames from navigation to
 *                             mount, laid side by side. No placeholder appears.
 *   filmstrip-slow-<theme>    bundle held 1.5 s: blank for the first 300 ms,
 *                             then the placeholder, then the app.
 * Each filmstrip also prints the in-page timeline a MutationObserver recorded
 * (when the placeholder and the app body first appeared), which is the
 * frame-rate-independent proof that a fast load never paints the placeholder.
 *
 * Usage (from website/, after `npm run build`):
 *   node capture/shoot-app-loading-layouts.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { serveDist } from '../scripts/lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from '../scripts/lib/stub-dashboard-api.mjs'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const OUT = process.argv[2] || '../temp-screenshots/app-loading-layouts'
mkdirSync(OUT, { recursive: true })

const LAYOUTS = ['page', 'rail', 'centered', 'split']

const appRecord = (name, layout) => ({
  name,
  version: '1.0.0',
  displayName: 'Ledger Lens',
  enabled: true,
  origin: 'local',
  manifest: {
    name,
    version: '1.0.0',
    displayName: 'Ledger Lens',
    description: 'Reads your books and explains them.',
    ui: {
      entry: 'index.mjs',
      ...(layout ? { loading: { layout } } : {}),
      pages: [{ route: `/apps/${name}`, label: 'Ledger Lens' }],
    },
  },
})

// The "app": resolves `react` through the host's import map, like a real bundle.
const BUNDLE = `
import React from 'react'
const h = React.createElement
export default function LedgerLens() {
  return h('div', { 'data-testid': 'the-app', style: { padding: '24px', color: 'var(--text)' } },
    h('h2', { style: { fontSize: '20px', fontWeight: 600, margin: '0 0 8px' } }, 'Ledger Lens'),
    h('p', { style: { color: 'var(--muted)', margin: 0 } }, 'Your books, explained.'))
}
`

const { srv, base } = await serveDist()
const browser = await chromium.launch({
  executablePath: chromiumExecutable(),
  env: { ...process.env, LD_LIBRARY_PATH: '' },
})

async function openApp(theme, { name, layout, holdMs, reducedMotion = 'no-preference', width = 1280, height = 760 }) {
  const page = await browser.newPage({ viewport: { width, height }, reducedMotion, serviceWorkers: 'block' })
  logPageProblems(page)
  const extra = async (path, route) => {
    if (path === `/api/apps/${name}`) return json(route, appRecord(name, layout)), true
    if (path === '/api/apps') return json(route, [appRecord(name, layout)]), true
    return false
  }
  await stubDashboardApi(page, { theme, extra })
  // The loopback server has no `.mjs` MIME type, and a module script must be
  // served as JavaScript: hand the host's vendored shims over directly.
  await page.route('**/vendor/*.mjs', route => route.fulfill({
    status: 200,
    contentType: 'text/javascript',
    body: readFileSync(fileURLToPath(new URL(`../dist${new URL(route.request().url()).pathname}`, import.meta.url))),
  }))
  await page.route(`**/apps/${name}/ui/**`, async route => {
    await page.evaluate(() => { window.__loadTimeline.requested ??= performance.now() }).catch(() => {})
    await new Promise(r => setTimeout(r, holdMs))
    await route.fulfill({ status: 200, contentType: 'text/javascript', body: BUNDLE })
  })
  // Timeline, relative to the moment the app's bundle was requested.
  await page.addInitScript(() => {
    const t = { requested: null, fallback: null, app: null }
    window.__loadTimeline = t
    new MutationObserver(() => {
      const now = performance.now()
      if (t.fallback === null && document.querySelector('[data-testid="app-loading-fallback"]')) t.fallback = now
      if (t.app === null && document.querySelector('[data-testid="the-app"]')) t.app = now
    }).observe(document, { subtree: true, childList: true })
  })
  await page.goto(`${base}/apps/${name}`, { waitUntil: 'domcontentloaded' })
  return page
}

for (const theme of ['dark', 'light']) {
  for (const layout of LAYOUTS) {
    const name = `ledger-lens-${layout}`
    const page = await openApp(theme, { name, layout, holdMs: 5000 })
    await page.getByTestId('app-loading-fallback').waitFor({ timeout: 15000 })
    await page.waitForTimeout(450) // past the 200 ms fade
    const got = await page.getByTestId('app-loading-fallback').getAttribute('data-layout')
    if (got !== layout) throw new Error(`expected layout ${layout}, got ${got}`)
    await page.screenshot({ path: `${OUT}/layout-${layout}-${theme}.png` })
    await page.close()
  }

  for (const layout of ['rail', 'split']) {
    const name = `ledger-lens-${layout}-narrow`
    const page = await openApp(theme, { name, layout, holdMs: 5000, width: 390, height: 844 })
    await page.getByTestId('app-loading-fallback').waitFor({ timeout: 15000 })
    await page.waitForTimeout(450)
    await page.screenshot({ path: `${OUT}/layout-${layout}-narrow-${theme}.png` })
    await page.close()
  }

  for (const [label, holdMs] of [['fast', 120], ['slow', 1500]]) {
    const name = `ledger-lens-${label}`
    const page = await openApp(theme, { name, layout: 'rail', holdMs })
    const frames = []
    const t0 = Date.now()
    // Shoot as fast as the browser allows until the app has been up for a beat.
    let appSeenAt = null
    while (Date.now() - t0 < 6000) {
      // State read BEFORE the shot: anything present then is in the frame, so a
      // caption never claims what the picture does not show.
      const hasFallback = await page.getByTestId('app-loading-fallback').count()
      const hasApp = await page.getByTestId('the-app').count()
      const at = Date.now() - t0
      const buf = await page.screenshot({ scale: 'css' })
      frames.push({ at, buf, hasFallback, hasApp })
      if (hasApp && appSeenAt === null) appSeenAt = at
      if (appSeenAt !== null && at - appSeenAt > 150) break
    }
    const tl = await page.evaluate(() => window.__loadTimeline)
    const rel = v => (v === null || tl.requested === null ? null : Math.round(v - tl.requested))
    const summary = { label, holdMs, fallbackAtMs: rel(tl.fallback), appAtMs: rel(tl.app), frames: frames.length }
    console.log(JSON.stringify({ theme, ...summary }))
    await page.close()

    // Pick at most 6 frames evenly, always keeping the first and last.
    const pick = frames.length <= 6 ? frames
      : [...Array(6).keys()].map(i => frames[Math.round(i * (frames.length - 1) / 5)])
    const cells = pick.map(f => {
      const tag = f.hasApp ? 'app' : f.hasFallback ? 'placeholder' : 'blank'
      return `<figure><img src="data:image/png;base64,${f.buf.toString('base64')}"/>`
        + `<figcaption>${f.at} ms after navigation · ${tag}</figcaption></figure>`
    }).join('')
    const title = label === 'fast'
      ? `Fast load (bundle ${holdMs} ms): placeholder ${summary.fallbackAtMs === null ? 'never painted' : `at ${summary.fallbackAtMs} ms`}, app at ${summary.appAtMs} ms`
      : `Slow load (bundle ${holdMs} ms): placeholder at ${summary.fallbackAtMs} ms, app at ${summary.appAtMs} ms`
    const html = `<!doctype html><html data-theme="${theme}"><body style="margin:0;padding:16px;font:13px system-ui;background:${theme === 'dark' ? '#111' : '#f4f4f5'};color:${theme === 'dark' ? '#ddd' : '#222'}">
      <div style="margin:0 0 10px;font-weight:600">${title}</div>
      <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px">${cells}</div>
      <style>figure{margin:0}img{width:100%;border:1px solid #8884;border-radius:6px;display:block}figcaption{margin-top:4px;opacity:.8}</style>
      </body></html>`
    const sheet = await browser.newPage({ viewport: { width: 1600, height: 900 } })
    await sheet.setContent(html, { waitUntil: 'load' })
    await sheet.screenshot({ path: `${OUT}/filmstrip-${label}-${theme}.png`, fullPage: true })
    writeFileSync(`${OUT}/filmstrip-${label}-${theme}.json`, JSON.stringify({ theme, ...summary,
      frames: frames.map(({ at, hasFallback, hasApp }) => ({ at, hasFallback, hasApp })) }, null, 2))
    await sheet.close()
  }
}

await browser.close()
srv.close()

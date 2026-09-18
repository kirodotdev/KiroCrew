/**
 * Screenshots for issue #10620, residual 3: channel message bodies and the
 * notification detail body wrapped in the per-item `MessageErrorBoundary`
 * (capture/bare-markdown-error-boundary.html).
 *
 * Boots Vite in-process with ONE alias: the `MarkdownRenderer` module resolves
 * to `capture/stubs/CrashingMarkdownRenderer.tsx`, which throws on a marker
 * string and forwards everything else to the real renderer. That is the
 * stand-in for "the next spelling the depth bound does not model" -- the real
 * renderer has no known crash on main and this follow-up must not publish one.
 *
 * Self-checking: each frame asserts the crashed item shows the boundary
 * fallback, the healthy neighbours / header rendered, and nothing reached the
 * page level (no uncaught pageerror). A frame of the wrong state is worse
 * evidence than none.
 *
 * Usage:
 *   node scripts/capture-bare-markdown-error-boundary.mjs [../temp-screenshots/10620-bare-markdown-error-boundary]
 */
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import { createServer } from 'vite'

import { chromiumExecutable } from './lib/chromium-executable.mjs'

const OUT = process.argv[2]
  || fileURLToPath(new URL('../../temp-screenshots/10620-bare-markdown-error-boundary/', import.meta.url))
mkdirSync(OUT, { recursive: true })

const ROOT = fileURLToPath(new URL('../', import.meta.url))
const vite = await createServer({
  root: ROOT,
  configFile: join(ROOT, 'vite.config.ts'),
  server: { host: '127.0.0.1', port: 0, strictPort: false },
  logLevel: 'warn',
  resolve: {
    alias: [{
      // Any relative import of the renderer module (`../components/MarkdownRenderer`,
      // `../MarkdownRenderer`) lands on the stand-in. The stand-in itself imports
      // the real file with its extension, which this pattern does not match.
      find: /^(\.\.\/)+(components\/)?MarkdownRenderer$/,
      replacement: join(ROOT, 'capture/stubs/CrashingMarkdownRenderer.tsx'),
    }],
  },
})
await vite.listen()
const { port } = vite.httpServer.address()
const base = `http://127.0.0.1:${port}`

const FALLBACK = 'Message failed to render'

const browser = await chromium.launch({ executablePath: chromiumExecutable() })
try {
  for (const theme of ['dark', 'light']) {
    const page = await (await browser.newContext({ viewport: { width: 1100, height: 720 }, deviceScaleFactor: 2 })).newPage()
    const pageErrors = []
    page.on('pageerror', (e) => pageErrors.push(e.message))

    // Channel: one poisoned message between two healthy ones.
    await page.goto(`${base}/capture/bare-markdown-error-boundary.html?scene=channel&theme=${theme}`, { waitUntil: 'networkidle' })
    await page.getByText(FALLBACK).waitFor({ state: 'visible', timeout: 20_000 })
    for (const healthy of ['Checkout p99 doubled at 14:02', 'Correlates with the rollout', 'Incident 4412']) {
      if ((await page.getByText(healthy, { exact: false }).count()) === 0) throw new Error(`channel/${theme}: healthy content "${healthy}" did not render -- the crash escaped the item`)
    }
    if ((await page.getByText(FALLBACK).count()) !== 1) throw new Error(`channel/${theme}: expected exactly one fallback`)
    await page.screenshot({ path: join(OUT, `channel-${theme}.png`) })
    console.log(`captured channel-${theme}.png`)

    // Notification detail: poisoned body, header and actions intact.
    await page.goto(`${base}/capture/bare-markdown-error-boundary.html?scene=notification&theme=${theme}`, { waitUntil: 'networkidle' })
    await page.getByText(FALLBACK).waitFor({ state: 'visible', timeout: 20_000 })
    if ((await page.getByText('Nightly report finished').count()) === 0) throw new Error(`notification/${theme}: the panel header did not render -- the crash escaped the item`)
    await page.screenshot({ path: join(OUT, `notification-${theme}.png`) })
    console.log(`captured notification-${theme}.png`)

    // React re-throws a caught render error to window.onerror in dev builds, so
    // the boundary's own catch shows up here by design; anything else is a
    // real escape.
    const foreign = pageErrors.filter(m => !m.includes('simulated markdown render crash'))
    if (foreign.length) throw new Error(`${theme}: uncaught page errors: ${foreign.join(' | ')}`)
    await page.context().close()
  }
} finally {
  await browser.close()
  await vite.close()
}
console.log(`done → ${OUT}`)

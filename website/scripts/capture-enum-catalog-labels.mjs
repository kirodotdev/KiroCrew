/**
 * Screenshots for #8487: four wire-enum values that now render through the i18n
 * catalog instead of a raw wire string CSS-capitalized. Evidence has to be a
 * screenshot because the change is a rendered STRING — a diff cannot show that
 * the pane now reads "Conflicting"/"Clean (hooks)"/"Modified"/"Connected", nor
 * that an UNMAPPED Jira state still reads "In Review" (capitalized) beside the
 * catalog-cased "Merged" (the Fix-1 casing split). Shot in English AND zh-CN,
 * because a lone English "Modified" is indistinguishable from the old raw
 * "modified" with `capitalize` — only the non-English frame proves the string
 * comes from the catalog.
 *
 * Boots Vite in-process to serve capture/enum-catalog-labels.html, which mounts
 * the four REAL components with fixture inputs (see that file). Each frame is
 * self-checking: it waits for a catalog label to appear and fails loudly if a
 * surface rendered blank, so a broken frame is never published as evidence.
 *
 * Usage:
 *   node scripts/capture-enum-catalog-labels.mjs [../temp-screenshots/8487-enum-catalog-labels]
 */
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import { createServer } from 'vite'

import { chromiumExecutable } from './lib/chromium-executable.mjs'

const OUT = process.argv[2]
  || fileURLToPath(new URL('../../temp-screenshots/8487-enum-catalog-labels/', import.meta.url))
mkdirSync(OUT, { recursive: true })

const ROOT = fileURLToPath(new URL('../', import.meta.url))
const vite = await createServer({
  root: ROOT,
  configFile: join(ROOT, 'vite.config.ts'),
  server: { host: '127.0.0.1', port: 0, strictPort: false },
  logLevel: 'warn',
})
await vite.listen()
const { port } = vite.httpServer.address()
const base = `http://127.0.0.1:${port}`

// One label per surface that ONLY the catalog can produce, per locale — the
// readiness signal and the proof the string is translated, not CSS-cased.
const EXPECT = {
  en: ['Clean (hooks)', 'Modified', 'Merged', 'In Review', 'Connected'],
  'zh-CN': ['干净（钩子）', '已修改', '已合并', '已连接'],
}

const browser = await chromium.launch({ executablePath: chromiumExecutable() })
try {
  for (const lang of ['en', 'zh-CN']) {
    const context = await browser.newContext({ viewport: { width: 960, height: 1400 }, deviceScaleFactor: 2 })
    const page = await context.newPage()
    const pageErrors = []
    page.on('pageerror', (e) => pageErrors.push(e.message))

    await page.goto(`${base}/capture/enum-catalog-labels.html?theme=dark&lang=${lang}`, { waitUntil: 'networkidle' })

    // The IssuePanel opens on its Description tab; its linked-change states live
    // on the Linked tab, so switch to it before shooting. The tab label is a
    // catalog string, matched by its stable count suffix "2".
    const linkedTab = page.getByRole('tab', { name: /2/ }).last()
    await linkedTab.click({ timeout: 25_000 }).catch(() => {})

    // Every surface must have rendered its catalog label before we shoot.
    for (const label of EXPECT[lang]) {
      await page.getByText(label, { exact: false }).first().waitFor({ state: 'visible', timeout: 25_000 })
    }

    // Crop to the capture root so the four sections are one frame.
    const root = page.locator('[data-capture-root]')
    await root.screenshot({ path: join(OUT, `enum-labels-${lang}.png`) })
    console.log(`captured enum-labels-${lang}.png`)

    if (pageErrors.length) throw new Error(`${lang}: uncaught page errors: ${pageErrors.join(' | ')}`)
    await context.close()
  }
} finally {
  await browser.close()
  await vite.close()
}
console.log(`done → ${OUT}`)

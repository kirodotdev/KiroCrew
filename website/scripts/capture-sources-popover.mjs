/**
 * Screenshot for PR #14970: the App Store Sources popover
 * (components/appstore/SourcesPopover.tsx), open, showing the three
 * registry-trust surfaces this PR touched on ONE frame:
 *   (a) the store-wide registry-trust hint with its Settings → Security link,
 *   (b) a registries-editor row dropped by the merge (served:false,
 *       name_collision) carrying the "Not listed — …" note, and
 *   (c) a normal index-tier operator row with its per-row trust hint.
 *
 * Boots Vite in-process to serve capture/appstore-sources.html?popover=1, which
 * mounts the REAL SourcesPopover against the real stylesheet with
 * GET /api/apps/registries mocked to return those rows (see that entry file).
 * The frame is self-checking: it waits for the text a reviewer needs to see and
 * fails loudly if the surface rendered blank or wrong, so a broken frame is
 * never published as evidence.
 *
 * Usage:
 *   node scripts/capture-sources-popover.mjs [outDir]
 * Default outDir: $KIROCREW_SCRATCH/shots-14970
 */
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import { createServer } from 'vite'

import { chromiumExecutable } from './lib/chromium-executable.mjs'

const OUT = process.argv[2]
  || join(process.env.KIROCREW_SCRATCH || '/tmp', 'shots-14970')
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

// The three strings the frame must render before the shot — each both the
// readiness signal AND proof the scene drew what it claims.
const HINT = 'Private repositories in a registry stay generic'
const NOT_LISTED = 'Not listed — another registry you added has the same name'

const browser = await chromium.launch({ executablePath: chromiumExecutable() })
try {
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  const pageErrors = []
  page.on('pageerror', e => pageErrors.push(e.message))

  await page.goto(`${base}/capture/appstore-sources.html?theme=dark&popover=1`, { waitUntil: 'networkidle' })

  // The not-served note proves the dropped row; the hint (rendered store-wide AND
  // per row) proves the index-tier surfaces. Both must be present.
  await page.getByText(NOT_LISTED, { exact: false }).first().waitFor({ state: 'visible', timeout: 25_000 })
  await page.getByText(HINT, { exact: false }).first().waitFor({ state: 'visible', timeout: 25_000 })
  // A per-row index-tier row: its repo url is on screen (rendered under the row).
  await page.getByText('git.example.test/team/apps-index', { exact: false }).first()
    .waitFor({ state: 'visible', timeout: 25_000 })

  await page.waitForTimeout(300)
  await page.screenshot({ path: join(OUT, 'sources-popover-dark.png') })
  console.log('captured sources-popover-dark.png')

  if (pageErrors.length) throw new Error(`sources-popover: uncaught page errors: ${pageErrors.join(' | ')}`)
  await context.close()
} finally {
  await browser.close()
  await vite.close()
}
console.log(`done → ${OUT}`)

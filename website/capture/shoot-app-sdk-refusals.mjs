/**
 * Screenshot harness for the App SDK refusals (capture/app-sdk-refusals.tsx).
 *
 * Serves the isolated entry with a programmatic Vite dev server on loopback,
 * picks the file in the "diff" frame so its diff pane shows the diff refusal,
 * then shoots the page and each frame: dark and light at 1024px (the diff frame's
 * list sits beside its narrow diff column there), dark at 320px (stacked).
 * The one request the entry sends (the diff frame's file list) is answered
 * inside the entry; no gateway or token is involved.
 *
 * Usage (from website/): node capture/shoot-app-sdk-refusals.mjs [outDir] [lang]
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const OUT = process.argv[2] || resolve(dirname(fileURLToPath(import.meta.url)), '../../temp-screenshots/app-sdk-refusals')
const LANG = process.argv[3] || 'en'
mkdirSync(OUT, { recursive: true })

const server = await createServer({ configFile: './vite.config.ts', server: { host: '127.0.0.1', port: 5198, strictPort: false }, logLevel: 'warn' })
await server.listen()
const origin = server.resolvedUrls?.local?.[0]?.replace(/\/$/, '') || `http://127.0.0.1:${server.config.server.port}`
const browser = await chromium.launch({ executablePath: chromiumExecutable(), env: { ...process.env, LD_LIBRARY_PATH: '' } })

const FRAMES = ['pr', 'picker', 'issues', 'changes', 'diff']
for (const [theme, width] of [['dark', 1024], ['light', 1024], ['dark', 320]]) {
  const page = await browser.newPage({ viewport: { width, height: 1600 }, deviceScaleFactor: 2 })
  page.on('pageerror', e => console.log('PAGE EXCEPTION:', e.message))
  await page.goto(`${origin}/capture/app-sdk-refusals.html?theme=${theme}&lang=${LANG}`)
  const diff = page.getByTestId('frame-diff')
  await diff.getByTestId('session-file-row').waitFor({ timeout: 20000 })
  await diff.getByTestId('session-file-diff').click()
  await diff.getByTestId('app-session-changes-diff-not-declared').waitFor({ timeout: 20000 })
  for (const id of FRAMES) await page.getByTestId(`frame-${id}`).getByRole('alert').first().waitFor({ timeout: 20000 })
  await page.waitForTimeout(400)
  await page.getByTestId('frames').screenshot({ path: `${OUT}/all-${LANG}-${theme}-${width}.png` })
  for (const id of FRAMES) {
    const file = `${OUT}/${id}-${LANG}-${theme}-${width}.png`
    await page.getByTestId(`frame-${id}`).screenshot({ path: file })
    console.log('wrote', file)
  }
  await page.close()
}

await browser.close()
await server.close()

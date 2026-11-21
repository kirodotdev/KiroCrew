/**
 * Screenshot harness for the App SDK file tree (capture/app-sdk-file-tree.tsx).
 *
 * Serves the isolated entry with a programmatic Vite dev server on loopback.
 * Shots: the tree with `src` and `src/components` open and a file opened, in
 * both themes; the same with the git status read declared (changed rows
 * coloured); a typed filter; the refusal for an app that declares only the
 * status read; and a failed tree read with its Retry. Every response is
 * fabricated inside the entry; no gateway or token is involved.
 *
 * Usage (from website/): node capture/shoot-app-sdk-file-tree.mjs [outDir]
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const OUT = process.argv[2] || resolve(dirname(fileURLToPath(import.meta.url)), '../../temp-screenshots/app-sdk-file-tree')
mkdirSync(OUT, { recursive: true })

const server = await createServer({
  configFile: './vite.config.ts',
  server: { host: '127.0.0.1', port: 5199, strictPort: false },
  logLevel: 'warn',
})
await server.listen()
const base = server.resolvedUrls?.local?.[0]?.replace(/\/$/, '') || `http://127.0.0.1:${server.config.server.port}`

const browser = await chromium.launch({
  executablePath: chromiumExecutable(),
  env: { ...process.env, LD_LIBRARY_PATH: '' },
})

/** [name, scene, theme, filter] */
const shots = [
  ['tree', 'tree', 'dark', ''], ['tree', 'tree', 'light', ''],
  ['status', 'status', 'dark', ''], ['status', 'status', 'light', ''],
  ['filter', 'status', 'dark', 'list'],
  ['refused', 'refused', 'dark', ''],
  ['error', 'error', 'light', ''],
  ['status-error', 'status-error', 'dark', ''],
]
for (const [name, scene, theme, filter] of shots) {
  const page = await browser.newPage({ viewport: { width: 800, height: 600 }, deviceScaleFactor: 2 })
  page.on('pageerror', e => console.log('PAGE EXCEPTION:', e.message))
  page.on('console', m => { if (m.type() === 'error') console.log('CONSOLE:', m.text()) })
  await page.goto(`${base}/capture/app-sdk-file-tree.html?scene=${scene}&theme=${theme}`)
  if (scene === 'refused') await page.getByTestId('app-file-tree-not-declared').waitFor({ timeout: 20000 })
  else if (scene === 'error') await page.getByTestId('app-file-tree-error').waitFor({ timeout: 20000 })
  else {
    // Rows live in the tree's open shadow root, which CSS locators pierce.
    const row = p => page.locator(`[data-type="item"][data-item-path="${p}"], [data-type="item"][data-item-path="${p}/"]`).first()
    await row('README.md').waitFor({ timeout: 20000 })
    if (filter) {
      await page.getByLabel('Filter files').fill(filter)
    } else {
      await row('src').click({ timeout: 8000 }).catch(async e => {
        await page.screenshot({ path: `${OUT}/debug-${name}-${theme}.png` })
        throw e
      })
      await row('src/components').click()
      await row('src/app.tsx').click()
      await page.getByTestId('opened').getByText('/home/me/widgets/src/app.tsx').waitFor({ timeout: 10000 })
    }
    if (scene === 'status-error') await page.getByTestId('app-file-tree-status-error').waitFor({ timeout: 30000 })
  }
  await page.waitForTimeout(500)
  const path = `${OUT}/${name}-${theme}.png`
  await page.getByTestId('frame').first().screenshot({ path }).catch(async () => {
    await page.screenshot({ path })
  })
  console.log('wrote', path)
  await page.close()
}

await browser.close()
await server.close()

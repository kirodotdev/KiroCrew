/**
 * Screenshot harness for the conductor mode Board's "N dropped" milestone fold.
 *
 * Serves the isolated capture entry (capture/conductor-mode-run-states.tsx) with a
 * programmatic Vite dev server on loopback and photographs the REAL
 * ConductorModeCrewPanel at side-panel width: open work above a closed dropped
 * fold, the same fold opened, and a run whose only leftovers are abandoned.
 * The reads are stubbed inside the entry, so no gateway / kiro-cli / token is involved.
 *
 * Usage (from website/): node capture/shoot-conductor-mode-dropped-fold.mjs [outDir]
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const OUT = process.argv[2] || resolve(dirname(fileURLToPath(import.meta.url)), '../../temp-screenshots/conductor-mode-dropped-fold')
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
const page = await browser.newPage({ viewport: { width: 420, height: 800 }, deviceScaleFactor: 2 })
page.on('pageerror', e => console.log('PAGE EXCEPTION:', e.message))

const shoot = async (name) => {
  await page.waitForTimeout(400)
  await page.locator('[data-testid="capture-frame"]').screenshot({ path: `${OUT}/${name}.png` })
  console.log('wrote', `${OUT}/${name}.png`)
}

for (const theme of ['dark', 'light']) {
  await page.goto(`${base}/capture/conductor-mode-run-states.html?state=dropped&theme=${theme}`)
  await page.locator('[data-testid="conductor-mode-milestones-dropped"]').waitFor({ timeout: 30000 })
  await shoot(`open-list-${theme}`)
  await page.locator('[data-testid="conductor-mode-milestones-dropped"] summary').click()
  await shoot(`dropped-open-${theme}`)
  await page.goto(`${base}/capture/conductor-mode-run-states.html?state=only-abandoned&theme=${theme}`)
  await page.locator('[data-testid="conductor-mode-milestones-dropped"]').waitFor({ timeout: 30000 })
  await shoot(`only-abandoned-${theme}`)
}

await browser.close()
await server.close()

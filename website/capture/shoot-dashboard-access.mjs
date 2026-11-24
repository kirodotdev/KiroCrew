/**
 * Screenshot harness for the app detail page's "Dashboard access" card.
 *
 * Serves capture/dashboard-access.tsx with a programmatic Vite dev server on
 * loopback and photographs three frames: the needs-access state (the app asked
 * and was refused), the granted flow (after the owner confirms: the access is on
 * and a restart is offered), and the platform reason where the sandbox cannot bind
 * the access. These are component renders with the three API calls answered
 * locally, not the live page.
 *
 * Usage (from website/): node capture/shoot-dashboard-access.mjs [outDir]
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const OUT = process.argv[2] || resolve(dirname(fileURLToPath(import.meta.url)), '../../temp-screenshots/dashboard-access')
mkdirSync(OUT, { recursive: true })

const server = await createServer({
  configFile: './vite.config.ts',
  server: { host: '127.0.0.1', port: 5198, strictPort: false },
  logLevel: 'warn',
})
await server.listen()
const base = server.resolvedUrls?.local?.[0]?.replace(/\/$/, '') || `http://127.0.0.1:${server.config.server.port}`

const browser = await chromium.launch({
  executablePath: chromiumExecutable(),
  env: { ...process.env, LD_LIBRARY_PATH: '' },
})
const page = await browser.newPage({ viewport: { width: 720, height: 420 }, deviceScaleFactor: 2 })
page.on('pageerror', e => console.log('PAGE EXCEPTION:', e.message))
page.on('console', m => { if (m.type() === 'error') console.log('CONSOLE ERROR:', m.text()) })
const shoot = async name => {
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/${name}.png`, animations: 'disabled' })
  console.log('wrote', `${OUT}/${name}.png`)
}

await page.goto(`${base}/capture/dashboard-access.html?scene=needs`)
await page.getByTestId('dashboard-access-needed').waitFor({ timeout: 20000 })
await shoot('1-needs-access')

await page.getByRole('button', { name: 'Allow dashboard access' }).click()
await page.getByRole('dialog').waitFor({ timeout: 5000 })
await shoot('2-confirm')
await page.getByRole('dialog').getByRole('button', { name: 'Allow dashboard access' }).click()
await page.getByTestId('dashboard-access-restart').waitFor({ timeout: 5000 })
await shoot('3-granted-restart-offered')

await page.goto(`${base}/capture/dashboard-access.html?scene=unsupported`)
await page.getByTestId('dashboard-access-unsupported').waitFor({ timeout: 20000 })
await shoot('4-platform-unsupported')

await browser.close()
await server.close()

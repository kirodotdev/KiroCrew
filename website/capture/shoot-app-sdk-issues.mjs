/**
 * Screenshot harness for the App SDK issue list (capture/app-sdk-issues.tsx).
 *
 * Serves the isolated entry with a programmatic Vite dev server on loopback.
 * Shots: condensed and expanded views at 720px in both themes, both views at
 * 320px, the refusal for an app that declares only /api/source/issue, "Assigned
 * to me" on, and the empty, loading and error states. Every
 * response is fabricated inside the entry; no gateway or token is involved.
 *
 * Usage (from website/): node capture/shoot-app-sdk-issues.mjs [outDir]
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const OUT = process.argv[2] || resolve(dirname(fileURLToPath(import.meta.url)), '../../temp-screenshots/app-sdk-issues')
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

const shots = [
  ['condensed', 'dark', 760], ['condensed', 'light', 760],
  ['expanded', 'dark', 760], ['expanded', 'light', 760],
  ['condensed', 'dark', 320], ['expanded', 'light', 320],
  ['refused', 'dark', 760],
  ['mine', 'dark', 760], ['empty', 'light', 760], ['loading', 'dark', 760], ['error', 'light', 760],
]
for (const [scene, theme, width] of shots) {
  const page = await browser.newPage({ viewport: { width, height: 1800 }, deviceScaleFactor: 2 })
  page.on('pageerror', e => console.log('PAGE EXCEPTION:', e.message))
  await page.goto(`${base}/capture/app-sdk-issues.html?scene=${scene === 'mine' ? 'condensed' : scene}&theme=${theme}`)
  if (scene === 'refused' || scene === 'error') await page.getByRole('alert').first().waitFor({ timeout: 20000 })
  else if (scene === 'empty') await page.getByTestId('app-issue-list-empty').waitFor({ timeout: 20000 })
  else if (scene === 'loading') await page.getByTestId('issue-list-loading').waitFor({ timeout: 20000 })
  else await page.getByText('CREW-142').first().waitFor({ timeout: 20000 })
  if (scene === 'mine') {
    await page.getByRole('button', { name: 'Assigned to me' }).click()
    await page.waitForFunction(() => document.querySelector('[aria-pressed="true"]'))
  }
  await page.waitForTimeout(400)
  const path = `${OUT}/${scene}-${theme}-${width}.png`
  await page.getByTestId('frame').screenshot({ path })
  console.log('wrote', path)
  await page.close()
}

await browser.close()
await server.close()

/**
 * Photographs capture/muted-meta-contrast.tsx (the REAL AssistantMessage
 * footer) in each palette named in SHOT_THEMES (default `dark,light`), and
 * prints the computed colour of the credits line and its surface.
 *
 * Usage (from website/): SHOT_PREFIX=before|after [SHOT_THEMES=nord-dark,…] node capture/shoot-muted-meta-contrast.mjs <outDir>
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const OUT = process.argv[2]
mkdirSync(OUT, { recursive: true })
const prefix = process.env.SHOT_PREFIX || 'shot'
const themes = (process.env.SHOT_THEMES || 'dark,light').split(',').map(t => t.trim()).filter(Boolean)

const server = await createServer({ configFile: './vite.config.ts', server: { host: '127.0.0.1', port: 5199, strictPort: false }, logLevel: 'warn' })
await server.listen()
const base = server.resolvedUrls?.local?.[0]?.replace(/\/$/, '') || `http://127.0.0.1:${server.config.server.port}`
const browser = await chromium.launch({ executablePath: chromiumExecutable(), env: { ...process.env, LD_LIBRARY_PATH: '' } })
const page = await browser.newPage({ viewport: { width: 900, height: 600 }, deviceScaleFactor: 2 })
page.on('pageerror', e => console.log('PAGE EXCEPTION:', e.message))

for (const theme of themes) {
  await page.goto(`${base}/capture/muted-meta-contrast.html?theme=${theme}`)
  await page.locator('[data-testid="turn-stats"]').first().waitFor({ timeout: 30000 })
  await page.waitForTimeout(500)
  const colors = await page.evaluate(() => [...document.querySelectorAll('[data-testid^="on-"]')].map(box => ({
    box: box.getAttribute('data-testid'),
    surface: getComputedStyle(box.getAttribute('data-testid') === 'on-bg' ? box.parentElement : box).backgroundColor,
    credits: getComputedStyle(box.querySelector('[data-testid="turn-stats"]')).color,
  })))
  console.log(theme, JSON.stringify(colors))
  const path = `${OUT}/${prefix}-${theme}.png`
  await page.locator('[data-capture-root]').screenshot({ path })
  console.log('wrote', path)
}
await browser.close()
await server.close()

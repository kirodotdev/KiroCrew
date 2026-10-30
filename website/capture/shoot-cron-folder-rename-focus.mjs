// Shoot where focus lands after the cron folder rename editor closes.
// Frames: after Enter, after Escape (focus ring on the collapse toggle).
// Run from website/: node capture/shoot-cron-folder-rename-focus.mjs [outDir]
import { createServer } from 'vite'
import { chromium } from 'playwright-core'
import path from 'node:path'
import { mkdirSync } from 'node:fs'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const outDir = process.argv[2] || '../temp-screenshots/cron-folder-rename-focus'
mkdirSync(outDir, { recursive: true })

const server = await createServer({
  configFile: 'vite.config.ts',
  server: { port: 5198, strictPort: true, host: '127.0.0.1' },
})
await server.listen()
const browser = await chromium.launch({ executablePath: chromiumExecutable() })

for (const key of ['Enter', 'Escape']) {
  const ctx = await browser.newContext({ viewport: { width: 700, height: 200 }, reducedMotion: 'reduce' })
  const page = await ctx.newPage()
  await page.goto('http://127.0.0.1:5198/capture/cron-folder-rename-focus.html?theme=dark')
  const actions = page.getByRole('button', { name: 'Folder actions' })
  await actions.waitFor({ timeout: 45000 })
  await actions.focus()
  await page.keyboard.press('Enter')
  await page.getByRole('menuitem', { name: 'Rename' }).waitFor()
  await page.keyboard.press('Enter')
  const input = page.getByRole('textbox', { name: 'Rename' })
  await input.waitFor()
  await input.fill(key === 'Enter' ? 'Nightly reports' : 'Discarded name')
  await page.keyboard.press(key)
  const toggle = page.getByRole('button', { name: /^Collapse folder/ })
  await page.waitForFunction(() => document.activeElement?.getAttribute('aria-label')?.startsWith('Collapse folder'))
  const label = await toggle.getAttribute('aria-label')
  console.log(key, '-> focused:', label)
  await page.getByTestId('scene').screenshot({ path: path.join(outDir, `after-${key.toLowerCase()}.png`) })
  await ctx.close()
}
await browser.close()
await server.close()

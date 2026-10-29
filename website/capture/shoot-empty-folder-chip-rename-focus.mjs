// Shoot where focus lands after the empty-folder chip's rename editor closes:
// one frame after Enter, one after Escape, both with the focus ring on the
// folder-actions trigger. Run from website/:
//   node capture/shoot-empty-folder-chip-rename-focus.mjs [outDir]
import { createServer } from 'vite'
import { chromium } from 'playwright-core'
import path from 'node:path'
import { mkdirSync } from 'node:fs'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const outDir = process.argv[2] || '../temp-screenshots/empty-folder-chip-rename-focus'
mkdirSync(outDir, { recursive: true })

const server = await createServer({
  configFile: 'vite.config.ts',
  server: { port: 5199, strictPort: true, host: '127.0.0.1' },
})
await server.listen()
const browser = await chromium.launch({ executablePath: chromiumExecutable() })
const page = await browser.newPage({ viewport: { width: 540, height: 140 }, reducedMotion: 'reduce' })
await page.goto('http://127.0.0.1:5199/capture/empty-folder-chip-rename-focus.html?theme=dark')
const actions = page.getByRole('button', { name: 'Folder actions' })
await actions.waitFor({ timeout: 45000 })

const focusedLabel = () => page.evaluate(() => document.activeElement?.getAttribute('aria-label') ?? document.activeElement?.tagName)

// Open the menu from the keyboard, pick Rename, type, close with `key`.
async function renameAndClose(key, draft) {
  await actions.focus()
  await page.keyboard.press('Enter')
  await page.getByRole('menuitem', { name: 'Rename' }).click()
  const input = page.getByRole('textbox', { name: 'Rename' })
  await input.waitFor()
  // Radix's deferred close-focus restore runs after the menu is gone; the
  // editor must still be open and focused once it has had its turn.
  await page.getByRole('menu').waitFor({ state: 'detached' })
  await page.evaluate(() => new Promise(r => setTimeout(r, 50)))
  if (await focusedLabel() !== 'Rename') throw new Error(`rename editor lost focus after the menu closed: ${await focusedLabel()}`)
  await input.fill(draft)
  await page.keyboard.press(key)
  await page.waitForFunction(() => document.activeElement?.getAttribute('aria-label') === 'Folder actions')
  console.log(key, '-> focused:', await focusedLabel(), '| name:', await page.getByText(/jobs$/).first().evaluate(el => el.previousElementSibling?.textContent))
  await page.getByTestId('scene').screenshot({ path: path.join(outDir, `after-${key.toLowerCase()}.png`) })
}

await renameAndClose('Enter', 'Nightly reports')
await renameAndClose('Escape', 'Discarded name')

await browser.close()
await server.close()

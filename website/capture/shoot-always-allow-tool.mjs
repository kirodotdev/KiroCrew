import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const out = process.argv[2]
if (!out) throw new Error('Pass the screenshot output directory')
mkdirSync(out, { recursive: true })
const server = await createServer({ configFile: './vite.config.ts', server: { host: '127.0.0.1', port: 0 }, logLevel: 'warn' })
let browser
try {
  await server.listen()
  const base = server.resolvedUrls.local[0]
  console.log('Capture listener:', base)
  browser = await chromium.launch({ executablePath: chromiumExecutable(), env: { ...process.env, LD_LIBRARY_PATH: '' } })
  const page = await browser.newPage({ viewport: { width: 1000, height: 620 }, deviceScaleFactor: 2 })
  await page.route('**/*', async route => {
    const url = new URL(route.request().url())
    if (url.origin !== new URL(base).origin) return route.abort()
    if (url.pathname.startsWith('/api/')) return route.fulfill({ json: {} })
    return route.continue()
  })
  page.on('pageerror', error => console.error('PAGE ERROR:', error.message))
  for (const state of ['off', 'on', 'refused', 'card', 'card-refused', 'error']) {
    await page.goto(`${base}capture/always-allow-tool.html?state=${state}`)
    if (state.startsWith('card')) {
      await page.getByRole('button', { name: 'Trust', exact: true }).click()
      const item = page.getByRole('menuitem', { name: /Always allow this tool/ })
      await item.waitFor({ state: 'visible' })
      if ((await item.getAttribute('aria-disabled') === 'true') !== (state === 'card-refused')) throw new Error('Wrong menu item state')
    } else {
      const toggle = page.getByRole('switch', { name: 'Always allow', exact: true })
      await toggle.waitFor({ state: 'visible' })
      if ((await toggle.getAttribute('aria-checked') === 'true') !== (state === 'on')) throw new Error('Wrong toggle state')
      if (state === 'refused') await page.getByTestId('mcp-always-allow-refusal').waitFor({ state: 'visible' })
      if (state === 'error') {
        await toggle.click()
        await page.getByRole('alert').waitFor({ state: 'visible' })
      }
    }
    await page.evaluate(() => document.fonts.ready)
    const path = `${out}/${state}.png`
    await page.screenshot({ path, animations: 'disabled' })
    console.log(path)
  }
} finally {
  await browser?.close()
  await server.close()
}

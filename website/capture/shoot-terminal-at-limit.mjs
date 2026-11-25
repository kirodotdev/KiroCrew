// Screenshots for a terminal tab the gateway refused at its terminal ceiling:
// dark and light at a desktop width, and dark at a phone width.
//   node capture/shoot-terminal-at-limit.mjs [outDir] [port]
import { createServer } from 'vite'
import { chromium } from 'playwright-core'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

const OUT = process.argv[2] || '/tmp/terminal-at-limit-shots'
const PORT = Number(process.argv[3] || 5179)
const CHROMIUM_PATH = process.env.CHROMIUM_PATH
mkdirSync(OUT, { recursive: true })

const server = await createServer({
  configFile: 'vite.config.ts',
  server: { port: PORT, strictPort: true, host: '127.0.0.1' },
})
await server.listen()

const browser = await chromium.launch({ executablePath: CHROMIUM_PATH, args: ['--no-sandbox'] })

async function shoot(theme, width, name) {
  const ctx = await browser.newContext({ viewport: { width, height: 360 }, reducedMotion: 'reduce' })
  const page = await ctx.newPage()
  page.on('pageerror', (e) => console.error('pageerror', e.message))
  await page.goto(`http://127.0.0.1:${PORT}/capture/terminal-at-limit.html?theme=${theme}`)
  await page.waitForSelector('[data-testid="cli-panel-disconnected"]', { timeout: 30000 })
  await page.waitForTimeout(400)
  await page.screenshot({ path: join(OUT, name) })
  console.log('shot', name)
  await ctx.close()
}

await shoot('dark', 900, 'at-limit-dark.png')
await shoot('light', 900, 'at-limit-light.png')
await shoot('dark', 390, 'at-limit-dark-phone.png')

await browser.close()
await server.close()

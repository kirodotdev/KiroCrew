// Headless screenshots for the one-click Dashboard card send preview (#22367):
// the Profile switch off (the default), the switch on, and the error notice the
// host shows when a card send is refused or fails and the reply is filled into
// the chat box instead. Dark and light.
//   node capture/shoot-dashboard-card-send.mjs [outDir] [port]
import { createServer } from 'vite'
import { chromium } from 'playwright-core'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

const OUT = process.argv[2] || '/tmp/dashboard-card-send-shots'
const PORT = Number(process.argv[3] || 5191)
const CHROMIUM_PATH = process.env.CHROMIUM_PATH
mkdirSync(OUT, { recursive: true })

const server = await createServer({
  configFile: 'vite.config.ts',
  server: { port: PORT, strictPort: true, host: '127.0.0.1' },
})
await server.listen()
console.log(`vite up on ${PORT}`)

const browser = await chromium.launch({ executablePath: CHROMIUM_PATH, args: ['--no-sandbox'] })

async function shoot(theme, state, prefix) {
  const ctx = await browser.newContext({ viewport: { width: 560, height: 320 }, reducedMotion: 'reduce' })
  const page = await ctx.newPage()
  await page.goto(`http://127.0.0.1:${PORT}/capture/dashboard-card-send.html?theme=${theme}&state=${state}`)
  await page.locator('[data-testid="card-send-fixture"]').waitFor({ timeout: 15000 })
  if (state === 'error') await page.locator('[data-testid="crew-dashboard-send-error"]').waitFor({ timeout: 15000 })
  await page.waitForTimeout(200)
  await page.screenshot({ path: join(OUT, `${prefix}.png`) })
  console.log('shot', prefix)
  await ctx.close()
}

await shoot('dark', 'off', '1-dark-switch-off-default')
await shoot('dark', 'on', '2-dark-switch-on')
await shoot('dark', 'error', '3-dark-send-refused-notice')
await shoot('light', 'off', '4-light-switch-off-default')

await browser.close()
await server.close()
console.log('done')

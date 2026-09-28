// Shoot the loopback sign-in screens via the kas-login-copy harness.
// Run from website/: node capture/shoot-kas-loopback.mjs <outdir>
import { createServer } from 'vite'
import { chromium } from 'playwright-core'
import path from 'node:path'

const outDir = process.argv[2] || '/tmp/kas-loopback-shots'
const executablePath = process.env.CHROMIUM_PATH

const server = await createServer({
  configFile: 'vite.config.ts',
  server: { port: 5199, strictPort: true, host: '127.0.0.1' },
})
await server.listen()
const browser = await chromium.launch({ executablePath })

const shots = [
  { name: 'loopback-waiting-en', scene: 'loopback', lang: 'en', slow: false },
  { name: 'loopback-slow-hint-en', scene: 'loopback', lang: 'en', slow: true },
  { name: 'loopback-fallback-en', scene: 'fallback', lang: 'en', slow: false },
  { name: 'loopback-waiting-zh-CN', scene: 'loopback', lang: 'zh-CN', slow: false },
  { name: 'loopback-fallback-zh-CN', scene: 'fallback', lang: 'zh-CN', slow: false },
]
for (const shot of shots) {
  const ctx = await browser.newContext({
    viewport: { width: 1440, height: 960 },
    reducedMotion: 'reduce',
  })
  const page = await ctx.newPage()
  if (shot.slow) {
    // Fake time so the 30s slow-hint promotion fires without waiting 30s.
    await page.clock.install()
  }
  await page.goto(
    `http://127.0.0.1:5199/capture/kas-login-copy.html?scene=${shot.scene}&theme=dark&lang=${shot.lang}`,
  )
  const marker = shot.scene === 'fallback' ? '[data-testid="kas-login-fell-back"]' : 'h1'
  await page.waitForSelector(marker, { timeout: 45000 })
  if (shot.slow) {
    await page.clock.runFor(31_000)
    await page.waitForSelector('[data-testid="kas-login-loopback-slow"]', { timeout: 10_000 })
  }
  await page.waitForTimeout(300)
  await page.screenshot({ path: path.join(outDir, `${shot.name}.png`) })
  console.log('shot', shot.name)
  await ctx.close()
}
await browser.close()
await server.close()

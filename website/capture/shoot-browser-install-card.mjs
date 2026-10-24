// Shoot the in-chat browser install card in each state via its capture harness.
// Run from website/: node capture/shoot-browser-install-card.mjs <outdir>
import { createServer } from 'vite'
import { chromium } from 'playwright-core'
import path from 'node:path'

const outDir = process.argv[2] || 'browser-install-card-shots'
const executablePath = process.env.CHROMIUM_PATH

const server = await createServer({
  configFile: 'vite.config.ts',
  server: { port: 5198, strictPort: true, host: '127.0.0.1' },
})
await server.listen()
const browser = await chromium.launch({ executablePath })
try {
  for (const theme of ['dark', 'light']) {
    // scene -> the card state it settles in. `refused` is a needed card whose
    // Install click the gateway rejects; `installed-continued` is shot after the
    // Continue click, `installed` before it.
    const scenes = {
      needed: 'needed',
      running: 'running',
      installed: 'installed',
      'installed-continued': 'installed',
      failed: 'failed',
      refused: 'needed',
      unavailable: 'unavailable',
    }
    for (const [scene, state] of Object.entries(scenes)) {
      const ctx = await browser.newContext({ viewport: { width: 860, height: 420 }, reducedMotion: 'reduce' })
      const page = await ctx.newPage()
      await page.goto(`http://127.0.0.1:5198/capture/browser-install-card.html?scene=${scene}&theme=${theme}`)
      await page.waitForSelector(`[data-testid="browser-install-card"][data-state="${state}"]`, { timeout: 60000 })
      if (scene === 'installed-continued') {
        await page.click('[data-testid="browser-install-card-continue"]')
        await page.waitForSelector('[data-testid="browser-install-card-continue"]', { state: 'detached' })
      }
      if (scene === 'refused') {
        await page.click('[data-testid="browser-install-card-install"]')
        await page.waitForSelector('[data-testid="browser-install-card-error"]')
      }
      const file = path.join(outDir, `browser-install-card-${scene}-${theme}.png`)
      await page.locator('[data-testid="capture-reply"]').screenshot({ path: file })
      console.log(file)
      await ctx.close()
    }
  }
} finally {
  await browser.close()
  await server.close()
}

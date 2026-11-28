// Shoot the Schedule page's "waiting to start" state via the cron-waiting-run harness.
// Run from website/: CHROMIUM_PATH=<chrome> node capture/shoot-cron-waiting-run.mjs <outdir> [suffix]
import { createServer } from 'vite'
import { chromium } from 'playwright-core'
import path from 'node:path'
import { mkdirSync } from 'node:fs'

const outDir = process.argv[2] || path.join(process.env.TMPDIR || '/tmp', 'cron-waiting-shots')
// Appended to each file name, so a second run against another build keeps the first run's shots.
const suffix = process.argv[3] ? `-${process.argv[3]}` : ''
mkdirSync(outDir, { recursive: true })
const executablePath = process.env.CHROMIUM_PATH

const server = await createServer({
  configFile: 'vite.config.ts',
  server: { port: 5199, strictPort: true, host: '127.0.0.1' },
})
await server.listen()
const browser = await chromium.launch({ executablePath })

const shots = [
  { scene: 'rows', ready: 'text=nightly-export', viewport: { width: 1280, height: 520 } },
  { scene: 'panel', ready: 'text=No execution history yet', viewport: { width: 700, height: 420 } },
]
try {
  for (const shot of shots) {
    for (const theme of ['light', 'dark']) {
      // The zone is pinned so the start time in the shot does not depend on the host.
      const ctx = await browser.newContext({ viewport: shot.viewport, reducedMotion: 'reduce', timezoneId: 'UTC', locale: 'en-US' })
      const page = await ctx.newPage()
      await page.goto(`http://127.0.0.1:5199/capture/cron-waiting-run.html?scene=${shot.scene}&theme=${theme}`)
      await page.waitForSelector(shot.ready, { timeout: 60000 })
      await page.waitForLoadState('networkidle')
      const name = `${shot.scene}-${theme}${suffix}.png`
      await page.locator(`[data-testid="scene-${shot.scene}"]`).screenshot({ path: path.join(outDir, name) })
      console.log('shot', name)
      await ctx.close()
    }
  }
} finally {
  await browser.close()
  await server.close()
}

// Shoot the speech-model wait on the recording strip via the
// voice-status-preparing harness.
// Run from website/: node capture/shoot-voice-status-preparing.mjs <outdir>
import { createServer } from 'vite'
import { chromium } from 'playwright-core'
import path from 'node:path'

const outDir = process.argv[2] || 'voice-status-shots'
const executablePath = process.env.CHROMIUM_PATH

const server = await createServer({
  configFile: 'vite.config.ts',
  server: {
    port: 5203,
    strictPort: true,
    host: '127.0.0.1',
    // Nothing edits these files while the shots are taken, so the watcher is
    // pure cost. `CAPTURE_POLL=1` trades it for polling on a host whose inotify
    // instances are already spent by other work: the dev server otherwise fails
    // to start at all with EMFILE, which reads like a broken harness.
    watch: process.env.CAPTURE_POLL ? { usePolling: true, interval: 1000 } : undefined,
  },
})
await server.listen()
const browser = await chromium.launch({ executablePath })

const shots = [
  { name: 'voice-recording-preparing-en', scene: 'recording-preparing', lang: 'en' },
  { name: 'voice-released-preparing-en', scene: 'released-preparing', lang: 'en' },
  { name: 'voice-released-downloading-en', scene: 'released-downloading', lang: 'en' },
  { name: 'voice-released-expired-en', scene: 'released-expired', lang: 'en' },
  { name: 'voice-released-preparing-zh-CN', scene: 'released-preparing', lang: 'zh-CN' },
]
for (const shot of shots) {
  const ctx = await browser.newContext({
    viewport: { width: 900, height: 320 },
    deviceScaleFactor: 2,
    // The spinner would otherwise land at an arbitrary rotation, which makes two
    // shots of the same scene differ for no reason a reviewer cares about.
    reducedMotion: 'reduce',
  })
  const page = await ctx.newPage()
  await page.goto(
    `http://127.0.0.1:5203/capture/voice-status-preparing.html?scene=${shot.scene}&theme=dark&lang=${shot.lang}`,
  )
  const marker = shot.scene === 'recording-preparing'
    ? 'text=Recording'
    : shot.scene === 'released-expired'
      ? '[role="alert"]'
      : '[data-testid="voice-status-download"]'
  await page.waitForSelector(marker, { timeout: 45000 })
  await page.waitForTimeout(300)
  await page.screenshot({ path: path.join(outDir, `${shot.name}.png`) })
  console.log('shot', shot.name)
  await ctx.close()
}
await browser.close()
await server.close()

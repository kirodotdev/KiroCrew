// Shoot the speech-model wait and the two audio start-up errors on the
// recording strip via the voice-status-preparing harness.
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

// `width` is the composer's width in the harness; a phone shot narrows the
// window with it, so the strip wraps the way it does on a 375 px screen.
const PHONE = { width: 343, viewport: 375 }
const shots = [
  { name: 'voice-recording-preparing-en', scene: 'recording-preparing', lang: 'en' },
  { name: 'voice-released-preparing-en', scene: 'released-preparing', lang: 'en' },
  { name: 'voice-released-downloading-en', scene: 'released-downloading', lang: 'en' },
  { name: 'voice-released-expired-en', scene: 'released-expired', lang: 'en' },
  { name: 'voice-released-preparing-zh-CN', scene: 'released-preparing', lang: 'zh-CN' },
  { name: 'voice-audio-module-failed-en', scene: 'audio-module-failed', lang: 'en' },
  { name: 'voice-audio-module-failed-en-phone', scene: 'audio-module-failed', lang: 'en', ...PHONE },
  { name: 'voice-audio-start-failed-en', scene: 'audio-start-failed', lang: 'en' },
  { name: 'voice-audio-start-failed-en-phone', scene: 'audio-start-failed', lang: 'en', ...PHONE },
  { name: 'voice-audio-module-failed-de-phone', scene: 'audio-module-failed', lang: 'de', ...PHONE },
  { name: 'voice-audio-start-failed-de-phone', scene: 'audio-start-failed', lang: 'de', ...PHONE },
]
for (const shot of shots) {
  const ctx = await browser.newContext({
    viewport: { width: shot.viewport || 900, height: 320 },
    deviceScaleFactor: 2,
    // The spinner would otherwise land at an arbitrary rotation, which makes two
    // shots of the same scene differ for no reason a reviewer cares about.
    reducedMotion: 'reduce',
  })
  const page = await ctx.newPage()
  await page.goto(
    `http://127.0.0.1:5203/capture/voice-status-preparing.html?scene=${shot.scene}&theme=dark&lang=${shot.lang}${shot.width ? `&width=${shot.width}` : ''}`,
  )
  const marker = shot.scene === 'recording-preparing'
    ? 'text=Recording'
    : shot.scene === 'released-expired' || shot.scene.startsWith('audio-')
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

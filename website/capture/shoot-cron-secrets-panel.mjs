// Shoot the Schedule page's vault-secrets panel via the cron-secrets-panel harness.
// Run from website/: node capture/shoot-cron-secrets-panel.mjs <outdir>
import { createServer } from 'vite'
import { chromium } from 'playwright-core'
import path from 'node:path'
import { mkdirSync } from 'node:fs'

const outDir = process.argv[2] || path.join(process.env.TMPDIR || '/tmp', 'cron-secrets-shots')
mkdirSync(outDir, { recursive: true })
const executablePath = process.env.CHROMIUM_PATH

const server = await createServer({
  configFile: 'vite.config.ts',
  server: { port: 5199, strictPort: true, host: '127.0.0.1' },
})
await server.listen()
const browser = await chromium.launch({ executablePath })

const shots = [
  // The pending banner: the approve button unlocks only after the reviewed
  // source has rendered, so wait for the code block before shooting.
  { name: 'secrets-panel-pending', scene: 'pending', ready: '[data-testid="scene-pending"] pre, [data-testid="scene-pending"] code' },
  // The active list with Revoke armed (confirm label showing).
  { name: 'secrets-panel-active-revoke-armed', scene: 'active', ready: 'text=Revoke all?' },
  // The empty state naming the mint path.
  { name: 'secrets-panel-empty', scene: 'empty', ready: 'text=Ask the agent to request secrets for this job.' },
]
for (const shot of shots) {
  const ctx = await browser.newContext({ viewport: { width: 900, height: 1000 }, reducedMotion: 'reduce' })
  const page = await ctx.newPage()
  await page.goto(`http://127.0.0.1:5199/capture/cron-secrets-panel.html?scene=${shot.scene}&theme=dark`)
  await page.waitForSelector(shot.ready, { timeout: 45000 })
  await page.waitForTimeout(400)
  const frame = page.locator(`[data-testid="scene-${shot.scene}"]`)
  await frame.screenshot({ path: path.join(outDir, `${shot.name}.png`) })
  console.log('shot', shot.name)
  await ctx.close()
}
await browser.close()
await server.close()

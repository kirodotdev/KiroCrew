// Screenshots for the Read aloud failure notice: the generic
// "failed" notice (before), and the two new cause-naming notices after —
// the system no-engine case and the Amazon Polly rejection — each in light
// and dark. Assert the expected sentence is on screen before every shot so a
// blank or wrong-copy frame cannot pass as evidence.
//   node capture/shoot-read-aloud-failure-notice.mjs [outDir] [port]
import { createServer } from 'vite'
import { chromium } from 'playwright-core'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

const OUT = process.argv[2] || '/tmp/read-aloud-failure-shots'
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

// code -> a phrase that must be on screen for the shot to be honest.
const SCENES = [
  ['failed', 'Read aloud failed', 'before-failed'],
  ['voice_polly_rejected', 'Amazon Polly does not offer the selected voice or engine', 'after-polly-rejected'],
]

async function shoot(theme, code, phrase, prefix) {
  const ctx = await browser.newContext({ viewport: { width: 760, height: 260 }, reducedMotion: 'reduce' })
  const page = await ctx.newPage()
  await page.goto(`http://127.0.0.1:${PORT}/capture/read-aloud-failure-notice.html?theme=${theme}&code=${code}`)
  await page.waitForSelector('[data-testid="voice-playback-error"]', { timeout: 15000 })
  await page.getByText(phrase, { exact: false }).first().waitFor({ timeout: 15000 })
  await page.waitForTimeout(200)
  await page.screenshot({ path: join(OUT, `${theme}-${prefix}.png`) })
  console.log('shot', theme, prefix)
  await ctx.close()
}

for (const theme of ['light', 'dark']) {
  for (const [code, phrase, prefix] of SCENES) {
    await shoot(theme, code, phrase, prefix)
  }
}

await browser.close()
await server.close()
console.log('done')

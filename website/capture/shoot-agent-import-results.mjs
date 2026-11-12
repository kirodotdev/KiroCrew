/**
 * Screenshot harness for the agent import review and results steps (#29075).
 *
 * Serves the isolated capture entry (capture/agent-import-results.tsx) with a
 * programmatic Vite dev server on loopback and clicks the REAL flow through:
 *   review  - the skipped list, one reason of each kind, a failed read among them;
 *   running - the results step while Keep both runs;
 *   resolve - the results step after Keep both fails.
 * Both themes. Every response is fabricated inside the entry, so no gateway,
 * kiro-cli or token is involved.
 *
 * Usage (from website/): node capture/shoot-agent-import-results.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const OUT = process.argv[2] || resolve(dirname(fileURLToPath(import.meta.url)), '../../temp-screenshots/29075')
const PREFIX = process.argv[3] || 'after'
mkdirSync(OUT, { recursive: true })

const server = await createServer({
  configFile: './vite.config.ts',
  server: { host: '127.0.0.1', port: 5199, strictPort: false },
  logLevel: 'warn',
})
await server.listen()
const base = server.resolvedUrls?.local?.[0]?.replace(/\/$/, '') || `http://127.0.0.1:${server.config.server.port}`

const browser = await chromium.launch({
  executablePath: chromiumExecutable(),
  env: { ...process.env, LD_LIBRARY_PATH: '' },
})
const page = await browser.newPage({ viewport: { width: 1000, height: 820 }, deviceScaleFactor: 2 })
page.on('console', m => { if (m.type() === 'error') console.log('PAGE ERROR:', m.text()) })
page.on('pageerror', e => console.log('PAGE EXCEPTION:', e.message))

const dialog = () => page.getByRole('dialog', { name: 'Import agent setup' })
const click = name => dialog().getByRole('button', { name, exact: true }).click()

for (const theme of ['dark', 'light']) {
  await page.goto(`${base}/capture/agent-import-results.html?theme=${theme}`)
  await dialog().getByRole('heading', { name: 'Choose sources' }).waitFor({ timeout: 20000 })
  await click('Continue')
  await dialog().getByRole('heading', { name: 'Select items to import' }).waitFor()
  await click('Continue')
  await dialog().getByRole('heading', { name: 'Review import' }).waitFor()
  await page.getByText('Claude Code: Settings').scrollIntoViewIfNeeded()
  await page.waitForTimeout(500)
  const review = `${OUT}/${PREFIX}-review-${theme}.png`
  await page.screenshot({ path: review })
  console.log('wrote', review)

  await click('Import selected')
  await dialog().getByRole('button', { name: 'Keep both' }).waitFor()
  await click('Keep both')
  await page.waitForTimeout(400)
  const running = `${OUT}/${PREFIX}-retry-running-${theme}.png`
  await page.screenshot({ path: running })
  console.log('wrote', running)
  await page.waitForTimeout(3200)
  const results = `${OUT}/${PREFIX}-resolve-failed-${theme}.png`
  await page.screenshot({ path: results })
  console.log('wrote', results)
}

await browser.close()
await server.close()

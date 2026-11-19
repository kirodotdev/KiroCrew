/**
 * Screenshots of the registry banner for a private GitHub registry that the
 * GitHub CLI on the gateway's computer could not read.
 *
 * Drives the ISOLATED capture entry (website/capture/registry-private-github.html),
 * which mounts RegistryManager with the backend stubbed to answer a refresh with a
 * `gh_*` cause. The run clicks the row's refresh control and fails unless the
 * banner names the repository and the GitHub CLI, so a frame that does not show
 * the sentence is never written.
 *
 * The script starts its own Vite dev server on 127.0.0.1 and stops it when done.
 *
 * Usage: node scripts/capture-registry-private-github.mjs [outDir]
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'

const OUT = resolve(process.argv[2] || '../temp-screenshots/registry-private-github')
mkdirSync(OUT, { recursive: true })
const server = await createServer({
  configFile: './vite.config.ts',
  server: { host: '127.0.0.1', port: 5199, strictPort: false },
  logLevel: 'warn',
})
await server.listen()
const BASE = server.resolvedUrls?.local?.[0]?.replace(/\/$/, '') || `http://127.0.0.1:${server.config.server.port}`

const CAUSES = ['gh_signed_out', 'gh_no_access', 'gh_missing', 'gh_strict_mode']

const run = async () => {
  const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
  const browser = await chromium.launch({ env: browserEnv })
  let failed = 0
  for (const theme of ['dark', 'light']) {
    for (const cause of CAUSES) {
      const ctx = await browser.newContext({ viewport: { width: 780, height: 420 }, deviceScaleFactor: 2, colorScheme: theme })
      const page = await ctx.newPage()
      page.on('pageerror', e => console.error(`  page error ${theme}/${cause}: ${e.message}`))
      await page.goto(`${BASE}/capture/registry-private-github.html?theme=${theme}&cause=${cause}`, { waitUntil: 'networkidle' })
      try {
        await page.waitForSelector('[data-capture-root]', { timeout: 15000 })
        await page.click('[aria-label="Refresh Team Apps registry"]', { timeout: 10000, force: true })
        await page.waitForSelector('text=acme/team-apps', { timeout: 10000 })
        await page.waitForSelector('text=GitHub CLI', { timeout: 10000 })
      } catch (e) {
        console.error(`  FAIL ${theme}/${cause}: ${e instanceof Error ? e.message.split('\n')[0] : e}`)
        failed += 1
        await ctx.close()
        continue
      }
      const file = `${OUT}/${cause}-${theme}.png`
      await page.locator('[data-capture-root]').screenshot({ path: file })
      console.log(`  ok ${file}`)
      await ctx.close()
    }
  }
  await browser.close()
  await server.close()
  if (failed) process.exit(1)
}

await run()

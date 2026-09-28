/**
 * Screenshot harness for the crewmate Notes side-panel readability pass.
 *
 * Serves the isolated capture entry (capture/notes-readability.tsx) with a
 * programmatic Vite dev server on loopback, then photographs the REAL
 * CrewNotesTab at the ~400px panel width in both themes. The briefing read is
 * stubbed inside the entry, so no gateway / kiro-cli / token is involved.
 *
 * Usage (from website/): node capture/shoot-notes-readability.mjs [outDir]
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

// Default to the repo's gitignored temp-screenshots dir (portable across
// machines); override with an explicit outDir argument.
const OUT = process.argv[2] || resolve(dirname(fileURLToPath(import.meta.url)), '../../temp-screenshots/notes-readability')
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
const page = await browser.newPage({ viewport: { width: 640, height: 1100 }, deviceScaleFactor: 2 })
page.on('console', m => { if (m.type() === 'error') console.log('PAGE ERROR:', m.text()) })
page.on('pageerror', e => console.log('PAGE EXCEPTION:', e.message))

for (const theme of ['dark', 'light']) {
  await page.goto(`${base}/capture/notes-readability.html?theme=${theme}`)
  await page.locator('[data-testid="member-notes-body"]').waitFor({ timeout: 20000 })
  await page.waitForTimeout(500)
  const panel = page.locator('[data-testid="member-notes"]')
  await panel.screenshot({ path: `${OUT}/${process.env.SHOT_PREFIX || 'shot'}-${theme}.png` })
  console.log('wrote', `${OUT}/${process.env.SHOT_PREFIX || 'shot'}-${theme}.png`)
}

await browser.close()
await server.close()

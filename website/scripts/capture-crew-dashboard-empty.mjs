/**
 * Real-browser evidence for the Dashboard tab's EMPTY state.
 *
 * Serves the isolated capture entry (capture/crew-dashboard-empty.html) with a
 * programmatic Vite dev server on loopback and photographs the REAL
 * `CrewDashboardFrame` in its `!html` branch. Every frame asserts the state it
 * documents before writing, so a frame cannot show the wrong one:
 *   01-pinned-dark     a crewmate wearing its builder-pinned face, three prompts
 *   02-pinned-light    the same in the light palette
 *   03-seeded-dark     a crewmate on its name-derived ghost
 *   04-no-chat-dark    no chat box to land a prompt in: prompts withheld, no button
 *
 * Usage (from website/): node scripts/capture-crew-dashboard-empty.mjs [outDir]
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { chromiumExecutable } from './lib/chromium-executable.mjs'

const OUT = process.argv[2] || resolve(dirname(fileURLToPath(import.meta.url)), '../../temp-screenshots/crew-dashboard-empty')
mkdirSync(OUT, { recursive: true })

const server = await createServer({
  configFile: './vite.config.ts',
  server: { host: '127.0.0.1', port: 5197, strictPort: false },
  logLevel: 'warn',
})
await server.listen()
const base = server.resolvedUrls?.local?.[0]?.replace(/\/$/, '') || `http://127.0.0.1:${server.config.server.port}`

// mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, older than
// the system Mesa needs; children inherit it, so scrub it here.
const { LD_LIBRARY_PATH: _mise, ...env } = process.env
const browser = await chromium.launch({ executablePath: chromiumExecutable(), env })
const page = await browser.newPage({ viewport: { width: 760, height: 720 }, deviceScaleFactor: 2 })
let failures = 0
page.on('pageerror', e => { console.error('pageerror:', e.message); failures++ })
const check = (label, ok) => {
  console.log(`${label} => ${ok ? 'OK' : 'FAIL'}`)
  if (!ok) failures++
}

const FRAMES = [
  ['01-pinned-dark', 'theme=dark&avatar=pinned', { prompts: 3 }],
  ['02-pinned-light', 'theme=light&avatar=pinned', { prompts: 3 }],
  ['03-seeded-dark', 'theme=dark&avatar=seeded', { prompts: 3 }],
  ['04-no-chat-dark', 'theme=dark&avatar=pinned&prompts=0', { prompts: 0 }],
]

for (const [name, query, want] of FRAMES) {
  await page.goto(`${base}/capture/crew-dashboard-empty.html?${query}`)
  const empty = page.locator('[data-testid="crew-webview-empty"]')
  await empty.waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(300)
  const text = (await empty.textContent()) || ''
  check(`${name} bubble speaks first-person`, text.includes("I haven't published a dashboard yet."))
  check(`${name} face rendered`, (await empty.locator('img').count()) === 1)
  const prompts = await page.locator('[data-testid="crew-webview-empty-prompt"]').count()
  check(`${name} prompts=${want.prompts}`, prompts === want.prompts)
  if (want.prompts === 0) check(`${name} no buttons at all`, (await empty.locator('button').count()) === 0)
  // No set-up control anywhere: nothing in the crew editor makes a crewmate publish.
  check(`${name} no set-up control`, (await page.locator('[data-testid="crew-webview-setup"]').count()) === 0)
  await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${name}.png` })
  console.log('wrote', `${OUT}/${name}.png`)
}

await browser.close()
await server.close()
if (failures) {
  console.error(`${failures} assertion(s) failed`)
  process.exit(1)
}

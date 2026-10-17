/**
 * Screenshot harness for the configurable terminal cursor style (issue #6469).
 * House pattern of website/capture/shoot-agent-display-name.mjs: the REAL built
 * SPA (website/dist) behind the in-process static server, every /api/** answered
 * from fixtures via Playwright route interception — gateway-free, no kiro-cli,
 * no token. The client code under test is unmodified.
 *
 * The new control is a SettingsButtonGroup (Block / Bar / Underline) in
 * Display → Terminal, backed by the per-client useTerminalFont localStorage
 * store, so no server config drives it — the default stub's /api/config/kirocrew
 * is enough to render the pane.
 *
 * Frames:
 *   10-terminal-cursor-style-default   the Terminal pane with the Cursor style
 *                                      group showing the default (Block) active.
 *   11-terminal-cursor-style-bar       the same group after selecting Bar.
 *   12-terminal-cursor-style-underline the same group after selecting Underline.
 *
 * Usage (from website/): node capture/shoot-terminal-cursor-style.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from '../scripts/lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from '../scripts/lib/stub-dashboard-api.mjs'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const OUT = process.argv[2] || '../temp-screenshots/6469'
mkdirSync(OUT, { recursive: true })

const { srv, base } = await serveDist()
// mise's node wrapper re-exports LD_LIBRARY_PATH pointing at its own (older)
// libstdc++, which breaks the system graphics libs chrome loads. Strip it from
// the chrome child; harmless when it was never set.
const browser = await chromium.launch({
  executablePath: chromiumExecutable(),
  env: { ...process.env, LD_LIBRARY_PATH: '' },
})
const page = await browser.newPage({ viewport: { width: 1200, height: 900 } })
logPageProblems(page)
await stubDashboardApi(page)

// Deep-link straight to the Terminal pane: SettingsSubNav path mode is
// `${basePath}/<tab>/<sub>` → /settings/display/terminal.
await page.goto(base + '/settings/display/terminal')

// The cursor-style group's own label is the readiness anchor.
const label = page.getByText('Cursor style', { exact: true }).first()
await label.waitFor({ timeout: 30000 })
await page.waitForTimeout(500)

// Frame 10 — the default (Block active).
await page.screenshot({ path: `${OUT}/10-terminal-cursor-style-default.png` })
console.log('shot 10-terminal-cursor-style-default')

// Frame 11 — select Bar.
await page.getByRole('button', { name: 'Bar', exact: true }).first().click()
await page.waitForTimeout(300)
await page.screenshot({ path: `${OUT}/11-terminal-cursor-style-bar.png` })
console.log('shot 11-terminal-cursor-style-bar')

// Frame 12 — select Underline.
await page.getByRole('button', { name: 'Underline', exact: true }).first().click()
await page.waitForTimeout(300)
await page.screenshot({ path: `${OUT}/12-terminal-cursor-style-underline.png` })
console.log('shot 12-terminal-cursor-style-underline')

await browser.close()
srv.close()
console.log('frames written to', OUT)

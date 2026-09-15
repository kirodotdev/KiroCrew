/**
 * Screenshot harness for Settings → Display → Terminal → Command completion.
 *
 * The toggle behind `dashboard.terminal.completion.enabled`: default on, off
 * only for a literal `false` (the backend's rule). Two states carry the design:
 *
 *  - `on`:  the key is absent from the config, so the switch reads ON.
 *  - `off`: the config holds `completion.enabled: false`.
 *
 * Runs the REAL built SPA (website/dist) with every /api/** call answered from
 * fixtures — gateway-free. Labels are read from the CATALOG, so a key rename
 * breaks the capture loudly instead of silently screenshotting the wrong row.
 *
 * Usage: node scripts/capture-terminal-completion-toggle.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json, KIROCREW_CONFIG_FIXTURE } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/terminal-completion-toggle'
mkdirSync(OUT, { recursive: true })

const LOCALES = fileURLToPath(new URL('../src/i18n/locales/', import.meta.url))
const manual = JSON.parse(readFileSync(LOCALES + 'en.manual.json', 'utf-8'))
const dp = manual.pages.settings.displayPanel
const LABEL = dp.terminal_completion // "Command completion"
const SHELL = dp.terminal_shell // "Default shell"
if (!LABEL || !SHELL) throw new Error('displayPanel terminal keys missing — renamed?')

// Mutable per-state fixture for GET /api/config/kirocrew.
let completion = undefined

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  logPageProblems(page)

  await stubDashboardApi(page, {
    extra: async (path, route) => {
      if (path === '/api/config/kirocrew') {
        const terminal = completion === undefined ? { shell: '' } : { shell: '', completion }
        await json(route, { ...KIROCREW_CONFIG_FIXTURE, dashboard: { terminal } })
        return true
      }
      return false
    },
  })

  for (const [state, fixture] of [['on', undefined], ['off', { enabled: false }]]) {
    completion = fixture
    await page.goto(base + '/settings/display', { waitUntil: 'domcontentloaded' })
    const sw = page.getByRole('switch', { name: LABEL })
    await sw.waitFor({ timeout: 15000 })
    await page.getByLabel(SHELL).waitFor({ timeout: 5000 })
    const expected = state === 'on' ? 'true' : 'false'
    await page.waitForFunction(
      ([label, want]) => document.querySelector(`[role="switch"][aria-label="${label}"]`)?.getAttribute('aria-checked') === want,
      [LABEL, expected],
      { timeout: 5000 },
    )
    // Frame the Terminal card: scroll the switch into view and crop around it.
    await sw.scrollIntoViewIfNeeded()
    await page.waitForTimeout(300)
    const card = sw.locator('xpath=ancestor::*[contains(@class,"rounded")][1]')
    const box = await card.boundingBox()
    const clip = box
      ? { x: Math.max(0, box.x - 8), y: Math.max(0, box.y - 8), width: box.width + 16, height: box.height + 16 }
      : undefined
    await page.screenshot({ path: `${OUT}/${state}.png`, clip })
    console.log(`wrote ${OUT}/${state}.png`)
  }

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })

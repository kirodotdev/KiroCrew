/**
 * Screenshot harness for the segmented-control (`SettingsButtonGroup`) restyle.
 *
 * Captured in a LIGHT theme first, because that is where the bug lived: in every
 * light theme `--bg-elevated` and `--card` are the same #ffffff, so the old
 * `bg-elevated` track was invisible against the card and only the selected pill
 * rendered. A dark shot goes alongside it as the regression check — the track
 * was faintly visible there, so the new recessed/raised treatment has to not
 * over-correct.
 *
 * Settings → Display holds three of the nine call sites (Interface, Font, Mode),
 * which is why one panel shot covers most of the blast radius; Settings → Chat
 * is captured too because its groups carry longer labels.
 *
 * Runs the REAL built SPA (website/dist) with every /api/** call answered from
 * the shared boot fixtures — no gateway, no token. Same technique as
 * capture-i18n-labels.mjs.
 *
 * Usage: node scripts/capture-segmented-control.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

import { serveDist } from './lib/serve-dist.mjs'
import { makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/segmented-control'
const PROJECT = '/home/user/.kiro/crew/workspace'
const FIXED_API = makeFixedApi(PROJECT)

/** `theme` is the data-theme attribute; `mode` is what /api/theme/boot returns. */
const SCENES = [
  { name: 'light', theme: 'kiro-light', mode: 'light' },
  { name: 'dark', theme: 'kiro-dark', mode: 'dark' },
]

mkdirSync(OUT, { recursive: true })

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const scene = { mode: 'light' }

  try {
    const context = await browser.newContext({
      viewport: { width: 1280, height: 1000 },
      // 12–13px type: a 1x shot cannot show a 1px border or a subtle shadow,
      // which is the entire subject of this change.
      deviceScaleFactor: 2,
    })
    const page = await context.newPage()
    await page.routeWebSocket(/\/api\/ws/, () => {})
    await page.route('**/api/**', route =>
      handleBootRoute(route, new URL(route.request().url()).pathname, {
        project: PROJECT, theme: scene.mode, fixedApi: FIXED_API,
      }))
    page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))

    for (const s of SCENES) {
      scene.mode = s.mode
      await page.addInitScript(({ theme, mode }) => {
        localStorage.clear()
        localStorage.setItem('mc-onboarded', '1')
        localStorage.setItem('mc-privacy-notice-v1', '1')
        localStorage.setItem('mc-theme', mode)
        localStorage.setItem('mc-color-theme', theme)
      }, s)

      for (const panel of ['Display', 'Chat']) {
        await page.goto(`${base}/settings`, { waitUntil: 'domcontentloaded' })
        // The theme is applied by a boot effect, so assert on the attribute
        // rather than sleeping: a shot taken pre-swap is the wrong palette.
        await page.waitForFunction(
          t => document.documentElement.getAttribute('data-theme') === t,
          s.theme, { timeout: 8000 })
        // Reached by clicking the settings nav, not by a deep link: the panel
        // routes are rendered inside SettingsPage and a cold /settings/<panel>
        // load error-boundaries out.
        await page.getByRole('button', { name: panel, exact: true }).click()
        const groups = page.getByRole('group')
        await groups.first().waitFor({ timeout: 8000 })

        const path = `${OUT}/${panel.toLowerCase()}-${s.name}.png`
        await page.screenshot({ path, fullPage: false })
        console.log(`wrote ${path} (${await groups.count()} segmented controls)`)
      }
    }
  } finally {
    await browser.close()
    srv.close()
  }
}

await main()

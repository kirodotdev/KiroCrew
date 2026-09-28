/**
 * Screenshot harness for the Feature Previews move: Developer page tab →
 * Settings > Developer section.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server and answers every /api/** call from fixtures via Playwright route
 * interception — gateway-free, no kiro-cli, no dashboard auth.
 *
 * Six frames, each pinning one half of the move:
 *   settings-developer-{light,dark}.png  the section on its new pane, both themes
 *   developer-rail.png                   the Developer page rail without the tab
 *   legacy-redirect.png                  /developer?tab=feature-previews landing
 *                                        on /settings/developer with the whole
 *                                        section ringed
 *   webhooks-on.png                      the Webhooks card ON, revealing the
 *                                        "Open Webhooks" ingress (only door to
 *                                        that page)
 *   search-webhooks.png                  Settings search reaching the moved
 *                                        toggle through the registry
 *
 * Usage: node scripts/capture-settings-feature-previews.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../.github/screenshots/settings-feature-previews'
mkdirSync(OUT, { recursive: true })

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const shot = []

  const openPage = async (theme) => {
    const context = await browser.newContext({
      viewport: { width: 1400, height: 900 },
      deviceScaleFactor: 2, // 12-13px type renders soft at 1x on GitHub
    })
    const page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, { theme })
    // AFTER the shared stub, whose own init script clears storage (and seeds
    // `mc-theme` from the option above). Developer Mode is on so the Developer
    // rail row exists for the rail frame; the previews section itself does NOT
    // depend on it, which the settings frames would show either way.
    await page.addInitScript(() => {
      localStorage.setItem('mc-dev-mode', '1')
    })
    return page
  }
  const save = async (page, name) => {
    await page.screenshot({ path: `${OUT}/${name}.png` })
    shot.push(`${name}.png`)
  }
  const settled = async (page) => {
    await page.getByRole('switch', { name: /webhooks/i }).waitFor({ state: 'visible', timeout: 15000 })
    await page.waitForTimeout(600) // let the cards' rise animation finish
  }

  for (const theme of ['light', 'dark']) {
    const page = await openPage(theme)
    await page.goto(base + '/settings/developer', { waitUntil: 'domcontentloaded' })
    await settled(page)
    await save(page, `settings-developer-${theme}`)
    await page.context().close()
  }

  {
    const page = await openPage('light')
    await page.goto(base + '/developer?tab=config', { waitUntil: 'domcontentloaded' })
    await page.getByRole('button', { name: /debug tools/i }).waitFor({ state: 'visible', timeout: 15000 })
    if (await page.getByRole('button', { name: /feature previews/i }).count() > 0) {
      throw new Error('Developer rail still offers a Feature Previews tab')
    }
    await page.waitForTimeout(400)
    await save(page, 'developer-rail')
    await page.context().close()
  }

  {
    const page = await openPage('light')
    await page.goto(base + '/developer?tab=feature-previews', { waitUntil: 'domcontentloaded' })
    // The highlight param is stripped ~100ms after the ring is applied, so
    // wait for the ring itself, not the URL that requested it.
    await page.waitForURL(/\/settings\/developer/, { timeout: 15000 })
    await settled(page)
    await page.locator('[data-setting-key="feature-previews-section"][style*="outline"]')
      .waitFor({ state: 'visible', timeout: 5000 })
    await save(page, 'legacy-redirect')
    await page.context().close()
  }

  {
    // The Webhooks card ON: the "Open Webhooks" ingress is the only door to
    // the hidden page and exists only in this state, so it needs its own frame.
    const page = await openPage('light')
    await page.goto(base + '/settings/developer', { waitUntil: 'domcontentloaded' })
    await settled(page)
    await page.getByRole('switch', { name: /webhooks/i }).click()
    await page.getByRole('button', { name: /open webhooks/i }).waitFor({ state: 'visible', timeout: 5000 })
    await page.waitForTimeout(300)
    await save(page, 'webhooks-on')
    await page.context().close()
  }

  {
    // Settings search reaching the moved toggles: the three new registry
    // entries are only visible as search hits.
    const page = await openPage('light')
    await page.goto(base + '/settings/developer', { waitUntil: 'domcontentloaded' })
    await settled(page)
    const input = page.getByRole('combobox', { name: 'Search settings' })
    await input.fill('webhooks')
    await page.getByRole('listbox').waitFor({ state: 'visible', timeout: 5000 })
    await page.waitForTimeout(300)
    await save(page, 'search-webhooks')
    await page.context().close()
  }

  await browser.close()
  srv.close()
  console.log(`wrote ${shot.length} shot(s) to ${OUT}: ${shot.join(', ')}`)
}

main().catch(err => { console.error(err); process.exit(1) })

/**
 * Screenshot harness for Settings > About's update-channel switcher.
 *
 * Runs the REAL built SPA (website/dist) behind a tiny in-process static server
 * with SPA fallback and answers every /api/** call from the shared fixture
 * router via Playwright route interception. The desktop-only surface is reached
 * by injecting a window.updateAPI bridge before app scripts run — AboutPanel
 * derives isDesktop from that object, so the Electron-only rows (update channel,
 * platform) render in a plain browser without packaging the app.
 *
 * Two shots per run: the About card, and the card with the switcher's other lane
 * hovered, so the fix (both lanes side by side, nothing overlapping the Platform
 * row) is visible.
 *
 * Usage: node scripts/capture-channel-switcher.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { installApiFixtures, logPageFailures } from './lib/api-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/channel-switcher'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1500, height: 950 },
    // Settings rows are 12-13px type; a 1x shot renders soft on GitHub.
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()

  await installApiFixtures(page)
  logPageFailures(page)

  // The Electron preload bridge AboutPanel reads. Presence => isDesktop, and
  // channelSwitchable + setChannel => the switcher instead of a read-only row.
  await page.addInitScript(() => {
    localStorage.clear()
    localStorage.setItem('mc-theme', 'dark')
    localStorage.setItem('mc-onboarded', '1')
    window.updateAPI = {
      onState: () => () => {},
      check: async () => ({ ok: true }),
      download: async () => ({ ok: true }),
      install: async () => ({ ok: true }),
      getInfo: async () => ({
        version: '0.5.0',
        channel: 'stable',
        stampedChannel: 'stable',
        channelSwitchable: true,
        channelPreference: '',
        platform: 'darwin-arm64',
        packaged: true,
      }),
      setChannel: async () => ({ ok: true }),
    }
  })

  await page.goto(base + '/settings?tab=about', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)

  /** Crop to the identity hero card, which carries both rows in question. */
  async function card(name) {
    const switcher = page.locator('[data-testid="channel-switcher"]')
    const hero = switcher.locator('xpath=ancestor::div[contains(@class,"card-glow")][1]')
    const target = (await hero.count()) ? hero.first() : switcher.first()
    if (await target.count()) {
      const b = await target.boundingBox()
      if (b) {
        const pad = 16
        await page.screenshot({
          path: `${OUT}/${name}.png`,
          clip: {
            x: Math.max(0, b.x - pad),
            y: Math.max(0, b.y - pad),
            width: Math.min(1500 - Math.max(0, b.x - pad), b.width + pad * 2),
            height: b.height + pad * 2,
          },
        })
        console.log('wrote', `${OUT}/${name}.png`)
        return
      }
    }
    await page.screenshot({ path: `${OUT}/${name}.png` })
    console.log('wrote (full page fallback)', `${OUT}/${name}.png`)
  }

  await card(`${PREFIX}-01-about-card`)

  // The other lane: on the broken build this needs the dropdown opened first,
  // so click whatever control carries the current lane and re-shoot.
  const stable = page.getByRole('button', { name: /^Stable$/ })
  if (await stable.count()) {
    await stable.first().click()
    await page.waitForTimeout(600)
  }
  await card(`${PREFIX}-02-about-card-interacted`)

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })

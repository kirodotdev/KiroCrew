/**
 * Screenshot harness for Settings > About's channel explainer + prerelease note.
 *
 * Same shape as capture-channel-switcher.mjs: serves the REAL built SPA
 * (website/dist), answers /api/** from the shared fixture router, and injects a
 * window.updateAPI bridge before app scripts run so the Electron-only rows
 * render in a plain browser. `getInfo` reads a channel from the ?chan= query
 * param so one run can shoot both the stable and the insider state without two
 * harnesses.
 *
 * Builds the SPA first: serve-dist serves whatever is on disk, so shooting a
 * UI-only change against a stale dist yields an "after" image identical to
 * before — indistinguishable from the change not working.
 *
 * Four shots: stable collapsed (at rest), stable expanded (the explanation),
 * insider at rest (the always-on report note), insider expanded (both).
 *
 * Usage: node scripts/capture-channel-explainer.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { serveDist } from './lib/serve-dist.mjs'
import { installApiFixtures, logPageFailures } from './lib/api-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/channel-explainer'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

async function main() {
  if (!process.env.SKIP_BUILD) {
    console.log('building dist (SKIP_BUILD=1 to reuse)…')
    execFileSync('npm', ['run', 'build'], { stdio: 'inherit' })
  }

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

  await page.addInitScript(() => {
    localStorage.clear()
    localStorage.setItem('mc-theme', 'dark')
    localStorage.setItem('mc-onboarded', '1')
    // Channel comes from a QUERY PARAM (not the hash) so the same harness can
    // shoot both lanes: a hash-only change is a same-document navigation, so
    // neither this init script nor React state would be re-created and the
    // second pass would silently re-shoot the first lane.
    const chan = new URLSearchParams(location.search).get('chan') === 'insider' ? 'insider' : 'stable'
    window.updateAPI = {
      onState: () => () => {},
      check: async () => ({ ok: true }),
      download: async () => ({ ok: true }),
      install: async () => ({ ok: true }),
      getInfo: async () => ({
        version: chan === 'insider' ? '0.5.0-insider.2' : '0.5.0',
        channel: chan,
        // The note keys on the INSTALLED lane, so the fixture must set it.
        stampedChannel: chan,
        channelSwitchable: true,
        channelPreference: '',
        platform: 'darwin-arm64',
        packaged: true,
      }),
      setChannel: async () => ({ ok: true }),
    }
  })

  /** Crop to the identity hero card, which carries every row in question. */
  async function card(name) {
    const anchor = page.locator('[data-testid="channel-switcher"]')
    if (!(await anchor.count())) {
      // boundingBox() on a missing locator TIMES OUT rather than returning
      // null, so the absent case has to be handled before measuring.
      await page.screenshot({ path: `${OUT}/${name}.png` })
      console.log('wrote (full page fallback — switcher not found)', `${OUT}/${name}.png`)
      return
    }
    const hero = anchor.locator('xpath=ancestor::div[contains(@class,"card-glow")][1]')
    const target = (await hero.count()) ? hero.first() : anchor.first()
    const b = await target.boundingBox()
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
  }

  async function load(channel) {
    await page.goto(`${base}/settings?tab=about&chan=${channel}`, { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2600)
    // The disclosure sits where the pointer was left by the previous pass, so
    // its hover styling would leak into the next "at rest" shot.
    await page.mouse.move(1400, 900)
  }

  await load('stable')
  await card(`${PREFIX}-01-stable-collapsed`)
  await page.locator('[data-testid="channel-help-toggle"]').click()
  await page.waitForTimeout(300)
  await card(`${PREFIX}-02-stable-expanded`)

  await load('insider')
  await card(`${PREFIX}-03-insider-report-note`)
  await page.locator('[data-testid="channel-help-toggle"]').click()
  await page.waitForTimeout(300)
  await card(`${PREFIX}-04-insider-expanded`)

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })

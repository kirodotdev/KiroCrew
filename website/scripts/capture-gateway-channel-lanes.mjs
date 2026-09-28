/**
 * Screenshot harness for Settings > About's GATEWAY update-channel switcher —
 * the CLI / wheel install's control, not the Electron one that
 * capture-channel-switcher.mjs shoots.
 *
 * Runs the REAL built SPA (website/dist) behind the shared static server and
 * answers every /api/** call from the shared fixture router. Two things put the
 * gateway control on screen: NO window.updateAPI bridge (so AboutPanel derives
 * isDesktop=false) and an /api/status body carrying `update_channel`, which is
 * the only signal that says this layout has a channel the backend will move.
 *
 * Three shots per run, because the lane set is state-dependent:
 *   01 an insider install — the lanes the control offers
 *   02 the same card with the "What's the difference" disclosure open, which
 *      must describe exactly the lanes on screen and no others
 *   03 a nightly install — the one state where the Nightly segment renders, so
 *      the user can see the lane they are on and click out of it
 *
 * Usage: node scripts/capture-gateway-channel-lanes.mjs [outDir] [prefix]
 * For a before/after pair, run it once per source state (git stash the change,
 * rebuild, re-run with prefix `before`): the harness only ever mounts the real
 * component out of src/, so the difference comes from what is on disk.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { installApiFixtures, logPageFailures } from './lib/api-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/gateway-channel-lanes'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

/** A status body for an install that follows `channel` and runs `running`. */
const status = (channel, running) => ({
  sessions: 0, crons: 0, lessons: 0, uptime: 120, version: '0.5.0',
  update_channel: channel,
  release_channel: running,
  update_checked: true,
  update_available: false,
  update_self_updatable: false,
  update_command: `curl -fsSL https://download.crew.kiro.dev/cli.sh | sh -s -- --channel ${channel}`,
})

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()

  /**
   * One page per install shape: the fixture table is installed per page, and the
   * switcher reads the status once on mount.
   */
  async function shoot(name, channel, running, openHelp = false) {
    const context = await browser.newContext({
      viewport: { width: 1500, height: 950 },
      // Settings rows are 12-13px type; a 1x shot renders soft on GitHub.
      deviceScaleFactor: 2,
    })
    const page = await context.newPage()
    await installApiFixtures(page, { '/api/status': status(channel, running) })
    logPageFailures(page)
    await page.addInitScript(() => {
      localStorage.clear()
      localStorage.setItem('mc-theme', 'dark')
      localStorage.setItem('mc-onboarded', '1')
    })

    await page.goto(base + '/settings?tab=about', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2600)

    if (openHelp) {
      const toggle = page.locator('[data-testid="gateway-channel-help-toggle"]')
      if (await toggle.count()) {
        await toggle.first().click()
        await page.waitForTimeout(400)
      }
    }

    // Crop to the identity hero card, which carries the switcher row.
    const switcher = page.locator('[data-testid="gateway-channel-switcher"]')
    const hero = switcher.locator('xpath=ancestor::div[contains(@class,"card-glow")][1]')
    const target = (await hero.count()) ? hero.first() : switcher.first()
    const box = (await target.count()) ? await target.boundingBox() : null
    if (box) {
      const pad = 16
      await page.screenshot({
        path: `${OUT}/${name}.png`,
        clip: {
          x: Math.max(0, box.x - pad),
          y: Math.max(0, box.y - pad),
          width: Math.min(1500 - Math.max(0, box.x - pad), box.width + pad * 2),
          height: box.height + pad * 2,
        },
      })
      console.log('wrote', `${OUT}/${name}.png`)
    } else {
      await page.screenshot({ path: `${OUT}/${name}.png` })
      console.log('wrote (full page fallback — switcher not found)', `${OUT}/${name}.png`)
    }
    await context.close()
  }

  await shoot(`${PREFIX}-01-insider-lanes`, 'insider', 'insider')
  await shoot(`${PREFIX}-02-insider-explainer`, 'insider', 'insider', true)
  await shoot(`${PREFIX}-03-nightly-install`, 'nightly', 'nightly')

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })

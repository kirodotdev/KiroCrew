/**
 * Screenshot harness for Settings > About on an externally-managed install.
 *
 * Same shape as capture-channel-explainer.mjs: serves the REAL built SPA
 * (website/dist), answers /api/** from the shared fixture router, and injects a
 * window.updateAPI bridge before app scripts run so the Electron-only surfaces
 * render in a plain browser. The `?meta=` query param picks whether the marker
 * carried metadata, so one harness shoots both states.
 *
 * Builds the SPA first: serve-dist serves whatever is on disk, so shooting a
 * UI-only change against a stale dist yields an "after" image identical to
 * before — indistinguishable from the change not working.
 *
 * Two shots: the marker WITH metadata (managed-by message + copyable command,
 * and no channel switcher anywhere in the hero card), and the bare marker
 * (generic message, no command box).
 *
 * Usage: node scripts/capture-externally-managed.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { serveDist } from './lib/serve-dist.mjs'
import { installApiFixtures, logPageFailures } from './lib/api-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/externally-managed'
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
    // Metadata presence comes from a QUERY PARAM (not the hash) so the same
    // harness can shoot both states: a hash-only change is a same-document
    // navigation, so neither this init script nor React state would be
    // re-created and the second pass would silently re-shoot the first.
    const withMeta = new URLSearchParams(location.search).get('meta') !== 'none'
    window.updateAPI = {
      onState: () => () => {},
      check: async () => ({ ok: true }),
      download: async () => ({ ok: true }),
      install: async () => ({ ok: true }),
      getInfo: async () => ({
        version: '0.5.0',
        channel: 'stable',
        stampedChannel: 'stable',
        // What the marker forces: no switchable lane, and the disabled reason.
        channelSwitchable: false,
        channelPreference: '',
        platform: 'darwin-arm64',
        packaged: true,
        disabled: 'externally-managed',
        managedBy: withMeta ? 'internal-registry' : '',
        updateCommand: withMeta ? 'pkgtool update kirocrew' : '',
      }),
      setChannel: async () => ({ ok: true }),
    }
  })

  /** Crop to the Updates card (the managed message + command box). */
  async function updatesCard(name) {
    const anchor = page.locator('[data-testid="externally-managed-updates"]')
    if (!(await anchor.count())) {
      // boundingBox() on a missing locator TIMES OUT rather than returning
      // null, so the absent case has to be handled before measuring.
      await page.screenshot({ path: `${OUT}/${name}.png` })
      console.log('wrote (full page fallback — managed block not found)', `${OUT}/${name}.png`)
      return
    }
    const card = anchor.locator('xpath=ancestor::div[contains(@class,"card-glow")][1]')
    const target = (await card.count()) ? card.first() : anchor.first()
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

  async function load(meta) {
    await page.goto(`${base}/settings?tab=about&meta=${meta}`, { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2600)
    await page.mouse.move(1400, 900)
  }

  await load('full')
  // The switcher must be GONE, not merely restyled: its absence is half the
  // feature. A stray switcher here means the getInfo gate regressed.
  const switchers = await page.locator('[data-testid="channel-switcher"]').count()
  if (switchers) throw new Error('channel switcher rendered on an externally-managed install')
  await updatesCard(`${PREFIX}-01-managed-with-command`)

  await load('none')
  await updatesCard(`${PREFIX}-02-managed-bare-marker`)

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })

/**
 * Screenshot harness for the adoption notice when the archive rename is held (#32683).
 *
 * Runs the REAL built SPA behind `serveDist` and answers every /api/** call from
 * fixtures through `stubDashboardApi`. No gateway, no dashboard auth, no kiro-cli.
 *
 * One installed app whose `adoption` row is `pending`, shot twice: with the reason
 * the gateway recorded for a held archive before the fix (`network_unreachable`,
 * so the notice tells the owner to check the network) and with the reason it
 * records now (`stage_io_failed`, the notice's "local" remedy copy).
 *
 * Usage: node scripts/capture-32683-adoption-local-hold.mjs [outDir] [--dist=path]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

import { json } from './lib/boot-api.mjs'
import { serveDist, DEFAULT_DIST } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const args = process.argv.slice(2)
const flag = (name, fallback) => {
  const hit = args.find(a => a.startsWith(`--${name}=`))
  return hit ? hit.slice(name.length + 3) : fallback
}
const OUT = args.find(a => !a.startsWith('--')) || '/tmp/adoption-local-hold-shots'
const DIST = flag('dist', DEFAULT_DIST)
mkdirSync(OUT, { recursive: true })

const APP = 'pixel-pal'
const NOTICE = {
  network_unreachable: 'could not download its App Store version yet',
  stage_io_failed: 'could not prepare its App Store version on this computer',
}

function installedApp(reason) {
  return {
    name: APP, displayName: 'Pixel Pal', version: '1.0.0', enabled: true,
    installedAt: '2026-07-20T10:00:00Z', origin: 'builtin',
    resources: 'gateway', lifecycle: 'gateway',
    adoption: { state: 'pending', reason },
    manifest: {
      name: APP, version: '1.0.0', displayName: 'Pixel Pal',
      description: 'A former built-in moving to its App Store copy.',
      author: 'octocat', tags: ['images'],
      ui: { pages: [{ route: `/${APP}`, label: 'Pixel Pal', iconUrl: 'icon.svg' }] },
    },
  }
}

function fail(msg) { throw new Error(`ASSERTION FAILED: ${msg}`) }

const { srv, base } = await serveDist(DIST)
const browser = await chromium.launch()

for (const [label, reason] of [['before', 'network_unreachable'], ['after', 'stage_io_failed']]) {
  for (const theme of ['light', 'dark']) {
    const context = await browser.newContext({
      viewport: { width: 1400, height: 1000 }, deviceScaleFactor: 2, serviceWorkers: 'block',
    })
    const page = await context.newPage()
    const app = installedApp(reason)
    const extra = async (path, route) => {
      if (path === '/api/apps/registry') {
        await json(route, { apps: [], serverPlatform: { os: 'linux', arch: 'x86_64' } })
        return true
      }
      if (path === `/api/apps/${APP}`) { await json(route, app); return true }
      if (path === '/api/apps') { await json(route, [app]); return true }
      if (path === '/api/source/contributors') { await json(route, { contributors: [] }); return true }
      return false
    }
    await stubDashboardApi(page, { extra, theme })
    page.on('pageerror', err => console.log('PAGEERROR:', (err.stack || String(err)).slice(0, 600)))

    await page.goto(`${base}/apps/detail/${APP}`, { waitUntil: 'domcontentloaded' })
    const notice = page.getByRole('alert').filter({ hasText: NOTICE[reason] }).first()
    await notice.waitFor({ timeout: 15000 }).catch(() => fail(`${label}/${theme}: no notice naming "${NOTICE[reason]}"`))
    await page.waitForTimeout(800)

    const box = await notice.boundingBox()
    if (!box) fail(`${label}/${theme}: notice has no box`)
    const clip = { x: Math.max(0, box.x - 24), y: Math.max(0, box.y - 120), width: Math.min(1400, box.width + 48), height: box.height + 160 }
    const path = `${OUT}/adoption-${label}-${theme}.png`
    await page.screenshot({ path, clip })
    console.log(`OK ${label}/${theme} (${reason}) -> ${path}`)
    await context.close()
  }
}

await browser.close()
srv.close()

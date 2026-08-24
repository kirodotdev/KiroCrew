/**
 * Screenshot harness for the Overview built-in suppression seam.
 *
 * The suppressed state is NOT reachable from this repo's own code: nothing in
 * the stock tree calls `suppressOverviewBuiltin`, which is exactly the point —
 * the stock OSS build must render as it always did. So a single build cannot
 * show both states, and this harness runs TWO REAL BUILDS of the SPA:
 *
 *   stock    KIROCREW_EDITION_DIR unset  -> `virtual:kirocrew-edition` is inert
 *   edition  KIROCREW_EDITION_DIR=<tmp>  -> a throwaway composition root whose
 *                                           only line calls the new seam
 *
 * The edition fixture is WRITTEN BY THIS SCRIPT into a temp dir rather than
 * committed, so the capture exercises the real downstream path (an out-of-repo
 * edition composed through the same vite pass) with no permanent fixture in the
 * OSS tree.
 *
 * Both runs answer `GET /api/tailnet/mobile` with the SAME pinned payload, so
 * the only difference between the two screenshots is the seam.
 *
 * This harness ASSERTS as well as captures: it fails if the card is missing
 * from the stock shot or present in the edition shot. Two screenshots that
 * merely look different are not evidence — a harness that would still "pass"
 * with the gate deleted proves nothing.
 *
 * Captures:
 *   overview-tailnet-card-stock.png       card renders (unchanged OSS default)
 *   overview-tailnet-card-suppressed.png  card gone, no gap left behind
 *
 * Usage: node scripts/capture-overview-builtin-suppression.mjs <outDir>
 * (`npm run build` first if you also want the typecheck; this script invokes
 * `vite build` directly, twice, into temp out-dirs of its own.)
 */
import { chromium } from 'playwright'
import { execFileSync } from 'node:child_process'
import { mkdirSync, mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { handleBootRoute, json, makeFixedApi } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'

const OUT = process.argv[2] || join(tmpdir(), 'shots')
const PROJECT = '/home/kirocrew/workspace'
const WEBSITE = fileURLToPath(new URL('..', import.meta.url))
mkdirSync(OUT, { recursive: true })

const work = mkdtempSync(join(tmpdir(), 'overview-suppression-'))
const editionDir = join(work, 'edition')
mkdirSync(editionDir, { recursive: true })

// The whole downstream contribution: one call, no component, no override.
writeFileSync(
  join(editionDir, 'extensions.tsx'),
  "import { suppressOverviewBuiltin } from '@/pages/overviewBuiltins'\n\n" +
    "suppressOverviewBuiltin('tailnet-mobile')\n"
)

const VITE = join(WEBSITE, 'node_modules', 'vite', 'bin', 'vite.js')

function build(label, outDir, env) {
  console.log(`[build:${label}] -> ${outDir}`)
  execFileSync(
    process.execPath,
    ['--max-old-space-size=6144', VITE, 'build', '--outDir', outDir, '--emptyOutDir'],
    { cwd: WEBSITE, stdio: 'inherit', env: { ...process.env, ...env } }
  )
}

const stockDist = join(work, 'dist-stock')
const editionDist = join(work, 'dist-edition')
build('stock', stockDist, { KIROCREW_EDITION_DIR: '', KIROCREW_ALLOW_EDITION: '' })
build('edition', editionDist, {
  KIROCREW_EDITION_DIR: editionDir,
  KIROCREW_ALLOW_EDITION: '1',
})

const status = {
  sessions: 3, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0,
  uptime: 921, version: '0.1.0',
}

/** The pinned state: an administrator's policy has pinned tailnet access off,
 *  so the card can only ever tell the operator to go ask someone. That is the
 *  dead end an enterprise edition wants gone from its Overview. */
const tailnetPinned = {
  step: 'pinned',
  host: '', origin: '',
  installed: false, reachable: false, logged_in: false,
  peer_count: 0, peers_online: 0,
  trusted: false, startup_trusted: false, published: null,
  keep_awake: false, governance_pinned: true,
  detail: '', download_url: 'https://tailscale.com/download',
  qr_ttl_secs: 3600, serve_port: 443, dashboard_port: 5476,
}

const fixedApi = makeFixedApi(PROJECT)
fixedApi.set('/api/status', status)
fixedApi.set('/api/dashboard/branding', { bot_name: 'Kiro Crew', avatar: '/logo.png' })

const browser = await chromium.launch()

/** Screenshot the Overview of one dist, and report whether the card rendered. */
async function shoot(dist, name) {
  const { srv, base } = await serveDist(dist)
  const context = await browser.newContext({
    viewport: { width: 1520, height: 900 },
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()
  let wsServer = null
  await page.routeWebSocket(/\/api\/ws/, ws => { wsServer = ws })
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/tailnet/mobile') return json(route, tailnetPinned)
    if (path === '/api/memory/settings') {
      return json(route, { history_idle_hours: 3, history_max_days: 90, migrated: false })
    }
    return handleBootRoute(route, path, { project: PROJECT, theme: 'light', fixedApi })
  })
  page.on('pageerror', err => console.log('PAGEERROR:', (err.stack || String(err)).slice(0, 400)))
  await page.addInitScript(() => { localStorage.setItem('mc-onboarded', '1') })

  await page.goto(`${base}/settings?tab=overview`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(1800)
  if (wsServer) wsServer.send(JSON.stringify({ type: 'status', data: status }))
  await page.waitForTimeout(800)

  // The pinned card's own wording, so this cannot be satisfied by the OTHER
  // "Phone access" surface an edition may register in the same region.
  const card = page.getByText(/pins tailnet access off/i)
  const present = (await card.count()) > 0
  await page.screenshot({ path: `${OUT}/${name}.png` })
  await context.close()
  srv.close()
  return present
}

const stockHasCard = await shoot(stockDist, 'overview-tailnet-card-stock')
const editionHasCard = await shoot(editionDist, 'overview-tailnet-card-suppressed')
await browser.close()

console.log(`stock build   card rendered: ${stockHasCard}`)
console.log(`edition build card rendered: ${editionHasCard}`)

// Fail loudly rather than emit a pair of pictures that prove nothing: a stock
// build that never rendered the card would make the "after" shot meaningless,
// and an edition build that still renders it means the seam does not work.
if (!stockHasCard) {
  throw new Error(
    'stock build did NOT render the pinned card — the fixture no longer reaches ' +
      'the card, so the suppressed shot would be vacuous.'
  )
}
if (editionHasCard) {
  throw new Error('edition build STILL renders the pinned card — the suppression seam did not take.')
}
console.log(`done -> ${OUT}`)

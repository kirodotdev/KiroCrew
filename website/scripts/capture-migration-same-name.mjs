/**
 * Screenshot harness for the migration page when a retired builtin's successor
 * ships under the SAME name.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server with every /api/** answered from fixtures (gateway-free).
 *
 * The scenario: `retired-app` is a stale builtin record (origin builtin,
 * orphaned) and the registry carries its successor under the same name, marked
 * installed only because the stale record holds the slot. The page must offer
 * Install, not "Migration Complete", and clicking it must clean up the stale
 * record and open the install page. Pass `--expect-stale` with a base-branch
 * dist (`--dist <dir>`) to capture the BEFORE frame. Pass `--notice` to have
 * cleanup answer `ok` with a `notice` and capture the completion card that keeps
 * the notice and the Install handoff.
 *
 * Usage: node scripts/capture-migration-same-name.mjs [outDir] [prefix] [--dist <dir>] [--expect-stale] [--notice]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const positional = process.argv.slice(2).filter((a, i, all) => !a.startsWith('--') && all[i - 1] !== '--dist')
const OUT = positional[0] || '../temp-screenshots/migration-same-name'
const PREFIX = positional[1] || 'after'
const distIdx = process.argv.indexOf('--dist')
const DIST = distIdx > -1 ? process.argv[distIdx + 1] : undefined
const EXPECT_STALE = process.argv.includes('--expect-stale')
const NOTICE = process.argv.includes('--notice')
  ? 'Conversation pointers were dropped in memory, but the write did not persist -- a reinstall may still resume one'
  : undefined

mkdirSync(OUT, { recursive: true })

const STALE = {
  name: 'retired-app', displayName: 'Retired App', version: '1.0.0', enabled: true,
  origin: 'builtin', source: 'builtin',
}

async function main() {
  const { srv, base } = await serveDist(DIST)
  const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
  const browser = await chromium.launch({ env: browserEnv })
  const context = await browser.newContext({ viewport: { width: 1280, height: 760 }, deviceScaleFactor: 2 })
  const page = await context.newPage()

  let cleanupCalls = 0
  await stubDashboardApi(page, {
    theme: 'light',
    extra: async (path, route) => {
      if (path === '/api/apps/retired-app/migrate-cleanup') {
        cleanupCalls += 1
        await json(route, { ok: true, name: 'retired-app', message: 'cleaned up migrated builtin entry, data preserved', ...(NOTICE ? { notice: NOTICE } : {}) })
        return true
      }
      if (path === '/api/apps') { await json(route, [{ ...STALE, orphaned: true }]); return true }
      if (path === '/api/apps/retired-app') { await json(route, STALE); return true }
      if (path === '/api/apps/registry') {
        await json(route, { apps: [{ name: 'retired-app', displayName: 'Retired App', installed: true }] })
        return true
      }
      return false
    },
  })
  logPageProblems(page)
  page.on('pageerror', e => console.log('PAGEERROR', e.message))

  await page.goto(`${base}/apps/migrate/retired-app`, { waitUntil: 'domcontentloaded' })
  await page.getByText('has moved to a standalone app').first().waitFor({ timeout: 20_000 })
  await page.waitForTimeout(500)

  const claimsComplete = await page.getByText('Migration Complete').count()
  const install = page.getByRole('button', { name: /Install from Apps/ })
  if (EXPECT_STALE) {
    if (!claimsComplete) throw new Error('expected the BEFORE frame to claim "Migration Complete"')
    await page.screenshot({ path: `${OUT}/${PREFIX}-1-page.png` })
  } else {
    if (claimsComplete) throw new Error('page still claims "Migration Complete" for a successor that is not installed')
    await install.waitFor({ state: 'visible', timeout: 10_000 })
    await page.screenshot({ path: `${OUT}/${PREFIX}-1-page.png` })
    await install.click()
    if (NOTICE) {
      await page.getByText('Cleanup Complete').waitFor({ timeout: 10_000 })
      await page.getByText(NOTICE).waitFor({ timeout: 10_000 })
      await page.waitForTimeout(300)
      await page.screenshot({ path: `${OUT}/${PREFIX}-2-notice.png` })
      console.log(`wrote ${OUT}/${PREFIX}-2-notice.png`)
      await page.getByRole('button', { name: /Install from Apps/ }).click()
    }
    await page.waitForURL(/\/apps\/detail\/retired-app/, { timeout: 10_000 })
    if (cleanupCalls !== 1) throw new Error(`expected one cleanup call, saw ${cleanupCalls}`)
    console.log('cleanup called, landed on', new URL(page.url()).pathname)
  }

  await browser.close()
  srv.close()
  console.log(`wrote ${OUT}/${PREFIX}-1-page.png`)
}

main().catch(err => { console.error(err); process.exit(1) })

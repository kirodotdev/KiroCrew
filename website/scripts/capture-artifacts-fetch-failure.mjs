/**
 * Screenshot harness for the Artifacts library when the LIST FETCH FAILS
 * (#10867).
 *
 * The reported symptom is a full library rendering as "No artifacts yet" after
 * a dashboard window outlives its auth (gateway restart / cookie TTL): the
 * list query errors, `data` is undefined, and the derived `[]` was
 * indistinguishable from a genuinely empty library. Two frames:
 *
 *   1. `failed.png`    — GET /api/artifacts is a 403; the page-level
 *      ErrorNotice banner names the failure AND the gallery shows the
 *      truthful "Couldn't load your artifacts" placeholder with Retry.
 *      "No artifacts yet" is nowhere on the page.
 *   2. `recovered.png` — after Retry, with the endpoint healthy, the library.
 *
 * The frames cannot lie: the script ASSERTS the error copy is present and the
 * misleading "No artifacts yet" is absent in frame 1, and that every seeded
 * artifact renders in frame 2. Any of those failing exits non-zero and the
 * PNGs are not citable.
 *
 * Runs the REAL built SPA (website/dist) behind the shared `serveDist` server
 * and answers every /api/** call from fixtures through `stubDashboardApi`.
 *
 * Usage: node scripts/capture-artifacts-fetch-failure.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi, logPageProblems } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/artifacts-fetch-failure-shots'
const ERROR_COPY = 'Couldn’t load your artifacts'
const MISLEADING = 'No artifacts yet'

mkdirSync(OUT, { recursive: true })

const ARTIFACTS = [
  { slug: 'cr-queue', name: 'cr queue', kind: 'widget', source: 'chat', pinned: true, description: '', tags: ['ops'], version: 3, created_at: '2026-09-01T10:00:00.000000+00:00', updated_at: '2026-09-12T10:00:00.000000+00:00' },
  { slug: 'pipeline-health', name: 'pipeline health', kind: 'widget', source: 'chat', pinned: true, description: '', tags: ['ops'], version: 1, created_at: '2026-09-02T10:00:00.000000+00:00', updated_at: '2026-09-11T10:00:00.000000+00:00' },
  { slug: 'latency-report', name: 'latency report', kind: 'markdown', source: 'chat', pinned: false, description: '', tags: [], version: 2, created_at: '2026-09-03T10:00:00.000000+00:00', updated_at: '2026-09-10T10:00:00.000000+00:00' },
]

// Flipped between the two frames: the list endpoint is broken for the first,
// healthy for the second, so Retry has something to recover to.
let listHealthy = false

const { srv, base } = await serveDist()
const browser = await chromium.launch()

try {
  const context = await browser.newContext({
    // 1x, per the image-read ceiling the sibling harnesses observe.
    viewport: { width: 1500, height: 900 },
    deviceScaleFactor: 1,
  })
  const page = await context.newPage()
  logPageProblems(page)

  const FIXTURES = new Map([
    ['/api/artifact-session-docs', { docs: [] }],
  ])

  await stubDashboardApi(page, {
    localStorageEntries: { 'mc-lang': 'en' },
    extra: async (path, route) => {
      if (path === '/api/artifacts' || path.startsWith('/api/artifacts?') || path === '/api/artifact-folders') {
        if (!listHealthy) {
          // The #10867 trigger: a window that outlived its auth gets 403s on
          // EVERY endpoint — the folder list fails alongside the artifact
          // list, so the heal must recover both.
          await route.fulfill({ status: 403, contentType: 'application/json', body: '{"error":"authentication required"}' })
          return true
        }
        await json(route, path === '/api/artifact-folders' ? { folders: [] } : { artifacts: ARTIFACTS })
        return true
      }
      if (path.startsWith('/api/publish-providers')) {
        await json(route, { providers: [] })
        return true
      }
      if (!FIXTURES.has(path)) return false
      await json(route, FIXTURES.get(path))
      return true
    },
  })

  await page.goto(base + '/artifacts', { waitUntil: 'domcontentloaded' })

  // Frame 1: banner + truthful placeholder, no lie.
  const placeholder = page.getByTestId('artifacts-error-state')
  await placeholder.waitFor({ timeout: 15000 })
  await placeholder.getByText(ERROR_COPY).waitFor({ timeout: 5000 })
  // The banner animates in (animate-rise); let it settle before the shot.
  await page.waitForTimeout(600)

  const misleading = await page.getByText(MISLEADING).count()
  const bannerShowsError = await page.getByText(/authentication required/i).count()
  await page.screenshot({ path: join(OUT, 'failed.png') })

  // Frame 2: Retry with the endpoint healthy this time.
  listHealthy = true
  await page.getByTestId('artifacts-error-state-action').getByRole('button').click()
  await page.getByText('cr queue').first().waitFor({ timeout: 10000 })
  await page.waitForTimeout(600)

  const offered = await Promise.all(ARTIFACTS.map(a => page.getByText(a.name).count()))
  await page.screenshot({ path: join(OUT, 'recovered.png') })

  await context.close()

  console.log(`frame 1 — "${MISLEADING}" occurrences:`, misleading, '(expected 0)')
  console.log('frame 1 — banner carries the real error:', bannerShowsError > 0, '(expected true)')
  console.log('frame 2 — artifacts rendered after Retry:', offered.filter(n => n > 0).length, 'of', ARTIFACTS.length)

  if (misleading !== 0) {
    console.error(`FAIL: frame 1 still shows "${MISLEADING}"`)
    process.exit(1)
  }
  if (bannerShowsError === 0) {
    console.error('FAIL: frame 1 banner does not surface the real error')
    process.exit(1)
  }
  if (offered.some(n => n === 0)) {
    console.error('FAIL: frame 2 is missing seeded artifacts')
    process.exit(1)
  }
  console.log('shots at', OUT)
} finally {
  await browser.close()
  srv.close()
}

/**
 * Screenshot harness for the artifact star (pinned) affordance.
 *
 * Runs the REAL built SPA (website/dist) behind a tiny in-process static server
 * and answers every /api/** call from fixtures via Playwright route interception
 * (gateway-free — no kiro-cli, no live backend).
 *
 * The star used to exist ONLY in the table view, while the Starred filter and
 * the Starred StatCard applied to BOTH views — so in the default gallery you
 * could filter by starred without being able to star anything, and the detail
 * page (where you actually read an artifact and decide to keep it) had no star
 * at all. `pinned` is also the retention control: prune_auto_widgets only
 * sweeps unpinned records.
 *
 * Frames:
 *   01-gallery-cards   masonry card footers — starred vs unstarred
 *   02-detail-starred  detail header chip, pinned   ("★ Starred", accent)
 *   03-detail-unstarred detail header chip, unpinned ("☆ Star", muted)
 *
 * The point of the change is presence, so run against the branch (after) and
 * against origin/main (before) to see the delta.
 *
 * Usage: node scripts/capture-artifact-star.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/artifact-star'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

const artifact = (slug, name, pinned, overrides = {}) => ({
  slug,
  name,
  kind: 'widget',
  source: 'chat',
  session_title: 'Artifact starring',
  description: '',
  tags: [],
  version: 3,
  pinned,
  created_at: '2026-07-30T10:00:00.000000+00:00',
  updated_at: '2026-08-01T21:00:00.000000+00:00',
  ...overrides,
})

// One starred and two unstarred so both card states sit in a single frame.
const ARTIFACTS = [
  artifact('i18n-measurement-gaps', 'i18n measurement gaps', true, {
    description: 'Audit of the i18n mechanism vs measured translation quality',
    tags: ['rfc', 'i18n'],
  }),
  artifact('cr-queue', 'CR Queue', false, {
    description: 'Hourly snapshot of the review queue',
    tags: ['ops'],
  }),
  artifact('pipeline-health', 'Pipeline health', false, {
    description: 'Stage timings and blockage for the release pipeline',
    tags: ['ops'],
  }),
]

const BODY = '<div style="padding:14px;font:13px system-ui">Artifact preview body</div>'

const byslug = Object.fromEntries(ARTIFACTS.map(a => [a.slug, a]))

/** Artifact endpoints the library + detail page reach for. */
const extra = async (path, route) => {
  if (path === '/api/artifacts') return json(route, { artifacts: ARTIFACTS }), true
  if (path === '/api/artifact-folders') return json(route, { folders: [] }), true
  if (path === '/api/artifacts/session-docs') return json(route, { docs: [] }), true

  const m = /^\/api\/artifacts\/([^/]+)(\/.*)?$/.exec(path)
  if (!m) return false
  const slug = decodeURIComponent(m[1])
  const rest = m[2] || ''
  const a = byslug[slug]
  if (!a) return false

  if (rest === '/versions') return json(route, { slug, versions: [1, 2, 3] }), true
  if (rest === '/events') return json(route, { slug, events: [] }), true
  if (rest === '/comments') return json(route, { comments: [] }), true
  if (rest === '/upstream-status') return json(route, {}), true
  if (rest === '') return json(route, { ...a, content: BODY }), true
  return false
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1500, height: 1100 },
    deviceScaleFactor: 2, // 11-13px chip type renders soft at 1x on GitHub
  })
  const page = await context.newPage()

  await stubDashboardApi(page, { extra })
  logPageProblems(page)

  // ── Frame 1: the default gallery, card footers ───────────────────────────
  await page.goto(base + '/artifacts', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)
  await page.screenshot({ path: `${OUT}/${PREFIX}-01-gallery-cards.png`, fullPage: false })
  console.log('wrote', `${OUT}/${PREFIX}-01-gallery-cards.png`)

  // ── Frames 2 + 3: the detail header chip, both states ────────────────────
  async function detailShot(slug, name) {
    await page.goto(base + `/artifacts/${slug}`, { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2200)
    // Crop to the header: page title + the metadata chip row beneath it.
    await page.screenshot({
      path: `${OUT}/${PREFIX}-${name}.png`,
      clip: { x: 0, y: 0, width: 1500, height: 300 },
    })
    console.log('wrote', `${OUT}/${PREFIX}-${name}.png`)
  }

  await detailShot('i18n-measurement-gaps', '02-detail-starred')
  await detailShot('cr-queue', '03-detail-unstarred')

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })

/**
 * Screenshot harness for artifacts list sorting + detail copy-content.
 *
 * Runs the REAL built SPA (website/dist) behind a tiny in-process static
 * server and answers every /api/** call from fixtures via Playwright route
 * interception (gateway-free — no kiro-cli, no live backend).
 *
 * Frames:
 *   01-table-default     table view, server order, no sort armed
 *   02-table-sort-name   Name header clicked — ascending, chevron indicator
 *   03-table-sort-ver    Ver header clicked twice — numeric descending
 *   04-detail-copy       detail body with the floating copy-content button
 *   05-detail-copied     the same control in its post-click "Copied" state
 *
 * Usage: node scripts/capture-artifacts-sort-copy.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/artifacts-sort-copy'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

const artifact = (slug, name, version, updated, overrides = {}) => ({
  slug,
  name,
  kind: 'markdown',
  source: 'chat',
  session_title: 'Docs session',
  description: '',
  tags: ['docs'],
  version,
  pinned: false,
  created_at: '2026-07-01T10:00:00.000000+00:00',
  updated_at: updated,
  ...overrides,
})

// Mixed names/versions/dates so every sort visibly rearranges — including a
// numeric-suffix pair (report-2 vs report-10) that proves natural ordering.
const ARTIFACTS = [
  artifact('report-10', 'report-10', 10, '2026-08-02T09:00:00.000000+00:00', { tags: ['ops'] }),
  artifact('alpha-notes', 'alpha notes', 1, '2026-08-05T12:00:00.000000+00:00'),
  artifact('report-2', 'report-2', 2, '2026-08-01T08:00:00.000000+00:00', { tags: ['ops'] }),
  artifact('zeta-summary', 'zeta summary', 4, '2026-08-04T16:00:00.000000+00:00'),
]

const RAW_MD = '# Release notes\n\n- sortable columns on the artifacts list\n- copy-content on the detail view\n'

const byslug = Object.fromEntries(ARTIFACTS.map(a => [a.slug, a]))

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

  if (rest === '/versions') return json(route, { slug, versions: [1] }), true
  if (rest === '/events') return json(route, { slug, events: [] }), true
  if (rest === '/comments') return json(route, { comments: [] }), true
  if (rest === '/upstream-status') return json(route, {}), true
  if (rest === '') return json(route, { ...a, content: RAW_MD }), true
  return false
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1500, height: 900 },
    deviceScaleFactor: 2,
    permissions: ['clipboard-read', 'clipboard-write'],
  })
  const page = await context.newPage()
  // Table view + the "All" filter, so the sortable headers are on screen.
  await page.addInitScript(() => {
    localStorage.setItem('mc-artifacts-view', 'table')
    localStorage.setItem('mc-artifacts-pinned-only', '0')
  })

  await stubDashboardApi(page, { extra })
  logPageProblems(page)

  // ── Frames 1-3: the table, unsorted then sorted ──────────────────────────
  await page.goto(base + '/artifacts', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2200)
  // Switch to the table view via its segmented control (the persisted-view
  // localStorage seed is not reliable across the harness's navigations).
  await page.getByRole('button', { name: /^table$/i }).click()
  await page.waitForTimeout(600)
  const tableClip = { x: 0, y: 0, width: 1500, height: 640 }
  await page.screenshot({ path: `${OUT}/${PREFIX}-01-table-default.png`, clip: tableClip })
  console.log('wrote', `${OUT}/${PREFIX}-01-table-default.png`)

  // Header text renders uppercase (CSS), so accessible names are uppercased —
  // match case-insensitively.
  await page.getByRole('button', { name: /^name$/i }).click()
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/${PREFIX}-02-table-sort-name.png`, clip: tableClip })
  console.log('wrote', `${OUT}/${PREFIX}-02-table-sort-name.png`)

  await page.getByRole('button', { name: /^ver$/i }).click()
  await page.getByRole('button', { name: /^ver$/i }).click()
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/${PREFIX}-03-table-sort-ver.png`, clip: tableClip })
  console.log('wrote', `${OUT}/${PREFIX}-03-table-sort-ver.png`)

  // ── Frames 4 + 5: detail copy button, idle then confirmed ────────────────
  await page.goto(base + '/artifacts/alpha-notes', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2200)
  const detailClip = { x: 0, y: 0, width: 1500, height: 700 }
  await page.screenshot({ path: `${OUT}/${PREFIX}-04-detail-copy.png`, clip: detailClip })
  console.log('wrote', `${OUT}/${PREFIX}-04-detail-copy.png`)

  await page.getByRole('button', { name: 'Copy content' }).click()
  await page.waitForTimeout(300)
  await page.screenshot({ path: `${OUT}/${PREFIX}-05-detail-copied.png`, clip: detailClip })
  console.log('wrote', `${OUT}/${PREFIX}-05-detail-copied.png`)

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })

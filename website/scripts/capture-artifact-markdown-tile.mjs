/**
 * Screenshot probe: the two Gallery surfaces this PR changes.
 *
 * Runs the REAL built SPA (website/dist) behind the in-process static server and
 * answers every /api/** call from fixtures via Playwright route interception, so
 * there is no gateway and no network in the frame. Two frames, both fixtures the
 * review lanes asked to see:
 *   01 — a folder holding a markdown report whose first lines are a table, which
 *        is what used to preview as a literal `|---|---|` row.
 *   02 — a registered publish provider whose browse call is REJECTED, which is
 *        what used to render as a full-width provider Card below the library.
 *
 * Usage: node scripts/capture-artifact-markdown-tile.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/artifact-markdown-tile'
const PREFIX = process.argv[3] || 'after'
mkdirSync(OUT, { recursive: true })

const REPORT = {
  slug: 'weekly-gate-report',
  name: 'weekly gate report',
  kind: 'markdown',
  source: 'chat',
  pinned: false,
  description: '',
  tags: [],
  version: 1,
  folder_id: 'reports',
  created_at: '2026-09-14T09:00:00.000000+00:00',
  updated_at: '2026-09-14T09:00:00.000000+00:00',
}
const CONTENT = [
  '# Weekly gate report',
  '',
  '| Check | Result |',
  '|---|---|',
  '| Build | Passed |',
  '| Coverage | 81.2% |',
  '',
  'Every lane held this week; the coverage ratchet moved once.',
].join('\n')

const FOLDERS = [{ id: 'reports', name: 'Reports', order: 0, parent_id: '', item_count: 1 }]

/** One discovery-capable provider, so a RemoteBrowseSection mounts at all. Its
 * browse call is answered with a 502 below, which is the state under review. */
const PROVIDER = {
  name: 'companion',
  display_name: 'Companion Provider',
  capabilities: ['content_versions', 'sharing'],
  kind_support: 'native',
  capable: true,
  sharing_model: {
    supports_private: true, supports_shared: true, supports_public: true,
    principal_kind: 'user', supports_roles: false, supports_expiration: false,
    programmable: true, out_of_band_url: '',
  },
  sync_model: { authority: 'mirror', concurrency: 'token', collab_mode: 'mirror' },
  discovery_model: {
    list_mine: true, list_shared_with_me: true, list_public: true,
    full_text_search: false, pull_by_id: true,
  },
}
// Long on purpose: the two-line clamp and the full-text `title` are only
// observable on a message that does not fit the bounded row.
const BROWSE_ERROR =
  'Companion Provider could not be reached: the discovery endpoint answered 502 ' +
  'Bad Gateway after 3 attempts, so the remote listing for this device is unavailable ' +
  'until the provider recovers.'

async function main() {
  const served = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1200, height: 900 },
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()
  await stubDashboardApi(page, {
    extra: async (path, route) => {
      if (path === '/api/artifacts') { await json(route, { artifacts: [REPORT] }); return true }
      if (path === '/api/artifact-folders') { await json(route, { folders: FOLDERS }); return true }
      if (path === '/api/artifacts/session-docs') { await json(route, { docs: [] }); return true }
      if (path === '/api/artifacts/publish-providers') { await json(route, { providers: [], kind: 'markdown' }); return true }
      if (path === '/api/publish-providers') { await json(route, { providers: [] }); return true }
      if (path === `/api/artifacts/${REPORT.slug}`) { await json(route, { ...REPORT, content: CONTENT }); return true }
      return false
    },
  })
  logPageProblems(page)
  await page.goto(served.base + '/artifacts', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(3000)
  const tile = page.getByTestId('artifact-mini-text').first()
  const text = (await tile.count()) ? await tile.innerText() : '<<no mini tile>>'
  console.log('MINI TILE TEXT:', JSON.stringify(text))
  console.log('CONTAINS PIPE:', text.includes('|'))
  await page.screenshot({
    path: `${OUT}/${PREFIX}-01-markdown-tile.png`,
    clip: { x: 0, y: 0, width: 1100, height: 380 },
  })

  // Frame 02: same page, but the provider registry now holds one provider and
  // its browse call fails. A separate page so frame 01 keeps a clean registry.
  const failing = await context.newPage()
  await stubDashboardApi(failing, {
    extra: async (path, route) => {
      if (path === '/api/artifacts') { await json(route, { artifacts: [REPORT] }); return true }
      if (path === '/api/artifact-folders') { await json(route, { folders: FOLDERS }); return true }
      if (path === '/api/artifacts/session-docs') { await json(route, { docs: [] }); return true }
      if (path === '/api/artifacts/publish-providers') { await json(route, { providers: [PROVIDER], kind: 'markdown' }); return true }
      if (path === '/api/publish-providers') { await json(route, { providers: [PROVIDER] }); return true }
      if (path === `/api/artifacts/${REPORT.slug}`) { await json(route, { ...REPORT, content: CONTENT }); return true }
      if (path.startsWith('/api/remote-artifacts/companion/browse')) {
        await route.fulfill({ status: 502, contentType: 'application/json', body: JSON.stringify({ error: BROWSE_ERROR }) })
        return true
      }
      return false
    },
  })
  logPageProblems(failing)
  await failing.goto(served.base + '/artifacts', { waitUntil: 'domcontentloaded' })
  await failing.waitForTimeout(6000)
  const notice = failing.getByTestId('remote-browse-error-companion').first()
  const found = await notice.count()
  console.log('ERROR NOTICE PRESENT:', !!found)
  if (found) {
    console.log('ERROR NOTICE TEXT:', JSON.stringify(await notice.innerText()))
    const row = notice.locator('xpath=..')
    console.log('ROW CLASS:', await row.getAttribute('class'))
    await notice.scrollIntoViewIfNeeded()
    const box = await row.boundingBox()
    console.log('ROW WIDTH:', box && Math.round(box.width))
    await failing.screenshot({
      path: `${OUT}/${PREFIX}-02-remote-browse-error.png`,
      clip: box
        ? { x: Math.max(0, box.x - 24), y: Math.max(0, box.y - 40), width: Math.min(1100, box.width + 120), height: box.height + 90 }
        : { x: 0, y: 0, width: 1100, height: 380 },
    })
  }
  await browser.close()
  await served.close?.()
  process.exit(found ? 0 : 1)
}

main()

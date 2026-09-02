/**
 * Screenshot harness for artifacts sort persistence + copy UX.
 *
 * Runs the real built SPA behind the shared gateway-free fixture server.
 * Frames:
 *   01-table-sort-persisted  Updated descending after a full reload
 *   02-detail-medium         copy control aligned to the reading-width card
 *   03-detail-copy-failed    localized clipboard failure feedback
 *   04-detail-iframe         HTML iframe + copy control both full width
 *
 * Usage: node scripts/capture-artifacts-sort-copy.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/artifacts-sort-copy-2907'
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

const ARTIFACTS = [
  artifact('report-10', 'report-10', 10, '2026-08-02T09:00:00.000000+00:00', { tags: ['ops'] }),
  artifact('alpha-notes', 'alpha notes', 1, '2026-08-05T12:00:00.000000+00:00'),
  artifact('report-2', 'report-2', 2, '2026-08-01T08:00:00.000000+00:00', { tags: ['ops'] }),
  artifact('zeta-summary', 'zeta summary', 4, '2026-08-04T16:00:00.000000+00:00'),
  artifact('html-report', 'HTML report', 3, '2026-08-03T14:00:00.000000+00:00', { kind: 'html' }),
]

const RAW_MD = '# Release notes\n\n- persisted sorting on the artifacts list\n- aligned copy-content feedback\n'
const RAW_HTML = '<main style="padding:32px"><h1>Full-width HTML report</h1><p>The iframe and copy control share the available width.</p></main>'
const byslug = Object.fromEntries(ARTIFACTS.map(a => [a.slug, a]))

const extra = async (path, route) => {
  if (path === '/api/artifacts') return json(route, { artifacts: ARTIFACTS }), true
  if (path === '/api/artifact-folders') return json(route, { folders: [] }), true
  if (path === '/api/artifacts/session-docs') return json(route, { docs: [] }), true

  const match = /^\/api\/artifacts\/([^/]+)(\/.*)?$/.exec(path)
  if (!match) return false
  const slug = decodeURIComponent(match[1])
  const rest = match[2] || ''
  const current = byslug[slug]
  if (!current) return false

  if (rest === '/versions') return json(route, { slug, versions: [1] }), true
  if (rest === '/events') return json(route, { slug, events: [] }), true
  if (rest === '/comments') return json(route, { comments: [] }), true
  if (rest === '/upstream-status') return json(route, {}), true
  if (rest === '') {
    return json(route, { ...current, content: current.kind === 'html' ? RAW_HTML : RAW_MD }), true
  }
  return false
}

async function main() {
  const { srv, base } = await serveDist()
  const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH
  const browser = await chromium.launch(executablePath ? { executablePath } : {})
  const context = await browser.newContext({
    viewport: { width: 1500, height: 900 },
    deviceScaleFactor: 2,
    permissions: ['clipboard-read', 'clipboard-write'],
  })
  const page = await context.newPage()
  await page.addInitScript(() => {
    localStorage.setItem('mc-artifacts-view', 'table')
    localStorage.setItem('mc-artifacts-pinned-only', '0')
  })
  await stubDashboardApi(page, { extra, preserveStorage: true })
  logPageProblems(page)

  await page.goto(base + '/artifacts', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2200)
  await page.getByRole('button', { name: /^table$/i }).click()
  await page.getByRole('button', { name: /^updated$/i }).click()
  await page.getByRole('button', { name: /^updated$/i }).click()
  await page.reload({ waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(1200)
  const updatedHeader = page.getByRole('button', { name: /^updated$/i }).locator('xpath=..')
  if (await updatedHeader.getAttribute('aria-sort') !== 'descending') {
    throw new Error('Updated descending sort did not survive reload')
  }
  const tableClip = { x: 0, y: 0, width: 1500, height: 640 }
  await page.screenshot({ path: `${OUT}/${PREFIX}-01-table-sort-persisted.png`, clip: tableClip })
  console.log('wrote', `${OUT}/${PREFIX}-01-table-sort-persisted.png`)

  await page.goto(base + '/artifacts/alpha-notes', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2200)
  const detailClip = { x: 0, y: 0, width: 1500, height: 700 }
  const copy = page.getByRole('button', { name: 'Copy content' })
  const alignment = await copy.evaluate(button => {
    const toolbar = button.parentElement.getBoundingClientRect()
    const card = button.closest('.flex-1').querySelector('.rounded-xl.border').getBoundingClientRect()
    return { toolbarWidth: toolbar.width, cardWidth: card.width, rightDelta: toolbar.right - card.right }
  })
  if (Math.abs(alignment.rightDelta) > 0.5 || Math.abs(alignment.toolbarWidth - alignment.cardWidth) > 0.5) {
    throw new Error(`medium alignment mismatch: ${JSON.stringify(alignment)}`)
  }
  await page.screenshot({ path: `${OUT}/${PREFIX}-02-detail-medium.png`, clip: detailClip })
  console.log('wrote', `${OUT}/${PREFIX}-02-detail-medium.png`)

  await page.evaluate(() => {
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: () => Promise.reject(new Error('fixture clipboard failure')) },
    })
    document.execCommand = () => { throw new Error('fixture fallback failure') }
  })
  await copy.click()
  await page.getByRole('button', { name: 'Copy failed' }).waitFor()
  await page.waitForTimeout(300)
  await page.screenshot({ path: `${OUT}/${PREFIX}-03-detail-copy-failed.png`, clip: detailClip })
  console.log('wrote', `${OUT}/${PREFIX}-03-detail-copy-failed.png`)

  await page.goto(base + '/artifacts/html-report', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2200)
  if (await page.getByRole('button', { name: /medium width/i }).count()) {
    throw new Error('iframe artifact unexpectedly exposes reading-width control')
  }
  const iframeAlignment = await page.getByRole('button', { name: 'Copy content' }).evaluate(button => {
    const toolbar = button.parentElement.getBoundingClientRect()
    const frame = button.closest('.flex-1').querySelector('iframe').parentElement.getBoundingClientRect()
    return { toolbarWidth: toolbar.width, frameWidth: frame.width, rightDelta: toolbar.right - frame.right }
  })
  if (Math.abs(iframeAlignment.rightDelta) > 0.5 || Math.abs(iframeAlignment.toolbarWidth - iframeAlignment.frameWidth) > 0.5) {
    throw new Error(`iframe alignment mismatch: ${JSON.stringify(iframeAlignment)}`)
  }
  await page.screenshot({ path: `${OUT}/${PREFIX}-04-detail-iframe.png`, clip: detailClip })
  console.log('wrote', `${OUT}/${PREFIX}-04-detail-iframe.png`)

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })

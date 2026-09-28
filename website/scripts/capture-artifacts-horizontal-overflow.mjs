/**
 * Page-level horizontal-overflow probe + screenshot harness.
 *
 * Reproduces "the Artifacts page gained a horizontal scrollbar" on the real
 * built SPA (gateway-free fixture server), then WALKS THE DOM to name the
 * element(s) whose box escapes the viewport so the fix targets the cause, not
 * the symptom. Also sweeps a few other routes so sibling pages with the same
 * class of bug are reported as follow-ups.
 *
 * Every frame asserts the page-level invariant the PR pins:
 *   document.scrollingElement.scrollWidth <= clientWidth
 * and no scroll container inside the shell can scroll horizontally.
 * Set EXPECT_OVERFLOW=1 to capture the BEFORE state without failing.
 *
 * Usage:
 *   node scripts/capture-artifacts-horizontal-overflow.mjs [outDir] [prefix]
 * Env:
 *   REPRO_DIST  serve a different dist (e.g. the installed product bundle)
 *   EXPECT_OVERFLOW=1  do not fail when overflow is found (BEFORE capture)
 *   ROUTES  comma-separated route list (default: /artifacts only)
 */
import { chromium } from 'playwright'
import { mkdirSync, writeFileSync } from 'node:fs'
import { serveDist, DEFAULT_DIST } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/artifacts-horizontal-overflow'
const PREFIX = process.argv[3] || 'after'
const EXPECT_OVERFLOW = process.env.EXPECT_OVERFLOW === '1'
const ROUTES = (process.env.ROUTES || '/artifacts').split(',').map(s => s.trim()).filter(Boolean)
const WIDTHS = [1280, 1440, 1920]
const THEMES = ['light', 'dark']

mkdirSync(OUT, { recursive: true })

const artifact = (slug, name, kind, version, updated, overrides = {}) => ({
  slug,
  name,
  kind,
  source: 'chat',
  session_title: 'Docs session',
  description: 'Column measurement evidence fixture',
  tags: ['docs'],
  version,
  pinned: false,
  created_at: '2026-08-20T10:00:00.000000+00:00',
  updated_at: updated,
  ...overrides,
})

const ARTIFACTS = [
  artifact('cr-queue', 'CR Queue Dashboard', 'widget', 7, '2026-09-06T09:00:00.000000+00:00', { tags: ['cr'] }),
  artifact('design-note-1', 'Design note 1', 'markdown', 2, '2026-09-05T12:00:00.000000+00:00'),
  artifact('pipeline-health', 'Pipeline health', 'html', 3, '2026-09-04T08:00:00.000000+00:00', { tags: ['pipeline'] }),
  artifact('release-plan', 'Release plan Q3', 'markdown', 1, '2026-09-03T16:00:00.000000+00:00', { pinned: true }),
  artifact('logo-draft', 'Logo draft', 'svg', 1, '2026-09-02T14:00:00.000000+00:00'),
]
// Pad past VIRTUALIZE_AT (30): a real library is large enough that the gallery
// virtualizes and OWNS its scroll axis, which is the layout that regressed.
const KINDS = ['markdown', 'widget', 'html', 'svg', 'json', 'text']
const COUNT = Number(process.env.ARTIFACT_COUNT || 36)
for (let i = ARTIFACTS.length; i < COUNT; i++) {
  ARTIFACTS.push(artifact(`note-${i}`, `Design note ${i}`, KINDS[i % KINDS.length], 1 + (i % 4), `2026-08-${String(1 + (i % 28)).padStart(2, '0')}T10:00:00.000000+00:00`))
}
const FOLDERS = [
  { id: 'f1', name: 'Reports', parent_id: null, path: 'Reports', item_count: 2 },
  { id: 'f2', name: 'Design', parent_id: null, path: 'Design', item_count: 1 },
]
const RAW_MD = '# Release notes\n\n- one\n- two\n'
const RAW_HTML = '<main style="padding:32px"><h1>Report</h1></main>'
const byslug = Object.fromEntries(ARTIFACTS.map(a => [a.slug, a]))

const extra = async (path, route) => {
  if (path === '/api/artifacts') return json(route, { artifacts: ARTIFACTS }), true
  if (path === '/api/artifact-folders') return json(route, { folders: FOLDERS }), true
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
  if (rest === '') return json(route, { ...current, content: current.kind === 'html' ? RAW_HTML : RAW_MD }), true
  return false
}

/**
 * Runs in the page. Returns the document-level overflow reading plus every
 * element that (a) is a horizontal scroll container with real overflow, or
 * (b) has a border box whose right edge escapes the viewport while its nearest
 * clipping ancestor does not clip it — i.e. the elements a user would see a
 * horizontal scrollbar FOR.
 */
function probe() {
  const se = document.scrollingElement
  const vw = window.innerWidth
  const clips = new Set(['hidden', 'clip', 'auto', 'scroll'])
  const describe = el => {
    const cls = typeof el.className === 'string' ? el.className.trim().split(/\s+/).slice(0, 12).join(' ') : ''
    const id = el.id ? `#${el.id}` : ''
    const tid = el.getAttribute('data-testid') ? `[data-testid=${el.getAttribute('data-testid')}]` : ''
    return `${el.tagName.toLowerCase()}${id}${tid}${cls ? '.' + cls.replace(/\s+/g, '.') : ''}`
  }
  const isClippedByAncestor = el => {
    for (let p = el.parentElement; p; p = p.parentElement) {
      const ox = getComputedStyle(p).overflowX
      if (clips.has(ox)) return true
    }
    return false
  }
  const scrollers = []
  const escapers = []
  for (const el of document.querySelectorAll('body *')) {
    const cs = getComputedStyle(el)
    if (cs.display === 'none' || cs.position === 'fixed') continue
    const r = el.getBoundingClientRect()
    if (r.width === 0 && r.height === 0) continue
    const ox = cs.overflowX
    // `overflow-x: scroll` paints a bar even with nothing to scroll, so it is
    // reported unconditionally; `auto` only when content really overflows.
    if (ox === 'scroll' || (ox === 'auto' && el.scrollWidth > el.clientWidth + 1)) {
      scrollers.push({ el: describe(el), overflowX: ox, overflowPx: el.scrollWidth - el.clientWidth, width: Math.round(r.width) })
    }
    if (r.right > vw + 0.5 && !isClippedByAncestor(el)) {
      escapers.push({ el: describe(el), right: Math.round(r.right), overshoot: Math.round(r.right - vw), width: Math.round(r.width), depth: (() => { let d = 0; for (let p = el; p; p = p.parentElement) d++; return d })() })
    }
  }
  // Shallowest escaper is the structural cause; deeper ones are its children.
  escapers.sort((a, b) => a.depth - b.depth)
  // Folder-grid geometry: the fix swapped the folder grid's gutter from
  // "-mr-3 wrapper + per-card mr-3" to a grid `gap-x-3`, and the two must
  // produce IDENTICAL card boxes: with N columns over a container of width W
  // each card is (W + 12) / N - 12 wide, on a pitch of (W + 12) / N. The
  // gallery columns are logged alongside for the eye; they are NOT asserted
  // against because the masonry's own 6px vertical scrollbar already shifted
  // them ~2px per column before this change (pre-existing, out of scope).
  const folderCards = [...document.querySelectorAll('[aria-label^="Open folder"]')].map(el => el.getBoundingClientRect())
  const folderGrid = document.querySelector('[aria-label^="Open folder"]')?.parentElement
  let folderGeometry = null
  if (folderGrid && folderCards.length) {
    const W = folderGrid.getBoundingClientRect().width
    const cols = getComputedStyle(folderGrid).gridTemplateColumns.split(' ').length
    const pitch = (W + 12) / cols
    const expectedCard = pitch - 12
    const left0 = folderGrid.getBoundingClientRect().left
    const deviations = folderCards.map((r, i) => Math.max(Math.abs(r.width - expectedCard), Math.abs(r.left - (left0 + (i % cols) * pitch))))
    folderGeometry = { W: Math.round(W), cols, pitch: +pitch.toFixed(2), expectedCard: +expectedCard.toFixed(2), maxDeviationPx: +Math.max(...deviations).toFixed(2) }
  }
  const galleryCards = [...document.querySelectorAll('div.mb-3.mr-3.rounded-lg.bg-card')].map(el => el.getBoundingClientRect()).filter(r => r.width > 0)
  const uniq = xs => [...new Set(xs.map(x => Math.round(x)))].sort((a, b) => a - b)
  const columns = { folderLefts: uniq(folderCards.map(r => r.left)), folderRights: uniq(folderCards.map(r => r.right)), galleryLefts: uniq(galleryCards.map(r => r.left)), galleryRights: uniq(galleryCards.map(r => r.right)) }
  return {
    vw,
    columns,
    folderGeometry,
    docScrollWidth: se.scrollWidth,
    docClientWidth: se.clientWidth,
    docOverflowPx: se.scrollWidth - se.clientWidth,
    bodyOverflowX: getComputedStyle(document.body).overflowX,
    htmlOverflowX: getComputedStyle(document.documentElement).overflowX,
    scrollers,
    escapers: escapers.slice(0, 8),
  }
}

async function main() {
  const dist = process.env.REPRO_DIST || DEFAULT_DIST
  const { srv, base } = await serveDist(dist)
  const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH
  // Playwright hides scrollbars in headless Chromium by default; the bug IS a
  // scrollbar, so keep them visible in the evidence frames.
  const browser = await chromium.launch({ ...(executablePath ? { executablePath } : {}), ignoreDefaultArgs: ['--hide-scrollbars'] })
  const report = []
  let failures = 0
  try {
    for (const route of ROUTES) {
      for (const theme of THEMES) {
        for (const width of WIDTHS) {
          const context = await browser.newContext({ viewport: { width, height: 900 }, deviceScaleFactor: 1 })
          const page = await context.newPage()
          await stubDashboardApi(page, { extra, theme, localStorageEntries: { 'mc-artifacts-view': 'gallery', 'mc-artifacts-pinned-only': '0' } })
          logPageProblems(page)
          await page.goto(base + route, { waitUntil: 'domcontentloaded' })
          await page.waitForTimeout(2200)
          const r = await page.evaluate(probe)
          const slug = route.replace(/\W+/g, '-').replace(/^-|-$/g, '') || 'root'
          const file = `${OUT}/${PREFIX}-${slug}-${theme}-${width}.png`
          await page.screenshot({ path: file })
          const overflowing = r.docOverflowPx > 0 || r.scrollers.length > 0 || r.escapers.length > 0
          const line = `${route} ${theme} ${width}px: doc scrollWidth=${r.docScrollWidth} clientWidth=${r.docClientWidth} (+${r.docOverflowPx}px) html.overflow-x=${r.htmlOverflowX} body.overflow-x=${r.bodyOverflowX}`
          console.log(line)
          for (const s of r.scrollers) console.log(`  scroller  overflow-x=${s.overflowX} +${s.overflowPx}px  ${s.el}`)
          for (const e of r.escapers) console.log(`  escapes   right=${e.right} (+${e.overshoot}px) w=${e.width}  ${e.el}`)
          console.log(`  columns   folders L=${r.columns.folderLefts} R=${r.columns.folderRights} | gallery L=${r.columns.galleryLefts} R=${r.columns.galleryRights}`)
          if (r.folderGeometry) {
            console.log(`  geometry  folder grid W=${r.folderGeometry.W} cols=${r.folderGeometry.cols} pitch=${r.folderGeometry.pitch} card=${r.folderGeometry.expectedCard} maxDeviation=${r.folderGeometry.maxDeviationPx}px`)
            if (r.folderGeometry.maxDeviationPx > 1) {
              console.error('  GEOMETRY: folder cards left the (W+12)/N pitch the margin grid had')
              failures++
            }
          }
          report.push({ route, theme, width, ...r, screenshot: file })
          if (overflowing) failures++
          await context.close()
        }
      }
    }
  } finally {
    await browser.close()
    srv.close()
  }
  writeFileSync(`${OUT}/${PREFIX}-overflow-report.json`, JSON.stringify(report, null, 2))
  console.log(`wrote ${OUT}/${PREFIX}-overflow-report.json`)
  if (failures && !EXPECT_OVERFLOW) {
    console.error(`FAIL: ${failures} frame(s) overflow horizontally`)
    process.exit(1)
  }
  if (!failures && EXPECT_OVERFLOW) {
    console.error('NOTE: EXPECT_OVERFLOW=1 but no overflow was found')
  }
}

main().catch(err => { console.error(err); process.exit(1) })

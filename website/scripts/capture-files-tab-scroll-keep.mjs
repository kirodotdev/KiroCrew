/**
 * Screenshot harness: the Files tab tree keeps its scroll position across a
 * tab round trip.
 *
 * The REAL built SPA behind the shared static server, every `/api/**` answered
 * from fixtures (the `capture-pierre-files-tab.mjs` pattern). Real Chromium
 * input drives the tree: a mouse wheel over the tree scrolls it, a click on a
 * row opens that file in its own tab, and a click on the Files chip comes
 * back.
 *
 * Scenarios, each a fresh page load:
 *   open-file   wheel the tree down, open a file from it, come back to Files
 *   switch-tab  wheel the tree down, click the other tab's chip, come back
 *   short-end   a short panel, the tree wheeled to its last screenful, then
 *               the same round trip
 *   toggle-below  wheel the tree down, switch to the file tab, collapse a
 *               folder BELOW the Files viewport in that tab's own tree, come back
 *   toggle-above  the same, collapsing a folder ABOVE the Files viewport
 *
 * `ONLY=<scenario>` runs a single scenario.
 *
 * Frames (`<out>/<prefix>-<scenario>-NN-*.png`):
 *   01-before-leave   the tree where the user left it
 *   02-after-return   the tree right after coming back
 *
 * Besides the frames, each scenario logs the scroller's `scrollTop` and top
 * row before leaving and after returning, and the `scrollTop` sampled in
 * every animation frame once the scroller is laid out again. An animation
 * frame callback runs before that frame is painted, so a first sample other
 * than the final offset would mean another position was painted first.
 *
 * Usage: node scripts/capture-files-tab-scroll-keep.mjs <out-dir> [prefix] [dist-dir]
 */
import { chromium } from 'playwright'
import { mkdirSync, writeFileSync } from 'node:fs'
import { serveDist, DEFAULT_DIST } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'

const OUT = process.argv[2] || '../temp-screenshots/files-tab-scroll-keep'
const PREFIX = process.argv[3] || 'after'
const DIST = process.argv[4] || DEFAULT_DIST
const PROJECT = '/home/dev/project'
const SLOT = 'chat-files-scroll'

mkdirSync(OUT, { recursive: true })

const SCENARIOS = ['open-file', 'switch-tab', 'short-end', 'toggle-below', 'toggle-above']
// An unknown `ONLY` would fall through to the plain tab round trip and be
// recorded under the wrong label.
if (process.env.ONLY && !SCENARIOS.includes(process.env.ONLY)) {
  console.error(`Unknown scenario "${process.env.ONLY}". Expected one of: ${SCENARIOS.join(', ')}`)
  process.exit(1)
}

/** Eight folders of twenty files: tall enough that the tree scrolls by many
 *  screens, with names that make the top row legible in a frame. */
const FOLDERS = ['api', 'cli', 'core', 'docs', 'jobs', 'store', 'ui', 'utils']
const TREE_PATHS = ['README.md', 'package.json', ...FOLDERS.flatMap(folder =>
  Array.from({ length: 20 }, (_, i) => `src/${folder}/${folder}-${String(i + 1).padStart(2, '0')}.ts`))]
// Bottom folder first: opening a folder pushes only the rows below it, so
// every folder still to open stays inside the virtualized viewport.
const EXPANDED = ['src', ...FOLDERS.map(folder => `src/${folder}`).reverse()]

const OTHER_PATH = `${PROJECT}/README.md`
const slots = [{
  key: SLOT, title: 'Files scroll', running: false, last_message: 'hi', messages: 2,
  agent: 'kirocrew', memory_mode: 'persistent', project: PROJECT,
  modified: Math.floor(Date.now() / 1000), source_links: [], source_links_total: 0,
}]
const t0 = Math.floor(Date.now() / 1000) - 600
const slotDetail = {
  running: false, has_more: false, total: 2, queue: [],
  messages: [
    { role: 'user', content: 'Walk me through the store folder.', ts: String(t0) },
    { role: 'assistant', content: 'Open any file in the tree and I will explain it.', ts: String(t0 + 30) },
  ],
}

const fileTabFor = path => ({ id: `file:${path}`, kind: 'file', title: path.split('/').pop(), path, slot: SLOT, diffMode: false })

async function main() {
  const { srv, base } = await serveDist(DIST)
  const browser = await chromium.launch({ executablePath: chromiumExecutable() })
  const results = []
  try {
    for (const scenario of (process.env.ONLY ? [process.env.ONLY] : SCENARIOS)) {
      results.push(await run(browser, base, scenario))
    }
  } finally {
    await browser.close()
    srv.close()
  }
  writeFileSync(`${OUT}/${PREFIX}-results.json`, JSON.stringify(results, null, 2))
  console.log(JSON.stringify(results, null, 2))
}

async function run(browser, base, scenario) {
  const short = scenario === 'short-end'
  const context = await browser.newContext({
    viewport: { width: 1280, height: short ? 560 : 820 },
    deviceScaleFactor: 1,
    recordVideo: { dir: `${OUT}/video-${PREFIX}-${scenario}`, size: { width: 1280, height: short ? 560 : 820 } },
  })
  const page = await context.newPage()
  const extra = async (path, route) => {
    const q = new URL(route.request().url()).searchParams.get('path') || ''
    if (path === '/api/chat/slots') return json(route, slots), true
    if (/^\/api\/chat\/slots\/[^/]+/.test(path)) return json(route, slotDetail), true
    if (path === '/api/project/tree') return json(route, { root: PROJECT, paths: TREE_PATHS, repo: false, truncated: false }), true
    if (path === '/api/project/git') return json(route, { path: PROJECT, repo: false }), true
    if (path === '/api/file-read') {
      return route.fulfill({ status: 200, contentType: 'text/plain; charset=utf-8', body: `// ${q.split('/').pop()}\nexport const value = 1\n` }), true
    }
    if (path === '/api/file-diff') return json(route, { diff: '', original: '', status: 'clean' }), true
    if (path === '/api/recent-projects') return json(route, { dirs: [PROJECT] }), true
    return false
  }
  await stubDashboardApi(page, { slots, extra })
  logPageProblems(page)

  // The expansion memory's storage shape is internal to the tree, so the
  // folders are opened by clicking them below rather than seeded here.
  const tabs = [{ id: 'files', kind: 'files', title: 'Files' }, fileTabFor(OTHER_PATH)]
  await page.addInitScript(([slot, tabsJson]) => {
    localStorage.clear()
    localStorage.setItem('mc-theme', 'dark')
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-active-slot-chat', slot)
    localStorage.setItem('mc-activity-open:' + slot, 'true')
    localStorage.setItem('mc-panel-tabs:' + slot, tabsJson)
    localStorage.setItem('mc-side-panel-width', '560')
    // The file tab shows its own tree beside the document.
    localStorage.setItem('mc-files-rail-open', '1')
    localStorage.setItem('kirocrew:comment-hint-dismissed', '1')
    localStorage.setItem('mc-chat-config', JSON.stringify({ pinLastPrompt: false, streamMode: 'immediate' }))
  }, [SLOT, JSON.stringify({ activeId: 'files', tabs })])
  await page.goto(base + '/?sid=' + encodeURIComponent(SLOT), { waitUntil: 'domcontentloaded' })

  const scroller = page.locator('[data-file-tree-virtualized-scroll="true"]').first()
  await scroller.waitFor({ state: 'visible', timeout: 20000 })
  for (const folder of EXPANDED) {
    const row = page.locator(`[data-item-path="${folder}/"]`).first()
    await row.waitFor({ state: 'visible', timeout: 10000 })
    if ((await row.getAttribute('aria-expanded')) !== 'true') await row.click()
    await page.waitForTimeout(80)
  }

  const state = () => scroller.evaluate(el => {
    const top = el.getBoundingClientRect().top
    const rows = [...el.querySelectorAll('[data-item-path]')]
      .map(r => ({ path: r.getAttribute('data-item-path'), y: r.getBoundingClientRect().top - top }))
      .filter(r => r.y >= -2)
      .sort((a, b) => a.y - b.y)
    return { scrollTop: Math.round(el.scrollTop), clientHeight: el.clientHeight, scrollHeight: el.scrollHeight, topRow: rows[0]?.path ?? null }
  })

  // Real wheel input over the tree, as a user scrolls it.
  const box = await scroller.boundingBox()
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
  const target = short ? 100000 : 1500
  for (let moved = 0; moved < target; moved += 300) {
    await page.mouse.wheel(0, 300)
    await page.waitForTimeout(40)
    if (short) {
      const s = await state()
      if (s.scrollTop + s.clientHeight >= s.scrollHeight - 1) break
    }
  }
  await page.waitForTimeout(400)
  const before = await state()
  await page.screenshot({ path: `${OUT}/${PREFIX}-${scenario}-01-before-leave.png` })

  if (scenario === 'open-file') {
    // A row in the lower half of the view, so the click does not scroll it.
    const rowPath = await scroller.evaluate(el => {
      const mid = el.getBoundingClientRect().top + el.clientHeight / 2
      return [...el.querySelectorAll('[data-item-path]')]
        .find(r => r.getBoundingClientRect().top > mid && !r.getAttribute('data-item-path').endsWith('/'))
        ?.getAttribute('data-item-path')
    })
    await page.locator(`[data-item-path="${rowPath}"]`).first().click()
  } else {
    await page.getByRole('tab', { name: /README\.md/ }).first().click()
  }
  await page.waitForTimeout(700)

  // The file tab's own tree shares the project's expansion memory. Collapse
  // one folder there: `src/utils` sits below the Files viewport, `src/api`
  // above it.
  let toggled = null
  if (scenario.startsWith('toggle-')) {
    toggled = scenario === 'toggle-below' ? 'src/utils/' : 'src/api/'
    const rail = page.locator('[data-file-tree-virtualized-scroll="true"]:visible').first()
    await rail.waitFor({ state: 'visible', timeout: 10000 })
    const railBox = await rail.boundingBox()
    await page.mouse.move(railBox.x + railBox.width / 2, railBox.y + railBox.height / 2)
    const row = page.locator(`[data-item-path="${toggled}"]:visible`).first()
    for (let i = 0; i < 40 && !(await row.isVisible()); i++) {
      await page.mouse.wheel(0, 300)
      await page.waitForTimeout(40)
    }
    await row.click()
    await page.waitForTimeout(300)
    // A click that lands while the wheel-scrolled list is still settling only
    // focuses the row. The tree's own key then collapses the focused folder.
    if ((await row.getAttribute('aria-expanded')) === 'true') {
      await page.keyboard.press('ArrowLeft')
      await page.waitForTimeout(300)
    }
    toggled = { path: toggled, expandedAfterClick: await row.getAttribute('aria-expanded') }
  }

  // Whether the tree stayed in the page while the other tab was shown.
  const keptWhileHidden = await page.evaluate(() => !!document.querySelector('file-tree-container'))
  // Sample scrollTop in every animation frame from the moment the tree's
  // scroller is laid out again; the sampling starts before the Files chip
  // click. A scroller with no layout box (hidden) is skipped, since its
  // scrollTop reads 0 there whatever it will be once shown.
  await page.evaluate(() => {
    window.__scrollSamples = []
    const tick = () => {
      const el = document.querySelector('file-tree-container')?.shadowRoot?.querySelector('[data-file-tree-virtualized-scroll="true"]')
      if (el && el.getClientRects().length > 0) window.__scrollSamples.push(Math.round(el.scrollTop))
      if (window.__scrollSamples.length < 20) requestAnimationFrame(tick)
    }
    requestAnimationFrame(tick)
  })
  await page.getByRole('tab', { name: /^Files/ }).first().click()
  await scroller.waitFor({ state: 'visible', timeout: 10000 })
  await page.waitForTimeout(900)
  const after = await state()
  // The toggled folder as the Files tree shows it after the sync.
  const filesToggled = toggled
    ? await scroller.evaluate((el, p) => el.querySelector(`[data-item-path="${p}"]`)?.getAttribute('aria-expanded') ?? 'not rendered', toggled.path)
    : null
  const samples = await page.evaluate(() => window.__scrollSamples)
  await page.screenshot({ path: `${OUT}/${PREFIX}-${scenario}-02-after-return.png` })
  await context.close()
  return { prefix: PREFIX, scenario, keptWhileHidden, toggled, filesToggled, before, after, firstFrameScrollTops: samples }
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})

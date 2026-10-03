/**
 * Evidence harness for issue #14557 — in the "N files changed" card a row's
 * `-N +N` count must never draw under its diffstat cells, and a row for a
 * Windows `C:\…` or UNC path must show the basename, not the whole path.
 *
 * Runs the REAL built SPA behind the shared in-process static server and
 * answers every /api/** call from fixtures (gateway-free). The client code is
 * unmodified: `FileChangeChips` renders `meta.file_changes`, and the fixture's
 * rows are the shapes the issue was reported with — a Windows path under a
 * deep `.kiro\crew` tree carrying a `-965 +1032` count, a UNC path, short
 * counts, a POSIX name with a backslash in it, and an artifact-badged row.
 *
 * For every row it MEASURES, with getBoundingClientRect:
 *   overlap  = diffstat cells' right edge − first count span's left edge
 *              (> 0 means the count is drawn under the cells)
 *   name     = the text the row shows for the file
 * and prints one line per row per viewport, then writes element screenshots.
 *
 * Point it at the base build (`FCC_DIST=<dist>`) for the BEFORE frames and
 * at website/dist for the AFTER frames; the out dir names the run.
 *
 * Usage: FCC_DIST=/path/to/dist node scripts/capture-diff-card-count-overlap.mjs [outDir] [label]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { serveDist, DEFAULT_DIST } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'

const OUT = process.argv[2] || '../temp-screenshots/diff-card-count-overlap'
const LABEL = process.argv[3] || 'after'
const DIST = process.env.FCC_DIST || DEFAULT_DIST
const PROJECT = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
/** Hard ceiling for a PR-attached PNG, on BOTH edges. */
const MAX_EDGE = 2000

mkdirSync(OUT, { recursive: true })

const lines = (prefix, n) => Array.from({ length: n }, (_, i) => `${prefix} line ${i}`).join('\n')

const WIN_HOME = 'C:\\Users\\reporter\\.kiro\\crew\\workspace\\kirocrew-github-autofix'
const ROWS = [
  // The reported row: a deep Windows path with a count wider than 8ch.
  { path: `${WIN_HOME}\\release-notes.md`, before: lines('old', 965), after: lines('new', 1032), basename: 'release-notes.md' },
  { path: `${WIN_HOME}\\src\\overview.ts`, before: lines('a', 2), after: lines('a', 2) + '\n' + lines('b', 13), basename: 'overview.ts' },
  { path: `${WIN_HOME}\\src\\pipeline.ts`, before: '', after: lines('x', 36), basename: 'pipeline.ts' },
  { path: '\\\\fileserver\\share\\team\\summary.md', before: lines('q', 2), after: lines('q', 1) + '\n' + lines('z', 13), basename: 'summary.md' },
  // Artifact-badged row with a wide count: badge + cells + count all compete.
  { path: `${WIN_HOME}\\docs\\design.md`, before: lines('d', 400), after: lines('e', 1200), basename: 'design.md', artifact: true },
  // POSIX name that legitimately contains a backslash: must stay whole.
  { path: '/srv/projects/notes/odd\\name.txt', before: 'a', after: 'a\nb', basename: 'odd\\name.txt' },
]
const FILE_CHANGES = ROWS.map(({ path, before, after }) => ({ path, before, after }))

const t0 = Math.floor(Date.now() / 1000) - 900
const SLOT_KEY = 'chat-diff-card-count-overlap'

const messages = [
  { role: 'user', content: 'Write the release notes and tidy the pipeline module.', ts: String(t0) },
  {
    role: 'assistant',
    ts: String(t0 + 120),
    content: 'Done — the release notes were rewritten and the pipeline module tidied.',
    meta: { file_changes: FILE_CHANGES },
  },
]

const slots = [{
  key: SLOT_KEY,
  title: 'Diff card count overlap',
  running: false,
  last_message: 'Diff card count overlap',
  messages: messages.length,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

/** PNG width/height straight out of the IHDR chunk — no image dependency. */
function pngSize(path) {
  const b = readFileSync(path)
  return { w: b.readUInt32BE(16), h: b.readUInt32BE(20) }
}

async function main() {
  const { srv, base } = await serveDist(DIST)
  const executablePath = chromiumExecutable()
  console.log('dist:', DIST)
  console.log('chromium:', executablePath || '(playwright default)')
  const browser = await chromium.launch({ executablePath })
  const wrote = []
  let worst = -Infinity
  const names = []

  for (const width of [1440, 900, 640]) {
    const context = await browser.newContext({ viewport: { width, height: 900 }, deviceScaleFactor: 2 })
    const page = await context.newPage()

    const extra = async (path, route) => {
      if (path === '/api/chat/slots') return json(route, slots), true
      if (/^\/api\/chat\/slots\/[^/]+/.test(path)) {
        return json(route, { running: false, has_more: false, total: messages.length, queue: [], messages }), true
      }
      if (path === '/api/file-read') return route.fulfill({ status: 200, body: '' }), true
      if (path === '/api/artifacts/session-docs') {
        return json(route, { docs: ROWS.filter(r => r.artifact).map(r => ({ path: r.path })) }), true
      }
      if (path === '/api/recent-projects') return json(route, { dirs: [PROJECT] }), true
      return false
    }

    await stubDashboardApi(page, {
      slots,
      extra,
      localStorageEntries: {
        'mc-active-slot-chat': SLOT_KEY,
        'mc-chat-config': JSON.stringify({ pinLastPrompt: false, fileChipStyle: 'expanded', streamMode: 'immediate' }),
      },
    })
    logPageProblems(page)

    await page.goto(base + '/?sid=' + encodeURIComponent(SLOT_KEY), { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2600)
    await page.keyboard.press('Escape')
    const close = page.locator('[aria-label="Close"]')
    if (await close.count()) await close.first().click().catch(() => {})
    await page.waitForTimeout(400)

    const card = page.locator('div.ft-block-reveal:has([data-testid^="fcc-row-"])').first()
    await card.waitFor({ state: 'visible', timeout: 15000 })
    // Every row, including the ones behind "Show N more".
    const more = card.getByRole('button', { name: /^Show \d+ more$/ })
    if (await more.count()) { await more.click(); await page.waitForTimeout(400) }

    const measured = await page.evaluate(() => {
      const out = []
      for (const header of document.querySelectorAll('[data-testid^="fcc-header-"]')) {
        const path = header.getAttribute('data-testid').slice('fcc-header-'.length)
        const name = header.querySelector('[data-fcc-filename]')
        const cells = header.querySelectorAll('[data-fcc-secondary-metadata] span[aria-hidden] > span')
        const counts = header.querySelectorAll('.font-mono.text-danger, .font-mono.text-ok')
        const barRight = cells.length ? Math.max(...Array.from(cells, c => c.getBoundingClientRect().right)) : null
        const countLeft = counts.length ? Math.min(...Array.from(counts, c => c.getBoundingClientRect().left)) : null
        const row = header.getBoundingClientRect()
        out.push({
          path,
          name: name ? name.textContent : null,
          rowWidth: Math.round(row.width),
          rowRight: Math.round(row.right),
          nameRight: name ? Math.round(name.getBoundingClientRect().right) : null,
          countText: Array.from(counts, c => c.textContent).join(' '),
          countRight: counts.length ? Math.round(Math.max(...Array.from(counts, c => c.getBoundingClientRect().right))) : null,
          overlap: barRight != null && countLeft != null ? Math.round((barRight - countLeft) * 10) / 10 : null,
        })
      }
      return out
    })
    const cardWidth = Math.round((await card.boundingBox()).width)
    console.log(`\n[${LABEL}] viewport ${width}px, card ${cardWidth}px`)
    for (const m of measured) {
      const fixture = ROWS.find(r => r.path === m.path)
      const nameOk = fixture && m.name === fixture.basename
      names.push({ width, path: m.path, name: m.name, expected: fixture?.basename, ok: nameOk })
      if (m.overlap != null) worst = Math.max(worst, m.overlap)
      console.log(
        `  ${nameOk ? 'name=basename' : 'name=FULL-PATH'}  overlap=${m.overlap == null ? 'n/a' : m.overlap + 'px'}` +
        `  count="${m.countText}"  shown="${m.name}"  rowW=${m.rowWidth} nameR=${m.nameRight} countR=${m.countRight} rowR=${m.rowRight}`,
      )
    }

    const file = `${OUT}/${LABEL}-${width}.png`
    await card.screenshot({ path: file })
    const { w, h } = pngSize(file)
    const over = w > MAX_EDGE || h > MAX_EDGE
    console.log(`wrote ${file}  ${w}x${h}${over ? '  ⚠️ OVER 2000px' : ''}`)
    wrote.push({ file, w, h, over })
    await context.close()
  }

  await browser.close()
  srv.close()
  const fullPaths = names.filter(n => !n.ok)
  console.log(`\nSUMMARY [${LABEL}]: worst overlap ${worst}px; rows showing the full path: ${fullPaths.length}/${names.length}`)
  if (wrote.some(w => w.over)) { console.error('a frame exceeds the 2000px edge budget'); process.exit(2) }
}

main().catch(err => { console.error(err); process.exit(1) })

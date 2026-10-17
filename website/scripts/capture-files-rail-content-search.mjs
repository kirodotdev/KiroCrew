/**
 * Real-browser evidence for the Files rail's CONTENT search.
 *
 * Drives the isolated capture entry (website/capture/files-rail-content-search.html),
 * which mounts the REAL `FileBrowserRail` with `/api/file-grep` stubbed at the
 * fetch boundary. The mode toggle is CLICKED here rather than seeded, so each
 * frame shows a state a user actually reaches.
 *
 * The unit suite (src/test/FileBrowserRail.test.tsx) pins the same behaviour in
 * happy-dom. This exists for what a DOM assertion cannot carry: whether the row
 * reads at rail width — a path, a `:line` or a location badge, and a preview
 * with the match marked, inside 300-520px.
 *
 * Frames per theme:
 *   01-name-mode        the filename filter, unchanged, for contrast
 *   02-content-results  text hits plus three document hits with their badges
 *   03-content-capped   the status line's other arm: partial, python, 6 skipped
 *   04-content-hint     below the two-character floor -- what a reader sees FIRST
 *   05-content-empty    a search that ran and found nothing
 *   06-content-error    a coded 503, rendered through ErrorNotice
 *   07-content-searching  the request held open: the in-flight word over a blank list
 *
 * The last three exist because a reader meets them when the feature does NOT go
 * well, and none of them had a frame: hint, empty and failed all reviewed as
 * "unknown".
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6837 --strictPort   # in another shell (website/)
 *   node scripts/capture-files-rail-content-search.mjs http://127.0.0.1:6837 ../temp-screenshots/files-rail-content-search
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6837'
const OUT = process.argv[3] || '../temp-screenshots/files-rail-content-search'
mkdirSync(OUT, { recursive: true })

const VIEWPORT = { width: 520, height: 760 }

// mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, which is
// older than the system Mesa needs; children inherit it, so scrub it here.
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0

/** Every frame this run produces: [file suffix, fixture arm, what to do]. */
const FRAMES = [
  ['01-name-mode', 'ok', 'name'],
  ['02-content-results', 'ok', 'content'],
  ['03-content-capped', 'capped', 'content'],
  ['04-content-hint', 'ok', 'hint'],
  ['05-content-empty', 'empty', 'content'],
  ['06-content-error', 'error', 'content'],
  ['07-content-searching', 'searching', 'content'],
]

for (const theme of ['light', 'dark']) {
  for (const [name, arm, mode] of FRAMES) {
    const page = await browser.newPage({ viewport: VIEWPORT })
    page.on('pageerror', e => { console.error(`[${theme}/${name}] pageerror:`, e.message); failures++ })
    await page.goto(
      `${BASE}/capture/files-rail-content-search.html?theme=${theme}&arm=${arm}`,
      { waitUntil: 'networkidle' },
    )

    let observed = ''
    if (mode === 'hint') {
      // One character: under the floor, so no request is made at all. This is the
      // first thing a reader sees after switching to Content, and it had no frame.
      await page.getByLabel('Search file contents').click()
      await page.getByLabel('Search in files…').fill('r')
      await page.waitForTimeout(400)
    } else if (mode === 'content') {
      await page.getByLabel('Search file contents').click()
      await page.getByLabel('Search in files…').fill('rate limit')
      if (arm === 'error') {
        await page.waitForSelector('[data-testid="file-grep-error"]', { timeout: 15000 })
      } else if (arm === 'searching') {
        // The stub never answers, so the status row is the in-flight word and
        // stays that way; wait for it to appear rather than for it to settle.
        await page.waitForFunction(
          () => (document.querySelector('[data-testid="file-grep-status"]')?.textContent || '')
            .includes('Searching'),
          undefined,
          { timeout: 15000 },
        )
        await page.waitForTimeout(400)
      } else if (arm === 'empty') {
        await page.waitForFunction(
          () => !document.querySelector('[data-testid="file-grep-status"]')
            || !(document.querySelector('[data-testid="file-grep-status"]')?.textContent || '')
              .includes('Search'),
          undefined,
          { timeout: 15000 },
        )
        await page.waitForTimeout(250)
      } else {
        // Wait for the rail's own settled shape rather than a fixed sleep: the
        // status line is the last thing to resolve.
        await page.waitForFunction(
          () => (document.querySelector('[data-testid="file-grep-status"]')?.textContent || '')
            .includes('result'),
          undefined,
          { timeout: 15000 },
        )
      }
      observed = (await page.getByTestId('file-grep-status').textContent().catch(() => '')) || ''
    } else {
      // Name mode must still be the tree, not a hit list.
      await page.getByLabel('Filter files…').fill('limits')
      await page.waitForTimeout(300)
    }
    await page.waitForTimeout(150)

    // The palette is asserted, not assumed: a filename claiming a theme the
    // frame does not carry is exactly the failure this catches.
    const applied = await page.evaluate(() => document.documentElement.getAttribute('data-theme'))
    const themeOk = applied === (theme === 'light' ? 'kiro-light' : 'kiro-dark')
    await page.screenshot({ path: `${OUT}/${theme}-${name}.png` })

    // The badges are the point of the document pass, so their presence is
    // asserted rather than left to a reader of the image. Matched as whole
    // element text, not against the row: a row concatenates the path and the
    // badge with no separator, so `docs/deck.pptx` + `slide 7` reads as
    // `pptxslide 7` and any word-boundary pattern over the row finds nothing.
    const badges = await page.evaluate(() =>
      [...document.querySelectorAll('span')]
        .map(el => (el.textContent || '').trim())
        .filter(text => /^(slide \d+|\S+ · row \d+)$/.test(text)).length)
    // Every fixture preview contains the query, so every row must show a marked
    // run. A row without one is what made the reader read code hits and document
    // hits as two different kinds of "match".
    const marks = await page.evaluate(() => document.querySelectorAll('mark').length)

    let ok = themeOk
    if (mode === 'hint') {
      // No request may have been made, and the hint must be the visible answer.
      ok = ok && badges === 0 && marks === 0
        && !(await page.locator('[data-testid="file-grep-status"]').count())
    } else if (arm === 'error') {
      // The failure renders through the shared ErrorNotice with the agent
      // hand-off, not as a hand-rolled red line.
      ok = ok && (await page.locator('[data-testid="file-grep-error"]').count()) === 1
    } else if (arm === 'searching') {
      // Still in flight at capture time: the word is up, no row and no count has
      // arrived, and nothing claims the search ran.
      ok = ok && badges === 0 && marks === 0 && observed.includes('Searching')
        && !observed.includes('result')
        && (await page.getByText('No results').count()) === 0
    } else if (arm === 'empty') {
      // "ran and found nothing" must not look like "has not run": the count is
      // stated (0 results) AND the empty state names it, which together are what
      // tell a reader the search happened.
      const noMatches = await page.getByText('No results').count()
      ok = ok && badges === 0 && marks === 0 && observed.includes('0') && noMatches === 1
    } else if (mode === 'content' && arm === 'ok') {
      // The engine name is NOT in the status line any more: it read as "the
      // search was narrowed to Python files". It lives in the row's tooltip.
      ok = ok && badges === 2 && !observed.includes('partial') && !observed.includes('rg')
    } else if (mode === 'content' && arm === 'capped') {
      // "partial", the same word the sentence below the row uses -- not a second
      // word for one event, which is what made the reader stop and compare them.
      ok = ok && observed.includes('partial') && observed.includes('not searched')
    } else if (mode === 'name') {
      ok = ok && (await page.getByLabel('Search file names').getAttribute('aria-pressed')) === 'true'
    }
    if (mode === 'content' && (arm === 'ok' || arm === 'capped')) {
      ok = ok && marks === (arm === 'capped' ? 4 : 7)
    }
    // The visible note that a document hit opens the file from its start: present
    // with the deck and workbook hits (results arm), absent when the partial
    // payload holds only text hits.
    const note = await page.locator('[data-testid="file-grep-doc-note"]').count()
    if (mode === 'content' && (arm === 'ok' || arm === 'capped')) {
      ok = ok && note === (arm === 'capped' ? 0 : 1)
    }
    console.log(`[${theme}/${name}] data-theme=${applied} badges=${badges} marks=${marks} `
      + `status=${JSON.stringify(observed)} => ${ok ? 'OK' : 'FAIL'}`)
    if (!ok) failures++
    await page.close()
  }
}

await browser.close()
if (failures) {
  console.error(`${failures} assertion(s) failed`)
  process.exit(1)
}
console.log(`done - evidence in ${OUT}`)

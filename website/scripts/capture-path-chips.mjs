/**
 * Screenshots of markdown path chips and the folder panel.
 *
 * Drives the ISOLATED capture entry (website/capture/path-chips.html), which
 * mounts MarkdownRenderer and FolderPanel against the real stylesheet and theme
 * tokens, with `fetch` stubbed to answer the path-kind probe using the same
 * `X-Path-Kind` header the real endpoint sends (api_file_read in
 * dashboard/handlers/files.py). The chips therefore classify themselves exactly
 * as they do in production — the stub replaces the backend, not the component.
 *
 * Why not the full SPA: the chips only reach their interesting states inside a
 * rendered assistant turn, which needs the app shell, a live websocket and a
 * seeded session; a half-stubbed shell renders its ERROR BOUNDARY instead, and a
 * screenshot of the wrong thing is worse evidence than none.
 *
 * The chips scene asserts the FULL classification of the sample transcript, so
 * this can never quietly emit a screenshot where a git ref is still clickable.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6807 --strictPort   # in another shell
 *   node scripts/capture-path-chips.mjs http://127.0.0.1:6807 ../temp-screenshots/path-chips
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6807'
const OUT = process.argv[3] || '../temp-screenshots/path-chips'
mkdirSync(OUT, { recursive: true })

const WORKSPACE = '/Demo Workspace'
const PROJECT = `${WORKSPACE}/Product Guide`
const NOTES = `${PROJECT}/release-notes.md`
const DISPATCH = `${PROJECT}/src/overview.md`

/**
 * The classification the chips scene MUST produce, in document order.
 * Anything actionable that should not be, or vice versa, fails the run.
 */
const EXPECTED_KINDS = [
  [PROJECT, 'dir'],
  ['HEAD', 'plain'],
  ['refs/heads/fix/investigation-record-403', 'plain'],
  ['4a72aec5f04d3f44ba8042931226db051242d48a', 'plain'],
  ['origin/main', 'plain'],
  [WORKSPACE, 'dir'],
  [`${PROJECT}/README.md`, 'file'],
  [`${WORKSPACE}/deleted-notes.md`, 'plain'],
]

/**
 * Cited source locations, in document order.
 *
 * Asserts `data-path` and `data-path-line` alongside the kind, because the whole
 * point of this scene is that the chip RESOLVES the path without the `:line`
 * while still DISPLAYING it. A regression that went back to probing the whole
 * token shows up here as `plain`; one that silently dropped the line from the
 * click shows up as a missing `data-path-line`.
 */
const EXPECTED_CITED = [
  ['purpose', 'plain', undefined, undefined],
  [`${DISPATCH}:447`, 'file', DISPATCH, '447'],
  [':493', 'plain', undefined, undefined],
  [`${DISPATCH}:504:12`, 'file', DISPATCH, '504'],
  [DISPATCH.replace('overview.md', 'missing.md') + ':12', 'plain', undefined, undefined],
  [`${NOTES}:10-16`, 'file', NOTES, '10'],
]

/**
 * Unicode paths (issue #6483), in document order. The three paths must
 * classify as files (rooted CJK, NFD-decomposed accented, home-relative
 * Devanagari) and the two slash-separated prose spans must stay plain — so
 * this can never quietly emit a screenshot where prose became clickable or a
 * Unicode path stayed inert.
 */
const EXPECTED_UNICODE = [
  [`${PROJECT}/产品文档/发布说明.md`, 'file'],
  [`${PROJECT}/cafe\u0301-menu\u0308/notes.md`, 'file'],
  ['~/दस्तावेज़/रिपोर्ट.md', 'file'],
  ['要么这样/要么那样', 'plain'],
  ['и/или', 'plain'],
]

const SCENES = [
  { scene: 'chips', marker: 'code[data-path-kind="dir"]', note: 'directory chip resolved; git refs inert' },
  { scene: 'unicode', marker: 'code[data-path-kind="file"]', note: 'Unicode paths (CJK/NFD/Devanagari) classify; prose stays plain' },
  { scene: 'cited', marker: 'code[data-path-line="447"]', note: 'file:line chips live; bare :line inert' },
  // Waits on the DECORATION, not just the editor: the marker is the highlight
  // itself, so a reveal that scrolled but failed to paint fails the run.
  { scene: 'reveal', marker: '.mc-line-reveal', note: 'panel scrolled to line 447 and flashed it' },
  // The marker only proves SOME line was painted; the assertion block below counts
  // the painted lines, which is what would catch a first-line-only reveal.
  { scene: 'range', marker: '.mc-line-reveal', note: 'panel revealed the whole 10-16 span' },
  { scene: 'folder', marker: '[role="treeitem"]', note: 'project folder tab renders the shared workspace tree' },
  { scene: 'folder-flow', marker: '[role="tablist"]', note: 'tree opens a separate file tab and preserves expansion' },
  { scene: 'markdown-link', marker: 'a[href*="release-notes.md"]', note: 'Markdown file link opened the file panel' },
]

const requestedScenes = new Set(
  (process.env.CAPTURE_SCENES || '').split(',').map(s => s.trim()).filter(Boolean),
)
const selectedScenes = requestedScenes.size
  ? SCENES.filter(({ scene }) => requestedScenes.has(scene))
  : SCENES
if (requestedScenes.size && selectedScenes.length !== requestedScenes.size) {
  const known = new Set(SCENES.map(({ scene }) => scene))
  const unknown = [...requestedScenes].filter(scene => !known.has(scene))
  throw new Error(`Unknown CAPTURE_SCENES: ${unknown.join(', ')}`)
}

const run = async () => {
  const browser = await chromium.launch(
    process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
      ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE }
      : undefined,
  )
  let failed = 0
  for (const theme of ['dark', 'light']) {
    for (const { scene, marker, note } of selectedScenes) {
      const ctx = await browser.newContext({
        viewport: { width: 900, height: 500 },
        deviceScaleFactor: 2,
        colorScheme: theme,
      })
      const page = await ctx.newPage()
      const errors = []
      page.on('pageerror', e => errors.push(e.message))
      await page.goto(`${BASE}/capture/path-chips.html?scene=${scene}&theme=${theme}`, {
        waitUntil: 'networkidle',
      })
      try {
        await page.waitForSelector('[data-capture-root]', { timeout: 15000 })
        await page.waitForSelector(marker, { timeout: 10000 })
      } catch {
        console.error(
          `  FAIL ${theme}/${scene}: ${marker} never rendered` +
            (errors.length ? ` (${errors[0]})` : ''),
        )
        failed += 1
        await ctx.close()
        continue
      }
      if (scene === 'range') {
        // Monaco paints one whole-line decoration node per visible line, so a
        // `:10-16` span must render 7 of them. A reveal that centred line 10 and
        // dropped the rest would render 1 and fail here.
        const painted = await page.$$eval('.mc-line-reveal', els => els.length)
        if (painted !== 7) {
          console.error(`  FAIL ${theme}/${scene}: expected 7 painted lines for :10-16, saw ${painted}`)
          failed += 1
          await ctx.close()
          continue
        }
      }
      if (scene === 'markdown-link') {
        await page.locator('a[href*="release-notes.md"]').click()
        await page.waitForSelector('.mc-line-reveal', { timeout: 10000 })
        if (await page.getByLabel('Opened file panel').count() !== 1) {
          console.error(`  FAIL ${theme}/${scene}: file panel did not open`)
          failed += 1
          await ctx.close()
          continue
        }
      }
      if (scene === 'chips' || scene === 'cited' || scene === 'unicode') {
        const cited = scene === 'cited'
        const expected = cited ? EXPECTED_CITED : scene === 'unicode' ? EXPECTED_UNICODE : EXPECTED_KINDS
        const actual = await page.$$eval('code', (els, withPath) =>
          els.map(e => withPath
            ? [e.textContent, e.dataset.pathKind ?? 'plain', e.dataset.path, e.dataset.pathLine]
            : [e.textContent, e.dataset.pathKind ?? 'plain']), cited)
        if (JSON.stringify(actual) !== JSON.stringify(expected)) {
          console.error(`  FAIL ${theme}/${scene}: classification drifted`)
          console.error(`    expected ${JSON.stringify(expected)}`)
          console.error(`    actual   ${JSON.stringify(actual)}`)
          failed += 1
          await ctx.close()
          continue
        }
      }
      const target = await page.$('[data-capture-root]')
      if (scene === 'folder') {
        const src = page.getByRole('treeitem', { name: 'src' })
        await src.waitFor()
        await page.getByText('Parent folder').waitFor()
        await target.screenshot({ path: `${OUT}/${theme}-${scene}-collapsed.png` })
        await src.click()
        await page.getByRole('treeitem', { name: 'overview.md' }).waitFor()
        await target.screenshot({ path: `${OUT}/${theme}-${scene}-expanded.png` })
        const search = page.getByLabel('Search files')
        await search.fill('head')
        await page.getByText('includes subfolders').waitFor()
        await target.screenshot({ path: `${OUT}/${theme}-${scene}-search.png` })
        await search.fill('')
        console.log(`  ${theme}/${scene} -> ${note}; collapsed + expanded + search` )
      } else if (scene === 'folder-flow') {
        const src = page.getByRole('treeitem', { name: 'src' })
        await src.waitFor()
        await page.getByText('Parent folder').waitFor()
        await target.screenshot({ path: `${OUT}/${theme}-${scene}-initial.png` })
        await src.click()
        const overview = page.getByRole('treeitem', { name: 'overview.md' })
        await overview.waitFor()
        await target.screenshot({ path: `${OUT}/${theme}-${scene}-expanded.png` })
        await overview.click()
        await page.getByRole('tab', { name: 'overview.md' }).waitFor()
        await page.getByText('This file opened in a separate tab.').waitFor()
        await target.screenshot({ path: `${OUT}/${theme}-${scene}-file-tab.png` })
        await page.getByRole('tab', { name: 'Project tree' }).click()
        if (await src.getAttribute('aria-expanded') !== 'true') {
          console.error(`  FAIL ${theme}/${scene}: tree expansion state was not preserved`)
          failed += 1
          await ctx.close()
          continue
        }
        await target.screenshot({ path: `${OUT}/${theme}-${scene}-returned.png` })
        console.log(`  ${theme}/${scene} -> ${note}; file tab + returned tree`)
      } else {
        await target.screenshot({ path: `${OUT}/${theme}-${scene}.png` })
        console.log(`  ${theme}/${scene} -> ${note}`)
      }
      await ctx.close()
    }
  }
  await browser.close()
  if (failed) {
    console.error(`${failed} scene(s) failed`)
    process.exit(1)
  }
}

run()

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

const SCENES = [
  { scene: 'chips', marker: 'code[data-path-kind="dir"]', note: 'directory chip resolved; git refs inert' },
  { scene: 'cited', marker: 'code[data-path-line="447"]', note: 'file:line chips live; bare :line inert' },
  // Waits on the DECORATION, not just the editor: the marker is the highlight
  // itself, so a reveal that scrolled but failed to paint fails the run.
  { scene: 'reveal', marker: '.mc-line-reveal', note: 'panel scrolled to line 447 and flashed it' },
  // The marker only proves SOME line was painted; the assertion block below counts
  // the painted lines, which is what would catch a first-line-only reveal.
  { scene: 'range', marker: '.mc-line-reveal', note: 'panel revealed the whole 10-16 span' },
  { scene: 'folder', marker: 'text=website', note: 'folder tab body lists dirs then files' },
  { scene: 'markdown-link', marker: 'a[href*="release-notes.md"]', note: 'Markdown file link opened the file panel' },
]

const run = async () => {
  const browser = await chromium.launch(
    process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
      ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE }
      : undefined,
  )
  let failed = 0
  for (const theme of ['dark', 'light']) {
    for (const { scene, marker, note } of SCENES) {
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
      if (scene === 'chips' || scene === 'cited') {
        const cited = scene === 'cited'
        const expected = cited ? EXPECTED_CITED : EXPECTED_KINDS
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
      await target.screenshot({ path: `${OUT}/${theme}-${scene}.png` })
      console.log(`  ${theme}/${scene} -> ${note}`)
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

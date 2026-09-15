/**
 * Screenshots of unfurled link previews — favicon legibility per theme.
 *
 * Drives the ISOLATED capture entry (website/capture/link-preview.html), which
 * mounts the real MarkdownRenderer against the real stylesheet and theme tokens
 * with `fetch` stubbed at the same `/api/link-meta` seam the real hook uses. The
 * icons therefore measure and plate themselves exactly as they do in production
 * — the stub replaces the backend, not the component.
 *
 * Why not the full SPA: a chip only exists inside a rendered assistant turn,
 * which needs the app shell, a live websocket and a seeded session; a
 * half-stubbed shell renders its ERROR BOUNDARY instead, and a screenshot of the
 * wrong thing is worse evidence than none.
 *
 * The run is SELF-CHECKING: it asserts which icons were plated and which variant
 * each one rendered, per theme, so it can never quietly emit a screenshot of the
 * bug it is supposed to prove fixed.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6808 --strictPort   # in another shell
 *   node scripts/capture-link-preview.mjs http://127.0.0.1:6808 ../temp-screenshots/link-preview-favicon
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6808'
const OUT = process.argv[3] || '../temp-screenshots/link-preview-favicon'
mkdirSync(OUT, { recursive: true })

/**
 * Per theme, in document order: whether the icon box got the plate, and which
 * variant its `<img>` is showing.
 *
 * Row 1 is the reported bug — one near-black icon, so a dark theme must plate it
 * and a light theme must leave it alone. Row 2 declares its own dark variant, so
 * the dark theme must render THAT and skip the plate entirely. Row 3 is a
 * mid-tone mark that must come back untouched from both.
 */
const EXPECTED = {
  dark: [
    { plate: true, variant: 'light' },
    { plate: false, variant: 'dark' },
    { plate: false, variant: 'mid' },
  ],
  light: [
    { plate: false, variant: 'light' },
    { plate: false, variant: 'light' },
    { plate: false, variant: 'mid' },
  ],
}

const SCENES = [
  { scene: 'chips', note: 'inline chips: plate, declared variant, and an untouched mid-tone' },
  { scene: 'card', note: 'block card: the same decision at the 32px icon size' },
]

/** Classify an `<img src>` data URI by length, which is stable per fixture. */
const variantOf = (src) => {
  if (!src) return 'none'
  if (src.length > 1200) return 'light' // GitHub's light-mode octocat (958 B)
  if (src.length > 600) return 'dark' // its white counterpart (584 B)
  return 'mid' // the synthetic mid-tone control (213 B)
}

const run = async () => {
  const browser = await chromium.launch()
  let failed = 0
  for (const theme of ['dark', 'light']) {
    for (const { scene, note } of SCENES) {
      const ctx = await browser.newContext({
        viewport: { width: 760, height: scene === 'card' ? 190 : 240 },
        deviceScaleFactor: 2,
        colorScheme: theme,
      })
      const page = await ctx.newPage()
      const errors = []
      page.on('pageerror', (e) => errors.push(e.message))
      await page.goto(`${BASE}/capture/link-preview.html?scene=${scene}&theme=${theme}`, {
        waitUntil: 'networkidle',
      })
      try {
        await page.waitForSelector('[data-capture-root] img', { timeout: 15000 })
      } catch {
        console.error(
          `  FAIL ${theme}/${scene}: no favicon rendered` +
            (errors.length ? ` (${errors[0]})` : ''),
        )
        failed += 1
        await ctx.close()
        continue
      }
      // The decision runs in an effect after the icon decodes, so wait for the
      // measurement to settle rather than photographing the pre-decision frame.
      await page.waitForTimeout(400)

      const seen = await page.$$eval('[data-capture-root] [aria-hidden="true"]', (boxes) =>
        boxes
          .filter((b) => b.querySelector('img'))
          .map((b) => ({
            plate: b.className.split(/\s+/).includes('bg-text'),
            src: b.querySelector('img').getAttribute('src'),
          })),
      )
      const actual = seen.map(({ plate, src }) => ({ plate, src }))
      const expected = scene === 'card' ? EXPECTED[theme].slice(0, 1) : EXPECTED[theme]
      if (actual.length !== expected.length) {
        console.error(
          `  FAIL ${theme}/${scene}: expected ${expected.length} icons, saw ${actual.length}`,
        )
        failed += 1
        await ctx.close()
        continue
      }
      let mismatch = false
      expected.forEach((want, i) => {
        const got = { plate: actual[i].plate, variant: variantOf(actual[i].src) }
        if (got.plate !== want.plate || got.variant !== want.variant) {
          console.error(
            `  FAIL ${theme}/${scene} icon ${i + 1}: expected ` +
              `plate=${want.plate} variant=${want.variant}, saw ` +
              `plate=${got.plate} variant=${got.variant}`,
          )
          mismatch = true
        }
      })
      if (mismatch) {
        failed += 1
        await ctx.close()
        continue
      }

      const file = `${OUT}/${scene}-${theme}.png`
      await page.locator('[data-capture-root]').screenshot({ path: file })
      console.log(`  ok   ${theme}/${scene} -> ${file}  (${note})`)
      await ctx.close()
    }
  }
  await browser.close()
  if (failed) {
    console.error(`\n${failed} capture(s) failed`)
    process.exit(1)
  }
}

run()

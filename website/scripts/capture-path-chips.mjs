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

/**
 * The classification the chips scene MUST produce, in document order.
 * Anything actionable that should not be, or vice versa, fails the run.
 */
const EXPECTED_KINDS = [
  ['/Users/diwm/.kiro/crew/workspace/KiroCrew', 'dir'],
  ['HEAD', 'plain'],
  ['refs/heads/fix/investigation-record-403', 'plain'],
  ['4a72aec5f04d3f44ba8042931226db051242d48a', 'plain'],
  ['origin/main', 'plain'],
  ['/Users/diwm/.kiro/crew', 'dir'],
  ['/Users/diwm/.kiro/crew/workspace/KiroCrew/README.md', 'file'],
  ['/Users/diwm/.kiro/crew/deleted-notes.md', 'plain'],
]

const SCENES = [
  { scene: 'chips', marker: 'code[data-path-kind="dir"]', note: 'directory chip resolved; git refs inert' },
  { scene: 'folder', marker: 'text=website', note: 'folder tab body lists dirs then files' },
]

const run = async () => {
  const browser = await chromium.launch()
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
      if (scene === 'chips') {
        const actual = await page.$$eval('code', els =>
          els.map(e => [e.textContent, e.dataset.pathKind ?? 'plain']))
        if (JSON.stringify(actual) !== JSON.stringify(EXPECTED_KINDS)) {
          console.error(`  FAIL ${theme}/${scene}: classification drifted`)
          console.error(`    expected ${JSON.stringify(EXPECTED_KINDS)}`)
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

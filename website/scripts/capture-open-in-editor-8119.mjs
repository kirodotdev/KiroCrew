/**
 * Screenshot of the chat path chip's right-click menu carrying the new
 * "Open in editor" row (issue #8119). Drives the SAME isolated capture entry
 * as capture-path-chips.mjs (website/capture/path-chips.html), which mounts the
 * real MarkdownRenderer + FilePathMenu against the real stylesheet with the
 * path-kind probe stubbed, so the chip classifies itself exactly as in
 * production.
 *
 * The one thing this harness adds: it injects `window.fileOpenAPI` BEFORE load,
 * because the "Open in editor" row renders only when the desktop shell's
 * preload bridge is present (canOpenFileInEditor). That mirrors the desktop app;
 * a plain browser (no bridge) correctly shows no such row.
 *
 * It ASSERTS the row is present (not just photographs it), so a capture that
 * silently lost the row fails the run rather than emitting misleading evidence.
 *
 * Usage:
 *   ./node_modules/.bin/vite --host 127.0.0.1 --port 6808 --strictPort   # another shell
 *   node scripts/capture-open-in-editor-8119.mjs http://127.0.0.1:6808 ../temp-screenshots/open-in-editor-8119
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6808'
const OUT = process.argv[3] || '../temp-screenshots/open-in-editor-8119'
mkdirSync(OUT, { recursive: true })

// The file chip the entry renders and reports as a file (see path-chips.tsx
// FILES set). README.md is the plain file chip in the default 'chips' scene.
const FILE_CHIP = '/Demo Workspace/Product Guide/README.md'

const run = async () => {
  const browser = await chromium.launch(
    process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
      ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE }
      : undefined,
  )
  let failed = 0
  for (const theme of ['dark', 'light']) {
    const ctx = await browser.newContext({
      viewport: { width: 900, height: 500 },
      deviceScaleFactor: 2,
      colorScheme: theme,
    })
    const page = await ctx.newPage()
    const errors = []
    page.on('pageerror', e => errors.push(e.message))
    // Inject the desktop-shell bridge the row feature-detects, before any app
    // code runs. open() resolves ok so a click in the harness is inert.
    await page.addInitScript(() => {
      window.fileOpenAPI = { open: () => Promise.resolve({ ok: true }) }
    })
    await page.goto(`${BASE}/capture/path-chips.html?scene=chips&theme=${theme}`, {
      waitUntil: 'networkidle',
    })
    try {
      await page.waitForSelector('[data-capture-root]', { timeout: 15000 })
      await page.waitForSelector('code[data-path-kind="file"]', { timeout: 10000 })
    } catch {
      console.error(`  FAIL ${theme}: file chip never rendered${errors.length ? ` (${errors[0]})` : ''}`)
      failed += 1
      await ctx.close()
      continue
    }
    // Right-click the README.md file chip to open its FilePathMenu.
    const chip = page.locator(`code[data-path="${FILE_CHIP}"]`).first()
    await chip.waitFor({ state: 'visible', timeout: 10000 })
    await chip.click({ button: 'right' })
    const menu = page.locator('[role="menu"]').last()
    await menu.waitFor({ state: 'visible', timeout: 10000 })
    await page.waitForTimeout(300)

    const items = (await menu.locator('[role="menuitem"]').allInnerTexts()).map(s => s.trim())
    if (!items.includes('Open in editor')) {
      console.error(`  FAIL ${theme}: "Open in editor" row missing; menu had ${JSON.stringify(items)}`)
      failed += 1
      await ctx.close()
      continue
    }
    await menu.getByText('Open in editor', { exact: true }).hover()
    await page.waitForTimeout(150)

    const box = await menu.boundingBox()
    const x = Math.max(0, box.x - 40)
    const y = Math.max(0, box.y - 40)
    await page.screenshot({
      path: `${OUT}/${theme}-open-in-editor.png`,
      clip: { x, y, width: Math.min(900 - x, box.width + 80), height: Math.min(500 - y, box.height + 70) },
    })
    console.log(`  ${theme} -> menu shows: ${JSON.stringify(items)}`)
    await ctx.close()
  }
  await browser.close()
  if (failed) {
    console.error(`${failed} capture(s) failed`)
    process.exit(1)
  }
  console.log('OK: "Open in editor" row present in both themes')
}

run()

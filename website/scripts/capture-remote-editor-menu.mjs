/**
 * Screenshots of the file-path chip context menu carrying the new
 * "Open in <editor>" row on a REMOTE session (PR #11345 / issue #11338).
 * Drives the isolated capture entry (website/capture/remote-editor-menu.html),
 * which mounts the real MarkdownRenderer + FilePathMenu inside the REAL
 * BrandingProvider with only the transport stubbed, so the row's gate
 * (!directLocal && editor && host) is exercised rather than bypassed.
 *
 * Frames:
 *   file-menu-vscode-dark.png    the config-file chip's menu: "Open in VS Code"
 *   dir-menu-kiro-dark.png       a DIRECTORY chip's menu: "Open in Kiro" —
 *                                evidence the row is deliberately not file-gated
 *                                (a directory opens as a remote workspace)
 *   file-menu-vscode-light.png   light parity of the file-chip frame
 *
 * ASSERTS the row is present in each menu (not just photographs it), so a
 * capture that silently lost the row fails the run rather than emitting
 * misleading evidence.
 *
 * Usage:
 *   ./node_modules/.bin/vite --host 127.0.0.1 --port 6812 --strictPort  # another shell
 *   node scripts/capture-remote-editor-menu.mjs http://127.0.0.1:6812 ../temp-screenshots/remote-editor-menu
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6812'
const OUT = process.argv[3] || '../temp-screenshots/remote-editor-menu'
mkdirSync(OUT, { recursive: true })

const FILE_CHIP = '/home/user/.kiro/crew/config.json'
const DIR_CHIP = '/home/user/.kiro/crew'

// mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, which is
// older than the system Mesa needs; children inherit it, so scrub it here.
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env

/** Right-click `path`'s chip, assert `row` is in the menu, shoot a crop. */
async function shoot(page, { path, kind, row, name }) {
  const chip = page.locator(`code[data-path="${path}"][data-path-kind="${kind}"]`).first()
  await chip.waitFor({ state: 'visible', timeout: 10000 })
  await chip.click({ button: 'right' })
  const menu = page.locator('[role="menu"]').last()
  await menu.waitFor({ state: 'visible', timeout: 10000 })
  await page.waitForTimeout(300)

  const items = (await menu.locator('[role="menuitem"]').allInnerTexts()).map(s => s.trim())
  if (!items.includes(row)) {
    throw new Error(`${name}: "${row}" row missing; menu had ${JSON.stringify(items)}`)
  }
  await menu.getByText(row, { exact: true }).hover()
  await page.waitForTimeout(150)

  const box = await menu.boundingBox()
  const x = Math.max(0, box.x - 48)
  const y = Math.max(0, box.y - 48)
  await page.screenshot({
    path: `${OUT}/${name}.png`,
    clip: { x, y, width: Math.min(760 - x, box.width + 96), height: Math.min(420 - y, box.height + 84) },
  })
  console.log(`wrote ${OUT}/${name}.png — menu: ${JSON.stringify(items)}`)
  await page.keyboard.press('Escape')
  await page.waitForTimeout(150)
}

const browser = await chromium.launch({ env: browserEnv })
try {
  for (const scene of [
    { theme: 'dark', editor: 'vscode', shots: [
      { path: FILE_CHIP, kind: 'file', row: 'Open in VS Code', name: 'file-menu-vscode-dark' },
    ] },
    { theme: 'dark', editor: 'kiro', shots: [
      { path: DIR_CHIP, kind: 'dir', row: 'Open in Kiro', name: 'dir-menu-kiro-dark' },
    ] },
    { theme: 'light', editor: 'vscode', shots: [
      { path: FILE_CHIP, kind: 'file', row: 'Open in VS Code', name: 'file-menu-vscode-light' },
    ] },
  ]) {
    const ctx = await browser.newContext({
      viewport: { width: 760, height: 420 },
      deviceScaleFactor: 2,
      colorScheme: scene.theme,
    })
    const page = await ctx.newPage()
    const errors = []
    page.on('pageerror', e => errors.push(e.message))
    await page.goto(
      `${BASE}/capture/remote-editor-menu.html?theme=${scene.theme}&editor=${scene.editor}`,
      { waitUntil: 'networkidle' },
    )
    await page.waitForSelector('[data-capture-root]', { timeout: 15000 })
    // Both chips must classify before any menu is opened; the row is per-kind.
    await page.waitForSelector(`code[data-path-kind="file"]`, { timeout: 10000 })
    await page.waitForSelector(`code[data-path-kind="dir"]`, { timeout: 10000 })
    for (const shot of scene.shots) await shoot(page, shot)
    if (errors.length) throw new Error(`pageerror: ${errors[0]}`)
    await ctx.close()
  }
  console.log('OK: remote-editor row present in every frame')
} finally {
  await browser.close()
}

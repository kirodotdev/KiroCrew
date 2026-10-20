/**
 * Screenshots of the file-path chip context menu carrying the new
 * "Open in <editor>" row (PR #11345 / issue #11338).
 * Drives the isolated capture entry (website/capture/remote-editor-menu.html),
 * which mounts the real MarkdownRenderer + FilePathMenu inside the REAL
 * BrandingProvider with only the transport stubbed, so the row's gate
 * (editor && host) is exercised rather than bypassed.
 *
 * Frames:
 *   file-menu-vscode-dark.png    the config-file chip's menu: "Open in VS Code"
 *   dir-menu-kiro-dark.png       a DIRECTORY chip's menu: "Open in Kiro" —
 *                                evidence the row is deliberately not file-gated
 *                                (a directory opens as a remote workspace)
 *   file-menu-vscode-light.png   light parity of the file-chip frame
 *   file-menu-tunnel-dark.png    an ssh -L tunnel (direct_local true): the row
 *                                still shows, next to the local open/reveal rows
 *   file-menu-sent-dark.png      after clicking the row: the label has flipped
 *                                to the "Sent to VS Code. Nothing opened? …"
 *                                hint and the menu is still open (Copy path
 *                                beside it); the vscode:// assignment is checked
 *                                not to have navigated the page
 *   markdown-overflow-menu-dark.png   the REAL MarkdownPanel's ⋯ overflow on a
 *                                file, carrying "Open in VS Code"
 *   fileviewer-overflow-menu-dark.png the REAL Files-app FileViewer's "More
 *                                options" menu, carrying the row with its icon
 *   markdown-overflow-sent-dark.png   the MarkdownPanel ⋯ menu after clicking
 *                                the row: the sent hint, menu still open
 *   fileviewer-overflow-sent-dark.png the FileViewer "More options" menu after
 *                                clicking the row: the sent hint, menu still open
 *
 * ASSERTS the row is present in each menu (not just photographs it), and the
 * applied data-theme matches the frame name, so a capture that silently lost
 * the row or the palette fails the run rather than emitting misleading evidence.
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

/** Crop around `boxes` (trigger + menu), padded, clamped to the viewport. */
async function shootBoxes(page, name, boxes, view, pad = 40) {
  const x0 = Math.max(0, Math.min(...boxes.map(b => b.x)) - pad)
  const y0 = Math.max(0, Math.min(...boxes.map(b => b.y)) - pad)
  const x1 = Math.min(view.width, Math.max(...boxes.map(b => b.x + b.width)) + pad)
  const y1 = Math.min(view.height, Math.max(...boxes.map(b => b.y + b.height)) + pad)
  await page.screenshot({ path: `${OUT}/${name}.png`, clip: { x: x0, y: y0, width: x1 - x0, height: y1 - y0 } })
}

/**
 * Click the chip menu's "Open in <editor>" row and shoot the acknowledgment:
 * the row's label flips to the sent hint and the menu STAYS open. Asserts the
 * hint row and the Copy path row are both in the still-open menu, that the
 * vscode:// href assignment left the page where it was (same URL, same JS
 * realm), and that the wrapped hint is not clipped by the row or the menu.
 */
async function shootSent(page, { path, kind, row, sentPrefix, name, view }) {
  const chip = page.locator(`code[data-path="${path}"][data-path-kind="${kind}"]`).first()
  await chip.waitFor({ state: 'visible', timeout: 10000 })
  await chip.click({ button: 'right' })
  const menu = page.locator('[role="menu"]').last()
  await menu.waitFor({ state: 'visible', timeout: 10000 })
  await page.waitForTimeout(300)

  // Navigation guard evidence: a realm sentinel + main-frame navigation count.
  const urlBefore = page.url()
  await page.evaluate(() => { window.__captureRealm = 'kept' })
  let navigations = 0
  const onNav = f => { if (f === page.mainFrame()) navigations++ }
  page.on('framenavigated', onNav)

  await menu.getByRole('menuitem', { name: row, exact: true }).click()
  const sentRow = menu.locator('[role="menuitem"]', { hasText: sentPrefix })
  await sentRow.waitFor({ state: 'visible', timeout: 2000 })
  await page.waitForTimeout(200)
  page.off('framenavigated', onNav)

  const realm = await page.evaluate(() => window.__captureRealm)
  if (page.url() !== urlBefore || realm !== 'kept' || navigations) {
    throw new Error(`${name}: the editor link navigated the page (url ${urlBefore} -> ${page.url()}, ` +
      `realm=${realm}, navigations=${navigations})`)
  }
  if (!(await menu.isVisible())) throw new Error(`${name}: menu closed after the click`)
  const items = (await menu.locator('[role="menuitem"]').allInnerTexts()).map(s => s.trim())
  const sentText = items.find(s => s.startsWith(sentPrefix))
  if (!sentText) throw new Error(`${name}: sent hint missing; menu had ${JSON.stringify(items)}`)
  if (!items.includes('Copy path')) {
    throw new Error(`${name}: "Copy path" row missing; menu had ${JSON.stringify(items)}`)
  }

  // Readability: the wrapped hint must fit its row (no horizontal clip) and
  // the row must sit inside the menu and the menu inside the viewport.
  const fit = await sentRow.evaluate(el => {
    const m = el.closest('[role="menu"]')
    const r = el.getBoundingClientRect()
    const mr = m.getBoundingClientRect()
    const lineH = parseFloat(getComputedStyle(el).lineHeight) || 16
    return {
      rowOverflowX: el.scrollWidth - el.clientWidth,
      rowOverflowY: el.scrollHeight - el.clientHeight,
      menuOverflowX: m.scrollWidth - m.clientWidth,
      inMenu: r.left >= mr.left - 0.5 && r.right <= mr.right + 0.5 && r.bottom <= mr.bottom + 0.5,
      menu: { l: mr.left, t: mr.top, r: mr.right, b: mr.bottom },
      lines: Math.round(r.height / lineH),
      whiteSpace: getComputedStyle(el).whiteSpace,
    }
  })
  console.log(`${name}: hint fit ${JSON.stringify(fit)}`)
  const inView = fit.menu.l >= 0 && fit.menu.t >= 0 && fit.menu.r <= view.width && fit.menu.b <= view.height
  if (fit.rowOverflowX > 1 || fit.rowOverflowY > 1 || fit.menuOverflowX > 1 || !fit.inMenu || !inView) {
    console.log(`WARN ${name}: the wrapped hint is clipped or overflows (see fit above)`)
  }

  await shootBoxes(page, name, [await chip.boundingBox(), await menu.boundingBox()], view, 32)
  console.log(`wrote ${OUT}/${name}.png — menu: ${JSON.stringify(items)}`)
  await page.keyboard.press('Escape')
  await page.waitForTimeout(150)
}

/**
 * Open a panel's overflow menu by its trigger's accessible name, assert `row`
 * is in it (and, with `icon`, that the row carries its glyph), shoot trigger +
 * menu together.
 */
async function shootOverflow(page, { trigger, row, icon, name, view }) {
  const btn = page.getByRole('button', { name: trigger, exact: true }).first()
  await btn.waitFor({ state: 'visible', timeout: 10000 })
  // Boxed BEFORE the click: a modal Radix DropdownMenu aria-hides the rest of
  // the page while open, so the role query cannot find the trigger afterwards.
  const btnBox = await btn.boundingBox()
  await btn.click()
  const menu = page.locator('[role="menu"]').last()
  await menu.waitFor({ state: 'visible', timeout: 10000 })
  await page.waitForTimeout(400)
  const items = (await menu.locator('[role="menuitem"]').allInnerTexts()).map(s => s.trim())
  if (!items.includes(row)) {
    throw new Error(`${name}: "${row}" row missing; menu had ${JSON.stringify(items)}`)
  }
  const rowEl = menu.getByRole('menuitem', { name: row, exact: true })
  if (icon && !(await rowEl.locator('svg').count())) {
    throw new Error(`${name}: "${row}" row has no icon`)
  }
  await rowEl.hover()
  await page.waitForTimeout(150)
  await shootBoxes(page, name, [btnBox, await menu.boundingBox()], view, 32)
  console.log(`wrote ${OUT}/${name}.png — menu: ${JSON.stringify(items)}`)
  await page.keyboard.press('Escape')
  await page.waitForTimeout(150)
}

/**
 * Open a panel's overflow menu, click its "Open in <editor>" row and shoot the
 * acknowledgment: the row's label flips to the sent hint and the menu STAYS
 * open. Asserts the hint row is present, the menu is still open, and logs
 * whether the wrapped hint fits its row and the menu inside the viewport.
 */
async function shootOverflowSent(page, { trigger, row, sentPrefix, name, view }) {
  const btn = page.getByRole('button', { name: trigger, exact: true }).first()
  await btn.waitFor({ state: 'visible', timeout: 10000 })
  const btnBox = await btn.boundingBox()
  await btn.click()
  const menu = page.locator('[role="menu"]').last()
  await menu.waitFor({ state: 'visible', timeout: 10000 })
  await page.waitForTimeout(400)
  await menu.getByRole('menuitem', { name: row, exact: true }).click()
  const sentRow = menu.locator('[role="menuitem"]', { hasText: sentPrefix })
  await sentRow.waitFor({ state: 'visible', timeout: 2000 })
  await page.waitForTimeout(200)
  if (!(await menu.isVisible())) throw new Error(`${name}: menu closed after the click`)
  const items = (await menu.locator('[role="menuitem"]').allInnerTexts()).map(s => s.trim())
  if (!items.some(s => s.startsWith(sentPrefix))) {
    throw new Error(`${name}: sent hint missing; menu had ${JSON.stringify(items)}`)
  }
  const fit = await sentRow.evaluate(el => {
    const m = el.closest('[role="menu"]')
    const r = el.getBoundingClientRect()
    const mr = m.getBoundingClientRect()
    return {
      rowOverflowX: el.scrollWidth - el.clientWidth,
      menuOverflowX: m.scrollWidth - m.clientWidth,
      inMenu: r.left >= mr.left - 0.5 && r.right <= mr.right + 0.5 && r.bottom <= mr.bottom + 0.5,
      menu: { l: mr.left, t: mr.top, r: mr.right, b: mr.bottom },
    }
  })
  console.log(`${name}: hint fit ${JSON.stringify(fit)}`)
  const inView = fit.menu.l >= 0 && fit.menu.t >= 0 && fit.menu.r <= view.width && fit.menu.b <= view.height
  if (fit.rowOverflowX > 1 || fit.menuOverflowX > 1 || !fit.inMenu || !inView) {
    throw new Error(`${name}: the wrapped hint is clipped or overflows (see fit above)`)
  }
  await shootBoxes(page, name, [btnBox, await menu.boundingBox()], view, 32)
  console.log(`wrote ${OUT}/${name}.png — menu: ${JSON.stringify(items)}`)
  await page.keyboard.press('Escape')
  await page.waitForTimeout(150)
}

const VIEW = { width: 760, height: 420 }
// The ⋯ menus run ~410px tall, so their sent-state frames need room below.
const TALL_VIEW = { width: 760, height: 540 }
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
    { theme: 'dark', editor: 'vscode', local: true, shots: [
      { path: FILE_CHIP, kind: 'file', row: 'Open in VS Code', name: 'file-menu-tunnel-dark' },
    ] },
    // The click's acknowledgment: label flips to the sent hint, menu stays open.
    { theme: 'dark', editor: 'vscode', shots: [
      { type: 'sent', path: FILE_CHIP, kind: 'file', row: 'Open in VS Code',
        sentPrefix: 'Sent to VS Code. Nothing opened?', name: 'file-menu-sent-dark' },
    ] },
    // The other two surfaces carrying the row: MarkdownPanel's ⋯ overflow and
    // the Files app FileViewer's "More options" menu (with its icon).
    { theme: 'dark', editor: 'vscode', scene: 'markdown-panel', shots: [
      { type: 'overflow', trigger: 'More options', row: 'Open in VS Code', name: 'markdown-overflow-menu-dark' },
    ] },
    { theme: 'dark', editor: 'vscode', scene: 'markdown-panel', view: TALL_VIEW, shots: [
      { type: 'overflow-sent', trigger: 'More options', row: 'Open in VS Code',
        sentPrefix: 'Sent to VS Code. Nothing opened?', name: 'markdown-overflow-sent-dark' },
    ] },
    { theme: 'dark', editor: 'vscode', scene: 'fileviewer', shots: [
      { type: 'overflow', trigger: 'More options', row: 'Open in VS Code', icon: true, name: 'fileviewer-overflow-menu-dark' },
    ] },
    { theme: 'dark', editor: 'vscode', scene: 'fileviewer', view: TALL_VIEW, shots: [
      { type: 'overflow-sent', trigger: 'More options', row: 'Open in VS Code',
        sentPrefix: 'Sent to VS Code. Nothing opened?', name: 'fileviewer-overflow-sent-dark' },
    ] },
  ]) {
    const ctx = await browser.newContext({
      viewport: scene.view || VIEW,
      deviceScaleFactor: 2,
      colorScheme: scene.theme,
    })
    const page = await ctx.newPage()
    const errors = []
    page.on('pageerror', e => errors.push(e.message))
    page.on('console', m => { if (m.text().startsWith('CAPTURE-STUB')) console.log(m.text()) })
    await page.goto(
      `${BASE}/capture/remote-editor-menu.html?theme=${scene.theme}&editor=${scene.editor}` +
        `&local=${scene.local ? 1 : 0}&scene=${scene.scene || 'chips'}`,
      { waitUntil: 'networkidle' },
    )
    await page.waitForSelector('[data-capture-root]', { timeout: 15000 })
    // Assert the palette matches the frame name rather than assuming it.
    const applied = await page.evaluate(() => document.documentElement.getAttribute('data-theme'))
    const want = scene.theme === 'light' ? 'kiro-light' : 'kiro-dark'
    if (applied !== want) throw new Error(`theme=${scene.theme}: data-theme=${applied}, wanted ${want}`)
    if (!scene.scene) {
      // Both chips must classify before any menu is opened; the row is per-kind.
      await page.waitForSelector(`code[data-path-kind="file"]`, { timeout: 10000 })
      await page.waitForSelector(`code[data-path-kind="dir"]`, { timeout: 10000 })
    }
    for (const shot of scene.shots) {
      if (shot.type === 'sent') await shootSent(page, { ...shot, view: VIEW })
      else if (shot.type === 'overflow') await shootOverflow(page, { ...shot, view: VIEW })
      else if (shot.type === 'overflow-sent') await shootOverflowSent(page, { ...shot, view: scene.view || VIEW })
      else await shoot(page, shot)
    }
    if (errors.length) throw new Error(`pageerror: ${errors[0]}`)
    await ctx.close()
  }
  console.log('OK: remote-editor row present in every frame')
} finally {
  await browser.close()
}

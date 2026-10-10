/**
 * Screenshot harness for the transcript file-download UX in PR #16831.
 *
 * Captures the repaired menus from the current build with mocked API responses:
 *
 *   30-chip-menu-rightclick    File-path chip context menu opened by right-click,
 *                              showing Download + Copy path + Open entries.
 *   31-chip-menu-keyboard      Same menu opened via Shift+F10 (keyboard activation).
 *   32-diff-menu-multi         Diff card "⋯" menu with multi-file Download entries
 *                              showing unique basenames and Open; layout stays in the header.
 *   33-diff-menu-pending       Diff card "⋯" menu with a Download entry in the
 *                              disabled/busy state while a transfer is in flight.
 *   desktop / mobile         Full transcript menus, including a 320px viewport.
 *   35-chip-menu-remote       Remote file-path chip: Download and Copy path only.
 *   34-chip-menu-refusal       File-path chip menu after a credential-scan refusal:
 *                              error notice visible, "Ask the agent" hand-off present.
 *   36-single-file-header      Single-file diff card header with ⋯ menu open showing
 *                              Open and Download (one file, no multi-file card row).
 *   37-compact-header-menu     Compact header ⋯ menu showing "Switch to split/unified
 *                              view" layout toggle (file HEAD probe fails → no directLayout).
 *   38-disabled-more           Bare diff with no file names: ⋯ button disabled because
 *                              hasMenuActions is false (no Open, no layout, no Download).
 *
 * House pattern: the REAL built SPA (website/dist) behind the in-process static
 * server, every /api/** answered from fixtures via Playwright route interception
 * — gateway-free, no kiro-cli, no token.
 *
 * Usage (from website/):
 *   npm run build          # if dist/ is stale
 *   node capture/shoot-file-download-evidence.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { serveDist } from '../scripts/lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from '../scripts/lib/stub-dashboard-api.mjs'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const OUT = process.argv[2] || '../temp-screenshots/file-download'
const PROJECT = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const SLOT = 'chat-download-evidence'
const MAX_EDGE = 2000
const MIN_MBPP = 15

mkdirSync(OUT, { recursive: true })

// ── Fixture data ─────────────────────────────────────────────────────────────

/** A two-file unified diff patch the DiffBlock will render as a multi-file card. */
const MULTI_DIFF = [
  '```diff',
  '--- /tmp/project/src/utils/format.ts',
  '+++ /tmp/project/src/utils/format.ts',
  '@@ -12,3 +12,4 @@',
  ' export function formatCurrency(amount: number) {',
  '-  return `$${amount.toFixed(2)}`',
  '+  const abs = Math.abs(amount)',
  "+  return `${amount < 0 ? '-' : ''}$${abs.toFixed(2)}`",
  ' }',
  '--- /tmp/project/src/utils/validate.ts',
  '+++ /tmp/project/src/utils/validate.ts',
  '@@ -1,3 +1,5 @@',
  ' export function isEmail(s: string) {',
  '-  return s.includes("@")',
  '+  if (!s || typeof s !== "string") return false',
  '+  const re = /^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$/',
  '+  return re.test(s)',
  ' }',
  '```',
].join('\n')

/** A single-file unified diff for the single-file header evidence. */
const SINGLE_DIFF = [
  '```diff',
  '--- /tmp/project/src/utils/format.ts',
  '+++ /tmp/project/src/utils/format.ts',
  '@@ -12,3 +12,4 @@',
  ' export function formatCurrency(amount: number) {',
  '-  return `$${amount.toFixed(2)}`',
  '+  const abs = Math.abs(amount)',
  "+  return `${amount < 0 ? '-' : ''}$${abs.toFixed(2)}`",
  ' }',
  '```',
].join('\n')

/** A diff referencing a nonexistent path so the HEAD probe fails →
 *  directLayout is false → layout toggle lives in the compact ⋯ menu. */
const COMPACT_DIFF = [
  '```diff',
  '--- /nonexistent/nowhere/compact-demo.ts',
  '+++ /nonexistent/nowhere/compact-demo.ts',
  '@@ -1,2 +1,3 @@',
  ' export const x = 1',
  '+export const y = 2',
  ' export const z = 3',
  '```',
].join('\n')

/** A bare diff with no ---/+++ headers → no file names → no download paths,
 *  no Open target, no layout in menu → hasMenuActions is false → ⋯ disabled. */
const BARE_DIFF = [
  '```diff',
  '@@ -1,2 +1,3 @@',
  ' const a = 1',
  '+const b = 2',
  ' const c = 3',
  '```',
].join('\n')

/** An assistant message referencing a file path so InlineCode renders a chip. */
const CHIP_MESSAGE = 'The validation helper is `/tmp/project/src/utils/validate.ts`. I updated the formatting helper at `/tmp/project/src/utils/format.ts` — the negative-amount branch now prefixes a minus sign.'

const slots = [{
  key: SLOT, title: 'Download evidence', running: false,
  last_message: 'Updated formatting.', messages: 3, agent: 'kirocrew',
  memory_mode: 'persistent', modified: 1791288000,
  source_links: [], source_links_total: 0,
}]

const slotDetail = {
  running: false, has_more: false, total: 7, queue: [],
  messages: [
    { role: 'user', content: 'Fix the currency formatter to handle negative amounts and tighten the email validator', ts: 1791288000 - 600 },
    { role: 'assistant', ts: 1791288000 - 120, content: CHIP_MESSAGE + '\n\nHere are the changes:\n\n' + MULTI_DIFF },
    { role: 'assistant', ts: 1791288000 - 60, content: 'Both changes verified — the formatter handles negatives and the validator rejects malformed addresses.' },
    // Single-file diff for frame 36 (single-file header evidence)
    { role: 'assistant', ts: 1791288000 - 30, content: 'Here is just the formatter fix:\n\n' + SINGLE_DIFF },
    // Compact header diff for frame 37 (file does not resolve → layout in menu)
    { role: 'assistant', ts: 1791288000 - 20, content: 'A change in a file that no longer exists on disk:\n\n' + COMPACT_DIFF },
    // Bare diff for frame 38 (no file names → disabled ⋯)
    { role: 'assistant', ts: 1791288000 - 10, content: 'An anonymous patch:\n\n' + BARE_DIFF },
  ],
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function pngSize(path) {
  const b = readFileSync(path)
  return { w: b.readUInt32BE(16), h: b.readUInt32BE(20) }
}

const wrote = []
function record(file, note) {
  const { w, h } = pngSize(file)
  const bytes = readFileSync(file).length
  const mbpp = Math.round((bytes * 1000) / (w * h))
  const over = w > MAX_EDGE || h > MAX_EDGE
  const blank = mbpp < MIN_MBPP
  console.log(`wrote ${file}  ${w}×${h}  ${bytes}B  ${mbpp} mB/px${over ? '  OVER' : ''}${blank ? '  BLANK' : ''}  ${note}`)
  wrote.push({ file, over, blank })
  if (blank) throw new Error(`frame ${file}: ${mbpp} mB/px below ${MIN_MBPP} blank floor`)
  if (over) throw new Error(`frame ${file}: over ${MAX_EDGE}px`)
}

// ── Main ─────────────────────────────────────────────────────────────────────

async function main() {

/** Hover an element then click a target inside it, bypassing Playwright's pointer-intercept check.
 *  Uses mouse.move for the hover (fires CSS :hover → opacity transitions) then
 *  dispatchEvent('click') on the target (Radix DropdownMenu triggers listen for click). */
async function hoverAndClick(page, hoverTarget, clickTarget) {
  await hoverTarget.scrollIntoViewIfNeeded()
  // Force ALL opacity-0 action rows inside the hoverTarget to be visible
  await hoverTarget.evaluate(el => {
    el.querySelectorAll('.opacity-0').forEach(row => row.style.opacity = '1')
  })
  await page.waitForTimeout(200)
  // Dispatch a full pointer event sequence: Radix DropdownMenu uses onPointerDown
  await clickTarget.evaluate(el => {
    for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
      el.dispatchEvent(new PointerEvent(type, { bubbles: true, cancelable: true, composed: true }))
    }
  })
}

/** Right-click: Radix ContextMenu listens for 'contextmenu', not mouseup button=2. */
async function rightClick(page, target) {
  await target.scrollIntoViewIfNeeded()
  await target.dispatchEvent('contextmenu', { bubbles: true })
}

  const { srv, base } = await serveDist()
  const executablePath = chromiumExecutable()
  console.log('chromium:', executablePath || '(playwright default)')
  const browser = await chromium.launch({ executablePath })
  const context = await browser.newContext({ viewport: { width: 1200, height: 820 }, deviceScaleFactor: 1 })
  const page = await context.newPage()

  try {
  /** Track in-flight /api/file-download requests so we can screenshot the busy state. */
  let downloadGate = null

  let directLocal = true
  const extra = async (path, route) => {
    if (path === '/api/dashboard/branding') return json(route, { bot_name: 'Kiro Crew', avatar: '/logo.png', direct_local: directLocal }), true
    if (path === '/api/chat/slots') return json(route, slots), true
    if (/^\/api\/chat\/slots\/[^/]+/.test(path)) return json(route, slotDetail), true
    if (path === '/api/recent-projects') return json(route, { dirs: [PROJECT] }), true
    if (path === '/api/chat/nav/resolve-links') return json(route, { summaries: [] }), true
    // The file-stat probe that classifies chips as file/dir. Answer "file"
    // for everything EXCEPT /nonexistent/ paths (those return 404 so the
    // compact-header diff gets no directLayout → layout goes in menu).
    if (path === '/api/file-read') {
      const url = new URL(route.request().url())
      const filePath = url.searchParams.get('path') || ''
      if (filePath.includes('/nonexistent/')) {
        return route.fulfill({ status: 404, body: 'Not found' }), true
      }
      return route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'text/plain', 'X-Path-Kind': 'file' },
        body: '// file content',
      }), true
    }
    // /api/file-download: conditionally gate to capture the busy state.
    if (path === '/api/file-download') {
      const url = new URL(route.request().url())
      const filePath = url.searchParams.get('path') || ''

      // If gated, wait for release before responding — this lets us
      // screenshot the menu while the download entry is disabled/busy.
      if (downloadGate) {
        await downloadGate
        downloadGate = null
      }

      // Check if this is the "refusal" path (credential scan).
      if (filePath.includes('validate')) {
        return route.fulfill({
          status: 400,
          contentType: 'application/json',
          body: JSON.stringify({ error: 'file content was redacted; download aborted', code: 'content_redacted' }),
        }), true
      }

      // Normal download: return some bytes.
      return route.fulfill({
        status: 200,
        headers: {
          'Content-Type': 'application/octet-stream',
          'Content-Disposition': 'attachment',
        },
        body: '// file content\n',
      }), true
    }
    // DiffBlock's "Open" HEAD probe — 200 so the Open row renders,
    // except /nonexistent/ paths which return 404 (compact-header evidence).
    if (path.startsWith('/api/files')) {
      const url = new URL(route.request().url())
      const filePath = url.searchParams.get('path') || url.pathname.replace('/api/files', '')
      if (filePath.includes('/nonexistent/') || filePath.includes('nonexistent')) {
        return route.fulfill({ status: 404, body: 'Not found' }), true
      }
      return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' }), true
    }
    return false
  }

  await stubDashboardApi(page, { slots, extra, localStorageEntries: { 'mc-active-slot-chat': SLOT, 'mc-chat-config': JSON.stringify({ pinLastPrompt: false, streamMode: 'immediate' }) } })
  logPageProblems(page)

  async function load() {
    await page.goto(base + '/?sid=' + encodeURIComponent(SLOT), { waitUntil: 'domcontentloaded' })

  }

  await load()

  // Wait for the foldable diff chip to render (diffs are collapsed by default in the transcript).
  const diffChip = page.locator('[data-testid="prose-diff-chip"]').first()
  try {
    await diffChip.waitFor({ state: 'visible', timeout: 20000 })
  } catch {
    await page.screenshot({ animations: 'disabled', path: `${OUT}/DEBUG-page.png`, fullPage: true })
    const bodyText = await page.locator('body').innerText()
    console.log('DEBUG: page body (first 500):', bodyText.slice(0, 500).replace(/\n/g, ' | '))
    const html = await page.content()
    const hasDiff = html.includes('diff-block') || html.includes('prose-diff-chip')
    console.log('DEBUG: html has diff-block or prose-diff-chip?', hasDiff, ' length:', html.length)
    throw new Error('diff chip not visible — check DEBUG-page.png')
  }
  console.log('diff chip found — clicking to expand')
  // Click the chip to expand the diff block.
  await diffChip.evaluate(el => el.click())

  const diffBlock = page.locator('.diff-block').first()
  await diffBlock.waitFor({ state: 'visible', timeout: 10000 })


  // ── Frame 30: File-path chip context menu (right-click) ────────────────────
  // Find a file-path chip in the transcript — InlineCode renders paths
  // starting with / as clickable chips.
  const chip = page.locator('code').filter({ hasText: 'format.ts' }).first()
  await chip.waitFor({ state: 'visible', timeout: 10000 })
  await rightClick(page, chip)
  const chipMenu = page.locator('[role="menu"]').first()
  await chipMenu.waitFor({ state: 'visible', timeout: 8000 })

  {
    const rows = await chipMenu.locator('[role="menuitem"]').allInnerTexts()
    console.log('DIAG chip-menu rows:', JSON.stringify(rows))
    if (!rows.some(r => /Download/.test(r))) throw new Error(`frame 30: no Download in chip menu: ${JSON.stringify(rows)}`)
    await chipMenu.screenshot({ animations: 'disabled', path: `${OUT}/30-chip-menu-rightclick.png` })
    record(`${OUT}/30-chip-menu-rightclick.png`, `rows=${JSON.stringify(rows)}`)
  }
  await page.keyboard.press('Escape')


  // ── Frame 31: File-path chip context menu (keyboard: Shift+F10) ────────────
  await chip.focus()
  await page.keyboard.down('Shift')
  await page.keyboard.press('F10')
  await page.keyboard.up('Shift')
  await chipMenu.waitFor({ state: 'visible', timeout: 8000 })

  {
    const rows = await chipMenu.locator('[role="menuitem"]').allInnerTexts()
    console.log('DIAG chip-menu-keyboard rows:', JSON.stringify(rows))
    await chipMenu.screenshot({ animations: 'disabled', path: `${OUT}/31-chip-menu-keyboard.png` })
    record(`${OUT}/31-chip-menu-keyboard.png`, `rows=${JSON.stringify(rows)}`)
  }
  await page.keyboard.press('Escape')


  // ── Frame 32: Diff card "⋯" menu with multi-file Download basenames ────────
  // The diff block header has a "⋯" More options button with aria-label.
  // Hover the diff block to make the opacity-0 actions row visible.
  const moreBtn = diffBlock.getByRole('button', { name: /more options/i }).first()
  await hoverAndClick(page, diffBlock, moreBtn)
  const diffMenu = page.locator('[role="menu"]').first()
  await diffMenu.waitFor({ state: 'visible', timeout: 8000 })

  {
    const rows = await diffMenu.locator('[role="menuitem"]').allInnerTexts()
    console.log('DIAG diff-menu rows:', JSON.stringify(rows))
    // Verify basenames are shown, not full paths.
    const downloadRows = rows.filter(r => /Download/.test(r))
    console.log('DIAG download rows:', JSON.stringify(downloadRows))
    if (downloadRows.length !== 2) throw new Error(`expected 2 download rows, got ${downloadRows.length}`)
    // Check for basename display (should show "format.ts" not "src/utils/format.ts").
    for (const r of downloadRows) {
      if (r.includes('src/utils/')) {
        throw new Error(`unexpected full path for unique basename: ${r}`)
      }
    }
    await page.screenshot({ animations: 'disabled', path: `${OUT}/desktop.png` })
    record(`${OUT}/desktop.png`, 'completed multi-file diff with basename labels')
    await diffMenu.screenshot({ animations: 'disabled', path: `${OUT}/32-diff-menu-multi.png` })
    record(`${OUT}/32-diff-menu-multi.png`, `rows=${JSON.stringify(rows)}`)
  }
  await page.keyboard.press('Escape')


  // ── Frame 33: Download pending/busy state ──────────────────────────────────
  // Gate the /api/file-download response so we can screenshot the disabled state.
  let releaseDownload
  downloadGate = new Promise(resolve => { releaseDownload = resolve })

  await moreBtn.scrollIntoViewIfNeeded()
  await hoverAndClick(page, diffBlock, moreBtn)
  await diffMenu.waitFor({ state: 'visible', timeout: 8000 })

  // Click a Download entry — it will go busy while the gate holds.
  const downloadItem = diffMenu.getByRole('menuitem', { name: /Download/ }).first()
  { const db = await downloadItem.boundingBox(); await page.mouse.click(db.x + db.width/2, db.y + db.height/2) }
  // The pending label is the signal that the held transfer reached the UI.
  await diffMenu.getByRole('menuitem', { name: /Downloading/ }).waitFor()

  {
    const rows = await diffMenu.locator('[role="menuitem"]').allInnerTexts()
    if (!rows.some(row => row.includes('Downloading'))) throw new Error('missing pending label')
    console.log('DIAG pending-state rows:', JSON.stringify(rows))
    await diffMenu.screenshot({ animations: 'disabled', path: `${OUT}/33-diff-menu-pending.png` })
    record(`${OUT}/33-diff-menu-pending.png`, `pending download in flight`)
  }
  // Release the gate so the download completes.
  releaseDownload()
  await diffMenu.waitFor({ state: 'hidden' })
  await page.waitForFunction(el => el === document.activeElement, await moreBtn.elementHandle())
  await page.keyboard.press('Escape')


  // ── Refusal, narrow viewports, and remote context ─────────────────────────
  const refusalChip = page.locator('code').filter({ hasText: 'validate.ts' }).first()
  await rightClick(page, refusalChip)
  const refusalMenu = page.getByRole('menu')
  { const dlBtn = refusalMenu.getByRole('menuitem', { name: 'Download', exact: true }); const b = await dlBtn.boundingBox(); await page.mouse.click(b.x + b.width/2, b.y + b.height/2) }
  await page.waitForTimeout(500)
  await refusalMenu.getByRole('menuitem', { name: /Ask the agent/ }).waitFor()
  await refusalMenu.screenshot({ animations: 'disabled', path: `${OUT}/34-chip-menu-refusal.png` })
  record(`${OUT}/34-chip-menu-refusal.png`, 'mock credential refusal with retry and agent hand-off')
  await page.screenshot({ animations: 'disabled', path: `${OUT}/refusal.png` })
  record(`${OUT}/refusal.png`, 'mock credential refusal in transcript')
  await page.keyboard.press('Escape')
  await refusalMenu.waitFor({ state: 'hidden' })

  // Preserve the upstream 375px evidence and add the minimum 320px viewport.
  for (const width of [375, 320]) {
    await page.setViewportSize({ width, height: width === 375 ? 812 : 740 })
    await hoverAndClick(page, diffBlock, moreBtn)
    await diffMenu.waitFor({ state: 'visible', timeout: 8000 })
    const file = width === 375 ? '35-mobile-menu.png' : 'mobile.png'
    await page.screenshot({ animations: 'disabled', path: `${OUT}/${file}` })
    record(`${OUT}/${file}`, `${width}px multi-file menu with current basename labels`)
    const bounds = await diffMenu.boundingBox()
    if (!bounds || bounds.x < 0 || bounds.x + bounds.width > width) throw new Error('mobile menu overflows')
    await page.keyboard.press('Escape')
    await diffMenu.waitFor({ state: 'hidden' })
  }
  directLocal = false
  await page.setViewportSize({ width: 1200, height: 820 })
  await load()
  // Re-query chip locator after page reload (old reference is detached)
  const remoteChip = page.locator('code').filter({ hasText: 'format.ts' }).first()
  await remoteChip.waitFor({ state: 'visible', timeout: 10000 })
  await rightClick(page, remoteChip)
  const remoteMenu = page.locator('[role="menu"]').first()
  await remoteMenu.waitFor({ state: 'visible', timeout: 8000 })
  const remoteRows = await remoteMenu.locator('[role="menuitem"]').allInnerTexts()
  if (remoteRows.some(row => /Open|Finder/.test(row))) throw new Error('remote menu exposes local actions')
  await remoteMenu.screenshot({ animations: 'disabled', path: `${OUT}/35-chip-menu-remote.png` })
  record(`${OUT}/35-chip-menu-remote.png`, 'remote fixture: Download and Copy path')


  // ── Frame 36: Single-file diff header with ⋯ menu open ────────────────────
  // The fourth assistant message has a single-file diff. Find its diff chip,
  // expand it, then open the ⋯ menu showing Open + Download (1 file only).
  await page.setViewportSize({ width: 1200, height: 820 })
  directLocal = true
  await load()

  // Wait for all diff chips. The single-file diff is the second diff chip
  // (first is the multi-file, third is compact, fourth is bare).
  const allDiffChips = page.locator('[data-testid="prose-diff-chip"]')
  await allDiffChips.first().waitFor({ state: 'visible', timeout: 20000 })
  const chipCount = await allDiffChips.count()
  console.log(`DIAG: found ${chipCount} diff chips`)

  // The single-file diff should be the 2nd chip (index 1)
  if (chipCount >= 2) {
    const singleChip = allDiffChips.nth(1)
    await singleChip.scrollIntoViewIfNeeded()
    await singleChip.evaluate(el => el.click())
    // After reload, no diff blocks are expanded yet. This chip creates the FIRST .diff-block.
    const singleDiffBlock = page.locator('.diff-block').first()
    await singleDiffBlock.waitFor({ state: 'visible', timeout: 10000 })

    const singleMoreBtn = singleDiffBlock.getByRole('button', { name: /more options/i }).first()
    await hoverAndClick(page, singleDiffBlock, singleMoreBtn)
    const singleMenu = page.locator('[role="menu"]').first()
    await singleMenu.waitFor({ state: 'visible', timeout: 8000 })

    {
      const rows = await singleMenu.locator('[role="menuitem"]').allInnerTexts()
      console.log('DIAG single-file-menu rows:', JSON.stringify(rows))
      // Verify: Open + Download (no basename suffix for single file)
      if (!rows.some(r => /Open/.test(r))) console.warn('WARN: no Open in single-file menu')
      if (!rows.some(r => /Download/.test(r))) console.warn('WARN: no Download in single-file menu')
      // Screenshot the whole header area with menu open
      await page.screenshot({ animations: 'disabled', path: `${OUT}/36-single-file-header.png`, clip: await (async () => {
        const hdrBox = await singleDiffBlock.boundingBox()
        const menuBox = await singleMenu.boundingBox()
        if (!hdrBox || !menuBox) return undefined
        const x = Math.min(hdrBox.x, menuBox.x) - 8
        const y = Math.min(hdrBox.y, menuBox.y) - 8
        const right = Math.max(hdrBox.x + hdrBox.width, menuBox.x + menuBox.width) + 8
        const bottom = Math.max(hdrBox.y + hdrBox.height, menuBox.y + menuBox.height) + 8
        return { x: Math.max(0, x), y: Math.max(0, y), width: right - Math.max(0, x), height: bottom - Math.max(0, y) }
      })() })
      record(`${OUT}/36-single-file-header.png`, `single-file header, rows=${JSON.stringify(rows)}`)
    }
    await page.keyboard.press('Escape')
  } else {
    console.warn('WARN: not enough diff chips for single-file capture')
  }


  // ── Frame 37: Compact header with layout toggle in ⋯ menu ─────────────────
  // The compact diff (index 2) has a nonexistent path so HEAD probe fails →
  // directLayout is false → layout toggle lands in the ⋯ menu.
  if (chipCount >= 3) {
    const compactChip = allDiffChips.nth(2)
    await compactChip.scrollIntoViewIfNeeded()
    await compactChip.evaluate(el => el.click())
    // Wait a moment for the HEAD probe to fail (404)
    await page.waitForTimeout(1500)

    // After expanding single + compact chips, compact is the 2nd expanded block
    const compactDiffBlock = page.locator('.diff-block').nth(1)
    await compactDiffBlock.waitFor({ state: 'visible', timeout: 10000 })

    const compactMoreBtn = compactDiffBlock.getByRole('button', { name: /more options/i }).first()
    await hoverAndClick(page, compactDiffBlock, compactMoreBtn)
    const compactMenu = page.locator('[role="menu"]').first()
    await compactMenu.waitFor({ state: 'visible', timeout: 8000 })

    {
      const rows = await compactMenu.locator('[role="menuitem"]').allInnerTexts()
      console.log('DIAG compact-header-menu rows:', JSON.stringify(rows))
      // Should have "Switch to split view" or "Switch to unified view" in the menu
      const hasLayoutToggle = rows.some(r => /Switch to/.test(r))
      console.log('DIAG compact menu has layout toggle:', hasLayoutToggle)
      await page.screenshot({ animations: 'disabled', path: `${OUT}/37-compact-header-menu.png`, clip: await (async () => {
        const hdrBox = await compactDiffBlock.boundingBox()
        const menuBox = await compactMenu.boundingBox()
        if (!hdrBox || !menuBox) return undefined
        const x = Math.min(hdrBox.x, menuBox.x) - 8
        const y = Math.min(hdrBox.y, menuBox.y) - 8
        const right = Math.max(hdrBox.x + hdrBox.width, menuBox.x + menuBox.width) + 8
        const bottom = Math.max(hdrBox.y + hdrBox.height, menuBox.y + menuBox.height) + 8
        return { x: Math.max(0, x), y: Math.max(0, y), width: right - Math.max(0, x), height: bottom - Math.max(0, y) }
      })() })
      record(`${OUT}/37-compact-header-menu.png`, `compact header, rows=${JSON.stringify(rows)}`)
    }
    await page.keyboard.press('Escape')
  } else {
    console.warn('WARN: not enough diff chips for compact-header capture')
  }


  // ── Frame 38: Disabled More button (no menu actions) ───────────────────────
  // The bare diff (index 3) has no ---/+++ headers → no file names →
  // hasMenuActions is false → the ⋯ button is disabled.
  if (chipCount >= 4) {
    const bareChip = allDiffChips.nth(3)
    await bareChip.scrollIntoViewIfNeeded()
    await bareChip.evaluate(el => el.click())
    await page.waitForTimeout(500)

    // After expanding single + compact + bare chips, bare is the 3rd (nth(2))
    const bareDiffBlock = page.locator('.diff-block').nth(2)
    await bareDiffBlock.waitFor({ state: 'visible', timeout: 10000 })

    // Hover to make controls visible
    // Force opacity visible
    await bareDiffBlock.evaluate(el => {
      el.querySelectorAll('.opacity-0').forEach(row => row.style.opacity = '1')
    })
    await page.waitForTimeout(300)

    const bareMoreBtn = bareDiffBlock.getByRole('button', { name: /more options/i }).first()
    const isDisabled = await bareMoreBtn.isDisabled()
    console.log('DIAG bare-diff More button disabled:', isDisabled)

    // Screenshot the header row showing the disabled ⋯
    await page.screenshot({ animations: 'disabled', path: `${OUT}/38-disabled-more.png`, clip: await (async () => {
      const box = await bareDiffBlock.boundingBox()
      if (!box) return undefined
      // Capture just the top portion (header row) — about 60px high
      return { x: Math.max(0, box.x - 8), y: Math.max(0, box.y - 8), width: box.width + 16, height: Math.min(80, box.height + 16) }
    })() })
    record(`${OUT}/38-disabled-more.png`, `disabled More button, isDisabled=${isDisabled}`)
  } else {
    console.warn('WARN: not enough diff chips for disabled-more capture')
  }

  // ── Summary ────────────────────────────────────────────────────────────────
  console.log('\n── SUMMARY ────────────────────────────────')
  const bad = wrote.filter(w => w.over || w.blank)
  console.log(bad.length ? `FAIL ${bad.length}` : `all ${wrote.length} frames ok`)

  } finally {
    await browser.close()
    await new Promise(resolve => srv.close(resolve))
  }
}

main().catch(err => { console.error(err); process.exit(1) })

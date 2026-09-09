// Shoot the offline MENU surfaces the UX lane found unevidenced: the session
// menu's standing reason row (with Tags left at full weight), the tag popover's
// refused writes, the dimmed colour swatch row, and the folder menu's own reason
// row. Run from website/: node scripts/capture-offline-menu-shots.mjs <outdir>
//
// Same narrow route stubbing and the same launch workaround as
// capture-offline-rename-shots.mjs, for the same measured reasons.
import { createServer } from 'vite'
import { chromium } from 'playwright-core'
import path from 'node:path'
import { mkdirSync } from 'node:fs'

const outDir = process.argv[2] || path.join(process.env.TMPDIR || '/tmp', 'offline-menu-shots')
mkdirSync(outDir, { recursive: true })

const STUB_EMPTY_LIST = ['/api/chat/tag-columns']
const TAGS = [
  { id: 't1', name: 'release', color: '#e0754a', order: 0 },
  { id: 't2', name: 'urgent', color: '#4a90e0', order: 1 },
]
const FOLDERS = [{ id: 'f1', name: 'Drafts', parent_id: null, order: 0 }]

const server = await createServer({
  configFile: 'vite.config.ts',
  server: { port: 5215, strictPort: true, host: '127.0.0.1' },
})
await server.listen()
const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM_PATH,
  env: { ...process.env, LD_LIBRARY_PATH: '' },
})

/**
 * Each shot opens one offline surface and asserts the thing being evidenced is
 * actually on screen before the shutter — a screenshot of a surface that failed
 * to open would otherwise ship as evidence of the opposite.
 */
const shots = [
  {
    name: 'session-menu-offline-reason',
    // Taller than the default: this menu overflowed a 700px shutter, so the
    // reason row it is cited for sat below the frame while still passing a
    // DOM-only assert.
    viewport: { width: 460, height: 940 },
    open: async page => {
      // The row's menu trigger carries no testid; right-click renders the same
      // SessionActionsMenu through its context-menu variant.
      await page.locator('.session-row').first().click({ button: 'right', force: true })
      await page.waitForSelector('[data-testid="menu-offline-reason"]', { timeout: 15000 })
    },
    // Tags must be present and NOT marked disabled: that is the exclusion the
    // lanes asked for, and a screenshot is where a reader can see it.
    assert: async page => {
      const reason = await page.locator('[data-testid="menu-offline-reason"]').count()
      const tags = page.getByRole('menuitem', { name: /Tags/ })
      const tagsCount = await tags.count()
      const tagsDisabled = await page.locator('[role="menuitem"][aria-disabled="true"]').filter({ hasText: /Tags/ }).count()
      // In DOM is not in FRAME: a clipped reason row evidences nothing.
      const box = await page.locator('[data-testid="menu-offline-reason"]').boundingBox()
      const size = page.viewportSize()
      const inFrame = !!box && box.y + box.height <= size.height
      return { ok: reason === 1 && tagsCount >= 1 && tagsDisabled === 0 && inFrame, reason, tagsCount, tagsDisabled, inFrame }
    },
  },
  {
    name: 'tag-popover-offline-refusal',
    open: async page => {
      await page.locator('.session-row').first().click({ button: 'right', force: true })
      await page.getByRole('menuitem', { name: /Tags/ }).first().click({ force: true })
      await page.waitForSelector('[data-testid="tag-offline-reason"]', { timeout: 15000 })
    },
    assert: async page => {
      const reason = await page.locator('[data-testid="tag-offline-reason"]').count()
      const options = await page.locator('[role="menuitemcheckbox"][aria-disabled="true"]').count()
      return { ok: reason === 1 && options > 0, reason, options }
    },
  },
  {
    name: 'folder-menu-offline-reason',
    open: async page => {
      await page.locator('[data-testid^="folder-menu-"]').first().click({ force: true })
      await page.waitForSelector('[data-testid="folder-offline-reason"]', { timeout: 15000 })
    },
    assert: async page => {
      const reason = await page.locator('[data-testid="folder-offline-reason"]').count()
      const dimmed = await page.locator('[role="menuitem"][aria-disabled="true"]').count()
      return { ok: reason === 1 && dimmed > 0, reason, dimmed }
    },
  },
  {
    // The artifacts folder menu is one of the four this change gates and was the
    // only one with no frame of its own.
    name: 'artifacts-folder-menu-offline',
    scene: 'artifacts',
    ready: '[aria-label^="Actions for folder"]',
    open: async page => {
      await page.locator('[aria-label^="Actions for folder"]').first().click({ force: true })
      await page.waitForSelector('[data-testid="artifact-folder-offline-reason"]', { timeout: 15000 })
    },
    assert: async page => {
      const reason = await page.locator('[data-testid="artifact-folder-offline-reason"]').count()
      const dimmed = await page.locator('[role="menuitem"][aria-disabled="true"]').count()
      return { ok: reason === 1 && dimmed > 0, reason, dimmed }
    },
  },
]

let failures = 0
for (const shot of shots) {
  const ctx = await browser.newContext({ viewport: shot.viewport || { width: 460, height: 700 }, reducedMotion: 'reduce' })
  const page = await ctx.newPage()
  for (const p of STUB_EMPTY_LIST) {
    await page.route(`**${p}`, route => route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }))
  }
  await page.route('**/api/chat/tags', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(TAGS) }))
  await page.route('**/api/chat/folders', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(FOLDERS) }))
  await page.goto(`http://127.0.0.1:5215/capture/offline-rename-gate.html?scene=${shot.scene || 'refused'}`)
  await page.waitForSelector(shot.ready || 'text=Release notes draft', { timeout: 45000 })
  await shot.open(page)
  await page.waitForTimeout(300)

  const detail = await shot.assert(page)
  const errorCards = await page.locator('text=Cannot read properties').count()
  const notices = await page.locator('text=Could not load').count()
  const clean = errorCards === 0 && notices === 0
  if (!detail.ok || !clean) {
    console.error(`ASSERT FAILED ${shot.name}: ${JSON.stringify(detail)} errorCards=${errorCards} notices=${notices}`)
    failures++
  }
  await page.screenshot({ path: path.join(outDir, `${shot.name}.png`) })
  console.log('shot', shot.name, JSON.stringify(detail), detail.ok && clean ? 'OK' : 'MISMATCH')
  await ctx.close()
}
await browser.close()
await server.close()
if (failures) {
  console.error(`${failures} scene(s) did not show the claimed state`)
  process.exit(1)
}

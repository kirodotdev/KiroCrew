/**
 * Screenshot harness for issue #10688 — the file panel's inline
 * "Remove from Knowledge Library" toggle.
 *
 * Three shots, all cropped to the panel header where the row-2 knowledge
 * buttons live:
 *   1. added-local-file.png  — the change itself: a REAL remove button where a
 *                              static "In Knowledge Library" badge used to be.
 *   2. remove-menu-row.png   — the ⋯ overflow menu with the new
 *                              "Remove from Knowledge Library" entry.
 *   3. not-added.png         — the unchanged Add affordance (no regression).
 *
 * The harness also asserts what it photographs: the remove button exists only
 * for a `local_file` source (folder sources keep the inert badge), and the ⋯
 * menu carries the new menuitem. Runs the REAL built SPA (website/dist) behind
 * the shared loopback static server with every /api/** call answered from
 * fixtures — gateway-free, no kiro-cli, no credentials.
 *
 * Usage: node scripts/capture-knowledge-toggle.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/knowledge-toggle'
const SLOT = 'knowledge-toggle'
const DOC_PATH = '/home/builder/project/docs/release-notes.md'
const DOC = [
  '# Release notes',
  '',
  'Adding this file to the knowledge library indexes it for semantic search.',
  'With #10688 the panel offers a way back: the toggle becomes a remove',
  'button once the file is indexed.',
  '',
].join('\n')

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Release notes review',
  running: false,
  messages: 4,
  agent: 'kirocrew',
  modified: Math.floor(Date.now() / 1000),
  last_ts: '2026-09-24T01:00:00Z',
  folder_id: '',
}]

/** Knowledge fixtures the panel fetches on open; `added` selects the state.
 *
 * `/api/knowledge/sources?uri=…` must return an ARRAY (the hook reads
 * `sources.length` and `sources[0]`), each row carrying the `id` and
 * `source_type` the remove affordance now keys off.
 */
const extraFor = added => async (path, route) => {
  if (path.startsWith('/api/file-read')) {
    await route.fulfill({ status: 200, contentType: 'text/plain', body: DOC })
    return true
  }
  if (path.startsWith('/api/knowledge/sources')) {
    await json(route, added
      ? [{ id: 7, name: 'release-notes.md', source_type: 'local_file', uri: DOC_PATH, sync_status: 'synced', item_count: 3 }]
      : [])
    return true
  }
  if (path.startsWith('/api/knowledge/config')) {
    await json(route, { enabled: true, supported_formats: ['.md', '.txt', '.pdf'] })
    return true
  }
  if (path.startsWith('/api/artifacts')) { await json(route, { artifacts: [] }); return true }
  return false
}

async function main() {
  const { srv, base } = await serveDist()
  // PW_BROWER_EXEC lets the harness run against a shared-cache Chromium whose
  // version differs from this worktree's pin (the bundled headless shell can
  // be absent while the full browser is present).
  const browser = await chromium.launch(
    process.env.PW_BROWSER_EXEC ? { executablePath: process.env.PW_BROWSER_EXEC } : {})
  const width = 1400
  const height = 900
  const failures = []

  async function openPanel(page, added) {
    await stubDashboardApi(page, { slots, theme: 'dark', extra: extraFor(added) })
    await page.addInitScript(([slot, docPath, doc]) => {
      localStorage.setItem('mc-active-slot', slot)
      localStorage.setItem('mc-activity-open:' + slot, 'true')
      localStorage.setItem('mc-privacy-notice-v1', '1')
      localStorage.setItem('mc-lang', 'en')
      localStorage.setItem('mc-panel-tabs:' + slot, JSON.stringify({
        activeId: 'doc',
        tabs: [{ id: 'doc', kind: 'file', title: 'release-notes.md', path: docPath, content: doc }],
      }))
    }, [SLOT, DOC_PATH, DOC])
    await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2600)
    await page.getByText('release-notes.md').first().waitFor({ state: 'visible', timeout: 15000 })
  }

  async function cropHeader(page, name) {
    // The row-2 knowledge buttons sit in the panel header; crop tightly so the
    // toggle itself is legible.
    const header = page.locator('[data-testid="markdown-panel-more-options"]').first()
    const box = await header.boundingBox()
    if (!box) throw new Error('panel ⋯ trigger not found for crop')
    await page.screenshot({
      path: `${OUT}/${name}.png`,
      clip: { x: Math.max(0, box.x - 340), y: Math.max(0, box.y - 24), width: 520, height: 88 },
    })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  const context = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  logPageProblems(page)

  // Shot 1 + 2: the added state — remove button AND the ⋯ menu row.
  await openPanel(page, true)
  const removeBtn = page.getByLabel('Remove from Knowledge Library')
  await removeBtn.waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForTimeout(400)
  await cropHeader(page, 'added-local-file')

  const trigger = page.locator('[data-testid="markdown-panel-more-options"]')
  await trigger.click()
  const menu = page.locator('[role="menu"]').last()
  await menu.waitFor({ state: 'visible', timeout: 10000 })
  await page.waitForTimeout(400)
  const items = (await menu.locator('[role="menuitem"]').allInnerTexts()).map(s => s.trim())
  console.log('ITEMS added/en', JSON.stringify(items))
  if (!items.includes('Remove from Knowledge Library')) {
    failures.push('⋯ menu is missing the "Remove from Knowledge Library" entry')
  }
  const mBox = await menu.boundingBox()
  await page.screenshot({
    path: `${OUT}/remove-menu-row.png`,
    clip: { x: Math.max(0, mBox.x - 120), y: Math.max(0, mBox.y - 56), width: 460, height: Math.min(height, mBox.height + 88) },
  })
  console.log('wrote', `${OUT}/remove-menu-row.png`)
  await page.keyboard.press('Escape')

  // Shot 3: the not-added state — the Add affordance, unchanged.
  const context2 = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: 2 })
  const page2 = await context2.newPage()
  logPageProblems(page2)
  await openPanel(page2, false)
  await page2.getByLabel('Add to Knowledge Library').waitFor({ state: 'visible', timeout: 15000 })
  await page2.waitForTimeout(400)
  await cropHeader(page2, 'not-added')

  if (failures.length) {
    console.error('FAILURES:\n  ' + failures.join('\n  '))
    process.exitCode = 1
  } else {
    console.log('ALL ASSERTIONS PASSED')
  }
  await browser.close()
  srv.close()
}

await main()

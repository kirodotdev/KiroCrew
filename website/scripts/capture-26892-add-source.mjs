/**
 * Screenshot harness for "Add Source takes files and folders in one view, and
 * lists the formats once" (issue #26892).
 *
 * Runs the REAL built SPA (website/dist, or the dist named by argv[3]) behind
 * the shared loopback static server with every /api/** call answered from
 * fixtures: no gateway, no token.
 *
 * Frames: <prefix>-panel-<theme>.png (the Add Source panel as it opens),
 * <prefix>-folder-dark.png (after Choose folder / Local Folder),
 * <prefix>-panel-mobile.png (390 px wide), <prefix>-dropped-folder-dark.png
 * (a folder dropped in a plain browser, which gives no path), <prefix>-no-picker-dark.png
 * (a gateway that cannot open a folder picker: the button adds by path).
 *
 * Usage (in website/): node scripts/capture-26892-add-source.mjs <outDir> [distDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'

import { serveDist, DEFAULT_DIST } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = resolve(process.argv[2] || '../temp-screenshots/26892-add-source')
const DIST = process.argv[3] ? resolve(process.argv[3]) : DEFAULT_DIST
const PREFIX = process.argv[4] || 'after'
mkdirSync(OUT, { recursive: true })

const FORMATS = ['.md', '.txt', '.pdf', '.docx', '.pptx', '.xlsx', '.csv', '.html', '.json', '.yaml', '.py', '.ts', '.js', '.go', '.rs', '.java', '.zip']
const SOURCES = [{
  id: 'src-1', name: 'notes', source_type: 'local_folder', uri: '/home/user/notes', sync_status: 'idle',
  last_synced: new Date().toISOString(), item_count: 12, properties: '{}',
}]

const extraFor = picker => async (path, route) => {
  if (path === '/api/knowledge/config') {
    await json(route, { enabled: true, supported_formats: FORMATS, accepts_no_extension: true, folder_picker: picker, url_allowed_hosts: [] })
    return true
  }
  if (path === '/api/knowledge/pick-folder') { await json(route, { path: '/home/user/notes' }); return true }
  if (path === '/api/knowledge/sources') { await json(route, SOURCES); return true }
  if (path === '/api/dashboard/config' || path === '/api/dashboard-config') {
    await json(route, { knowledge_upload_max_mb: 100, upload_max_mb: 100 })
    return true
  }
  return false
}

const { srv, base } = await serveDist(DIST)
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || undefined })
try {
  const shots = [
    { theme: 'dark', width: 1280, name: 'panel-dark' },
    { theme: 'light', width: 1280, name: 'panel-light' },
    { theme: 'dark', width: 1280, name: 'folder-dark', folder: true },
    { theme: 'dark', width: 390, name: 'panel-mobile' },
    { theme: 'dark', width: 1280, name: 'dropped-folder-dark', drop: true },
    { theme: 'dark', width: 1280, name: 'no-picker-dark', noPicker: true },
  ]
  for (const s of shots) {
    const context = await browser.newContext({ viewport: { width: s.width, height: 900 }, deviceScaleFactor: 2 })
    const page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, { theme: s.theme, extra: extraFor(!s.noPicker), localStorageEntries: { 'mc-lang': 'en', 'mc-onboarded': '1', 'mc-privacy-notice-v1': '1' } })
    await page.goto(`${base}/capabilities?tab=knowledge`, { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2500)
    await page.getByRole('button', { name: /^Sources/ }).last().click()
    await page.getByText('+ Add Source').click()
    if (s.folder) {
      const choose = page.getByRole('button', { name: 'Choose folder' })
      if (await choose.count()) await choose.click()
      else await page.getByRole('button', { name: 'Local Folder' }).click()
    }
    if (s.drop) {
      // A page cannot build a real folder drop, so hand React a drop event
      // whose items say "directory", the way Chromium reports one.
      await page.getByText(/^Drop files/).evaluate(el => {
        const file = new File([''], 'notes')
        const ev = new Event('drop', { bubbles: true, cancelable: true })
        Object.defineProperty(ev, 'dataTransfer', { value: {
          files: [file],
          items: [{ kind: 'file', getAsFile: () => file, webkitGetAsEntry: () => ({ isDirectory: true, name: 'notes' }) }],
        } })
        el.closest('[role="button"]').dispatchEvent(ev)
      })
    }
    await page.waitForTimeout(500)
    const panel = page.getByText('Add Source', { exact: true }).locator('xpath=..')
    await panel.scrollIntoViewIfNeeded()
    await panel.screenshot({ path: resolve(OUT, `${PREFIX}-${s.name}.png`) })
    await context.close()
  }
} finally {
  await browser.close()
  srv.close()
}
console.log(`wrote ${OUT}`)

/**
 * Screenshot harness for PR #1910: "Org Mode (.org)" in the Knowledge
 * supported-formats copy.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback server with
 * /api/** answered by fixtures. Captures the two render sites of
 * `SUPPORTED_FORMATS_KEY`:
 *   1. the Knowledge help dialog (`ONBOARDING.steps`)
 *   2. the Sources add-source DropZone
 *
 * Doubles as a regression check: exits non-zero unless "Org Mode (.org)" is
 * visible text at both sites.
 *
 * Usage: node scripts/capture-knowledge-org-format.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/knowledge-org-format'
mkdirSync(OUT, { recursive: true })

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const failures = []

try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  logPageProblems(page)
  await stubDashboardApi(page, {
    extra: async (path, route) => {
      if (path === '/api/knowledge/sources') { await json(route, []); return true }
      if (path === '/api/knowledge/config') {
        await json(route, { enabled: true, supported_formats: ['md', 'txt', 'org'], folder_picker: false })
        return true
      }
      if (path === '/api/knowledge/namespaces') { await json(route, []); return true }
      if (path === '/api/knowledge/stats') {
        await json(route, {
          items: 0, entities: 0, relations: 0, sources: 0,
          embeddings: { enabled: true, available: true, model: 'bge-small', embedded_items: 0 },
        })
        return true
      }
      return false
    },
  })

  await page.goto(`${base}/knowledge`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(1000)

  const expect = (cond, msg) => { if (!cond) failures.push(msg) }

  // 1. Help dialog — the ONBOARDING.steps render site.
  const helpBtn = page.getByRole('button', { name: /help/i }).first()
  if (!(await helpBtn.count())) throw new Error('Help button not found')
  await helpBtn.click()
  const dialog = page.locator('[role="dialog"][aria-labelledby="help-title"]')
  await dialog.waitFor({ state: 'visible', timeout: 15_000 })
  await page.waitForTimeout(600)
  const dialogText = await dialog.innerText()
  expect(/Supported formats:/.test(dialogText), 'help dialog: supported-formats sentence missing')
  expect(/Org Mode \(\.org\)/.test(dialogText), 'help dialog: Org Mode (.org) missing')
  expect(/DOCX, Org Mode \(\.org\), PDF/.test(dialogText), 'help dialog: list order changed (must read DOCX, Org Mode (.org), PDF, matching the Sources info box)')
  await page.screenshot({ path: `${OUT}/help-dialog-org-mode.png` })
  await page.keyboard.press('Escape')
  await page.waitForTimeout(400)

  // 2. Sources DropZone — the second render site.
  const tab = page.getByRole('button', { name: /^sources$/i }).first()
  if (!(await tab.count())) throw new Error('Sources tab not found')
  await tab.click()
  await page.waitForTimeout(800)
  const addBtn = page.getByRole('button', { name: /add source/i }).first()
  if (await addBtn.count()) {
    await addBtn.click()
    await page.waitForTimeout(600)
  }
  const body = await page.locator('body').innerText()
  expect(/Drop files here or click to upload/i.test(body), 'DropZone not visible on Sources tab')
  expect(/Org Mode \(\.org\)/.test(body), 'DropZone: Org Mode (.org) missing')
  // The info box rendered directly under the DropZone must AGREE with it about
  // Org Mode AND PDF — two adjacent format lists disagreeing is the defect the
  // design and UX reviews flagged.
  expect(/Org Mode \(\.org\), PDF/.test(body), 'DropZone caption: PDF missing after Org Mode (.org)')
  expect(/Supports: .*Org Mode.*PDF.*Max 50 MB/s.test(body), 'supports info box: Org Mode/PDF missing')
  await page.screenshot({ path: `${OUT}/sources-dropzone-org-mode.png` })

  await page.close()
} finally {
  await browser.close()
  srv.close()
}

if (failures.length) {
  console.error('FAILURES:\n' + failures.map(f => `  - ${f}`).join('\n'))
  process.exit(1)
}
console.log('ok: both render sites show Org Mode (.org)')

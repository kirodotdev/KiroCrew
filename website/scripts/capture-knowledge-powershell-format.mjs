/**
 * Screenshot harness for the PowerShell Knowledge-format PR (#4959): the three
 * new extensions (.ps1, .psm1, .psd1) in the Knowledge supported-formats copy.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback server with
 * /api/** answered by fixtures. `/api/knowledge/config` serves the same list
 * the backend now derives from `FileReader.SUPPORTED`, so the frame shows what
 * a live gateway would render. Captures the Sources add-source DropZone (the
 * render site of the supported-formats copy users see when uploading).
 *
 * Doubles as a regression check: exits non-zero unless the three extensions
 * are visible text at the capture site.
 *
 * Usage: node scripts/capture-knowledge-powershell-format.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/knowledge-powershell-format'
mkdirSync(OUT, { recursive: true })

// sorted(FileReader.SUPPORTED - {''}) as /api/knowledge/config serves it,
// dots stripped -- the three PowerShell entries sit between pdf and py.
const SUPPORTED = [
  'c', 'cpp', 'csv', 'docx', 'go', 'h', 'htm', 'html', 'java', 'js',
  'json', 'jsonl', 'log', 'md', 'ndjson', 'org', 'pdf', 'ps1', 'psd1',
  'psm1', 'py', 'rb', 'rs', 'sh', 'ts', 'txt', 'yaml', 'yml',
]

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
        await json(route, { enabled: true, supported_formats: SUPPORTED, folder_picker: false })
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

  // Sources DropZone -- the render site of the supported-formats copy.
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
  for (const ext of ['ps1', 'psd1', 'psm1']) {
    expect(new RegExp(`\\b${ext}\\b`, 'i').test(body), `DropZone copy: ${ext} missing`)
  }
  await page.screenshot({ path: `${OUT}/sources-dropzone-powershell.png` })

  await page.close()
} finally {
  await browser.close()
  srv.close()
}

if (failures.length) {
  console.error('FAILURES:\n' + failures.map(f => `  - ${f}`).join('\n'))
  process.exit(1)
}
console.log('ok: DropZone copy shows ps1, psd1, psm1')

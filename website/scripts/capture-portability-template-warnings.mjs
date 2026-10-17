/**
 * Screenshot harness for the crew-template warnings on Settings > Imports.
 *
 * Runs the REAL built SPA (website/dist) behind the shared static server and
 * answers every /api/** call from fixtures, so no gateway and no auth is needed.
 * One shot: an export whose crews name templates the bundle does not carry, then
 * an import whose config names a template this machine lacks.
 *
 * Usage: node scripts/capture-portability-template-warnings.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../.github/screenshots/portability-template-warnings'
mkdirSync(OUT, { recursive: true })

const MANIFEST = {
  version: 1, created_at: '2026-09-28T16:00:00Z', hostname: 'laptop', user: 'alice',
  contents: { 'config.json': 4096, workspace_files: 12, skill_count: 3 },
}

async function extra(path, route) {
  if (path === '/api/portability/export') {
    await route.fulfill({
      status: 200,
      contentType: 'application/zip',
      headers: {
        'Content-Disposition': 'attachment; filename="kirocrew-export.zip"',
        'X-Kirocrew-Unbundled-Templates': JSON.stringify(['reviewer', 'triage-bot', '+4']),
      },
      body: 'PK',
    })
    return true
  }
  if (path === '/api/portability/preview') {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true, manifest: MANIFEST }) })
    return true
  }
  if (path === '/api/portability/import') {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        ok: true,
        manifest: MANIFEST,
        summary: {
          mode: 'merge', staging: 'pinned',
          items: ['memory (merged)', 'config (restored)', 'skills (merged, auto/ skipped)'],
          missing_agent_templates: [
            { crew: 'reviews', kiro_agent: 'reviewer' },
            { crew: 'triage', kiro_agent: 'triage-bot' },
          ],
          missing_agent_templates_more: 2,
        },
      }),
    })
    return true
  }
  return false
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1200, height: 1400 }, deviceScaleFactor: 2, acceptDownloads: true })
const page = await context.newPage()
logPageProblems(page)
await stubDashboardApi(page, { extra })

try {
  await page.goto(`${base}/settings?tab=imports`, { waitUntil: 'domcontentloaded' })
  const main$ = page.locator('#main-content')
  await main$.getByRole('button', { name: /download export/i }).click()
  const exportWarning = main$.getByTestId('portability-export-warning')
  await exportWarning.waitFor({ state: 'visible', timeout: 15000 })
  if (!(await exportWarning.textContent()).includes('reviewer, triage-bot, 4 more')) throw new Error('export warning text wrong')

  await main$.locator('#portability-import-file').setInputFiles({ name: 'kirocrew-export.zip', mimeType: 'application/zip', buffer: Buffer.from('PK') })
  const importBtn = main$.getByRole('button', { name: /^import$/i })
  await importBtn.waitFor({ state: 'visible' })
  await page.waitForFunction(() => {
    const b = [...document.querySelectorAll('#main-content button')].find(x => x.textContent?.trim() === 'Import')
    return b && !b.disabled
  }, null, { timeout: 15000 })
  await importBtn.click()
  const importWarning = main$.getByTestId('portability-import-warning')
  await importWarning.waitFor({ state: 'visible', timeout: 15000 })
  if (!(await importWarning.textContent()).includes('reviews \u2192 reviewer')) throw new Error('import warning text wrong')
  await page.waitForTimeout(300)
  await page.screenshot({ path: `${OUT}/after-warnings.png`, fullPage: true })
  console.log(`wrote ${OUT}/after-warnings.png`)
} finally {
  await browser.close()
  srv.close()
}

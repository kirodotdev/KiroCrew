/**
 * Screenshot harness for the Research Lab delete-campaign confirm dialog.
 *
 * Runs the REAL built SPA (website/dist) behind the shared static server and
 * answers every /api/** call from fixtures, so no gateway or kiro-cli is needed.
 * Captures the in-app destructive confirmation that replaced window.confirm
 * (the native confirm is synchronous and cannot be screenshotted headlessly —
 * which is fitting, since blocking the renderer is exactly why it had to go):
 *   01 dark  → campaign detail with the delete dialog open
 *   02 light → same surface, light theme
 *
 * Usage: node scripts/capture-research-delete-confirm.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/research-delete-confirm'

mkdirSync(OUT, { recursive: true })

const CAMPAIGN = {
  id: 'cafe0001', name: 'Rate limiting research',
  question: 'How do other teams handle API rate limiting effectively?',
  sub_questions: '[]', sources: '["web"]', max_cycles: 30, idle_secs: 60,
  status: 'stopped', total_cycles: 8,
  findings: [{
    cycle: 1, summary: 'Initial scan of rate-limiting approaches.',
    sources_checked: ['web:nginx-docs'], sources_empty: [],
    new_findings_count: 2, evidence_strength: 'strong',
    key_insight: 'Token bucket is the common baseline',
    verification: { passed: true },
  }],
}

const extra = (path, route) => {
  if (path === '/api/apps/auto-research/campaigns') return json(route, [CAMPAIGN]), true
  if (path === `/api/apps/auto-research/campaigns/${CAMPAIGN.id}`) return json(route, CAMPAIGN), true
  if (path.endsWith('/knowledge-status')) return json(route, { in_library: false }), true
  if (path.endsWith('/report-status')) return json(route, { slug: null }), true
  return false
}

async function capture(browser, theme, name) {
  const context = await browser.newContext({
    viewport: { width: 1400, height: 900 },
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()
  await stubDashboardApi(page, { theme, extra })
  logPageProblems(page)

  await page.goto(BASE + '/auto-research', { waitUntil: 'domcontentloaded' })
  await page.waitForSelector(`text=${CAMPAIGN.question}`, { timeout: 15000 })
  await page.click(`text=${CAMPAIGN.question}`)
  await page.waitForSelector('button:has-text("Delete")', { timeout: 10000 })
  await page.click('button:has-text("Delete")')
  await page.waitForSelector('[role="dialog"]', { timeout: 5000 })
  await page.waitForTimeout(500) // let the spring settle
  await page.screenshot({ path: `${OUT}/${name}.png` })
  console.log('wrote', `${OUT}/${name}.png`)
  await context.close()
}

let BASE
async function main() {
  const { srv, base } = await serveDist()
  BASE = base
  const browser = await chromium.launch()
  await capture(browser, 'dark', '01-delete-dialog-dark')
  await capture(browser, 'light', '02-delete-dialog-light')
  await browser.close()
  srv.close()
}

main().catch(e => { console.error(e); process.exit(1) })

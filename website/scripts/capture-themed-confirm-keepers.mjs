/**
 * Screenshot harness for the shared themed confirm dialog (#699 keeper sites).
 *
 * Runs the REAL built SPA (website/dist) behind the shared static server and
 * answers every /api/** call from fixtures, so no gateway or kiro-cli is needed.
 * Captures the in-app confirmations that replaced window.confirm (the native
 * confirm is synchronous and cannot be screenshotted headlessly — blocking the
 * renderer is exactly why it had to go):
 *   01 dark  → artifact detail: unsaved-edit Back guard (Group A)
 *   02 dark  → deploy page: irreversible destroy-site dialog (Group C)
 *   03 light → the destroy dialog again, light theme
 *
 * Usage: node scripts/capture-themed-confirm-keepers.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/themed-confirm-keepers'

mkdirSync(OUT, { recursive: true })

const ARTIFACT = {
  slug: 'cr-queue',
  name: 'CR Queue',
  kind: 'markdown',
  source: 'chat',
  description: 'Hourly CR snapshot',
  tags: ['ops'],
  version: 2,
  created_at: '2026-05-21T22:00:00.000000+00:00',
  updated_at: '2026-05-21T22:30:00.000000+00:00',
  content: '# CR Queue\n\nOpen reviews, refreshed hourly.',
}

const SITE = {
  site_id: 'blog', bucket: 'bkt-9f3', distribution_id: 'E1DIST',
  status: 'deployed', url: 'https://d111.cloudfront.net', profile: 'ship-prod',
}

const extra = (path, route) => {
  // Artifact detail surface
  if (path === '/api/artifacts/cr-queue') return json(route, ARTIFACT), true
  if (path === '/api/artifacts/cr-queue/versions') return json(route, { versions: [1, 2] }), true
  if (path === '/api/artifacts/cr-queue/events') return json(route, { events: [] }), true
  if (path === '/api/artifacts/cr-queue/comments') return json(route, { comments: [] }), true
  if (path === '/api/artifact-folders') return json(route, []), true
  // Deploy surface
  if (path === '/api/deploy/list') return json(route, { sites: [SITE], configured: true }), true
  if (path === '/api/deploy/pending') return json(route, { pending: [] }), true
  if (path === '/api/deploy/profiles') {
    return json(route, { profiles: [{ name: 'ship-prod', region: 'us-east-1', verified_at: 1 }], default: 'ship-prod', available: [] }), true
  }
  if (path === '/api/deploy/destroy') {
    return json(route, { resources: { bucket: 'bkt-9f3', distribution_id: 'E1DIST' } }), true
  }
  if (path === '/api/artifacts') return json(route, { artifacts: [] }), true
  return false
}

async function newPage(browser, theme) {
  const context = await browser.newContext({
    viewport: { width: 1400, height: 900 },
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()
  await stubDashboardApi(page, { theme, extra })
  logPageProblems(page)
  return { context, page }
}

async function captureArtifactGuard(browser, theme, name) {
  const { context, page } = await newPage(browser, theme)
  await page.goto(BASE + '/artifacts/cr-queue', { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('text=CR Queue', { timeout: 15000 })
  await page.click('[title="Edit content"]')
  // The Pierre edit surface takes real key events on its contenteditable —
  // fill() bypasses its input pipeline and never dirties the buffer. Wait for
  // the lazy Pierre chunk to replace the plain fallback before typing.
  const editor = page.locator('[contenteditable="true"]').first()
  await editor.waitFor({ timeout: 10000 })
  await page.waitForTimeout(1000)
  await page.locator('[contenteditable="true"] >> text=CR Queue').first().click()
  await page.keyboard.insertText('Edited-A ')
  await page.waitForTimeout(800)
  await editor.pressSequentially('Edited, not yet saved. ')
  await page.waitForFunction(
    () => document.body.textContent?.includes('unsaved'),
    undefined, { timeout: 5000 },
  )
  await page.click('button:has-text("Back")')
  await page.waitForSelector('[role="dialog"]', { timeout: 5000 })
  await page.waitForTimeout(500) // let the spring settle
  await page.screenshot({ path: `${OUT}/${name}.png` })
  console.log('wrote', `${OUT}/${name}.png`)
  await context.close()
}

async function captureDestroyDialog(browser, theme, name) {
  const { context, page } = await newPage(browser, theme)
  await page.goto(BASE + '/deploy', { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('text=blog', { timeout: 15000 })
  await page.click('button:has-text("Destroy")')
  await page.waitForSelector('[role="dialog"]', { timeout: 5000 })
  await page.waitForTimeout(500)
  await page.screenshot({ path: `${OUT}/${name}.png` })
  console.log('wrote', `${OUT}/${name}.png`)
  await context.close()
}

let BASE
async function main() {
  const { srv, base } = await serveDist()
  BASE = base
  const browser = await chromium.launch()
  await captureArtifactGuard(browser, 'dark', '01-artifact-discard-guard-dark')
  await captureDestroyDialog(browser, 'dark', '02-deploy-destroy-dark')
  await captureDestroyDialog(browser, 'light', '03-deploy-destroy-light')
  await browser.close()
  srv.close()
}

main().catch(e => { console.error(e); process.exit(1) })

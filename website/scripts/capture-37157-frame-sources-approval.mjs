/**
 * Screenshot harness for the staged "Framed sites" row on the app detail page.
 *
 * Runs the REAL built SPA behind the shared `serveDist` server and answers every
 * /api/** call from fixtures through `stubDashboardApi`. No gateway, no dashboard
 * auth, no kiro-cli.
 *
 * The fixture is an installed, enabled app whose update added a `ui.frameSources`
 * origin, so the row carries `frameSourcesGrant.staged`. The page must list the
 * origin as a chip under the "Framed sites" label with the approve button; after
 * the approve click the enable route answers ok, the next read has nothing
 * staged, and the page shows "Approved: <origin>". Then, on the app's own page, a
 * declared frame refused by the document's older policy must show the notice that
 * names the origin and offers "Reload page", without reloading by itself; while the
 * origin still waits for approval, it offers "Open app details" instead. Light and
 * dark each.
 *
 * Usage: node scripts/capture-37157-frame-sources-approval.mjs [outDir] [--dist=path]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

import { json } from './lib/boot-api.mjs'
import { serveDist, DEFAULT_DIST } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const args = process.argv.slice(2)
const flag = (name, fallback) => {
  const hit = args.find(a => a.startsWith(`--${name}=`))
  return hit ? hit.slice(name.length + 3) : fallback
}
const OUT = args.find(a => !a.startsWith('--')) || '/tmp/frame-sources-approval-shots'
const DIST = flag('dist', DEFAULT_DIST)
mkdirSync(OUT, { recursive: true })

const APP = 'mood-music'
const OLD = 'https://www.youtube-nocookie.com'
const NEW = 'https://player.bilibili.com'

const installedApp = staged => ({
  name: APP, displayName: 'Mood Music', version: '1.1.0', enabled: true,
  installedAt: '2026-11-14T10:00:00Z', origin: 'registry',
  resources: 'gateway', lifecycle: 'gateway',
  frameSourcesGrant: staged ? { approved: [OLD], staged: [NEW] } : { approved: [OLD, NEW], staged: [] },
  manifest: {
    name: APP, version: '1.1.0', displayName: 'Mood Music',
    description: 'Music picks for how you feel.',
    author: 'iamwhatever', tags: ['music'],
    ui: { entry: 'index.js', pages: [{ route: `/${APP}`, label: 'Mood Music' }], frameSources: [OLD, NEW] },
  },
})

const { srv, base } = await serveDist(DIST)
const browser = await chromium.launch()

function fail(msg) { throw new Error(`ASSERTION FAILED: ${msg}`) }

async function shoot(mode) {
  let staged = true
  let consent = null
  const context = await browser.newContext({
    viewport: { width: 1400, height: 1000 }, deviceScaleFactor: 2, serviceWorkers: 'block', colorScheme: mode,
  })
  const page = await context.newPage()
  page.on('pageerror', err => console.log('PAGEERROR:', (err.stack || String(err)).slice(0, 600)))
  await stubDashboardApi(page, {
    theme: mode,
    localStorageEntries: { 'mc-onboarded': '1', 'mc-theme-mode': mode },
    extra: async (path, route) => {
      if (path === `/api/apps/${APP}/enable`) {
        consent = route.request().postDataJSON()
        staged = false
        return json(route, { ok: true }), true
      }
      if (path === '/api/apps/registry') return json(route, { apps: [], serverPlatform: { os: 'linux', arch: 'x86_64' } }), true
      if (path === '/api/apps/registries') return json(route, { registries: [] }), true
      if (path === `/api/apps/${APP}`) return json(route, installedApp(staged)), true
      if (path === '/api/apps') return json(route, [installedApp(staged)]), true
      if (path === '/api/source/contributors') return json(route, { contributors: [] }), true
      return false
    },
  })

  await page.goto(`${base}/apps/detail/${APP}`, { waitUntil: 'domcontentloaded' })
  const chips = page.getByTestId('staged-frame-sources')
  await chips.waitFor({ timeout: 15000 })
  await page.waitForTimeout(1200)
  if (await chips.getByText(NEW, { exact: true }).count() !== 1) fail(`staged chip ${NEW} missing`)
  if (await chips.getByText(OLD, { exact: true }).count() !== 0) fail(`approved ${OLD} must not be offered`)
  await page.getByText('Framed sites:').first().waitFor()
  await page.screenshot({ path: `${OUT}/frame-sources-staged-${mode}.png`, fullPage: false })
  console.log(`wrote ${OUT}/frame-sources-staged-${mode}.png`)

  await page.getByRole('button', { name: /approve/i }).first().click()
  await page.getByText(`Approved: ${NEW}`, { exact: false }).first().waitFor({ timeout: 10000 })
  if (JSON.stringify(consent?.grantsConsent?.frameSources) !== JSON.stringify([NEW])) {
    fail(`approve sent frameSources=${JSON.stringify(consent?.grantsConsent?.frameSources)}`)
  }
  if (await page.getByTestId('staged-frame-sources').count() !== 0) fail('staged row still shown after approval')
  await page.waitForTimeout(800)
  await page.screenshot({ path: `${OUT}/frame-sources-approved-${mode}.png`, fullPage: false })
  console.log(`wrote ${OUT}/frame-sources-approved-${mode}.png`)

  // The app's own page: a declared frame refused by this document's older policy.
  await page.route(`**/apps/${APP}/ui/index.js`, route => route.fulfill({
    status: 200, contentType: 'text/javascript',
    body: 'export default function App() { return null }',
  }))
  await page.goto(`${base}/apps/${APP}`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(1500)
  await page.evaluate(origin => {
    window.__reloads = 0
    document.dispatchEvent(Object.assign(new Event('securitypolicyviolation'), {
      effectiveDirective: 'frame-src', blockedURI: `${origin}/player/1`, originalPolicy: "frame-src 'self'",
    }))
  }, NEW)
  const notice = page.getByTestId('app-host-frame-blocked')
  await notice.waitFor({ timeout: 10000 })
  if (await notice.getByText(NEW).count() === 0) fail('the notice does not name the origin')
  if (await notice.getByRole('button', { name: /reload page/i }).count() !== 1) fail('no Reload page button')
  await page.waitForTimeout(600)
  // Still the same document: the notice waits for the owner instead of reloading.
  if (await page.evaluate(() => window.__reloads) !== 0) fail('the page reloaded by itself')
  await page.screenshot({ path: `${OUT}/frame-blocked-refresh-${mode}.png`, fullPage: false })
  console.log(`wrote ${OUT}/frame-blocked-refresh-${mode}.png`)

  // The same refusal while the origin still waits for approval: a reload would be
  // refused again, so the notice points to the details page instead.
  staged = true
  await page.goto(`${base}/apps/${APP}`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(1500)
  await page.evaluate(origin => {
    document.dispatchEvent(Object.assign(new Event('securitypolicyviolation'), {
      effectiveDirective: 'frame-src', blockedURI: `${origin}/player/1`, originalPolicy: "frame-src 'self'",
    }))
  }, NEW)
  const pending = page.getByTestId('app-host-frame-blocked')
  await pending.waitFor({ timeout: 10000 })
  if (await pending.getByRole('button', { name: /open app details/i }).count() !== 1) fail('no Open app details button')
  if (await pending.getByRole('button', { name: /reload page/i }).count() !== 0) fail('a reload is offered for an unapproved origin')
  await page.waitForTimeout(600)
  await page.screenshot({ path: `${OUT}/frame-needs-approval-${mode}.png`, fullPage: false })
  console.log(`wrote ${OUT}/frame-needs-approval-${mode}.png`)
  await context.close()
}

await shoot('light')
await shoot('dark')
console.log('OK staged chip, approve consent, approved message, refresh prompt and approval prompt in light and dark')

await browser.close()
srv.close()

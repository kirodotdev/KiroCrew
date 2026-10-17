/**
 * Screenshot harness for the registry review tier on the install consent gate
 * (#11159).
 *
 * Runs the REAL built SPA behind the shared static server and answers every
 * /api/** call from fixtures, including the SSE install stream, so no gateway
 * is needed. The app is listed by a build-pinned registry; the install is
 * refused by the execution gate, which opens the consent modal.
 *
 *   01 community-index  → a `community` registry pinned at the credential-free
 *                         `index` tier: the not-vetted box, without the Git
 *                         credentials clause
 *   02 community-owner  → the same registry at the credentialed `owner` tier:
 *                         the not-vetted box names the Git credentials
 *   03 curated          → a `curated` registry: no box, the dialog as before
 *   04 community-dark   → 01 in the dark theme (01-03 and 05 are light)
 *   05 community-narrow → 01 at a 360px phone width
 *   06 registries-loading → the registries list never answers: the tier is
 *                         unknown, so the box shows (credential-free copy)
 *   07 registries-error → the registries list fails (500): same, the box
 *                         shows; the confirm button is unchanged in both
 *
 * Every frame first asserts that the existing consent lines are still there,
 * so a screenshot can never show a state the run did not verify.
 *
 * Usage: node scripts/capture-trust-modal-registry-tier.mjs [outDir] [prefix] [dist]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist, DEFAULT_DIST } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/trust-modal-registry-tier'
const PREFIX = process.argv[3] || 'after'
const DIST = process.argv[4] || DEFAULT_DIST

mkdirSync(OUT, { recursive: true })

const APP = 'weather-board'
const DISPLAY = 'Weather Board'
const REPO = 'https://git.example.test/community/weather-board.git'
const REGISTRY = 'community-apps'

const NOT_VETTED = 'Not vetted'
const LISTING = 'the Kiro Crew team did not vet this listing'
const CREDENTIALS = 'downloads it with your saved Git sign-in'
/** Lines of the consent body that must render unchanged at every tier. */
const KEPT = [
  'has not reviewed its code',
  'Run its own code inside Kiro Crew',
  'Trusting the app grants all three',
  'changes nothing: nothing is installed',
  'You can take this back at any time',
]

const REGISTRY_APP = {
  name: APP, displayName: DISPLAY, version: '1.0.0',
  description: 'Local forecasts on a board.', author: 'community-dev',
  repo: REPO, gitUrl: REPO, trustRepository: REPO, tags: ['weather'],
  installed: false, enabled: false, origin: 'registry', updateAvailable: false,
  _registry: REGISTRY,
  manifest: { name: APP, version: '1.0.0', displayName: DISPLAY },
}

const DENIED = {
  ok: false, name: APP, code: 'app_execution_denied',
  error: `blocked by execution policy: App ${APP} is not trusted to run its own code.`, log: '',
}
const sse = (payload) => `event: done\ndata: ${JSON.stringify(payload)}\n\n`

async function openModal(context, base, { review, trust, registries }, theme) {
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    theme,
    extra: async (path, route) => {
      if (path === '/api/apps') { await route.fulfill({ json: [] }); return true }
      if (path === `/api/apps/${APP}`) { await route.fulfill({ status: 404, json: { error: 'app not installed' } }); return true }
      if (path === '/api/apps/registry') {
        await route.fulfill({ json: { apps: [REGISTRY_APP], serverPlatform: { os: 'darwin', arch: 'arm64' } } })
        return true
      }
      if (path === '/api/apps/registries') {
        // Loading: leave the request unanswered for the whole frame.
        if (registries === 'loading') return true
        if (registries === 'error') { await route.fulfill({ status: 500, json: { error: 'registries unavailable' } }); return true }
        await route.fulfill({
          json: {
            registries: [],
            pinned: [{ name: REGISTRY, label: 'Community apps', repo: 'https://git.example.test/community/index.git', branch: 'main', trust, review }],
          },
        })
        return true
      }
      if (path === '/api/apps/registry/install-stream') {
        await route.fulfill({ status: 200, contentType: 'text/event-stream', body: sse(DENIED) })
        return true
      }
      return false
    },
  })
  await page.goto(`${base}/apps/detail/${APP}`, { waitUntil: 'domcontentloaded' })
  await page.getByText(DISPLAY).first().waitFor({ timeout: 15000 })
  await page.getByRole('button', { name: 'Install', exact: true }).first().click()
  const modal = page.locator('[role="dialog"]').filter({ hasText: /to run its own code\?/ })
  await modal.waitFor({ timeout: 15000 })
  await page.waitForTimeout(500)
  return { page, modal }
}

const fail = (msg, text) => { throw new Error(`${PREFIX}: ${msg}: ${JSON.stringify(text)}`) }

async function frame(context, base, name, tier, expect, theme = 'light') {
  const { page, modal } = await openModal(context, base, tier, theme)
  const text = await modal.innerText()
  for (const line of KEPT) if (!text.includes(line)) fail(`consent line missing: ${line}`, text)
  if (expect.notVetted !== text.includes(LISTING)) fail(`not-vetted sentence expected=${expect.notVetted}`, text)
  if (expect.notVetted !== text.includes(NOT_VETTED)) fail(`not-vetted badge expected=${expect.notVetted}`, text)
  if (expect.credentials !== text.includes(CREDENTIALS)) fail(`credentials clause expected=${expect.credentials}`, text)
  if (text.includes('Team reviewed')) fail('the gate must not add an endorsement', text)
  // The source badge names the registry the box names, never the first-party
  // fallback beside a not-vetted warning (#20656).
  // Every frame's app is listed by a third-party registry, whatever its tier.
  if (text.includes('Kiro Crew registry')) fail('badge shows the first-party fallback for a third-party registry', text)
  if (!(await modal.getByText(REGISTRY, { exact: true }).count())) fail('badge does not name the registry', text)
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)
  if (overflow) fail('horizontal overflow', text)
  // The tier is display only: the confirm button is never held or disabled.
  const confirm = modal.getByRole('button', { name: 'Trust this app' })
  if (!(await confirm.isEnabled()) || (await confirm.getAttribute('aria-busy'))) fail('confirm button changed', text)
  // A phone-width dialog scrolls inside itself: bring the box into the frame.
  if (expect.notVetted) await page.getByTestId('trust-app-registry-community').scrollIntoViewIfNeeded()
  await modal.screenshot({ path: `${OUT}/${PREFIX}-${name}.png` })
  await page.close()
}

async function main() {
  const { srv, base } = await serveDist(DIST)
  const browser = await chromium.launch()
  try {
    const light = await browser.newContext({ viewport: { width: 1280, height: 1000 } })
    await frame(light, base, '01-community-index', { review: 'community', trust: 'index' }, { notVetted: true, credentials: false })
    await frame(light, base, '02-community-owner', { review: 'community', trust: 'owner' }, { notVetted: true, credentials: true })
    await frame(light, base, '03-curated', { review: 'curated', trust: 'owner' }, { notVetted: false, credentials: false })
    const dark = await browser.newContext({ viewport: { width: 1280, height: 1000 } })
    await frame(dark, base, '04-community-dark', { review: 'community', trust: 'index' }, { notVetted: true, credentials: false }, 'dark')
    const narrow = await browser.newContext({ viewport: { width: 360, height: 800 } })
    await frame(narrow, base, '05-community-narrow', { review: 'community', trust: 'index' }, { notVetted: true, credentials: false })
    // Unknown tier: the list WOULD say curated (no box), but it has not landed.
    await frame(light, base, '06-registries-loading', { review: 'curated', trust: 'owner', registries: 'loading' }, { notVetted: true, credentials: false })
    await frame(light, base, '07-registries-error', { review: 'curated', trust: 'owner', registries: 'error' }, { notVetted: true, credentials: false })
  } finally {
    await browser.close()
    srv.close()
  }
  console.log(`Wrote ${OUT}/${PREFIX}-0{1..7}-*.png`)
}

main().catch(e => { console.error(e); process.exit(1) })

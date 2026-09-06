/**
 * Screenshot harness for Settings > Security's flagged-file delivery section.
 *
 * Same shape as capture-security-inspector.mjs: serves the REAL built SPA
 * (website/dist) and answers /api/** from the shared fixture router, with the
 * security endpoints supplied here because the default table has no security
 * routes (unmatched paths fall through to `[]`, which would render every pane in
 * its empty state).
 *
 * The states worth a frame are the ones a reviewer cannot infer from the diff:
 * the grant absent, the grant held (which is the only state showing a timestamp
 * and the withdraw control), and the READ FAILED state -- that last one is the
 * point of the card's error handling, because an unreadable authorization must
 * render as unknown rather than as "Not confirmed".
 *
 * Builds the SPA first: serve-dist serves whatever is on disk, so shooting a
 * UI-only change against a stale dist yields an "after" image identical to
 * before -- indistinguishable from the change not working.
 *
 * Usage: node scripts/capture-file-delivery-consent.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { serveDist } from './lib/serve-dist.mjs'
import { installApiFixtures, logPageFailures } from './lib/api-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/file-delivery-consent'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

const CONSENT_PATH = '/api/file-delivery/consent'
const ARM_PATH = '/api/file-delivery/consent/arm'

/** The GET's real shape: server-owned lists, and `grants` keyed by class with
 *  null meaning "not confirmed". The label is the backend's plain-language one. */
const consent = (grantedAt = null) => ({
  ok: true,
  grantable: ['owner_dashboard'],
  never_grantable: ['channel_upload', 'slack_upload'],
  labels: { owner_dashboard: 'This computer and your dashboard Files view' },
  grants: {
    owner_dashboard: grantedAt ? { destination_class: 'owner_dashboard', granted_at: grantedAt } : null,
  },
})

/** The arm-status GET's shape. `armed:false` is the resting state; the armed
 *  view carries the host command but never a nonce. */
const notArmed = { ok: true, armed: false }
const armedView = {
  ok: true, armed: true, request_id: 'req-1', destination_class: 'owner_dashboard',
  expires_in: 600, approve_command: 'kirocrew file-delivery approve',
}

const FIXTURES = {
  '/api/security/posture': { controls: [], counts: {} },
  '/api/security/denied-commands': {
    builtins: [], user_added: [], disable_all: false, effective_count: 0, governance_locked: false,
  },
  '/api/governance/policy': {
    version: null, has_policy: false, profile: null, unavailable: false, scopes: [],
  },
  '/api/config/kirocrew': { agent: { yolo_duration: '6h', apps_allow_third_party: false } },
  '/api/tailnet/status': {
    enabled: false, governance_pinned: false, host: '', origin: '', resolved_at: 0, state: 'off',
  },
}

async function main() {
  if (!process.env.SKIP_BUILD) {
    console.log('building dist (SKIP_BUILD=1 to reuse)…')
    execFileSync('npm', ['run', 'build'], { stdio: 'inherit', shell: process.platform === 'win32' })
  }

  const { srv, base } = await serveDist()
  const browser = await chromium.launch()

  async function shoot(name, { width = 1500, height = 980, theme = 'dark', consentBody, failConsent = false, armBody = notArmed, failSave = false, failArmStatus = false }) {
    const context = await browser.newContext({
      viewport: { width, height },
      // Settings rows are 12-13px type; a 1x shot renders soft on GitHub.
      deviceScaleFactor: 2,
    })
    const page = await context.newPage()
    await installApiFixtures(page, {
      ...FIXTURES,
      '/api/theme/boot': { mode: theme, theme: '' },
      ...(failConsent ? {} : { [CONSENT_PATH]: consentBody }),
    })
    // The arm-status GET shares a path PREFIX with the consent GET, so it is
    // matched by its own explicit route to render the armed step-up panel. A GET
    // to /consent/arm returns the armed view. Regex so it matches with or without
    // a query string.
    await page.route(/\/api\/file-delivery\/consent\/arm(\?|$)/, route => {
      if (route.request().method() !== 'GET') return route.continue()
      // The arm-status read-failure state the UX review asked for: the consent
      // GET still succeeds (so the row renders) but the arm-status GET 500s, so
      // "Could not check whether an approval is in progress." surfaces.
      if (failArmStatus) {
        return route.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"unreachable"}' })
      }
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(armBody) })
    })
    // Registered AFTER the fixture router so it wins: Playwright matches the most
    // recently added route first. This is the only way to render the failed-read
    // branch, which the fixture table (always 200) cannot express.
    if (failConsent) {
      await page.route(/\/api\/file-delivery\/consent(\?|$)/, route =>
        route.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"unreachable"}' }))
    }
    // The write-failure state the UX review asked for: the consent GET succeeds
    // (so the row renders) but the arming POST fails, so "Could not save that
    // change." surfaces. The arm POST carries a `?destination_class=` query, so
    // the route must match the query too (a plain path glob would miss it and the
    // POST would fall through to the always-200 fixture router).
    if (failSave) {
      await page.route(/\/api\/file-delivery\/consent(\?|$)/, route => {
        if (route.request().method() === 'POST') {
          return route.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"nope"}' })
        }
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(consentBody) })
      })
    }
    logPageFailures(page)
    await page.addInitScript(t => {
      localStorage.clear()
      localStorage.setItem('mc-theme', t)
      localStorage.setItem('mc-onboarded', '1')
      // The app shell reads the Electron updater bridge during boot and does not
      // tolerate its absence in a plain browser. Same stub the sibling harnesses
      // install.
      window.updateAPI = {
        onState: () => () => {},
        check: async () => ({ ok: true }),
        download: async () => ({ ok: true }),
        install: async () => ({ ok: true }),
        getInfo: async () => ({
          version: '0.5.0', channel: 'stable', stampedChannel: 'stable',
          channelSwitchable: true, channelPreference: '',
          platform: 'darwin-arm64', packaged: true,
        }),
        setChannel: async () => ({ ok: true }),
      }
    }, theme)

    // Path-routed, NOT hash-routed: serve-dist has an index.html fallback so
    // /settings resolves.
    await page.goto(`${base}/settings?tab=security&section=delivery`, { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(1800)
    // The write-failure state only appears after the arming POST is attempted, so
    // click Allow delivery and wait for the error notice to surface.
    if (failSave) {
      const allow = page.getByRole('button', { name: 'Allow delivery' })
      if (!(await allow.count())) {
        throw new Error(`shoot(${name}): no "Allow delivery" control to click for the save-failure state`)
      }
      await allow.first().click()
      // Wait for the actual failure notice rather than a fixed delay, and FAIL if
      // it never renders: a plausible frame of the wrong state is worse than no
      // frame, because it satisfies the gate and misleads the reviewer.
      await page.getByText('Could not save that change.').waitFor({ timeout: 5000 })
    }
    if (failArmStatus) {
      // Assert the arm-status read-failure notice actually rendered before the
      // shot. No fixed-delay fallback and no swallowed error: if the notice is
      // absent the harness throws rather than emitting a misleading capture.
      await page.getByText('Could not check whether an approval is in progress.').waitFor({ timeout: 5000 })
    }
    await page.screenshot({ path: `${OUT}/${PREFIX}-${name}.png` })
    console.log(`${PREFIX}-${name}.png`)
    await context.close()
  }

  await shoot('not-confirmed', { consentBody: consent() })
  await shoot('confirmed', { consentBody: consent('2026-09-05T23:00:06+00:00') })
  // The armed step-up: Allow delivery clicked, grant NOT yet recorded, the host
  // command shown. This is the security fix's visible surface.
  await shoot('armed', { consentBody: consent(), armBody: armedView })
  // The write-failure state the UX review explicitly asked for.
  await shoot('save-failed', { consentBody: consent(), failSave: true })
  // The arm-status read-failure notice this PR adds, which the UX review flagged
  // as appearing in no committed screenshot.
  await shoot('arm-status-failed', { consentBody: consent(), failArmStatus: true })
  await shoot('read-failed', { failConsent: true })
  await shoot('not-confirmed-light', { consentBody: consent(), theme: 'light' })
  // The breakpoint below which the rail stacks above the pane.
  await shoot('narrow', { width: 900, height: 1000, consentBody: consent() })

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })

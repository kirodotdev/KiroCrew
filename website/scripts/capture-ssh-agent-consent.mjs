/**
 * Screenshot harness for Settings > Security's SSH agent forwarding section.
 *
 * Same shape as capture-file-delivery-consent.mjs: serves the REAL built SPA
 * (website/dist) and answers /api/** from the shared fixture router, with the
 * ssh-agent consent endpoints supplied here.
 *
 * The states worth a frame are the ones a reviewer cannot infer from the diff:
 * the grant absent, the armed step-up (Allow clicked, host command shown, nothing
 * granted yet), the grant held (timestamp + Revoke), the no-socket note, and the
 * READ FAILED state, because an unreadable authorization must render as unknown
 * rather than as "Not allowed".
 *
 * Builds the SPA first: serve-dist serves whatever is on disk, so shooting a
 * UI-only change against a stale dist yields an "after" image identical to
 * before -- indistinguishable from the change not working.
 *
 * Usage: node scripts/capture-ssh-agent-consent.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { serveDist } from './lib/serve-dist.mjs'
import { installApiFixtures, logPageFailures } from './lib/api-fixtures.mjs'
import { SECURITY_RAIL_FIXTURES } from './lib/security-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/ssh-agent-consent'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

const CONSENT_PATH = '/api/ssh-agent/consent'

/** The GET's real shape. `granted_at` is null for a hand-written grant. */
const consent = ({ granted = false, grantedAt = null, socketPresent = true } = {}) => ({
  granted,
  granted_at: granted ? grantedAt : null,
  socket_present: socketPresent,
})

/** The arm-status GET's shape. `armed:false` is the resting state; the armed
 *  view carries the host command but never a nonce. */
const notArmed = { armed: false, request_id: null, expires_in: null, approve_command: 'kirocrew ssh-agent approve' }
const armedView = { armed: true, request_id: 'req-1', expires_in: 600, approve_command: 'kirocrew ssh-agent approve' }

const FIXTURES = SECURITY_RAIL_FIXTURES

async function main() {
  if (!process.env.SKIP_BUILD) {
    console.log('building dist (SKIP_BUILD=1 to reuse)…')
    execFileSync('npm', ['run', 'build'], { stdio: 'inherit', shell: process.platform === 'win32' })
  }

  const { srv, base } = await serveDist()
  const browser = await chromium.launch()

  async function shoot(name, { width = 1500, height = 980, theme = 'dark', consentBody, failConsent = false, armBody = notArmed, cancel = false, armExpiring = false }) {
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
    // The arm-status GET shares a path PREFIX with the consent GET, so it gets
    // its own explicit route. Regex so it matches with or without a query string.
    // The two end-of-request states drive the REAL `wasArmed && !isArmed`
    // branch through the same GET the backend owns: the arm-status poll reports
    // armed once, then not. For `cancel` the flip follows the owner's DELETE;
    // for `armExpiring` it follows on its own, as when the ~10-minute window
    // lapses. Nothing in the feature is altered to reach either frame.
    let armStatusPolls = 0
    let deleted = false
    await page.route(/\/api\/ssh-agent\/consent\/arm(\?|$)/, route => {
      if (route.request().method() !== 'GET') return route.continue()
      let body = armBody
      if (cancel) body = deleted ? notArmed : armedView
      if (armExpiring) {
        armStatusPolls += 1
        body = armStatusPolls <= 1 ? armedView : notArmed
      }
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
    })
    if (cancel) {
      await page.route(/\/api\/ssh-agent\/consent(\?|$)/, route => {
        if (route.request().method() === 'DELETE') {
          deleted = true
          return route.fulfill({ status: 200, contentType: 'application/json', body: '{"granted":false}' })
        }
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(consentBody) })
      })
    }
    // Registered AFTER the fixture router so it wins: Playwright matches the most
    // recently added route first.
    if (failConsent) {
      await page.route(/\/api\/ssh-agent\/consent(\?|$)/, route =>
        route.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"unreachable"}' }))
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

    await page.goto(`${base}/settings?tab=security&section=ssh_agent`, { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(1800)
    // Assert the section actually rendered before the shot: a plausible frame of
    // the wrong state satisfies the gate and misleads the reviewer.
    if (!failConsent && !(await page.getByTestId('ssh-agent-row').count())) {
      throw new Error(`shoot(${name}): the ssh-agent row did not render`)
    }
    if (cancel) {
      const btn = page.getByTestId('ssh-agent-cancel')
      if (!(await btn.count())) throw new Error(`shoot(${name}): no Cancel control in the armed block`)
      await btn.first().click()
      // Assert the real cancelled notice rendered before the shot.
      await page.getByText('Request cancelled. Nothing was allowed.').waitFor({ timeout: 8000 })
    }
    if (armExpiring) {
      await page.getByText('That request ended before it was allowed. Allow SSH agent again to start over.').waitFor({ timeout: 10000 })
    }
    await page.screenshot({ path: `${OUT}/${PREFIX}-${name}.png` })
    console.log(`${PREFIX}-${name}.png`)
    await context.close()
  }

  await shoot('not-allowed', { consentBody: consent() })
  // The armed step-up: Allow clicked, grant NOT yet recorded, the host command shown.
  await shoot('armed', { consentBody: consent(), armBody: armedView })
  await shoot('allowed', { consentBody: consent({ granted: true, grantedAt: '2026-10-09T07:40:00+00:00' }) })
  // A hand-written {"enabled": true} store: granted, no timestamp.
  await shoot('allowed-hand-written', { consentBody: consent({ granted: true }) })
  await shoot('no-socket', { consentBody: consent({ socketPresent: false }) })
  // The owner's Cancel: the armed block gives way to "Request cancelled."
  await shoot('cancelled', { consentBody: consent(), cancel: true })
  // The window lapsed with no approve and no Cancel: the expiry wording.
  await shoot('armed-expired', { consentBody: consent(), armExpiring: true })
  await shoot('read-failed', { failConsent: true })
  await shoot('not-allowed-light', { consentBody: consent(), theme: 'light' })

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })

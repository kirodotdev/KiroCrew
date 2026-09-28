/**
 * Screenshot harness for Settings > Security's credential-redaction section.
 *
 * Same shape as capture-file-delivery-consent.mjs: serves the REAL built SPA
 * (website/dist) and answers /api/** from the shared fixture router, with the
 * security endpoints supplied here because the default table has no security
 * routes.
 *
 * The states worth a frame are the ones a reviewer cannot infer from the diff:
 * the default (ON, no notice), the switch OFF (the loud notice with the time it
 * was switched off), and the READ FAILED state -- an unreadable switch must
 * render as unknown rather than as the reassuring default.
 *
 * Usage: node scripts/capture-credential-redaction.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { serveDist } from './lib/serve-dist.mjs'
import { installApiFixtures, logPageFailures } from './lib/api-fixtures.mjs'
import { SECURITY_RAIL_FIXTURES } from './lib/security-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/credential-redaction'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

const SWITCH_PATH = '/api/security/credential-redaction'

async function main() {
  // serve-dist serves whatever is on disk, so a UI-only change shot against a
  // stale dist yields an "after" identical to before; build unless told not to.
  if (!process.env.SKIP_BUILD) execFileSync('npm', ['run', 'build'], { stdio: 'inherit', shell: process.platform === 'win32' })
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()

  async function shoot(name, { width = 1500, height = 980, theme = 'dark', state, failRead = false, nonOwner = false, failWrite = false }) {
    const context = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: 2 })
    const page = await context.newPage()
    await installApiFixtures(page, {
      ...SECURITY_RAIL_FIXTURES,
      '/api/theme/boot': { mode: theme, theme: '' },
      ...(failRead ? {} : { [SWITCH_PATH]: state }),
    })
    // Registered AFTER the fixture router so it wins. The only way to render the
    // failed-read branch, which the always-200 fixture table cannot express.
    if (failRead) {
      await page.route(/\/api\/security\/credential-redaction(\?|$)/, route =>
        route.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"unreachable"}' }))
    }
    // A non-owner: the owner-gated GET refuses with 403 and the card must say so
    // rather than offer a Retry that can never succeed.
    if (nonOwner) {
      await page.route(/\/api\/security\/credential-redaction(\?|$)/, route =>
        route.fulfill({ status: 403, contentType: 'application/json', body: '{"error":"dashboard owner required","code":"dashboard_owner_required"}' }))
    }
    // A write that fails: the GET keeps answering, the PUT refuses, and the card
    // must show the position still in force with the save-failed notice.
    if (failWrite) {
      await page.route(/\/api\/security\/credential-redaction(\?|$)/, route => {
        if (route.request().method() === 'PUT') {
          return route.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"could not record the switch","code":"credential_redaction_write_failed"}' })
        }
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(state) })
      })
    }
    logPageFailures(page)
    await page.addInitScript(t => {
      localStorage.clear()
      localStorage.setItem('mc-theme', t)
      localStorage.setItem('mc-onboarded', '1')
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
    await page.goto(`${base}/settings?tab=security&section=redaction`, { waitUntil: 'domcontentloaded' })
    // Assert the state actually rendered before the shot: a plausible frame of the
    // wrong state satisfies the gate and misleads the reviewer.
    if (failRead) {
      await page.getByTestId('credential-redaction-read-failed').waitFor({ timeout: 8000 })
    } else if (nonOwner) {
      await page.getByTestId('credential-redaction-owner-only').waitFor({ timeout: 8000 })
    } else if (failWrite) {
      await page.getByTestId('credential-redaction-row').waitFor({ timeout: 8000 })
      await page.getByRole('switch').click()
      await page.getByTestId('credential-redaction-write-failed').waitFor({ timeout: 8000 })
    } else {
      await page.getByTestId('credential-redaction-row').waitFor({ timeout: 8000 })
      if (state.enabled === false) {
        await page.getByTestId('credential-redaction-off-notice').waitFor({ timeout: 5000 })
      }
    }
    await page.waitForTimeout(400)
    await page.screenshot({ path: `${OUT}/${PREFIX}-${name}.png` })
    console.log(`${PREFIX}-${name}.png`)
    await context.close()
  }

  await shoot('on', { state: { enabled: true, changed_at: '' } })
  await shoot('off', { state: { enabled: false, changed_at: '2026-09-24T23:00:00+00:00' } })
  await shoot('read-failed', { failRead: true })
  await shoot('non-owner', { nonOwner: true })
  await shoot('save-failed', { state: { enabled: true, changed_at: '' }, failWrite: true })
  await shoot('on-light', { state: { enabled: true, changed_at: '' }, theme: 'light' })
  // The breakpoint below which the rail stacks above the pane.
  await shoot('narrow', { width: 900, height: 1000, state: { enabled: false, changed_at: '2026-09-24T23:00:00+00:00' } })

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })

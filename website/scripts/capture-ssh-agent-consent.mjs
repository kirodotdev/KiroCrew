/**
 * Screenshot harness for Settings > Security's SSH agent forwarding section.
 *
 * Same shape as capture-credential-redaction.mjs: serves the REAL built SPA
 * (website/dist, or `DIST` to shoot another build such as main's for the
 * "before" frame) and answers /api/** from the shared fixture router, with the
 * ssh-agent consent endpoint supplied here.
 *
 * The states worth a frame are the ones a reviewer cannot infer from the diff:
 * the grant absent, the grant held (timestamp + Withdraw), a hand-written grant,
 * the no-socket note, the tier note, a failed save, and the READ FAILED state,
 * because an unreadable authorization must render as unknown rather than as
 * "Not allowed".
 *
 * Usage: node scripts/capture-ssh-agent-consent.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { serveDist, DEFAULT_DIST } from './lib/serve-dist.mjs'
import { installApiFixtures, installSettingsShell, logPageFailures } from './lib/api-fixtures.mjs'
import { SECURITY_RAIL_FIXTURES } from './lib/security-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/ssh-agent-consent'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

const CONSENT_PATH = '/api/ssh-agent/consent'
const CONSENT_RE = /\/api\/ssh-agent\/consent(\?|$)/

/** `harness_labels` as the gateway sends it: every harness the verdict covers,
 *  config id -> `tool_gate.label_for` (kiro, kas and claude render the panel's
 *  own translated names instead). */
const HARNESS_LABELS = {
  kiro: 'Kiro CLI', kas: 'kas', claude: 'claude', codex: 'OpenAI Codex', opencode: 'OpenCode',
  pi: 'Pi', goose: 'goose', deepseek: 'DeepSeek Harness', copilot: 'GitHub Copilot CLI',
}

/** The GET's real shape. `granted_at` is null for a hand-written grant. */
const consent = ({ granted = false, grantedAt = null, socketPresent = true, tierForwards = false, someForward = [] } = {}) => ({
  granted,
  granted_at: granted ? grantedAt : null,
  socket_present: socketPresent,
  forwarded_without_consent: tierForwards,
  forwarded_harnesses: tierForwards ? Object.keys(HARNESS_LABELS).sort() : someForward,
  harness_labels: HARNESS_LABELS,
})

async function main() {
  // serve-dist serves whatever is on disk, so a UI-only change shot against a
  // stale dist yields an "after" identical to before; build unless told not to.
  if (!process.env.SKIP_BUILD) execFileSync('npm', ['run', 'build'], { stdio: 'inherit', shell: process.platform === 'win32' })
  const { srv, base } = await serveDist(process.env.DIST || DEFAULT_DIST)
  const browser = await chromium.launch()

  async function shoot(name, { width = 1500, height = 980, theme = 'dark', state, failRead = false, failWrite = false, expectRow = true }) {
    const context = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: 2 })
    const page = await context.newPage()
    await installApiFixtures(page, {
      ...SECURITY_RAIL_FIXTURES,
      '/api/theme/boot': { mode: theme, theme: '' },
      ...(failRead ? {} : { [CONSENT_PATH]: state }),
    })
    // Registered AFTER the fixture router so it wins. The only way to render the
    // failed-read branch, which the always-200 fixture table cannot express.
    if (failRead) {
      await page.route(CONSENT_RE, route =>
        route.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"unreachable"}' }))
    }
    // A grant whose write fails: the GET keeps answering, the POST refuses, and
    // the card must keep the position in force with the save-failed notice.
    if (failWrite) {
      await page.route(CONSENT_RE, route => {
        if (route.request().method() === 'POST') {
          return route.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"could not record the grant","code":"ssh_agent_grant_write_failed"}' })
        }
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(state) })
      })
    }
    logPageFailures(page)
    await installSettingsShell(page, theme)
    await page.goto(`${base}/settings?tab=security&section=ssh_agent`, { waitUntil: 'domcontentloaded' })
    // Assert the state actually rendered before the shot: a plausible frame of the
    // wrong state satisfies the gate and misleads the reviewer.
    if (!expectRow) {
      await page.getByRole('listbox').first().waitFor({ timeout: 8000 })
    } else if (failRead) {
      await page.getByText('Could not read whether sessions can use your SSH agent.').waitFor({ timeout: 8000 })
    } else {
      await page.getByTestId('ssh-agent-row').waitFor({ timeout: 8000 })
      if (failWrite) {
        await page.getByTestId('ssh-agent-allow').click()
        await page.getByText('Could not save the SSH agent setting.').waitFor({ timeout: 8000 })
      }
      if (!state.socket_present) await page.getByTestId('ssh-agent-no-socket').waitFor({ timeout: 5000 })
      if (state.forwarded_without_consent) await page.getByTestId('ssh-agent-tier-forwards').waitFor({ timeout: 5000 })
      else if (state.forwarded_harnesses.length) await page.getByTestId('ssh-agent-tier-forwards-some').waitFor({ timeout: 5000 })
    }
    await page.waitForTimeout(400)
    await page.screenshot({ path: `${OUT}/${PREFIX}-${name}.png` })
    console.log(`${PREFIX}-${name}.png`)
    await context.close()
  }

  if (PREFIX === 'before') {
    // main has no SSH agent section: the same URL lands on the Security rail
    // without the row, which is the whole "before".
    await shoot('security-rail', { state: consent(), expectRow: false })
  } else {
    await shoot('not-allowed', { state: consent() })
    await shoot('allowed', { state: consent({ granted: true, grantedAt: '2026-10-09T07:40:00+00:00' }) })
    // A hand-written {"enabled": true} store: granted, no timestamp.
    await shoot('allowed-hand-written', { state: consent({ granted: true }) })
    await shoot('no-socket', { state: consent({ socketPresent: false }) })
    await shoot('tier-forwards', { state: consent({ tierForwards: true }) })
    // A grant held while the level forwards anyway: the badge says on, Withdraw stays.
    await shoot('tier-forwards-allowed', { state: consent({ granted: true, grantedAt: '2026-10-09T07:40:00+00:00', tierForwards: true }) })
    // The usual default: kiro keeps the socket on the standard level, masked harnesses need the grant.
    await shoot('some-forward', { state: consent({ someForward: ['claude', 'kiro'] }) })
    await shoot('some-forward-allowed', { state: consent({ granted: true, grantedAt: '2026-10-09T07:40:00+00:00', someForward: ['claude', 'kiro'] }) })
    await shoot('save-failed', { state: consent(), failWrite: true })
    await shoot('read-failed', { failRead: true })
    await shoot('not-allowed-light', { state: consent(), theme: 'light' })
    await shoot('narrow', { width: 900, height: 1000, state: consent({ granted: true, grantedAt: '2026-10-09T07:40:00+00:00' }) })
  }

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })

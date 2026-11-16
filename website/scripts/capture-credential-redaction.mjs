/**
 * Screenshot harness for Settings > Security's credential-redaction section.
 *
 * Serves the REAL built SPA (website/dist) and answers /api/** from the shared fixture router, with the
 * security endpoints supplied here because the default table has no security
 * routes.
 *
 * The section is one toggle, so the frames that matter are its three states: ON, OFF (the
 * default of a fresh install) and LOCKED (a governance policy keeps redaction on, so the
 * toggle is disabled with one line saying an administrator controls it). Each is shot in
 * the light theme at the full width and at the breakpoint below which the rail stacks
 * above the pane.
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
const LABEL = 'Redact credentials'

const STATES = {
  off: { enabled: false, changed_at: '', locked_by_policy: false },
  on: { enabled: true, changed_at: '2026-11-14T22:00:00+00:00', locked_by_policy: false },
  locked: { enabled: true, changed_at: '', locked_by_policy: true },
}

const WIDTHS = [
  { tag: 'wide', width: 1500, height: 980 },
  // The breakpoint below which the rail stacks above the pane.
  { tag: 'narrow', width: 900, height: 1000 },
]

async function main() {
  // serve-dist serves whatever is on disk, so a UI-only change shot against a
  // stale dist yields an "after" identical to before; build unless told not to.
  if (!process.env.SKIP_BUILD) execFileSync('npm', ['run', 'build'], { stdio: 'inherit', shell: process.platform === 'win32' })
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()

  async function shoot(name, { width, height, state }) {
    const context = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: 2 })
    const page = await context.newPage()
    await installApiFixtures(page, {
      ...SECURITY_RAIL_FIXTURES,
      '/api/theme/boot': { mode: 'light', theme: '' },
      // The nav dock's starred bots: without it the frame carries a load-failed toast.
      '/api/members': { members: [] },
      [SWITCH_PATH]: STATES[state],
    })
    logPageFailures(page)
    await page.addInitScript(() => {
      localStorage.clear()
      localStorage.setItem('mc-theme', 'light')
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
    })
    await page.goto(`${base}/settings?tab=security&section=redaction`, { waitUntil: 'domcontentloaded' })
    // Assert the state actually rendered before the shot: a plausible frame of the
    // wrong state satisfies the gate and misleads the reviewer.
    const toggle = page.getByRole('switch', { name: LABEL })
    await toggle.waitFor({ timeout: 8000 })
    const checked = await toggle.getAttribute('aria-checked')
    if (checked !== String(STATES[state].enabled)) throw new Error(`${name}: aria-checked ${checked}`)
    const disabled = (await toggle.getAttribute('aria-disabled')) === 'true'
    if (disabled !== STATES[state].locked_by_policy) throw new Error(`${name}: aria-disabled ${disabled}`)
    const lockedLines = await page.getByTestId('credential-redaction-locked').count()
    if (lockedLines !== (STATES[state].locked_by_policy ? 1 : 0)) throw new Error(`${name}: ${lockedLines} locked lines`)
    await page.waitForTimeout(400)
    await page.screenshot({ path: `${OUT}/${PREFIX}-${name}.png` })
    console.log(`${PREFIX}-${name}.png`)
    await context.close()
  }

  for (const { tag, width, height } of WIDTHS) {
    for (const state of Object.keys(STATES)) {
      await shoot(`${state}-${tag}`, { width, height, state })
    }
  }

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })

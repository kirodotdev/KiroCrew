/**
 * Screenshot harness for the mobile sign-in card's mint-failure copy.
 *
 * The card refuses a mint with one paragraph, so the whole change is WHICH
 * sentence that paragraph carries — a diff cannot show it and a unit test only
 * proves the string was selected, not that it renders as readable copy in the
 * panel it lives in. Four shots pin the whole branch:
 *
 *   restricted-session.png     — 403 `restricted_session`, a code that must NOT
 *                                say "try again"
 *   caller-session-expired.png — 403 `caller_session_expired`, likewise
 *   external-origin.png        — the one code that was already mapped, so the
 *                                shots also show it did not regress
 *   unmapped-code.png          — `governance_denied`, an unmapped code, proving
 *                                the default retry copy is still reached
 *
 * Builds the SPA first: serve-dist serves whatever is on disk, so shooting a
 * catalog change against a stale dist yields an "after" frame identical to
 * before. SKIP_BUILD=1 reuses an existing dist.
 *
 * Usage: node scripts/capture-mobile-link-errors.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { serveDist } from './lib/serve-dist.mjs'
import { installApiFixtures, json, logPageFailures } from './lib/api-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/mobile-link-errors'

mkdirSync(OUT, { recursive: true })

/* The posture rail section is the one holding the card ("Live Security
 * Posture"). Empty fixtures: nothing else in the section is under test, and a
 * populated rule table would push the card off the top of the frame. */
const FIXTURES = {
  '/api/security/posture': { controls: [], counts: {} },
  '/api/security/denied-commands': {
    builtins: [], user_added: [], disable_all: false,
    effective_count: 0, governance_locked: false,
  },
  '/api/governance/policy': {
    version: null, has_policy: false, profile: null, unavailable: false, scopes: [],
  },
  '/api/security/trusted-apps': { apps: [], ineffective: [], allowAll: false },
  '/api/tailnet/status': { enabled: false, state: 'off', governance_pinned: false },
  '/api/config/kirocrew': { agent: { yolo_duration: '6h', apps_allow_third_party: false } },
}

/** Every refusal the card distinguishes, plus one it deliberately does not. */
const CASES = [
  { name: 'restricted-session', code: 'restricted_session', status: 403 },
  { name: 'caller-session-expired', code: 'caller_session_expired', status: 403 },
  { name: 'external-origin', code: 'external_origin_unavailable', status: 409 },
  { name: 'unmapped-code', code: 'governance_denied', status: 403 },
]

async function main() {
  if (!process.env.SKIP_BUILD) {
    console.log('building dist (SKIP_BUILD=1 to reuse)…')
    // On Windows `npm` is a `.cmd` shim and Node refuses to spawn it without a
    // shell; safe here because the argv is three static literals.
    execFileSync('npm', ['run', 'build'], {
      stdio: 'inherit',
      shell: process.platform === 'win32',
    })
  }

  const { srv, base } = await serveDist()
  const browser = await chromium.launch()

  for (const { name, code, status } of CASES) {
    const context = await browser.newContext({
      viewport: { width: 1500, height: 900 },
      // Settings copy is 13px type; a 1x shot renders soft on GitHub.
      deviceScaleFactor: 2,
    })
    const page = await context.newPage()
    await installApiFixtures(page, FIXTURES)
    // Registered AFTER the fixture router so it wins: Playwright consults route
    // handlers most-recent-first, and the shared table only ever answers 200.
    await page.route('**/api/auth/mobile-link', route =>
      json(route, { error: code, code }, status),
    )
    logPageFailures(page)
    await page.addInitScript(() => {
      localStorage.clear()
      localStorage.setItem('mc-theme', 'dark')
      localStorage.setItem('mc-onboarded', '1')
      // The app shell reads the Electron updater bridge during boot and does
      // not tolerate its absence in a plain browser — without this stub every
      // settings tab dies in the shell's error boundary before the panel
      // renders.
      window.updateAPI = {
        onState: () => () => {},
        check: async () => ({ ok: true }),
        download: async () => ({ ok: true }),
        install: async () => ({ ok: true }),
        getInfo: async () => ({
          version: '0.5.0', channel: 'stable', stampedChannel: 'stable',
          channelSwitchable: true, channelPreference: '',
          platform: 'linux-x64', packaged: true,
        }),
        setChannel: async () => ({ ok: true }),
      }
    })

    // Path-routed, NOT hash-routed: serve-dist has an index.html fallback so
    // /settings resolves, while a '#/settings' URL leaves location.pathname at
    // '/' and the shell dies before the panel renders.
    await page.goto(`${base}/settings?tab=security&section=posture`, {
      waitUntil: 'domcontentloaded',
    })

    const card = page.locator('h3', { hasText: 'Sign in on mobile' }).locator('..')
    await card.waitFor({ timeout: 15000 })
    await page.getByRole('button', { name: 'Create mobile sign-in link' }).click()
    // The alert is the subject of the shot, so wait for it rather than a timeout:
    // a frame taken before it mounts is a card with no error in it, which looks
    // like a pass.
    await card.getByRole('alert').waitFor({ timeout: 15000 })
    const copy = (await card.getByRole('alert').textContent())?.trim()
    if (!copy) {
      console.error(`FAIL: ${code} rendered an empty alert`)
      process.exit(1)
    }
    await card.screenshot({ path: `${OUT}/${name}.png` })
    console.log(`${name}.png — ${copy}`)
    await context.close()
  }

  await browser.close()
  srv.close()
}

main()

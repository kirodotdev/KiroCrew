/**
 * Screenshot harness for the Security panel in a chosen UI language.
 *
 * `capture-security-inspector.mjs` shoots the rail's sections in English, which
 * cannot show whether a pane's strings are actually localised. This one drives the
 * same panel with the language forced, so a catalog change is verifiable as a
 * rendered frame rather than as a diff of JSON values.
 *
 * Shoots the three panes whose strings are language-sensitive without needing an
 * enterprise policy or a populated rule table: the auto-approve duration card, the
 * Tailnet origin card, and the third-party app toggle. The tailnet fixture reports
 * `active` with a startup resolution several hours old, so the status chips render
 * with a real clock time — the case that tells a state label apart from a
 * timestamp label.
 *
 * Builds the SPA first: serve-dist serves whatever is on disk, so shooting a
 * catalog change against a stale dist yields an "after" frame identical to before.
 *
 * Usage: node scripts/capture-security-locales.mjs [outDir] [locales]
 *   locales is a comma-separated list of catalog tags, e.g. de,es,zh-CN
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { serveDist } from './lib/serve-dist.mjs'
import { installApiFixtures, logPageFailures } from './lib/api-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/security-locales'
const LOCALES = (process.argv[3] || 'de,es,zh-CN').split(',')

mkdirSync(OUT, { recursive: true })

/** Resolved a few hours ago, so the "resolved" chip carries a clock time. */
const RESOLVED_AT = Math.floor(Date.now() / 1000) - 4 * 3600

const TAILNET = {
  enabled: true,
  governance_pinned: false,
  host: 'my-devbox.tailnet-a1b2.ts.net',
  origin: 'https://my-devbox.tailnet-a1b2.ts.net',
  resolved_at: RESOLVED_AT,
  state: 'active',
}

/* Empty on purpose: the posture registry, the rule table and the governance
 * ceiling all render server-supplied English regardless of the UI language, so
 * populating them here would put untranslatable strings in a frame whose job is
 * to show what the catalogs control. */
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
  '/api/tailnet/status': TAILNET,
  '/api/config/kirocrew': { agent: { yolo_duration: '6h', apps_allow_third_party: false } },
}

const SECTIONS = ['approval', 'tailnet', 'apps']

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

  for (const lang of LOCALES) {
    for (const section of SECTIONS) {
      const context = await browser.newContext({
        viewport: { width: 1500, height: 900 },
        // Settings rows are 11-13px type; a 1x shot renders soft on GitHub.
        deviceScaleFactor: 2,
      })
      const page = await context.newPage()
      // The language has to come from /api/theme/boot as well as localStorage:
      // a boot payload without it reverts the UI to English on first paint.
      await installApiFixtures(page, {
        ...FIXTURES,
        '/api/theme/boot': { mode: 'dark', theme: '', language: lang },
      })
      logPageFailures(page)
      await page.addInitScript(l => {
        localStorage.clear()
        localStorage.setItem('mc-theme', 'dark')
        localStorage.setItem('mc-onboarded', '1')
        localStorage.setItem('mc-lang', l)
        localStorage.setItem('mc-yolo-ack', '1')
        // The app shell reads the Electron updater bridge during boot and does
        // not tolerate its absence in a plain browser — without this stub every
        // settings tab dies in the shell's error boundary before the panel
        // renders. Same bridge capture-security-inspector.mjs installs.
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
      }, lang)

      // Path-routed, NOT hash-routed: serve-dist has an index.html fallback so
      // /settings resolves, while a '#/settings' URL leaves location.pathname at
      // '/' and the shell dies before the panel renders.
      await page.goto(`${base}/settings?tab=security&section=${section}`, {
        waitUntil: 'domcontentloaded',
      })
      await page.waitForTimeout(2000)
      const name = `${lang}-${section}.png`
      await page.screenshot({ path: `${OUT}/${name}` })
      console.log(name)
      await context.close()
    }
  }

  await browser.close()
  srv.close()
}

main()

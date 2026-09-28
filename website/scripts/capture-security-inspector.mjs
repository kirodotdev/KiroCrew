/**
 * Screenshot harness for Settings > Security's inspector rail.
 *
 * Same shape as capture-channel-explainer.mjs: serves the REAL built SPA
 * (website/dist) and answers /api/** from the shared fixture router, with the
 * four security endpoints supplied here because the default table has no
 * security routes (unmatched paths fall through to `[]`, which would render
 * every pane in its empty state).
 *
 * The rail's whole point is that each pane is a screenful, so this shoots one
 * frame per section rather than one tall frame of everything, plus the rule
 * search mid-filter (the one interaction the layout adds) and the narrow
 * stacked layout the breakpoint falls back to.
 *
 * Builds the SPA first: serve-dist serves whatever is on disk, so shooting a
 * UI-only change against a stale dist yields an "after" image identical to
 * before — indistinguishable from the change not working.
 *
 * Usage: node scripts/capture-security-inspector.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { serveDist } from './lib/serve-dist.mjs'
import { installApiFixtures, logPageFailures } from './lib/api-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/security-inspector'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

/** Ten categories at realistic sizes, so the rule pane shows the real shape. */
const CATEGORIES = {
  'aws-destructive': 14,
  'credential-exfil': 21,
  'destructive-fs': 18,
  'git-destructive': 9,
  'kernel': 7,
  'network-exfil': 13,
  'package-managers': 11,
  'pipe-exec': 11,
  'process-control': 16,
  'shell-escape': 17,
}

const builtins = []
for (const [category, n] of Object.entries(CATEGORIES)) {
  for (let i = 0; i < n; i++) {
    builtins.push({
      id: `${category}-${i}`,
      pattern: i === 0 && category === 'credential-exfil'
        ? 'cat\\s+.*(id_rsa|id_ed25519|\\.pem)\\b'
        : `${category}\\s+pattern-${i}`,
      category,
      description: category === 'credential-exfil' && i === 0
        ? 'Print an SSH private key'
        : `Blocks ${category.replace(/-/g, ' ')} operation ${i + 1}`,
      enabled: true,
      pinned: category === 'credential-exfil' && i === 1,
    })
  }
}

const DENIED = {
  builtins,
  user_added: [
    { id: 'user-1', pattern: 'rm -rf ~/scratch/.*', enabled: true },
    { id: 'user-2', pattern: 'curl .* \\| bash', enabled: true },
  ],
  disable_all: false,
  effective_count: builtins.length + 2,
  governance_locked: false,
}

const control = (key, label, unit, count, summary, items) => ({
  key, label, unit, count, summary, unavailable: false,
  source: 'src/kiro_crew/security.py',
  items,
})

const POSTURE = {
  controls: [
    control('sensitive_paths', 'Sensitive paths', 'credential paths', 13,
      'Paths the agent cannot read or write.',
      Array.from({ length: 13 }, (_, i) => ({ label: `~/.credentials-${i}`, detail: 'Credential store' }))),
    control('denied_commands', 'Denied commands', 'built-in rules', builtins.length,
      'Destructive shell operations blocked at the gate.',
      [{ label: 'Print an SSH private key', detail: 'credential-exfil' }]),
    control('suspicious_patterns', 'Suspicious patterns', 'patterns', 42,
      'Deletion, exfiltration and pipe-execution shapes.',
      Array.from({ length: 42 }, (_, i) => ({ label: `pattern-${i}`, detail: 'heuristic' }))),
    control('tool_schemas', 'Tool schemas', 'tool schemas', 12,
      'Every MCP handler validates its input.',
      Array.from({ length: 12 }, (_, i) => ({ label: `tool_${i}`, detail: 'typed schema' }))),
    control('redaction_paths', 'Redaction paths', 'output paths', 5,
      'Outputs scanned for plaintext and encoded secrets.',
      Array.from({ length: 5 }, (_, i) => ({ label: `path_${i}`, detail: 'redacted' }))),
    control('audit_surfaces', 'Audit surfaces', 'surfaces', 8,
      'Immutable security event trail.',
      Array.from({ length: 8 }, (_, i) => ({ label: `surface_${i}`, detail: 'SEL' }))),
  ],
  counts: { sensitive_paths: 13, denied_commands: builtins.length, redaction_paths: 5 },
}

const GOVERNANCE = {
  version: null,
  has_policy: false,
  profile: null,
  unavailable: false,
  scopes: [
    'tools', 'mcp', 'apps', 'commands',
    'filesystem.read', 'filesystem.write', 'network.egress',
    'channels', 'approval_mode', 'sandbox.min_level',
    'capabilities.cron', 'capabilities.spawn', 'capabilities.messaging',
    'capabilities.memory_writes', 'capabilities.script_hooks',
  ].map(scope => ({ scope, archetype: 'ruleset', governed: false, source: 'ungoverned', detail: {} })),
}

const FIXTURES = {
  '/api/security/posture': POSTURE,
  '/api/security/denied-commands': DENIED,
  '/api/governance/policy': GOVERNANCE,
  '/api/config/kirocrew': { agent: { yolo_duration: '6h', apps_allow_third_party: false } },
}

const SECTIONS = ['posture', 'approval', 'rules', 'apps', 'layers', 'governance', 'docs']

async function main() {
  if (!process.env.SKIP_BUILD) {
    console.log('building dist (SKIP_BUILD=1 to reuse)…')
    // On Windows `npm` is a `.cmd` shim, and since the CVE-2024-27980 hardening
    // Node refuses to spawn a `.bat`/`.cmd` without a shell — so naming
    // `npm.cmd` does not help either; the shell is what is actually required.
    // Safe here because the argv is three static literals with no interpolated
    // input, which is the injection hazard that hardening exists to prevent.
    execFileSync('npm', ['run', 'build'], {
      stdio: 'inherit',
      shell: process.platform === 'win32',
    })
  }

  const { srv, base } = await serveDist()
  const browser = await chromium.launch()

  async function shoot(name, { width, height, section, theme = 'dark', after, fixtures = {} }) {
    const context = await browser.newContext({
      viewport: { width, height },
      // Settings rows are 12-13px type; a 1x shot renders soft on GitHub.
      deviceScaleFactor: 2,
    })
    const page = await context.newPage()
    // The theme comes from /api/theme/boot, which the shared DEFAULTS pin to
    // dark — localStorage alone does not flip it, so the light shot has to
    // override the endpoint too.
    await installApiFixtures(page, { ...FIXTURES, '/api/theme/boot': { mode: theme, theme: '' }, ...fixtures })
    logPageFailures(page)
    await page.addInitScript(t => {
      localStorage.clear()
      localStorage.setItem('mc-theme', t)
      localStorage.setItem('mc-onboarded', '1')
      // The app shell reads the Electron updater bridge during boot and does not
      // tolerate its absence in a plain browser — without this stub every
      // settings tab dies in the shell's error boundary with "Cannot read
      // properties of undefined (reading 'startsWith')", long before the panel
      // under test renders. Same bridge capture-channel-explainer.mjs installs.
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

    const q = section ? `&section=${section}` : ''
    // Path-routed, NOT hash-routed: serve-dist has an index.html fallback so
    // /settings resolves. A '#/settings' URL leaves location.pathname at '/', and
    // the app shell then dies in its error boundary before the panel renders.
    await page.goto(`${base}/settings?tab=security${q}`, { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(1800)
    if (after) await after(page)
    await page.screenshot({ path: `${OUT}/${PREFIX}-${name}.png` })
    console.log(`${PREFIX}-${name}.png`)
    await context.close()
  }

  for (const section of SECTIONS) {
    await shoot(section, { width: 1500, height: 980, section })
  }

  // The one interaction the layout adds: search across 137 rules.
  await shoot('rules-search', {
    width: 1500, height: 980, section: 'rules',
    after: async page => {
      await page.fill('input[aria-label="Search rules, patterns, categories…"]', 'private key')
      await page.waitForTimeout(400)
    },
  })

  // Auto-approve ACTIVE. The rail's approval summary only says anything
  // interesting in this state: with no grant it reads "Interactive", so the
  // default frames above cannot show the expiry line at all.
  await shoot('approval-active', {
    width: 1500, height: 980, section: 'approval',
    fixtures: {
      '/api/status': {
        sessions: 0, crons: 0, lessons: 0, uptime: 120, version: '0.5.0',
        yolo: true, yolo_expires_at: '2026-08-07T18:40:00Z', yolo_until_shutdown: false,
      },
    },
  })

  // Light theme, so a reviewer can see the rail's selected state in both.
  await shoot('posture-light', { width: 1500, height: 980, section: 'posture', theme: 'light' })

  // Below TWO_PANE_MIN_WIDTH the rail becomes the whole view...
  await shoot('narrow-rail', { width: 620, height: 900 })
  // ...and choosing a section replaces it, with a back link.
  await shoot('narrow-detail', { width: 620, height: 900, section: 'rules' })

  await browser.close()
  if (srv) srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })

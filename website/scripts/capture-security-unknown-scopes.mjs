/**
 * Screenshot harness for the governance-policy viewer's companion-scope rows.
 *
 * Same shape as capture-profile-fallback-banner.mjs: serves the REAL built SPA
 * (website/dist) and answers /api/** from the shared fixture router. The
 * per-frame boilerplate lives in lib/governance-capture.mjs, shared with that
 * harness.
 *
 * Two frames, because the feature's whole claim is that a profile file's
 * skipped keys are now accounted for on the page:
 *
 *   01-governed-without-unknown-scopes  the governed page with the field absent
 *                                       — no block renders, nothing changes for
 *                                       the common case.
 *   02-unknown-scopes-per-profile       two profiles each listing the companion
 *                                       scopes this build skipped, rendered
 *                                       stacked with the fallback-profile banner
 *                                       they are siblings of in the payload —
 *                                       the frame pins a fallback profile so the
 *                                       adjacency it claims is actually shown.
 *
 * Builds the SPA first: serve-dist serves whatever is on disk, so shooting a
 * UI-only change against a stale dist yields an "after" frame identical to
 * before — indistinguishable from the change not working.
 *
 * Usage: node scripts/capture-security-unknown-scopes.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { serveDist } from './lib/serve-dist.mjs'
import { shootSettingsFrame } from './lib/governance-capture.mjs'

const OUT = process.argv[2] || '../temp-screenshots/security-unknown-scopes'
const PREFIX = process.argv[3] || 'shot'

mkdirSync(OUT, { recursive: true })

/** A permissive governed row, so the frames read as a healthy install. */
const permissiveRuleset = scope => ({
  scope,
  archetype: 'ruleset',
  governed: true,
  source: 'policy+profile',
  scope_note: 'host_profile',
  detail: { mode: 'deny', allow_count: 0, deny_count: 3 },
})

const enabledCapability = scope => ({
  scope,
  archetype: 'capability',
  governed: true,
  source: 'policy+profile',
  scope_note: 'host_profile',
  detail: { enabled: true, inner: {} },
})

const scopes = [
  ...['tools', 'mcp', 'apps', 'commands', 'filesystem.read', 'filesystem.write', 'network.egress']
    .map(permissiveRuleset),
  ...['capabilities.spawn', 'capabilities.memory_writes', 'capabilities.script_hooks',
    'capabilities.cron', 'capabilities.messaging', 'capabilities.publish']
    .map(enabledCapability),
]

const governance = (unknown_profile_scopes, fallback_profiles = []) => ({
  version: 1,
  has_policy: true,
  profile: 'host',
  surface: 'host',
  other_bound_surfaces: ['cron', 'subagent'],
  fallback_profiles,
  ...(unknown_profile_scopes ? { unknown_profile_scopes } : {}),
  unavailable: false,
  scopes,
})

const POSTURE = { controls: [], counts: {} }
const DENIED = {
  builtins: [], user_added: [], disable_all: false,
  effective_count: 0, governance_locked: false,
}

async function main() {
  if (!process.env.SKIP_BUILD) {
    console.log('building dist (SKIP_BUILD=1 to reuse)…')
    // On Windows `npm` is a `.cmd` shim and Node refuses to spawn one without a
    // shell; the argv is three static literals, so there is no injection input.
    execFileSync('npm', ['run', 'build'], {
      stdio: 'inherit',
      shell: process.platform === 'win32',
    })
  }

  const { srv, base } = await serveDist()
  const browser = await chromium.launch()

  async function shoot(name, unknown_profile_scopes, fallback_profiles = []) {
    await shootSettingsFrame(browser, base, `${OUT}/${PREFIX}-${name}.png`, {
      '/api/security/posture': POSTURE,
      '/api/security/denied-commands': DENIED,
      '/api/governance/policy': governance(unknown_profile_scopes, fallback_profiles),
      '/api/config/kirocrew': { agent: { yolo_duration: '6h', apps_allow_third_party: false } },
      '/api/theme/boot': { mode: 'dark', theme: '' },
    })
    console.log(`${PREFIX}-${name}.png`)
  }

  await shoot('01-governed-without-unknown-scopes', undefined)
  // A fallback profile alongside the unknown scopes: the frame's claim is the
  // stacked adjacency of the two sibling diagnostics, so both must render.
  await shoot('02-unknown-scopes-per-profile', {
    host: ['capabilities.board', 'capabilities.channels'],
    subagent: ['capabilities.board'],
  }, ['cron'])

  await browser.close()
  if (srv) srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })

/**
 * Screenshot harness for the governance-policy viewer's unusable-profile banner.
 *
 * Same shape as capture-security-inspector.mjs: serves the REAL built SPA
 * (website/dist) and answers /api/** from the shared fixture router, supplying
 * the security endpoints here because the default table has no security routes.
 *
 * Three frames, because the banner's whole claim is that a broken profile no
 * longer looks like a deliberate lockdown:
 *
 *   01-lockdown-without-signal  the state this PR fixes, captured by pinning
 *                               `fallback_profiles: []` while every row reads
 *                               "Nothing allowed" — i.e. what an operator saw
 *                               before: a total lockdown with nothing to say
 *                               whether it was intended.
 *   02-banner-names-the-profile the same page with the profile reported unusable.
 *   03-banner-names-siblings    a broken NON-host profile while the host's own is
 *                               fine — the case a host-only boolean could not
 *                               express, and the reason the contract is a list.
 *
 * Builds the SPA first: serve-dist serves whatever is on disk, so shooting a
 * UI-only change against a stale dist yields an "after" frame identical to
 * before — indistinguishable from the change not working.
 *
 * Usage: node scripts/capture-profile-fallback-banner.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { serveDist } from './lib/serve-dist.mjs'
import { shootSettingsFrame } from './lib/governance-capture.mjs'

const OUT = process.argv[2] || '../temp-screenshots/profile-fallback-visibility'
const PREFIX = process.argv[3] || 'shot'

mkdirSync(OUT, { recursive: true })

/** A deny-all row: allow-mode with nothing allowed, which renders "Nothing allowed". */
const denyAllRuleset = scope => ({
  scope,
  archetype: 'ruleset',
  governed: true,
  source: 'profile',
  scope_note: 'host_profile',
  detail: { mode: 'allow', allow_count: 0, deny_count: 0 },
})

/** A disabled capability, which renders "Disabled for the host surface". */
const disabledCapability = scope => ({
  scope,
  archetype: 'capability',
  governed: true,
  source: 'policy+profile',
  scope_note: 'host_profile',
  detail: { enabled: false, inner: {} },
})

/** A permissive row, for the frame where the HOST profile is itself healthy. */
const permissiveRuleset = scope => ({
  scope,
  archetype: 'ruleset',
  governed: true,
  source: 'policy+profile',
  scope_note: 'host_profile',
  detail: { mode: 'deny', allow_count: 0, deny_count: 3 },
})

/** The page as it looks while a deny-all fallback is in effect. */
const lockedScopes = [
  ...['tools', 'mcp', 'apps', 'commands', 'filesystem.read', 'filesystem.write', 'network.egress']
    .map(denyAllRuleset),
  ...['capabilities.spawn', 'capabilities.memory_writes', 'capabilities.script_hooks',
    'capabilities.cron', 'capabilities.messaging', 'capabilities.publish']
    .map(disabledCapability),
]

/**
 * The page when the host's OWN profile parsed fine and a sibling surface's did
 * not. Rows must render permissively here: reusing `lockedScopes` for this frame
 * makes the evidence contradict its own caption — a locked-down page beside a
 * banner naming other surfaces reproduces exactly the unattributed lockdown this
 * change exists to remove.
 */
const healthyHostScopes = [
  ...['tools', 'mcp', 'apps', 'commands', 'filesystem.read', 'filesystem.write', 'network.egress']
    .map(permissiveRuleset),
  ...['capabilities.spawn', 'capabilities.memory_writes', 'capabilities.script_hooks',
    'capabilities.cron', 'capabilities.messaging', 'capabilities.publish']
    .map(scope => ({
      scope,
      archetype: 'capability',
      governed: true,
      source: 'policy+profile',
      scope_note: 'host_profile',
      detail: { enabled: true, inner: {} },
    })),
]

const governance = (fallback_profiles, scopes = lockedScopes) => ({
  version: 1,
  has_policy: true,
  profile: 'host',
  surface: 'host',
  other_bound_surfaces: ['cron', 'subagent'],
  fallback_profiles,
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

  async function shoot(name, fallback_profiles, scopes) {
    await shootSettingsFrame(browser, base, `${OUT}/${PREFIX}-${name}.png`, {
      '/api/security/posture': POSTURE,
      '/api/security/denied-commands': DENIED,
      '/api/governance/policy': governance(fallback_profiles, scopes),
      '/api/config/kirocrew': { agent: { yolo_duration: '6h', apps_allow_third_party: false } },
      '/api/theme/boot': { mode: 'dark', theme: '' },
    })
    console.log(`${PREFIX}-${name}.png`)
  }

  await shoot('01-lockdown-without-signal', [])
  await shoot('02-banner-names-the-profile', ['host'])
  // Host rows permissive here: the point of the frame is a healthy host beside
  // broken siblings, which a locked-down page would contradict.
  await shoot('03-banner-names-siblings', ['cron', 'subagent'], healthyHostScopes)

  await browser.close()
  if (srv) srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })

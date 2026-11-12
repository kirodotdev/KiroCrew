/**
 * Screenshot harness for #33069: sentences split around inline code or bold text.
 *
 * Shoots three surfaces in Japanese, where the old catalog pieces read in English
 * word order around the marked-up word:
 *
 *  1. `teams`: Settings → Channels → Microsoft Teams (setup intro, messaging
 *     endpoint, public-URL warning, side-load step).
 *  2. `deploy`: Artifact Deploy with "Getting started" and "How this is secured"
 *     expanded (gateway-host and SSO notes, IAM step, credentials, bucket, scan).
 *  3. `governance`: Settings → Security → Governance policy (ceiling note and the
 *     no-policy line).
 *
 * Runs the REAL built SPA behind the shared loopback static server with every
 * /api/** call answered from fixtures.
 *
 * Usage: npm run build && node scripts/capture-33069-inline-code-sentences.mjs <outDir> <before|after> [distDir] [lang]
 *   Run `before` against a build of origin/main (pass its dist as distDir).
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { serveDist, DEFAULT_DIST } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/33069-inline-code-sentences'
const LABEL = process.argv[3] === 'before' ? 'before' : 'after'
const DIST = process.argv[4] || DEFAULT_DIST
const LANG = process.argv[5] || 'ja'
mkdirSync(OUT, { recursive: true })

// The catalog of the build being shot, so a `before` run reads origin/main's keys.
const catalog = JSON.parse(readFileSync(join(DIST, '..', 'src', 'i18n', 'locales', `${LANG}.json`), 'utf8'))
const tr = key => key.split('.').reduce((node, part) => node?.[part], catalog)

const TEAMS_CONFIG = {
  connected: false, connect_error: '', configured: true, read_only: false,
  app_id_set: true, app_password_set: true, enabled: true, tenant_id: '',
  allowed_emails: ['alice@example.com'], jwt_available: true,
  soft_threshold_pct: 80, hard_threshold_pct: 95, session_folder: '', sdk_installed: true,
}

const GOVERNANCE = {
  version: null, has_policy: false, profile: null, unavailable: false,
  scopes: ['tools', 'mcp', 'apps', 'commands', 'filesystem.read', 'network.egress']
    .map(scope => ({ scope, archetype: 'ruleset', governed: false, source: 'ungoverned', detail: {} })),
}

const control = (key, label, unit, count) => ({
  key, label, unit, count, summary: label, unavailable: false, source: 'src/kiro_crew/security.py',
  items: [{ label: `${key}-1`, detail: label }],
})
const POSTURE = {
  controls: [
    control('sensitive_paths', 'Sensitive paths', 'credential paths', 13),
    control('denied_commands', 'Denied commands', 'built-in rules', 1),
    control('suspicious_patterns', 'Suspicious patterns', 'patterns', 42),
    control('tool_schemas', 'Tool schemas', 'tool schemas', 12),
    control('redaction_paths', 'Redaction paths', 'output paths', 5),
    control('audit_surfaces', 'Audit surfaces', 'surfaces', 8),
  ],
  counts: { sensitive_paths: 13, denied_commands: 1, redaction_paths: 5 },
}
const DENIED = { builtins: [], user_added: [], disable_all: false, effective_count: 0, governance_locked: false }

const extra = async (path, route) => {
  // The starred-bots dock reads this; a guessed [] shows a load-failed toast in every frame.
  if (path === '/api/members') return json(route, { members: [], default_agent: 'kirocrew' }), true
  if (path === '/api/security/posture') return json(route, POSTURE), true
  if (path === '/api/security/denied-commands') return json(route, DENIED), true
  if (path === '/api/config/kirocrew') return json(route, { agent: { yolo_duration: '6h', apps_allow_third_party: false } }), true
  if (path === '/api/teams/config') return json(route, TEAMS_CONFIG), true
  if (path === '/api/governance/channels') return json(route, { teams: true }), true
  if (path === '/api/governance/policy') return json(route, GOVERNANCE), true
  if (path === '/api/deploy/config') {
    return json(route, { cloudDeploymentEnabled: true, reaperInstallScript: '/home/example/.kirocrew/skills/artifact-deploy/install-reaper.sh' }), true
  }
  if (path === '/api/deploy/profiles') {
    return json(route, { profiles: [], default: '', available: [] }), true
  }
  if (path === '/api/deploy/list') return json(route, { sites: [], configured: false, profile_errors: [] }), true
  if (path === '/api/artifacts') return json(route, { artifacts: [] }), true
  return false
}

let failures = 0
const fail = msg => { console.error(`FAIL: ${msg}`); failures++ }

const { srv, base } = await serveDist(DIST)
const browser = await chromium.launch()

const open = async (url, theme) => {
  const context = await browser.newContext({ viewport: { width: 1280, height: 1000 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  await stubDashboardApi(page, { extra, theme, localStorageEntries: { 'mc-lang': LANG, 'mc-privacy-notice-v1': '1' } })
  logPageProblems(page)
  await page.goto(`${base}${url}`, { waitUntil: 'domcontentloaded' })
  return { context, page }
}

/** Check the page shows the new whole sentence (after) or still the old fragment (before). */
const expectText = async (page, name, afterKey, beforeKey) => {
  const text = (await page.locator('body').innerText()).replaceAll('\n', ' ')
  const probe = LABEL === 'after'
    ? tr(afterKey).split('<')[0].trim()
    : tr(beforeKey).split('\n')[0].trim()
  if (!probe || !text.includes(probe)) fail(`${name}: expected ${JSON.stringify(probe)} on the page`)
}

for (const theme of ['light', 'dark']) {
  // 1. Teams
  {
    const { context, page } = await open('/settings?tab=channels&channel=teams', theme)
    await page.getByText('/api/messaging/teams', { exact: false }).first().waitFor({ timeout: 30000 }).catch(() => fail('teams: panel did not render'))
    await page.waitForTimeout(800)
    await expectText(page, 'teams', 'pages.settings.teamsPanel.register_azure_bot_intro', 'pages.settings.teamsPanel.register_an_azure_bot_add_the')
    await page.screenshot({ path: `${OUT}/01-teams-${LANG}-${LABEL}-${theme}.png` })
    console.log(`wrote 01-teams-${LANG}-${LABEL}-${theme}.png`)
    await context.close()
  }
  // 2. Artifact Deploy, both guides open
  {
    const { context, page } = await open('/deploy', theme)
    // Open each collapsible only if it is closed: the guide starts open when no
    // profile is saved, so a blind click would close it.
    const cards = []
    for (const key of ['pages.artifactDeployPage.getting_started_one_time_aws_setup', 'pages.artifactDeployPage.how_this_is_secured']) {
      const toggle = page.locator('[aria-expanded]', { hasText: tr(key) }).first()
      await toggle.waitFor({ timeout: 30000 }).catch(() => fail(`deploy: ${key} did not render`))
      if ((await toggle.getAttribute('aria-expanded')) !== 'true') await toggle.click()
      cards.push(toggle.locator('xpath=..'))
    }
    await page.waitForTimeout(800)
    await expectText(page, 'deploy-guide', 'pages.artifactDeployPage.sso_needs_cli_v2', 'pages.artifactDeployPage.sso_needs')
    await expectText(page, 'deploy-security', 'pages.artifactDeployPage.only_profile_name_is_stored', 'pages.artifactDeployPage.only_the')
    for (const [i, card] of cards.entries()) {
      const file = `02${'ab'[i]}-deploy-${i ? 'security' : 'guide'}-${LANG}-${LABEL}-${theme}.png`
      await card.screenshot({ path: `${OUT}/${file}` })
      console.log(`wrote ${file}`)
    }
    await context.close()
  }
  // 3. Governance policy
  {
    const { context, page } = await open('/settings?tab=security&section=governance', theme)
    const heading = page.getByText(tr('pages.settings.securityPanel.effective_security_ceiling'), { exact: true })
    await heading.waitFor({ timeout: 30000 }).catch(() => fail('governance: card did not render'))
    await heading.scrollIntoViewIfNeeded()
    await page.waitForTimeout(800)
    await expectText(page, 'governance', 'pages.settings.securityPanel.no_policy_author_ceiling', 'pages.settings.securityPanel.no_policy_or_host_profile_restricts_the_host_sur')
    await page.screenshot({ path: `${OUT}/03-governance-${LANG}-${LABEL}-${theme}.png` })
    console.log(`wrote 03-governance-${LANG}-${LABEL}-${theme}.png`)
    await context.close()
  }
}

await browser.close()
srv.close()
if (failures) { console.error(`${failures} assertion(s) failed`); process.exit(1) }
console.log('OK')

/**
 * Screenshot evidence for PR #6120 — path-based settings navigation.
 *
 * Runs the REAL built SPA (website/dist) with every /api/** call answered
 * from fixtures (repo's standard evidence harness). No gateway, no token.
 *
 * Frames:
 *   desktop-path-deeplink   — /settings/channels/slack resolves to the drilled pane
 *   desktop-legacy-translate — /settings?tab=security&section=rules replace-redirects
 *                              to /settings/security/rules (final URL printed)
 *   mobile-settings-root    — /settings top level
 *   mobile-tab-level        — /settings/channels: outer "‹ Settings" back bar
 *   mobile-drilled          — /settings/channels/slack: SubNav's own back bar
 *   mobile-trailing-slash   — /settings/channels/ (empty segment) is NOT a
 *                             drill-in; the outer back bar stays
 *
 * Usage: node scripts/capture-path-nav-evidence.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/path-nav-evidence'
mkdirSync(OUT, { recursive: true })

const { srv, base: BASE } = await serveDist()

const browser = await chromium.launch()

async function shot(name, url, viewport, settle = 1800) {
  const ctx = await browser.newContext({ viewport })
  const page = await ctx.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    // The Slack pane asks /api/governance/channels before rendering its
    // settings; without a fixture it shows the "policy status unavailable"
    // error state, which would photograph as a bug that is not this PR's.
    extra: async (path, route) => {
      // Panes photographed here fetch these beyond the shared fixture map;
      // without them the pane renders an error state that is not this PR's.
      if (path === '/api/governance/channels') {
        await json(route, { slack: true, discord: true, telegram: true, teams: true, webex: true, wecom: true, weixin: true, whatsapp: true, imessage: true })
        return true
      }
      if (path === '/api/slack/config') {
        await json(route, {
          connected: false, connect_error: '', configured: false, read_only: false,
          bot_token_set: false, app_token_set: false, bot_token_preview: '', app_token_preview: '',
          owner_id: '', command: 'kirocrew', allowed_enterprise_ids: [],
          reactions_enabled: true, show_thinking: false, session_folder: '',
        })
        return true
      }
      if (path === '/api/security/denied-commands') {
        await json(route, { builtins: [], user_added: [], disable_all: false, effective_count: 0, governance_locked: false })
        return true
      }
      return false
    },
  })
  await page.goto(BASE + url, { waitUntil: 'networkidle' }).catch(() => {})
  await page.waitForTimeout(settle)
  console.log(`${name}: requested=${url} final=${page.url().replace(BASE, '')}`)
  await page.screenshot({ path: `${OUT}/${name}.png` })
  await ctx.close()
}

const desktop = { width: 1440, height: 900 }
const mobile = { width: 390, height: 844 }

await shot('desktop-path-deeplink', '/settings/channels/slack', desktop)
await shot('desktop-legacy-translate', '/settings?tab=security&section=rules', desktop)
await shot('mobile-settings-root', '/settings', mobile)
await shot('mobile-tab-level', '/settings/channels', mobile)
await shot('mobile-drilled', '/settings/channels/slack', mobile)
await shot('mobile-trailing-slash', '/settings/channels/', mobile)

await browser.close()
srv.close()

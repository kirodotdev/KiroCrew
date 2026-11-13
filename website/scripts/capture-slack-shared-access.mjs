/**
 * Screenshot harness for Settings → Messaging Channels → Slack → Share Slack access.
 *
 * Runs the REAL built SPA (website/dist) behind `serveDist` and answers every
 * /api/** call from fixtures through `stubDashboardApi`. No gateway, no Slack.
 *
 * Six shots: the switch off with the warning already on the page, the confirm
 * that turning it on opens (naming who gets access), the switch on with two
 * people listed (one Slack did not confirm), the hint a malformed member ID gets,
 * what turning it on with nobody listed says, and the read-only remote view.
 *
 * Usage: node scripts/capture-slack-shared-access.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/slack-shared-access-shots'
mkdirSync(OUT, { recursive: true })

const slackConfig = {
  connected: true, connect_error: '', configured: true, read_only: false,
  bot_token_set: true, app_token_set: true, bot_token_preview: 'xoxb-••••', app_token_preview: 'xapp-••••',
  owner_id: 'U0OWNER001', command: 'kirocrew', allowed_enterprise_ids: [],
  reactions_enabled: true, show_thinking: true, session_folder: '', auto_link_sessions: false,
  agent_sessions: false, code_channels: false,
  code_channel_defaults: { folder: null, agent: null, model: null, reasoning_effort: null },
}

const people = [
  { user_id: 'U0ADA00001', name: 'Ada Lovelace', confirmed: true, reason: 'ok' },
  { user_id: 'U0GRACE001', name: 'Grace', confirmed: false, reason: 'deactivated' },
]

const { srv, base } = await serveDist()
const browser = await chromium.launch()

async function loadPanel(shared) {
  const context = await browser.newContext({ viewport: { width: 1400, height: 1000 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  const extra = async (path, route) => {
    if (path === '/api/slack/config') { await json(route, slackConfig); return true }
    if (path === '/api/slack/shared-access') { await json(route, shared); return true }
    if (path === '/api/slack/manifest/status') { await json(route, {}); return true }
    if (path === '/api/governance/channels') {
      await json(route, { slack: true, discord: true, telegram: true, webex: true, wecom: true, teams: true, weixin: true, imessage: true })
      return true
    }
    return false
  }
  await stubDashboardApi(page, { extra })
  await page.goto(`${base}/settings?tab=channels&channel=slack`, { waitUntil: 'domcontentloaded' })
  const section = page.getByTestId('slack-shared-access')
  await section.waitFor({ timeout: 15000 })
  await section.scrollIntoViewIfNeeded()
  await page.waitForTimeout(800)
  return { page, section }
}

async function shotOf(locator, name) {
  await locator.screenshot({ path: `${OUT}/${name}.png` })
  console.log('wrote', `${OUT}/${name}.png`)
}

try {
  const off = await loadPanel({ enabled: false, users: people, read_only: false })
  await shotOf(off.section, '1-off-warning-shown')

  await off.page.getByRole('switch', { name: 'Share Slack access' }).click()
  const dialog = off.page.getByRole('dialog')
  await dialog.waitFor({ timeout: 5000 })
  await off.page.waitForTimeout(500)
  await shotOf(dialog, '2-confirm-before-on')

  const on = await loadPanel({ enabled: true, users: people, read_only: false })
  await shotOf(on.section, '3-on-with-people')

  await on.page.getByRole('textbox', { name: 'Slack member ID to add' }).fill('B0BOT')
  await on.page.getByRole('button', { name: /Add/ }).last().click()
  await on.page.waitForTimeout(300)
  await shotOf(on.section, '4-malformed-id-hint')

  const empty = await loadPanel({ enabled: false, users: [], read_only: false })
  await empty.page.getByRole('switch', { name: 'Share Slack access' }).click()
  await empty.page.waitForTimeout(300)
  await shotOf(empty.section, '5-empty-list-needs-someone')

  const remote = await loadPanel({ enabled: true, users: people, read_only: true })
  await shotOf(remote.section, '6-read-only-remote')
} finally {
  await browser.close()
  srv.close()
}

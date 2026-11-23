/**
 * Screenshot harness for Settings → Messaging Channels → Slack → Share Slack access.
 *
 * Runs the REAL built SPA (website/dist) behind `serveDist` and answers every
 * /api/** call from fixtures through `stubDashboardApi`. No gateway, no Slack.
 *
 * Fourteen shots: the switch off with the warning already on the page, the confirm
 * that turning it on opens (naming who gets access), the switch on with two
 * people listed (one Slack did not confirm), the hint a malformed member ID gets,
 * what turning it on with nobody listed says, the read-only remote view, the hint
 * when Slack confirmed nobody, a person Slack could not be asked about, the switch
 * refused while someone listed is unconfirmed, a change saved but not applied, a
 * failed save, a failed load, the Try again notice when the section's own chunk
 * fails to load, and the switch on with `verify_with_slack` off (listed people, no
 * Slack check, the not-checked warning).
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

const confirmedBo = { user_id: 'U0BO000001', name: 'Bo', confirmed: true, reason: 'ok' }

const { srv, base } = await serveDist()
const browser = await chromium.launch()

// `opts.put`: the answer to turning the switch, as [status, body]; `opts.getStatus`: a
// failing read of the section; `opts.blockChunk`: the section's own chunk fails to load.
async function loadPanel(shared, opts = {}) {
  // No service worker: a request it serves bypasses page.route, so blockChunk could not fail it.
  const context = await browser.newContext({ viewport: { width: 1400, height: 1000 }, deviceScaleFactor: 2, serviceWorkers: 'block' })
  const page = await context.newPage()
  const extra = async (path, route) => {
    if (path === '/api/slack/config') { await json(route, slackConfig); return true }
    if (path === '/api/slack/shared-access' && route.request().method() === 'PUT' && opts.put) {
      await json(route, opts.put[1], opts.put[0]); return true
    }
    if (path === '/api/slack/shared-access' && opts.getStatus) {
      await json(route, { error: 'unavailable' }, opts.getStatus); return true
    }
    if (path === '/api/slack/shared-access') { await json(route, shared); return true }
    if (path === '/api/slack/manifest/status') { await json(route, {}); return true }
    if (path === '/api/governance/channels') {
      await json(route, { slack: true, discord: true, telegram: true, webex: true, wecom: true, teams: true, weixin: true, imessage: true })
      return true
    }
    return false
  }
  await stubDashboardApi(page, { extra })
  // After the stub: the route registered last answers first.
  if (opts.blockChunk) await page.route(/\/assets\/SlackSharedAccess-[^/]*\.js(\?|$)/, route => route.abort())
  await page.goto(`${base}/settings?tab=channels&channel=slack`, { waitUntil: 'domcontentloaded' })
  if (opts.getStatus || opts.blockChunk) {
    // No section renders: a notice stands in its place inside the Slack panel. A failed
    // chunk first reloads the page once (main.tsx's vite:preloadError guard).
    const notice = page.getByText(opts.getStatus ? /Could not load the shared access settings/ : /Try again/i).first()
    await notice.waitFor({ timeout: 45000 })
    await notice.scrollIntoViewIfNeeded()
    await page.waitForTimeout(800)
    // The chunk notice is one line (the notice and its two buttons) in its own card
    // row: from Try again, frame that row. The load notice sits beside its button.
    return { page, section: notice.locator('xpath=../..') }
  }
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

  // The confirm opens only when everyone listed is confirmed (an unconfirmed person refuses at the switch).
  const ready = await loadPanel({ enabled: false, users: [people[0], confirmedBo], read_only: false })
  await ready.page.getByRole('switch', { name: 'Share Slack access' }).click()
  const dialog = ready.page.getByRole('dialog')
  await dialog.waitFor({ timeout: 5000 })
  await ready.page.waitForTimeout(500)
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

  const nobody = await loadPanel({ enabled: false, users: [people[1]], read_only: false })
  await nobody.page.getByRole('switch', { name: 'Share Slack access' }).click()
  await nobody.page.waitForTimeout(300)
  await shotOf(nobody.section, '7-none-confirmed-hint')

  const unchecked = [people[0], { user_id: 'U0LIN00001', name: 'Lin', confirmed: false, reason: 'unverifiable' }]
  const down = await loadPanel({ enabled: false, users: unchecked, read_only: false })
  await shotOf(down.section, '8-unverifiable-row')

  const mixed = await loadPanel({ enabled: false, users: [...people, unchecked[1]], read_only: false })
  await mixed.page.getByRole('switch', { name: 'Share Slack access' }).click()
  await mixed.page.getByText(/^You can't turn sharing on while/).waitFor({ timeout: 5000 })
  await shotOf(mixed.section, '9-refused-unconfirmed')

  const pending = await loadPanel({ enabled: true, users: people, read_only: false },
    { put: [409, { error: 'not applied', code: 'not_applied', unread: ['slack'] }] })
  await pending.page.getByRole('switch', { name: 'Share Slack access' }).click()
  await pending.page.getByText(/saved but not applied/).waitFor({ timeout: 5000 })
  await shotOf(pending.section, '10-not-applied')

  const broken = await loadPanel({ enabled: true, users: people, read_only: false }, { put: [500, { error: 'boom' }] })
  await broken.page.getByRole('switch', { name: 'Share Slack access' }).click()
  await broken.page.getByText(/Could not save the change/).waitFor({ timeout: 5000 })
  await shotOf(broken.section, '11-save-failed')

  const unread = await loadPanel(null, { getStatus: 500 })
  await shotOf(unread.section, '12-load-failed')

  const noChunk = await loadPanel(null, { blockChunk: true })
  await shotOf(noChunk.section, '13-try-again')

  const listed = [
    { user_id: 'U0ADA00001', name: '', confirmed: true, reason: 'listed' },
    { user_id: 'U0BO000001', name: '', confirmed: true, reason: 'listed' },
  ]
  const unverified = await loadPanel({ enabled: true, verify_with_slack: false, users: listed, read_only: false })
  await shotOf(unverified.section, '14-on-listed-not-checked')
} finally {
  await browser.close()
  srv.close()
}

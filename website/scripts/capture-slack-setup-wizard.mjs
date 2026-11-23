/**
 * Screenshot harness for the Slack setup wizard (Settings ▸ Messaging channels ▸ Slack).
 *
 * Loads the REAL built SPA with canned Slack config, manifest and install
 * status, then walks the wizard from "have a workspace" to "send a test
 * message": one shot per step, each with its schematic picture, in the dark and
 * light themes. Every scene asserts the step and its link before it shoots, so
 * a broken render fails the run rather than producing a plausible PNG.
 *
 * Credentials here are placeholders; they only reach the stubbed writer.
 *
 * Usage: node scripts/capture-slack-setup-wizard.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/slack-setup-wizard'
mkdirSync(OUT, { recursive: true })

const APP = 'A0123ABCD'
const TEAM = 'T0456EFGH'
const STATUS = {
  features: [], required_scopes: [], required_events: [], required_features: [], feature_changes: {},
  install: { status: 'up_to_date', missing_scopes: [], extra_scopes: [], disabled_features: [], app_id: APP, team_id: TEAM, reason: '' },
  app_manifest_url: `https://api.slack.com/apps/${APP}/app-manifest`,
  app_basic_info_url: `https://api.slack.com/apps/${APP}/general`,
  app_dm_url: `https://slack.com/app_redirect?app=${APP}&team=${TEAM}`,
}

function check(label, cond) {
  if (!cond) throw new Error(`assertion failed: ${label}`)
}

/** A mutable Slack config the stubbed writer updates, so the wizard advances.
 *  `over` seeds a saved state; `stub` steers the stubbed writer and Reconnect:
 *  `rejectToken` refuses a token save as Slack would, `reconnect` is
 *  'ok' | 'fail' | 'hang' (never answers, so the in-progress state stays up). */
function makeState(over = {}, stub = {}) {
  return {
    connected: false, connect_error: '', configured: false, read_only: false,
    bot_token_set: false, app_token_set: false, bot_token_preview: '', app_token_preview: '',
    owner_id: '', command: 'kirocrew', allowed_enterprise_ids: [], reactions_enabled: true,
    show_thinking: false, session_folder: '', auto_link_sessions: false, show_in_session_list: true,
    ...over,
    _stub: { rejectToken: false, reconnect: 'ok', orgGuide: null, ...stub },
  }
}

const configOf = state => Object.fromEntries(Object.entries(state).filter(([k]) => k !== '_stub'))

/** `open`: 'wizard' opens it and waits for it; 'click' only clicks Setup
 *  wizard (its chunk may fail); 'none' leaves the panel as loaded. */
async function load(context, theme, state, { panelShot = true, open = 'wizard' } = {}) {
  const page = await context.newPage()
  logPageProblems(page)
  const extra = async (path, route) => {
    const method = route.request().method()
    if (path === '/api/slack/config' && method === 'GET') return json(route, configOf(state)), true
    if (path === '/api/slack/config' && method === 'PUT') {
      const body = route.request().postDataJSON() || {}
      if (state._stub.rejectToken && (body.bot_token || body.app_token)) {
        const field = body.bot_token ? 'bot_token' : 'app_token'
        return json(route, { error: `${field} rejected by Slack (invalid_auth)` }, 400), true
      }
      if (body.bot_token) { state.bot_token_set = true; state.bot_token_preview = 'xoxb-…abcd' }
      if (body.app_token) { state.app_token_set = true; state.app_token_preview = 'xapp-…abcd'; state.configured = true }
      if (body.owner_id) state.owner_id = body.owner_id
      if (body.command) state.command = body.command
      return json(route, { ok: true, restart_required: false, reconnect_required: !!(body.bot_token || body.app_token || body.owner_id), verify_warning: '' }), true
    }
    if (path === '/api/slack/reconnect') {
      if (state._stub.reconnect === 'hang') return true // left unanswered: Reconnect stays in progress
      if (state._stub.reconnect === 'fail') return json(route, { connected: false, connect_error: 'invalid_auth' }), true
      state.connected = true
      return json(route, { connected: true, connect_error: '' }), true
    }
    if (path === '/api/slack/manifest') {
      const alias = new URL(route.request().url()).searchParams.get('alias') || 'kirocrew'
      return json(route, {
        alias, manifest: `display_information:\n  name: KiroCrew-${alias}\n`, // brand-ok: the app name slack-manifest.yaml renders
        create_url: 'https://api.slack.com/apps?new_app=1&manifest_yaml=x',
        command: alias === 'kirocrew' ? 'kirocrew' : `kirocrew-${alias.toLowerCase()}`, features: [],
        org_guide_url: state._stub.orgGuide,
      }), true
    }
    if (path === '/api/slack/manifest/status') return json(route, state.bot_token_set ? STATUS : { ...STATUS, install: { ...STATUS.install, status: 'not_configured', app_id: '', team_id: '' }, app_basic_info_url: null, app_dm_url: null, app_manifest_url: null }), true
    if (path === '/api/governance/channels') return json(route, { slack: true }), true
    if (path === '/api/config/schema') return json(route, { entries: [{ path: 'slack.command', type: 'string' }] }), true
    return false
  }
  await stubDashboardApi(page, { theme, extra, localStorageEntries: { 'mc-privacy-notice-v1': '1' } })
  await page.goto(context._base + '/settings/channels/slack', { waitUntil: 'domcontentloaded' })
  if (open === 'none') {
    await page.getByRole('heading', { name: 'Slack', level: 3 }).waitFor({ timeout: 20_000 })
    await page.waitForTimeout(800)
    return page
  }
  // Opt-in: the panel opens with the wizard closed; its button opens it.
  await page.getByTestId('slack-wizard-open').waitFor({ state: 'visible', timeout: 20_000 })
  if (panelShot) await page.screenshot({ path: join(OUT, `0-panel-default-${theme}.png`) })
  await page.getByTestId('slack-wizard-open').click()
  if (open === 'click') return page
  await page.getByTestId('slack-wizard').waitFor({ state: 'visible', timeout: 20_000 })
  await page.waitForTimeout(800)
  return page
}

async function shoot(page, name) {
  const wizard = page.getByTestId('slack-wizard')
  await wizard.scrollIntoViewIfNeeded()
  await page.mouse.move(2, 2)
  const card = wizard.locator('xpath=ancestor::*[contains(@class,"rounded")][1]')
  await (await card.count() ? card : wizard).screenshot({ path: join(OUT, name) })
}

const stepOf = page => page.getByTestId('slack-wizard').getAttribute('data-step')

/** Open the wizard on a seeded state in a fresh context. */
async function openOn(browser, base, theme, state, { open = 'wizard', beforeLoad } = {}) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 1000 }, deviceScaleFactor: 2 })
  context._base = base
  if (beforeLoad) await beforeLoad(context)
  return { context, page: await load(context, theme, state, { panelShot: false, open }) }
}

/** The panel header and as much below it as one viewport holds. */
async function shootPanel(page, name) {
  await page.evaluate(() => window.scrollTo(0, 0))
  await page.mouse.move(2, 2)
  await page.screenshot({ path: join(OUT, name) })
}

/** The states the happy path never reaches: rejected token saves, an owner
 *  already saved (its Connect button), a failed and an in-progress Reconnect,
 *  a read-only panel without the Setup wizard button, an organization guide
 *  on step 1, an invalid alias on step 2, and a wizard chunk that fails to load. */
async function edgeScenes(browser, base, theme) {
  const rejected = /rejected by Slack \(invalid_auth\)/

  // Step 3: Slack refuses the bot token; the step stays put with the reason.
  let { context, page } = await openOn(browser, base, theme, makeState({}, { rejectToken: true }))
  await page.getByRole('button', { name: 'I have a workspace' }).click()
  await page.getByTestId('slack-wizard-alias').fill('jdoe')
  await page.getByRole('button', { name: 'I created the app' }).click()
  await page.waitForFunction(() => document.querySelector('[data-testid="slack-wizard"]')?.getAttribute('data-step') === 'bot_token')
  await page.getByTestId('slack-wizard-bot-token').fill('xoxb-placeholder')
  await page.getByRole('button', { name: 'Verify and save' }).click()
  await page.getByText(rejected).waitFor()
  check('rejected bot token stays on step 3', (await stepOf(page)) === 'bot_token')
  await shoot(page, `3b-bot-token-rejected-${theme}.png`)
  await context.close()

  // Step 4: Slack refuses the app-level token.
  ;({ context, page } = await openOn(browser, base, theme,
    makeState({ bot_token_set: true, bot_token_preview: 'xoxb-…abcd' }, { rejectToken: true })))
  check('opens on the app-level token step', (await stepOf(page)) === 'app_token')
  await page.getByTestId('slack-wizard-app-token').fill('xapp-placeholder')
  await page.getByRole('button', { name: 'Verify and save' }).click()
  await page.getByText(rejected).waitFor()
  check('rejected app-level token stays on step 4', (await stepOf(page)) === 'app_token')
  await shoot(page, `4b-app-token-rejected-${theme}.png`)
  await context.close()

  // Step 5 with an owner already saved: the Connect button, then a failed
  // Reconnect, then one still in progress.
  const saved = {
    bot_token_set: true, app_token_set: true, configured: true, owner_id: 'U0123ABC456',
    bot_token_preview: 'xoxb-…abcd', app_token_preview: 'xapp-…abcd',
  }
  const state = makeState(saved, { reconnect: 'fail' })
  ;({ context, page } = await openOn(browser, base, theme, state))
  check('opens on the owner step', (await stepOf(page)) === 'owner')
  await page.getByTestId('slack-wizard-reconnect').waitFor({ state: 'visible' })
  await shoot(page, `5b-owner-saved-connect-${theme}.png`)
  await page.getByTestId('slack-wizard-reconnect').click()
  await page.getByText(/Could not connect:/).waitFor()
  check('failed Reconnect stays on step 5', (await stepOf(page)) === 'owner')
  await shoot(page, `5c-reconnect-failed-${theme}.png`)
  state._stub.reconnect = 'hang'
  await page.getByTestId('slack-wizard-reconnect').click()
  await page.getByText('Connecting…', { exact: true }).waitFor()
  check('Connect is disabled while connecting', await page.getByTestId('slack-wizard-reconnect').isDisabled())
  await shoot(page, `5d-reconnect-in-progress-${theme}.png`)
  await context.close()

  // A read-only remote session: no Setup wizard button, like Save and Reconnect.
  ;({ context, page } = await openOn(browser, base, theme, makeState({ read_only: true }), { open: 'none' }))
  check('read-only has no Setup wizard button', (await page.getByTestId('slack-wizard-open').count()) === 0)
  check('read-only has no wizard', (await page.getByTestId('slack-wizard').count()) === 0)
  await shootPanel(page, `r0-read-only-panel-${theme}.png`)
  await context.close()

  // Step 1 with an organization guide configured (`slack.setup_guide_url`),
  // then step 2 with an alias the manifest cannot render.
  ;({ context, page } = await openOn(browser, base, theme,
    makeState({}, { orgGuide: 'https://example.com/slack-setup' })))
  const orgGuide = page.getByTestId('slack-wizard-org-guide')
  await orgGuide.waitFor({ state: 'visible' })
  check('org guide link', (await orgGuide.getAttribute('href')) === 'https://example.com/slack-setup')
  check('org guide rel', (await orgGuide.getAttribute('rel')) === 'noopener noreferrer')
  await shoot(page, `1b-workspace-org-guide-${theme}.png`)
  await page.getByRole('button', { name: 'I have a workspace' }).click()
  await page.getByTestId('slack-wizard-alias').fill('bad/alias')
  await page.getByText('Use 1 to 32 letters, digits, - or _.').waitFor()
  check('Create stays off for a bad alias',
    (await page.getByTestId('slack-wizard-create').getAttribute('aria-disabled')) === 'true')
  await shoot(page, `2b-create-invalid-alias-${theme}.png`)
  await context.close()

  // The wizard's chunk fails to load. The first failure makes main.tsx's
  // `vite:preloadError` guard reload the tab once; the next click inside its
  // cooldown shows the panel's notice with Try again, as a user would see it.
  ;({ context, page } = await openOn(browser, base, theme, makeState(), {
    open: 'click',
    beforeLoad: ctx => ctx.route(/\/assets\/SlackSetupWizard-[^/]*\.js(\?.*)?$/, r => r.abort()),
  }))
  await page.waitForEvent('framenavigated', { timeout: 20_000 })
  await page.getByTestId('slack-wizard-open').waitFor({ state: 'visible', timeout: 20_000 })
  await page.getByTestId('slack-wizard-open').click()
  await page.getByText('The setup wizard did not load.').waitFor({ timeout: 20_000 })
  await page.getByRole('button', { name: 'Try again' }).first().waitFor({ state: 'visible' })
  check('no wizard rendered', (await page.getByTestId('slack-wizard').count()) === 0)
  await shootPanel(page, `0b-wizard-load-failed-${theme}.png`)
  await context.close()
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  try {
    for (const theme of ['dark', 'light']) {
      const context = await browser.newContext({ viewport: { width: 1280, height: 1000 }, deviceScaleFactor: 2 })
      context._base = base
      const state = makeState()
      const page = await load(context, theme, state)
      check('opens on the workspace step', (await stepOf(page)) === 'workspace')
      check('workspace picture', await page.getByTestId('slack-wizard-diagram-workspace').isVisible())
      await shoot(page, `1-workspace-${theme}.png`)

      await page.getByRole('button', { name: 'I have a workspace' }).click()
      await page.getByTestId('slack-wizard-alias').fill('jdoe')
      await page.waitForFunction(() => document.querySelector('[data-testid="slack-wizard-create"]')?.getAttribute('aria-disabled') === 'false')
      await page.getByText('Slack app name: KiroCrew-jdoe').waitFor() // brand-ok: the rendered app name
      check('create picture', await page.getByTestId('slack-wizard-diagram-create').isVisible())
      await shoot(page, `2-create-${theme}.png`)
      // The link opens Slack in a new tab; close it, nothing leaves this machine.
      await context.route('https://api.slack.com/**', r => r.abort())
      const popup = context.waitForEvent('page')
      await page.getByTestId('slack-wizard-create').click()
      await (await popup).close()
      await page.waitForFunction(() => true)

      await page.getByRole('button', { name: 'I created the app' }).click()
      await page.getByTestId('slack-wizard-bot-token').fill('xoxb-placeholder')
      check('bot token picture', await page.getByTestId('slack-wizard-diagram-bot_token').isVisible())
      await shoot(page, `3-bot-token-${theme}.png`)

      await page.getByRole('button', { name: 'Verify and save' }).click()
      await page.waitForFunction(() => document.querySelector('[data-testid="slack-wizard"]')?.getAttribute('data-step') === 'app_token')
      await page.waitForFunction(u => document.querySelector('[data-testid="slack-wizard-basic-info"]')?.getAttribute('href') === u, STATUS.app_basic_info_url)
      await page.getByTestId('slack-wizard-app-token').fill('xapp-placeholder')
      await shoot(page, `4-app-token-${theme}.png`)

      await page.getByRole('button', { name: 'Verify and save' }).click()
      await page.waitForFunction(() => document.querySelector('[data-testid="slack-wizard"]')?.getAttribute('data-step') === 'owner')
      await page.getByTestId('slack-wizard-owner').fill('U0123ABC456')
      await page.waitForTimeout(400)
      await shoot(page, `5-owner-${theme}.png`)

      await page.getByRole('button', { name: 'Save and connect' }).click()
      await page.waitForFunction(() => document.querySelector('[data-testid="slack-wizard"]')?.getAttribute('data-step') === 'test')
      await page.waitForFunction(u => document.querySelector('[data-testid="slack-wizard-dm"]')?.getAttribute('href') === u, STATUS.app_dm_url)
      check('the Create click saved the alias command', state.command === 'kirocrew-jdoe')
      await shoot(page, `6-test-${theme}.png`)
      await page.getByRole('button', { name: 'I got a reply' }).click()
      await page.getByText('Slack is set up.').waitFor()
      await shoot(page, `7-done-${theme}.png`)
      // The whole panel, with the wizard open above the form, then closed.
      await page.evaluate(() => window.scrollTo(0, 0))
      await page.screenshot({ path: join(OUT, `8-panel-wizard-open-${theme}.png`), fullPage: true })
      await page.getByTestId('slack-wizard-close').click()
      await page.getByTestId('slack-wizard-open').waitFor()
      await page.evaluate(() => window.scrollTo(0, 0))
      await page.screenshot({ path: join(OUT, `9-panel-wizard-closed-${theme}.png`) })
      await context.close()
      await edgeScenes(browser, base, theme)
      console.log(`${theme}: OK`)
    }
    console.log(`\nOK — screenshots in ${OUT}`)
  } finally {
    await browser.close()
    srv.close()
  }
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})

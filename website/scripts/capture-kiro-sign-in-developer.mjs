/**
 * Screenshot harness for the Kiro sign-in card's move: Settings > Overview →
 * Developer > Agent Backend.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server and answers every /api/** call from fixtures via Playwright route
 * interception — gateway-free, no kiro-cli, no dashboard auth.
 *
 * Seven frames, each pinning one half of the move:
 *   chat-auth-required-row.png      the chat error row an `AcpAuthRequired` turn
 *                                   produces (the backend's signed-out remedy
 *                                   text) with its "Sign in to Kiro" action, the
 *                                   door that opens KIRO_SIGN_IN_PATH
 *   overview.png                    Settings > Overview with no sign-in card
 *                                   above the guided cards (Kiro CLI selected)
 *   overview-kas-signpost.png       the same page with KAS selected: the
 *                                   one-line "moved to" signpost where the
 *                                   card used to sit, and still no chooser
 *   agent-backend-deep-link.png     arrival through KIRO_SIGN_IN_PATH (the chat
 *                                   error row's link): the card scrolled into
 *                                   view and ringed by useSettingHighlight
 *   agent-backend-signed-out.png    the provider chooser under the backend
 *                                   switch (Kiro CLI still the pressed option)
 *   agent-backend-signed-in.png     the signed-in summary in the same place
 *   agent-backend-no-kas.png        a deployment that cannot select KAS: the
 *                                   switch without the KAS option AND without
 *                                   the card
 *
 * Usage: node scripts/capture-kiro-sign-in-developer.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { KIROCREW_CONFIG_FIXTURE, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../.github/screenshots/kiro-sign-in-developer'
mkdirSync(OUT, { recursive: true })

/** `GET /api/config/schema` advertising exactly `values` for the backend field. */
const schemaWith = values => ({
  entries: [
    {
      path: 'agent.acp_backend',
      type: 'enum',
      label: 'Agent backend',
      help: '',
      tags: [],
      enumValues: values,
      defaultValue: '',
    },
  ],
})

/** One `GET /api/acp-backends` row: selectable and installed unless overridden. */
const probeRow = (id, over = {}) => ({
  id,
  policy_id: id || 'kiro',
  selectable: true,
  installed: 'installed',
  missing_components: [],
  install_command: '',
  restart_required: false,
  ...over,
})

const SIGNED_OUT = {
  authenticated: false,
  provider: '',
  identity: '',
  transport: 'device',
  expires_at: null,
  expired: false,
  has_refresh_token: false,
  refresh_rejected: false,
  usable: false,
}

/**
 * The transcript an `AcpAuthRequired` turn leaves behind: the user's prompt and
 * the terminal error row the gateway stamps `kind: 'auth_required'`, carrying
 * the backend's signed-out remedy verbatim (`_KAS_SIGNED_OUT` in
 * `agent_sdk/host_auth.py`). Served through the slot detail route, which is
 * what ChatPage refetches on mount and REPLACES the transcript with.
 */
const AUTH_REQUIRED_SLOT = {
  key: 'kas-signed-out',
  title: 'Deploy notes',
  messages: [
    { role: 'user', content: 'Summarize what changed in the deploy branch since Monday.', cls: '', ts: '2026-09-11T09:12:00Z' },
    {
      role: 'error',
      kind: 'auth_required',
      content:
        'Not signed in to Kiro. Sign in again from Developer → Agent Backend → Kiro sign-in, or run `kiro-cli login` in your terminal if kiro-cli owns the sign-in, then start a new chat.',
      cls: '',
      ts: '2026-09-11T09:12:03Z',
    },
  ],
}

const SIGNED_IN = {
  authenticated: true,
  provider: 'Google',
  identity: 'social',
  transport: 'loopback',
  expires_at: new Date(Date.now() + 55 * 60 * 1000).toISOString(),
  expired: false,
  has_refresh_token: true,
  refresh_rejected: false,
  usable: true,
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const shot = []

  const openPage = async ({ backends, kasLogin, selected = '', slot = null }) => {
    const context = await browser.newContext({
      viewport: { width: 1400, height: 900 },
      deviceScaleFactor: 2, // 12-13px type renders soft at 1x on GitHub
    })
    const page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, {
      theme: 'dark',
      // Developer Mode on, so the Developer rail row exists in the frames; the
      // /developer route itself does not depend on it.
      localStorageEntries: { 'mc-dev-mode': '1', ...(slot ? { 'mc-active-slot': slot.key } : {}) },
      slots: slot ? [{ key: slot.key, messages: slot.messages.length, running: false, agent: 'kirocrew', mode: '' }] : [],
      extra: async (path, route) => {
        const fixture =
          slot && path === `/api/chat/slots/${slot.key}`
            ? { key: slot.key, title: slot.title, running: false, has_more: false, total: slot.messages.length, messages: slot.messages }
          : path === '/api/config/kirocrew'
            ? { ...KIROCREW_CONFIG_FIXTURE, agent: { ...(KIROCREW_CONFIG_FIXTURE.agent ?? {}), acp_backend: selected } }
            : path === '/api/config/schema'
            ? schemaWith(backends)
            : path === '/api/acp-backends'
              ? { backends: backends.map(id => probeRow(id)) }
              : path === '/api/kas-login'
                ? kasLogin
                : undefined
        if (fixture === undefined) return false
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(fixture) })
        return true
      },
    })
    return page
  }
  const save = async (page, name) => {
    await page.screenshot({ path: `${OUT}/${name}.png` })
    shot.push(`${name}.png`)
  }

  const ALL = ['', 'claude', 'kas']

  {
    const page = await openPage({ backends: ALL, kasLogin: SIGNED_OUT })
    await page.goto(base + '/settings/overview', { waitUntil: 'domcontentloaded' })
    await page.getByRole('heading', { name: /memory/i }).waitFor({ state: 'visible', timeout: 15000 })
    if (await page.getByRole('button', { name: /continue with/i }).count() > 0) {
      throw new Error('Overview still renders the Kiro sign-in chooser')
    }
    await page.waitForTimeout(600) // let the tiles' rise animation finish
    if (await page.getByTestId('kiro-sign-in-moved').count() > 0) {
      throw new Error('Overview signposts the sign-in card although Kiro CLI is selected')
    }
    await save(page, 'overview')
    await page.context().close()
  }

  {
    const page = await openPage({ backends: ALL, kasLogin: SIGNED_IN, selected: 'kas' })
    await page.goto(base + '/settings/overview', { waitUntil: 'domcontentloaded' })
    await page.getByTestId('kiro-sign-in-moved').waitFor({ state: 'visible', timeout: 15000 })
    if (await page.getByRole('button', { name: /continue with/i }).count() > 0) {
      throw new Error('Overview renders the Kiro sign-in chooser under KAS')
    }
    await page.waitForTimeout(600)
    await save(page, 'overview-kas-signpost')
    await page.context().close()
  }

  {
    // The chat error row's link: land on the tab, wait for the card (it mounts
    // after the tab's config query), and catch the 2s ring the highlight hook
    // paints -- so the frame is taken as soon as the outline is on the card.
    const page = await openPage({ backends: ALL, kasLogin: SIGNED_OUT, selected: 'kas' })
    await page.goto(base + '/developer?tab=agent-backend&highlight=key%3Akiro-sign-in', { waitUntil: 'domcontentloaded' })
    await page.waitForFunction(
      () => (document.querySelector('[data-setting-key="kiro-sign-in"]')?.style.outline ?? '') !== '',
      null,
      { timeout: 15000 },
    )
    if (new URL(page.url()).searchParams.has('highlight')) {
      throw new Error('highlight param survived the landing')
    }
    await save(page, 'agent-backend-deep-link')
    await page.context().close()
  }

  {
    // The door itself: the error row's action is the only in-product route to
    // the card for a user who has not opened the Developer page.
    const page = await openPage({ backends: ALL, kasLogin: SIGNED_OUT, selected: 'kas', slot: AUTH_REQUIRED_SLOT })
    await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
    await page.getByText(/Not signed in to Kiro/).first().waitFor({ state: 'visible', timeout: 15000 })
    const signIn = page.getByRole('button', { name: 'Sign in to Kiro' })
    await signIn.waitFor({ state: 'visible', timeout: 15000 })
    await page.waitForTimeout(600)
    await save(page, 'chat-auth-required-row')
    // Press it: the row's door must land on the ringed card, the same arrival
    // the previous frame shows.
    await signIn.click()
    await page.waitForFunction(
      () => (document.querySelector('[data-setting-key="kiro-sign-in"]')?.style.outline ?? '') !== '',
      null,
      { timeout: 15000 },
    )
    await page.context().close()
  }

  for (const [name, kasLogin, expectText] of [
    ['agent-backend-signed-out', SIGNED_OUT, /continue with google/i],
    ['agent-backend-signed-in', SIGNED_IN, /signed in with google/i],
  ]) {
    const page = await openPage({ backends: ALL, kasLogin })
    await page.goto(base + '/developer?tab=agent-backend', { waitUntil: 'domcontentloaded' })
    await page.getByRole('button', { name: /^kas/i }).waitFor({ state: 'visible', timeout: 15000 })
    await page.getByText(expectText).waitFor({ state: 'visible', timeout: 15000 })
    await page.waitForTimeout(600)
    await save(page, name)
    await page.context().close()
  }

  {
    const page = await openPage({ backends: ['', 'claude'], kasLogin: SIGNED_OUT })
    await page.goto(base + '/developer?tab=agent-backend', { waitUntil: 'domcontentloaded' })
    await page.getByRole('button', { name: /claude code/i }).waitFor({ state: 'visible', timeout: 15000 })
    await page.waitForTimeout(600)
    if (await page.getByRole('button', { name: /continue with/i }).count() > 0) {
      throw new Error('Sign-in card rendered although KAS is not selectable')
    }
    await save(page, 'agent-backend-no-kas')
    await page.context().close()
  }

  await browser.close()
  srv.close()
  console.log(`wrote ${shot.length} frame(s) to ${OUT}: ${shot.join(', ')}`)
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})

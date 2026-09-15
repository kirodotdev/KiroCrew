/**
 * Video harness for the WHATSAPP CHANNEL pairing flow.
 *
 * The panel's states are photographed by `capture-whatsapp-panel.mjs`; this
 * records the TRANSITIONS between them, which a still cannot show: an operator
 * arriving at an unpaired channel where the control is inert, the channel coming
 * up and the code becoming available, the QR card appearing, and the panel
 * settling into the connected view with the access policy revealed.
 *
 * The inert first beat is deliberate. Pairing is STARTED by the channel's own
 * `connect()` and by no dashboard call, so the recording advances the stub the
 * way a gateway restart would rather than clicking a button that is disabled
 * until it happens. An earlier cut clicked first and recorded a timeout.
 *
 * The QR is a code for fixed placeholder text, never a live pairing code: a real
 * one is a credential, and whoever scans it links their phone to the operator's
 * account.
 *
 * Emits a .webm from Playwright and, when ffmpeg is present, an .mp4 and a .gif
 * beside it, because a GitHub PR body embeds a raw-URL GIF but not a raw-URL
 * webm.
 *
 * Usage: node scripts/capture-whatsapp-panel-video.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, renameSync, existsSync } from 'node:fs'
import { join } from 'node:path'
import { execFileSync } from 'node:child_process'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'
import { QR_PNG } from './lib/whatsapp-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/whatsapp-panel'
const LABEL = 'whatsapp-pairing-flow'
const VIEW = { width: 1280, height: 900 }

/** Mutable so the routes can advance the channel through its lifecycle. */
const state = {
  config: {
    configured: false,
    connected: false,
    connect_error: '',
    read_only: false,
    enabled: true,
    dm_policy: 'self',
    allowed_wa_ids: [],
    groups: [],
    session_folder: '',
    state: 'unpaired',
  },
  qr: { state: 'unpaired', qr_data_url: null, detail: '' },
}

function routes() {
  return async (path, route) => {
    if (path === '/api/whatsapp/config') return json(route, state.config), true
    if (path === '/api/channels/whatsapp/qr/status') return json(route, state.qr), true
    if (path === '/api/whatsapp/groups') return json(route, { groups: [] }), true
    if (path === '/api/governance/channels') return json(route, { whatsapp: true }), true
    if (path === '/api/channels/whatsapp/qr/start') {
      state.qr = { state: 'pairing', qr_data_url: QR_PNG, detail: '' }
      state.config = { ...state.config, state: 'pairing' }
      return json(route, { ok: true }), true
    }
    return false
  }
}

const wait = ms => new Promise(r => setTimeout(r, ms))

async function main() {
  mkdirSync(OUT, { recursive: true })
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: VIEW,
    deviceScaleFactor: 1, // a 2x video is needlessly heavy for a PR body
    recordVideo: { dir: OUT, size: VIEW },
  })
  const page = await context.newPage()
  logPageProblems(page)
  try {
    await stubDashboardApi(page, { theme: 'dark', extra: routes() })
    await page.goto(base + '/settings?tab=channels&channel=whatsapp', {
      waitUntil: 'domcontentloaded',
    })
    await page.getByText(/WhatsApp/).first().waitFor({ state: 'visible', timeout: 20000 })
    // The unpaired state, code UNAVAILABLE: the control is inert and the panel
    // says what would produce a code. That is a scene, not a dead frame — it is
    // the step every first-time operator is actually stopped at.
    await wait(2400)

    // The gateway starts the channel, which is what BEGINS pairing. Nothing the
    // dashboard can call does this, so the recording has to model it rather than
    // click for it — and the button is disabled until it happens.
    state.config = { ...state.config, state: 'pairing' }
    state.qr = { state: 'pairing', qr_data_url: QR_PNG, detail: '' }
    await wait(2000)

    // Addressed by test id, not by label. This waited on `/Pair/` while the panel
    // ships "Show pairing code", so it timed out AFTER recording had started and
    // the artifact was written from a half-finished flow. A harness whose
    // selectors track user-facing copy re-breaks on every wording change, and the
    // failure is a stale video rather than a red test.
    const pair = page.getByTestId('whatsapp-connect')
    await pair.waitFor({ state: 'visible', timeout: 15000 })
    await pair.click()
    await wait(2600) // the card with the code

    // The phone scans: the channel comes up and the policy controls appear.
    state.config = { ...state.config, configured: true, connected: true, state: 'connected' }
    state.qr = { state: 'connected', qr_data_url: null, detail: '' }
    await wait(2600)

    // And the access policy an operator would set next. Awaited rather than
    // probed with `isVisible()`: that guard silently skipped this whole segment
    // once the label it matched on changed, and a recording that quietly drops a
    // scene is indistinguishable from one that never had it.
    const policy = page.getByTestId('whatsapp-dm-policy').getByRole('combobox').first()
    await policy.waitFor({ state: 'visible', timeout: 15000 })
    await policy.click()
    await wait(1200)
    const option = page.getByRole('option', { name: /allowed numbers/ }).first()
    await option.waitFor({ state: 'visible', timeout: 15000 })
    state.config = {
      ...state.config,
      dm_policy: 'allowlist',
      allowed_wa_ids: ['447700900111'],
    }
    await option.click()
    await wait(2200)
  } finally {
    const video = page.video()
    await context.close() // finalizes the recording
    await browser.close()
    srv.close()
    if (video) {
      const src = await video.path()
      const webm = join(OUT, `${LABEL}.webm`)
      renameSync(src, webm)
      console.log('wrote', webm)
      try {
        const mp4 = join(OUT, `${LABEL}.mp4`)
        execFileSync('ffmpeg', ['-y', '-i', webm, '-movflags', 'faststart',
          '-pix_fmt', 'yuv420p', '-vf', 'scale=1280:-2', mp4], { stdio: 'ignore' })
        console.log('wrote', mp4)
        const gif = join(OUT, `${LABEL}.gif`)
        execFileSync('ffmpeg', ['-y', '-i', webm,
          '-vf', 'fps=8,scale=980:-1:flags=lanczos,split[a][b];[a]palettegen[p];[b][p]paletteuse',
          gif], { stdio: 'ignore' })
        console.log('wrote', gif)
      } catch {
        console.log('ffmpeg unavailable or failed; the .webm alone is the artifact')
      }
      if (!existsSync(webm)) console.error('video missing after finalize')
    }
  }
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})

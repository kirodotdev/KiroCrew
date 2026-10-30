/**
 * Screenshot harness for Settings > Voice with `stt.model = custom`.
 *
 * Sibling of `capture-stt-local-voice.mjs` and identical in method: the REAL built
 * SPA (website/dist) behind the shared `serveDist` server, with every /api/** call
 * answered from fixtures through `stubDashboardApi`. No gateway, no recogniser, no
 * network.
 *
 * Six frames:
 *   1. custom selected, file in the models folder -> the settled everyday case
 *   2. custom selected, file missing               -> the reason, with the hand-off
 *   3. pair incomplete, selection degraded         -> the self-explaining line
 *   4. a file name outside the folder is saved     -> the refusal under the rows
 *   5. a complete pair, catalog model selected     -> the rows say they are idle
 *   6. "Work out the SHA-256 yourself" opened      -> the command for each system
 *
 * Usage: node scripts/capture-stt-custom-model.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/stt-custom-model-shots'
mkdirSync(OUT, { recursive: true })

/** The catalog the gateway serves, byte sizes included so the UI can price a click. */
const CATALOG = [
  { name: 'tiny', size_bytes: 77_691_713, present: false },
  { name: 'base', size_bytes: 147_951_465, present: true },
  { name: 'small', size_bytes: 487_601_967, present: false },
  { name: 'large-v3-turbo', size_bytes: 1_624_555_275, present: false },
]

/**
 * The row `GET /api/stt/status` appends for a configured custom model. A custom
 * model has no published size, and `error` says why an absent file is unusable.
 */
const customRow = (present, error = '') => ({ name: 'custom', size_bytes: 0, present, error })

const IDLE_DOWNLOAD = { step: 'idle', model: '', downloaded_bytes: 0, total_bytes: 0, error: '' }

/** A real published whisper.cpp ggml digest shape -- 64 hex, not a row of zeros. */
const SHA = '3fa1b2c9d84e7605f1c2a3b4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708'
const FILE = 'ggml-medium.en-q5_0.bin'
const DIR = '/home/you/.kiro/crew/models/whisper'

/**
 * The text-to-speech half of this page, which this change does not touch.
 *
 * Stubbed anyway and completely, for the reason the sibling records: under the
 * catch-all's `{}` the TTS card renders its provider as an em dash and its speed
 * as the literal `undefined`, and a reviewer reading the screenshot would
 * reasonably take that for damage this PR did.
 */
const VOICE_CONFIG_FIXTURE = {
  enabled: false,
  autoSpeak: false,
  provider: 'system',
  voice: 'Ruth',
  engine: 'generative',
  rate: '100%',
  aws_profile: '',
  region: 'us-east-1',
}

const STT_CONFIG = {
  enabled: true,
  provider: 'local',
  model: 'base',
  language_code: 'en-US',
  streaming: true,
  silence_ms: 700,
  partial_interval_ms: 400,
  idle_evict_secs: 600,
  endpointing: false,
  dictation_panel: true,
  timeout_secs: 300,
  transcribe_region: 'us-east-1',
  transcribe_profile: '',
  custom_model_path: '',
  custom_model_sha256: '',
  custom_model_dir: DIR,
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()

/** Open Settings > Voice with the gateway's STT answers seeded. */
async function openVoiceSettings({ config = {}, status = {}, putError = null } = {}) {
  // Taller than the sibling's on purpose: the custom pair adds two input rows and
  // an explanation line to the same card. An element shot of a card taller than
  // the viewport captures the un-rendered region below the fold as blank, which
  // reads as a broken layout rather than a cropped one.
  const context = await browser.newContext({
    viewport: { width: 1180, height: 2200 },
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()

  const cfg = { ...STT_CONFIG, ...config }
  const st = {
    available: true,
    code: '',
    detail: '',
    provider: cfg.provider,
    model: cfg.model,
    model_present: true,
    model_bytes: 147_951_465,
    engine_loaded: false,
    models: CATALOG,
    download: IDLE_DOWNLOAD,
    ...status,
  }

  const extra = async (path, route) => {
    if (path === '/api/config/stt') {
      if (putError && route.request().method() === 'PUT') {
        await json(route, putError, 400)
        return true
      }
      await json(route, cfg)
      return true
    }
    if (path === '/api/stt/status') {
      await json(route, st)
      return true
    }
    if (path === '/api/voice/config') {
      await json(route, VOICE_CONFIG_FIXTURE)
      return true
    }
    return false
  }

  await stubDashboardApi(page, { extra })
  // Pin the locale: without it the SPA negotiates one from the environment and the
  // shot comes out in whatever language the runner happens to prefer.
  await page.addInitScript(() => localStorage.setItem('mc-lang', 'en'))
  await page.goto(`${base}/settings?tab=voice`, { waitUntil: 'domcontentloaded' })
  // Anchored on the panel's own control rather than a timeout: the card renders
  // only once both queries have answered.
  const heading = page.getByText('Speech-to-Text', { exact: true }).first()
  await heading.waitFor({ timeout: 15_000 })
  await heading.scrollIntoViewIfNeeded()
  await page.waitForTimeout(1200)
  return { context, page, heading }
}

/**
 * Shoot the speech-to-text card.
 *
 * The heading sits inside its own header row, so the card is that ROW's next
 * sibling, not the heading's. Getting it wrong is silent: the locator resolves to
 * nothing and a fallback photographs the whole viewport instead, which looks like
 * a working harness and is not the surface under review -- so an empty locator
 * throws here rather than falling back.
 *
 * Deliberately not `fullPage` and not the viewport: the page also carries the
 * text-to-speech card, which this change does not touch, and including it invites
 * a reviewer to read an unrelated row as part of the diff.
 */
const shoot = async (page, heading, name) => {
  const out = join(OUT, name)
  const card = heading.locator('xpath=../following-sibling::*[1]')
  if (!(await card.count())) throw new Error(`could not locate the STT card for ${name}`)
  await card.screenshot({ path: out })
  console.log('wrote', out)
}

// 1 - the settled everyday case: `custom` selected, the file is in the models
//     folder and matched its SHA-256.
{
  const { context, page, heading } = await openVoiceSettings({
    config: { model: 'custom', custom_model_path: FILE, custom_model_sha256: SHA },
    status: { model: 'custom', model_bytes: 0, models: [...CATALOG, customRow(true)] },
  })
  await shoot(page, heading, '01-custom-configured.png')
  await context.close()
}

// 2 - the same pair after the file was removed. Nothing is downloaded for a custom
//     model, so the panel says why it cannot be used, with the agent hand-off.
{
  const { context, page, heading } = await openVoiceSettings({
    config: { model: 'custom', custom_model_path: FILE, custom_model_sha256: SHA },
    status: {
      model: 'custom',
      model_present: false,
      model_bytes: 0,
      models: [...CATALOG, customRow(false, 'No file with that name is in the speech models folder.')],
    },
  })
  await shoot(page, heading, '02-custom-file-missing.png')
  await context.close()
}

// 3 - half a pair. The file is set and the digest is not, so the gateway degraded
//     the selection to the default model and the picker reads `base`. The line
//     under the rows stops that reading as a select that ignored the click.
{
  const { context, page, heading } = await openVoiceSettings({
    config: { model: 'base', custom_model_path: FILE, custom_model_sha256: '' },
    status: { model: 'base' },
  })
  await shoot(page, heading, '03-custom-pair-incomplete.png')
  await context.close()
}

// 4 - a name that resolves outside the models folder is saved. The gateway refuses
//     it with a 400 naming the rule; the typed text stays in the row.
{
  const { context, page, heading } = await openVoiceSettings({
    config: { model: 'custom', custom_model_path: FILE, custom_model_sha256: SHA },
    status: { model: 'custom', model_bytes: 0, models: [...CATALOG, customRow(true)] },
    putError: {
      error: 'The model file must be inside the speech models folder.',
      code: 'custom_model_path_outside',
    },
  })
  const field = page.getByLabel('Custom model file')
  await field.fill('../../other/ggml-model.bin')
  await field.blur()
  await page.getByTestId('stt-custom-model-error').waitFor({ timeout: 10_000 })
  await shoot(page, heading, '04-custom-path-refused.png')
  await context.close()
}

// 5 - a complete pair is stored but a catalog model is selected. The rows stay
//     open so the file can be corrected or cleared, and say they are not in use.
{
  const { context, page, heading } = await openVoiceSettings({
    config: { model: 'base', custom_model_path: FILE, custom_model_sha256: SHA },
    status: { model: 'base' },
  })
  await shoot(page, heading, '05-custom-rows-idle.png')
  await context.close()
}

// 6 - the terminal route under the SHA-256 box, opened: the explanation and the
//     command for each system, with the configured file name quoted for that shell.
{
  const { context, page, heading } = await openVoiceSettings({
    config: { model: 'custom', custom_model_path: FILE, custom_model_sha256: SHA },
    status: { model: 'custom', model_bytes: 0, models: [...CATALOG, customRow(true)] },
  })
  await page.getByText('Work out the SHA-256 yourself', { exact: true }).click()
  await page.getByText('Windows PowerShell', { exact: true }).waitFor({ timeout: 10_000 })
  await shoot(page, heading, '06-custom-hash-commands-open.png')
  await context.close()
}

await browser.close()
srv.close()

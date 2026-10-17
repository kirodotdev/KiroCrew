/**
 * Screenshot harness for Settings > Voice after the STT provider convergence.
 *
 * Runs the REAL built SPA (website/dist) behind the shared `serveDist` server and
 * answers every /api/** call from fixtures through `stubDashboardApi`. No gateway,
 * no kiro-cli, no recogniser.
 *
 * The frames are FIXTURE STATES rather than a click sequence, because the three
 * things worth showing are steady states the panel reaches from the gateway's
 * answer, not from anything a user does in the browser:
 *   1. local provider, model on disk        -> the settled everyday case
 *   2. local provider, model downloading    -> the one-time first-run transfer
 *   3. recogniser not installed             -> the coded, actionable refusal
 *
 * The provider list itself gets no frame: it is a native <select>, whose popup the
 * OS renders outside the page, so a screenshot of it is indistinguishable from a
 * screenshot of the closed control. `SttProviders.test.ts` pins that set instead.
 *
 * State 3 is the one that most needs a picture: the panel maps each machine-readable
 * availability code to its own localised sentence, so "install the voice extra" and
 * "your platform has no prebuilt wheel" read as different actions rather than one
 * generic failure.
 *
 * Usage: node scripts/capture-stt-local-voice.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/stt-local-voice-shots'
mkdirSync(OUT, { recursive: true })

/** The catalog the gateway serves, byte sizes included so the UI can price a click. */
const MODELS = [
  { name: 'tiny', size_bytes: 77_691_713, present: false },
  { name: 'base', size_bytes: 147_951_465, present: true },
  { name: 'small', size_bytes: 487_601_967, present: false },
  { name: 'large-v3-turbo', size_bytes: 1_624_555_275, present: false },
]

const IDLE_DOWNLOAD = { step: 'idle', model: '', downloaded_bytes: 0, total_bytes: 0, error: '' }

/**
 * The text-to-speech half of this page, which this change does not touch.
 *
 * Stubbed anyway and completely: under the catch-all's `{}` the TTS card renders
 * its provider as an em dash and its speed as the literal `undefined`, and a
 * reviewer reading the screenshot would reasonably take that for damage this PR
 * did. Shipping evidence that misreports the surface next to the one under review
 * is worse than shipping no evidence.
 */
const VOICE_CONFIG_FIXTURE = {
  enabled: false,
  autoSpeak: false,
  provider: 'piper',
  voice: 'Ruth',
  engine: 'generative',
  rate: '100%',
  aws_profile: '',
  region: 'us-east-1',
  piper_binary: '',
  piper_model: '~/piper/en_US-lessac-medium.onnx',
  piper_model_config: '',
  piper_length_scale: 1.0,
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
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()

/** Open Settings > Voice with the gateway's STT answers seeded. */
async function openVoiceSettings({ config = {}, status = {} } = {}) {
  // Tall enough that the WHOLE speech-to-text card renders. An element shot of a
  // card taller than the viewport captures the un-rendered region below the fold
  // as blank, which reads as a broken layout rather than a cropped one.
  const context = await browser.newContext({
    viewport: { width: 1180, height: 1800 },
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
    models: MODELS,
    download: IDLE_DOWNLOAD,
    ...status,
  }

  const extra = async (path, route) => {
    if (path === '/api/config/stt') {
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
 * a working harness and is not the surface under review.
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

// 1 - the settled everyday case: local provider, model already on disk.
{
  const { context, page, heading } = await openVoiceSettings()
  await shoot(page, heading, '01-local-ready.png')
  await context.close()
}

// 2 - the one-time first-run transfer, mid-flight. The byte counts are what makes
//     this distinguishable from a hang, which is the whole reason they are reported.
{
  const { context, page, heading } = await openVoiceSettings({
    config: { model: 'small' },
    status: {
      model: 'small',
      model_present: false,
      model_bytes: 487_601_967,
      download: {
        step: 'downloading',
        model: 'small',
        downloaded_bytes: 208_642_048,
        total_bytes: 487_601_967,
        error: '',
      },
    },
  })
  await shoot(page, heading, '02-model-downloading.png')
  await context.close()
}

// 3 - the recogniser is not installed. A coded refusal, so the panel can say what
//     to actually do instead of rendering an untranslated backend sentence.
{
  const { context, page, heading } = await openVoiceSettings({
    status: {
      available: false,
      code: 'stt_extra_missing',
      detail:
        "speech recognition needs its voice dependencies: pip install 'boto3>=1.34,<2' "
        + "'amazon-transcribe>=0.6,<1' 'pywhispercpp>=1.5,<2'",
      model_present: false,
      models: MODELS.map((m) => ({ ...m, present: false })),
    },
  })
  await shoot(page, heading, '03-recogniser-not-installed.png')
  await context.close()
}

await browser.close()
srv.close()

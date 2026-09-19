/**
 * Screenshot harness for Settings > Voice after `stt.model = custom` was added.
 *
 * Sibling of `capture-stt-local-voice.mjs` and deliberately identical in method:
 * the REAL built SPA (website/dist) behind the shared `serveDist` server, with
 * every /api/** call answered from fixtures through `stubDashboardApi`. No
 * gateway, no kiro-cli, no recogniser, no network.
 *
 * The frames are FIXTURE STATES rather than a click sequence, for the same reason
 * the sibling gives: what is worth showing are steady states the panel reaches
 * from the gateway's answer. Three of them, and each one is a branch this change
 * introduced:
 *   1. custom selected, weights on disk        -> the settled everyday case
 *   2. custom selected, weights not fetched    -> the UNSIZED download prompt
 *   3. pair incomplete, selection degraded     -> the self-explaining refusal
 *
 * Why 2 earns its own frame rather than reusing the sibling's transfer shot: a
 * catalog row carries a published size and a custom row cannot, so the panel has
 * a separate sentence for it. The sized version of that string would render
 * "0 B" against a custom model and read as a free download, so the branch exists
 * precisely to avoid a wrong number and a picture is the only way to show that it
 * does.
 *
 * Why 3 is the most important frame: `custom` with half a pair does not fail, it
 * DEGRADES to the default model, so the picker legitimately reads `base` after a
 * user selected `custom`. Without the explanation line that looks exactly like the
 * select ignoring the click. The frame is the evidence that it does not.
 *
 * The model picker's own popup gets no frame: it is a native <select>, whose list
 * the OS draws outside the page, so a screenshot of it open is indistinguishable
 * from one of it closed. The option set is pinned by test instead.
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
 * The row `GET /api/stt/status` appends for a configured custom model.
 *
 * `size_bytes: 0` is not a placeholder, it is the real contract: a caller-pinned
 * URL has no published size, and 0 is what the panel reads as "size unknown".
 */
const customRow = (present) => ({ name: 'custom', size_bytes: 0, present })

const IDLE_DOWNLOAD = { step: 'idle', model: '', downloaded_bytes: 0, total_bytes: 0, error: '' }

/** A real published whisper.cpp ggml digest shape -- 64 hex, not a row of zeros. */
const SHA = '3fa1b2c9d84e7605f1c2a3b4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708'
const URL = 'https://models.example.org/ggml-medium-en-q5_0.bin'

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
  custom_model_url: '',
  custom_model_sha256: '',
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()

/** Open Settings > Voice with the gateway's STT answers seeded. */
async function openVoiceSettings({ config = {}, status = {} } = {}) {
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

// 1 - the settled everyday case: `custom` selected, both halves pinned, weights
//     already on disk. The two rows carry the values the digest was checked
//     against, which is the whole point of moving the pin to the caller.
{
  const { context, page, heading } = await openVoiceSettings({
    config: { model: 'custom', custom_model_url: URL, custom_model_sha256: SHA },
    status: {
      model: 'custom',
      model_bytes: 0,
      models: [...CATALOG, customRow(true)],
    },
  })
  await shoot(page, heading, '01-custom-configured.png')
  await context.close()
}

// 2 - the same pair, before the one-time fetch. The prompt is the UNSIZED string:
//     a custom row publishes no size, and the sized sentence beside it would say
//     "0 B" here and read as a free download.
{
  const { context, page, heading } = await openVoiceSettings({
    config: { model: 'custom', custom_model_url: URL, custom_model_sha256: SHA },
    status: {
      model: 'custom',
      model_present: false,
      model_bytes: 0,
      models: [...CATALOG, customRow(false)],
    },
  })
  await shoot(page, heading, '02-custom-not-downloaded.png')
  await context.close()
}

// 3 - half a pair. The URL is configured and the digest is not, so the gateway
//     degraded the selection to the default model rather than failing the voice
//     session -- and the picker therefore reads `base`. The line under the rows is
//     what stops that reading as a select that ignored the click.
{
  const { context, page, heading } = await openVoiceSettings({
    config: { model: 'base', custom_model_url: URL, custom_model_sha256: '' },
    status: { model: 'base' },
  })
  await shoot(page, heading, '03-custom-pair-incomplete.png')
  await context.close()
}

await browser.close()
srv.close()

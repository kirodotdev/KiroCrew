/**
 * Screenshot harness for Settings > Voice with the `off` speech provider.
 *
 * Runs the REAL built SPA (website/dist) behind the shared `serveDist` server and
 * answers every /api/** call from fixtures through `stubDashboardApi`. No gateway,
 * no kiro-cli, no recogniser.
 *
 * Two frames, both steady states the panel reaches from the gateway's answer:
 *   1. the provider control with `off` SELECTED, and the availability line that
 *      names the provider control rather than the Enabled toggle
 *      (`stt_provider_off`, not `stt_disabled`)
 *   2. the provider control's option list open, so `Off` is visible among its
 *      siblings. The control is the shared `SimpleSelect` popover, rendered in
 *      the page, so the open list is a real frame rather than an OS popup.
 *
 * Usage: node scripts/capture-stt-provider-off.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/stt-provider-off-shots'
mkdirSync(OUT, { recursive: true })

const MODELS = [
  { name: 'tiny', size_bytes: 77_691_713, present: false },
  { name: 'base', size_bytes: 147_951_465, present: true },
  { name: 'small', size_bytes: 487_601_967, present: false },
  { name: 'large-v3-turbo', size_bytes: 1_624_555_275, present: false },
]

const IDLE_DOWNLOAD = { step: 'idle', model: '', downloaded_bytes: 0, total_bytes: 0, error: '' }

/** The text-to-speech card, stubbed completely so it cannot read as damage. */
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
  provider: 'off',
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
  // What `GET /api/config/stt` serves from `_stt_providers()`: `off` is selectable
  // everywhere; `apple` is omitted on a non-macOS host exactly as the backend does.
  providers: ['local', 'transcribe', 'off'],
  streaming_providers: ['local', 'apple', 'transcribe'],
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()

async function openVoiceSettings() {
  const context = await browser.newContext({
    viewport: { width: 1180, height: 1800 },
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()
  const status = {
    available: false,
    code: 'stt_provider_off',
    detail: 'the speech provider is set to off; choose a provider',
    provider: 'off',
    model: 'base',
    model_present: true,
    model_bytes: 147_951_465,
    engine_loaded: false,
    models: MODELS,
    download: IDLE_DOWNLOAD,
  }
  const extra = async (path, route) => {
    if (path === '/api/config/stt') {
      await json(route, STT_CONFIG)
      return true
    }
    if (path === '/api/stt/status') {
      await json(route, status)
      return true
    }
    if (path === '/api/voice/config') {
      await json(route, VOICE_CONFIG_FIXTURE)
      return true
    }
    return false
  }
  await stubDashboardApi(page, { extra })
  await page.addInitScript(() => localStorage.setItem('mc-lang', 'en'))
  await page.goto(`${base}/settings?tab=voice`, { waitUntil: 'domcontentloaded' })
  const heading = page.getByText('Speech-to-Text', { exact: true }).first()
  await heading.waitFor({ timeout: 15_000 })
  await heading.scrollIntoViewIfNeeded()
  await page.waitForTimeout(1200)
  return { context, page, heading }
}

const card = (heading) => heading.locator('xpath=../following-sibling::*[1]')

// 1 - `off` selected, and the status line that names the provider control.
{
  const { context, page, heading } = await openVoiceSettings()
  const target = card(heading)
  if (!(await target.count())) throw new Error('could not locate the STT card')
  const out = join(OUT, '01-provider-off-selected.png')
  await target.screenshot({ path: out })
  console.log('wrote', out)
  await context.close()
}

// 2 - the option list open, `Off` among its siblings. The control is the shared
//     `SimpleSelect` popover, so the list is in-page and the shot can see it.
{
  const { context, page, heading } = await openVoiceSettings()
  // The text-to-speech card above has its own Provider combobox; pick the one
  // showing the speech-to-text value.
  const trigger = page
    .getByRole('combobox', { name: 'Provider' })
    .filter({ hasText: 'Off (no speech recognition)' })
    .first()
  await trigger.waitFor({ timeout: 15_000 })
  await trigger.click()
  const offOption = page.getByRole('option', { name: /Off \(no speech recognition\)/ }).first()
  await offOption.waitFor({ timeout: 15_000 })
  await page.waitForTimeout(300)
  const out = join(OUT, '02-provider-options-with-off.png')
  const box = await card(heading).boundingBox()
  await page.screenshot({
    path: out,
    clip: { x: box.x, y: box.y, width: box.width, height: box.height + 8 },
  })
  console.log('wrote', out)
  await context.close()
}

await browser.close()
srv.close()

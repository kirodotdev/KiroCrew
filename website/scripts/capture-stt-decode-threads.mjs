/**
 * Screenshot harness for the decode-thread picker in Settings > Voice > Fine-tuning.
 *
 * Runs the REAL built SPA (website/dist) behind `serveDist` and answers every
 * /api/** call from fixtures through `stubDashboardApi`. No gateway, no recogniser.
 *
 * Frames are fixture states on a 128-core host:
 *   1. auto (the default)               -> the picker reads Auto (16)
 *   2. 64 stored                        -> what an operator on a large host picks
 *   3. KIROCREW_STT_THREADS set         -> the picker is disabled and the note says the env var wins
 *
 * Usage: node scripts/capture-stt-decode-threads.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/stt-decode-threads-shots'
mkdirSync(OUT, { recursive: true })

const MODELS = [
  { name: 'tiny', size_bytes: 77_691_713, present: false },
  { name: 'base', size_bytes: 147_951_465, present: false },
  { name: 'small', size_bytes: 487_601_967, present: false },
  { name: 'large-v3-turbo', size_bytes: 1_624_555_275, present: true },
]

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
  model: 'large-v3-turbo',
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
  providers: ['local', 'transcribe', 'off'],
  streaming_providers: ['local', 'transcribe'],
  language_codes: ['auto', 'en-US'],
  prereqs: [],
  threads: 0,
  thread_options: [0, 1, 2, 4, 8, 12, 16, 24, 32, 48, 64, 96, 128],
  threads_auto: 16,
  threads_env: null,
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()

async function openFineTuning(config = {}) {
  const context = await browser.newContext({ viewport: { width: 1180, height: 1800 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  const cfg = { ...STT_CONFIG, ...config }
  const st = {
    available: true,
    code: '',
    detail: '',
    provider: 'local',
    model: cfg.model,
    model_present: true,
    model_bytes: 1_624_555_275,
    engine_loaded: true,
    models: MODELS,
    download: { step: 'idle', model: '', downloaded_bytes: 0, total_bytes: 0, error: '' },
    backend: { name: 'cpu', accelerated: false, encoder_only: false, detail: 'AVX2', cpu_features: ['AVX2'], threads: cfg.threads || 16 },
  }
  const extra = async (path, route) => {
    if (path === '/api/config/stt') { await json(route, cfg); return true }
    if (path === '/api/stt/status') { await json(route, st); return true }
    if (path === '/api/voice/config') { await json(route, VOICE_CONFIG_FIXTURE); return true }
    return false
  }
  await stubDashboardApi(page, { extra })
  await page.addInitScript(() => localStorage.setItem('mc-lang', 'en'))
  await page.goto(`${base}/settings?tab=voice`, { waitUntil: 'domcontentloaded' })
  const toggle = page.getByRole('button', { name: /fine-tuning/i })
  await toggle.waitFor({ timeout: 15_000 })
  await toggle.click()
  const picker = page.getByRole('combobox', { name: /decode threads/i })
  await picker.waitFor({ timeout: 10_000 })
  await picker.scrollIntoViewIfNeeded()
  await page.waitForTimeout(600)
  return { context, page }
}

/** The settings page body, so the picker is seen inside its Fine-tuning group. */
const shoot = async (page, name) => {
  await page.locator('main').screenshot({ path: join(OUT, name) })
  console.log('wrote', join(OUT, name))
}

{
  const { context, page } = await openFineTuning()
  await shoot(page, '01-threads-auto.png')
  await context.close()
}
{
  const { context, page } = await openFineTuning({ threads: 64 })
  await shoot(page, '02-threads-64.png')
  await context.close()
}
{
  const { context, page } = await openFineTuning({ threads: 16, threads_env: '64' })
  await shoot(page, '03-threads-env-override.png')
  await context.close()
}
await browser.close()
srv.close()

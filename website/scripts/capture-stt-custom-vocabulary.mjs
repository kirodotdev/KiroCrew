/**
 * Screenshot harness for Settings > Voice with an Amazon Transcribe custom vocabulary.
 *
 * Runs the REAL built SPA (website/dist) behind the shared `serveDist` server and
 * answers every /api/** call from fixtures through `stubDashboardApi`. No gateway,
 * no AWS account, no recogniser.
 *
 * Seven frames, each a state the panel reaches from the gateway's answers:
 *   1. a ready vocabulary chosen for the dictation language: the picker and nothing
 *      else, because there is nothing to warn about
 *   2. the picker's option list open: None, then each READY vocabulary with its
 *      language. A PENDING one is in the fixture and is not offered.
 *   3. the stored vocabulary is not ready in the configured region: the warning
 *      that dictation will fail, with the stored name still selected
 *   4. the vocabulary's language differs from the dictation language, which
 *      Amazon Transcribe refuses: the warning that dictation will fail
 *   6. the listing is refused (502 `stt_vocabularies_access_denied`): the notice
 *      naming the IAM action the backend served, with the stored name still shown
 *   7. the listing fails for any other reason (502 `stt_vocabularies_list_failed`):
 *      the generic notice
 *   8. the listing is still in flight: the picker is disabled, still showing the
 *      stored name, with a loading line under it, so it never reads as "you have none"
 *
 * Frame 5, the dictation panel refusing a vocabulary, is not a Settings state and
 * is captured separately.
 *
 * Usage: node scripts/capture-stt-custom-vocabulary.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/stt-custom-vocabulary-shots'
mkdirSync(OUT, { recursive: true })

const REGION = 'us-east-1'
const ACCOUNT = '111122223333'

/** The text-to-speech card, stubbed completely so it cannot read as damage. */
const VOICE_CONFIG_FIXTURE = {
  enabled: false,
  autoSpeak: false,
  provider: 'piper',
  voice: 'Ruth',
  engine: 'generative',
  rate: '100%',
  aws_profile: '',
  region: REGION,
  piper_binary: '',
  piper_model: '~/piper/en_US-lessac-medium.onnx',
  piper_model_config: '',
  piper_length_scale: 1.0,
}

/** `GET /api/config/stt` for the `transcribe` provider; scenes override two fields. */
const sttConfig = (over = {}) => ({
  enabled: true,
  provider: 'transcribe',
  model: 'base',
  available: true,
  language_code: 'en-US',
  streaming: true,
  silence_ms: 700,
  partial_interval_ms: 400,
  idle_evict_secs: 600,
  endpointing: false,
  dictation_panel: true,
  polish: false,
  timeout_secs: 300,
  transcribe_region: REGION,
  transcribe_profile: '',
  transcribe_vocabulary: 'team-terms',
  providers: ['local', 'transcribe', 'off'],
  streaming_providers: ['local', 'apple', 'transcribe'],
  language_codes: ['en-US', 'en-GB', 'fr-FR', 'de-DE', 'es-US'],
  prereqs: [],
  transcribe_unsupported: false,
  bundled_interpreter: false,
  ffmpeg_missing: false,
  ...over,
})

/** `GET /api/stt/status` once Transcribe is importable: ready, no local model block. */
const STT_STATUS = {
  provider: 'transcribe',
  available: true,
  code: '',
  detail: '',
  model: 'base',
  model_present: true,
  model_bytes: 147_951_465,
  engine_loaded: false,
  models: [],
  download: { step: 'idle', model: '', downloaded_bytes: 0, total_bytes: 0, error: '' },
}

/** `GET /api/aws/consent?service=transcribe`: confirmed for the default chain. */
const CONSENT = {
  service: 'transcribe',
  serviceLabel: 'Amazon Transcribe',
  profile: '',
  credentialSource: 'default credential chain',
  region: REGION,
  account: ACCOUNT,
  arn: `arn:aws:iam::${ACCOUNT}:user/dev`,
  identityResolved: true,
  identityDetail: '',
  granted: true,
  reason: '',
  revokedOnAccountChange: false,
  grant: { account: ACCOUNT, granted_at: '2026-10-01T09:00:00+00:00' },
}

/** `GET /api/stt/vocabularies`, read from AWS: three ready vocabularies and one still building. */
const VOCABULARIES = {
  profile: '',
  region: REGION,
  listed: true,
  vocabularies: [
    { name: 'product-names', language_code: 'en-US', state: 'READY' },
    { name: 'release-train', language_code: 'en-US', state: 'PENDING' },
    { name: 'team-terms', language_code: 'en-US', state: 'READY' },
    { name: 'vocabulaire-equipe', language_code: 'fr-FR', state: 'READY' },
  ],
}

/**
 * The listing's two refusals, as `api_stt_vocabularies` answers them: a code and
 * never the service's text. Only the denial carries the IAM action to grant.
 */
const LISTING_ACCESS_DENIED = {
  error: 'not allowed to list custom vocabularies',
  code: 'stt_vocabularies_access_denied',
  permission: 'transcribe:ListVocabularies',
}
const LISTING_FAILED = { error: 'could not list custom vocabularies', code: 'stt_vocabularies_list_failed' }

/** The listing answered from the fixture; the default for every scene. */
const listingOk = route => json(route, VOCABULARIES)
/** The listing refused with `body` at 502. */
const listingRefused = body => route => json(route, body, 502)
/** The listing never answered, so the request stays in flight for the frame. */
const listingPending = () => {}

const { srv, base } = await serveDist()
const browser = await chromium.launch()

async function openVoiceSettings(config, listing = listingOk) {
  const context = await browser.newContext({
    viewport: { width: 1180, height: 2200 },
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()
  const extra = async (path, route) => {
    if (path === '/api/config/stt') return json(route, config), true
    if (path === '/api/stt/status') return json(route, STT_STATUS), true
    if (path === '/api/stt/vocabularies') return listing(route), true
    if (path === '/api/aws/consent') return json(route, CONSENT), true
    if (path === '/api/voice/config') return json(route, VOICE_CONFIG_FIXTURE), true
    return false
  }
  await stubDashboardApi(page, { extra })
  await page.addInitScript(() => localStorage.setItem('mc-lang', 'en'))
  await page.goto(`${base}/settings?tab=voice`, { waitUntil: 'domcontentloaded' })
  const picker = page.getByRole('combobox', { name: 'Custom vocabulary' })
  await picker.waitFor({ timeout: 15_000 })
  await picker.scrollIntoViewIfNeeded()
  await page.waitForTimeout(1200)
  return { context, page, picker }
}

/**
 * The Transcribe block, from the consent card down to the last line under the
 * picker, so a frame shows the vocabulary in the context it is read from.
 */
async function transcribeBlock(page) {
  const top = await page.getByTestId('aws-consent-transcribe').boundingBox()
  const field = page.locator('[data-setting-key="stt.transcribe_vocabulary"]')
  const fieldBox = await field.boundingBox()
  const notices = page.locator(
    '[data-testid="stt-vocabulary-unavailable"], [data-testid="stt-vocabulary-language-mismatch"], [data-testid="stt-vocabularies-error"], [data-testid="stt-vocabularies-loading"]',
  )
  // An open option list is a popover below the field, outside the field's own box.
  const below = [...(await notices.all()), ...(await page.getByRole('listbox').all())]
  let bottom = fieldBox.y + fieldBox.height
  for (const box of await Promise.all(below.map(w => w.boundingBox()))) {
    if (box) bottom = Math.max(bottom, box.y + box.height)
  }
  const pad = 12
  return {
    x: Math.min(top.x, fieldBox.x) - pad,
    y: top.y - pad,
    width: Math.max(top.width, fieldBox.width) + 2 * pad,
    height: bottom - top.y + 2 * pad,
  }
}

async function shoot(name, config, prepare, listing) {
  const { context, page, picker } = await openVoiceSettings(config, listing)
  if (prepare) await prepare(page, picker)
  const out = join(OUT, name)
  await page.screenshot({ path: out, clip: await transcribeBlock(page) })
  console.log('wrote', out)
  await context.close()
}

const noticeShown = testId => async page => {
  await page.getByTestId(testId).waitFor({ timeout: 15_000 })
}

// 1 - a ready vocabulary in the dictation language: no warning to show.
await shoot('01-vocabulary-selected.png', sttConfig())

// 2 - the option list open. `release-train` is PENDING and must not be offered.
await shoot('02-vocabulary-options.png', sttConfig(), async (page, picker) => {
  await picker.click()
  await page.getByRole('option', { name: 'team-terms (en-US)' }).waitFor({ timeout: 15_000 })
  if (await page.getByRole('option', { name: /release-train/ }).count()) {
    throw new Error('a PENDING vocabulary was offered')
  }
  await page.waitForTimeout(300)
})

// 3 - the stored vocabulary is gone from the region: dictation would fail.
await shoot(
  '03-vocabulary-not-ready.png',
  sttConfig({ transcribe_vocabulary: 'deleted-terms' }),
  noticeShown('stt-vocabulary-unavailable'),
)

// 4 - a French vocabulary under English dictation: refused by AWS, dictation would fail.
await shoot(
  '04-vocabulary-language-mismatch.png',
  sttConfig({ transcribe_vocabulary: 'vocabulaire-equipe' }),
  noticeShown('stt-vocabulary-language-mismatch'),
)

// 6 - the credentials may not list: the notice names the IAM action to grant.
await shoot(
  '06-vocabularies-access-denied.png',
  sttConfig(),
  async page => {
    const notice = page.getByTestId('stt-vocabularies-error')
    await notice.waitFor({ timeout: 15_000 })
    if (!(await notice.innerText()).includes(LISTING_ACCESS_DENIED.permission)) {
      throw new Error('the access-denied notice does not name the IAM action')
    }
  },
  listingRefused(LISTING_ACCESS_DENIED),
)

// 7 - the listing failed for any other reason: the generic notice.
await shoot(
  '07-vocabularies-list-failed.png',
  sttConfig(),
  noticeShown('stt-vocabularies-error'),
  listingRefused(LISTING_FAILED),
)

// 8 - the listing still in flight: the picker is disabled and says it is loading.
await shoot(
  '08-vocabulary-loading.png',
  sttConfig(),
  async (page, picker) => {
    await page.getByTestId('stt-vocabularies-loading').waitFor({ timeout: 15_000 })
    if (!(await picker.isDisabled())) throw new Error('the picker is enabled while the list is loading')
    if (!(await picker.innerText()).includes('team-terms')) {
      throw new Error('the stored name is not shown while the list is loading')
    }
  },
  listingPending,
)

await browser.close()
srv.close()

/**
 * Screenshot harness for the decoder (ffmpeg) block on Settings → Voice.
 *
 * Runs the REAL built SPA (website/dist) behind the in-process static server,
 * answering every /api/** call from fixtures via Playwright route interception —
 * no gateway, no token. The client code under test is unmodified, so
 * Settings → Voice → Speech-to-Text renders exactly as in production.
 *
 * ## What the shots prove
 *
 * 1. `missing`: a source install with no decoder, on a platform the pinned
 *    upstream executable covers — the fetch sentence and the Download decoder
 *    button, where the panel previously printed a shell command.
 * 2. `downloading`: the decoder's OWN progress bar and its own caption, which
 *    names the decoder rather than the speech model whose bar sits beside it.
 * 3. `failed`: the backend's failure detail, plus the hand-off button that
 *    pre-fills a chat composer with the repair prompt.
 * 4. `unsupported`: a platform with no pinned executable (32-bit ARM Linux) still
 *    gets the manual system-decoder command and no button, because a fetch there
 *    cannot succeed.
 *
 * Every scene declares what must AND must not be on screen, and one function
 * loads, asserts and shoots — so a frame cannot be produced without its
 * assertions having run, and an adjacent state leaking in fails the harness
 * loudly instead of yielding a misleading frame.
 *
 * Usage: node scripts/capture-stt-decoder.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { serveDist } from './lib/serve-dist.mjs'
import { json, handleBootRoute } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/stt-ffmpeg-decoder'
const PROJECT = '/home/user/workspace/KiroCrew'

/** Headline of the decoder card, and the element the frames are cropped to. */
const CARD = 'ffmpeg is missing'
/** The manual system-decoder command, offered only where no pin exists. */
const MANUAL = 'sudo apt-get install -y ffmpeg'
const FETCH_OFFER = 'can fetch a digest-verified copy for this machine'
const BUTTON = 'Download decoder'

mkdirSync(OUT, { recursive: true })

/** What GET /api/config/stt serves; only the decoder-relevant fields vary. */
const sttConfig = {
  enabled: true,
  provider: 'whisper',
  model: 'turbo',
  mlx_model: '',
  available: true,
  streaming: false,
  endpointing: false,
  dictation_panel: false,
  transcribe_region: 'us-east-1',
  transcribe_profile: '',
  language_code: 'en-US',
  models: { turbo: '~1.6 GB' },
  mlx_models: {},
  providers: ['whisper', 'mlx', 'transcribe', 'faster'],
  streaming_providers: ['transcribe', 'apple'],
  language_codes: ['en-US'],
  install_step: 'idle',
  install_detail: '',
  install_error: '',
  // The command a Debian-family host can actually run. Rendered only when no
  // pinned executable exists for the platform.
  prereqs: [MANUAL],
  transcribe_unsupported: false,
  bundled_interpreter: false,
  ffmpeg_missing: true,
}

/** An idle `download` block, i.e. no fetch has been started. */
const idleFetch = {
  stage: 'idle',
  artifact: '',
  downloaded_bytes: 0,
  total_bytes: 0,
  error_code: '',
  error_detail: '',
}

/** The `ffmpeg` object on GET /api/stt/status. Mutated per scene. */
const ffmpeg = {
  present: false,
  source: null,
  auto_fetch: 'available',
  os: 'Linux',
  arch: 'x86_64',
  download: { ...idleFetch },
}

const scene = { theme: 'dark' }

/**
 * The four states, in the order a reader meets them. `must` / `absent` are what
 * makes each one itself: `absent` matters as much as `must`, because the fetch
 * offer and the manual command are mutually exclusive and a frame showing both
 * would document a bug as if it were the design.
 */
const SCENES = [
  {
    name: '01-decoder-missing-dark',
    theme: 'dark',
    ffmpeg: { auto_fetch: 'available', arch: 'x86_64', download: { ...idleFetch } },
    marker: BUTTON,
    must: [CARD, FETCH_OFFER, BUTTON],
    absent: [MANUAL],
  },
  {
    name: '02-decoder-downloading-dark',
    theme: 'dark',
    ffmpeg: {
      auto_fetch: 'available',
      arch: 'x86_64',
      download: {
        ...idleFetch,
        stage: 'downloading',
        artifact: 'ffmpeg-linux-x86_64-v7.0.2',
        downloaded_bytes: 33_554_432,
        total_bytes: 79_826_272,
      },
    },
    marker: 'Downloading the audio decoder',
    must: [CARD, 'Downloading the audio decoder'],
    absent: [BUTTON, MANUAL],
  },
  {
    name: '03-decoder-failed-dark',
    theme: 'dark',
    ffmpeg: {
      auto_fetch: 'available',
      arch: 'x86_64',
      download: {
        stage: 'failed',
        artifact: 'ffmpeg-linux-x86_64-v7.0.2',
        downloaded_bytes: 12_058_624,
        total_bytes: 79_826_272,
        error_code: 'decoder_digest_mismatch',
        error_detail: 'The downloaded wheel did not match its pinned SHA-256 digest.',
      },
    },
    marker: 'The decoder download failed',
    must: [CARD, 'The decoder download failed', 'pinned SHA-256 digest', 'fix it'],
    absent: [MANUAL],
  },
  {
    // 32-bit ARM Linux: nothing upstream is pinned for it, so a fetch cannot
    // succeed and the manual command is the only honest remedy.
    name: '04-decoder-unsupported-platform-light',
    theme: 'light',
    ffmpeg: {
      auto_fetch: 'unsupported',
      arch: 'armv7l',
      download: { ...idleFetch, stage: 'unsupported' },
    },
    marker: MANUAL,
    must: [CARD, MANUAL],
    absent: [BUTTON, FETCH_OFFER],
  },
]

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1760, height: 1400 },
    // Settings rows are 12–13px type; a 1x shot renders soft on GitHub.
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()

  await page.routeWebSocket(/\/api\/ws/, () => {})

  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/config/stt') return json(route, sttConfig)
    if (path === '/api/stt/status') {
      return json(route, {
        provider: sttConfig.provider,
        available: true,
        code: '',
        detail: '',
        model: sttConfig.model,
        model_present: true,
        model_bytes: 1_610_612_736,
        models: [{ name: 'turbo', size_bytes: 1_610_612_736, present: true }],
        engine_loaded: false,
        download: { step: 'idle', model: '', done: 0, total: 0, error: '' },
        ffmpeg,
      })
    }
    return handleBootRoute(route, path, { project: PROJECT, theme: scene.theme })
  })

  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))
  page.on('console', msg => { if (msg.type() === 'error') console.log('CONSOLE:', msg.text().slice(0, 300)) })

  /**
   * Load the voice tab in this scene's theme, prove the state on screen is the
   * one asked for, and shoot the decoder card.
   *
   * The card is addressed through its headline because it carries no test hook,
   * and it is scrolled in before measuring: it sits well down a long settings
   * page, and an element that has never been laid out yields nothing to shoot.
   */
  async function shoot({ name, theme, marker, must, absent }) {
    scene.theme = theme
    await page.addInitScript(s => {
      localStorage.clear()
      localStorage.setItem('mc-theme', s.theme)
      localStorage.setItem('mc-onboarded', '1')
    }, scene)
    await page.goto(`${base}/settings?tab=voice`, { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2600)

    for (const [texts, wanted] of [[must, true], [absent, false]]) {
      for (const text of texts) {
        const onScreen = (await page.getByText(text, { exact: false }).count()) > 0
        if (onScreen !== wanted) {
          const how = onScreen ? 'rendered but must not be' : 'not rendered'
          throw new Error(`ASSERT FAILED in ${name}: "${text}" ${how}`)
        }
      }
    }

    const card = page.getByText(CARD, { exact: false }).first()
    await card.scrollIntoViewIfNeeded()
    await page.waitForTimeout(400)
    if (!(await page.getByText(marker, { exact: false }).first().boundingBox())) {
      throw new Error(`ASSERT FAILED in ${name}: marker "${marker}" has no box`)
    }
    await card.locator('xpath=..').screenshot({ path: `${OUT}/${name}.png` })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  for (const s of SCENES) {
    Object.assign(ffmpeg, s.ffmpeg)
    await shoot(s)
  }

  // Evidence integrity: two scenes producing the same bytes means the crop missed
  // the distinguishing element and the "proof" proves nothing.
  const seen = new Map()
  for (const { name } of SCENES) {
    const sum = createHash('sha256').update(readFileSync(`${OUT}/${name}.png`)).digest('hex')
    if (seen.has(sum)) throw new Error(`ASSERT FAILED: ${name}.png is byte-identical to ${seen.get(sum)}.png`)
    seen.set(sum, name)
  }
  console.log('frames verified distinct')

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })

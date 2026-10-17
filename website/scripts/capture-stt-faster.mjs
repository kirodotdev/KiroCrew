/**
 * Screenshot harness for the `faster` (faster-whisper) STT provider UI (#2192).
 *
 * Runs the REAL built SPA (website/dist) behind the in-process static server,
 * answering every /api/** call from fixtures via Playwright route interception —
 * no gateway, no token. The client code under test is unmodified, so
 * Settings → Voice → Speech-to-Text renders exactly as in production.
 *
 * ## What the shots prove
 *
 * 1. `not-installed`: choosing the new `faster` provider shows its localized
 *    dropdown label, the MODEL picker (previously gated on `whisper` alone —
 *    the PR's WHISPER_MODEL_PROVIDERS fix), the "Install faster-whisper"
 *    button and its "no separate ffmpeg install" blurb.
 * 2. `installing`: the progress bar and the `step_installing_faster` label the
 *    status parser drives off the script's "Installing faster-whisper..." line.
 * 3. `ready`: the available state once the library imports.
 *
 * Each scene ASSERTS the strings programmatically before shooting, so a broken
 * render fails the harness loudly instead of producing a misleading frame.
 *
 * Usage: node scripts/capture-stt-faster.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, handleBootRoute } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/stt-faster-whisper'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

/** Mutated per scene: what /api/config/stt returns. */
const stt = {
  enabled: true,
  provider: 'faster',
  model: 'turbo',
  mlx_model: '',
  available: false,
  streaming: false,
  endpointing: false,
  dictation_panel: false,
  transcribe_region: 'us-east-1',
  transcribe_profile: '',
  language_code: 'en-US',
  models: {
    tiny: '~75 MB', base: '~145 MB', small: '~484 MB',
    medium: '~1.5 GB', 'large-v3': '~3.1 GB', turbo: '~1.6 GB',
  },
  mlx_models: {},
  providers: ['whisper', 'mlx', 'transcribe', 'faster'],
  streaming_providers: ['transcribe', 'apple'],
  language_codes: ['en-US', 'zh-CN', 'de-DE'],
  install_step: 'idle',
  install_detail: '',
  install_error: '',
  prereqs: [],
  transcribe_unsupported: false,
  bundled_interpreter: false,
  ffmpeg_missing: false,
}

const scene = { theme: 'dark' }

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
    // Scene-specific routes first; everything else is the shared boot fixture.
    if (path === '/api/config/stt') return json(route, stt)
    if (path === '/api/config/kirocrew') {
      return json(route, {
        agent: { model: 'claude-opus-4.8', reasoning_effort: 'high' },
        session: { autocompact_pct: 90 },
        dashboard: { user_role: '', user_technical_level: '' },
      })
    }
    if (path === '/api/chat/slots') return json(route, [])
    return handleBootRoute(route, path, { project: PROJECT, theme: scene.theme })
  })

  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))
  page.on('console', msg => { if (msg.type() === 'error') console.log('CONSOLE:', msg.text().slice(0, 300)) })

  async function load(theme = 'dark') {
    scene.theme = theme
    await page.addInitScript(s => {
      localStorage.clear()
      localStorage.setItem('mc-theme', s.theme)
      localStorage.setItem('mc-onboarded', '1')
    }, scene)
    await page.goto(base + '/settings?tab=voice', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2600)
  }

  /** Must-see strings per scene; a miss fails the harness before any shot. */
  async function mustSee(...texts) {
    for (const t of texts) {
      const n = await page.getByText(t, { exact: false }).count()
      if (!n) throw new Error(`ASSERT FAILED: "${t}" not rendered`)
    }
  }

  /** Crop from the Provider select down through the scene's distinguishing
   *  element (install button / progress label / ready row). Anchoring only on
   *  the provider label once produced two byte-identical "different" scenes —
   *  the install block sat below the fixed-height window — so the crop is now
   *  the UNION of the anchor and the element that makes the scene the scene. */
  async function sttCard(name, sceneText) {
    const anchor = page.getByText('faster-whisper (local — no separate ffmpeg install)').first()
    const marker = page.getByText(sceneText, { exact: false }).first()
    const a = await anchor.boundingBox()
    const m = await marker.boundingBox()
    if (!a) throw new Error('ASSERT FAILED: provider label has no box')
    if (!m) throw new Error(`ASSERT FAILED: scene marker "${sceneText}" has no box`)
    const pad = 24
    const x0 = Math.max(0, Math.min(a.x, m.x) - 340)
    const y0 = Math.max(0, Math.min(a.y, m.y) - 150)
    const y1 = Math.max(a.y + a.height, m.y + m.height) + 120
    const clip = { x: x0, width: Math.min(1180, 1760 - x0), y: y0, height: Math.min(y1 - y0 + pad, 1400 - y0) }
    await page.screenshot({ path: `${OUT}/${name}.png`, clip })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  // Scene 1 — provider selected, library not installed: dropdown label, model
  // picker (WHISPER_MODEL_PROVIDERS gating), install button + blurb.
  stt.provider = 'faster'
  stt.available = false
  stt.install_step = 'idle'
  await load('dark')
  await mustSee(
    'faster-whisper (local — no separate ffmpeg install)',
    'Install faster-whisper',
    'Installs faster-whisper via pip',
    'turbo (~1.6 GB)',
  )
  await sttCard('01-faster-not-installed-dark', 'Installs faster-whisper via pip')

  // Scene 2 — install in flight: progress bar + step label from the exact
  // "Installing faster-whisper..." line the status parser matches.
  stt.install_step = 'installing_faster'
  stt.install_detail = 'Installing faster-whisper...'
  await load('dark')
  await mustSee('Installing faster-whisper…')
  await sttCard('02-faster-installing-dark', 'Installing faster-whisper…')

  // Scene 3 — ready: the library imports, model picker still visible.
  stt.install_step = 'done'
  stt.install_detail = ''
  stt.available = true
  await load('light')
  await mustSee('faster-whisper (local — no separate ffmpeg install)')
  await sttCard('03-faster-ready-light', 'faster-whisper (local — no separate ffmpeg install)')

  // Evidence integrity: two scenes producing the same bytes means the crop
  // missed the distinguishing element and the "proof" proves nothing.
  const { readFileSync } = await import('node:fs')
  const { createHash } = await import('node:crypto')
  const digest = f => createHash('sha256').update(readFileSync(`${OUT}/${f}.png`)).digest('hex')
  const frames = ['01-faster-not-installed-dark', '02-faster-installing-dark', '03-faster-ready-light']
  const seen = new Map()
  for (const f of frames) {
    const d = digest(f)
    if (seen.has(d)) throw new Error(`ASSERT FAILED: ${f}.png is byte-identical to ${seen.get(d)}.png`)
    seen.set(d, f)
  }
  console.log('frames verified distinct')

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })

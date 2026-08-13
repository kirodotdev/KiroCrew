/**
 * Recording harness for the voice dictation panel.
 *
 * Runs the REAL built SPA (website/dist) against a static file server with every
 * /api/** call answered from fixtures — no gateway, no token, no agent.
 *
 * ## What is real and what is not
 *
 * Only the capture DEVICE is substituted. `navigator.mediaDevices.getUserMedia`
 * is replaced with one that returns a MediaStream synthesized by Web Audio,
 * shaped like speech (syllable bursts, phrase-level amplitude drift, a sweeping
 * carrier so the spectral centroid moves, and real pauses between sentences).
 * Everything downstream is the unmodified production path: the same
 * `useVoiceInput` → `createLevelMeter` → `AnalyserNode` → 50ms-attack /
 * 250ms-release envelope → WebGL2 shader chain that runs with a live mic.
 *
 * The substitution is necessary, not a shortcut: headless Chromium has no audio
 * input device, so real `getUserMedia({audio:true})` rejects with
 * NotSupportedError even with `--use-fake-device-for-media-stream`. A synthetic
 * MediaStream keeps the harness headless and CI-runnable.
 *
 * Records video because the change is animated — a still frame cannot show the
 * strands tracking input level, which is the whole point of the surface.
 *
 * Usage: node scripts/capture-dictation-panel.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/dictation-panel'
const SLOT = 'chat-dictation'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Dictation',
  running: false,
  last_message: '',
  messages: 0,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const detail = { running: false, has_more: false, total: 0, queue: [], project: PROJECT, messages: [] }

/**
 * Installed before any app code runs. Returns a Web Audio MediaStream whose
 * envelope is shaped like speech, and stubs MediaRecorder — the batch capture
 * path constructs one over the stream, and Chromium refuses to encode a
 * synthetic track. The recorder's output is never used here (the run is stopped
 * before transcription), so a no-op recorder keeps the real code path intact
 * without needing an encoder.
 */
function installSyntheticMic() {
  const AC = window.AudioContext || window.webkitAudioContext
  const ctx = new AC()
  const dest = ctx.createMediaStreamDestination()

  // Carrier + harmonics, so the spectral centroid has something to move over.
  const gain = ctx.createGain()
  gain.gain.value = 0
  for (const [mult, level] of [[1, 0.6], [2, 0.25], [3.5, 0.15]]) {
    const osc = ctx.createOscillator()
    osc.type = 'sawtooth'
    osc.frequency.value = 150 * mult
    const g = ctx.createGain()
    g.gain.value = level
    osc.connect(g).connect(gain)
    osc.start()
    // Sweep the fundamental so brightness varies across the phrase.
    if (mult === 1) {
      const lfo = ctx.createOscillator()
      lfo.frequency.value = 0.37
      const lfoGain = ctx.createGain()
      lfoGain.gain.value = 90
      lfo.connect(lfoGain).connect(osc.frequency)
      lfo.start()
    }
  }
  gain.connect(dest)

  // Syllable envelope, scheduled ahead in 6s blocks: ~2.6Hz bursts for 5s of
  // "sentence", then a 2s pause. A constant tone would pin the shader to a flat
  // line and prove nothing about the envelope.
  const SENTENCE = 5, PAUSE = 2
  let at = ctx.currentTime + 0.1
  const schedule = () => {
    const blockEnd = at + SENTENCE
    let t = at
    while (t < blockEnd) {
      const peak = 0.35 + 0.3 * Math.abs(Math.sin(t * 0.9))
      gain.gain.linearRampToValueAtTime(peak, t + 0.05)   // sharp onset
      gain.gain.linearRampToValueAtTime(0.02, t + 0.33)   // decaying tail
      t += 0.385
    }
    gain.gain.setValueAtTime(0, blockEnd)                 // breath
    at = blockEnd + PAUSE
    setTimeout(schedule, (SENTENCE + PAUSE) * 1000 - 200)
  }
  schedule()

  const stream = dest.stream
  // Real code reads the device label for the status row.
  try {
    Object.defineProperty(stream.getAudioTracks()[0], 'label', {
      value: 'Synthetic Test Microphone',
    })
  } catch { /* label stays empty; the panel omits the row */ }

  navigator.mediaDevices.getUserMedia = async () => stream
  navigator.mediaDevices.enumerateDevices = async () => [
    { kind: 'audioinput', deviceId: 'synthetic', label: 'Synthetic Test Microphone', groupId: 'g' },
  ]

  class NoopRecorder {
    static isTypeSupported() { return true }
    constructor(s) { this.stream = s; this.state = 'inactive'; this.ondataavailable = null; this.onstop = null }
    start() { this.state = 'recording' }
    stop() { this.state = 'inactive'; this.onstop?.() }
  }
  window.MediaRecorder = NoopRecorder
}

const { srv, base } = await serveDist()

const browser = await chromium.launch({
  args: ['--use-gl=angle', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'],
})

const context = await browser.newContext({
  viewport: { width: 1280, height: 820 },
  deviceScaleFactor: 2,
  permissions: ['microphone'],
  recordVideo: { dir: OUT, size: { width: 1280, height: 820 } },
})

const page = await context.newPage()
const errors = []
page.on('pageerror', e => errors.push(`PAGEERROR: ${e.message}`))
page.on('console', m => { if (m.type() === 'error') errors.push(m.text().slice(0, 200)) })

await page.routeWebSocket(/\/api\/ws/, () => {})

const fixedApi = makeFixedApi(PROJECT)
await page.route('**/api/**', route => {
  const path = new URL(route.request().url()).pathname

  // The setting under test. `dictation_panel: true` is the backend default;
  // `streaming: false` keeps this on the batch path so no STT websocket is needed.
  if (path === '/api/config/stt') {
    return json(route, {
      enabled: true, dictation_panel: true, streaming: false,
      provider: 'whisper', available: true,
      model: 'turbo', models: { turbo: '809M' }, language_code: 'en-US',
      install_step: '', install_detail: '', install_error: '', prereqs: [],
    })
  }
  if (path === '/api/chat/slots') return json(route, slots)
  if (path.startsWith('/api/chat/slots/')) return json(route, detail)
  if (path.startsWith('/api/stt/transcribe')) return json(route, { text: '' })

  return handleBootRoute(route, path, { project: PROJECT, fixedApi })
})

await page.addInitScript(installSyntheticMic)
await page.addInitScript(slot => {
  localStorage.clear()
  localStorage.setItem('mc-theme', 'dark')
  localStorage.setItem('mc-onboarded', '1')
  localStorage.setItem('mc-active-slot-chat', slot)
}, SLOT)

await page.goto(`${base}/`, { waitUntil: 'domcontentloaded' })

// Text in the composer so the transcript area has content: with streaming off
// there is no partial hypothesis, so this renders as fully committed text.
const composer = page.getByLabel('Message input')
await composer.waitFor({ timeout: 20000 })
await composer.fill('summarize what changed in the startup fix')

await page.getByRole('button', { name: /voice input/i }).click()
await page.getByTestId('voice-dictation-panel').waitFor({ timeout: 15000 })

// Cover loud, quiet and loud again (5s sentence + 2s pause + 5s sentence).
await page.waitForTimeout(4000)
await page.screenshot({ path: `${OUT}/dictation-panel-dark.png` })
await page.waitForTimeout(5000)

await page.evaluate(() => { document.documentElement.dataset.theme = 'light' })
await page.waitForTimeout(2500)
await page.screenshot({ path: `${OUT}/dictation-panel-light.png` })
await page.evaluate(() => { document.documentElement.dataset.theme = 'dark' })
await page.waitForTimeout(1500)

// Stop via Escape — also exercises the affordance the panel advertises.
await page.keyboard.press('Escape')
await page.getByTestId('voice-dictation-panel').waitFor({ state: 'detached', timeout: 10000 })
await page.waitForTimeout(800)

const video = page.video()
await context.close()
if (video) await video.saveAs(`${OUT}/dictation-panel.webm`)
await browser.close()
srv.close()

console.log(JSON.stringify({ out: OUT, errors }, null, 2))
if (errors.length) process.exitCode = 1

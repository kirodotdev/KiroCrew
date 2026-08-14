/**
 * Screenshot harness for the data-driven input-source checkmark.
 *
 * Runs the REAL built SPA (website/dist) against fixture APIs — no gateway, no
 * token. Only the capture DEVICE is substituted (headless Chromium has no audio
 * input, so real getUserMedia({audio:true}) rejects); everything downstream is
 * the unmodified production path.
 *
 * ## The shape being proven
 *
 * The bug: the picker's checkmark keyed on `getPreferredMicId()` — the user's
 * INTENT — while the audio graph could be on a different device entirely,
 * because the old `ideal` constraint degrades silently. Picking AirPods moved
 * the checkmark and nothing else.
 *
 * So the harness sets up exactly that divergence:
 *   - saved preference        = 'airpods'      (what the user picked)
 *   - live track's deviceId   = 'builtin'      (what is actually capturing)
 *
 * Post-fix the mark MUST sit on the built-in row, not on AirPods. The harness
 * asserts that programmatically (not just visually) so it fails loudly if the
 * render regresses, then shoots the frame as evidence.
 *
 * Batch path (`streaming: false`) on purpose: it exercises the same
 * acquireMicStream + activeDeviceId reporting while needing no STT WebSocket.
 *
 * Usage: node scripts/capture-mic-source-menu.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/mic-device-switch'
const SLOT = 'chat-micsel'
const PROJECT = '/home/user/workspace/KiroCrew'

/** The device the SAVED PREFERENCE points at — deliberately not the live one. */
const PREFERRED_ID = 'airpods'
/** The device actually capturing, as the live track reports it. */
const LIVE_ID = 'builtin'
const LIVE_LABEL = 'MacBook Pro Microphone'

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Mic source',
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
 * Synthetic mic whose track REPORTS A DEVICE IDENTITY. The identity is the whole
 * point here: `activeDeviceId()` reads `track.getSettings().deviceId`, and that
 * is the value the picker must now render from.
 */
function installIdentifiedMic({ liveId, liveLabel, preferredId }) {
  const AC = window.AudioContext || window.webkitAudioContext
  const ctx = new AC()
  const dest = ctx.createMediaStreamDestination()
  const osc = ctx.createOscillator()
  const gain = ctx.createGain()
  gain.gain.value = 0.18
  osc.type = 'sawtooth'
  osc.frequency.value = 180
  osc.connect(gain).connect(dest)
  osc.start()

  const stream = dest.stream
  const track = stream.getAudioTracks()[0]
  try {
    Object.defineProperty(track, 'label', { value: liveLabel })
  } catch { /* label stays empty; the picker falls back to the id */ }
  // The substitution that matters: report the LIVE device, which is NOT the
  // saved preference. Pre-fix this value was ignored by the menu entirely.
  const realGetSettings = track.getSettings?.bind(track)
  track.getSettings = () => ({ ...(realGetSettings?.() || {}), deviceId: liveId })

  // `exact` on a device that does not exist in a headless browser would reject,
  // so answer any constraint with the one synthetic stream. The constraint SHAPE
  // is covered by unit tests (mic.acquire.test.ts); this harness is about render.
  navigator.mediaDevices.getUserMedia = async () => stream
  navigator.mediaDevices.enumerateDevices = async () => [
    { kind: 'audioinput', deviceId: liveId, label: liveLabel, groupId: 'g1' },
    { kind: 'audioinput', deviceId: preferredId, label: 'AirPods Pro', groupId: 'g2' },
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
})
const page = await context.newPage()
const errors = []
page.on('pageerror', e => errors.push(`PAGEERROR: ${e.message}`))
page.on('console', m => { if (m.type() === 'error') errors.push(m.text().slice(0, 200)) })

await page.routeWebSocket(/\/api\/ws/, () => {})

const fixedApi = makeFixedApi(PROJECT)
await page.route('**/api/**', route => {
  const path = new URL(route.request().url()).pathname
  if (path === '/api/config/stt') {
    return json(route, {
      enabled: true, dictation_panel: true, streaming: false,
      provider: 'whisper', available: true, docker_mode: false,
      model: 'turbo', models: { turbo: '809M' }, language_code: 'en-US',
      install_step: '', install_detail: '', install_error: '', prereqs: [],
    })
  }
  if (path === '/api/chat/slots') return json(route, slots)
  if (path.startsWith('/api/chat/slots/')) return json(route, detail)
  if (path.startsWith('/api/stt/transcribe')) return json(route, { text: '' })
  return handleBootRoute(route, path, { project: PROJECT, fixedApi })
})

await page.addInitScript(installIdentifiedMic, {
  liveId: LIVE_ID, liveLabel: LIVE_LABEL, preferredId: PREFERRED_ID,
})
await page.addInitScript(([slot, preferred]) => {
  localStorage.clear()
  localStorage.setItem('mc-theme', 'dark')
  localStorage.setItem('mc-onboarded', '1')
  localStorage.setItem('mc-active-slot-chat', slot)
  // The user's pick — which the audio graph will NOT honor in this harness.
  localStorage.setItem('mc-mic-device-id', preferred)
}, [SLOT, PREFERRED_ID])

await page.goto(`${base}/`, { waitUntil: 'domcontentloaded' })

const composer = page.getByLabel('Message input')
await composer.waitFor({ timeout: 20000 })

await page.getByRole('button', { name: /voice input/i }).click()
await page.getByTestId('voice-dictation-panel').waitFor({ timeout: 15000 })
await page.waitForTimeout(900)

// Open the picker from inside the dictation panel.
await page.getByRole('button', { name: /change input source/i }).click()
await page.getByRole('menu').waitFor({ timeout: 10000 })
await page.waitForTimeout(400)

// Assert the invariant, don't just photograph it: the checkmark belongs to the
// LIVE device row. A regression here must fail the harness, not produce a
// screenshot nobody re-reads.
const marks = await page.evaluate(() => {
  const rows = Array.from(document.querySelectorAll('[role="menuitemradio"]'))
  return rows.map(r => ({
    text: (r.textContent || '').trim(),
    checked: !!r.querySelector('svg'),
    ariaChecked: r.getAttribute('aria-checked'),
  }))
})
const live = marks.find(m => m.text.includes('MacBook Pro Microphone'))
const pref = marks.find(m => m.text.includes('AirPods Pro'))
if (!live?.checked) errors.push(`ASSERT: live device row is not checked: ${JSON.stringify(marks)}`)
if (pref?.checked) errors.push(`ASSERT: preference row is checked (the pre-fix lie): ${JSON.stringify(marks)}`)
if (live?.ariaChecked !== 'true') errors.push(`ASSERT: live row is not aria-checked: ${JSON.stringify(marks)}`)

await page.screenshot({ path: `${OUT}/mic-source-menu-live-device-dark.png` })

await page.evaluate(() => { document.documentElement.dataset.theme = 'light' })
await page.waitForTimeout(500)
await page.screenshot({ path: `${OUT}/mic-source-menu-live-device-light.png` })

await context.close()
await browser.close()
srv.close()

console.log(JSON.stringify({ out: OUT, marks, errors }, null, 2))
if (errors.length) process.exitCode = 1

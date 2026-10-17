/**
 * Screenshot harness for the kept recording after a failed batch transcription.
 *
 * Runs the REAL built SPA (website/dist) against a static file server with every
 * /api/** call answered from fixtures — no gateway, no token, no agent.
 *
 * Only the capture DEVICE is substituted: headless Chromium has no audio input,
 * so `getUserMedia` returns a silent Web Audio stream and `MediaRecorder` is a
 * stub that hands over one chunk on stop. Everything from `mr.onstop` on is the
 * production path: the POST to `/api/stt/transcribe`, the inbox settling the
 * failure with its audio, and the voice strip rendering the actions.
 *
 * Captures, in order:
 *   1. a 500 answer — Retry, plus one menu holding Save recording and Discard
 *      (dark and light, and the menu open);
 *   2. a 413 answer — Save recording and Discard only;
 *   3. Retry pressed after a 500, answered with text — the transcript lands in
 *      the composer and the strip is gone.
 *
 * Usage: node scripts/capture-voice-kept-recording.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/voice-kept-recording'
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

/** Installed before any app code runs: a silent stream and a recorder stub that yields one chunk. */
function installSyntheticMic() {
  const AC = window.AudioContext || window.webkitAudioContext
  const ctx = new AC()
  const dest = ctx.createMediaStreamDestination()
  const stream = dest.stream
  try {
    Object.defineProperty(stream.getAudioTracks()[0], 'label', { value: 'Synthetic Test Microphone' })
  } catch { /* label stays empty */ }
  navigator.mediaDevices.getUserMedia = async () => stream
  navigator.mediaDevices.enumerateDevices = async () => [
    { kind: 'audioinput', deviceId: 'synthetic', label: 'Synthetic Test Microphone', groupId: 'g' },
  ]
  class ChunkRecorder {
    static isTypeSupported() { return true }
    constructor(s) { this.stream = s; this.state = 'inactive'; this.ondataavailable = null; this.onstop = null }
    start() { this.state = 'recording' }
    stop() {
      this.state = 'inactive'
      this.ondataavailable?.({ data: new Blob(['x'.repeat(4096)], { type: 'audio/webm' }) })
      this.onstop?.()
    }
  }
  window.MediaRecorder = ChunkRecorder
}

// Scripted /api/stt/transcribe answers, consumed one per request.
const sttAnswers = []

const { srv, base } = await serveDist()
const browser = await chromium.launch({ args: ['--use-gl=angle', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'] })
const context = await browser.newContext({ viewport: { width: 1280, height: 820 }, deviceScaleFactor: 2, permissions: ['microphone'] })
const page = await context.newPage()
const errors = []
page.on('pageerror', e => errors.push(`PAGEERROR: ${e.message}`))
await page.routeWebSocket(/\/api\/ws/, () => {})

const fixedApi = makeFixedApi(PROJECT)
await page.route('**/api/**', route => {
  const path = new URL(route.request().url()).pathname
  if (path === '/api/config/stt') {
    return json(route, {
      enabled: true, dictation_panel: false, streaming: false,
      provider: 'whisper', available: true,
      model: 'turbo', models: { turbo: '809M' }, language_code: 'en-US',
      install_step: '', install_detail: '', install_error: '', prereqs: [],
    })
  }
  if (path === '/api/chat/slots') return json(route, slots)
  if (path.startsWith('/api/chat/slots/')) return json(route, detail)
  if (path.startsWith('/api/stt/transcribe')) {
    const [status, body] = sttAnswers.shift() ?? [200, { text: '' }]
    return json(route, body, status)
  }
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
const composer = page.getByLabel('Message input')
await composer.waitFor({ timeout: 20000 })

const mic = page.getByRole('button', { name: /voice input/i })
const stopMic = page.getByRole('button', { name: /stop recording/i })

/** Record, stop, and wait for the strip to show the failure. */
async function failOnce(status, body) {
  sttAnswers.push([status, body])
  await mic.click()
  await stopMic.waitFor({ timeout: 10000 })
  await page.waitForTimeout(700)
  await stopMic.click()
  await page.getByTestId('voice-status-error').waitFor({ timeout: 10000 })
  await page.waitForTimeout(300)
}

/** The strip plus the composer under it, so the actions are seen in place. */
async function shotComposer(name) {
  const strip = await page.getByTestId('voice-status-error').boundingBox()
  const input = await composer.boundingBox()
  const top = Math.max(0, Math.min(strip?.y ?? input.y, input.y) - 24)
  const bottom = Math.max((strip?.y ?? 0) + (strip?.height ?? 0), input.y + input.height) + 72
  const left = Math.max(0, Math.min(strip?.x ?? input.x, input.x) - 24)
  const width = Math.min(1280 - left, Math.max(strip?.width ?? 0, input.width) + 48)
  await page.screenshot({ path: `${OUT}/${name}.png`, clip: { x: left, y: top, width, height: bottom - top } })
}

// 1. A 500: the request can clear by itself, so Retry leads and Save and
//    Discard share the overflow menu.
await failOnce(500, { error: 'transcription failed', code: 'stt_transcription_failed' })
await shotComposer('kept-recording-500-dark')
await page.evaluate(() => { document.documentElement.dataset.theme = 'light' })
await page.waitForTimeout(600)
await shotComposer('kept-recording-500-light')
await page.evaluate(() => { document.documentElement.dataset.theme = 'dark' })
await page.waitForTimeout(400)
await page.getByTestId('voice-kept-more').click()
await page.getByRole('menuitem', { name: 'Discard recording' }).waitFor({ timeout: 5000 })
await page.waitForTimeout(300)
{
  // The composer gives the horizontal extent: the notice's own box ends where
  // the recording's actions begin.
  const strip = await page.getByTestId('voice-status-error').boundingBox()
  const box = await composer.boundingBox()
  const menu = await page.getByRole('menu').boundingBox()
  const top = Math.max(0, Math.min(strip.y, menu.y) - 24)
  const bottom = Math.max(strip.y + strip.height, menu.y + menu.height) + 24
  const left = Math.max(0, Math.min(box.x, strip.x) - 24)
  const right = Math.min(1280, Math.max(box.x + box.width, menu.x + menu.width) + 24)
  await page.screenshot({ path: `${OUT}/kept-recording-500-menu-dark.png`, clip: { x: left, y: top, width: right - left, height: bottom - top } })
}
await page.getByRole('menuitem', { name: 'Discard recording' }).click()
await page.getByTestId('voice-status-error').waitFor({ state: 'detached', timeout: 5000 })

// 2. A 413: the gateway refused this audio, so only Save and Discard.
await failOnce(413, { error: 'audio too large', code: 'stt_audio_too_large' })
await shotComposer('kept-recording-413-dark')
await page.getByTestId('voice-kept-discard').click()
await page.getByTestId('voice-status-error').waitFor({ state: 'detached', timeout: 5000 })

// 3. Retry after a 500, answered with text.
await failOnce(500, { error: 'transcription failed', code: 'stt_transcription_failed' })
sttAnswers.push([200, { text: 'summarize what changed in the startup fix' }])
await page.getByTestId('voice-kept-retry').click()
await page.getByTestId('voice-status-error').waitFor({ state: 'detached', timeout: 10000 })
await page.waitForFunction(() => document.querySelector('[aria-label="Message input"]')?.textContent?.includes('startup fix'), null, { timeout: 10000 })
await page.waitForTimeout(400)
const input = await composer.boundingBox()
await page.screenshot({ path: `${OUT}/kept-recording-retried-dark.png`, clip: { x: Math.max(0, input.x - 24), y: Math.max(0, input.y - 48), width: Math.min(1280, input.width + 48), height: input.height + 120 } })

await context.close()
await browser.close()
srv.close()

console.log(JSON.stringify({ out: OUT, errors }, null, 2))
if (errors.length) process.exitCode = 1

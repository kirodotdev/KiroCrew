/**
 * Capture harness for chat-core P3-b (#9775): ChatPane composer parity.
 *
 * Shoots the Crew Members DM composer on two REAL pods — a `main`-based one
 * (before) and this branch's (after) — in light and dark:
 *   crew-dm-composer-{before,after}-{theme}.png   mic button appears
 *   crew-dm-recording-{theme}.png                 dictation in progress
 *   crew-dm-voice-setup-{theme}.png               mic click with STT off → modal
 *   split-mic-held-elsewhere-{theme}.png          two panes: one records, the other's mic is held
 *   split-hold-bar-held-elsewhere-{theme}.png     same, on a touch device in hold-to-talk mode
 *   crew-dm-mic-error-{theme}.png                 mic permission blocked → ErrorNotice above the composer
 *   crew-dm-held-transcript-redelivered-{theme}.png transcript held while the pane showed another member, landed on return
 *
 * Usage:
 *   POD_INFO=<after.json> POD_BEFORE=<before.json> node scripts/capture-pane-composer-parity.mjs <outdir>
 *
 * Both JSON files are the last line of `kirocrew pod up <name> --json`.
 *
 * Recording uses Chromium's fake audio device, and `/api/config/stt` is
 * answered `enabled+available` from the harness (the pod's STT provider is not
 * installed) — everything rendered is the real composer reacting to a real
 * MediaRecorder capture. Esc discards the take, so no transcription is posted.
 */
import { chromium } from 'playwright'
import fs from 'node:fs'
import path from 'node:path'

const after = JSON.parse(fs.readFileSync(process.env.POD_INFO, 'utf8').trim().split('\n').pop())
const before = JSON.parse(fs.readFileSync(process.env.POD_BEFORE, 'utf8').trim().split('\n').pop())
const out = path.resolve(process.argv[2] || 'temp-screenshots/pane-composer-parity')
fs.mkdirSync(out, { recursive: true })

const VIEW = { width: 1280, height: 800 }
const MEMBER = 'default'
const COMPOSER = 'textarea[aria-label]'

async function settle(page) {
  await page.waitForURL(u => !String(u).includes('token='), { timeout: 20_000 }).catch(() => {})
  await page.waitForTimeout(800)
}

/** The pod token is exchanged for a session on first use, so log in ONCE per
 *  pod and hand every later context the resulting storage state. */
const authState = new Map()
async function login(browser, pod) {
  if (authState.has(pod.base_url)) return authState.get(pod.base_url)
  const ctx = await browser.newContext({ viewport: VIEW })
  const page = await ctx.newPage()
  await page.goto(`${pod.base_url}/?token=${pod.token}`, { waitUntil: 'load' })
  await settle(page)
  const ok = await page.evaluate(async () => (await fetch('/api/status')).status)
  if (ok !== 200) throw new Error(`login to ${pod.base_url} failed: /api/status ${ok}`)
  const state = await ctx.storageState()
  await ctx.close()
  authState.set(pod.base_url, state)
  return state
}

/** `stt` answers the config as enabled (the pod has no provider installed);
 *  `sttDelayMs` also answers the transcription itself, late, with a fixed text
 *  — the held-transcript capture needs a settle that lands AFTER the pane has
 *  switched slots. `members: false` stays on the main chat. */
async function session(browser, pod, theme, { stt = false, sttDelayMs = 0, members = true, touch = false, micDenied = false } = {}) {
  const storageState = await login(browser, pod)
  // `touch`: a coarse-pointer device, which is what turns the mic into the
  // hold-to-talk mode switch. `hasTouch` alone does not flip the CSS media
  // query, so the query is emulated as well.
  const ctx = await browser.newContext({ viewport: VIEW, permissions: ['microphone'], storageState, hasTouch: touch })
  const page = await ctx.newPage()
  if (micDenied) {
    // The permission prompt answered "block": the engine's start path reports it
    // and VoiceStatusBar renders the error (through ErrorNotice).
    await page.addInitScript(() => {
      navigator.mediaDevices.getUserMedia = () => Promise.reject(new DOMException('Permission denied', 'NotAllowedError'))
    })
  }
  if (touch) {
    await page.addInitScript(() => {
      const orig = window.matchMedia.bind(window)
      window.matchMedia = (q) => (/pointer:\s*coarse|hover:\s*none/.test(q)
        ? { matches: true, media: q, onchange: null, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}, dispatchEvent: () => false }
        : orig(q))
    })
  }
  if (stt) {
    await page.route('**/api/config/stt', route => route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ enabled: true, available: true, streaming: false, dictation_panel: true, provider: 'local' }),
    }))
  }
  if (sttDelayMs) {
    await page.route('**/api/stt/transcribe', async route => {
      await new Promise(r => setTimeout(r, sttDelayMs))
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ text: 'saved while you were in another chat' }) })
    })
  }
  await page.goto(`${pod.base_url}/`, { waitUntil: 'load' })
  await settle(page)
  const status = await page.evaluate(async (mode) => {
    const r = await fetch('/api/config/theme', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode }) })
    return r.status
  }, theme)
  if (status !== 200) throw new Error(`theme PUT ${status}`)
  await page.evaluate((t) => {
    localStorage.setItem('mc-theme', t)
    localStorage.setItem('mc-preview-crew', '1')
  }, theme)
  if (!members) return { ctx, page }
  await page.goto(`${pod.base_url}/members?member=${MEMBER}`, { waitUntil: 'load' })
  await settle(page)
  // A pod running an older build gets the "update available" popup, whose
  // backdrop blurs the whole page. Snooze it (persists in the pod's config).
  const snooze = page.getByRole('button', { name: /Remind me tomorrow/ })
  if (await snooze.isVisible().catch(() => false)) { await snooze.click(); await page.waitForTimeout(500) }
  await page.locator(COMPOSER).first().waitFor({ timeout: 20_000 })
  await page.waitForTimeout(600)
  return { ctx, page }
}

/** The pane's lower region: the ChatPane root (`[data-chat-pane]`) is the
 *  stable frame in every state — recording swaps the textarea for the
 *  dictation panel, so nothing inside the composer is a safe anchor. */
async function composerClip(page, extraTop = 140) {
  const box = await page.locator('[data-chat-pane]').first().boundingBox()
  const height = Math.min(box.height, 200 + extraTop)
  const y = box.y + box.height - height
  return { x: Math.max(0, box.x - 8), y, width: Math.min(VIEW.width - Math.max(0, box.x - 8), box.width + 16), height: Math.min(VIEW.height - y, height + 8) }
}

/** The lowest `height` px of the viewport — both composers of a split view. */
async function bottomStrip(page, height) {
  return { x: 0, y: VIEW.height - height, width: VIEW.width, height }
}

async function shoot(page, file, clip) {
  await page.screenshot({ path: file, clip })
  console.log(`${path.basename(file)}  ${fs.statSync(file).size} B`)
}

const browser = await chromium.launch({ args: ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream'] })
try {
  for (const theme of ['light', 'dark']) {
    // BEFORE — main: no mic on the pane composer.
    {
      const { ctx, page } = await session(browser, before, theme)
      await shoot(page, path.join(out, `crew-dm-composer-before-${theme}.png`), await composerClip(page, 40))
      await ctx.close()
    }
    // AFTER — mic present; STT off on this pod → the setup modal on click.
    {
      const { ctx, page } = await session(browser, after, theme)
      await page.getByRole('button', { name: /^(Voice input|Stop recording|Switch to voice)$/ }).first().waitFor({ timeout: 10_000 })
      await shoot(page, path.join(out, `crew-dm-composer-after-${theme}.png`), await composerClip(page, 40))
      await page.getByRole('button', { name: /^(Voice input|Stop recording|Switch to voice)$/ }).first().click()
      await page.getByRole('dialog').waitFor({ timeout: 10_000 })
      await page.waitForTimeout(400)
      await shoot(page, path.join(out, `crew-dm-voice-setup-${theme}.png`))
      await page.keyboard.press('Escape')
      await ctx.close()
    }
    // AFTER with STT on — recording (dictation panel at pane width).
    {
      const { ctx, page } = await session(browser, after, theme, { stt: true })
      const mic = page.getByRole('button', { name: /^(Voice input|Stop recording|Switch to voice)$/ }).first()
      await mic.click()
      // The dictation panel / pulsing mic is driven by the real MediaRecorder.
      await page.waitForTimeout(1500)
      await shoot(page, path.join(out, `crew-dm-recording-${theme}.png`), await composerClip(page, 220))
      await page.keyboard.press('Escape') // discard: nothing is transcribed
      await page.waitForTimeout(600)
      await ctx.close()
    }
    // AFTER, microphone blocked by the browser: the error renders through
    // ErrorNotice above the composer, dismissible.
    {
      const { ctx, page } = await session(browser, after, theme, { stt: true, micDenied: true })
      await page.getByRole('button', { name: /^(Voice input|Stop recording|Switch to voice)$/ }).first().click()
      await page.getByTestId('voice-status-error').waitFor({ timeout: 10_000 })
      await page.waitForTimeout(400)
      await shoot(page, path.join(out, `crew-dm-mic-error-${theme}.png`), await composerClip(page, 60))
      await ctx.close()
    }
    // AFTER, split view: two panes, one recording — the other's mic reads
    // "in use in another chat" (MicOff, disabled), not "Transcribing".
    {
      const { ctx, page } = await session(browser, after, theme, { stt: true, members: false })
      const slots = await page.evaluate(async () => (await (await fetch('/api/chat/slots')).json()).map(s => s.key))
      if (slots.length < 2) throw new Error('need two slots for the split-view capture')
      // The store is a map keyed by the split's anchor slot; ChatPage auto-enters
      // split view when that slot is active.
      await page.evaluate(([a, b]) => {
        localStorage.setItem('mc-split-layouts', JSON.stringify({ [a]: { type: 'split', dir: 'col', children: [{ type: 'leaf', kind: 'session', slot: a }, { type: 'leaf', kind: 'session', slot: b }], sizes: [50, 50] } }))
      }, slots.slice(0, 2))
      await page.goto(`${after.base_url}/?sid=${encodeURIComponent(slots[0])}`, { waitUntil: 'load' })
      await settle(page)
      await page.locator('[data-chat-pane]').nth(1).waitFor({ timeout: 20_000 })
      const mics = page.getByRole('button', { name: /^(Voice input|Stop recording)$/ })
      await mics.first().click()
      await page.getByRole('button', { name: /^Microphone in use in / }).waitFor({ timeout: 10_000 })
      await page.waitForTimeout(800)
      await shoot(page, path.join(out, `split-mic-held-elsewhere-${theme}.png`), await bottomStrip(page, 300))
      await page.keyboard.press('Escape')
      await ctx.close()
    }
    // AFTER, split view on a touch device in hold-to-talk mode: holding one
    // pane's bar records; the other pane's bar reads "in use in another chat".
    {
      const { ctx, page } = await session(browser, after, theme, { stt: true, members: false, touch: true })
      const slots = await page.evaluate(async () => (await (await fetch('/api/chat/slots')).json()).map(s => s.key))
      await page.evaluate(([a, b]) => {
        localStorage.setItem('mc-split-layouts', JSON.stringify({ [a]: { type: 'split', dir: 'col', children: [{ type: 'leaf', kind: 'session', slot: a }, { type: 'leaf', kind: 'session', slot: b }], sizes: [50, 50] } }))
        localStorage.setItem('mc-voice-mode', '1')
      }, slots.slice(0, 2))
      await page.goto(`${after.base_url}/?sid=${encodeURIComponent(slots[0])}`, { waitUntil: 'load' })
      await settle(page)
      const bars = page.getByTestId('hold-to-talk')
      await bars.nth(1).waitFor({ timeout: 20_000 })
      const box = await bars.first().boundingBox()
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
      await page.mouse.down()
      await page.waitForTimeout(1200) // past the hold threshold: capture is live
      // In hold mode the reason lives in the status row (the bar keeps its plain label).
      await page.getByTestId('voice-status-notice').filter({ hasText: /^Microphone in use in / }).waitFor({ timeout: 10_000 })
      await page.waitForTimeout(500)
      await shoot(page, path.join(out, `split-hold-bar-held-elsewhere-${theme}.png`), await bottomStrip(page, 300))
      await page.mouse.up()
      await page.waitForTimeout(400)
      await page.keyboard.press('Escape')
      await ctx.close()
    }
    // AFTER, held transcript: dictate in one member's DM, switch to another
    // member before /api/stt answers, come back — the text lands then.
    {
      const { ctx, page } = await session(browser, after, theme, { stt: true, sttDelayMs: 4000 })
      const mic = page.getByRole('button', { name: /^(Voice input|Stop recording)$/ }).first()
      await mic.click()
      await page.waitForTimeout(1500)
      await mic.click() // stop → the (delayed) transcription is in flight
      await page.getByRole('button', { name: /^radar$/ }).click()
      await page.waitForTimeout(5000) // settles while the pane shows radar: held, not dropped
      await page.getByRole('button', { name: /^default$/ }).click()
      await page.locator(COMPOSER).first().waitFor({ timeout: 10_000 })
      await page.waitForTimeout(800)
      await shoot(page, path.join(out, `crew-dm-held-transcript-redelivered-${theme}.png`), await composerClip(page, 40))
      await ctx.close()
    }
  }
} finally {
  await browser.close()
}

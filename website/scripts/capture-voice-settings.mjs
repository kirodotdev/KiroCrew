/**
 * Screenshots of the Speech-to-Text card's microphone and model controls
 * (#17601, #17602) via the capture/voice-settings harness.
 *
 * The microphone is Chromium's fake capture device, not a mock: the flags below
 * make getUserMedia return a generated tone and auto-grant the permission
 * prompt. The script prints the picker's option labels, and, when a mic test
 * button exists, starts it and records the level bar's width so the frame is
 * proof the meter moved.
 *
 * Usage: node scripts/capture-voice-settings.mjs <viteBase> <outDir> <prefix>
 */
import { chromium } from 'playwright'

const base = process.argv[2] || 'http://localhost:5199'
const out = process.argv[3] || '../temp-screenshots/voice-settings'
const prefix = process.argv[4] || 'after'

const b = await chromium.launch({
  args: [
    '--use-fake-device-for-media-stream',
    '--use-fake-ui-for-media-stream',
    // Optional steady input (a WAV), so a frame shows a held level rather than
    // landing between the default fake device's short beeps.
    ...(process.env.FAKE_AUDIO_WAV ? [`--use-file-for-fake-audio-capture=${process.env.FAKE_AUDIO_WAV}`] : []),
  ],
})
for (const [scene, theme] of [['idle', 'dark'], ['idle', 'light']]) {
  const ctx = await b.newContext({ viewport: { width: 820, height: 1100 }, deviceScaleFactor: 2 })
  await ctx.grantPermissions(['microphone'], { origin: base })
  const p = await ctx.newPage()
  // Count live capture tracks, so the script can prove Stop released them.
  await p.addInitScript(() => {
    const md = navigator.mediaDevices
    const orig = md.getUserMedia.bind(md)
    const streams = []
    window.__liveTracks = () => streams.flatMap(s => s.getTracks()).filter(t => t.readyState === 'live').length
    md.getUserMedia = async c => { const s = await orig(c); streams.push(s); return s }
  })
  await p.goto(`${base}/capture/voice-settings.html?scene=${scene}&theme=${theme}`, { waitUntil: 'networkidle' })
  const mic = p.getByRole('combobox', { name: /microphone/i })
  await mic.waitFor({ timeout: 20_000 })
  // Grant once so device labels are populated, the way a user who has
  // dictated before sees the panel.
  await p.evaluate(async () => {
    const s = await navigator.mediaDevices.getUserMedia({ audio: true })
    s.getTracks().forEach(t => t.stop())
    navigator.mediaDevices.dispatchEvent(new Event('devicechange'))
  })
  await p.waitForTimeout(300)
  // The picker is a custom listbox, so its options exist only while it is open.
  await mic.click()
  const labels = await p.getByRole('option').allTextContents()
  console.log(`${scene}/${theme} microphone options: ${JSON.stringify(labels)}`)
  await p.keyboard.press('Escape')
  const test = p.getByRole('button', { name: /test microphone/i })
  const runTest = scene === 'idle' && theme === 'dark' && (await test.count()) > 0
  if (runTest) {
    await test.click()
    const meter = p.getByRole('meter')
    await meter.waitFor({ timeout: 10_000 })
    let peak = 0
    for (let i = 0; i < 40; i++) {
      peak = Math.max(peak, Number(await meter.getAttribute('aria-valuenow')))
      await p.waitForTimeout(100)
    }
    console.log(`mic test peak level ${peak}`)
    if (peak <= 0) throw new Error('mic test level never moved')
    // Shoot on a beep, so the frame shows the bar up rather than between tones.
    await p.waitForFunction(() => Number(document.querySelector('[role=meter]')?.getAttribute('aria-valuenow')) >= 40, null, { timeout: 10_000, polling: 'raf' })
  }
  const file = `${out}/${prefix}-${scene}-${theme}.png`
  await p.locator('[data-capture-root]').screenshot({ path: file })
  console.log(`captured ${file}`)
  if (runTest) {
    console.log(`live tracks while testing: ${await p.evaluate(() => window.__liveTracks())}`)
    await p.getByRole('button', { name: /stop test/i }).click()
    const live = await p.evaluate(() => window.__liveTracks())
    console.log(`after stop: live tracks ${live}`)
    if (live !== 0) throw new Error('Stop left a capture track live')
  }
  await ctx.close()
}
await b.close()

// Error states. Both are SIMULATED in the page: a permission denial makes
// getUserMedia reject with the browser's own NotAllowedError, and a dropped
// device dispatches `ended` on the live track. Also measures where Provider
// sits idle and during a test, to prove the status line keeps it in place.
{
  const b2 = await chromium.launch({
    args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'],
  })
  const shoot = async (name, prep) => {
    const ctx = await b2.newContext({ viewport: { width: 820, height: 1100 }, deviceScaleFactor: 2 })
    await ctx.grantPermissions(['microphone'], { origin: base })
    const p = await ctx.newPage()
    await p.addInitScript(prep)
    await p.goto(`${base}/capture/voice-settings.html?scene=idle&theme=dark`, { waitUntil: 'networkidle' })
    await p.getByRole('combobox', { name: /microphone/i }).waitFor({ timeout: 20_000 })
    return { ctx, p }
  }
  {
    const { ctx, p } = await shoot('denied', () => {
      navigator.mediaDevices.getUserMedia = async () => { throw new DOMException('denied', 'NotAllowedError') }
    })
    await p.getByRole('button', { name: /test microphone/i }).click()
    await p.getByText(/permission denied/i).waitFor({ timeout: 10_000 })
    await p.locator('[data-capture-root]').screenshot({ path: `${out}/${prefix}-error-denied-dark.png` })
    console.log('captured error-denied')
    await ctx.close()
  }
  {
    const { ctx, p } = await shoot('dropped', () => {
      const md = navigator.mediaDevices
      const orig = md.getUserMedia.bind(md)
      window.__tracks = []
      md.getUserMedia = async c => { const s = await orig(c); window.__tracks.push(...s.getAudioTracks()); return s }
    })
    const provider = p.getByRole('combobox', { name: /provider/i })
    const yIdle = (await provider.boundingBox()).y
    await p.getByRole('button', { name: /test microphone/i }).click()
    // The meter shows while the device opens; wait for the live line.
    await p.getByText(/listening/i).waitFor({ timeout: 10_000 })
    const yLive = (await provider.boundingBox()).y
    console.log(`provider y idle=${yIdle} testing=${yLive}`)
    if (Math.abs(yIdle - yLive) > 1) throw new Error('starting a test moved the fields below it')
    await p.evaluate(() => {
      const t = window.__tracks.at(-1)
      t.stop()
      t.dispatchEvent(new Event('ended'))
    })
    await p.getByText(/the microphone disconnected/i).waitFor({ timeout: 10_000 })
    await p.locator('[data-capture-root]').screenshot({ path: `${out}/${prefix}-error-dropped-dark.png` })
    console.log('captured error-dropped')
    await ctx.close()
  }
  await b2.close()
}

// The open picker (no second "Default" row), and the "Listening." line a
// track with no label gets. The unlabeled track is SIMULATED by overriding the
// fake device track's `label` getter in the page.
{
  const b3 = await chromium.launch({
    args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'],
  })
  const ctx = await b3.newContext({ viewport: { width: 820, height: 1100 }, deviceScaleFactor: 2 })
  await ctx.grantPermissions(['microphone'], { origin: base })
  const p = await ctx.newPage()
  await p.goto(`${base}/capture/voice-settings.html?theme=dark`, { waitUntil: 'networkidle' })
  const mic = p.getByRole('combobox', { name: /microphone/i })
  await mic.waitFor({ timeout: 20_000 })
  await p.evaluate(async () => {
    const s = await navigator.mediaDevices.getUserMedia({ audio: true })
    s.getTracks().forEach(t => t.stop())
    navigator.mediaDevices.dispatchEvent(new Event('devicechange'))
  })
  await p.getByText(/system default \(/i).first().waitFor({ timeout: 10_000 })
  await mic.click()
  await p.getByRole('option').first().waitFor({ timeout: 10_000 })
  // Wait out the list's fade-in: a frame taken mid-animation shows the rows
  // behind the opaque panel and reads as overlapping text.
  await p.waitForFunction(() => {
    const el = document.querySelector('[role=listbox]')
    return !!el && el.getAnimations({ subtree: true }).every(a => a.playState === 'finished')
  }, null, { timeout: 5_000 })
  await p.locator('[data-capture-root]').screenshot({ path: `${out}/${prefix}-picker-open-dark.png` })
  console.log('captured picker-open')
  await p.keyboard.press('Escape')
  await p.evaluate(() => {
    Object.defineProperty(MediaStreamTrack.prototype, 'label', { get: () => '', configurable: true })
  })
  await p.getByRole('button', { name: /test microphone/i }).click()
  await p.getByText(/^listening\. speak/i).waitFor({ timeout: 10_000 })
  await p.locator('[data-capture-root]').screenshot({ path: `${out}/${prefix}-listening-unnamed-dark.png` })
  console.log('captured listening-unnamed')
  await b3.close()
}

// The `starting` state: getUserMedia is held pending (SIMULATED), standing in
// for the moment the browser's permission prompt is open. Headless Chromium
// cannot draw that prompt, so the frame shows the strip without it.
{
  const b4 = await chromium.launch({
    args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'],
  })
  const ctx = await b4.newContext({ viewport: { width: 820, height: 1100 }, deviceScaleFactor: 2 })
  const p = await ctx.newPage()
  await p.addInitScript(() => {
    navigator.mediaDevices.getUserMedia = () => new Promise(() => {})
  })
  await p.goto(`${base}/capture/voice-settings.html?theme=dark`, { waitUntil: 'networkidle' })
  await p.getByRole('button', { name: /test microphone/i }).click()
  await p.getByText(/opening the microphone/i).waitFor({ timeout: 10_000 })
  await p.locator('[data-capture-root]').screenshot({ path: `${out}/${prefix}-opening-dark.png` })
  console.log('captured opening')
  await b4.close()
}

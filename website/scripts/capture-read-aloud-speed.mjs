/**
 * Screenshot harness + assertions for the Read aloud playback bar's speed control.
 *
 * Drives the REAL built SPA (website/dist) with the gateway stubbed: `/api/voice/config`
 * keeps the speaking rate in memory (GET reads it, PUT stores it), and one silent WAV
 * chunk pushed over the mocked socket makes the bar show "Reading aloud".
 *
 * Writes three labelled composites (several states stacked in one PNG, each with a caption):
 *   desktop-wide-dark  before a choice, every preset chosen with the applies-next note, plus the
 *                      load-error and save-error rows
 *   phone-390-dark     preparing, playing, a choice with its note, plus the same two error rows
 *   phone-320-and-light  the narrowest width, and the light theme
 *
 * Exits non-zero unless:
 *   - the second row is a "Speed" radio group whose checked option is the stored rate,
 *     and choosing a preset sends PUT /api/voice/config with that rate;
 *   - arrow keys move focus without saving, and Enter saves the focused option;
 *   - the first row holds exactly two buttons (Pause or Resume, Stop) and the second row the
 *     Speed group and Read again, which keeps its label at every width;
 *   - the status text is never truncated on a 361-430px phone, preparing or playing;
 *   - on a touch viewport every option is at least 40px tall, and at 320px nothing overflows;
 *   - a failed load and a failed save each show their notice under the radio group;
 *   - the applies-next note is empty before a choice, shows after one, and never moves the group.
 *
 * Usage: node scripts/capture-read-aloud-speed.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/read-aloud-speed'
const SLOT = 'chat-read-aloud'
const PROJECT = '/home/user/workspace/KiroCrew'
const REQUEST_ID = 'speed-capture'

const slots = [{
  key: SLOT,
  title: 'Summarize the release notes',
  running: false,
  last_message: 'Here is the summary.',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  folder_id: '',
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: Date.now() / 1000 - 600, content: 'Summarize the release notes.' },
    { role: 'assistant', ts: Date.now() / 1000 - 60, content: 'Version 2 adds a speaking-speed control to the Read aloud bar.' },
  ],
}

/** Sixty seconds of 8-bit mono silence, so the bar stays up for the whole capture. */
function silentWavBase64(seconds = 60, sampleRate = 8000) {
  const data = Buffer.alloc(seconds * sampleRate, 128)
  const header = Buffer.alloc(44)
  header.write('RIFF', 0)
  header.writeUInt32LE(36 + data.length, 4)
  header.write('WAVEfmt ', 8)
  header.writeUInt32LE(16, 16)
  header.writeUInt16LE(1, 20)
  header.writeUInt16LE(1, 22)
  header.writeUInt32LE(sampleRate, 24)
  header.writeUInt32LE(sampleRate, 28)
  header.writeUInt16LE(1, 32)
  header.writeUInt16LE(8, 34)
  header.write('data', 36)
  header.writeUInt32LE(data.length, 40)
  return Buffer.concat([header, data]).toString('base64')
}

const failures = []
const check = (ok, message) => { if (!ok) failures.push(message) }

async function openBar({ browser, viewport, theme, touch, failLoad = false, failSave = false, voiceConfig = {} }) {
  const context = await browser.newContext({
    viewport, deviceScaleFactor: 2, hasTouch: touch, isMobile: touch,
  })
  const page = await context.newPage()
  logPageProblems(page)
  const voiceState = { rate: '100%', ...voiceConfig }
  const saved = []
  let wsServer = null
  await stubDashboardApi(page, {
    slots,
    theme,
    localStorageEntries: { 'mc-active-slot': SLOT },
    onWebSocket: ws => { wsServer = ws },
    extra: async (path, route) => {
      if (path === '/api/voice/config') {
        if (route.request().method() === 'PUT') {
          const body = JSON.parse(route.request().postData() || '{}')
          saved.push(body)
          if (failSave) {
            await route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ error: 'write failed' }) })
            return true
          }
          Object.assign(voiceState, body)
          await json(route, { ok: true })
        } else if (failLoad) {
          await route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ error: 'load failed' }) })
        } else {
          await json(route, {
            enabled: true, provider: 'system', voice: 'Ruth', engine: 'generative',
            pitch: '+0%', autoSpeak: false, aws_profile: '', region: '', system_voice: '',
            language_code: '', language_codes: [''], providers: [], ...voiceState,
          })
        }
        return true
      }
      if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
      return false
    },
  })
  await page.goto(`${base}/`, { waitUntil: 'domcontentloaded' })
  await page.getByLabel('Message input').waitFor({ timeout: 20000 })
  await page.waitForFunction(() => true)
  // The dashboard's own start event registers the request, as Read aloud does.
  await page.evaluate(({ slot, id }) => {
    window.dispatchEvent(new CustomEvent('voice-synthesis-start', { detail: { slot, request_id: id, text: 'x' } }))
  }, { slot: SLOT, id: REQUEST_ID })
  await page.getByTestId('voice-playback-bar').waitFor({ timeout: 10000 })
  return { context, page, saved, sendChunk: () => wsServer.send(JSON.stringify({
    type: 'voice_chunk',
    data: { slot: SLOT, request_id: REQUEST_ID, audio: silentWavBase64(), audioMime: 'audio/wav' },
  })) }
}

mkdirSync(OUT, { recursive: true })
const { srv, base } = await serveDist()
const browser = await chromium.launch({ args: ['--autoplay-policy=no-user-gesture-required'] })

const radio = (page, name) => page.getByRole('radio', { name, exact: true })
const checkedRates = page => page.getByRole('radio').evaluateAll(
  radios => radios.filter(r => r.getAttribute('aria-checked') === 'true').map(r => r.textContent.trim()),
)
const waitForChecked = (page, rate) => page.waitForFunction(
  text => [...document.querySelectorAll('[role="radio"][aria-checked="true"]')].some(r => r.textContent.trim() === text),
  rate,
)
const waitForLoaded = page => page.waitForFunction(() => {
  const radios = document.querySelectorAll('[data-testid="voice-playback-speed"] [role="radio"]')
  return radios.length === 4 && [...radios].every(r => !r.disabled)
})
const NOTE = 'Applies to what is read next. Press Read again to hear the whole reply at this speed.'
const waitForNote = page => page.getByTestId('voice-speed-applies-next').filter({ hasText: NOTE }).waitFor({ timeout: 10000 })
const barPng = page => page.locator('[data-testid="voice-playback-bar"] > div').screenshot()
const startReading = async ({ page, sendChunk }) => {
  sendChunk()
  await page.getByRole('status').filter({ hasText: 'Reading aloud' }).waitFor({ timeout: 10000 })
}
const statusTruncation = page => page.getByTestId('voice-playback-bar').getByRole('status').first().evaluate(el => el.scrollWidth - el.clientWidth)

/** One composite PNG: each state's bar under a caption, drawn from fixed HTML so reruns match. */
async function writeComposite(name, items) {
  const size = png => ({ width: png.readUInt32BE(16) / 2, height: png.readUInt32BE(20) / 2 })
  const padding = 24
  const captionHeight = 26
  const gap = 18
  const sizes = items.map(item => size(item.png))
  const width = Math.max(...sizes.map(s => s.width)) + padding * 2
  const height = padding * 2 + sizes.reduce((sum, s) => sum + captionHeight + s.height + gap, -gap)
  const deviceScaleFactor = Math.min(2, Math.floor((1990 / Math.max(width, height)) * 100) / 100)
  const html = `<body style="margin:0;background:#2a2a2e;font:14px/${captionHeight}px -apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#f2f2f2">
    <div style="padding:${padding}px;display:flex;flex-direction:column;gap:${gap}px">${items.map(item => `
      <figure style="margin:0"><figcaption style="height:${captionHeight}px;font-weight:600;white-space:nowrap">${item.caption}</figcaption>
      <img alt="${item.caption}" src="data:image/png;base64,${item.png.toString('base64')}" style="display:block;width:${size(item.png).width}px;height:${size(item.png).height}px;border-radius:8px"></figure>`).join('')}
    </div></body>`
  const context = await browser.newContext({ viewport: { width, height }, deviceScaleFactor })
  const page = await context.newPage()
  await page.setContent(html)
  await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: true })
  await context.close()
  console.log('wrote', `${OUT}/${name}.png`, `${width}x${height} css at ${deviceScaleFactor}x`)
}

/** The bar with the saved rate failing to load, or failing to save when 120% is chosen. */
async function errorBar({ failure, viewport, theme = 'dark', touch }) {
  const opened = await openBar({ browser, viewport, theme, touch, failLoad: failure === 'load', failSave: failure === 'save' })
  const { context, page } = opened
  await startReading(opened)
  if (failure === 'save') {
    await waitForLoaded(page)
    await radio(page, '120%').click()
  }
  const notice = page.getByTestId(failure === 'load' ? 'voice-speed-load-error' : 'voice-speed-save-error')
  await notice.waitFor({ timeout: 10000 })
  const groupBox = await page.getByRole('radiogroup', { name: 'Speed' }).boundingBox()
  const noticeBox = await notice.boundingBox()
  check(noticeBox.y >= groupBox.y + groupBox.height, `${failure} notice should sit under the radio group at ${viewport.width}px`)
  if (failure === 'load') {
    check(await page.getByRole('button', { name: 'Retry' }).isVisible(), `Retry should show at ${viewport.width}px`)
  }
  const overflow = await page.evaluate(() => {
    const bar = document.querySelector('[data-testid="voice-playback-bar"] > div')
    return bar.scrollWidth - bar.clientWidth
  })
  check(overflow <= 0, `${failure} error bar overflows by ${overflow}px at ${viewport.width}px`)
  if (failure === 'save') {
    await page.getByTestId('voice-playback-stop').click()
    await page.getByTestId('voice-playback-bar').waitFor({ state: 'detached', timeout: 10000 })
    await notice.waitFor({ timeout: 10000 })
    check(await page.getByTestId('voice-speed-save-error').count() === 1, `save error should remain once after playback ends at ${viewport.width}px`)
    const paneClass = await notice.evaluate(node => node.parentElement?.className ?? '')
    check(/\bliquid-glass\b/.test(paneClass) && /\bglass-thick\b/.test(paneClass), `save error should sit on the dock's thick glass at ${viewport.width}px`)
  }
  await page.waitForTimeout(300)
  const png = failure === 'save' ? await notice.locator('xpath=..').screenshot() : await barPng(page)
  await context.close()
  return png
}

/** The bar when Polly neural cannot apply a speaking rate. */
async function unavailableBar({ viewport, theme = 'dark', touch }) {
  const opened = await openBar({
    browser, viewport, theme, touch,
    voiceConfig: { provider: 'polly', engine: 'neural' },
  })
  const { context, page } = opened
  await startReading(opened)
  const reason = page.getByTestId('voice-speed-unavailable')
  await reason.waitFor({ timeout: 10000 })
  const radios = page.getByRole('radiogroup', { name: 'Speed' }).getByRole('radio')
  check(await radios.count() === 4, `Polly neural should still show four rates at ${viewport.width}px`)
  for (let index = 0; index < await radios.count(); index++) {
    check(await radios.nth(index).isDisabled(), `Polly neural rate ${index + 1} should be disabled at ${viewport.width}px`)
  }
  check(await page.getByTestId('voice-speed-applies-next').textContent() === '', `Polly neural should not show the applies-next note at ${viewport.width}px`)
  await page.waitForTimeout(300)
  const png = await barPng(page)
  await context.close()
  return png
}

try {
  const desktop = { width: 1280, height: 720 }
  const desktopItems = []

  // Desktop dark: choose every preset, then drive the group from the keyboard.
  {
    const opened = await openBar({ browser, viewport: desktop, theme: 'dark', touch: false })
    const { context, page, saved } = opened
    await waitForLoaded(page)
    check(await page.getByRole('radiogroup', { name: 'Speed' }).count() === 1, 'there should be one radio group named Speed')
    check(JSON.stringify(await checkedRates(page)) === '["100%"]', `checked rates were ${await checkedRates(page)}`)
    const rowButtons = await page.locator('[data-testid="voice-playback-bar"] > div > div').evaluateAll(rows => rows.map(row =>
      [...row.querySelectorAll('button:not([role="radio"])')].map(b => b.getAttribute('data-testid'))))
    check(JSON.stringify(rowButtons) === '[["voice-playback-pause","voice-playback-stop"],["voice-playback-restart"]]', `bar rows held ${JSON.stringify(rowButtons)}`)
    await startReading(opened)
    check(await page.getByTestId('voice-speed-applies-next').textContent() === '', 'the note should be empty before a choice')
    // The dock is anchored to the composer, so a taller bar grows upwards: compare the
    // group's place inside the bar, not on the page.
    const groupInBar = async () => {
      const bar = await page.locator('[data-testid="voice-playback-bar"] > div').boundingBox()
      const group = await page.getByRole('radiogroup', { name: 'Speed' }).boundingBox()
      return { x: group.x - bar.x, y: group.y - bar.y }
    }
    const groupBefore = await groupInBar()
    await page.waitForTimeout(300)
    desktopItems.push({ caption: 'Desktop 1280px, dark · reading aloud, before any choice (no note)', png: await barPng(page) })
    for (const rate of ['80%', '100%', '120%', '150%']) {
      await radio(page, rate).click()
      await waitForChecked(page, rate)
      await waitForLoaded(page)
      await waitForNote(page)
      await page.waitForTimeout(300)
      desktopItems.push({ caption: `Desktop 1280px, dark · speed ${rate} chosen, with the note that it applies next`, png: await barPng(page) })
    }
    const groupAfter = await groupInBar()
    check(groupBefore.y === groupAfter.y && groupBefore.x === groupAfter.x, `the radio group moved inside the bar when the note appeared (${JSON.stringify(groupBefore)} -> ${JSON.stringify(groupAfter)})`)
    check(JSON.stringify(saved.map(body => body.rate)) === '["80%","100%","120%","150%"]', `saved rates were ${JSON.stringify(saved)}`)
    check(await page.getByTestId('voice-playback-bar').isVisible(), 'bar should stay while the rate changes')

    await radio(page, '150%').focus()
    await page.keyboard.press('ArrowRight')
    check(await page.evaluate(() => document.activeElement?.textContent?.trim()) === '80%', 'ArrowRight from 150% should focus 80%')
    check(saved.length === 4, 'moving focus must not save')
    await page.keyboard.press('Enter')
    await waitForChecked(page, '80%')
    check(JSON.stringify(saved.map(body => body.rate)) === '["80%","100%","120%","150%","80%"]', `saved rates were ${JSON.stringify(saved)}`)
    await context.close()
  }
  desktopItems.push({ caption: 'Desktop 1280px, dark · Polly neural selected, speed unavailable', png: await unavailableBar({ viewport: desktop, touch: false }) })
  desktopItems.push({ caption: 'Desktop 1280px, dark · the saved speed failed to load (Retry)', png: await errorBar({ failure: 'load', viewport: desktop, touch: false }) })
  desktopItems.push({ caption: 'Desktop 1280px, dark · after reading ended, the save failure remains in the composer dock', png: await errorBar({ failure: 'save', viewport: desktop, touch: false }) })
  await writeComposite('desktop-wide-dark', desktopItems)

  // Phones: the status text must read in full from 361px up, preparing and playing.
  for (const width of [361, 375, 390, 412, 430]) {
    const opened = await openBar({ browser, viewport: { width, height: 740 }, theme: 'dark', touch: true })
    const { context, page } = opened
    await waitForLoaded(page)
    check(await statusTruncation(page) <= 0, `preparing status is truncated at ${width}px`)
    await startReading(opened)
    check(await statusTruncation(page) <= 0, `reading status is truncated at ${width}px`)
    await context.close()
  }

  const phone = { width: 390, height: 740 }
  const phoneItems = []
  {
    const opened = await openBar({ browser, viewport: phone, theme: 'dark', touch: true })
    const { page, context } = opened
    await waitForLoaded(page)
    await page.waitForTimeout(300)
    phoneItems.push({ caption: 'Phone 390px, dark · preparing audio', png: await barPng(page) })
    await startReading(opened)
    await page.waitForTimeout(300)
    phoneItems.push({ caption: 'Phone 390px, dark · reading aloud, speed 100% selected', png: await barPng(page) })
    await radio(page, '120%').click()
    await waitForChecked(page, '120%')
    await waitForLoaded(page)
    await waitForNote(page)
    check(await statusTruncation(page) <= 0, 'reading status is truncated at 390px with the note shown')
    await page.waitForTimeout(300)
    phoneItems.push({ caption: 'Phone 390px, dark · speed 120% chosen, with the note that it applies next', png: await barPng(page) })
    await page.getByTestId('voice-playback-pause').click()
    await page.getByTestId('voice-playback-bar').getByRole('status').filter({ hasText: 'Paused' }).waitFor({ timeout: 10000 })
    await page.waitForTimeout(300)
    phoneItems.push({ caption: 'Phone 390px, dark · paused (Resume reading), speed row unchanged', png: await barPng(page) })
    for (const rate of ['80%', '100%', '120%', '150%']) {
      const box = await radio(page, rate).boundingBox()
      check(box.height >= 40 && box.width >= 40, `${rate} option is ${box.width}x${box.height}px at 390px on touch`)
    }
    await context.close()
  }
  phoneItems.push({ caption: 'Phone 390px, dark · Polly neural selected, speed unavailable', png: await unavailableBar({ viewport: phone, touch: true }) })
  phoneItems.push({ caption: 'Phone 390px, dark · the saved speed failed to load (Retry)', png: await errorBar({ failure: 'load', viewport: phone, touch: true }) })
  phoneItems.push({ caption: 'Phone 390px, dark · after reading ended, the save failure remains in the composer dock', png: await errorBar({ failure: 'save', viewport: phone, touch: true }) })
  await writeComposite('phone-390-dark', phoneItems)

  // The narrowest width, and the light theme.
  const narrowItems = []
  {
    const opened = await openBar({ browser, viewport: { width: 320, height: 740 }, theme: 'dark', touch: true })
    const { page, context } = opened
    await startReading(opened)
    await waitForLoaded(page)
    for (const rate of ['80%', '100%', '120%', '150%']) {
      const box = await radio(page, rate).boundingBox()
      check(box.height >= 40 && box.width >= 40, `${rate} option is ${box.width}x${box.height}px at 320px on touch`)
    }
    const restartBox = await page.getByTestId('voice-playback-restart').boundingBox()
    const pauseBox = await page.getByTestId('voice-playback-pause').boundingBox()
    const stopBox = await page.getByTestId('voice-playback-stop').boundingBox()
    check(restartBox.height >= 40 && pauseBox.height >= 40 && stopBox.height >= 40, 'Pause, Read again and Stop should be at least 40px tall at 320px on touch')
    check(pauseBox.x + pauseBox.width <= stopBox.x, 'Pause overlaps Stop at 320px')
    check((await page.getByTestId('voice-playback-restart').textContent()).trim() === 'Read again', 'Read again should keep its label at 320px')
    const overflow = await page.evaluate(() => {
      const bar = document.querySelector('[data-testid="voice-playback-bar"] > div')
      return bar.scrollWidth - bar.clientWidth
    })
    check(overflow <= 0, `bar overflows by ${overflow}px at 320px`)
    await page.waitForTimeout(300)
    narrowItems.push({ caption: 'Phone 320px, dark · reading aloud, speed 100% selected', png: await barPng(page) })
    await context.close()
  }
  narrowItems.push({ caption: 'Phone 320px, dark · after reading ended, the save failure remains in the composer dock', png: await errorBar({ failure: 'save', viewport: { width: 320, height: 740 }, touch: true }) })
  {
    const opened = await openBar({ browser, viewport: desktop, theme: 'light', touch: false })
    const { page, context } = opened
    await startReading(opened)
    await waitForLoaded(page)
    await radio(page, '120%').click()
    await waitForChecked(page, '120%')
    await waitForLoaded(page)
    await page.waitForTimeout(300)
    narrowItems.push({ caption: 'Desktop 1280px, light · speed 120% chosen, with the note that it applies next', png: await barPng(page) })
    await context.close()
  }
  narrowItems.push({ caption: 'Desktop 1280px, light · the saved speed failed to load (Retry)', png: await errorBar({ failure: 'load', viewport: desktop, theme: 'light', touch: false }) })
  await writeComposite('phone-320-and-light', narrowItems)
} finally {
  await browser.close()
  srv.close()
}

if (failures.length) {
  console.error(failures.join('\n'))
  process.exit(1)
}
console.log('PASS')

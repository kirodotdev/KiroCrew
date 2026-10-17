/** Real-browser evidence for #19608 on the REAL Members page: the crewmate DM
 * composer is ONE row while the draft fits on one line (attach · text · mic ·
 * send on a single line), stays one row through a short draft, moves its
 * toolbar under the text once the draft wraps, comes back to one row when the
 * draft is cut short again, and offers no prompt-optimizer button in either
 * shape. The box's height animates between the shapes.
 *
 * Mounts through capture/members-page.html with ?route=/members?member=Radar;
 * the API is route-intercepted, gateway-free. Serves the dev build itself
 * (vite in-process, loopback, ephemeral port).
 *
 * Frames per theme (dark, light), each clipped to the composer dock and the
 * last bubble above it:
 *   one-row-<theme>.png    empty draft: the single capsule row
 *   short-<theme>.png      a short draft: still the single row
 *   two-row-<theme>.png    a wrapped draft: toolbar under the text
 *
 * RECORD_VIDEO=1 additionally records the dark run (idle -> type -> clear) to
 * <out>/video-raw/*.webm at deviceScaleFactor 1; the caller turns it into a gif.
 *
 * Usage (in website/):
 *   node scripts/capture-crew-composer-one-row.mjs ../temp-screenshots/crew-composer
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { THREAD, routeRadarDm, openRadarDm, makeCheck } from './lib/radar-dm-fixture.mjs'

const OUT = resolve(process.argv[2] || '../temp-screenshots/crew-composer')
mkdirSync(OUT, { recursive: true })
const server = await createServer({
  configFile: './vite.config.ts',
  server: { host: '127.0.0.1', port: 5197, strictPort: false },
  logLevel: 'warn',
})
await server.listen()
const BASE = server.resolvedUrls?.local?.[0]?.replace(/\/$/, '') || `http://127.0.0.1:${server.config.server.port}`
const RECORD = process.env.RECORD_VIDEO === '1'
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
const state = { failures: 0 }
const check = makeCheck(state)

// A shorter thread than the run-members harness: one bubble per run, so the
// composer has room above it in a 720px frame.
const SHORT_THREAD = [THREAD[0], THREAD[1], THREAD[3], THREAD[4]]
// No Enter in the draft: on a send-on-Enter composer it would SEND the first
// line instead of breaking it. The sentence is long enough to wrap on its own.
const SHORT = 'Take a look at why the Design lane on #19258 went red,'
const REST = ' fill in the docs while you are there, and tell me which of the two fixes you would pick.'

for (const theme of ['dark', 'light']) {
  const record = RECORD && theme === 'dark'
  const ctx = await browser.newContext({
    viewport: { width: 1280, height: 720 },
    deviceScaleFactor: record ? 1 : 2,
    ...(record ? { recordVideo: { dir: resolve(OUT, 'video-raw'), size: { width: 1280, height: 720 } } } : {}),
  })
  const page = await ctx.newPage()
  const errors = []; page.on('pageerror', e => errors.push(String(e)))
  await routeRadarDm(page, { thread: SHORT_THREAD })
  await openRadarDm(page, BASE, theme)
  await page.waitForTimeout(600)

  const dock = page.getByTestId('composer-dock')
  const box = page.getByTestId('input-wrapper')
  // The frame is the dock plus the thread's last bubble, not the whole page.
  const clipTo = async () => {
    const d = await dock.boundingBox()
    return { x: Math.max(0, d.x - 24), y: Math.max(0, d.y - 120), width: Math.min(1280, d.width + 48), height: Math.min(720 - Math.max(0, d.y - 120), d.height + 150) }
  }
  const radiusOf = () => dock.evaluate(el => getComputedStyle(el).borderRadius)
  // The bevel layer's shadow stack encodes the rim width; it must not change
  // between the two shapes (the rim is pinned to the two-row height).
  const bevelOf = () => dock.evaluate(el => {
    const layers = [...el.querySelectorAll('[data-liquid-glass-layer]')]
    const bevel = layers.find(l => (l.style.boxShadow || '').includes('inset 1px 0'))
    return bevel ? bevel.style.boxShadow : ''
  })
  const textarea = box.locator('textarea').first()
  const attach = box.getByRole('button', { name: /add files|attach files/i })
  const send = box.getByRole('button', { name: 'Send' })
  const optimize = box.getByRole('button', { name: /optimi[sz]e prompt/i })

  // Idle: one row. Attach, mic and send share the text's line -- their boxes
  // overlap the textarea vertically.
  const sameLine = async () => {
    const t = await textarea.boundingBox(); const s = await send.boundingBox()
    return !!t && !!s && s.y < t.y + t.height && s.y + s.height > t.y
  }
  check(`[${theme}] idle: box is in the one-row shape`, await box.getAttribute('data-inline-toolbar') === 'true')
  check(`[${theme}] idle: send button sits on the text's line`, await sameLine())
  check(`[${theme}] idle: attach button is offered`, await attach.count() >= 1)
  check(`[${theme}] idle: send button is present (disabled on an empty draft)`, await send.count() === 1 && await send.isDisabled())
  check(`[${theme}] idle: no prompt-optimizer button`, await optimize.count() === 0)
  const idleH = (await box.boundingBox()).height
  check(`[${theme}] idle: dock is a capsule (radius = half its height)`, await radiusOf() === '23px' && Math.round((await dock.boundingBox()).height) === 46)
  const idleBevel = await bevelOf()
  // The fit probe's column must be the same width from either shape, or a
  // draft in the difference fits on two rows and not on one and the box
  // springs between the shapes with no keystroke.
  const probeWidth = () => page.getByTestId('composer-one-row-probe').evaluate(el => el.style.width)
  const idleProbe = await probeWidth()
  check(`[${theme}] idle: bevel layer found`, idleBevel.length > 0)
  // The caret starts close to the attach button, not a wide blank after it.
  // The text column tucks under the attach cluster to get there, so the
  // button's own right edge must still take the click, and the fit probe must
  // be exactly as wide as the live one-row column it stands in for.
  const row = await page.evaluate(() => {
    const wrap = document.querySelector('[data-testid="input-wrapper"]')
    const ta = wrap.querySelector('textarea')
    const btn = wrap.querySelector('[data-testid="composer-bottom-row"] > div > div')
    const b = btn.getBoundingClientRect()
    const hit = document.elementFromPoint(b.right - 2, b.top + b.height / 2)
    const textLeft = ta.getBoundingClientRect().left + parseFloat(getComputedStyle(ta).paddingLeft)
    return { gap: Math.round(textLeft - b.right), hit: btn.contains(hit), column: Math.round(ta.getBoundingClientRect().width) }
  })
  check(`[${theme}] idle: text starts within 10px of the attach button (${row.gap}px)`, row.gap >= 4 && row.gap <= 10)
  check(`[${theme}] idle: attach button's right edge is still the button`, row.hit)
  check(`[${theme}] idle: fit probe is the live column's width (${idleProbe} vs ${row.column}px)`, Math.round(parseFloat(idleProbe)) === row.column)
  await page.screenshot({ path: resolve(OUT, `one-row-${theme}.png`), clip: await clipTo() })

  if (record) await page.waitForTimeout(900)
  // A short draft: still one row, and Send is live on it.
  await textarea.click()
  await textarea.pressSequentially(SHORT, { delay: record ? 22 : 0 })
  await page.waitForTimeout(record ? 700 : 400)
  check(`[${theme}] short draft: still the one-row shape`, await box.getAttribute('data-inline-toolbar') === 'true')
  check(`[${theme}] short draft: send button enabled on the single row`, await send.isEnabled())
  await page.screenshot({ path: resolve(OUT, `short-${theme}.png`), clip: await clipTo() })
  // Past the line: the toolbar moves under the text.
  await textarea.pressSequentially(REST, { delay: record ? 22 : 0 })
  await page.waitForTimeout(record ? 900 : 600)
  check(`[${theme}] typed: box left the one-row shape`, (await box.getAttribute('data-inline-toolbar')) === null)
  check(`[${theme}] typed: send button is below the text`, !(await sameLine()))
  check(`[${theme}] typed: send button enabled`, await send.isEnabled())
  check(`[${theme}] typed: still no prompt-optimizer button`, await optimize.count() === 0)
  const typedH = (await box.boundingBox()).height
  check(`[${theme}] typed box is taller than the idle box (${idleH} -> ${typedH})`, typedH > idleH)
  check(`[${theme}] typed: dock is back to the 16px corner`, await radiusOf() === '16px')
  check(`[${theme}] typed: glass rim width unchanged from idle`, (await bevelOf()) === idleBevel)
  check(`[${theme}] typed: fit probe column is the same width as on the single row (${idleProbe})`, idleProbe !== '' && (await probeWidth()) === idleProbe)
  await page.screenshot({ path: resolve(OUT, `two-row-${theme}.png`), clip: await clipTo() })

  // Cut the draft back to the short line: one row again, text kept.
  for (let i = 0; i < REST.length; i++) await textarea.press('Backspace', { delay: record ? 12 : 0 })
  await page.waitForTimeout(record ? 1200 : 600)
  check(`[${theme}] cut short: back to the one-row shape`, await box.getAttribute('data-inline-toolbar') === 'true')
  check(`[${theme}] cut short: send sits on the text's line again`, await sameLine())
  check(`[${theme}] cut short: the short draft is kept`, (await textarea.inputValue()) === SHORT)
  check(`[${theme}] no page errors`, errors.length === 0)
  if (errors.length) console.log(errors.join('\n'))
  if (record) {
    // The gif crop: the dock in its TALLER (typed) shape plus the bubble above
    // it, measured here rather than guessed, so the frame holds the whole box.
    const d = await dock.boundingBox()
    const crop = { x: Math.max(0, Math.round(d.x) - 24), y: Math.max(0, Math.round(d.y) - 150), w: Math.round(d.width) + 48, h: Math.round(d.height) + 56 + 150 }
    writeFileSync(resolve(OUT, 'gif-crop.json'), JSON.stringify(crop))
    const clip = await page.video()?.path(); console.log(`video: ${clip}`); console.log(`gif-crop: ${JSON.stringify(crop)}`)
  }
  await ctx.close()
}
await browser.close()
await server.close()
if (state.failures) { console.error(`${state.failures} check(s) failed`); process.exit(1) }
console.log(`wrote ${OUT}`)

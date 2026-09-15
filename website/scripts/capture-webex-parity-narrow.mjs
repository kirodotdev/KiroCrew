/**
 * Narrow-viewport evidence for the Webex settings this branch adds.
 *
 * The controls are a toggle, a tag-list editor and two numeric inputs — the
 * shapes that break first on a phone, because the tag editor puts an input and
 * an "Add" button on one row and the toggle puts a label and a switch on one
 * row. Both widths the repo requires are captured (390px and 320px), because 320
 * is where a two-column row stops fitting at all and 390 is the common case.
 *
 * Frames for the reveal are captured too: the interesting behaviour is that
 * turning the switch on REVEALS the space allow-list, and a still cannot show a
 * reveal. They are assembled into a GIF by whatever encoder is available, and
 * left as PNGs when none is — so this never fails the capture.
 *
 * Scene and route table come from lib/webex-scene.mjs, shared with the desktop
 * harness. Usage:
 *   npm run build && npx vite preview --port 6814 &
 *   node scripts/capture-webex-parity-narrow.mjs http://127.0.0.1:6814 \
 *     ../temp-screenshots/webex-parity
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import { scene, openWebexPanel } from './lib/webex-scene.mjs'

const BASE = process.argv[2] || 'http://127.0.0.1:6814'
const OUT = process.argv[3] || '../temp-screenshots/webex-parity'

mkdirSync(OUT, { recursive: true })
mkdirSync(`${OUT}/frames`, { recursive: true })

const SPACE_ID = 'Y2lzY29zcGFyazovL3VzL1JPT00vZXhhbXBsZQ'
const ANCHOR = 'Answer in group spaces'

/** A phone-shaped context at *width*. */
const phone = (browser, width) =>
  browser.newContext({
    viewport: { width, height: 900 },
    deviceScaleFactor: 2,
    isMobile: true,
    hasTouch: true,
  })

/** Scroll the group-spaces control into view and settle. */
async function focusControl(page) {
  const anchor = page.getByText(ANCHOR).first()
  await anchor.waitFor({ timeout: 10000 })
  await anchor.scrollIntoViewIfNeeded()
  await page.waitForTimeout(500)
  return anchor
}

async function main() {
  const browser = await chromium.launch()

  // ── Stills at both required widths, on and off ──
  for (const width of [390, 320]) {
    for (const on of [false, true]) {
      scene.allowGroupRooms = on
      scene.rooms = on ? [SPACE_ID] : []
      const context = await phone(browser, width)
      const page = await openWebexPanel(context, BASE)
      await focusControl(page)
      const name = `narrow-${width}-group-spaces-${on ? 'on' : 'off'}`
      await page.screenshot({ path: `${OUT}/${name}.png` })
      console.log('wrote', `${OUT}/${name}.png`)
      await context.close()
    }
  }

  // ── Frames for the reveal, at the common phone width ──
  scene.allowGroupRooms = false
  scene.rooms = []
  const context = await phone(browser, 390)
  const page = await openWebexPanel(context, BASE)
  const anchor = await focusControl(page)

  let frame = 0
  const grab = async () => {
    await page.screenshot({ path: `${OUT}/frames/f${String(frame).padStart(3, '0')}.png` })
    frame += 1
  }

  // The switch nearest the anchor is the group-spaces one; picking by position
  // rather than by index keeps this working when a control is added above it.
  const anchorBox = await anchor.boundingBox()
  let target = null
  let bestGap = Infinity
  for (const candidate of await page.getByRole('switch').all()) {
    const box = await candidate.boundingBox()
    if (!box) continue
    const gap = Math.abs(box.y - anchorBox.y)
    if (gap < bestGap) {
      bestGap = gap
      target = candidate
    }
  }

  // Hold on the default, flip, then hold on the revealed field, so the animation
  // reads as a deliberate action rather than a cut.
  for (let i = 0; i < 6; i += 1) await grab()
  if (target) {
    await target.click()
    for (let i = 0; i < 3; i += 1) {
      await page.waitForTimeout(120)
      await grab()
    }
    await page.waitForTimeout(400)
    for (let i = 0; i < 8; i += 1) await grab()
  } else {
    console.log('note: group-spaces switch not located; frames show the static state')
  }
  await context.close()
  await browser.close()

  // ── Assemble the GIF with whatever encoder is present ──
  const gif = `${OUT}/narrow-390-group-spaces-reveal.gif`
  const ff = spawnSync('ffmpeg', [
    '-y', '-framerate', '6',
    '-i', `${OUT}/frames/f%03d.png`,
    '-vf', 'scale=390:-1:flags=lanczos,split[a][b];[a]palettegen[p];[b][p]paletteuse',
    gif,
  ], { encoding: 'utf-8' })
  if (ff.status === 0) {
    console.log('wrote', gif)
  } else {
    console.log('note: ffmpeg unavailable; frames left in', `${OUT}/frames`)
  }
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})

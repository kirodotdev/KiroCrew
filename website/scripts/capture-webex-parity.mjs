/**
 * Screenshot harness for the Webex settings this branch adds.
 *
 * Four states a reviewer needs to see, because the copy is what carries the
 * security reasoning and a string assertion cannot show whether it fits:
 *
 *   1. group spaces OFF — the default, and the state the channel ships in
 *   2. group spaces ON with an empty allow-list — the fail-closed case, where the
 *      helper text has to say that the switch alone grants nothing AND an amber
 *      warning has to name the fix, because the switch is on and nothing answers
 *   3. group spaces ON with a space named
 *   4. the Behavior card — threading and both context thresholds, none of which
 *      had a control before this branch
 *
 * Runs the REAL built SPA (website/dist) with every /api/** call answered from
 * fixtures by Playwright. No gateway, no bot token, no data written: the client
 * code under test is unmodified and only the network and the localStorage seed
 * are stubbed. The scene and route table live in lib/webex-scene.mjs, shared
 * with the narrow-viewport harness.
 *
 * Usage:
 *   npm run build && npx vite preview --port 6814 &
 *   node scripts/capture-webex-parity.mjs http://127.0.0.1:6814 \
 *     ../temp-screenshots/webex-parity
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { scene, openWebexPanel } from './lib/webex-scene.mjs'

const BASE = process.argv[2] || 'http://127.0.0.1:6814'
const OUT = process.argv[3] || '../temp-screenshots/webex-parity'

mkdirSync(OUT, { recursive: true })

const SPACE_ID = 'Y2lzY29zcGFyazovL3VzL1JPT00vZXhhbXBsZQ'

async function main() {
  const browser = await chromium.launch()
  // Tall enough that a crop taken from the anchor downward still fits the amber
  // warning below the field: the clip is bounded by the viewport, so a short one
  // silently truncates the shot rather than failing.
  const VIEWPORT = { width: 1500, height: 1400 }
  const context = await browser.newContext({
    viewport: VIEWPORT,
    // The helper text under each control is ~11px; at 1x it renders too soft to
    // read, and reading it is the entire point of this capture.
    deviceScaleFactor: 2,
  })

  /** Crop from *anchorText* downward, so the label, its description and any
   *  field revealed beneath it are legible in one frame. The controls sit near
   *  the bottom of a long panel, so the box is off-screen until scrolled in and
   *  clipping to an off-screen box fails. */
  async function shot(page, name, anchorText, height = 460) {
    const anchor = page.getByText(anchorText).first()
    await anchor.waitFor({ timeout: 10000 })
    await anchor.scrollIntoViewIfNeeded()
    await page.waitForTimeout(600)
    const box = await anchor.boundingBox()
    const x = Math.max(0, box.x - 40)
    const y = Math.max(0, box.y - 110)
    await page.screenshot({
      path: `${OUT}/${name}.png`,
      clip: {
        x, y,
        width: Math.min(VIEWPORT.width - x, 1040),
        height: Math.min(VIEWPORT.height - y, height),
      },
    })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  // The empty-allow-list shot needs the taller crop: the amber "add the bot to
  // the space, then paste its ID" warning renders BELOW the input, and it is the
  // whole point of that state.
  const shots = [
    ['group-spaces-off-by-default', false, [], 460],
    ['group-spaces-on-empty-allowlist', true, [], 620],
    ['group-spaces-on-with-a-space', true, [SPACE_ID], 620],
  ]
  let page = null
  for (const [name, on, rooms, height] of shots) {
    scene.allowGroupRooms = on
    scene.rooms = rooms
    if (page) await page.close()
    page = await openWebexPanel(context, BASE)
    await shot(page, name, 'Answer in group spaces', height)
  }

  // The Behavior card, from the same page as the last shot.
  await shot(page, 'behavior-threading-and-thresholds', 'Reply in thread', 520)

  await browser.close()
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})

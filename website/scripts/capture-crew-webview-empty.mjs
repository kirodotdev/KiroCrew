/** Real-browser evidence for the crew webview EMPTY state.
 *
 * Drives the ISOLATED capture entry (website/capture/crew-webview-empty.html),
 * which mounts the REAL `CrewWebview` over a fetch boundary that answers the
 * member-panel read with `{ html: null }` -- the component's own `!html` empty
 * branch. The shot therefore shows the shipped `webview_empty` string, both
 * sentences, in the real drawer-width frame.
 *
 * The assertion the shot cannot fake: the SECOND sentence -- the setup
 * instruction "Add its panel server under Tools & MCP..." -- is present in the
 * rendered text, which is the sentence a prior screenshot missed.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6824 --strictPort    # in another shell (website/)
 *   node scripts/capture-crew-webview-empty.mjs http://127.0.0.1:6824 ../temp-screenshots/crew-webview
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6824'
const OUT = process.argv[3] || '../temp-screenshots/crew-webview'
mkdirSync(OUT, { recursive: true })

/** Drawer width of the crew detail panel, plus room for the empty-state copy. */
const VIEWPORT = { width: 360, height: 480 }

// mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, which is
// older than the system Mesa needs; children inherit it, so scrub it here.
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
const check = (label, ok) => {
  console.log(`${label} => ${ok ? 'OK' : 'FAIL'}`)
  if (!ok) failures++
}

const page = await browser.newPage({ viewport: VIEWPORT, deviceScaleFactor: 2 })
page.on('pageerror', e => { console.error('pageerror:', e.message); failures++ })
await page.goto(`${BASE}/capture/crew-webview-empty.html?theme=dark`, { waitUntil: 'networkidle' })

const empty = page.locator('[data-testid="crew-webview-empty"]')
await empty.waitFor({ state: 'visible', timeout: 15000 })
const text = (await empty.textContent()) || ''

// Both sentences must be present -- the first (state) and the second (the setup
// instruction a prior shot missed).
check('empty-state shows the state sentence', text.includes('has not published a dashboard yet'))
// The control names the OUTCOME in the reader's words, not the mechanism, and
// does not promise the click finishes the job. Three UX rounds bracketed this: a
// "Turn on dashboard publishing" label was blocked because the click only
// navigates, "Add the panel server in the crew manager" was blocked because
// "panel server" is jargon a reader would not click, and "Set up this member's
// dashboard" read as possibly costly or irreversible with no hint of where the
// click lands. "Open Edit member to set it up" makes the click legibly a
// navigation and names the destination by the title the members page gives it.
check('empty-state names the destination on the control', text.includes('Open Edit member to set it up'))
// The control is the half UX review could not see: the fixture used to omit
// `onSetUp`, so the camera photographed a state with nothing clickable while the
// product shipped one. Asserted by its test id rather than its label, so a copy
// edit does not silently turn this back into a shot of prose alone.
check(
  'empty-state offers the setup control',
  (await page.locator('[data-testid="crew-webview-setup"]').count()) === 1,
)

await page.waitForTimeout(150)
await page.screenshot({ path: `${OUT}/03-empty-state.png` })

await page.close()
await browser.close()
if (failures) {
  console.error(`${failures} assertion(s) failed`)
  process.exit(1)
}
console.log(`done - evidence in ${OUT}/03-empty-state.png`)

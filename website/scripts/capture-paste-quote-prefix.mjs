/**
 * Screenshot harness for the QUOTE-PREFIX PASTE FLOW in the composer.
 *
 * Runs the REAL built SPA (website/dist) behind the shared `serveDist` server
 * and answers every /api/** call from fixtures through the shared
 * `stubDashboardApi` helper. No gateway, no dashboard auth, no kiro-cli.
 *
 * The scene: the user types a markdown quote prefix (`> `) into the composer
 * and pastes a large block of text. The paste collapses into a
 * `[ Paste #N · M lines ]` chip:
 *   - `before` (pre-fix build): the chip is forced onto its own line, so the
 *     `> ` the user just typed is stranded on the line above it and they must
 *     delete the injected newline to make the quote read right.
 *   - `after` (fixed build): the chip flows on the quote line —
 *     `> [ Paste #1 · 12 lines ]`.
 *
 * The paste is dispatched as a real ClipboardEvent carrying a DataTransfer, so
 * it exercises the same onPaste handler as a user paste. The harness asserts
 * the composer's literal value for its phase and exits non-zero on a mismatch,
 * so the pixels cannot silently photograph the wrong state.
 *
 * To photograph the pre-fix layout, check the component out at a ref that
 * predates the fix (`git checkout origin/main -- src/components/ChatInput.tsx`),
 * `npm run build`, and run with the `before` phase; then restore and rebuild.
 *
 * Usage: node scripts/capture-paste-quote-prefix.mjs <outDir> <before|after>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/paste-quote-prefix-shots'
const PHASE = process.argv[3] === 'before' ? 'before' : 'after'

mkdirSync(OUT, { recursive: true })

const SLOT = 'chat-1'
const BIG_PASTE = Array.from({ length: 12 }, (_, i) => `payload line ${i}`).join('\n')

const EXPECTED = {
  before: '> \n[ Paste #1 · 12 lines ]',
  after: '> [ Paste #1 · 12 lines ]',
}

const { srv, base } = await serveDist()
// A mise-managed node exports its own lib/node on LD_LIBRARY_PATH, which the
// bundled chromium-headless-shell then loads as libstdc++ — too old for the
// system's mesa/LLVM. Point the child at the system libs instead.
const browser = await chromium.launch({ env: { ...process.env, LD_LIBRARY_PATH: '/usr/lib64' } })
const context = await browser.newContext({ viewport: { width: 1500, height: 950 }, deviceScaleFactor: 2 })
const page = await context.newPage()

await stubDashboardApi(page, {
  slots: [{ key: SLOT, messages: 0, running: false, agent: 'kirocrew', mode: '' }],
  // Pin the locale: without it the SPA picks one from the environment and the
  // shot comes out in whatever language the runner happens to negotiate.
  localStorageEntries: { 'mc-active-slot': SLOT, 'mc-lang': 'en' },
})

await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(2500)

const composer = page.getByLabel('Message input').first()
await composer.waitFor({ state: 'visible', timeout: 10000 })
await composer.click()
await page.keyboard.type('> ')

// Dispatch the paste as the browser would: a ClipboardEvent whose DataTransfer
// carries text/plain, bubbling so React's delegated onPaste receives it.
await composer.evaluate((el, text) => {
  const dt = new DataTransfer()
  dt.setData('text/plain', text)
  el.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true }))
}, BIG_PASTE)
await page.waitForTimeout(800)

// Crop to the composer band so the chip and the quote prefix fill the frame.
const box = await page.getByTestId('input-wrapper').first().boundingBox()
const out = join(OUT, `${PHASE}.png`)
await page.screenshot({
  path: out,
  clip: box
    ? { x: Math.max(0, box.x - 12), y: Math.max(0, box.y - 48), width: box.width + 24, height: box.height + 72 }
    : undefined,
})
console.log('wrote', out)

// Falsifiability: assert the composer holds this phase's literal value, so the
// harness fails loudly instead of photographing the wrong build.
const composerText = await composer.inputValue()
console.log(JSON.stringify({ phase: PHASE, composerText }))

await browser.close()
srv.close()

if (composerText !== EXPECTED[PHASE]) {
  console.error(`FAIL: expected ${JSON.stringify(EXPECTED[PHASE])}, got ${JSON.stringify(composerText)}`)
  process.exit(1)
}
console.log('OK')

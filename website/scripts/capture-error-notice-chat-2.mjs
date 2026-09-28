/**
 * Screenshot runner for capture/error-notice-chat-2.html (chat error-state
 * sweep, batch chat-2).
 *
 * From website/:
 *   npx vite --host 127.0.0.1 --port 6832 --strictPort
 *   node scripts/capture-error-notice-chat-2.mjs http://127.0.0.1:6832 <outdir> [before|after]
 *
 * The runner presses the wave chip's per-row Stop so the stubbed (rejecting)
 * `spawnDelete` lands: on the base tree that refusal only ever reached
 * `console.warn`, on this branch it renders a notice. `after` (default)
 * asserts every scene renders at least one "Ask the agent" hand-off and that
 * the legacy `text-red-500` line is gone; `before`, run against the base
 * branch with the same harness, asserts the opposite — so a frame can never
 * photograph the wrong tree.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6832'
const OUT = process.argv[3] || '../temp-screenshots/error-notice-chat-2'
const MODE = process.argv[4] === 'before' ? 'before' : 'after'
mkdirSync(OUT, { recursive: true })

const THEMES = MODE === 'after' ? ['dark', 'light'] : ['dark']
const browser = await chromium.launch()
let failed = 0

for (const theme of THEMES) {
  const ctx = await browser.newContext({
    viewport: { width: 780, height: 760 },
    deviceScaleFactor: 2,
    colorScheme: theme,
  })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))
  try {
    await page.goto(`${BASE}/capture/error-notice-chat-2.html?theme=${theme}`, { waitUntil: 'networkidle' })
    await page.addStyleTag({
      content: '*, *::before, *::after { animation-duration: 0s !important;'
        + ' animation-delay: 0s !important; transition-duration: 0s !important;'
        + ' transition-delay: 0s !important; }',
    })
    await page.waitForSelector('[data-capture-root]')
    // Settled error state: the summary read must have rejected.
    await page.getByText('Could not load the summary').first().waitFor({ timeout: 10000 })
    // Press the first per-row Stop; the stubbed spawnDelete rejects.
    await page.getByRole('button', { name: /^Stop subagent/ }).first().click()
    // Give the rejection a tick to land either way (the BEFORE tree renders nothing).
    await page.waitForTimeout(300)
    const handoffs = await page.getByRole('button', { name: 'Ask the agent' }).count()
    const scenes = await page.locator('[data-scene]').count()
    if (MODE === 'after') {
      await page.getByTestId('subagent-action-error').waitFor({ timeout: 5000 })
      if (handoffs < scenes) throw new Error(`expected a hand-off per scene (${scenes}), got ${handoffs}`)
      if (await page.locator('[data-scene] .text-red-500').count()) {
        throw new Error('legacy hand-written error markup still present')
      }
    } else {
      if (handoffs !== 0) throw new Error(`BEFORE frame unexpectedly renders ${handoffs} hand-off link(s)`)
      if (await page.getByTestId('subagent-action-error').count()) {
        throw new Error('BEFORE frame unexpectedly renders the stop-refused notice')
      }
    }
    if (errors.length) throw new Error(`page errors: ${errors.join(' | ')}`)
    await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${MODE}-${theme}.png` })
    console.log(`${MODE}-${theme}: ${scenes} scenes, ${handoffs} hand-off link(s) — OK`)
  } catch (e) {
    console.error(`${MODE}-${theme}: FAILED — ${e}`)
    failed++
  } finally {
    await ctx.close()
  }
}

await browser.close()
process.exit(failed ? 1 : 0)

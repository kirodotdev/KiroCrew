/**
 * Screenshot + assertion runner for capture/workflow-run-tree-agent-timing.html.
 *
 * Two shells, from website/:
 *   npx vite --host 127.0.0.1 --port 6817 --strictPort
 *   node scripts/capture-workflow-run-tree-agent-timing.mjs http://127.0.0.1:6817 \
 *     ../temp-screenshots/workflow-run-tree-agent-timing
 *
 * The frames are evidence, but the ASSERTIONS are the point: `after` must show
 * one reading per FINISHED agent and none on the running one, and `before` must
 * show no reading at all. A run that photographs the wrong state exits nonzero
 * rather than emitting a misleading image.
 *
 * 620px viewport at deviceScaleFactor 2 keeps each frame well inside 2000px per
 * edge.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6817'
const OUT = process.argv[3] || '../temp-screenshots/workflow-run-tree-agent-timing'

/** The four finished agents' readings, in the fixture's order. */
const EXPECTED = ['4.2s', '37s', '6m 38s', '2m 0s']

mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
let failed = 0

for (const theme of ['dark', 'light']) {
  for (const scene of ['before', 'after']) {
    const ctx = await browser.newContext({
      viewport: { width: 620, height: 760 },
      deviceScaleFactor: 2,
      colorScheme: theme,
    })
    const page = await ctx.newPage()
    const errors = []
    page.on('pageerror', e => errors.push(String(e)))

    const name = `${theme}-${scene}.png`
    try {
      // 'load' rather than 'networkidle': a Vite dev server's HMR socket never
      // goes network-idle, so that wait state is the flaky one. The selector
      // wait below is the real readiness signal.
      await page.goto(`${BASE}/capture/workflow-run-tree-agent-timing.html?scene=${scene}&theme=${theme}`, {
        waitUntil: 'load',
      })
      const root = page.locator('[data-capture-root]')
      await root.waitFor({ timeout: 15000 })
      // The phase rows mount expanded; the only motion is the running agent's
      // spinner, which is deliberately left turning rather than frozen.
      await page.waitForTimeout(250)

      const text = (await root.innerText()).replace(/\s+/g, ' ')
      const found = EXPECTED.filter(r => text.includes(r))

      if (scene === 'after' && found.length !== EXPECTED.length) {
        console.error(`FAIL ${name}: expected every reading ${EXPECTED.join(', ')}; found ${found.join(', ') || 'none'}`)
        failed++
      }
      if (scene === 'before' && found.length !== 0) {
        console.error(`FAIL ${name}: the pre-change scene must show no reading; found ${found.join(', ')}`)
        failed++
      }
      // The running agent must never carry a reading, in either scene: its
      // spinner already says it is running, and a 0s would read as a
      // measurement of a step that has not finished.
      const running = page.locator('[data-capture-root] li', { hasText: 'verify:mutations' })
      const runningText = (await running.innerText()).replace(/\s+/g, ' ')
      if (/\d+(\.\d+)?\s*[sm]\b/.test(runningText)) {
        console.error(`FAIL ${name}: the running agent shows a time: ${runningText}`)
        failed++
      }

      await root.screenshot({ path: `${OUT}/${name}` })
      console.log(`${name}: readings ${found.join(', ') || 'none'}`)
    } catch (e) {
      console.error(`FAIL ${name}: ${e}`)
      failed++
    }
    if (errors.length) {
      console.error(`FAIL ${name}: page errors ${errors.join(' | ')}`)
      failed++
    }
    await ctx.close()
  }
}

await browser.close()
if (failed) {
  console.error(`${failed} assertion(s) failed; frames in ${OUT} are not trustworthy evidence`)
  process.exit(1)
}
console.log(`frames in ${OUT}`)

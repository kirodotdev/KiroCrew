/**
 * Screenshots of the workflow-library change cards from the isolated capture entry
 * website/capture/workflow-save-card.html: a `workflow.save` card (name,
 * description, command and the whole script), a `workflow.update` card (revision
 * step and the script diff, then its Details opened on the whole new script) and
 * an applied save, each light and dark.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6815 --strictPort   # in another shell
 *   node scripts/capture-workflow-save-card.mjs http://127.0.0.1:6815 ../temp-screenshots/workflow-save-card
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6815'
const OUT = process.argv[3] || '../temp-screenshots/workflow-save-card'
mkdirSync(OUT, { recursive: true })

const SCENES = ['save', 'update', 'applied']
const THEMES = ['dark', 'light']
/** The code blocks each pending scene must show: the script, or the diff. */
const CODE_BLOCKS = { save: 1, update: 1, applied: 0 }

// mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, which is
// older than the system Mesa needs; children inherit it, so scrub it here.
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
const shot = async (locator, name) => {
  const path = `${OUT}/${name}.png`
  await locator.screenshot({ path })
  return path
}
for (const scene of SCENES) {
  const frames = {}
  for (const theme of THEMES) {
    const page = await browser.newPage({ viewport: { width: 760, height: 900 }, deviceScaleFactor: 2 })
    page.on('pageerror', e => { console.error(`  pageerror: ${e.message}`); failures++ })
    await page.goto(`${BASE}/capture/workflow-save-card.html?scene=${scene}&theme=${theme}`, { waitUntil: 'networkidle' })
    const root = page.locator('[data-capture-root]')
    await page.locator('[data-testid="change-card"]').waitFor()
    await page.waitForTimeout(400)
    const blocks = await page.locator('[data-testid="change-card-code"]:visible').count()
    if (blocks !== CODE_BLOCKS[scene]) {
      console.error(`FAIL: scene=${scene} theme=${theme} shows ${blocks} code block(s), want ${CODE_BLOCKS[scene]}`); failures++
    }
    frames[theme] = await shot(root, `${scene}-${theme}`)
    if (scene === 'update') {
      await page.locator('[data-testid="change-card-details"] summary').click()
      await page.waitForTimeout(300)
      const opened = await page.locator('[data-testid="change-card-code"]:visible').count()
      if (opened !== 2) { console.error(`FAIL: update details shows ${opened} code block(s), want 2`); failures++ }
      await shot(root, `update-details-${theme}`)
    }
    console.log(scene, theme, JSON.stringify((await root.textContent())?.slice(0, 200)))
    await page.close()
  }
  if (readFileSync(frames.dark).equals(readFileSync(frames.light))) {
    console.error(`FAIL: scene=${scene} light and dark frames are identical`); failures++
  }
}
await browser.close()
console.log(`wrote ${OUT}`)
process.exit(failures ? 1 : 0)

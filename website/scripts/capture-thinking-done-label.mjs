/**
 * Screenshots of the collapsed reasoning row's tense-aware header label.
 *
 * Drives the isolated capture entry (website/capture/thinking-live-line.html)
 * in two locales: while chunks arrive the header must read the in-progress
 * form ("Thinking" / "思考中"), and once they stop it must settle to the
 * finished form ("Thought process" / "思考过程"). zh-CN is captured because it
 * is the locale whose in-progress translation exposed the defect.
 *
 * The run is SELF-CHECKING: each frame is taken only after the header label
 * TEXT matches the expected form for that scene and locale, so it can never
 * quietly emit a screenshot of the state it is meant to prove.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6812 --strictPort   # in another shell
 *   node scripts/capture-thinking-done-label.mjs http://127.0.0.1:6812 ../temp-screenshots/thinking-done-label
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6812'
const OUT = process.argv[3] || '../temp-screenshots/thinking-done-label'
mkdirSync(OUT, { recursive: true })

const LABELS = {
  en: { live: 'Thinking', settled: 'Thought process' },
  'zh-CN': { live: '思考中', settled: '思考过程' },
}

/** `streaming` keeps chunks flowing so the frame is inside the live window;
 *  `settled` never appends, which is the state a finished block mounts in. */
const SCENES = [
  { scene: 'long', name: 'streaming', chunks: 1000, state: 'live' },
  { scene: 'settled', name: 'settled', chunks: 0, state: 'settled' },
]

const run = async () => {
  const browser = await chromium.launch()
  let failed = 0
  for (const lang of Object.keys(LABELS)) {
    for (const theme of ['dark', 'light']) {
      for (const { scene, name, chunks, state } of SCENES) {
        const ctx = await browser.newContext({ viewport: { width: 720, height: 140 }, deviceScaleFactor: 2 })
        const page = await ctx.newPage()
        await page.goto(`${BASE}/capture/thinking-live-line.html?scene=${scene}&theme=${theme}&chunks=${chunks}&lang=${lang}`)
        await page.waitForSelector('button[aria-expanded]')
        const want = LABELS[lang][state]
        const wrong = state === 'live' ? LABELS[lang].settled : LABELS[lang].live
        let ok = false
        try {
          // The frame is taken only once the header carries the expected form
          // and not the scene's opposite one (no form contains its opposite,
          // in either locale, so a plain contains/not-contains pair is exact).
          await page.waitForFunction(([w, x]) => {
            const t = document.querySelector('button[aria-expanded]')?.textContent || ''
            return t.includes(w) && !t.includes(x)
          }, [want, wrong], { timeout: 8000 })
          ok = true
        } catch {
          failed += 1
          const got = await page.locator('button[aria-expanded]').textContent()
          console.error(`FAIL ${lang}/${theme}/${name}: want "${want}", header reads "${got}"`)
        }
        await page.screenshot({ path: `${OUT}/${name}-${lang}-${theme}.png` })
        console.log(`${ok ? 'ok  ' : 'FAIL'} ${name}-${lang}-${theme}.png — expect "${want}"`)
        await ctx.close()
      }
    }
  }
  await browser.close()
  if (failed) process.exit(1)
}

run().catch((e) => { console.error(e); process.exit(1) })

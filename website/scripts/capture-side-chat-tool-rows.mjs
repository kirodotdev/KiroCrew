/**
 * Screenshots of the side transcript drawing the side turn's tool calls.
 *
 * The `before` scene is the transcript the old wire could carry (text only).
 * The `after` scene carries the same turn with its tool rows; the script expands
 * the folded "2 tool calls" header and asserts both pills are on screen — the
 * refused one in the blocked tone — before writing the frame, so a frame can
 * never show the old text-only panel under the new name.
 *
 * Usage (same shape as capture-sidechat-composer.mjs):
 *   npx vite --host <loopback> --port 6842 --strictPort   # in another shell
 *   node scripts/capture-side-chat-tool-rows.mjs <vite base url> ../temp-screenshots/side-chat-tool-rows
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2]
const OUT = process.argv[3] || '../temp-screenshots/side-chat-tool-rows'
if (!BASE) throw new Error('usage: capture-side-chat-tool-rows.mjs <vite base url> [out dir]')
mkdirSync(OUT, { recursive: true })

const SCENES = [
  { name: 'before-dark', scene: 'before', theme: 'dark' },
  { name: 'after-dark', scene: 'after', theme: 'dark' },
  { name: 'before-light', scene: 'before', theme: 'light' },
  { name: 'after-light', scene: 'after', theme: 'light' },
]

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 520, height: 820 }, deviceScaleFactor: 2 })

for (const s of SCENES) {
  await page.goto(`${BASE}/capture/side-chat-tool-rows.html?scene=${s.scene}&theme=${s.theme}`)
  await page.addStyleTag({
    content: '*, *::before, *::after { animation-duration: 0s !important;'
      + ' animation-delay: 0s !important; transition-duration: 0s !important;'
      + ' transition-delay: 0s !important; }',
  })
  await page.waitForSelector('[data-capture-root]')
  await page.locator('[data-side-chat-input] textarea[data-composer-input]').waitFor({ timeout: 5000 })
  await page.getByText('Checking the store file first.').waitFor({ timeout: 5000 })
  if (s.scene === 'after') {
    await page.getByRole('button', { name: '2 tool calls' }).click()
    await page.getByRole('button', { name: 'read the cron store' }).waitFor({ timeout: 5000 })
    const refused = page.getByRole('button', { name: 'resume the paused jobs' })
    await refused.waitFor({ timeout: 5000 })
    const cls = await refused.getAttribute('class')
    if (!cls || !cls.includes('text-warn')) throw new Error(`${s.name}: refused pill is not drawn blocked (${cls})`)
    // The expand re-measures the virtualized rows a frame later; wait until the
    // answer sits below the last pill so no frame shows the two overlapping.
    await page.waitForFunction(() => {
      const pill = Array.from(document.querySelectorAll('button')).find(b => b.textContent?.includes('resume the paused jobs'))
      const answer = Array.from(document.querySelectorAll('p')).find(p => p.textContent?.startsWith('The store holds'))
      return !!pill && !!answer && answer.getBoundingClientRect().top >= pill.getBoundingClientRect().bottom
    }, null, { timeout: 5000 })
    console.log(`${s.name}: 2 tool pills on screen, refused one blocked`)
  } else {
    const count = await page.getByRole('button', { name: /tool call/ }).count()
    if (count !== 0) throw new Error(`${s.name}: before scene shows ${count} tool-call header(s)`)
    console.log(`${s.name}: text-only transcript`)
  }
  await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${s.name}.png` })
}

await browser.close()
console.log(`wrote ${SCENES.length} screenshots to ${OUT}`)

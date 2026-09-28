/**
 * Screenshot harness for Code Review Sage's publish-failure notice.
 *
 * Drives the scene at capture/sage-publish-failure-notice.html: arm a verdict,
 * confirm it, and let the stubbed submit refuse. The frame is the state this
 * change alters — the refusal now rendered by the shared `ErrorNotice`.
 *
 * Self-checking: the notice must be a `role="alert"`, carry the failure title
 * and the provider's words, and expose exactly one hand-off whose accessible
 * name is the scoped label. A screenshot of the wrong state is worse evidence
 * than none, so a missing element fails the run instead of saving a frame.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6841 --strictPort    # in another shell
 *   node scripts/capture-sage-publish-failure.mjs http://127.0.0.1:6841 ../temp-screenshots/sage-publish-failure
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

const BASE = process.argv[2] || 'http://127.0.0.1:6841'
const OUT = process.argv[3] || '../temp-screenshots/sage-publish-failure'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 620, height: 420 }, deviceScaleFactor: 2 })

for (const theme of ['dark', 'light']) {
  await page.goto(`${BASE}/capture/sage-publish-failure-notice.html?theme=${theme}`)
  const scene = page.getByTestId('scene')
  await scene.waitFor()

  // Two clicks: the verdict arms a confirmation that restates it, and only the
  // second sends — the same path a reader takes.
  await page.getByRole('button', { name: 'Request changes' }).click()
  await page.getByRole('button', { name: 'Request changes' }).click()

  const alert = page.getByRole('alert')
  await alert.waitFor()
  const text = await alert.innerText()
  for (const needle of ['Could not publish the review', 'pull request write scope']) {
    if (!text.includes(needle)) throw new Error(`notice is missing ${needle}: ${text}`)
  }
  const handoff = page.getByRole('button', { name: 'Ask the agent about this failure' })
  if (await handoff.count() !== 1) {
    throw new Error(`expected exactly one scoped hand-off, found ${await handoff.count()}`)
  }

  await scene.screenshot({ path: join(OUT, `publish-failure-${theme}.png`) })
  console.log(`wrote publish-failure-${theme}.png`)
}

await browser.close()

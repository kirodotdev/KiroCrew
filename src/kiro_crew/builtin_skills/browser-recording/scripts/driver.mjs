// Playwright recording driver for the browser-recording skill.
//
// Invoked by record_browser.py — do not run directly (the Python wrapper
// resolves Playwright, validates arguments, and owns ffmpeg conversion).
//
// Contract (all via a single JSON config file, argv[2]):
//   {
//     "url": "http://127.0.0.1:5173/...",   // page to open
//     "scenarioPath": "/abs/path/scenario.mjs" | null,
//     "outDir": "/abs/path",                 // recordVideo dir
//     "width": 1280, "height": 800,
//     "settleMs": 600,                       // wait after load before scenario
//     "tailMs": 400                          // wait after scenario before close
//   }
//
// The scenario module is authored per task by the agent:
//   export default async (page) => { ...clicks, waits, assertions... }
// It receives the live Playwright Page. Keep it deterministic: wait on
// selectors/state, not bare timeouts, wherever possible.
//
// Prints exactly one line to stdout on success: RECORDED <webm-path>
// Exits non-zero with a stderr message on failure.

import { createRequire } from 'node:module'
import { readFileSync } from 'node:fs'
import { pathToFileURL } from 'node:url'

const cfg = JSON.parse(readFileSync(process.argv[2], 'utf8'))

// Resolve playwright from the scenario's project first (its node_modules is
// where the browsers were installed), then from cwd.
const requireFrom = createRequire(
  (cfg.scenarioPath || process.cwd() + '/') // a file path anchors resolution
)
let chromium
try {
  ;({ chromium } = requireFrom('playwright'))
} catch {
  try {
    ;({ chromium } = createRequire(process.cwd() + '/')('playwright'))
  } catch {
    console.error(
      'playwright not resolvable from the project. Install it in the project ' +
        'first: npm i -D playwright && npx playwright install chromium'
    )
    process.exit(3)
  }
}

const browser = await chromium.launch()
try {
  const context = await browser.newContext({
    viewport: { width: cfg.width, height: cfg.height },
    recordVideo: { dir: cfg.outDir, size: { width: cfg.width, height: cfg.height } },
  })
  const page = await context.newPage()
  await page.goto(cfg.url, { waitUntil: 'load' })
  await page.waitForTimeout(cfg.settleMs)

  if (cfg.scenarioPath) {
    const mod = await import(pathToFileURL(cfg.scenarioPath).href)
    if (typeof mod.default !== 'function') {
      console.error('scenario module must default-export an async (page) => {} function')
      process.exit(4)
    }
    await mod.default(page)
  }

  await page.waitForTimeout(cfg.tailMs)
  const video = page.video()
  await page.close() // flush
  await context.close()
  const webm = await video.path()
  console.log(`RECORDED ${webm}`)
} finally {
  await browser.close()
}

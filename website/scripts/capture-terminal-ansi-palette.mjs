/**
 * Screenshot harness for the terminal ANSI palette (issue #7223).
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server, every /api/** call answered from fixtures via Playwright route
 * interception — no gateway, no PTY. The terminal WebSocket is a scripted PTY
 * that prints a fixed ANSI sample on connect: nothing is typed, because the
 * subject under test is how xterm COLOURS bytes it is handed, not any
 * interaction.
 *
 * The sample is the output a real shell produces, in the three shapes that make
 * the palette visible: a powerline-style prompt on ANSI background colours, a
 * `git diff` fragment on red/green, and a swatch row naming all sixteen entries.
 * The swatch row is the load-bearing frame: with the palette unset, xterm paints
 * it from its own built-in colours, which is exactly the mismatch reported.
 *
 * Captured (per theme, so the palette is shown TRACKING the theme rather than
 * being one hard-coded set):
 *   01-ansi-nord-dark.png      nord-dark: reds/greens/yellows/blues are the
 *                              theme's --danger / --ok / --warn / --info
 *   02-ansi-monokai-dark.png   monokai-dark: the same sample, different theme,
 *                              no code path difference
 *   03-ansi-nord-light.png     nord-light: ANSI black stays DARK on a light
 *                              theme (ordered by luminance, not taken from --bg)
 *                              so `\e[30m` output is still readable
 *
 * Run this script on the branch build for the "after" frames and on an
 * origin/main build for the "before" ones; the harness itself is theme- and
 * branch-agnostic.
 *
 * Usage: node scripts/capture-terminal-ansi-palette.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'
import { ansiSample } from './lib/terminal-ansi-sample.mjs'

const OUT = process.argv[2] || '../temp-screenshots/terminal-ansi-palette'
mkdirSync(OUT, { recursive: true })

/** Themes to capture: two dark palettes plus a light one for the polarity case.
 *  `color` is the family; the applied `data-theme` is `${color}-${mode}`. */
const SCENES = [
  { file: '01-ansi-nord-dark', mode: 'dark', color: 'nord' },
  { file: '02-ansi-monokai-dark', mode: 'dark', color: 'monokai' },
  { file: '03-ansi-nord-light', mode: 'light', color: 'nord' },
]

async function stubContext(context, scene) {
  // The shared stub takes a page in its own harnesses; a BrowserContext exposes
  // the same three methods it uses (route, routeWebSocket, addInitScript) and
  // this harness runs exactly one page per context, so the effect is identical.
  await stubDashboardApi(context, {
    theme: scene.mode,
    localStorageEntries: {
      // The default color theme is kiro-dark, a different palette from `dark`:
      // set it explicitly or the frame shows the wrong theme's colours.
      'mc-color-theme': scene.color,
      'mc-bottom-terminal': JSON.stringify({
        open: true, height: 360,
        tabs: [{ id: 'fixture-tab-1' }], activeId: 'fixture-tab-1',
      }),
    },
    extra: async (path, route) => {
      if (path === '/api/chat/slots' && route.request().method() === 'POST') {
        await json(route, { key: 'fixture-chat', title: 'New Session…', agent: 'kirocrew' })
        return true
      }
      // The boot answer is the server-side source of truth and overwrites the
      // localStorage seed, so the named theme goes in its `color` field: seeding
      // localStorage alone lands on the kiro-dark default.
      if (path === '/api/theme/boot') {
        await json(route, { mode: scene.mode, color: scene.color })
        return true
      }
      if (path.startsWith('/api/chat/slots/')) {
        await json(route, {})
        return true
      }
      return false
    },
  })

  // AFTER the shared stub, which routes `/api/ws` itself: the terminal socket
  // matches that pattern too, and the LAST matching route registered wins.
  // Scripted PTY: ready, then the ANSI sample. No echo — nothing is typed.
  await context.routeWebSocket(/\/api\/ws\/terminal\//, ws => {
    ws.send(JSON.stringify({ type: 'ready' }))
    ws.send(Buffer.from(ansiSample()))
  })
}

/** Crop to the terminal panel: the rows of coloured output are the subject. */
async function shotTerminal(page, name) {
  const rows = await page.locator('.xterm-rows').first().boundingBox()
  if (!rows) throw new Error('xterm rows not laid out')
  await page.screenshot({
    path: `${OUT}/${name}.png`,
    clip: { x: rows.x - 6, y: rows.y - 6, width: rows.width + 12, height: rows.height + 12 },
  })
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  try {
    for (const scene of SCENES) {
      const context = await browser.newContext({
        // Wide enough that the sixteen-swatch rows fit on one line each: a
        // wrapped row reads as a missing colour.
        viewport: { width: 1240, height: 820 }, deviceScaleFactor: 2,
      })
      await stubContext(context, scene)
      const page = await context.newPage()
      logPageProblems(page)
      await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
      await page.locator('.xterm-rows').first().waitFor({ state: 'visible', timeout: 20000 })
      await page.waitForTimeout(1500) // scripted PTY output paints

      // A wrong-theme frame must fail loudly rather than ship: the palette is
      // only evidence if the theme it came from is the one named in the file.
      const applied = await page.evaluate(() => document.documentElement.dataset.theme)
      const wanted = `${scene.color}-${scene.mode}`
      if (applied !== wanted) {
        throw new Error(`theme mismatch: wanted ${wanted}, document has ${applied}`)
      }
      await shotTerminal(page, scene.file)
      await context.close()
    }
  } finally {
    await browser.close()
    srv.close()
  }
}

await main()

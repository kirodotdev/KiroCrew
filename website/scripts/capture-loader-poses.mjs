/**
 * Screenshot harness for the CHAT LOADING CAROUSEL's artwork under several themes.
 *
 * The 4-slot carousel (`.csb4`) is theme-owned: a theme may replace the whole
 * loader (`themeBranding.loader`) or just the artwork it cycles (`loaderIcons`),
 * and anything unregistered falls back to `ChatFooter`'s `DEFAULT_ICONS`. That
 * fallback is what this photographs, because it is invisible to the unit tests in
 * a meaningful way: they assert WHICH component type renders, not that the art
 * reads correctly against a palette it was not drawn for.
 *
 * The pose assets carry a baked black vector stroke rather than a CSS filter, so
 * the same art is supposed to read on every palette — a dark surface hides the
 * stroke, a pale one needs it. Shooting one dark and one light non-Kiro theme is
 * what makes that claim checkable instead of asserted.
 *
 * Nothing in CI runs this file; it is a manual capture for PR evidence. The
 * CI-enforced half lives in src/test/ChatFooter.test.tsx.
 *
 * Usage: node scripts/capture-loader-poses.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/loader-poses'
const SLOT = 'chat-loader'

mkdirSync(OUT, { recursive: true })

/**
 * `running: true` with a non-streaming last role is exactly the state the loader
 * owns: the turn is live, but no text is arriving, so MarkdownRenderer is not
 * already drawing its own caret and ChatFooter renders the carousel.
 */
const slots = [{
  key: SLOT,
  title: 'Why is the loader theme-gated?',
  running: true,
  last_message: 'Reading ChatFooter.tsx…',
  messages: 1,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: '/home/user/workspace/kirocrew',
  folder_id: '',
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: true,
  has_more: false,
  total: 1,
  queue: [],
  project: '/home/user/workspace/kirocrew',
  messages: [
    {
      role: 'user',
      ts: Date.now() / 1000 - 20,
      content: 'Which themes show the mascot in the loading indicator?',
    },
  ],
}

/** One shot per palette: two non-Kiro themes (dark + light) plus Kiro itself. */
const CASES = [
  { name: 'emerald-dark', colorTheme: 'emerald', mode: 'dark' },
  { name: 'solarized-light', colorTheme: 'solarized', mode: 'light' },
  { name: 'kiro-dark', colorTheme: 'kiro', mode: 'dark' },
]

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1100, height: 700 },
    // The poses render ~18px tall; 1x is too soft to judge the stroke on GitHub.
    deviceScaleFactor: 2,
  })

  try {
    for (const c of CASES) {
      const page = await context.newPage()
      logPageProblems(page)
      await stubDashboardApi(page, {
        slots,
        theme: c.mode,
        localStorageEntries: {
          'mc-color-theme': c.colorTheme,
          'mc-active-slot': SLOT,
        },
        extra: async (path, route) => {
          // The trailing slash matters: the shared stub owns the bare
          // /api/chat/slots LIST, and only the per-slot detail belongs here.
          if (path.startsWith('/api/chat/slots/')) {
            await route.fulfill({
              status: 200,
              contentType: 'application/json',
              body: JSON.stringify(detail),
            })
            return true
          }
          return false
        },
      })

      await page.goto(`${base}/`, { waitUntil: 'domcontentloaded' })
      const footer = page.getByTestId('chat-footer')
      await footer.waitFor({ state: 'visible', timeout: 20_000 })
      // Sample the one window where all FOUR slots are simultaneously visible.
      // Layer B (csb-b) is opaque from 45% to 82% of the 2800ms cycle, and slots
      // are staggered up to .75s, so every slot is inside that window only for
      // t ∈ [2.01s, 2.30s] after mount — 1.2s, the obvious guess, lands in the
      // crossover trough and photographs three ghosts and a hole.
      await page.waitForTimeout(2150)
      await footer.screenshot({ path: `${OUT}/${c.name}.png` })
      // Whole-window context: the indicator has to read in place, not only cropped.
      await page.screenshot({ path: `${OUT}/${c.name}-full.png` })
      console.log(`${c.name}: ${await page.locator('.csb4 img.kp').count()} pose img(s), `
        + `${await page.locator('.csb4 svg').count()} inline svg(s)`)
      await page.close()
    }
  } finally {
    await browser.close()
    srv.close()
  }
  console.log(`wrote ${CASES.length * 2} shots to ${OUT}`)
}

main()

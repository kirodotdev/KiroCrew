/**
 * Screenshot harness for editorial spotlights.
 *
 * Runs the REAL built SPA (website/dist) behind the shared static server and
 * answers every /api/** call from fixtures via Playwright route interception --
 * gateway-free, no kiro-cli, no token.
 *
 * WHAT NEEDS PROVING, and why one frame cannot. Three separate claims:
 *   1. With no published sections, Discover looks exactly as it did -- this
 *      ships as a no-op, which is today's live state (`sections: []`).
 *   2. A published spotlight replaces the derived one and carries CURATOR
 *      artwork instead of the app's own hero image. That is a change of BYTES,
 *      so the only honest evidence is the frame showing whose art won.
 *   3. A spotlight holding a GROUP renders as one placement with companion
 *      chips, under a curator title -- not as several stacked heroes.
 * Plus a dark frame, because artwork ships a light and an optional dark variant
 * and the selection is a change of bytes rather than of CSS.
 *
 * `deviceScaleFactor` is 1, not 2: at this viewport a 2x capture exceeds the
 * 2000px edge cap that wedges a conversation carrying the image.
 *
 * Usage: node scripts/capture-editorial-spotlight.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/editorial-spotlight'
mkdirSync(OUT, { recursive: true })

/** A 16:9 gradient tile standing in for curator artwork, as an inline SVG blob. */
const art = (from, to, label) =>
  'data:image/svg+xml;utf8,' +
  encodeURIComponent(
    `<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="900">
       <defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
         <stop offset="0" stop-color="${from}"/><stop offset="1" stop-color="${to}"/>
       </linearGradient></defs>
       <rect width="1600" height="900" fill="url(#g)"/>
       <text x="80" y="820" font-family="system-ui" font-size="64" fill="#ffffff" opacity=".85">${label}</text>
     </svg>`,
  )

const A = (name, displayName, description, tags, extra = {}) => ({
  name, displayName, description, tags,
  author: 'Kiro Crew', version: '1.0.0', installed: false, updateAvailable: false,
  provenance: 'official', verified: true, ...extra,
})

/**
 * The hero app ships its OWN hero image. Frame 2 is what proves the curator's
 * art wins over it -- a fixture without it could not tell the two apart.
 */
const registryApps = [
  A('ops-mission-control', 'Ops Mission Control', 'Triage alarms, pages and incidents on one board.', ['oncall', 'monitoring'], {
    featured: 1, heroImage: art('#7c3aed', '#2563eb', "the APP's own hero"),
  }),
  A('auto-research', 'Research Lab', 'Multi-cycle research campaigns that keep working after you walk away.', ['research', 'autonomy']),
  A('pptx-maker', 'Deck Maker', 'Turns an outline into a presentation.', ['productivity']),
  A('code-review-sage', 'Code Review Sage', 'Deep-reviews pull requests and says which changes deserve attention.', ['code-review', 'git']),
  A('agent-worlds', 'Agent Worlds', 'Running agents as characters in an animated scene.', ['agents', 'visualization']),
]

/** Frame 2: one app, curator artwork, curator blurb. */
const SINGLE = [{
  appRefs: ['ops-mission-control'],
  blurb: 'Editorial copy the curator wrote, not the app manifest description.',
  artwork: { url: art('#0f766e', '#065f46', 'CURATOR artwork (light)'), urlDark: art('#134e4a', '#022c22', 'CURATOR artwork (dark)'), alt: 'A calm on-call board' },
}]

/** Frame 3: a group -- one placement, a title, companion chips. */
const GROUP = [{
  appRefs: ['auto-research', 'pptx-maker', 'code-review-sage'],
  title: 'Ship it before lunch',
  blurb: 'A spotlight can hold a group; one app is the same shape with one entry.',
  artwork: { url: art('#b45309', '#7c2d12', 'CURATOR artwork — group'), alt: 'Three tools side by side' },
}]


/** Frame 5: the schema ceiling (20 refs) at a narrow width -- the layout stress. */
const MANY = [{
  appRefs: registryApps.map(a => a.name),
  title: 'Everything at once',
  blurb: 'The schema allows up to 20 refs; this is what the placement does with them.',
  artwork: { url: art('#1e40af', '#312e81', 'CURATOR artwork — many') },
}]

async function shoot(browser, base, sections, file, label, mode = 'dark') {
  const context = await browser.newContext({
    viewport: { width: 1280, height: 820 },
    deviceScaleFactor: 1,
    colorScheme: mode,
  })
  const page = await context.newPage()
  logPageProblems(page, label)

  await stubDashboardApi(page, {
    theme: mode,
    extra: (path, route) => {
      if (path === '/api/apps/registry') {
        return json(route, {
          apps: registryApps,
          serverPlatform: { os: 'darwin', arch: 'arm64' },
          categoryOrder: [],
          editorialSections: sections,
        }), true
      }
      if (path === '/api/apps/registries') return json(route, { registries: [] }), true
      return false
    },
  })

  await page.addInitScript(m => {
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-theme-mode', m)
  }, mode)

  await page.goto(`${base}/apps`, { waitUntil: 'domcontentloaded' })
  // Assert the hero rendered before capturing: a harness that screenshots an
  // empty page produces a green run and a useless image.
  await page.getByText('FEATURED', { exact: false }).first().waitFor({ state: 'visible', timeout: 15_000 })
  await page.waitForTimeout(700)

  await page.screenshot({ path: `${OUT}/${file}` })
  console.log(`wrote ${OUT}/${file}  (${label})`)
  await context.close()
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()
try {
  await shoot(browser, base, [], 'no-sections.png', 'no published sections — unchanged')
  await shoot(browser, base, SINGLE, 'single-artwork.png', 'one app, curator artwork')
  await shoot(browser, base, GROUP, 'group-chips.png', 'a group with companion chips')
  await shoot(browser, base, SINGLE, 'single-artwork-light.png', 'curator artwork, light', 'light')
  await shoot(browser, base, MANY, 'group-many.png', 'the appRefs ceiling')
} finally {
  await browser.close()
  srv.close()
}

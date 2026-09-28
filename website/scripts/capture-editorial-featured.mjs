/**
 * Screenshot harness for the editorial featured list.
 *
 * Runs the REAL built SPA (website/dist) behind the shared static server and
 * answers every /api/** call from fixtures via Playwright route interception --
 * gateway-free, no kiro-cli, no token.
 *
 * WHAT NEEDS PROVING, and why one frame cannot. Five separate claims:
 *   1. With no published sections, the DERIVED hero still renders -- and now in
 *      the new shape. This is deliberately NOT a no-op: `sections` is published
 *      empty today, so the card below is what every user sees on upgrade, and
 *      the frame is the only honest evidence of that.
 *   2. An `app` card carries CURATOR artwork instead of the app's own hero
 *      image. That is a change of BYTES, so the only honest evidence is the
 *      frame showing whose art won.
 *   3. A `collection` card renders one ROW per member, each with its own
 *      install control -- the thing chips could not do.
 *   4. Rows reflect each member's OWN state: Get, Installed and Enable side by
 *      side on one card. A single card-level control could not express this.
 *   5. Several cards stack as a feed, which is what "a featured list" means.
 * Plus a light frame, because artwork ships a light and an optional dark variant
 * and the selection is a change of bytes rather than of CSS.
 *
 * `deviceScaleFactor` is 1, not 2: at this viewport a 2x capture exceeds the
 * 2000px edge cap that wedges a conversation carrying the image.
 *
 * Usage: node scripts/capture-editorial-featured.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/editorial-featured'
mkdirSync(OUT, { recursive: true })

/**
 * Curator artwork, addressed the way the PUBLISHED document addresses it: an
 * absolute URL on the catalog host, which is what the server's projection emits.
 *
 * Deliberately NOT a `data:` URI. The client drops any ref that is not a local
 * path or an https URL, so a `data:` fixture would be screened out and every
 * frame would silently fall back to the gradient -- the harness would keep
 * passing while proving nothing about artwork. The bodies are served by a route
 * on the catalog host, below.
 */
const CATALOG = 'https://apps.crew.kiro.dev'
const artBodies = new Map()

const art = (from, to, label) => {
  const path = `/assets/editorial/${encodeURIComponent(label).replace(/%20/g, '-').toLowerCase()}.svg`
  artBodies.set(
    path,
    `<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="900">
       <defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
         <stop offset="0" stop-color="${from}"/><stop offset="1" stop-color="${to}"/>
       </linearGradient></defs>
       <rect width="1600" height="900" fill="url(#g)"/>
       <text x="80" y="480" font-family="system-ui" font-size="72" fill="#ffffff" opacity=".9">${label}</text>
     </svg>`,
  )
  return `${CATALOG}${path}`
}

const A = (name, displayName, description, tags, extra = {}) => ({
  name, displayName, description, tags,
  author: 'Kiro Crew', version: '1.0.0', installed: false, updateAvailable: false,
  provenance: 'official', verified: true, ...extra,
})

/**
 * The lead app ships its OWN hero image. Frame 2 is what proves the curator's
 * art wins over it -- a fixture without it could not tell the two apart.
 *
 * `auto-research` is installed and `agent-worlds` is a disabled builtin, so the
 * mixed-state frame has something real to show rather than three identical rows.
 */
const registryApps = [
  A('ops-mission-control', 'Ops Mission Control', 'Triage alarms, pages and incidents on one board.', ['oncall', 'monitoring'], {
    featured: 1, heroImage: art('#7c3aed', '#2563eb', "the APP's own hero"),
  }),
  A('auto-research', 'Research Lab', 'Multi-cycle research campaigns that keep working after you walk away.', ['research', 'autonomy'], {
    installed: true, installedVersion: '1.0.0', enabled: true,
  }),
  A('pptx-maker', 'Deck Maker', 'Turns an outline into a presentation.', ['productivity']),
  A('code-review-sage', 'Code Review Sage', 'Deep-reviews pull requests and says which changes deserve attention.', ['code-review', 'git']),
  A('agent-worlds', 'Agent Worlds', 'Running agents as characters in an animated scene.', ['agents', 'visualization'], {
    origin: 'builtin', installed: true, enabled: false,
  }),
]

/** Frame 2: one app, curator artwork, curator blurb. */
const APP_CARD = [{
  type: 'app',
  appRefs: ['ops-mission-control'],
  blurb: 'Editorial copy the curator wrote, not the app manifest description.',
  artwork: { url: art('#0f766e', '#065f46', 'CURATOR artwork (light)'), urlDark: art('#134e4a', '#022c22', 'CURATOR artwork (dark)'), alt: 'A calm on-call board' },
}]

/** Frame 3: a collection -- one card, a theme, one row per member. */
const COLLECTION = [{
  type: 'collection',
  appRefs: ['pptx-maker', 'code-review-sage'],
  title: 'Ship it before lunch',
  blurb: 'A theme is what makes two unrelated apps belong on one card.',
  artwork: { url: art('#b45309', '#7c2d12', 'CURATOR artwork — collection'), alt: 'Two tools side by side' },
}]

/**
 * Frame 4: mixed install state. Get, Installed and Enable on one card, which is
 * the whole reason rows carry their own controls.
 */
const MIXED = [{
  type: 'collection',
  appRefs: ['pptx-maker', 'auto-research', 'agent-worlds'],
  title: 'Each row acts on its own app',
  blurb: 'One not installed, one installed, one a disabled builtin.',
  artwork: { url: art('#1e40af', '#312e81', 'CURATOR artwork — mixed state') },
}]

/** Frame 5: the list. Several cards of both types stacked as a feed. */
const FEED = [
  APP_CARD[0],
  COLLECTION[0],
  {
    type: 'collection',
    appRefs: ['auto-research', 'agent-worlds', 'ops-mission-control', 'pptx-maker', 'code-review-sage'],
    title: 'At the schema ceiling',
    blurb: 'Six is the cap, because every member is rendered and there is no detail page for an overflow.',
    artwork: { url: art('#155e75', '#164e63', 'CURATOR artwork — ceiling') },
  },
]

async function shoot(browser, base, sections, file, label, mode = 'dark', full = false) {
  const context = await browser.newContext({
    viewport: { width: 1280, height: 820 },
    deviceScaleFactor: 1,
    colorScheme: mode,
  })
  const page = await context.newPage()
  logPageProblems(page, label)

  // Serve the artwork bodies from the catalog host, so the refs the fixture
  // publishes are the same SHAPE the real projection emits and the client's
  // boundary check is genuinely exercised rather than bypassed.
  await page.route(`${CATALOG}/**`, route => {
    const body = artBodies.get(new URL(route.request().url()).pathname)
    if (!body) return route.fulfill({ status: 404, body: '' })
    return route.fulfill({ status: 200, contentType: 'image/svg+xml', body })
  })

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
  // Assert a card rendered before capturing: a harness that screenshots an empty
  // page produces a green run and a useless image. Either kicker proves it.
  await page.locator('text=/FEATURED|COLLECTION/i').first().waitFor({ state: 'visible', timeout: 15_000 })
  await page.waitForTimeout(700)

  await page.screenshot({ path: `${OUT}/${file}`, fullPage: full })
  console.log(`wrote ${OUT}/${file}  (${label})`)
  await context.close()
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()
try {
  await shoot(browser, base, [], 'no-sections.png', 'no published sections — unchanged')
  await shoot(browser, base, APP_CARD, 'app-card.png', 'one app, curator artwork')
  await shoot(browser, base, COLLECTION, 'collection-rows.png', 'a collection with per-app rows')
  await shoot(browser, base, MIXED, 'collection-mixed-state.png', 'Get / Installed / Enable on one card')
  await shoot(browser, base, APP_CARD, 'app-card-light.png', 'curator artwork, light', 'light')
  await shoot(browser, base, FEED, 'featured-feed.png', 'several cards stacked as a list')
} finally {
  await browser.close()
  srv.close()
}

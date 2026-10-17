/**
 * Screenshots for the pinned app registry `label` / `review` tier.
 *
 * Drives the isolated capture entry (website/capture/registry-review-tier.html),
 * which mounts the REAL `RegistryManager` and `CategoryRail` with the payload
 * `GET /api/apps/registries` returns for a build that pins one curated and one
 * community registry.
 *
 * Every frame asserts its own state before writing: the two badges must be
 * present with their exact shipped copy, the community row must NOT carry the
 * "Trusted source" badge, and the rail must list curated above community. A
 * frame that rendered the pre-fix state fails here instead of shipping as
 * evidence.
 *
 * Frames:
 *   01-registries-card   the External Registries card: curated, unreviewed
 *                        operator row, community — with the id as a subtitle
 *                        under each label
 *   02-sources-rail      the App Store SOURCES rail, same order, labelled icons
 *   03-both-light        both surfaces in the light theme
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6801 --strictPort   # in another shell
 *   node scripts/capture-registry-review-tier.mjs http://127.0.0.1:6801 ../temp-screenshots/registry-review-tier
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6801'
const OUT = process.argv[3] || '../temp-screenshots/registry-review-tier'
mkdirSync(OUT, { recursive: true })

/** The exact shipped copy, from website/src/i18n/locales/en.manual.json with
 *  `{{productName}}` resolved to its stock default. A frame is only written when
 *  the tips carry the whole string. */
const CURATED_TIP =
  'Reviewed by the Kiro Crew team before listing. Apps clone with your Git credentials.'
const COMMUNITY_TIP =
  'Listed by contributors after a lighter review. Not vetted by the Kiro Crew team. '
  + 'Apps still clone with your Git credentials, so install only apps you recognise.'
const RAIL_CURATED = 'Reviewed by the Kiro Crew team'
const RAIL_COMMUNITY = 'Community-listed, not vetted by the Kiro Crew team'

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1060, height: 760 }, deviceScaleFactor: 2 })

/** Fail loudly rather than write a frame documenting a state the code would not produce. */
function must(cond, why) {
  if (!cond) throw new Error(`ASSERTION FAILED: ${why}`)
}

async function scene(theme) {
  await page.goto(`${BASE}/capture/registry-review-tier.html?theme=${theme}`, { waitUntil: 'networkidle' })
  await page.getByText('Internal apps').first().waitFor({ timeout: 10_000 })

  // Both tiers are badged, with the shipped copy on the label.
  must(await page.getByLabel(CURATED_TIP).count() > 0, 'curated tip copy missing')
  must(await page.getByLabel(COMMUNITY_TIP).count() > 0, 'community tip copy missing')
  must(await page.getByText('Team reviewed', { exact: true }).count() > 0, 'Team reviewed badge missing')
  must(await page.getByText('Not vetted', { exact: true }).count() > 0, 'Not vetted badge missing')
  // The rail claim must be VISIBLE, not only a title/aria-label, and must use the
  // card's OWN words -- one string per tier across both surfaces.
  must(await page.getByText('Not vetted ·').count() > 0, 'rail community tier not visible text')
  must(await page.getByText('Team reviewed ·').count() > 0, 'rail curated tier not visible text')
  must(await page.getByText('id: community').count() > 0, 'id subtitle is not prefixed')

  // The defect: a community source must not read as team-trusted.
  must(await page.getByText('Trusted source', { exact: true }).count() === 0,
    'a review-tiered row still shows the plain Trusted source badge')

  // The id stays readable beside the display label, prefixed so it does not read
  // as more label.
  must(await page.getByText('id: internal').count() > 0, 'registry id not shown')

  // The rail names both claims.
  must(await page.getByLabel(RAIL_CURATED).count() > 0, 'rail curated label missing')
  must(await page.getByLabel(RAIL_COMMUNITY).count() > 0, 'rail community label missing')

  // The two surfaces must read the SAME order, operator row in the middle:
  // curated, then unreviewed, then community.
  for (const scope of ['[data-capture-card]', '[data-capture-rail]']) {
    const labels = await page
      .locator(`${scope} :text-is("Internal apps"), ${scope} :text-is("my-team"), ${scope} :text-is("Community apps")`)
      .allTextContents()
    must(JSON.stringify(labels) === JSON.stringify(['Internal apps', 'my-team', 'Community apps']),
      `${scope} order is ${JSON.stringify(labels)}, not curated -> unreviewed -> community`)
  }
}

await scene('dark')
await page.locator('[data-capture-card]').screenshot({ path: `${OUT}/01-registries-card.png` })
await page.locator('[data-capture-rail]').screenshot({ path: `${OUT}/02-sources-rail.png` })

await scene('light')
await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/03-both-light.png` })

await browser.close()
console.log(`wrote 3 frames to ${OUT}`)

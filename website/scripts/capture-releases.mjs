/**
 * Screenshot harness for Settings > Releases (the per-version changelog archive).
 *
 * Same shape as capture-channel-explainer.mjs: serves the REAL built SPA
 * (website/dist) and answers /api/** from the shared fixture router, so the shot
 * goes through the app's own router, Settings nav, react-query and markdown
 * renderer rather than a hand-mounted panel.
 *
 * The /api/releases bodies are not written by hand and not checked in either:
 * they are produced HERE, at capture time, by running the real backend parser
 * over the repo's real CHANGELOG.md. That is what makes these shots evidence
 * rather than decoration, and it means the payload cannot drift from the
 * changelog the way a committed copy would. (A committed copy also re-adds the
 * changelog's own prose to the diff, which the brand-name gate reads as new
 * misspellings of the product name.)
 *
 * Builds the SPA first: serve-dist serves whatever is on disk, so shooting a
 * UI-only change against a stale dist yields an "after" image identical to
 * before -- indistinguishable from the change not working.
 *
 * Scenario, theme and locale ride QUERY PARAMS rather than being flipped in
 * place: a hash-only change is a same-document navigation, so the init script
 * would not re-run and the next pass would silently re-shoot the previous state.
 *
 * Usage: node scripts/capture-releases.mjs [outDir]   (SKIP_BUILD=1 to reuse dist)
 *        PYTHON=/path/to/python overrides the interpreter used for the parser.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { serveDist } from './lib/serve-dist.mjs'
import { installApiFixtures, logPageFailures, json } from './lib/api-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/releases'
const W = 1500
const H = 950

/** Real `GET /api/releases` bodies, from the real parser over the real CHANGELOG. */
function realPayloads() {
  const py = [
    'import json, pathlib, sys',
    'sys.path.insert(0, "src")',
    'from kiro_crew.changelog import build_release_list',
    'md = pathlib.Path("CHANGELOG.md").read_text()',
    'out = {}',
    'for name, ver in (("real", "0.2.0"), ("prerelease", "0.2.0-rc.1"), ("stable", "0.1.2")):',
    '    rels = build_release_list(md, ver)',
    '    out[name] = {"current_version": ver, "releases": [r._asdict() for r in rels],',
    '                 "stale": any(r.in_progress for r in rels)}',
    'print(json.dumps(out))',
  ].join('\n')
  const raw = execFileSync(process.env.PYTHON || 'python3', ['-c', py], {
    cwd: '..', encoding: 'utf8', maxBuffer: 32 * 1024 * 1024,
  })
  return JSON.parse(raw)
}

const FIXTURES = realPayloads()

mkdirSync(OUT, { recursive: true })

async function main() {
  if (!process.env.SKIP_BUILD) {
    console.log('building dist (SKIP_BUILD=1 to reuse)…')
    execFileSync('npm', ['run', 'build'], { stdio: 'inherit' })
  }

  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: W, height: H },
    // Settings rows are 12-13px type; a 1x shot renders soft on GitHub.
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()

  await installApiFixtures(page)
  logPageFailures(page)

  // Registered AFTER the shared router so it wins (Playwright runs the most
  // recently added matching handler first), and reads the scenario off the live
  // URL so one handler serves every pass.
  await page.route('**/api/releases', route => {
    const rel = new URL(route.request().url()).searchParams.get('rel')
      || new URL(page.url()).searchParams.get('rel')
      || 'prerelease'
    // `error` is not a payload: the panel has to tell a failed fetch apart from
    // an archive with nothing in it, and only a real non-2xx exercises that.
    if (rel === 'error') return route.fulfill({ status: 500, contentType: 'application/json', body: '{}' })
    return json(route, FIXTURES[rel])
  })
  await page.route('**/api/theme/boot', route => {
    const mode = new URL(page.url()).searchParams.get('theme') === 'light' ? 'light' : 'dark'
    return json(route, { mode, theme: '' })
  })

  await page.addInitScript(() => {
    const q = new URLSearchParams(location.search)
    localStorage.clear()
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-theme', q.get('theme') === 'light' ? 'light' : 'dark')
    if (q.get('lang')) localStorage.setItem('mc-lang', q.get('lang'))
  })

  /** Load a Settings tab and wait for the panel to settle. */
  async function load({ tab = 'releases', rel = 'prerelease', theme = 'dark', lang = '' } = {}) {
    const q = new URLSearchParams({ tab, rel, theme })
    if (lang) q.set('lang', lang)
    await page.goto(`${base}/settings?${q}`, { waitUntil: 'domcontentloaded' })
    // The error scenario renders a single line and never an <article>, so it
    // cannot share the populated page's wait condition.
    const settled = tab !== 'releases' ? '[data-testid="channel-switcher"], main'
      : rel === 'error' ? 'main' : 'article h3'
    await page.waitForSelector(settled, { timeout: 15_000 })
    await page.waitForTimeout(1200)
    // The pointer sits where the last pass left it, so its hover styling would
    // otherwise leak into the next "at rest" shot.
    await page.mouse.move(W - 40, H - 40)
  }

  const shot = async name => {
    await page.screenshot({ path: `${OUT}/${name}.png` })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  /** Crop the union of two elements plus padding -- used for the nav fence. */
  async function cropUnion(name, a, b, pad = 20) {
    const [x, y] = [a, b].reduce(([mx, my], box) => [Math.min(mx, box.x), Math.min(my, box.y)], [Infinity, Infinity])
    const right = Math.max(a.x + a.width, b.x + b.width)
    const bottom = Math.max(a.y + a.height, b.y + b.height)
    await page.screenshot({
      path: `${OUT}/${name}.png`,
      clip: {
        x: Math.max(0, x - pad),
        y: Math.max(0, y - pad),
        width: Math.min(W - Math.max(0, x - pad), right - x + pad * 2),
        height: bottom - y + pad * 2,
      },
    })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  /** Report what the page actually says, so a wrong-state shot is caught here. */
  async function describe(label) {
    const heading = (await page.locator('article h3').first().innerText()).replace(/\n/g, ' | ')
    const rows = (await page.locator('nav').filter({ hasText: '0.1.2' }).first().innerText()).replace(/\n/g, ' | ')
    const body = await page.locator('article').first().innerText()
    console.log(`  ${label}\n    heading: ${heading}\n    rows:    ${rows}\n    bodyLen: ${body.length}`)
  }

  // 1-2. An insider on 0.2.0-rc.1: the unreleased 0.2.0 row is selected by
  //      default and the staleness caveat is stated.
  for (const theme of ['dark', 'light']) {
    await load({ rel: 'prerelease', theme })
    await describe(`prerelease-${theme}`)
    await shot(`01-prerelease-in-progress-${theme}`)

    // 3-4. The real 0.1.2 notes, rendered by the app's own markdown renderer.
    await page.locator('nav').filter({ hasText: '0.1.2' }).getByText('0.1.2').first().click()
    await page.waitForTimeout(400)
    await page.mouse.move(W - 40, H - 40)
    await describe(`notes-${theme}`)
    await shot(`02-real-notes-0.1.2-${theme}`)
  }

  // 5. A stable build whose own version shipped without a changelog section.
  await load({ rel: 'real', theme: 'light' })
  await describe('current-no-notes')
  await shot('03-current-shipped-no-notes-light')

  // 6. The nav fence: Releases sits after the divider, About stays last.
  const relBtn = await page.getByRole('button', { name: 'Releases', exact: true }).first().boundingBox()
  const aboutBtn = await page.getByRole('button', { name: 'About', exact: true }).first().boundingBox()
  await cropUnion('04-settings-nav-fence-light', relBtn, aboutBtn)

  // 7. The other half of the diff: About no longer inlines the whole changelog,
  //    it links here. Cropped to the card that carries the link -- a blind
  //    padded box around a 20px-tall link lands half on the nav column.
  await load({ tab: 'about', rel: 'real', theme: 'light' })
  const link = page.getByRole('link', { name: /View all releases/i }).first()
  if (await link.count()) {
    const card = link.locator('xpath=ancestor::div[contains(@class,"card")][1]')
    const target = (await card.count()) ? card.first() : link
    const b = await target.boundingBox()
    await cropUnion('05-about-links-to-releases-light', b, b, 16)
  } else {
    await shot('05-about-links-to-releases-light')
    console.log('  (link not found -- full-page fallback)')
  }

  // 8. A non-English locale. German, not zh/ja: this box has no CJK font, so a
  //    CJK shot would show tofu boxes that say nothing about the UI.
  await load({ rel: 'prerelease', theme: 'light', lang: 'de' })
  await shot('06-prerelease-de-light')

  // 9. /api/releases answered 500. Distinct wording from the empty archive, so
  //    the reader is not sent hunting a build problem that does not exist.
  await load({ rel: 'error', theme: 'light' })
  const failed = await page.getByText(/Could not load the release notes/).count()
  console.log(`  load-failed state present: ${failed === 1}`)
  await shot('07-load-failed-light')

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })

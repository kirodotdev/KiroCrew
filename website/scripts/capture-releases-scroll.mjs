/**
 * Scroll-containment evidence for Settings > Releases.
 *
 * The claim under test is behavioural, not cosmetic: turning the wheel over the
 * release notes must move ONLY the notes. The tab header ("Releases" + its
 * description) and the version rail beside the notes must not move, and the
 * document must not scroll at all.
 *
 * Stated as measurements rather than screenshots because a still frame cannot
 * distinguish "the header is pinned" from "the header happens to be at the top
 * of this frame" — so each scenario records geometry BEFORE and AFTER a real
 * wheel gesture and compares. The screenshots are the human-readable record of
 * the same run; the numbers are what fails.
 *
 * Runs against the built SPA (website/dist) through the app's own router and
 * react-query, with /api/releases answered by the real backend parser over the
 * real CHANGELOG.md — the same fixture the state harness uses.
 *
 * Because the assertions are falsifiable they FAIL on a tree without the fix,
 * which is how the "before" images in the PR were shot: ALLOW_FAIL=1 collects
 * the shots and prints the failing table instead of exiting non-zero.
 *
 * Usage: node scripts/capture-releases-scroll.mjs [outDir]
 *          SKIP_BUILD=1  reuse dist as-is
 *          ALLOW_FAIL=1  report failures without a non-zero exit ("before" pass)
 *          LABEL=before  suffix written into the file names
 *          PYTHON=...    interpreter used for the parser
 */
import { chromium } from 'playwright'
import { mkdirSync, readdirSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { join } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import { installApiFixtures, logPageFailures, json } from './lib/api-fixtures.mjs'
import { realReleasePayloads } from './lib/releases-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/releases-scroll'
const LABEL = process.env.LABEL ? `-${process.env.LABEL}` : ''
const W = 1500
const H = 950
// Settings rows are 12-13px type; a 1x shot renders soft on GitHub. Videos stay
// at 1x -- a 2x webm of a 1500px viewport is ~4x the bytes for no added legibility.
const SCALE = 2
/** Wheel gesture size. Larger than any plausible header, so "did not move" is
 *  a real answer rather than a rounding one. */
const WHEEL = 600
/** Sub-pixel layout jitter (fonts settling, scrollbar reflow) is tolerated;
 *  anything a reader could see is not. */
const EPS = 1.5

const FIXTURES = realReleasePayloads()
const results = []

/** Record one assertion. `detail` is printed either way — a passing number is
 *  what makes the failing one interpretable later. */
function check(name, ok, detail) {
  results.push({ name, ok, detail })
  console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${name} — ${detail}`)
}

async function main() {
  mkdirSync(OUT, { recursive: true })
  // Playwright names videos by a random id, so the only way to tell this run's
  // capture from a file already there is to record what was there first.
  const preexisting = new Set(readdirSync(OUT).filter(f => f.endsWith('.webm')))

  if (!process.env.SKIP_BUILD) {
    console.log('building dist (SKIP_BUILD=1 to reuse)…')
    execFileSync('npm', ['run', 'build'], { stdio: 'inherit' })
  }

  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  // Stills context: NO recordVideo. The clip is made in a second context below,
  // so the assertion passes and the rail crops stay out of it.
  const context = await browser.newContext({
    viewport: { width: W, height: H },
    deviceScaleFactor: SCALE,
  })
  const page = await context.newPage()
  await installApiFixtures(page)
  logPageFailures(page)

  // Registered after the shared router so it wins, and reads the scenario off
  // the live URL so one handler serves every pass.
  await page.route('**/api/releases', route => {
    const rel = new URL(page.url()).searchParams.get('rel') || 'prerelease'
    return json(route, FIXTURES[rel])
  })
  await page.route('**/api/theme/boot', route =>
    json(route, { mode: new URL(page.url()).searchParams.get('theme') === 'light' ? 'light' : 'dark', theme: '' }))

  await page.addInitScript(() => {
    const q = new URLSearchParams(location.search)
    localStorage.clear()
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-theme', q.get('theme') === 'light' ? 'light' : 'dark')
  })

  // The tab title itself, not the header wrapper: the wrapper's test id is part
  // of this change, and a selector that only resolves on the fixed tree cannot
  // shoot the "before" comparison the PR needs.
  const header = page.locator('div.text-2xl.font-bold')
  const rail = page.locator('nav[aria-label="Versions"]')
  const notes = page.locator('article').first()

  const shot = async name => {
    await page.screenshot({ path: `${OUT}/${name}${LABEL}.png` })
    console.log('wrote', `${OUT}/${name}${LABEL}.png`)
  }

  async function load({ rel = 'prerelease', theme = 'dark' } = {}) {
    await page.goto(`${base}/settings?tab=releases&rel=${rel}&theme=${theme}`, { waitUntil: 'domcontentloaded' })
    await page.waitForSelector('article h3', { timeout: 15_000 })
    await page.waitForTimeout(1000)
    // A selector that silently matched two titles would compare one element's
    // position against another's and call the difference a scroll.
    const n = await header.count()
    if (n !== 1) throw new Error(`expected exactly 1 tab title, matched ${n}`)
    // The pointer keeps the position the last pass left it in, so its hover
    // styling would otherwise leak into the next "at rest" shot.
    await page.mouse.move(W - 40, H - 40)
  }

  /** Geometry of everything that must hold still, plus who is scrolled.
   *  Boxes and scroll offsets are kept under distinct keys on purpose: a
   *  `notes` that means both a rectangle and a number is exactly the kind of
   *  shadowing that turns a broken assertion into a passing one. */
  async function geometry() {
    const [headerBox, railBox, notesBox] = await Promise.all([
      header.boundingBox(), rail.boundingBox(), notes.boundingBox(),
    ])
    const scrolls = await page.evaluate(() => ({
      docScroll: document.scrollingElement?.scrollTop ?? 0,
      // Every scrolled ancestor, so a scroller that is neither the document nor
      // the notes column cannot hide between them.
      otherScrollers: [...document.querySelectorAll('*')]
        .filter(el => el.scrollTop > 0 && el.tagName !== 'ARTICLE' && el.tagName !== 'NAV')
        .map(el => `${el.tagName.toLowerCase()}.${el.className.toString().slice(0, 24)}=${Math.round(el.scrollTop)}`),
      notesScroll: document.querySelector('article')?.scrollTop ?? 0,
    }))
    return { headerBox, railBox, notesBox, ...scrolls }
  }

  /** Wheel over the NOTES column specifically — the surface a reader points at. */
  async function wheelOverNotes(box, dy = WHEEL) {
    await page.mouse.move(box.x + box.width / 2, box.y + Math.min(box.height / 2, H / 2))
    await page.mouse.wheel(0, dy)
    await page.waitForTimeout(500)
  }

  // ---- 1. Long notes: the case the bug was reported on --------------------
  // 0.1.2's section is the longest in the changelog, so it overflows any
  // plausible window and the wheel has somewhere to go.
  await load({ rel: 'prerelease', theme: 'light' })
  await rail.getByText('0.1.2', { exact: true }).click()
  await page.waitForTimeout(500)
  await page.mouse.move(W - 40, H - 40)
  await shot('01-long-notes-at-rest')

  const before = await geometry()
  await wheelOverNotes(before.notesBox)
  const after = await geometry()
  await shot('02-long-notes-scrolled')

  console.log(`\nlong notes, wheel ${WHEEL}px over the notes column:`)
  check('notes column actually scrolled',
    after.notesScroll > 100, `article.scrollTop ${before.notesScroll} -> ${after.notesScroll}`)
  check('tab header did not move',
    Math.abs(after.headerBox.y - before.headerBox.y) < EPS,
    `header.y ${before.headerBox.y.toFixed(1)} -> ${after.headerBox.y.toFixed(1)}`)
  check('version rail did not move',
    Math.abs(after.railBox.y - before.railBox.y) < EPS,
    `rail.y ${before.railBox.y.toFixed(1)} -> ${after.railBox.y.toFixed(1)}`)
  check('document did not scroll',
    after.docScroll === 0, `document.scrollTop ${after.docScroll}`)
  check('no scroller between the document and the notes',
    after.otherScrollers.length === 0, after.otherScrollers.length ? after.otherScrollers.join(', ') : 'none')

  // The rail is a scroller in its own right: a wheel over IT moves the version
  // list without disturbing the notes. Asserted so "contained" is not read as
  // "the rail can never scroll" by a future change that pins it outright.
  const railBefore = await geometry()
  await page.mouse.move(railBefore.railBox.x + railBefore.railBox.width / 2, railBefore.railBox.y + 40)
  await page.mouse.wheel(0, WHEEL)
  await page.waitForTimeout(400)
  const railAfter = await geometry()
  check('wheel over the rail leaves the notes where they were',
    Math.abs(railAfter.notesScroll - railBefore.notesScroll) < EPS,
    `article.scrollTop ${railBefore.notesScroll} -> ${railAfter.notesScroll}`)

  // ---- 2. Short notes: the divider case ----------------------------------
  // 0.1.3 is two sentences. The rail carries the divider (border-r), so a rail
  // that only grows to fit its own rows draws a stub line into empty space.
  await load({ rel: 'prerelease', theme: 'light' })
  await rail.getByText('0.1.3', { exact: true }).click()
  await page.waitForTimeout(500)
  await page.mouse.move(W - 40, H - 40)
  await shot('03-short-notes-divider')

  const short = await geometry()
  // How much of the window is left below the rail's top edge — the height a
  // full-height divider has to cover. A trailing gap smaller than the padding
  // that produced it is indistinguishable from "reaches the bottom".
  const reach = H - short.railBox.y
  check('divider spans the pane on short notes',
    short.railBox.height > reach - 8, `rail.height ${short.railBox.height.toFixed(1)} of ${reach.toFixed(1)}px available`)
  check('short notes do not scroll',
    short.notesScroll === 0, `article.scrollTop ${short.notesScroll}`)

  // ---- 3. Dark theme, so the divider is judged in both palettes ----------
  await load({ rel: 'prerelease', theme: 'dark' })
  await rail.getByText('0.1.3', { exact: true }).click()
  await page.waitForTimeout(500)
  await page.mouse.move(W - 40, H - 40)
  await shot('04-short-notes-divider-dark')

  // ---- 3b. The rail up close, in both selection states -------------------
  // The badge is 10px type in a 192px column, so a full-window shot cannot be
  // used to judge its treatment. Both states, because the unreleased row is the
  // DEFAULT selection on a prerelease build and the badge has to read on top of
  // `bg-accent-subtle` as well as on the plain rail.
  for (const theme of ['light', 'dark']) {
    for (const selected of [false, true]) {
      await load({ rel: 'prerelease', theme })
      if (!selected) {
        await rail.getByText('0.1.3', { exact: true }).click()
        await page.waitForTimeout(400)
        await page.mouse.move(W - 40, H - 40)
      }
      const box = await rail.boundingBox()
      const name = `05-rail-${theme}-${selected ? 'selected' : 'unselected'}`
      await page.screenshot({
        path: `${OUT}/${name}${LABEL}.png`,
        clip: { x: box.x - 6, y: box.y - 8, width: box.width + 12, height: 175 },
      })
      console.log('wrote', `${OUT}/${name}${LABEL}.png`)
    }
  }

  // ---- 4. The recording: what a still frame cannot show -----------------
  // A SECOND context, opened only now. Playwright records from context creation
  // to context close, so sharing the stills' context put every assertion pass
  // and every rail crop in the clip -- a 21s scroll demo became a 40s reel of
  // page reloads, and the GIF doubled in bytes for none of the signal.
  await context.close()
  const rec = await browser.newContext({
    viewport: { width: W, height: H },
    recordVideo: { dir: OUT, size: { width: W, height: H } },
  })
  const rp = await rec.newPage()
  await installApiFixtures(rp)
  logPageFailures(rp)
  await rp.route('**/api/releases', route => json(route, FIXTURES.prerelease))
  await rp.route('**/api/theme/boot', route => json(route, { mode: 'light', theme: '' }))
  await rp.addInitScript(() => {
    localStorage.clear()
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-theme', 'light')
  })

  const recRail = rp.locator('nav[aria-label="Versions"]')
  await rp.goto(`${base}/settings?tab=releases&rel=prerelease&theme=light`, { waitUntil: 'domcontentloaded' })
  await rp.waitForSelector('article h3', { timeout: 15_000 })
  await rp.waitForTimeout(900)
  // One continuous pass — long notes down, then back up, then a short release —
  // so the header and rail can be seen holding still through the whole gesture.
  await recRail.getByText('0.1.2', { exact: true }).click()
  await rp.waitForTimeout(900)
  const box = await rp.locator('article').first().boundingBox()
  const wheel = async dy => {
    await rp.mouse.move(box.x + box.width / 2, box.y + Math.min(box.height / 2, H / 2))
    await rp.mouse.wheel(0, dy)
    await rp.waitForTimeout(320)
  }
  for (let i = 0; i < 6; i++) await wheel(420)
  await rp.waitForTimeout(500)
  for (let i = 0; i < 6; i++) await wheel(-420)
  await rp.waitForTimeout(500)
  await recRail.getByText('0.1.3', { exact: true }).click()
  await rp.waitForTimeout(1400)

  await rec.close() // flushes the video
  await browser.close()
  srv.close()

  const produced = readdirSync(OUT).filter(f => f.endsWith('.webm') && !preexisting.has(f))
  // Exactly one: the context records one page. Zero means the capture never
  // started; more than one means picking arbitrarily would ship the wrong clip.
  if (produced.length !== 1) {
    throw new Error(`expected exactly 1 new .webm in ${OUT}, found ${produced.length}`)
  }
  console.log(`\nWEBM ${join(OUT, produced[0])}`)

  const failed = results.filter(r => !r.ok)
  console.log(`\n${results.length - failed.length}/${results.length} assertions pass`)
  if (failed.length && !process.env.ALLOW_FAIL) process.exit(1)
}

main().catch(err => { console.error(err); process.exit(1) })

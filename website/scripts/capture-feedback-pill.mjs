/**
 * Screenshot harness for the top bar's split feedback pill.
 *
 * Shoots the SAME control in all three lanes so the review can see both halves
 * of the contract at once: on stable it must be indistinguishable from the
 * single "Request a Feature" button it replaced, and on a prerelease lane it
 * must carry a visible, unmissable report chip.
 *
 * Same shape as capture-channel-explainer.mjs — serves the REAL built SPA from
 * website/dist and answers /api/** from the shared fixture router. The lane
 * comes from a QUERY PARAM (not the hash) because a hash-only change is a
 * same-document navigation: React state would not be re-created and every pass
 * after the first would silently re-shoot the first lane.
 *
 * Builds dist first: serve-dist serves whatever is on disk, so shooting a
 * UI-only change against a stale dist yields an "after" image identical to
 * before — indistinguishable from the change not working.
 *
 * Usage: node scripts/capture-feedback-pill.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { serveDist } from './lib/serve-dist.mjs'
import { installApiFixtures, logPageFailures, json } from './lib/api-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/feedback-pill'
const PREFIX = process.argv[3] || 'after'

const VERSIONS = {
  stable: '0.5.0',
  // PEP 440 spellings on purpose: this is what a CLI/wheel install reports, and
  // it is the case the old hyphen-only classifier called "stable".
  insider: '0.5.0rc2',
  nightly: '0.5.0.dev20260807061500',
}

mkdirSync(OUT, { recursive: true })

async function main() {
  if (!process.env.SKIP_BUILD) {
    console.log('building dist (SKIP_BUILD=1 to reuse)…')
    execFileSync('npm', ['run', 'build'], { stdio: 'inherit' })
  }

  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1500, height: 950 },
    // The pill's type is 11-12px; a 1x shot renders soft on GitHub.
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()

  await installApiFixtures(page)
  logPageFailures(page)

  // Answered AFTER installApiFixtures so this route wins over its catch-all:
  // the pill keys off status.release_channel, which the shared fixture has no
  // reason to carry.
  //
  // The lane is read off the REQUESTING DOCUMENT's url, not the request's own:
  // the SPA fetches a bare `/api/status` with no query string, so reading
  // `chan` from the request would always fall back to stable and every pass
  // would shoot an identical image.
  await page.route('**/api/status*', route => {
    const doc = route.request().frame().url()
    const chan = new URL(doc).searchParams.get('chan') || 'stable'
    return json(route, {
      sessions: 0,
      crons: 0,
      lessons: 0,
      uptime: 120,
      version: VERSIONS[chan],
      release_channel: chan,
    })
  })

  await page.addInitScript(() => {
    localStorage.clear()
    localStorage.setItem('mc-theme', 'dark')
    localStorage.setItem('mc-onboarded', '1')
  })

  async function shoot(name) {
    const pill = page.locator('[data-testid="feedback-pill"]')
    if (!(await pill.count())) {
      // boundingBox() on a missing locator TIMES OUT rather than returning
      // null, so the absent case is handled before measuring.
      await page.screenshot({ path: `${OUT}/${name}.png` })
      console.log('wrote (full page fallback — pill not found)', `${OUT}/${name}.png`)
      return
    }
    const b = await pill.first().boundingBox()
    const pad = 20
    await page.screenshot({
      path: `${OUT}/${name}.png`,
      clip: {
        x: Math.max(0, b.x - pad * 4),
        y: Math.max(0, b.y - pad),
        width: Math.min(1500 - Math.max(0, b.x - pad * 4), b.width + pad * 5),
        height: b.height + pad * 2,
      },
    })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  for (const [i, chan] of ['stable', 'insider', 'nightly'].entries()) {
    // `/settings`, not `/` — the top bar (and the pill) is part of the app
    // shell either way, but the root route mounts the chat page, which throws
    // under the empty-fixture slots list. capture-channel-explainer.mjs shoots
    // this same route for the same reason.
    await page.goto(`${base}/settings?tab=about&chan=${chan}`, { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2600)
    // Leave the pointer somewhere neutral: hover styling from the previous pass
    // would otherwise leak into the next "at rest" shot.
    await page.mouse.move(1400, 900)
    await shoot(`${PREFIX}-0${i + 1}-${chan}`)
  }

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })

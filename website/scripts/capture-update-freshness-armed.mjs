/**
 * Evidence capture for the update-freshness PR's one new user-visible surface:
 * the armed panel in Settings > About, in both themes.
 *
 * The scene is the whole point of the change. The panel's offer comes from a
 * verdict up to 12 hours old; arming re-checks the feed so the approval installs
 * the NEWEST build. When a release published in between, the button the user just
 * clicked and the panel they are looking at name different versions — so the
 * capture drives exactly that: offer v0.4.7, arm answers v0.4.8.
 *
 * Two frames per theme, because the offer is the only thing that makes the armed
 * frame legible:
 *   offer-<theme>.png  — "Update to v0.4.7"
 *   armed-<theme>.png  — the armed version line (v0.4.8) AND the note explaining
 *                        why the number moved
 *
 * Runs against a Vite dev server with every /api/* request answered inline —
 * gateway-free. Copy of the technique in capture-version-display-fold.mjs.
 *
 * Usage:
 *   npx vite --port 5199 &            # or `npm run dev -- --port 5199`
 *   node scripts/capture-update-freshness-armed.mjs [viteBase] [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const base = process.argv[2] || 'http://127.0.0.1:5199'
const outDir = process.argv[3] || '../temp-screenshots/update-freshness'
mkdirSync(outDir, { recursive: true })

const OFFERED = '0.4.7' // what the stale verdict advertises
const ARMED = '0.4.8' // what the arm's own re-check found, and what installs

// Escape every regex metacharacter, not just the dots a version string happens to
// carry: a partial escape leaves a backslash in the input able to change what the
// pattern matches, so the button lookup could silently miss and the capture would
// screenshot the wrong panel.
const escapeRe = (s) => String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

// Read the copy from the CATALOG, so a key rename fails the capture loudly
// instead of silently screenshotting a panel missing the line under test.
const LOCALES = fileURLToPath(new URL('../src/i18n/locales/', import.meta.url))
const manual = JSON.parse(readFileSync(LOCALES + 'en.manual.json', 'utf-8'))
const about = manual.pages.settings.aboutPanel
// The proactive popup's copy lives in the generated catalog, not the manual one.
const generated = JSON.parse(readFileSync(LOCALES + 'en.json', 'utf-8'))
const modal = generated.components.updateFoundModal
// Same for the arm button's pending label: this PR deliberately REUSED an existing
// key rather than adding a string, and that key is a generated one.
const aboutGenerated = generated.pages.settings.aboutPanel
for (const key of ['update_to_version', 'armed_version_changed', 'armed_run_on_host']) {
  if (!about[key]) throw new Error(`catalog key ${key} missing from en.manual.json — renamed?`)
}
if (!aboutGenerated.checking_for_updates) {
  throw new Error('catalog key checking_for_updates missing from en.json — renamed?')
}

const STATUS = {
  uptime: '2h', start_time: 0, sessions: 1, messages: 3, cron_jobs: 0,
  lessons: 0, subagents: 0, no_crons: false, branch: '', commit: '',
  release_channel: 'stable', version: '0.4.6', version_display: '0.4.6',
  update_available: true, update_can_apply: false, update_can_arm: true,
  update_check_status: 'succeeded', update_command: 'kirocrew update',
  update_latest_version: OFFERED, update_latest_version_display: OFFERED,
  update_channel: 'stable', update_managed_by: 'kirocrew',
  update_commits_ahead: 0, update_commits_behind: 0,
}

const browser = await chromium.launch()

async function scene(theme, opts = {}) {
  // `armAnswers` is what the arm's re-check reports. Defaulting to ARMED keeps the
  // headline scene (the versions differ) as the plain call; passing OFFERED drives
  // the ORDINARY path, where the note must be absent rather than merely unread.
  const { armAnswers = ARMED, label = `armed-${theme}`, expectNote = true, holdArm = false, captureOffer = true, armRefusal = false } = opts
  // When the arm is held, the click cannot resolve until this gate is released, so
  // the pending state is a state the capture can WAIT for instead of race.
  let releaseArm = () => {}
  const armHeld = new Promise((resolve) => { releaseArm = resolve })
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 2 })
  await ctx.addInitScript(mode => {
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-theme', mode)
  }, theme)
  const p = await ctx.newPage()
  await p.route('**/*', async route => {
    const u = new URL(route.request().url())
    if (!u.pathname.startsWith('/api/')) return route.continue()
    const json = body => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
    if (u.pathname === '/api/status') return json(STATUS)
    if (u.pathname === '/api/update/arm') {
      // The gateway refuses an arm whose offer the feed has since withdrawn. The
      // message is the backend's own English literal (the panel renders it
      // verbatim), so this frame shows the copy a user actually gets.
      if (armRefusal) {
        return route.fulfill({
          status: 409,
          contentType: 'application/json',
          body: JSON.stringify({
            error: "This update is no longer offered — you're already up to date",
            code: 'arm_no_longer_offered',
          }),
        })
      }
      // The re-check the PR adds: the gateway arms what the feed serves NOW.
      if (holdArm) await armHeld
      return json({
        ok: true, armed: true, request_id: 'r1',
        version: armAnswers, version_display: armAnswers,
        expires_in: 600, approve_command: 'kirocrew update approve r1',
      })
    }
    if (u.pathname.startsWith('/api/update/check')) {
      return json({
        check_status: 'succeeded', update_available: true, error_code: null,
        latest_version: OFFERED, latest_version_display: OFFERED,
        channel: 'stable', managed_by: 'kirocrew', can_apply: false, can_arm: true,
        update_command: 'kirocrew update', current_version: '0.4.6',
        commits_ahead: 0, commits_behind: 0,
      })
    }
    if (u.pathname.startsWith('/api/changelog')) return json({ content: '' })
    if (u.pathname.startsWith('/api/models')) return json({ models: [] })
    if (u.pathname.startsWith('/api/instances')) return json({ active: false, instances: [], warm_set_cap: 0, sso: {} })
    if (u.pathname.startsWith('/api/kiro-prerequisite')) return json({ ready: true, initial_setup_complete: true, setup_allowed: true })
    // List-shaped endpoints crash the app when handed `{}` (e.g.
    // pendingApprovals.filter): answer every array consumer with [].
    if (/approvals|sessions|crons|lessons|skills|notifications|artifacts|apps\b/.test(u.pathname)) return json([])
    return json({})
  })

  await p.goto(`${base}/settings/about`, { waitUntil: 'networkidle' })
  // The proactive update popup opens over the page and its backdrop intercepts
  // every click in the panel underneath. It mounts when the update poll resolves,
  // which lands AFTER `networkidle` — so a one-shot visibility check races it,
  // reads "not there", skips the dismissal, and leaves the backdrop to swallow the
  // arm click below. Wait for the popup, dismiss it by its catalog label, then wait
  // for it to detach, so the panel is reachable rather than probably reachable.
  const dismiss = p.getByRole('button', { name: modal.dismiss, exact: true })
  await dismiss.waitFor({ state: 'visible', timeout: 15_000 })
  await dismiss.click()
  await dismiss.waitFor({ state: 'detached', timeout: 15_000 })

  const offer = p.getByTestId('in-app-update')
  await offer.waitFor({ state: 'visible', timeout: 20_000 })
  await offer.scrollIntoViewIfNeeded()
  await p.waitForTimeout(300)
  // Only the headline scenes ship this frame. The added scenes still WAIT for the
  // offer above -- it is the precondition for the click -- but re-writing an
  // identical file would let a later scene overwrite a good frame with a worse one.
  if (captureOffer) {
    await p.screenshot({ path: `${outDir}/offer-${theme}.png` })
    console.log(`captured ${outDir}/offer-${theme}.png`)
  }

  await p.getByRole('button', { name: new RegExp(`update to v${escapeRe(OFFERED)}`, 'i') }).click()

  // The arm click waits on a feed round trip, so the button says what the wait is
  // for. With the response held, that state is stable enough to photograph.
  if (holdArm) {
    const pending = p.getByRole('button', { name: aboutGenerated.checking_for_updates, exact: true })
    await pending.waitFor({ state: 'visible', timeout: 10_000 })
    await pending.scrollIntoViewIfNeeded()
    await p.waitForTimeout(300)
    await p.screenshot({ path: `${outDir}/${label}.png` })
    console.log(`captured ${outDir}/${label}.png`)
    releaseArm()
    await ctx.close()
    return
  }

  // The refusal never reaches the armed panel: it leaves the offer on screen with
  // the notice under it, which is the state the user is actually looking at.
  if (armRefusal) {
    const notice = p.getByTestId('arm-error')
    await notice.waitFor({ state: 'visible', timeout: 10_000 })
    await notice.scrollIntoViewIfNeeded()
    await p.waitForTimeout(300)
    await p.screenshot({ path: `${outDir}/${label}.png` })
    console.log(`captured ${outDir}/${label}.png`)
    await ctx.close()
    return
  }

  const armed = p.getByTestId('in-app-update-armed')
  await armed.waitFor({ state: 'visible', timeout: 20_000 })
  const note = p.getByTestId('armed-version-changed')
  if (expectNote) {
    // Fail loudly rather than shipping a frame that does not show the fix.
    await note.waitFor({ state: 'visible', timeout: 10_000 })
  } else {
    // The ordinary path's guarantee is an ABSENCE, and absence needs the panel to
    // have settled first -- otherwise this passes simply by being early. The armed
    // panel is already visible above, so the note would be mounted by now if it
    // were going to be, and `count()` reads the settled DOM rather than a race.
    await p.waitForTimeout(300)
    const seen = await note.count()
    if (seen !== 0) throw new Error(`armed_version_changed rendered with matching versions (${seen}) — it must explain only a difference`)
  }
  await armed.scrollIntoViewIfNeeded()
  await p.waitForTimeout(300)
  await p.screenshot({ path: `${outDir}/${label}.png` })
  console.log(`captured ${outDir}/${label}.png`)

  await ctx.close()
}

await scene('light')
await scene('dark')
// The two states the UX lane found unphotographed. One frame each, in light only:
// both are theme-independent in a way the pair above already established.
await scene('light', { label: 'arming-light', holdArm: true, captureOffer: false })
await scene('light', { armAnswers: OFFERED, label: 'armed-match-light', expectNote: false, captureOffer: false })
// The refusal a stale offer earns when the feed has moved on — the one remaining
// user-visible state the UX lane found unphotographed.
await scene('light', { label: 'arm-refused-light', armRefusal: true, captureOffer: false })
await browser.close()

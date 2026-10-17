/**
 * Screenshot harness for the Command Bar's ARTIFACTS view.
 *
 * Runs the REAL built SPA (website/dist) behind the shared `serveDist` server and
 * answers every /api/** call from fixtures through `stubDashboardApi`. No gateway,
 * no dashboard auth, no kiro-cli — which is what lets it run on a host where the
 * pod's port-ownership proof cannot be made.
 *
 * The `/api/artifacts` stub matches on the `q` parameter the way the real endpoint
 * does — a case-insensitive substring of the NAME, and nothing else. That is
 * deliberate: a stub that matched descriptions too would photograph a feature this
 * stage does not have, and the point of these frames is the name search.
 *
 * Four frames, in the order a user meets them:
 *   1. the row in the launcher, before the view exists
 *   2. the view on entry — the newest artifacts, with nothing typed
 *   3. the view filtered by a typed name — two of five
 *   4. a name that matches nothing, with the way back to the listing offered
 *
 * Frame 4 is the one worth photographing most: a search that matched nothing used
 * to be the state with no selectable row at all, and this shows it is not.
 *
 * Usage: node scripts/capture-command-bar-artifacts.mjs [outDir]
 */
import { chromium } from 'playwright'
import { execFileSync } from 'node:child_process'
import { mkdirSync, rmSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/command-bar-artifacts'

mkdirSync(OUT, { recursive: true })

const SLOT = 'chat-1'

/** The launcher itself, so its overlay claims the host's quick-search slot. */
const APPS = [
  {
    name: 'command-bar',
    displayName: 'Command Bar',
    enabled: true,
    origin: 'builtin',
    source: 'builtin',
    version: '0.1.0',
    manifest: {
      name: 'command-bar',
      displayName: 'Command Bar',
      version: '0.1.0',
      ui: { overlays: [{ id: 'command-bar', replaces: 'quick-search' }] },
    },
  },
]

/**
 * Five artifacts, newest first, of which exactly TWO carry "revenue" in the name.
 *
 * The description on `Revenue Forecast Model` names a projection and the runbook's
 * body a checklist, neither of which appears in any name — so a frame showing
 * those rows under a name query would be showing a match this stage does not make.
 */
const ARTIFACTS = [
  {
    slug: 'trading-desk-dashboard', content: '<div style="font:14px system-ui">Positions, P&amp;L and risk tiles</div>', name: 'Trading Desk Dashboard', kind: 'widget',
    description: 'Positions, P&L and risk tiles', tags: [], version: 2,
    updated_at: '2026-09-18T09:40:00Z',
  },
  {
    slug: 'sydney-property-tracker', content: '<div style="font:14px system-ui">Inspection calendar and offers</div>', name: 'Sydney Property Tracker', kind: 'widget',
    description: 'Inspection calendar and offers', tags: [], version: 5,
    updated_at: '2026-09-17T18:05:00Z',
  },
  {
    slug: 'onboarding-runbook', content: '<div style="font:14px system-ui"># Onboarding\n\nDay one checklist.</div>', name: 'Onboarding Runbook', kind: 'markdown',
    description: 'How a new hire gets set up', tags: [], version: 1,
    updated_at: '2026-09-15T11:20:00Z',
  },
  {
    slug: 'revenue-forecast-model', content: '<div style="font:14px system-ui">Next four quarters</div>', name: 'Revenue Forecast Model', kind: 'widget',
    description: 'Next-four-quarters projection', tags: [], version: 3,
    updated_at: '2026-09-12T08:00:00Z',
  },
  {
    slug: 'q3-revenue-chart', content: '<div style="font:14px system-ui">Q3 revenue by region</div>', name: 'Q3 Revenue Chart', kind: 'widget',
    description: 'Bar chart of quarterly revenue by region', tags: [], version: 7,
    updated_at: '2026-09-10T16:30:00Z',
  },
]

/**
 * What the artifacts endpoint should do for the CURRENT section.
 *
 * Module-scoped rather than a parameter because `extra()` is built per context
 * and every section wants the same routing with one behaviour swapped. The states
 * this switches to are the ones a review cannot evaluate from prose: an instance
 * with nothing saved, and a search whose request was rejected.
 */
// More matches than ARTIFACTS_ROW_LIMIT (12), for the capped-list frame. Names all
// share a word so one query reaches every one of them.
const CROWDED = Array.from({ length: 17 }, (_, i) => ({
  ...ARTIFACTS[0],
  slug: `report-${i + 1}`,
  name: `Report ${i + 1} Revenue`,
  description: `Revenue report ${i + 1}`,
}))

let artifactsMode = 'ok'
/** Same, for the sessions search, whose failure surface this diff also changes. */
let sessionsMode = 'ok'

const { srv, base } = await serveDist()
// `chromiumSandbox: false` for the same reason `capture-flat-board.mjs` states:
// an AL2023 host cannot run Chromium's sandbox, and the launch dies with
// "No usable sandbox!" rather than producing a frame.
const browser = await chromium.launch({ chromiumSandbox: false })

/**
 * Every `/api/artifacts` request this harness answered, for the assertions below.
 *
 * Recorded with its query string because WHICH request matters. The chat page
 * behind the dialog has an in-session Artifacts tab of its own, and it asks for
 * `?session=<slot>` on load — a pre-existing, session-scoped read that has nothing
 * to do with the launcher. The invariant under test is narrower and is the one
 * `corpusWide` below picks out: the ROOT must issue no CORPUS-WIDE artifact query,
 * because that is the scan a keystroke could otherwise fan out to.
 */
const seen = []

/** A corpus-wide list: not scoped to one session's own artifacts. */
const corpusWide = search =>
  !new URLSearchParams(search).has('session')
  && !new URLSearchParams(search).has('touched_by')

async function openBar() {
  const context = await browser.newContext({
    viewport: { width: 1500, height: 950 }, deviceScaleFactor: 1,
  })
  const page = await context.newPage()

  const extra = async (path, route) => {
    if (path === '/api/apps') {
      await json(route, APPS)
      return true
    }
    if (path.startsWith('/api/sessions/search')) {
      if (sessionsMode === 'fail') {
        await json(route, { error: 'gateway unavailable' }, 502)
        return true
      }
      return false
    }
    if (path.startsWith('/api/artifacts')) {
      const url = new URL(route.request().url())
      // The DETAIL routes first, and each in its own shape. They were falling
      // through to the list response below, which is why pressing Enter used to
      // land on an error boundary: `ArtifactDetailPage` reads `artifact.tags.map`,
      // and a `{ artifacts: [...] }` body has no `tags`, so the page crashed on
      // camera. A demo that ends in a crash screen is worse than no demo.
      const detail = /^\/api\/artifacts\/([^/]+)(\/.*)?$/.exec(path)
      if (detail) {
        const [, slug, sub] = detail
        const hit = ARTIFACTS.find(a => a.slug === slug)
        if (!hit) {
          await json(route, { error: 'not found' }, 404)
          return true
        }
        if (sub === '/versions') {
          await json(route, { slug, versions: [hit.version] })
        } else if (sub === '/events') {
          await json(route, { slug, events: [] })
        } else if (sub === '/comments') {
          await json(route, { slug, comments: [] })
        } else if (sub) {
          await json(route, {})
        } else {
          await json(route, hit)
        }
        return true
      }
      seen.push(url.search)
      if (artifactsMode === 'fail') {
        await json(route, { error: 'gateway unavailable' }, 502)
        return true
      }
      const q = (url.searchParams.get('q') || '').toLowerCase()
      const pool = artifactsMode === 'empty' ? []
        : artifactsMode === 'crowded' ? CROWDED
        : ARTIFACTS
      const hits = q
        ? pool.filter(a => a.name.toLowerCase().includes(q))
        : pool
      await json(route, { artifacts: hits })
      return true
    }
    return false
  }

  await stubDashboardApi(page, {
    slots: [{ key: SLOT, messages: 0, running: false, agent: 'default', mode: '' }],
    extra,
  })

  await page.goto(`${base}/chat`)
  await page.waitForLoadState('networkidle')
  // The quick-search chord. The overlay claims the slot, so this opens the launcher.
  await page.keyboard.press('Control+k')
  await page.waitForSelector('[role="dialog"]', { timeout: 10_000 })
  return { context, page }
}

async function enterView(page) {
  await page.getByRole('combobox').fill('artifact')
  const row = page.getByRole('option').filter({ hasText: 'Search Artifacts' }).first()
  await row.waitFor({ timeout: 10_000 })
  // mousedown is what the list binds; a click would land after the blur.
  await row.dispatchEvent('mousedown')
  // The chip naming the view is what proves the scope was entered.
  await page.getByRole('button', { name: /Back to all commands/ }).waitFor({ timeout: 10_000 })
}

async function shot(page, name) {
  await page.waitForTimeout(400)
  const file = join(OUT, name)
  await page.screenshot({ path: file })
  console.log(`wrote ${file}`)
}

// ── 1. the row in the launcher, before the view exists ─────────────────────
{
  const { context, page } = await openBar()
  await page.getByRole('combobox').fill('artifact')
  await page.getByRole('option').filter({ hasText: 'Search Artifacts' }).first()
    .waitFor({ timeout: 10_000 })
  await shot(page, '1-row-in-launcher.png')
  // Typing a word that matches the COMMAND puts both artifact rows on one screen --
  // the command that opens the view and the fallback that carries this text into it.
  // Sharing one subtitle made a UX reader unable to "tell how their results would
  // differ", so each states its own thing. Read off the rendered rows, because the
  // duplication was only visible with both of them up.
  const commandRow = page.getByRole('option').filter({ hasText: 'Search Artifacts' }).first()
  const fallbackRow = page.getByRole('option').filter({ hasText: 'Search artifacts for' }).first()
  await fallbackRow.waitFor({ state: 'visible', timeout: 10_000 })
  const commandText = ((await commandRow.textContent()) || '').trim()
  const fallbackText = ((await fallbackRow.textContent()) || '').trim()
  const commandGloss = 'Browse and open the charts, documents and widgets you saved.'
  if (!commandText.includes(commandGloss)) {
    throw new Error(`the command row lost its corpus gloss: ${JSON.stringify(commandText)}`)
  }
  if (fallbackText.includes(commandGloss)) {
    throw new Error(`both artifact rows carry the same subtitle: ${JSON.stringify(fallbackText)}`)
  }
  if (!/matching that text/.test(fallbackText)) {
    throw new Error(`the fallback row does not say it carries the typed text: ${JSON.stringify(fallbackText)}`)
  }
  // The recovery row shares this frame, and a UX reader took its clipped tail for a
  // button. Its first words have to be the limitation, not the switch-off.
  const recovery = page.getByRole('option').filter({ hasText: 'turn full search back on' })
  if ((await recovery.count()) > 0) {
    const text = ((await recovery.first().textContent()) || '').trim()
    if (!text.startsWith('Knowledge')) {
      throw new Error(`the recovery row leads with the action, not the limit: ${JSON.stringify(text)}`)
    }
    if (/disabl/i.test(text)) {
      throw new Error(`the recovery row still says to switch the feature off: ${JSON.stringify(text)}`)
    }
    if (/App Store/i.test(text)) {
      throw new Error(`the recovery row frames the destination as a store: ${JSON.stringify(text)}`)
    }
  }
  // The launcher must have reached no corpus-wide artifact query to build that row.
  const early = seen.filter(corpusWide)
  if (early.length !== 0) {
    throw new Error(`the root issued ${early.length} corpus-wide query: ${early.join(', ')}`)
  }
  await context.close()
}

// ── 2. the view on entry: the newest artifacts, nothing typed ──────────────
{
  const { context, page } = await openBar()
  await enterView(page)
  await page.getByRole('option').filter({ hasText: 'Trading Desk Dashboard' }).first()
    .waitFor({ timeout: 10_000 })
  // The listing has to SAY what it is. Every group in the root announces itself,
  // and this list was unexplained rows until it did too.
  await page.getByText('Recent', { exact: true }).waitFor({ timeout: 10_000 })
  await shot(page, '2-view-listing.png')
  await context.close()
}

// ── 3. filtered by a typed name: two of five ───────────────────────────────
{
  const { context, page } = await openBar()
  await enterView(page)
  await page.getByRole('combobox').fill('revenue')
  await page.getByRole('option').filter({ hasText: 'Q3 Revenue Chart' }).first()
    .waitFor({ timeout: 10_000 })
  // The filter has to be visible as a filter: the other three must be gone.
  await page.getByRole('option').filter({ hasText: 'Onboarding Runbook' })
    .first().waitFor({ state: 'detached', timeout: 10_000 })
  await shot(page, '3-view-filtered.png')
  await context.close()
}

// ── 4. a name that matches nothing, with the way back offered ──────────────
{
  const { context, page } = await openBar()
  await enterView(page)
  await page.getByRole('combobox').fill('zzz-no-such-artifact')
  await page.getByRole('option').filter({ hasText: /show recent/ }).first()
    .waitFor({ timeout: 10_000 })
  await shot(page, '4-view-no-match.png')
  await context.close()
}

// ── 5. an instance with nothing saved ──────────────────────────────────────
//
// The state a UX review cannot evaluate from prose, and the one a new user meets
// first. It is reachable only here: a query that matched nothing shows frame 4's
// row, a failure shows frame 6, and a load in flight draws placeholders.
{
  artifactsMode = 'empty'
  const { context, page } = await openBar()
  await enterView(page)
  await page.getByText(/No artifacts yet/).waitFor({ timeout: 10_000 })
  await shot(page, '5-view-empty-corpus.png')
  await context.close()
  artifactsMode = 'ok'
}

// ── 6. a rejected search in the artifacts view ─────────────────────────────
//
// `ErrorNotice` above the listbox naming the corpus, and `Retry` still an option on
// the Arrow/Enter path. The ARTIFACTS scope only: frame 7 below is the evidence that
// the sessions scope was deliberately left on the failure row it already had.
{
  artifactsMode = 'fail'
  const { context, page } = await openBar()
  await enterView(page)
  await page.locator('[role="alert"]').filter({ hasText: 'Artifact search failed' }).first()
    .waitFor({ timeout: 10_000 })
  await page.getByRole('option').filter({ hasText: 'Retry' }).first()
    .waitFor({ timeout: 10_000 })
  await shot(page, '6-view-search-failed.png')
  await context.close()
  artifactsMode = 'ok'
}

// ── 7. the sessions view's failure, UNCHANGED by this diff ─────────────────
//
// An earlier round reshaped this surface too, on the argument that the two scopes
// should match. Review pointed out the flaw and it holds: this row's text was always
// the static `search_failed`, never a backend string, so nothing here misled the
// reader whose report motivated the artifacts wording -- they met the artifacts view.
// Changing it would have been a feature PR reworking a surface it does not own. So
// this frame exists to show the base row still rendering, and the probe below fails if
// the artifacts notice ever reaches this scope.
{
  sessionsMode = 'fail'
  const { context, page } = await openBar()
  await page.getByRole('combobox').fill('session')
  await page.getByRole('option').filter({ hasText: 'Search Sessions' }).first()
    .dispatchEvent('mousedown')
  await page.getByRole('combobox').fill('quarterly')
  await page.getByRole('option').filter({ hasText: 'Search failed' }).first()
    .waitFor({ timeout: 15_000 })
  // Scoped to the BAR. The page behind the overlay has live regions of its own, so
  // counting alerts document-wide reports a hit that has nothing to do with this
  // scope -- which is exactly what it did the first time this probe ran.
  if ((await page.locator('[role="dialog"] [role="alert"]').count()) > 0) {
    throw new Error('the artifacts failure notice reached the sessions scope')
  }
  await shot(page, '7-sessions-search-failed.png')
  await context.close()
  sessionsMode = 'ok'
}

// -- 8. the row cap, named --------------------------------------------------
//
// Seventeen names carry "revenue" and twelve rows fit. The line under the list
// says what the cap is holding back; before it, a crowded search was drawn exactly
// like a complete one. Outside the listbox, so it is not a row Enter cannot use.
{
  artifactsMode = 'crowded'
  const { context, page } = await openBar()
  await enterView(page)
  await page.getByRole('combobox').fill('revenue')
  await page.getByText('+5 more', { exact: false }).first()
    .waitFor({ state: 'visible', timeout: 10_000 })
  await shot(page, '8-view-row-cap.png')
  await context.close()
  artifactsMode = 'ok'
}

// -- 9. an artifact's NAME typed at the root ---------------------------------
//
// The state this round adds. The root ranks launcher rows on their own vocabulary,
// so "Q3 Revenue" matches no command: before this row, a reader who typed what they
// call the thing reached a dead end unless they first guessed the word "artifact".
// Two ways out now sit together, each naming its own corpus, and the footer names
// whichever one is selected. Still no corpus-wide query: the row advertises the
// view without reaching it.
{
  const { context, page } = await openBar()
  const before = seen.filter(corpusWide).length
  await page.getByRole('combobox').fill('Q3 Revenue')
  const row = page.getByRole('option').filter({ hasText: 'Search artifacts for' }).first()
  await row.waitFor({ state: 'visible', timeout: 10_000 })
  // Select it, so the frame also shows the footer naming this Enter.
  const rows = await page.getByRole('option').all()
  let index = -1
  for (let i = 0; i < rows.length; i++) {
    if (((await rows[i].textContent()) || '').includes('Search artifacts for')) {
      index = i
      break
    }
  }
  if (index < 0) throw new Error('no artifacts fallback row to select')
  for (let i = 0; i < index; i++) await page.keyboard.press('ArrowDown')
  await page.getByText('Search Artifacts', { exact: true }).last()
    .waitFor({ state: 'visible', timeout: 10_000 })
  await shot(page, '9-root-artifact-name.png')
  const after = seen.filter(corpusWide).length
  if (after !== before) {
    throw new Error(`offering the fallback row issued ${after - before} corpus-wide query`)
  }
  await context.close()
}

// -- 10. the dead-end query, where the recovery row is the whole answer -------
//
// This row names the corpora the launcher cannot reach, and it is the only row that
// is a sentence rather than a label. It failed two readings in a row. Truncated to
// one line it ended at "...disable Command Bar", which a UX test reader took for a
// button that would switch something off. Front-loaded and given a second line, the
// instruction that now survived still read as that switch: "I don't know how to get
// it back... I would not click it." Reworded to name the App Store, a third reader read
// that as an install or a purchase: "App Store sounds like it might want me to install or
// buy something." The destination is not a store -- it is AppDetailPage at
// /apps/detail/command-bar, this app's own page under Apps, carrying its Enabled switch.
// So the row names that section, and neither "disable" nor "App Store" may come back.
// page, so the sentence says that, and the end-state verb is gone from the copy.
//
// Measured, not asserted from a class name: this checks the rendered row is not
// clipped, at the real width, in the real browser -- a thing jsdom cannot answer
// because it has no layout. The wrap is load-bearing at every length here: the
// English sentence is 116 characters and es/fr/de reach 149/146/136, and a narrower
// window shrinks the popover further. The probe after the shot grows the rendered
// text and checks a longer string takes a second line instead of losing its tail.
{
  const { context, page } = await openBar()
  await page.getByRole('combobox').fill('zzzz-no-corpus-here')
  const hint = page.getByRole('option').filter({ hasText: 'turn full search back on' }).first()
  await hint.waitFor({ state: 'visible', timeout: 10_000 })

  const text = ((await hint.textContent()) || '').trim()
  if (!text.startsWith('Knowledge')) {
    throw new Error(`the hint does not lead with what is missing: ${JSON.stringify(text)}`)
  }
  if (/disabl/i.test(text)) {
    throw new Error(`the hint still tells the reader to switch the feature off: ${JSON.stringify(text)}`)
  }
  if (/App Store/i.test(text)) {
    throw new Error(`the hint frames the destination as a store, which read as a purchase: ${JSON.stringify(text)}`)
  }

  const shown = await hint.evaluate(row => {
    const title = row.querySelector('span.line-clamp-2')
    if (!title) throw new Error('the hint row is not the wrapping row')
    return {
      clipped: title.scrollWidth > title.clientWidth + 1 || title.scrollHeight > title.clientHeight + 1,
      height: Math.round(row.getBoundingClientRect().height),
    }
  })
  if (shown.clipped) throw new Error('the hint is clipped as rendered, which is the whole finding')

  await shot(page, '10-root-recovery-hint.png')

  // Now the capability, on the real element: a longer sentence must take a second
  // line rather than lose its end. Restored immediately, and the frame above was
  // already taken, so nothing in the evidence shows the probe's text.
  const probe = await hint.evaluate(row => {
    const title = row.querySelector('span.line-clamp-2')
    const original = title.textContent
    const one = Math.round(title.getBoundingClientRect().height)
    title.textContent = `${original} ${original}`
    const two = Math.round(title.getBoundingClientRect().height)
    const clipped = title.scrollHeight > title.clientHeight + 1
    title.textContent = original
    return { one, two, clipped }
  })
  if (!(probe.two > probe.one)) {
    throw new Error(`a doubled sentence did not take another line: ${probe.two}px vs ${probe.one}px`)
  }
  console.log(
    `  hint: one line at ${probe.one}px in English, ${probe.two}px when doubled, tail kept`,
  )
  await context.close()
}

// ── 11. the whole flow, as a video ─────────────────────────────────────────
//
// A still cannot prove a flow: what this change is FOR is that four keystrokes
// reach a saved artifact, and only moving pictures show the list narrowing as the
// name is typed. Two details are deliberate, both learned by getting them wrong:
// under `recordVideo` the codec's overhead makes `waitForSelector`'s default
// `visible` state time out even though the dialog is already mounted, so this
// waits on a locator instead; and Enter lands on the artifact's own page, which
// only renders because the detail routes above answer in their real shapes —
// without them the demo ended on an error boundary.
{
  const context = await browser.newContext({
    viewport: { width: 1500, height: 950 },
    deviceScaleFactor: 1,
    recordVideo: { dir: OUT, size: { width: 1500, height: 950 } },
  })
  const page = await context.newPage()

  const extra = async (path, route) => {
    if (path === '/api/apps') {
      await json(route, APPS)
      return true
    }
    if (path.startsWith('/api/artifacts')) {
      const url = new URL(route.request().url())
      const detail = /^\/api\/artifacts\/([^/]+)(\/.*)?$/.exec(path)
      if (detail) {
        const [, slug, sub] = detail
        const hit = ARTIFACTS.find(a => a.slug === slug)
        if (!hit) {
          await json(route, { error: 'not found' }, 404)
        } else if (sub === '/versions') {
          await json(route, { slug, versions: [hit.version] })
        } else if (sub === '/events') {
          await json(route, { slug, events: [] })
        } else if (sub === '/comments') {
          await json(route, { slug, comments: [] })
        } else if (sub) {
          await json(route, {})
        } else {
          await json(route, hit)
        }
        return true
      }
      const q = (url.searchParams.get('q') || '').toLowerCase()
      await json(route, {
        artifacts: q ? ARTIFACTS.filter(a => a.name.toLowerCase().includes(q)) : ARTIFACTS,
      })
      return true
    }
    return false
  }

  await stubDashboardApi(page, {
    slots: [{ key: SLOT, messages: 0, running: false, agent: 'default', mode: '' }],
    extra,
  })

  /** Open the launcher on /chat. Reused for the second beat. */
  const openLauncher = async () => {
    await page.goto(`${base}/chat`)
    await page.waitForLoadState('networkidle')
    await page.waitForTimeout(700)
    await page.keyboard.press('Control+k')
    await page.locator('[role="dialog"]').first().waitFor({ timeout: 15_000 })
    await page.waitForTimeout(500)
  }
  /** Type at reading speed, so the viewer can follow the list narrowing. */
  const say = async (text) => {
    await page.getByRole('combobox').click()
    await page.keyboard.type(text, { delay: 95 })
  }

  // Beat one: find the row, enter the view, narrow by name, open the artifact.
  await openLauncher()
  await say('artifact')
  await page.getByRole('option').filter({ hasText: 'Search Artifacts' }).first()
    .waitFor({ timeout: 15_000 })
  await page.waitForTimeout(1000)
  await page.getByRole('option').filter({ hasText: 'Search Artifacts' }).first()
    .dispatchEvent('mousedown')
  await page.getByText('Recent', { exact: true }).waitFor({ timeout: 15_000 })
  await page.waitForTimeout(1400)
  await say('revenue')
  await page.getByRole('option').filter({ hasText: 'Q3 Revenue Chart' }).first()
    .waitFor({ timeout: 15_000 })
  await page.waitForTimeout(1400)
  await page.getByRole('combobox').press('ArrowDown')
  await page.waitForTimeout(600)
  await page.getByRole('combobox').press('Enter')
  await page.waitForURL(/\/artifacts\//, { timeout: 15_000 })
  await page.waitForTimeout(2200)

  // Beat two: a name that matches nothing still has somewhere to go.
  await openLauncher()
  await say('artifact')
  await page.getByRole('option').filter({ hasText: 'Search Artifacts' }).first()
    .waitFor({ timeout: 15_000 })
  await page.getByRole('option').filter({ hasText: 'Search Artifacts' }).first()
    .dispatchEvent('mousedown')
  await page.getByText('Recent', { exact: true }).waitFor({ timeout: 15_000 })
  await say('zzz-no-such-artifact')
  await page.getByRole('option').filter({ hasText: /show recent/ }).first()
    .waitFor({ timeout: 15_000 })
  await page.waitForTimeout(2000)

  const video = page.video()
  await context.close()
  const raw = await video.path()
  console.log(`\nraw video: ${raw}`)
  // mp4 for the PR: GitHub renders it inline as a player; it will not play a webm
  // from an attachment. `-an` because there is no audio and a silent track only
  // makes some players show a muted control the reader wonders about.
  const mp4 = join(OUT, 'command-bar-artifacts-demo.mp4')
  execFileSync('ffmpeg', [
    '-y', '-loglevel', 'error', '-i', raw,
    '-vf', 'scale=1500:-2,fps=24', '-c:v', 'libx264', '-preset', 'veryfast',
    '-crf', '26', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', '-an', mp4,
  ])
  rmSync(raw, { force: true })
  console.log(`wrote ${mp4}`)
}

// Every request this feature made, printed so the frames are not the only
// evidence: no `content=1` and no `snippet=1` is the stage's whole claim.
console.log('\nartifact requests made:', JSON.stringify(seen))
console.log('corpus-wide ones (the bar\'s):', JSON.stringify(seen.filter(corpusWide)))
const leaky = seen.filter(s => s.includes('content=') || s.includes('snippet='))
if (leaky.length) {
  throw new Error(`asked the server for a content scan: ${leaky.join(', ')}`)
}

await browser.close()
srv.close()

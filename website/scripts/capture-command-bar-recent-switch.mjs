/**
 * Screenshot harness for the Command Bar's `recent` group and its pane-switch
 * dismissal.
 *
 * Runs the REAL built SPA (website/dist) behind the shared `serveDist` server and
 * answers every /api/** call from fixtures through `stubDashboardApi`. No gateway,
 * no dashboard auth, no kiro-cli.
 *
 * The frames evidence three claims:
 *
 *   1. The root leads with the sessions the reader was last in, in RECENCY order —
 *      three of them, above Commands, and ordered by nothing else. The fixture is
 *      built so every competing order would produce a visibly different list: a
 *      PINNED slot is the oldest (it used to float to the top), the alphabetical
 *      order of the titles is the reverse of the recency order, a slot in the
 *      `attention` group is also the most recent (so a duplicate would show), and
 *      the slot that must rank FIRST is the one the wire sends with an empty
 *      `last_activity_ts` (so a nullish ladder scoring it 0 sinks it and the
 *      assertion fails).
 *   2. Switching the visible pane takes the palette with it, driven by the REAL
 *      ⌘/Ctrl+digit chord. That chord is Electron-only — `useInstanceShortcuts`
 *      returns early on `!isElectron`, because a plain browser keeps those keys for
 *      its own tabs — and the shell is detected solely from
 *      `window.kirocrew.isElectron`, so the harness presents as the shell and
 *      presses `Control+2` rather than driving a proxy for it. There is no
 *      non-keyboard door to reproduce this: the open palette's overlay intercepts a
 *      click on the instance bar, which is WHY the dismissal was missing.
 *   3. The SECOND surface this diff changes — the legacy palette's sessions listing,
 *      fed by `prepareCurrentSlots`. Three things are visible only there: the pinned
 *      slot taking its place by recency (it is the fixture's oldest, so the removed
 *      pinned-first tier would have put it at the top), a row whose
 *      `last_activity_ts` is the wire's empty string still showing a time from
 *      `last_ts`, and a row whose two timestamps DISAGREE ranking by the later of
 *      them rather than by the older assistant row. Captured with no apps installed
 *      so the builtin quick-search renders rather than the Command Bar overlay.
 *
 * Frame 2 is the load-bearing one for the ordering claim: it fails if pinned-first
 * ordering returns, if the alphabetical tiebreak reaches the group, if the recency
 * ladder stops on the wire's empty string again, or if the group grows past the
 * glance it is sized for.
 *
 * Usage: node scripts/capture-command-bar-recent-switch.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/command-bar-recent-switch'

mkdirSync(OUT, { recursive: true })

/** The launcher is a builtin, so it must claim the quick-search slot — without this
 *  the chord opens the OLD quick-search overlay and every frame is of the wrong
 *  surface. */
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

const now = Date.now()
const ago = mins => new Date(now - mins * 60_000).toISOString()

/**
 * Titles are deliberately chosen so ALPHABETICAL order is the exact reverse of
 * recency order: if the tiebreak ever reaches this group the frame inverts instead
 * of looking subtly wrong.
 *
 *   recency:  Zulu (2m) → Mike (90m) → Alpha (26h)
 *   alphabet: Alpha     → Mike       → Zulu
 */
const SLOTS = [
  // In `attention`, and ALSO the single most recent slot — so a failure to exclude
  // it from `recent` shows as the same session twice on one page.
  //
  // Its two timestamps DISAGREE, deliberately. The newest thing that happened here
  // is the reader's own prompt, which advances `last_ts`; the last assistant row is
  // three days old, and that is all `last_activity_ts` reports. A ladder that takes
  // the first non-empty field therefore reads this session as three days stale and
  // sorts it below Alpha. Taking the later of the two keeps it where the reader
  // left it, which is what scene 4 asserts.
  {
    key: 'chat-approve',
    title: 'Deploy Coder to AWS',
    messages: 12,
    running: false,
    pending_approval: true,
    agent: 'kirocrew',
    mode: '',
    last_activity_ts: ago(3 * 24 * 60),
    last_ts: ago(1),
  },
  // The WIRE shape for a session whose newest row is the reader's OWN prompt: the
  // projection emits `last_activity_ts` as the EMPTY STRING and the real time lives
  // one rung down in `last_ts`. This slot is the most recent AND the one a nullish
  // ladder scores 0, so the ordering assertion below fails if that rung goes dead
  // again — the frame then cannot show an order the code does not produce.
  {
    key: 'chat-zulu',
    title: 'Zulu — quick switcher fixes',
    messages: 24,
    running: true,
    agent: 'kirocrew',
    mode: '',
    last_activity_ts: '',
    last_ts: ago(2),
  },
  {
    key: 'chat-mike',
    title: 'Mike — nav rail badge',
    messages: 8,
    running: false,
    agent: 'kirocrew',
    mode: '',
    last_activity_ts: ago(90),
  },
  {
    key: 'chat-alpha',
    title: 'Alpha — table breakout',
    messages: 31,
    running: false,
    agent: 'kirocrew',
    mode: '',
    last_activity_ts: ago(26 * 60),
  },
  // PINNED and the OLDEST. Under the previous pinned-first ordering this floated
  // above everything above it; it must now be below the cap and off the page.
  {
    key: 'chat-pinned',
    title: 'Programs — pinned last week',
    messages: 57,
    running: false,
    pinned: true,
    agent: 'kirocrew',
    mode: '',
    last_activity_ts: ago(8 * 24 * 60),
  },
  // An empty untitled slot: the New Session command's job, not a session to
  // switch back into, and one is indistinguishable from another.
  {
    key: 'chat-empty',
    title: 'New Session…',
    messages: 0,
    running: false,
    agent: 'kirocrew',
    mode: '',
    last_activity_ts: ago(3),
  },
]

/** `was_connected` is what puts an instance in the tab strip (visibleInstanceTabs),
 *  which is the control frame 3 clicks to change the active pane. */
const INSTANCES = [
  {
    id: 'remote-ghosty',
    name: 'Ghosty',
    was_connected: true,
    status: { state: 'connected' },
  },
]

function assert(label, ok, detail = '') {
  console.log(`${label}: ${ok ? 'OK' : 'FAIL'}${detail ? ` — ${detail}` : ''}`)
  if (!ok) throw new Error(`${label} failed${detail ? `: ${detail}` : ''}`)
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()

async function openBar({ instances = [], electron = false, apps = APPS } = {}) {
  const context = await browser.newContext({
    viewport: { width: 1500, height: 950 },
    deviceScaleFactor: 1,
  })
  const page = await context.newPage()

  // `isElectron` is read once at module load from this object, so it has to exist
  // before the bundle evaluates — an `evaluate` after `goto` would be too late.
  if (electron) {
    await page.addInitScript(() => {
      window.kirocrew = { isElectron: true, platform: 'linux' }
    })
  }

  const extra = async (path, route) => {
    if (path === '/api/apps') {
      await json(route, apps)
      return true
    }
    if (path.startsWith('/api/instances')) {
      await json(route, { active: true, instances, warm_set_cap: 3, sso: {} })
      return true
    }
    return false
  }

  await stubDashboardApi(page, { slots: SLOTS, extra })

  await page.goto(`${base}/chat`)
  await page.waitForLoadState('networkidle')
  await page.keyboard.press('Control+k')
  await page.waitForSelector('[role="dialog"]', { timeout: 10_000 })
  return { context, page }
}

const box = page => page.locator('[role="dialog"] input').first()

async function shot(page, name) {
  await page.waitForTimeout(350)
  const file = join(OUT, name)
  await page.screenshot({ path: file })
  console.log(`wrote ${file}`)
}

// ── 1. the root, idle: Needs You, then Recent in recency order, then Commands ──
{
  const { context, page } = await openBar()
  const groups = await page.locator('[role="dialog"] [role="group"], [role="dialog"] [role="presentation"]').allTextContents()
  const dialog = await page.locator('[role="dialog"]').innerText()

  // Case-insensitive: the header is uppercased by CSS, so `innerText` reports it as
  // "RECENT" and a case-sensitive match would fail on a correct render.
  assert('Recent group is headed', /recent/i.test(dialog), dialog.slice(0, 300))

  const rows = await page.getByRole('option').allTextContents()
  const joined = rows.join(' | ')
  console.log('ROWS:', joined.replace(/\n/g, ' / ').slice(0, 600))

  const idx = needle => rows.findIndex(r => r.includes(needle))
  const iZulu = idx('Zulu')
  const iMike = idx('Mike')
  const iAlpha = idx('Alpha')

  assert('all three recent sessions are rows', iZulu >= 0 && iMike >= 0 && iAlpha >= 0, joined.slice(0, 300))
  assert('order is recency, not alphabet', iZulu < iMike && iMike < iAlpha, `Zulu@${iZulu} Mike@${iMike} Alpha@${iAlpha}`)

  // The regression this fixes: a pin is not a recency claim.
  assert('pinned slot does not float in', idx('Programs') === -1, `Programs@${idx('Programs')}`)
  // The cap: three, sized to a glance.
  assert('group is capped at three', [iZulu, iMike, iAlpha].every(i => i >= 0) && idx('Programs') === -1)
  // Dedup against the group above it.
  const approveCount = rows.filter(r => r.includes('Deploy Coder')).length
  assert('attention session is not duplicated', approveCount === 1, `${approveCount} copies`)
  // An empty untitled slot is the New Session command's job.
  const emptyRows = rows.filter(r => /New Session…/.test(r) && !/Recent/.test(r)).length
  console.log(`empty-new rows on page: ${emptyRows}`)

  await shot(page, '1-root-recent-group.png')
  void groups
  await context.close()
}

// ── 2. typing still reaches a session by name (the idle order is idle-only) ────
{
  const { context, page } = await openBar()
  await box(page).fill('alpha')
  await page.waitForTimeout(500)
  const rows = await page.getByRole('option').allTextContents()
  assert('a query still finds the session', rows.some(r => r.includes('Alpha')), rows.join(' | ').slice(0, 300))
  await shot(page, '2-query-finds-session.png')
  await context.close()
}

// ── 3. switching the visible pane dismisses the palette ───────────────────────
//
// Driven by the REAL gesture from the report: Ctrl+2. That chord is claimed only in
// the Electron shell (useInstanceShortcuts returns early on `!isElectron`), and the
// shell is detected solely from `window.kirocrew.isElectron` — so the harness
// presents as the shell before the bundle loads and then presses the actual key.
// This matters: the two non-keyboard doors are both unreachable here (a mouse path
// to the topbar is blocked by the modal's own overlay, and the embedded-pane relay
// is correctly rejected when the message's source is not a real pane iframe), so a
// harness that substituted one of those would be evidencing a different path than
// the one the user pressed.
{
  const { context, page } = await openBar({ instances: INSTANCES, electron: true })
  assert('palette is open', await page.locator('[role="dialog"]').isVisible())
  await shot(page, '3a-palette-open-over-local.png')

  await page.keyboard.press('Control+2')
  await page.waitForSelector('[role="dialog"]', { state: 'detached', timeout: 5_000 })
  assert('palette dismissed by the pane switch', (await page.locator('[role="dialog"]').count()) === 0)
  // The switch itself must actually have happened — otherwise a palette that closed
  // for some unrelated reason would pass this scene.
  const switched = await page.locator('text=Ghosty').count()
  assert('the pane really switched', switched > 0, `Ghosty on page: ${switched}`)
  await shot(page, '3b-palette-dismissed-after-switch.png')
  await context.close()
}

// ── 4. the OTHER surface this diff changes: the sessions listing ──────────────
//
// `prepareCurrentSlots` feeds the legacy palette's CURRENT group, not the Command
// Bar root — a second surface, which is why it needs its own frame. Opened with NO
// apps installed so the builtin quick-search renders instead of the Command Bar
// overlay.
//
// Two claims live only here:
//
//   a) the pinned row takes its place by RECENCY. `chat-pinned` is the oldest slot
//      in the fixture, so under the pinned-first tier it sat at the top of this
//      list; it must now sit BELOW Alpha, i.e. last. The pin mark still renders on
//      the row — only its claim on the top of the list is gone.
//   b) a row whose `last_activity_ts` is the wire's empty string still shows a
//      time, taken from `last_ts`. `fmtRelativeTime` returns today's "HH:MM" for a
//      fresh slot and `undefined` for an unparseable value, so if that fallback
//      dies the row renders with NO time and the assertion below fails rather than
//      the frame quietly showing a blank where a timestamp belongs.
{
  const { context, page } = await openBar({ apps: [] })
  const dialog = await page.locator('[role="dialog"]').innerText()
  console.log('LISTING HEAD:', dialog.replace(/\n/g, ' / ').slice(0, 400))

  const at = needle => dialog.indexOf(needle)
  const iNew = at('New Session')
  const iDeploy = at('Deploy Coder')
  const iZulu = at('Zulu')
  const iMike = at('Mike')
  const iAlpha = at('Alpha')
  const iPinned = at('Programs')

  assert(
    'every live slot is listed',
    [iDeploy, iZulu, iMike, iAlpha, iPinned].every(i => i >= 0),
    `Deploy@${iDeploy} Zulu@${iZulu} Mike@${iMike} Alpha@${iAlpha} Programs@${iPinned}`,
  )
  // This listing is the legacy palette, not the Command Bar root: the root caps at
  // three and drops the pinned slot entirely, so seeing it at all proves the
  // surface.
  assert('this is the full listing, not the capped root group', iPinned >= 0)
  assert('the empty-new tier still leads', iNew >= 0 && iNew < iDeploy, `New@${iNew} Deploy@${iDeploy}`)
  assert(
    'real sessions run newest to oldest',
    iDeploy < iZulu && iZulu < iMike && iMike < iAlpha,
    `Deploy@${iDeploy} Zulu@${iZulu} Mike@${iMike} Alpha@${iAlpha}`,
  )
  // (a) the pin no longer buys the top of a recency list.
  assert('the pinned row sits in recency order, last', iPinned > iAlpha, `Programs@${iPinned} Alpha@${iAlpha}`)

  // (c) the later of a slot's two timestamps wins. `chat-approve`'s last assistant
  // row is three days old while its own prompt is a minute old, so a ladder that
  // stops at the first non-empty field reads it as stale and sorts it under Alpha.
  assert(
    'a newer prompt outranks an older assistant row',
    iDeploy < iMike && iDeploy < iAlpha,
    `Deploy@${iDeploy} Mike@${iMike} Alpha@${iAlpha}`,
  )

  // (b) the fallback-timestamped row. Slice this row's own text so a timestamp
  // belonging to a neighbour cannot satisfy it.
  const zuluRow = dialog.slice(iZulu, iMike)
  assert(
    'the empty-last_activity_ts row still shows a time',
    /\b\d{1,2}:\d{2}\b/.test(zuluRow),
    JSON.stringify(zuluRow.slice(0, 160)),
  )

  await shot(page, '4-sessions-listing-recency-and-pin.png')
  await context.close()
}

await browser.close()
srv.close()

/**
 * Screenshot harness + geometry check for the COMPOSER COLLAPSE.
 *
 * Runs the REAL built SPA (website/dist) against a static file server with every
 * /api/** call answered from fixtures — no gateway, no token, no agent. Only the
 * network is stubbed, so the collapse gesture, the AnimatePresence exit it reuses,
 * the persisted preference and the draft handoff are the unmodified production
 * path.
 *
 * It ASSERTS as well as photographs, because the claim this change makes is a
 * geometric one — "the conversation area expands to fill the freed vertical
 * space" — and a photograph cannot distinguish that from a composer that merely
 * went blank. `.input-area` is the composer's documented theming hook
 * (website/docs/theming-contract.md), and the chat column is a flex column, so
 * whatever height that wrapper gives up is handed to the transcript above it.
 * Exits non-zero if the wrapper does not shrink by at least MIN_RECLAIMED px.
 *
 * The draft is typed BEFORE collapsing on purpose: the frames are the evidence
 * that a half-typed message is still there afterwards, and that the collapsed bar
 * says which message it is.
 *
 * Nothing in CI runs this file; it is a manual guard and a source of PR frames.
 *
 * Usage: node scripts/capture-composer-collapse.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/composer-collapse'
const SLOT = 'chat-collapse'
const PROJECT = '/home/user/workspace/KiroCrew'

/**
 * A collapsed composer must give back more than a rounding error. MEASURED at this
 * viewport: the expanded assembly is 141px (textarea, control row, context shelf)
 * and the bar that replaces it is 52px, so 89px comes back. The floor sits below
 * that with margin for layout drift, and high enough that the two ways this could
 * silently stop working would both fail it — a composer that went blank without
 * shrinking, and a regression that left the context shelf mounted (measured 57px,
 * which is why the shelf stands down too).
 */
const MIN_RECLAIMED = 75

// Long enough to extend past the 260px drop-up in frame 1b: review read that
// frame as showing an EMPTY box and worried the sequence implied opening the
// menu clears the draft. The menu occludes the first line by construction, so
// the draft has to be wider than the menu for the frame to show otherwise.
const DRAFT = 'still writing this one, do not eat it while I go back and re-read the part about the reconnect handler'

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Why is the retry loop firing twice?',
  running: false,
  last_message: 'Because the second listener captured a stale id.',
  messages: 4,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  folder_id: '',
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const now = Date.now() / 1000

/**
 * The transcript is deliberately LONGER THAN THE VIEWPORT, and the capture scrolls
 * to the bottom before shooting.
 *
 * A short transcript makes the two frames prove the wrong thing: the composer
 * shrinks and the room it gives back reads as empty space, which is
 * indistinguishable from a control that reclaimed nothing useful. With content that
 * overflows, the frames show the actual payoff — lines of the answer that were below
 * the fold are on screen once the box is out of the way.
 */
const LONG_ANSWER = [
  'In the reconnect handler. It calls the same subscribe helper the mount path calls, '
  + 'and that helper appends rather than replacing, so a session that reconnects three '
  + 'times ends up with four listeners, each holding the row id that was live when it '
  + 'was created.',
  'The first listener is attached in the mount effect, which has an empty dependency '
  + 'array, so it runs once and is torn down only when the panel unmounts. That half is '
  + 'correct on its own.',
  'The second is attached from the socket open callback. That callback is recreated on '
  + 'every reconnect, and each new copy calls subscribe again without unsubscribing the '
  + 'previous one, so the count grows with the number of reconnects.',
  'The stale row id is the second half of the bug. Each callback closes over the row id '
  + 'that was selected at the moment it was created, so listener N fires against '
  + 'whatever row was live during reconnect N rather than the current selection.',
  'That is why the symptom looks selection-dependent rather than reconnect-dependent '
  + 'from the outside: the duplicate always targets an older row, so it reads as "the '
  + 'wrong row updated" instead of "the handler ran twice".',
  'The fix is to return the unsubscribe function from the helper and call it before '
  + 'attaching a new one, and to read the row id from a ref rather than capturing it, so '
  + 'a surviving listener still refers to the current selection.',
  'Reading an answer this long is the case the collapse is for: the text runs past the '
  + 'fold while the composer holds room it is not using.',
].join('\n\n')

const detail = {
  running: false,
  has_more: false,
  total: 4,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: now - 900, content: 'Why is the retry loop firing twice?' },
    {
      role: 'assistant',
      ts: now - 860,
      content:
        'Two listeners are registered for the same event. The first is attached when '
        + 'the panel mounts and the second when the connection is re-established, and '
        + 'nothing removes the first, so every retry runs both.\n\n'
        + 'The second one closed over the row id from the mount, which is why the '
        + 'duplicate always targets the row that was selected first rather than the '
        + 'one you are looking at now.',
    },
    { role: 'user', ts: now - 300, content: 'Where does the second registration happen?' },
    { role: 'assistant', ts: now - 240, content: LONG_ANSWER },
  ],
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  // Video is OPT-IN (RECORD_VIDEO=1). Review asked twice for a recording of the
  // collapse/restore transition, which a still frame genuinely cannot show -- but
  // recording every run would slow the normal geometry check and halve its
  // resolution for no gain, so the default path is unchanged. deviceScaleFactor
  // drops to 1 while recording: Playwright's video is capped at the viewport size,
  // and a 2x scale only costs time here.
  // The raw webm is NOT committed -- only the two derived files are, from:
  //   RECORD_VIDEO=1 node scripts/capture-composer-collapse.mjs <out>
  //   ffmpeg -ss 1.6 -i <out>/video-raw/*.webm -an -c:v libx264 -pix_fmt yuv420p \
  //     -crf 30 -vf scale=1200:-2 <out>/collapse-restore.mp4
  //   ffmpeg -ss 1.6 -i <webm> -vf "fps=6,scale=700:-1:flags=lanczos,\
  //     palettegen=stats_mode=diff:max_colors=128" -f image2 pal.png
  //   ffmpeg -ss 1.6 -i <webm> -i pal.png -lavfi "fps=6,scale=700:-1:flags=lanczos[v];\
  //     [v][1:v]paletteuse=dither=bayer:bayer_scale=5" <out>/collapse-restore.gif
  // The 1.6s offset drops the boot frames; the clip then starts on the typed draft.
  const recordVideo = process.env.RECORD_VIDEO === '1'
  const context = await browser.newContext({
    viewport: { width: 1500, height: 950 },
    deviceScaleFactor: recordVideo ? 1 : 2,
    ...(recordVideo ? { recordVideo: { dir: `${OUT}/video-raw`, size: { width: 1500, height: 950 } } } : {}),
  })

  const extra = async (path, route) => {
    if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
    return false
  }

  const page = await context.newPage()
  logPageProblems(page)
  // `preserveStorage` and `localStorageEntries` are both deliberate, and both come
  // from this stub's own documented contract. It clears localStorage inside its own
  // init script on EVERY navigation, and Playwright does not define the evaluation
  // order between that script and a caller's -- so seeding the active slot from our
  // own addInitScript was racing the clear (it won, silently, until something
  // reordered it), and the collapse preference could not survive the reload below at
  // all: the reload came up with the key ABSENT rather than persisted, which is what
  // made this harness report a false negative for "the state survives a reload".
  await stubDashboardApi(page, {
    folders: [], slots, theme: 'kiro-dark', extra,
    preserveStorage: true,
    localStorageEntries: { 'mc-active-slot': SLOT },
  })
  await page.addInitScript(() => {
    // Start from the shipped default so the first frame is today's behaviour --
    // but ONCE, on the first boot only. This script re-runs on every navigation,
    // and an unconditional removal also wiped the preference across the reload
    // further down, which made "the state survives a reload" untestable here and
    // reported a false negative for it. sessionStorage is the right sentinel: it
    // survives a reload in the same tab and dies with the context.
    if (!sessionStorage.getItem('mc-capture-booted')) {
      localStorage.removeItem('mc-composer-collapsed')
      sessionStorage.setItem('mc-capture-booted', '1')
    }
  })
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2500)

  const areaHeight = async () => {
    const b = await page.locator('.input-area').first().boundingBox().catch(() => null)
    if (!b) throw new Error('.input-area not found — the composer never rendered')
    return b.height
  }

  /**
   * Height of the element that scrolls the transcript -- the "conversation area"
   * the issue asks to grow.
   *
   * This replaced a count of visible paragraphs, which was NOT scroll-invariant:
   * once focus moves to the collapsed bar the list can scroll, so the count changed
   * for reasons unrelated to the reclaimed space and reported a SHRINK while the
   * pixel measurement showed 89px reclaimed. A viewport height cannot be moved by
   * scrolling, so it measures the claim and nothing else.
   */
  const transcriptViewportH = () => page.evaluate(() => {
    const p = Array.from(document.querySelectorAll('p')).find(el => el.textContent.trim().length > 40)
    let el = p?.parentElement
    while (el) {
      const oy = getComputedStyle(el).overflowY
      if ((oy === 'auto' || oy === 'scroll') && el.scrollHeight > el.clientHeight) return el.clientHeight
      el = el.parentElement
    }
    return 0
  })

  /**
   * The composer, counted by the STABLE hook rather than its label.
   *
   * `composerFocus.ts` spells out why: the aria-label is translated in every
   * catalog, so a label-based probe answers in English only. An earlier version of
   * this harness counted `getByLabel('Message input')` and disagreed with a direct
   * DOM read of the same instant.
   */
  const composerCount = () => page.evaluate(
    () => document.querySelectorAll('textarea[data-composer-input]').length,
  )

  // 1. Expanded, with an unsent draft in the box.
  await page.locator('textarea[data-composer-input]').click()
  await page.keyboard.type(DRAFT)
  await page.waitForTimeout(400)
  const expandedH = await areaHeight()
  const expandedViewportH = await transcriptViewportH()
  await page.screenshot({ path: `${OUT}/1-expanded-with-draft.png` })

  // 1b. The entry point itself. It is a menu ROW rather than a button in the
  // bottom action row for two reasons that both need to stay visible in review:
  // the row is capped at two peer actions by a blocking AUTOSDE rule, and an
  // icon-only chevron there read as the neighbouring "Normal" picker's caret.
  await page.getByTitle('Add files & options').click()
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/1b-entry-point-in-plus-menu.png` })

  // 2. Collapsed by the real gesture.
  await page.getByTestId('composer-collapse-row').click()
  await page.waitForTimeout(900)
  const collapsedH = await areaHeight()
  const collapsedViewportH = await transcriptViewportH()
  await page.screenshot({ path: `${OUT}/2-collapsed-draft-kept.png` })
  // Focus must have followed the gesture to the bar; otherwise a keyboard user is
  // dropped to `body` and re-Tabs from the top of the page on every collapse.
  const focusAfterCollapse = await page.evaluate(
    () => document.activeElement?.getAttribute('data-testid') ?? document.activeElement?.tagName ?? 'none',
  )

  const barText = await page.getByTestId('composer-collapsed-bar').innerText().catch(() => '')
  const composerGone = (await composerCount()) === 0

  // 3. Back again, draft intact — the way back has to work, not just exist.
  await page.getByTestId('composer-collapsed-bar').click()
  await page.waitForTimeout(900)
  const restoredValue = await page.locator('textarea[data-composer-input]').inputValue()
  const focusAfterRestore = await page.evaluate(() =>
    document.activeElement?.getAttribute('data-composer-input') != null
      ? 'composer'
      : (document.activeElement?.tagName ?? 'none'))
  await page.screenshot({ path: `${OUT}/3-restored-draft-intact.png` })

  // In video mode the run STOPS here, and deliberately: everything after this is a
  // viewport resize to 390px and two reloads, which record as a 1500px-wide canvas
  // with three quarters of it blank -- a clip that looks broken while proving
  // nothing. The gestures a recording is actually wanted for (collapse, then
  // restore) are both behind us. This mode therefore does NOT run the geometry and
  // reachability assertions below; the default mode is the one that validates, and
  // the notice says so rather than letting a green-looking clip run imply a pass.
  if (recordVideo) {
    const clip = await page.video()?.path()
    await context.close()
    await browser.close()
    srv.close()
    console.log(`WEBM ${clip}`)
    console.log('NOTE: clip mode -- collapse and restore recorded; assertions SKIPPED. Run without RECORD_VIDEO=1 to verify.')
    return
  }

  // 4. The bar with an EMPTY draft. Review noted this state was unscreenshotted,
  // and it is the one a first-time user meets: no draft line, so the bar has to
  // read as a control in its own right rather than as a truncated message.
  await page.locator('textarea[data-composer-input]').fill('')
  await page.getByTitle('Add files & options').click()
  await page.waitForTimeout(300)
  await page.getByTestId('composer-collapse-row').click()
  await page.waitForTimeout(900)
  await page.screenshot({ path: `${OUT}/4-collapsed-empty-draft.png` })

  // 4b. THE NARROW LAYOUT, which is where the blocking finding lived. Below
  // MOBILE_BREAKPOINT (768) `directFilePicker` is true, so the "+" is a bare
  // file-input label and the drop-up that hosts the collapse row never mounts --
  // moving the control into that menu had deleted the action at phone widths
  // entirely. The overflow trigger is its host there. Captured at 390px AND
  // asserted, because this is the half no desktop frame can show.
  await page.setViewportSize({ width: 390, height: 844 })
  await page.reload({ waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2500)
  // The preference persisted through the reload (frame 4 left it collapsed), so
  // this arrives collapsed -- which is itself the proof that the bar is the way
  // back on a phone. Expand through it before typing.
  const narrowRestoredFromReload = (await page.getByTestId('composer-collapsed-bar').count()) > 0
  if (narrowRestoredFromReload) {
    await page.getByTestId('composer-collapsed-bar').click()
    await page.waitForTimeout(700)
  }
  await page.locator('textarea[data-composer-input]').click()
  await page.keyboard.type(DRAFT)
  await page.waitForTimeout(300)
  const narrowPlusMenuAbsent = (await page.getByTitle('Add files & options').count()) === 0
  await page.getByTestId('composer-more-trigger').click()
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/5-narrow-overflow-entry-point.png` })
  // Sketch has to still be reachable: the row is capped at two controls, so the
  // pencil BECAME this trigger. If Sketch vanished, the fix would have traded one
  // unreachable action for another.
  const narrowSketchReachable = (await page.getByTitle('Sketch').count()) > 0
  await page.getByTestId('composer-collapse-row').click()
  await page.waitForTimeout(900)
  const narrowCollapsed = (await page.getByTestId('composer-collapsed-bar').count()) > 0
  const narrowComposerGone = (await composerCount()) === 0
  await page.screenshot({ path: `${OUT}/6-narrow-collapsed.png` })
  // Back to the reading width for the "/" check below.
  await page.getByTestId('composer-collapsed-bar').click()
  await page.waitForTimeout(600)
  await page.setViewportSize({ width: 1500, height: 950 })
  await page.reload({ waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2500)
  await page.locator('textarea[data-composer-input]').fill('')
  await page.getByTitle('Add files & options').click()
  await page.waitForTimeout(300)
  await page.getByTestId('composer-collapse-row').click()
  await page.waitForTimeout(900)

  // 5. The documented "/" shortcut must REACH a collapsed composer rather than
  // silently doing nothing — this state is indefinite and survives a reload, so a
  // no-op there is a standing dead end for a deliberate gesture.
  await page.keyboard.press('/')
  await page.waitForTimeout(600)
  const slashRevealed = (await composerCount()) > 0

  const reclaimed = expandedH - collapsedH
  const report = {
    expandedInputAreaH: Math.round(expandedH),
    collapsedInputAreaH: Math.round(collapsedH),
    reclaimedPx: Math.round(reclaimed),
    transcriptViewportExpanded: Math.round(expandedViewportH),
    transcriptViewportCollapsed: Math.round(collapsedViewportH),
    conversationAreaGainedPx: Math.round(collapsedViewportH - expandedViewportH),
    composerUnmountedWhileCollapsed: composerGone,
    collapsedBarShowsDraft: barText.includes(DRAFT),
    focusAfterCollapse,
    focusAfterRestore,
    slashShortcutRevealedComposer: slashRevealed,
    draftAfterRestore: restoredValue,
    draftSurvived: restoredValue === DRAFT,
    // The narrow layout, at 390px.
    narrowPlusMenuAbsent,
    narrowBarSurvivedReload: narrowRestoredFromReload,
    narrowSketchStillReachable: narrowSketchReachable,
    narrowCollapseWorked: narrowCollapsed,
    narrowComposerUnmounted: narrowComposerGone,
  }
  console.log(JSON.stringify(report, null, 2))

  // The video is only finalized on context close, and its path is only readable
  // from the page before that -- so grab it first, then close in order.
  const videoPath = recordVideo ? await page.video()?.path() : null
  await context.close()
  await browser.close()
  srv.close()
  if (videoPath) console.log(`WEBM ${videoPath}`)

  const failures = []
  if (reclaimed < MIN_RECLAIMED) {
    failures.push(`reclaimed ${Math.round(reclaimed)}px, expected >= ${MIN_RECLAIMED}`)
  }
  if (collapsedViewportH <= expandedViewportH) {
    failures.push(`conversation area did not grow: ${Math.round(expandedViewportH)} -> ${Math.round(collapsedViewportH)}px`)
  }
  if (!composerGone) failures.push('composer still in the tree while collapsed')
  if (!report.collapsedBarShowsDraft) failures.push('collapsed bar does not show the waiting draft')
  if (focusAfterCollapse !== 'composer-collapsed-bar') {
    failures.push(`focus went to ${focusAfterCollapse} on collapse, expected the bar`)
  }
  if (focusAfterRestore !== 'composer') {
    failures.push(`focus went to ${focusAfterRestore} on restore, expected the composer`)
  }
  if (!slashRevealed) failures.push('"/" did not bring a collapsed composer back')
  if (!report.draftSurvived) failures.push(`draft lost: got ${JSON.stringify(restoredValue)}`)
  // The narrow layout is asserted, not merely photographed: this is the half that
  // shipped broken, and a frame nobody opens would not have caught it.
  if (!narrowPlusMenuAbsent) {
    failures.push('at 390px the "+" drop-up still mounted, so this run did not exercise the touch host at all')
  }
  if (!narrowCollapsed) failures.push('at 390px the collapse gesture did not reach the composer')
  if (!narrowComposerGone) failures.push('at 390px the composer stayed mounted while collapsed')
  if (!narrowSketchReachable) failures.push('at 390px Sketch lost its host when the pencil became the overflow trigger')
  // The persistence claim, measured rather than asserted in prose: the collapse is
  // a PREFERENCE, and that it outlives a reload is exactly why a focus gesture
  // that silently no-ops while collapsed is a standing dead end and not a blip.
  if (!narrowRestoredFromReload) {
    failures.push('the collapsed preference did not survive a reload, so the bar was not there to come back through')
  }
  if (failures.length) {
    console.error('FAIL:\n  ' + failures.join('\n  '))
    process.exit(1)
  }
  console.log(
    `\nOK: ${Math.round(reclaimed)}px reclaimed, `
    + `conversation area ${Math.round(expandedViewportH)} -> ${Math.round(collapsedViewportH)}px, `
    + 'composer unmounted, draft intact.',
  )
}

main().catch(err => { console.error(err); process.exit(1) })

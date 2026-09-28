/**
 * Capture + regression harness for "drag a session into the open chat".
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback server with
 * /api/** answered by the shared fixture stub. The client code under test is
 * unmodified — only the network is stubbed — and the drag is driven with REAL
 * pointer events (dnd-kit's PointerSensor is pointer-event based, so there is no
 * synthetic shortcut), which is what makes this a regression test and not just a
 * camera.
 *
 * It asserts the four things that actually matter, and exits non-zero if any
 * fails:
 *   1. Dragging a session over the chat pane shows the drop affordance.
 *   2. Dropping stages a chip in the composer for THAT session.
 *   3. An incognito session shows the REFUSAL state and drops nothing.
 *   4. Sending transmits a LINK to the session — and NOT its content.
 *
 * Scenario 5 covers the OTHER refusal: the open session dragged onto its own
 * pane. It is not a guard — nothing was withheld, the user aimed at the pane they
 * dragged from — so it must not borrow the privacy copy or its warn tone. The
 * harness photographs both refusals so a reviewer can see they read differently.
 *
 * Usage: node scripts/capture-session-drag-ref.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/session-drag-ref'
const ACTIVE = 'chat-active'
const REF = 'chat-releases'
const PRIVATE = 'chat-private'
const VIEW = { width: 1500, height: 950 }

mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)
const slot = (key, title, extra = {}) => ({
  key,
  title,
  running: false,
  messages: 0,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  folder_id: '',
  modified: now,
  source_links: [],
  source_links_total: 0,
  ...extra,
})

const slots = [
  slot(ACTIVE, 'Composer keyboard shortcuts', { messages: 12, last_message: 'Where should Cmd+K land?' }),
  slot(REF, 'Release notes for 0.5.0', { messages: 137, last_message: 'Drafted the changelog section.' }),
  slot('chat-flake', 'Render gate flake', { messages: 48 }),
  // The privacy case. `temporary` is the OTHER private mode; using incognito here
  // and asserting the shared constant in unit tests covers both.
  slot(PRIVATE, 'Personal notes', { messages: 9, memory_mode: 'incognito' }),
]

/** Only the flat-view scenario needs folders — the flat toggle exists solely to
 *  escape a folder tree, so the lane never renders without at least one. */
const FOLDERS = [{ id: 'f-release', name: 'Release work', order: 0 }]

const detail = {
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  messages: [
    { role: 'user', ts: now - 600, content: 'Which shortcut should open the command palette?' },
    { role: 'assistant', ts: now - 30, content: 'Cmd+K is free on this surface — the composer only claims Cmd+Enter.' },
  ],
}

/** A phrase that exists ONLY in the referenced session, never in the fixture we
 *  send from. If it ever shows up in an outgoing message, the feature regressed
 *  from copying a link to copying content. */
const REFERENCED_ONLY_PHRASE = 'PARAKEET-CANARY-42'

/** Every POST /api/chat body, so the send payload can be asserted. */
const sends = []

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const results = []
  const record = (name, pass, note = '') => {
    results.push({ name, pass, note })
    console.log(`${pass ? 'PASS' : 'FAIL'}  ${name}${note ? ` — ${note}` : ''}`)
  }

  const extra = (path, route) => {
    if (path === '/api/chat' && route.request().method() === 'POST') {
      sends.push(route.request().postDataJSON?.() ?? null)
      return json(route, { ok: true, queued: false }), true
    }
    if (path.startsWith('/api/chat/slots/')) {
      // The referenced session's transcript carries the canary. It is reachable
      // over the API on purpose: the point is that the FRONTEND never pulls it.
      if (path.includes(REF)) {
        return json(route, {
          ...detail,
          messages: [{ role: 'assistant', ts: now - 90, content: `Release notes body ${REFERENCED_ONLY_PHRASE}` }],
        }), true
      }
      return json(route, detail), true
    }
    return false
  }

  const context = await browser.newContext({
    viewport: VIEW,
    deviceScaleFactor: 2,
    recordVideo: { dir: `${OUT}/video`, size: VIEW },
  })

  let page = null
  async function load(theme, { folders = [], flat = false } = {}) {
    if (page) await page.close()
    page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, { slots, theme, extra, folders })
    await page.addInitScript(s => localStorage.setItem('mc-active-slot', s), ACTIVE)
    // Flat view is a persisted sidebar preference, so seed it before boot
    // rather than clicking the toggle (which is only rendered once folders load).
    if (flat) await page.addInitScript(() => localStorage.setItem('mc-sidebar-flat-view', '1'))
    await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2500)
  }

  const shot = async (name) => {
    await page.screenshot({ path: `${OUT}/${name}.png` })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  /** Crop to the composer band — where the chip lands. */
  async function composerShot(name) {
    const strip = page.getByTestId('session-ref-strip')
    const target = (await strip.count()) ? strip.first() : page.getByTestId('input-wrapper').first()
    const box = await target.boundingBox()
    if (!box) return shot(name)
    await page.screenshot({
      path: `${OUT}/${name}.png`,
      clip: {
        x: Math.max(0, box.x - 24),
        y: Math.max(0, box.y - 28),
        width: Math.min(VIEW.width - Math.max(0, box.x - 24), box.width + 48),
        height: Math.min(VIEW.height - Math.max(0, box.y - 28), box.height + 150),
      },
    })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  /**
   * Drive a real dnd-kit drag from a session row to a point over the chat pane.
   * Multi-step moves are required, not cosmetic: PointerSensor only activates
   * past a 5px distance constraint, and collision detection reads pointer
   * coordinates on each move.
   */
  async function dragSessionToPane(key, { drop = true, midShot = null, onMid = null } = {}) {
    const row = page.locator(`[data-slot-key="${key}"]`).first()
    const from = await row.boundingBox()
    if (!from) throw new Error(`session row not found: ${key}`)
    const pane = await page.locator('.chat-container').first().boundingBox()
    if (!pane) throw new Error('chat pane not found')

    const sx = from.x + from.width / 2
    const sy = from.y + from.height / 2
    const tx = pane.x + pane.width / 2
    const ty = pane.y + pane.height / 2

    await page.mouse.move(sx, sy)
    await page.mouse.down()
    // Cross the activation threshold first, then travel in steps.
    await page.mouse.move(sx + 8, sy + 4, { steps: 4 })
    await page.waitForTimeout(150)
    for (let i = 1; i <= 12; i++) {
      await page.mouse.move(sx + ((tx - sx) * i) / 12, sy + ((ty - sy) * i) / 12)
      await page.waitForTimeout(35)
    }
    await page.waitForTimeout(450)
    const zone = page.getByTestId('chat-pane-drop-zone')
    const zoneVisible = (await zone.count()) > 0
    const refused = zoneVisible ? (await zone.first().getAttribute('data-refused')) !== null : null
    // The REASON, not just the fact. The two refusals must stay distinguishable:
    // 'private' is a guard, 'self' is a no-op, and one wearing the other's copy is
    // exactly the regression this reads for.
    const reason = zoneVisible ? await zone.first().getAttribute('data-refused') : null
    // What the pill actually says, so the copy is asserted rather than trusted to
    // follow from the reason attribute.
    const pillText = zoneVisible ? (await zone.first().innerText()).trim() : null
    // The drag preview must still be on screen out here, over the pane. The
    // sidebar rides inside OverlayDrawer's morph clip-path and a clip-path clips
    // fixed-position descendants too, so an in-place overlay is erased the
    // moment the cursor leaves the sidebar — exactly the half of the gesture
    // that aims at the chat. A layout box alone does not prove visibility
    // (clipping does not shrink it), so also walk the ancestor chain: any
    // clip-path above the ghost means it is being cut.
    const ghostLoc = page.getByTestId('session-drag-ghost')
    const ghostBox = (await ghostLoc.count()) ? await ghostLoc.first().boundingBox() : null
    const ghostClipped = await page.evaluate(() => {
      const g = document.querySelector('[data-testid="session-drag-ghost"]')
      if (!g) return null
      for (let el = g.parentElement; el; el = el.parentElement) {
        if (getComputedStyle(el).clipPath !== 'none') return true
      }
      return false
    })
    const ghostOverPane = !!ghostBox && ghostBox.x >= pane.x && ghostClipped === false
    // Where the cue is drawn, so "anchored on the composer" is guarded rather
    // than eyeballed: the outline must sit ON the composer, and the pill just
    // above it — NOT floating in the middle of the transcript.
    let anchored = null
    if (zoneVisible) {
      const outlineLoc = page.getByTestId('chat-pane-drop-target')
      // Absent by design in the refused state; `null` distinguishes "no outline"
      // from "outline in the wrong place".
      if (await outlineLoc.count()) {
        const outline = await outlineLoc.first().boundingBox()
        const composer = await page.getByTestId('input-wrapper').first().boundingBox()
        if (outline && composer) {
          anchored = Math.abs(outline.y - composer.y) <= 2 && Math.abs(outline.height - composer.height) <= 2
        }
      }
    }
    if (midShot) await shot(midShot)
    if (onMid) await onMid()
    if (drop) {
      await page.mouse.up()
      await page.waitForTimeout(600)
    } else {
      await page.keyboard.press('Escape')
      await page.mouse.up()
      await page.waitForTimeout(300)
    }
    return { zoneVisible, refused, reason, pillText, anchored, ghostOverPane }
  }

  /** Crop tight around the drop pill (which only exists mid-drag). */
  async function pillShot(name, text) {
    const box = await page.getByText(text, { exact: false }).first().boundingBox()
    if (!box) return shot(name)
    const pad = 22
    await page.screenshot({
      path: `${OUT}/${name}.png`,
      clip: {
        x: Math.max(0, box.x - pad),
        y: Math.max(0, box.y - pad),
        width: Math.min(VIEW.width - Math.max(0, box.x - pad), box.width + pad * 2),
        height: Math.min(VIEW.height - Math.max(0, box.y - pad), box.height + pad * 2),
      },
    })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  // ---------------------------------------------------------------- scenario 1
  // Normal session, dark theme: affordance mid-drag, then the staged chip.
  await load('dark')
  await shot('01-before-drag-dark')
  const normal = await dragSessionToPane(REF, {
    midShot: '02-drop-zone-dark',
    onMid: () => pillShot('02b-invite-pill-dark', 'Drop to reference'),
  })
  record('drop zone appears while dragging a session', normal.zoneVisible)
  record('drop zone invites (not refuses) a normal session', normal.refused === false)
  record('an invited drag carries no refusal reason', normal.reason === null, `reason=${normal.reason}`)
  record('cue is anchored on the composer, not floating mid-pane', normal.anchored === true)
  record('drag preview stays visible out over the pane', normal.ghostOverPane === true)

  const chip = page.locator('[data-testid="session-ref-chip"]')
  const chipCount = await chip.count()
  const chipKey = chipCount ? await chip.first().getAttribute('data-session-ref') : null
  record('drop stages exactly one chip', chipCount === 1, `count=${chipCount}`)
  record('chip points at the dropped session', chipKey === REF, `key=${chipKey}`)
  await composerShot('03-chip-staged-dark')

  // ---------------------------------------------------------------- scenario 2
  // The privacy guard: incognito shows the refusal and drops nothing.
  const priv = await dragSessionToPane(PRIVATE, {
    midShot: '04-refused-incognito-dark',
    onMid: () => pillShot('04b-private-pill-dark', 'Private sessions'),
  })
  record('drop zone refuses an incognito session', priv.refused === true)
  record('the incognito refusal names itself as the privacy guard', priv.reason === 'private',
    `reason=${priv.reason}`)
  record('a refused drag outlines no destination', priv.anchored === null)
  const afterPrivate = await page.locator('[data-testid="session-ref-chip"]').count()
  record('dropping an incognito session stages nothing', afterPrivate === chipCount,
    `chips ${chipCount} -> ${afterPrivate}`)

  // ---------------------------------------------------------------- scenario 3
  // Send: the wire carries a LINK, not the referenced transcript.
  await page.getByTestId('input-wrapper').locator('textarea').fill('Compare this against what we shipped')
  await page.waitForTimeout(200)
  await composerShot('05-chip-with-text-dark')
  await page.keyboard.press('Enter')
  await page.waitForTimeout(900)

  const sent = sends.at(-1)
  const msg = sent?.message ?? ''
  record('send reached the API', !!sent)
  record('sent text carries the session link', msg.includes(`?sid=${REF}`), msg.slice(0, 200))
  // The link must be the SAME shape the session menu's "Copy link" produces —
  // /chat/<title-slug>?sid=<slot> — not a second dialect the reader has to learn.
  // The trailing `)` is the markdown link's own closer, so anchor on it.
  const linkLine = msg.split('\n').find(l => l.includes('?sid=')) ?? ''
  record('link matches the Copy link shape (slug + sid)',
    /\/chat\/release-notes-for-0-5-0\?sid=chat-releases\)$/.test(linkLine), linkLine)
  record('sent text does NOT carry the referenced transcript',
    !msg.includes(REFERENCED_ONLY_PHRASE))
  record('link payload stays small (a pointer, not a transcript)', msg.length < 400, `${msg.length} chars`)
  const clearedAfterSend = (await page.locator('[data-testid="session-ref-chip"]').count()) === 0
  record('chips clear on send', clearedAfterSend)
  await composerShot('06-cleared-after-send-dark')

  // ---------------------------------------------------------------- scenario 4
  // Light theme, and the remove control.
  await load('light')
  await dragSessionToPane(REF)
  await composerShot('07-chip-staged-light')
  const removeBtn = page.locator('[data-testid="session-ref-chip"] button').first()
  await removeBtn.click()
  await page.waitForTimeout(400)
  const removed = (await page.locator('[data-testid="session-ref-chip"]').count()) === 0
  record('remove control unstages the chip', removed)
  await composerShot('08-after-remove-light')

  // Multiple refs, to show the strip scrolling behaviour.
  await dragSessionToPane(REF)
  await dragSessionToPane('chat-flake')
  const multi = await page.locator('[data-testid="session-ref-chip"]').count()
  record('two different sessions stage two chips', multi === 2, `count=${multi}`)
  await composerShot('09-two-chips-light')
  // A duplicate drop must not add a second chip for the same session.
  await dragSessionToPane(REF)
  const afterDup = await page.locator('[data-testid="session-ref-chip"]').count()
  record('re-dropping the same session does not duplicate', afterDup === 2, `count=${afterDup}`)

  // ---------------------------------------------------------------- scenario 5
  // The self-drop: the OPEN session dragged onto its own pane. A no-op, but the
  // user's near-miss rather than a guard firing — so it must NOT reuse the privacy
  // copy (which would assert something untrue about this session) or its warn
  // tone. Fresh loads so neither theme inherits a staged chip from above.
  const SELF_LINE = "Can't drop a session into itself"
  for (const theme of ['dark', 'light']) {
    await load(theme)
    const self = await dragSessionToPane(ACTIVE, {
      midShot: `10-self-drop-${theme}`,
      onMid: () => pillShot(`10b-self-pill-${theme}`, SELF_LINE),
    })
    record(`${theme}: dragging the open session onto its own pane is refused`, self.refused === true)
    record(`${theme}: the self-drop names itself 'self', not 'private'`, self.reason === 'self',
      `reason=${self.reason}`)
    record(`${theme}: the pill shows the recursive line`, (self.pillText ?? '').includes(SELF_LINE),
      JSON.stringify(self.pillText))
    record(`${theme}: the self-drop does NOT borrow the privacy copy`,
      !(self.pillText ?? '').includes('Private sessions'), JSON.stringify(self.pillText))
    record(`${theme}: the self-drop outlines no destination`, self.anchored === null)
    const stagedAfterSelf = await page.locator('[data-testid="session-ref-chip"]').count()
    record(`${theme}: dropping a session on itself stages nothing`, stagedAfterSelf === 0,
      `chips=${stagedAfterSelf}`)
  }

  // ---------------------------------------------------------------- scenario 6
  // FLAT view (every chat in one lane, no folder tree). The same gesture has to
  // work here — and reordering still must not, which is why the lane's
  // DndContext registers the chat pane and nothing else.
  await load('dark', { folders: FOLDERS, flat: true })
  const laneCount = await page.getByTestId('flat-view-lane').count()
  record('flat view is the lane under test', laneCount === 1, `lanes=${laneCount}`)
  const flatDrag = await dragSessionToPane(REF, { midShot: '11-flat-drag-dark' })
  record('flat view: drop zone appears while dragging a session', flatDrag.zoneVisible)
  record('flat view: drop zone invites (not refuses) a normal session', flatDrag.refused === false)
  record('flat view: drag preview stays visible out over the pane', flatDrag.ghostOverPane === true)
  const flatChips = await page.locator('[data-testid="session-ref-chip"]').count()
  const flatChipKey = flatChips
    ? await page.locator('[data-testid="session-ref-chip"]').first().getAttribute('data-session-ref')
    : null
  record('flat view: drop stages a chip for that session', flatChips === 1 && flatChipKey === REF,
    `count=${flatChips} key=${flatChipKey}`)
  await composerShot('12-flat-chip-staged-dark')
  // Order stays a pure function of the sort key: no in-lane drop target exists,
  // so there is nothing a release inside the sidebar could reorder against.
  const inLaneTargets = await page.locator('[data-folder-drop]').count()
  record('flat view: no in-lane drop target, so hand-reordering stays off', inLaneTargets === 0,
    `targets=${inLaneTargets}`)
  // A refused (private) session must be refused here too.
  const flatPrivate = await dragSessionToPane(PRIVATE)
  record('flat view: drop zone refuses an incognito session', flatPrivate.refused === true)
  const afterFlatPrivate = await page.locator('[data-testid="session-ref-chip"]').count()
  record('flat view: dropping an incognito session stages nothing', afterFlatPrivate === 1,
    `chips 1 -> ${afterFlatPrivate}`)
  // The rows are drag sources now, which must not swallow the tap that switches
  // session — MouseSensor only activates past 5px, so a click is still a click.
  await page.locator('[data-slot-key="chat-flake"]').first().click()
  await page.waitForTimeout(700)
  const switched = await page.locator('[data-slot-key="chat-flake"] .session-active').count()
  record('flat view: clicking a row still switches session', switched === 1, `active=${switched}`)

  await page.close()
  await context.close()
  await browser.close()
  srv.close()

  const failed = results.filter(r => !r.pass)
  console.log(`\n--- ${results.length - failed.length}/${results.length} assertions passed ---`)
  if (failed.length) {
    for (const f of failed) console.log(`FAILED: ${f.name} — ${f.note}`)
    process.exitCode = 1
  }
}

main().catch(e => { console.error(e); process.exitCode = 1 })

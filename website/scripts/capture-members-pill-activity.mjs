/**
 * Screenshot harness for the Crew Members DM header pill's ACTIVITY LINE: the
 * one line under the crewmate's name that says what it is doing right now
 * (`pages/members/pillActivity.ts`). Against a REAL pod, not fixtures.
 *
 * What the evidence has to show, per frame:
 *   - resting: the line is present and reads "Idle · <time ago>" — text only,
 *     no dot or glyph, and the pill is the same height it will be while busy;
 *   - a live turn: the line follows the slot's shared status (`toolStatusLabel`
 *     over `slotStatusDetail`) — "Thinking…" while the model reasons, the tool
 *     call's own purpose while a tool runs (simplified tool names on),
 *     "Writing…" once output flows — and a long purpose is cut at the cap
 *     with one ellipsis;
 *   - the line is not part of the button's accessible name.
 *
 * The live frames need the pod to actually run a turn, so the harness sends
 * one message through the real composer. A live turn moves faster than a
 * screenshot: photographing "whenever the attribute changes" produced frames
 * whose pixels showed the NEXT state (the UX lane caught a "tool" frame that
 * read "Thinking…"). So the socket is stepped: the dashboard's WebSocket is
 * routed through Playwright and, during the turn, server frames are released
 * to the page ONE AT A TIME; when a frame lands the pill in a state not yet
 * photographed, the release pauses, the frame is shot, and the pill is
 * re-read AFTER the shot to prove it still shows the same state. Real
 * backend frames, real UI -- only their pacing is controlled. A state the
 * turn never reaches is reported, not faked.
 *
 * Usage:
 *   kirocrew pod up <worktree> --json | tail -1 > "$KIROCREW_SCRATCH/pod-info.json"
 *   POD_INFO="$KIROCREW_SCRATCH/pod-info.json" \
 *     node scripts/capture-members-pill-activity.mjs ../temp-screenshots/members-pill-activity
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { check, openMemberPill, podInfo, primeCrewPod } from './lib/crew-pod-harness.mjs'

const OUT = process.argv[2] || '../temp-screenshots/members-pill-activity'
const CREW = 'oncall'
/** Mirrors `PILL_ACTIVITY_MAX_CHARS`; the harness checks the cut, not the number. */
// Two calls: one whose purpose fits the cap, one whose purpose is deliberately
// long, so the clamp is photographed on a real frame.
const PROMPT = 'You must use the shell tool; do not answer from memory. Run exactly two shell commands as two separate tool calls, each with a one-sentence tool-call purpose. First `ls /` with the purpose "List the root directory contents". Then `cat /etc/hostname` with the purpose "Read the machine hostname file so the answer can name this host and confirm the file is present and readable". Then answer in two short sentences.'
const LIVE_BUDGET_MS = 90_000
mkdirSync(OUT, { recursive: true })

const { BASE, authed } = podInfo(readFileSync)

/** This crewmate's header pill on a primed pod (shared boot dance). */
const openMember = (page) => openMemberPill(page, BASE, CREW)

async function shootHeader(page, path) {
  const box = await page.getByTestId('member-thread-header').boundingBox()
  await page.screenshot({ path, clip: { x: box.x, y: box.y, width: box.width, height: box.height + 72 } })
}

async function shootPill(page, path) {
  const box = await page.getByTestId('member-identity-pill').boundingBox()
  await page.screenshot({ path, clip: { x: box.x - 24, y: box.y - 12, width: box.width + 48, height: box.height + 24 } })
}

/** Kind and text in ONE read, so a state change between two reads cannot
 *  pair one state's kind with the next state's text. */
const readLine = (line) => line.evaluate(el => ({ kind: el.getAttribute('data-activity'), text: (el.textContent || '').trim() }))
/** Mirrors `PILL_ACTIVITY_MAX_CHARS`: a clamped line is at most this long and
 *  at least one under it (a cut on a space drops the space). */
const MAX_CHARS = 40
const isClamped = (kind, text) => kind === 'tool' && text.endsWith('…') && Array.from(text).length >= MAX_CHARS - 1

/**
 * Route the dashboard socket so server→page frames can be stepped. Page→server
 * frames pass straight through. Outside `stepping`, frames are forwarded as
 * they arrive; inside, they queue and `releaseOne` hands over the next one.
 * A reconnect (navigation) rebinds to the newest socket and drops frames that
 * belonged to the old one.
 */
function stepSocket(page) {
  const gate = { stepping: false, queue: [], ws: null }
  const flush = () => { while (!gate.stepping && gate.queue.length && gate.ws) gate.ws.send(gate.queue.shift()) }
  gate.releaseOne = () => {
    if (!gate.queue.length || !gate.ws) return false
    gate.ws.send(gate.queue.shift())
    return true
  }
  gate.install = () => page.routeWebSocket(/\/api\/ws/, ws => {
    const server = ws.connectToServer()
    gate.ws = ws
    gate.queue = []
    ws.onMessage(m => server.send(m))
    server.onMessage(m => { if (gate.ws === ws) { gate.queue.push(m); flush() } })
    server.onClose(() => ws.close())
    ws.onClose(() => { if (gate.ws === ws) gate.ws = null; server.close() })
  })
  return gate
}

async function stills(browser, theme) {
  const context = await browser.newContext({ viewport: { width: 1400, height: 900 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  const gate = stepSocket(page)
  await gate.install()
  await primeCrewPod(page, authed, CREW, theme)
  const pill = await openMember(page)
  const line = page.getByTestId('member-pill-activity')
  await page.mouse.move(5, 5)

  // 1: resting — line present, idle, text only, outside the accessible name.
  check(`[${theme}] the line sits inside the pill, under the title row`,
    await pill.evaluate(el => !!el.querySelector('[data-testid="member-pill-activity"]')
      && !el.querySelector('[data-testid="member-title-row"] [data-testid="member-pill-activity"]')))
  const rest = await readLine(line)
  check(`[${theme}] resting line is idle text only`, rest.kind === 'idle' && (await line.evaluate(el => el.children.length)) === 0, rest.text)
  check(`[${theme}] the same text sits outside the button for assistive tech`,
    (await page.getByTestId('member-pill-activity-sr').textContent()) === rest.text
      && !(await pill.evaluate(el => !!el.querySelector('[data-testid="member-pill-activity-sr"]'))))
  check(`[${theme}] the line is out of the button's accessible name`, (await line.getAttribute('aria-hidden')) === 'true')
  const restHeight = (await pill.boundingBox()).height
  await page.screenshot({ path: join(OUT, `01-dm-idle-${theme}.png`) })
  await shootHeader(page, join(OUT, `01b-header-idle-${theme}.png`))
  await shootPill(page, join(OUT, `01c-pill-idle-${theme}.png`))

  // 2: a live turn — step the socket one frame at a time and photograph each
  // state the line passes through, with the socket held while the shot is
  // taken so the pixels cannot move on. Each frame is then PROVEN: the pill is
  // re-read after the shot and must still show the state the file is named
  // for.
  const composer = page.getByRole('textbox').last()
  gate.stepping = true
  await composer.fill(PROMPT)
  await composer.press('Enter')
  const seen = new Map()
  const deadline = Date.now() + LIVE_BUDGET_MS
  let clampedShot = false
  let busySeen = false
  const shoot = async (kind, text, prefix, label) => {
    await shootHeader(page, join(OUT, `${prefix}-header-${label}-${theme}.png`))
    await shootPill(page, join(OUT, `${prefix}-pill-${label}-${theme}.png`))
    const after = await readLine(line)
    check(`[${theme}] the ${label} frame still reads ${JSON.stringify(text)} after the shot`, after.kind === kind && after.text === text, JSON.stringify(after))
    const h = (await pill.boundingBox()).height
    check(`[${theme}] pill height unchanged while ${label} (${restHeight} -> ${h})`, Math.abs(h - restHeight) <= 1)
  }
  while (Date.now() < deadline) {
    const { kind, text } = await readLine(line)
    if (kind && kind !== 'idle') busySeen = true
    if (kind && kind !== 'idle' && !seen.has(kind)) {
      seen.set(kind, text)
      await shoot(kind, text, '02', kind)
    }
    if (!clampedShot && isClamped(kind, text)) {
      clampedShot = true
      await shoot(kind, text, '02c', 'tool-clamped')
      console.log('ok  ', `[${theme}] a long purpose is cut at the cap with an ellipsis:`, JSON.stringify(text))
    }
    // The turn is over once the page has drained every frame and rests.
    if (busySeen && kind === 'idle' && gate.queue.length === 0) break
    // Hand the page its next frame and give React one tick to paint it.
    if (!gate.releaseOne()) await page.waitForTimeout(40)
    else await page.waitForTimeout(30)
  }
  gate.stepping = false
  if (!clampedShot) console.log('warn', `[${theme}] no tool purpose reached the cap during the live turn`)
  for (const [kind, text] of seen) console.log('ok  ', `[${theme}] saw ${kind}:`, JSON.stringify(text))
  for (const want of ['thinking', 'tool', 'writing']) {
    if (!seen.has(want)) console.log('warn', `[${theme}] never saw ${want} within ${LIVE_BUDGET_MS / 1000}s`)
  }
  check(`[${theme}] the live turn showed at least one working state`, seen.size > 0)
  await context.close()
}

async function main() {
  const browser = await chromium.launch()
  try {
    for (const theme of ['dark', 'light']) await stills(browser, theme)
  } finally {
    await browser.close()
  }
  console.log('wrote', OUT)
}

main().catch((err) => { console.error(err); process.exit(1) })

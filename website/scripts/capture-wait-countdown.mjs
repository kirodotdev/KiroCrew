/**
 * Screenshot + video harness for the `wait` tool's live countdown row.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback server with
 * every /api/** call answered from fixtures and the /api/ws websocket bound by
 * Playwright, so the scene is assembled exactly the way the backend assembles
 * it: GET /api/chat/slots carries the slot's `wait_state`, and the running
 * `wait` pill arrives as a `tool_call` frame followed by the persisted `🔧 wait`
 * `chat_message`. No gateway, no auth, no real sleep.
 *
 * That fidelity is the point, because the countdown is a JOIN across two
 * independent sources and neither half is sufficient alone: the pill comes from
 * the tool log (websocket) while the deadline comes from the slots payload
 * (REST). A fixture that seeded only a transcript would photograph a pill with
 * no countdown; one that seeded only wait_state would photograph nothing at all.
 *
 * `deadline_ts` is minted ONCE, in absolute epoch seconds, and the same value is
 * served to every poll — so the label ticks down on its own from the browser
 * clock, which is what makes the video real motion rather than a re-render of a
 * moving fixture.
 *
 * The recording deliberately does not stop at the click. Ending a wait is
 * COOPERATIVE: the sleeping tool runs no listener, so the request is parked on
 * the slot and only collected on the tool's next keepalive poll — which means
 * WAIT_PING_SECS (5s, read out of mcp_core.py below rather than guessed) IS the
 * button's advertised worst-case latency. A clip that cut at the greyed
 * "Ending…" label would photograph exactly the frame a user reads as "the button
 * hung". So this harness plays the whole cooperative handshake, at real wall
 * speed, in ONE take:
 *
 *   ticking → click → ~5s of greyed "Ending…" → row gone, pill completed
 *
 * The resolution half replays what the backend does, in the backend's order:
 *   1. POST lands, `endingWait` latches the button (nothing else changes yet —
 *      the sleep has not polled).
 *   2. Up to WAIT_PING_SECS later the poll collects the request:
 *      `_service_wait_ping` clears `slot._wait_state` and calls
 *      `push_slots_update()` — so the fixture's /api/chat/slots stops serving
 *      wait_state AND a `slots` frame is pushed over the bound websocket.
 *   3. The tool returns a NORMAL result (never ToolCancelled — that would
 *      suppress the response), which arrives as a `tool_result` frame carrying
 *      "Wait ended early by the user after Ns of 300s. Resuming: …".
 *
 * Both halves of step 2/3 are pushed because both are load-bearing in the real
 * app and each alone would leave a half-truth on screen: the slots frame is what
 * a mid-wait RELOAD would rely on, while the tool_result is what flips the pill
 * to completed and carries the text that proves the turn resumed.
 *
 * It asserts the things that actually matter, and exits non-zero if any fails:
 *   1. The countdown row exists while the `wait` tool is running.
 *   2. Its label DECREASES between two samples ~3s apart (it is live, not a
 *      static string).
 *   3. Under a minute the label is seconds-only — no bare "0m" prefix.
 *   4. The End-wait button exists, and becomes disabled + relabelled after a click.
 *   5. The click issues POST /api/chat/slots/<slot>/end-wait quoting the right wait_id.
 *   6. The row SURVIVES the click — a disabled button next to a still-ticking
 *      countdown is the correct intermediate state, not a stuck one.
 *   7. Once the simulated poll collects the request the row AND the button are
 *      gone, the pill has flipped to its completed rendering, and its result
 *      text reports the early end.
 *   8. The click → row-gone gap is >= 4.5s of REAL time, so the clip cannot
 *      quietly cut the latency it exists to document.
 *   9. The produced recording contains no partially-painted (mid-grey) frames —
 *      a defect that every DOM assertion above is blind to. See
 *      verifyNoPartialFrames, and the two-pass split it forced.
 *  10. The resolved still actually FRAMES the result panel, measured against the
 *      output block's own box — the panel opens on a height transition, so "the
 *      text was in the DOM" is not evidence that the picture shows it.
 *
 * Usage: node scripts/capture-wait-countdown.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, renameSync, existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawnSync } from 'node:child_process'
import { serveDist } from './lib/serve-dist.mjs'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/wait-countdown'
const SLOT = 'chat-wait-countdown'
const PROJECT = '/home/user/workspace/KiroCrew'
const WAIT_ID = 'wait-7f3a91'
const TOOL_CALL_ID = 'tc_wait_1'
const VIEW = { width: 1200, height: 780 }
/** Total sleep the fixture's `wait` call asked for, and the reason it will
 *  resume with. Both are quoted back verbatim in the tool result, exactly as
 *  mcp_core.py formats it, so the recorded pill reads like a real one. */
const WAIT_SECONDS = 300
const RESUME_REASON = 're-check the PR for new review comments'
const INPUT_PREVIEW = JSON.stringify({ seconds: WAIT_SECONDS, reason: RESUME_REASON })

/**
 * The wait tool's keepalive interval, READ from the source of truth rather than
 * pinned here.
 *
 * This number is the whole point of the post-click half of the recording: it is
 * how long the button can legitimately sit greyed before anything happens, so a
 * harness that hardcoded its own 5 would keep "proving" a latency the product no
 * longer has the day someone tunes the constant. Falls back to the current value
 * with a printed note if the file moves, since a missing constant must not cost
 * the whole capture.
 */
function readWaitPingSecs() {
  const src = fileURLToPath(new URL('../../src/kiro_crew/mcp_core.py', import.meta.url))
  try {
    const m = /^WAIT_PING_SECS\s*=\s*([0-9]+(?:\.[0-9]+)?)/m.exec(readFileSync(src, 'utf8'))
    if (m) return Number(m[1])
    console.log(`NOTE: WAIT_PING_SECS not found in ${src} — using 5.0`)
  } catch (err) {
    console.log(`NOTE: could not read ${src} (${err.code || err.message}) — using 5.0`)
  }
  return 5.0
}
const WAIT_PING_SECS = readWaitPingSecs()

mkdirSync(OUT, { recursive: true })

const results = []
const record = (name, pass, note = '') => {
  results.push({ name, pass, note })
  console.log(`${pass ? 'PASS' : 'FAIL'}  ${name}${note ? ` — ${note}` : ''}`)
}

/** Every POST to the end-wait route, so the click's wire payload can be asserted. */
const endWaitCalls = []

const slot = (waitState) => ({
  key: SLOT,
  title: 'Babysit the PR until CI is green',
  running: true,
  last_message: '🔧 wait',
  messages: 3,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
  wait_state: waitState,
})

const detail = {
  running: true,
  has_more: false,
  total: 1,
  queue: [],
  project: PROJECT,
  messages: [{
    role: 'user',
    ts: Date.now() / 1000 - 40,
    content: 'Wait five minutes, then re-check the PR for new review comments.',
  }],
}

/**
 * Read the countdown's digits back as a NUMBER of seconds.
 *
 * The visible label is localized narrow-unit text ("4m 12s left"), so the
 * assertion parses the units rather than string-comparing: a plain `!==` would
 * also pass if the label went UP, and a substring test would not notice
 * "4m 12s" → "3m 12s" wrapping the wrong way.
 */
function parseRemaining(text) {
  const m = /(\d+)\s*m/.exec(text)
  const s = /(\d+)\s*s/.exec(text)
  if (!m && !s) return null
  return (m ? Number(m[1]) * 60 : 0) + (s ? Number(s[1]) : 0)
}

/**
 * webm → mp4 + GIF, when ffmpeg is on PATH.
 *
 * Attempted rather than required: Playwright's own bundled ffmpeg is stripped to
 * the webm muxer and has no GIF encoder, and the container this harness usually
 * runs in ships no ffmpeg at all. So the conversion degrades to PRINTING the
 * exact command (the convention capture-terminal-subcommand.mjs already set),
 * which keeps the webm the authoritative artifact and the GIF reproducible by
 * hand. `-y` on both calls so a re-run overwrites instead of prompting.
 */
function convertVideo(webm) {
  const gifFilter = 'fps=12,scale=900:-1:flags=lanczos,split[a][b];'
    + '[a]palettegen=max_colors=128:stats_mode=diff[p];'
    + '[b][p]paletteuse=dither=bayer:bayer_scale=3'
  const mp4 = webm.replace(/\.webm$/, '.mp4')
  const gif = webm.replace(/\.webm$/, '.gif')
  const jobs = [
    ['-y', '-i', webm, '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', mp4],
    ['-y', '-i', webm, '-vf', gifFilter, '-loop', '0', gif],
  ]
  const probe = spawnSync('ffmpeg', ['-version'], { stdio: 'ignore' })
  if (probe.error) {
    console.log('  ffmpeg not on PATH — webm kept as-is. To produce mp4 + gif:')
    for (const args of jobs) console.log(`    ffmpeg ${args.join(' ')}`)
    return { mp4: null, gif: null }
  }
  const made = {}
  for (const [key, args] of [['mp4', jobs[0]], ['gif', jobs[1]]]) {
    const r = spawnSync('ffmpeg', args, { stdio: ['ignore', 'ignore', 'pipe'] })
    const dest = args[args.length - 1]
    if (r.status === 0 && existsSync(dest)) { made[key] = dest; console.log('wrote', dest) }
    else {
      made[key] = null
      console.log(`  ffmpeg ${key} conversion failed:`, String(r.stderr ?? '').split('\n').slice(-4).join(' '))
    }
  }
  return made
}

/**
 * Fail the run if the recording contains PARTIALLY-PAINTED frames.
 *
 * Chromium hands the video encoder whatever the compositor has, and under some
 * interleavings (most reproducibly: a `page.screenshot()` on a recording page)
 * that is a surface where only part of the viewport was composited — the rest
 * arrives as flat mid-grey, ~rgb(128,128,128). Two or three such frames is 80–120ms
 * of the clip flashing grey, and it is invisible to every other assertion here:
 * the DOM was correct the whole time, only the capture was not.
 *
 * This gate exists because the previous revision of this harness reported 13/13
 * green while doing exactly that, so "the assertions passed" cannot be the last
 * word on a video artifact.
 *
 * Method: average a 1200x300 band of the LOWER viewport (empty transcript +
 * composer — uniformly light in this scene's light theme) down to a single pixel
 * per frame, and flag frames whose average is neutral grey near 128. Real content
 * in that band averages ~253; the pre-first-paint frames at the head of every
 * recording are near-black (~24), so neither is mistaken for a partial paint.
 *
 * Unverifiable counts as FAILED, deliberately: unlike the mp4/gif conversion
 * (which degrades to printing its command because the FORMAT is a convenience),
 * a video harness that cannot inspect its own video has not finished its job.
 */
function verifyNoPartialFrames(webm) {
  const probe = spawnSync('ffmpeg', ['-version'], { stdio: 'ignore' })
  if (probe.error) return { ok: false, note: 'NOT VERIFIED — ffmpeg not on PATH' }
  const r = spawnSync('ffmpeg', [
    '-v', 'error', '-i', webm,
    '-vf', `crop=${VIEW.width}:300:0:${VIEW.height - 310},scale=1:1,fps=25`,
    '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-',
  ], { maxBuffer: 64 * 1024 * 1024 })
  if (r.status !== 0 || !r.stdout?.length) {
    return { ok: false, note: `NOT VERIFIED — ffmpeg probe failed: ${String(r.stderr ?? '').slice(0, 200)}` }
  }
  const buf = r.stdout
  const grey = []
  for (let i = 0; i + 2 < buf.length; i += 3) {
    const [red, green, blue] = [buf[i], buf[i + 1], buf[i + 2]]
    const neutral = Math.max(red, green, blue) - Math.min(red, green, blue) <= 10
    const midGrey = red >= 110 && red <= 150 && green >= 110 && green <= 150 && blue >= 110 && blue <= 150
    if (neutral && midGrey) grey.push(((i / 3) / 25).toFixed(2))
  }
  const frames = Math.floor(buf.length / 3)
  return grey.length === 0
    ? { ok: true, note: `${frames} frames scanned, 0 mid-grey` }
    : { ok: false, note: `${grey.length}/${frames} frames mid-grey at t=${grey.slice(0, 12).join(', ')}s` }
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()

  // Boot fixtures, with branding corrected to the backend's own two-word default.
  // boot-api.mjs ships `bot_name: 'Kiro'`, and a single-word name silently drops
  // the accented "CREW" from the nav brand and renders a "Message Kiro…"
  // composer — wrong chrome in every frame of the recording.
  const fixedApi = makeFixedApi(PROJECT)
  fixedApi.set('/api/dashboard/branding', { bot_name: 'Kiro Crew', avatar: '/logo.png' })

  /**
   * Mount one scene: stub the API around `waitState`, boot the SPA, then push the
   * running `wait` pill over the websocket.
   *
   * Returns a mutable scene handle — `{ page, waitState, send }`. The route
   * handler reads `scene.waitState` on EVERY poll instead of closing over the
   * argument, and `send` writes frames onto the same socket the SPA is already
   * listening to, so a run can retire the countdown mid-recording exactly the way
   * `_service_wait_ping` does: blank the slot's wait_state, then broadcast.
   *
   * The pill is seeded per-scene because the tool log is websocket-only state —
   * it does not survive a reload, so each fresh page has to be re-narrated.
   */
  async function openScene(context, { theme, waitState }) {
    const page = await context.newPage()
    const scene = { page, waitState, send: null }
    let wsServer = null
    await page.routeWebSocket(/\/api\/ws/, ws => { wsServer = ws })

    await page.route('**/api/**', async route => {
      const req = route.request()
      const path = new URL(req.url()).pathname
      // Checked BEFORE the detail route: this path is also under
      // /api/chat/slots/, and the detail arm would swallow the POST.
      if (path === `/api/chat/slots/${SLOT}/end-wait` && req.method() === 'POST') {
        endWaitCalls.push({ path, body: req.postDataJSON?.() ?? null, ts: Date.now() })
        // `{ok: true}` and nothing else, matching api_chat_slot_end_wait: the
        // route only PARKS the request on the slot. Any "ended" flag here would
        // imply the sleep is already over and quietly excuse the harness from
        // reproducing the poll that actually ends it.
        return json(route, { ok: true })
      }
      if (path === '/api/chat/slots') return json(route, [slot(scene.waitState)])
      if (path === `/api/chat/slots/${SLOT}`) return json(route, detail)
      return handleBootRoute(route, path, { project: PROJECT, theme, fixedApi })
    })

    page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))
    page.on('console', msg => {
      if (msg.type() === 'error') console.log('CONSOLE:', msg.text().slice(0, 300))
    })

    await page.addInitScript(([themeMode, activeSlot]) => {
      localStorage.clear()
      localStorage.setItem('mc-theme', themeMode)
      localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-active-slot', activeSlot)
    }, [theme, SLOT])
    await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2500)
    if (!wsServer) throw new Error('websocket route never bound')
    scene.send = frame => wsServer.send(JSON.stringify(frame))

    // The tool log entry — what makes the pill LIVE. Without it ToolCallLine
    // takes its historical-message branch, reports isDone, and renders no row.
    scene.send({
      type: 'tool_call',
      data: {
        slot: SLOT, tool: 'wait', kind: 'other',
        purpose: 'Wait for the CI run to report', input_preview: INPUT_PREVIEW,
        tool_call_id: TOOL_CALL_ID, auto: true, is_shell: false,
      },
    })
    // The persisted pill message, broadcast the way the backend broadcasts it
    // (see the `tool_call` arm of useWebSocket: it deliberately does NOT insert
    // the row itself, so the row only exists once this frame lands).
    scene.send({
      type: 'chat_message',
      data: {
        slot: SLOT, role: 'tool', content: '🔧 wait',
        ts: new Date().toISOString(), meta: { tool_call_id: TOOL_CALL_ID },
      },
    })
    await page.waitForSelector('[data-testid="wait-countdown"]', { timeout: 10_000 })
    // Let the pill's one-shot `.ft-block-reveal` entrance fade finish before any
    // still is taken. It animates OPACITY for 0.6s, so a screenshot fired on the
    // selector alone catches the row at partial opacity and the label is barely
    // legible — which is exactly how the first run of this harness came out.
    // The class is self-clearing (ToolCallLine drops it on animationend), so its
    // absence is a real settle signal rather than a guessed timeout.
    await page.waitForFunction(
      () => !document.querySelector('[data-testid="wait-countdown"]')?.closest('.ft-block-reveal'),
      null, { timeout: 5000 },
    ).catch(() => console.log('NOTE: reveal fade never cleared; stills may be faded'))
    return scene
  }

  /** Crop to the pill band, so the countdown row is legible at review size.
   *  Returns the clip it used, so the post-resolution still can be framed on the
   *  SAME rectangle — the row's absence only reads as an absence against the
   *  identical crop. */
  async function rowShot(page, name) {
    const box = await page.getByTestId('wait-countdown').first().boundingBox()
    const clip = box
      ? {
        x: Math.max(0, box.x - 40),
        y: Math.max(0, box.y - 60),
        width: Math.min(VIEW.width - Math.max(0, box.x - 40), box.width + 320),
        height: Math.min(VIEW.height - Math.max(0, box.y - 60), box.height + 90),
      }
      : undefined
    await page.screenshot({ path: `${OUT}/${name}.png`, ...(clip ? { clip } : {}) })
    console.log('wrote', `${OUT}/${name}.png`)
    return clip
  }

  /** Screenshot a caller-supplied rectangle — for states where the anchor
   *  element no longer exists to measure. */
  async function clipShot(page, name, clip) {
    await page.screenshot({ path: `${OUT}/${name}.png`, ...(clip ? { clip } : {}) })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  const label = page => page.getByTestId('wait-countdown').locator('span[aria-hidden="true"]').first().innerText()

  /**
   * Play the whole early-end handshake on an open scene, at real wall speed:
   * ticking → click → one full WAIT_PING_SECS of greyed "Ending…" → the poll
   * collects the request → row gone, pill completed, result text carried.
   *
   * Run TWICE, and never with both flags on:
   *   `assert` — record the assertions (once, so the summary counts once).
   *   `shots`  — take the stills.
   *
   * They are separate passes because `page.screenshot()` on a page whose context
   * is recording pushes PARTIALLY-PAINTED frames into the video: mid-grey
   * (~128,128,128) below the region that happened to be composited. Measured on
   * the previous revision of this harness — 3 of its 4 stills each produced a
   * 2–3 frame grey burst, and one of those bursts landed exactly on the frame
   * where the countdown row disappears, i.e. on the one moment the clip exists to
   * show. So the recorded pass shoots nothing and a second, video-less pass
   * shoots everything; `verifyNoPartialFrames` below then gates the result so this
   * cannot come back unnoticed.
   */
  async function playEarlyEnd(scene, deadlineState, { assert, shots }) {
    const page = scene.page
    const rec = assert ? record : () => {}
    // Still-integrity assertions belong to the pass that actually shoots, so each
    // assertion is still recorded exactly once across the two passes.
    const recShot = shots ? record : () => {}
    // Only the shots pass measures a clip; the recorded pass returns null and
    // clipShot no-ops on it.
    const rowShotIf = shots ? name => rowShot(page, name) : async () => null
    const clipShotIf = shots ? (name, clip) => clipShot(page, name, clip) : async () => {}
    // Snapshot the POST log so the assertions read THIS pass's call, not the
    // other pass's leftovers — endWaitCalls is module-scoped on purpose (the
    // route handler has nowhere else to put it).
    const callsBefore = endWaitCalls.length

    const rowCount = await page.getByTestId('wait-countdown').count()
    rec('countdown row renders for a running wait tool', rowCount === 1, `count=${rowCount}`)
    await rowShotIf('01-countdown-mid-wait-light')

    const t0Text = await label(page)
    const t0 = parseRemaining(t0Text)
    await page.waitForTimeout(3200)
    const t1Text = await label(page)
    const t1 = parseRemaining(t1Text)
    rec('countdown label parses as a duration', t0 !== null && t1 !== null, `"${t0Text}" → "${t1Text}"`)
    rec('countdown decreases over ~3s (it is live)',
      t0 !== null && t1 !== null && t1 < t0 && t0 - t1 >= 2 && t0 - t1 <= 5,
      `${t0}s → ${t1}s (Δ${t0 === null || t1 === null ? '?' : t0 - t1}s)`)
    rec('label reads minutes + seconds above a minute', /m\b/.test(t1Text) && /s\b/.test(t1Text), t1Text)
    // Let the recording carry a few more visible ticks before the click.
    await page.waitForTimeout(2600)

    const btn = page.getByTestId('wait-end-now')
    const btnCount = await btn.count()
    const beforeLabel = btnCount ? await btn.first().innerText() : ''
    rec('End-wait button exists next to the countdown', btnCount === 1, `count=${btnCount}`)
    const enabledBefore = btnCount ? await btn.first().isEnabled() : false
    rec('End-wait button is enabled before the click', enabledBefore)

    await btn.first().click()
    // Wall-clock origin for the latency assertion. Taken AFTER the click resolves,
    // so it can only ever UNDER-report the gap the video holds.
    const clickTs = Date.now()
    await page.waitForTimeout(500)
    const disabledAfter = await btn.first().isDisabled()
    const afterLabel = await btn.first().innerText()
    rec('End-wait button becomes disabled after the click', disabledAfter)
    rec('End-wait button relabels while ending', afterLabel !== beforeLabel,
      `"${beforeLabel}" → "${afterLabel}"`)

    // The correct intermediate state, and the one the old clip ended on: request
    // parked, button greyed, countdown STILL TICKING because the sleep has not
    // polled yet. Asserted so a future change that optimistically hides the row on
    // click (making the UI lie about a wait that is still running) fails here.
    const rowDuringEnding = await page.getByTestId('wait-countdown').count()
    rec('countdown row survives the click while the request is parked',
      rowDuringEnding === 1 && disabledAfter,
      `row=${rowDuringEnding} disabled=${disabledAfter} at +${Date.now() - clickTs}ms`)
    const endingClip = await rowShotIf('02-ending-disabled-light')

    const call = endWaitCalls[callsBefore]
    rec('click issued exactly one end-wait POST', endWaitCalls.length - callsBefore === 1,
      `count=${endWaitCalls.length - callsBefore}`)
    rec('end-wait POST went to this slot\'s route',
      call?.path === `/api/chat/slots/${SLOT}/end-wait`, String(call?.path))
    rec('end-wait POST quotes the wait_id from wait_state',
      call?.body?.wait_id === deadlineState.wait_id, JSON.stringify(call?.body ?? null))

    // ── the cooperative handshake, at real speed ────────────────────────────
    // Hold the greyed button for a FULL keepalive interval measured from the
    // click, not from here — the assertions above already spent part of the
    // budget, and paying it twice would inflate the latency the clip claims.
    const holdRemaining = clickTs + WAIT_PING_SECS * 1000 - Date.now()
    if (holdRemaining > 0) await page.waitForTimeout(holdRemaining)

    // The poll collects the request. Order matters and mirrors the backend:
    // _service_wait_ping blanks slot._wait_state and pushes slots FIRST (so a
    // reload at this instant would already find no wait), and only then does the
    // tool return its result.
    scene.waitState = null
    // `type`/`data` is the owner-websocket frame shape (state.py
    // push_slots_update); `sseSlots` REPLACES the slots array, which is what
    // retires the countdown. The real frame also carries `yolo`/`channelTrusted`;
    // both are omitted here because `sseStatus` assigns over `state.status`
    // wholesale, so echoing them would blank the boot fixture's status object and
    // change unrelated chrome mid-recording.
    scene.send({ type: 'slots', data: [slot(null)] })
    // The tool's own return value — a normal result, formatted exactly as
    // mcp_core.py formats it, including the seconds actually slept.
    const waited = Math.max(0, WAIT_SECONDS - Math.max(0, Math.round(deadlineState.deadline_ts - Date.now() / 1000)))
    const resultText = `Wait ended early by the user after ${waited}s of ${WAIT_SECONDS}s. Resuming: ${RESUME_REASON}`
    scene.send({ type: 'tool_result', data: { slot: SLOT, output: resultText, tool_call_id: TOOL_CALL_ID } })

    await page.waitForSelector('[data-testid="wait-countdown"]', { state: 'detached', timeout: 5000 })
      .catch(() => console.log('NOTE: countdown row never detached'))
    const goneTs = Date.now()
    const rowAfter = await page.getByTestId('wait-countdown').count()
    const btnAfter = await page.getByTestId('wait-end-now').count()
    rec('countdown row is gone once the poll collects the request', rowAfter === 0, `count=${rowAfter}`)
    rec('End-wait button goes with the row', btnAfter === 0, `count=${btnAfter}`)

    // The pill itself, located by its aria-label (`label` is the raw "wait" from
    // the 🔧 message) so the lookup is independent of the simplified-tool-names
    // setting, which decides the VISIBLE label but never the aria one.
    const pill = page.locator('button[aria-label*="tool: wait"]')
    const pillCount = await pill.count()
    const iconClass = pillCount ? (await pill.locator('svg').first().getAttribute('class')) ?? '' : ''
    // Completed rendering = the green CircleDot (`text-ok`) in place of the
    // spinning LoaderCircle, and no shimmer span. Both are checked: the icon alone
    // also goes green on `!slotRunning`, and this scene deliberately keeps the slot
    // running so that only the tool_result can have flipped it.
    const shimmerCount = pillCount ? await pill.locator('.bg-clip-text').count() : -1
    rec('pill flips from running to the completed state',
      pillCount === 1 && iconClass.includes('text-ok') && !iconClass.includes('animate-spin') && shimmerCount === 0,
      `pills=${pillCount} icon="${iconClass}" shimmerSpans=${shimmerCount}`)

    await clipShotIf('05-resolved-after-early-end-light', endingClip)
    // Hold the resolved state so the clip does not cut on the frame it resolves.
    await page.waitForTimeout(2200)

    // Open the pill to surface the result text — the proof that the turn resumed
    // with a normal tool result rather than a swallowed cancellation.
    const container = pill.locator('xpath=../..')
    await pill.click()
    let panelText = ''
    for (let i = 0; i < 24; i++) {
      panelText = (await container.innerText()).replace(/\s+/g, ' ').trim()
      if (/ended early/i.test(panelText)) break
      await page.waitForTimeout(150)
    }
    rec('pill\'s result text reports the early end', /ended early/i.test(panelText),
      panelText.slice(0, 160))

    // The details panel opens on an AnimatePresence height 0 → auto transition, so
    // the text is in the DOM — and the assertion above is green — a third of a
    // second before the panel reaches full size. Measuring the crop right there
    // produced a still holding a 40px sliver of the panel and none of the result
    // text: the same "assertion passed, screenshot useless" failure this harness
    // was already bitten by once with the reveal fade. So settle on the box no
    // longer growing, which is a real signal rather than a guessed delay.
    let settled = null
    let prevHeight = -1
    for (let i = 0; i < 20; i++) {
      const box = await container.boundingBox()
      settled = box ?? settled
      if (box && Math.abs(box.height - prevHeight) < 1) break
      prevHeight = box?.height ?? -1
      await page.waitForTimeout(100)
    }
    const resultClip = settled
      ? {
        x: Math.max(0, settled.x - 40),
        y: Math.max(0, settled.y - 30),
        width: Math.min(VIEW.width - Math.max(0, settled.x - 40), settled.width + 80),
        height: Math.min(VIEW.height - Math.max(0, settled.y - 30), settled.height + 60),
      }
      : null
    // Prove the still actually CONTAINS the result text rather than merely being
    // taken while the text existed. `<pre>` is PayloadView's output block; the
    // containment test is the only thing standing between a green run and another
    // near-empty screenshot.
    const preBox = shots ? await container.locator('pre').first().boundingBox() : null
    const framed = !!(resultClip && preBox && preBox.height > 20
      && preBox.x >= resultClip.x && preBox.y >= resultClip.y
      && preBox.x + preBox.width <= resultClip.x + resultClip.width + 1
      && preBox.y + preBox.height <= resultClip.y + resultClip.height + 1)
    recShot('resolved still frames the whole result panel', framed,
      `clip=${resultClip ? `${Math.round(resultClip.width)}x${Math.round(resultClip.height)}` : 'none'}`
      + ` output=${preBox ? `${Math.round(preBox.width)}x${Math.round(preBox.height)}@${Math.round(preBox.x)},${Math.round(preBox.y)}` : 'none'}`)
    await clipShotIf('06-resolved-result-text-light', resultClip ?? undefined)
    await page.waitForTimeout(1600)

    // Last, so it reports the gap the recording actually held rather than a gap
    // this assertion caused. 4.5s not 5s: the browser's timer can fire a few ms
    // early and the threshold is there to catch a clip that cut INSTANTLY.
    const latency = goneTs - clickTs
    rec('the click → row-gone gap holds the real poll latency',
      latency >= 4500, `${latency}ms (WAIT_PING_SECS=${WAIT_PING_SECS}s from mcp_core.py)`)
  }

  /** A fresh 300s wait whose deadline sits 252s out, minted in absolute epoch
   *  seconds exactly like slot._wait_state — so every poll returns the SAME
   *  deadline and the label ticks from the browser clock. */
  const freshWait = () => ({
    wait_id: WAIT_ID, seconds: WAIT_SECONDS, deadline_ts: Math.floor(Date.now() / 1000) + 252,
  })

  // ── pass 1: the recording, and every behavioural assertion ────────────────
  // Its own context so exactly ONE video exists and page.video() unambiguously
  // names it. Later scenes reuse a video-less context.
  const recorded = await browser.newContext({
    viewport: VIEW,
    deviceScaleFactor: 2,
    recordVideo: { dir: OUT, size: VIEW },
  })
  const recordedWait = freshWait()
  const scene = await openScene(recorded, { theme: 'light', waitState: recordedWait })
  const page = scene.page
  await playEarlyEnd(scene, recordedWait, { assert: true, shots: false })

  const video = page.video()
  await page.close()
  await recorded.close()

  let webm = null
  if (video) {
    const src = await video.path()
    webm = join(OUT, 'wait-countdown.webm')
    if (src !== webm) renameSync(src, webm)
    console.log('wrote', webm)
    convertVideo(webm)
  }
  record('a video of the ticking countdown was recorded', !!webm, String(webm))
  const partials = webm ? verifyNoPartialFrames(webm) : { ok: false, note: 'no video' }
  record('recording contains no partially-painted frames', partials.ok, partials.note)

  // ── pass 2: the same sequence again, video-less, purely for the stills ─────
  const plain = await browser.newContext({ viewport: VIEW, deviceScaleFactor: 2 })
  const shotWait = freshWait()
  const shotScene = await openScene(plain, { theme: 'light', waitState: shotWait })
  await playEarlyEnd(shotScene, shotWait, { assert: false, shots: true })
  await shotScene.page.close()

  // ── scene 3: the sub-minute label ─────────────────────────────────────────
  const { page: shortPage } = await openScene(plain, {
    theme: 'light',
    waitState: { wait_id: 'wait-c0ffee', seconds: WAIT_SECONDS, deadline_ts: Math.floor(Date.now() / 1000) + 44 },
  })
  const shortText = await label(shortPage)
  record('sub-minute label is seconds-only (no "0m" prefix)',
    /\d+\s*s/.test(shortText) && !/\d+\s*m/.test(shortText), shortText)
  await rowShot(shortPage, '03-sub-minute-light')
  await shortPage.close()

  // ── scene 4: dark theme, matching the sibling harnesses' both-themes habit ─
  const { page: darkPage } = await openScene(plain, {
    theme: 'dark',
    waitState: { wait_id: 'wait-d4rk01', seconds: WAIT_SECONDS, deadline_ts: Math.floor(Date.now() / 1000) + 187 },
  })
  await rowShot(darkPage, '04-countdown-mid-wait-dark')
  await darkPage.close()
  await plain.close()

  await browser.close()
  srv.close()

  const failed = results.filter(r => !r.pass)
  console.log(`\n--- ${results.length - failed.length}/${results.length} assertions passed ---`)
  if (failed.length) {
    for (const f of failed) console.log(`FAILED: ${f.name} — ${f.note}`)
    process.exitCode = 1
  }
}

main().catch(err => { console.error(err); process.exit(1) })

/**
 * Self-checking screenshot + frame-sequence harness for the shell elapsed line
 * threshold: the "Running · Ns" line under a shell pill appears only once the
 * command has run for SHELL_ACTIVITY_MIN_SECS (ToolCallLine.tsx) and is removed
 * the moment the command completes.
 *
 * Runs the REAL built SPA (website/dist) behind a static server on the shared
 * mid-turn scene (lib/tool-row-scene.mjs) and drives a third shell tool over
 * the WebSocket exactly the way the backend does: a `chat_message` carrying the
 * tool bubble, a `tool_call` frame that puts the row in flight (`is_shell`),
 * then a `tool_result` that completes it.
 *
 * The behaviour is TEMPORAL, so the evidence is a timeline rather than one
 * still: named stills at the moments that matter (1s in: no line; 6s in: still
 * no line; past the threshold: line up and counting; done: line gone), plus a
 * frame every FRAME_MS for the whole sequence, written to a scratch dir so a
 * follow-up step can assemble them into a GIF. Every still is ASSERTED against
 * the DOM before it is written -- a frame that contradicts the claim fails the
 * run instead of being committed.
 *
 * Usage: node scripts/capture-shell-elapsed-threshold.mjs <outDir> <framesDir>
 */
import { chromium } from 'playwright'
import { mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import { TOOL_ROW_VIEW, makeToolRowScene, openToolRowScene } from './lib/tool-row-scene.mjs'

const OUT = process.argv[2] || '../temp-screenshots/shell-elapsed-threshold'
const FRAMES = process.argv[3] || join(process.env.KIROCREW_SCRATCH || process.env.TMPDIR || '/tmp', 'shell-elapsed-frames')
const SLOT = 'chat-shell-elapsed-threshold'
// Mirrors SHELL_ACTIVITY_MIN_SECS in ToolCallLine.tsx.
const THRESHOLD_S = 10
const FRAME_MS = 500
const RUNNING_PURPOSE = 'Check npm ci progress'

mkdirSync(OUT, { recursive: true })
mkdirSync(FRAMES, { recursive: true })

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: TOOL_ROW_VIEW, deviceScaleFactor: 2 })
  const page = await context.newPage()
  const send = await openToolRowScene(page, { base, slotKey: SLOT, scene: makeToolRowScene(SLOT) })

  /** The status line's DOM state, read fresh: how many are mounted and what
   *  the live one says. */
  const lineState = () => page.evaluate(() => {
    const lines = Array.from(document.querySelectorAll('[data-testid="shell-activity"]'))
    return { count: lines.length, text: lines.map(l => l.textContent?.trim() ?? '').join(' | ') }
  })

  const assertions = []
  const shot = async (name, expect) => {
    const state = await lineState()
    const ok = expect(state)
    assertions.push({ still: name, ...state, ok })
    if (!ok) throw new Error(`${name}: unexpected status-line state ${JSON.stringify(state)}`)
    await page.screenshot({ path: `${OUT}/${name}.png` })
    console.log('wrote', `${OUT}/${name}.png`, JSON.stringify(state))
  }

  // Frame sequence for the GIF: one frame every FRAME_MS from the moment the
  // command starts until it has completed and the line has gone.
  let frameNo = 0
  let recording = true
  const frameLoop = (async () => {
    while (recording) {
      const t0 = Date.now()
      await page.screenshot({ path: join(FRAMES, `f${String(frameNo++).padStart(3, '0')}.png`) })
      const spent = Date.now() - t0
      if (spent < FRAME_MS) await new Promise(r => setTimeout(r, FRAME_MS - spent))
    }
  })()

  const started = Date.now()
  const at = ms => new Promise(r => setTimeout(r, Math.max(0, started + ms - Date.now())))

  send('chat_message', {
    role: 'tool',
    ts: new Date().toISOString(),
    content: '🔧 Watch the install finish',
    meta: { tool_call_id: 'tc-c', purpose: RUNNING_PURPOSE },
  })
  send('tool_call', {
    tool: 'Watch the install finish',
    kind: 'execute',
    purpose: RUNNING_PURPOSE,
    input_preview: 'docker logs -f installer',
    tool_call_id: 'tc-c',
    is_shell: true,
  })

  await page.waitForSelector(`text=${RUNNING_PURPOSE}`, { timeout: 5000 })

  // 1s in: the pill is in flight, and there is NO status line under it.
  await at(1000)
  await shot('01-running-1s-no-line', s => s.count === 0)

  // 6s in: still nothing -- a command this long used to carry a line already.
  await at(6000)
  await shot('02-running-6s-no-line', s => s.count === 0)

  // Past the threshold: the line is up and already counting from the tool's
  // own start, not from its own appearance.
  await at((THRESHOLD_S + 1.5) * 1000)
  await shot('03-line-appears-past-threshold', s => s.count === 1 && /Running · 1[01]s/.test(s.text))

  await at((THRESHOLD_S + 3.5) * 1000)
  await shot('04-line-counting', s => s.count === 1 && /Running · 1[234]s/.test(s.text))

  // The command completes: the line goes away with it.
  send('tool_result', { output: 'added 948 packages in 13s', tool_call_id: 'tc-c' })
  await page.waitForSelector('[data-testid="shell-activity"]', { state: 'detached', timeout: 5000 })
  await page.waitForTimeout(600)
  await shot('05-done-line-gone', s => s.count === 0)

  await page.waitForTimeout(1000)
  recording = false
  await frameLoop

  // Where the running pill sits, so the GIF can be cropped to the part of the
  // transcript that actually changes.
  const pillBox = await page.evaluate(purpose => {
    const pill = Array.from(document.querySelectorAll('button[aria-label*="details for tool"]'))
      .find(b => b.textContent?.includes(purpose))
    const r = pill?.getBoundingClientRect()
    return r ? { top: r.top, bottom: r.bottom, left: r.left, right: r.right } : null
  }, RUNNING_PURPOSE)

  writeFileSync(join(OUT, 'assertions.json'), JSON.stringify({ viewport: TOOL_ROW_VIEW, deviceScaleFactor: 2, thresholdSeconds: THRESHOLD_S, frameMs: FRAME_MS, frames: frameNo, pillBox, stills: assertions }, null, 2))
  console.log('wrote', join(OUT, 'assertions.json'), `frames=${frameNo}`, JSON.stringify(pillBox))

  await context.close()
  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })

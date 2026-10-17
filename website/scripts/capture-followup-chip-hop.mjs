/**
 * Video + screenshot harness for the follow-up option chips' entrance.
 *
 * Runs the REAL built SPA (website/dist) with every /api/** call answered from
 * fixtures and /api/ws bound by Playwright, so the turn ends exactly the way the
 * backend ends it: streamed `chat_chunk` frames, then the terminal
 * `chat_message`. That last frame is what flips the composer from streaming to
 * idle, which is the only moment the option chips mount — so it is the only way
 * to film their entrance.
 *
 * A still cannot carry a staggered animation, hence the recording. The stills
 * alongside it are the settled row (what a reader sees a second later) and a
 * mid-entrance frame, so a reviewer skimming the PR body still sees the ladder
 * without opening the video.
 *
 * Run it twice — once against a dist built from the base ref, once from the
 * change — and the pair is the before/after evidence.
 *
 * Usage: node scripts/capture-followup-chip-hop.mjs <outDir> [label]
 */
import { mkdirSync, renameSync } from 'node:fs'
import { join } from 'node:path'
import { openTranscriptHarness } from './lib/transcript-harness.mjs'

const OUT = process.argv[2] || '../temp-screenshots/followup-chip-hop'
const LABEL = process.argv[3] || 'after'
const SLOT = 'chat-chip-hop'
const PROJECT = '/home/user/workspace/Kiro Crew'
const VIEWPORT = { width: 1200, height: 760 }

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Make the option pills arrive, not blink',
  running: true,
  last_message: 'Staggered the follow-up chips.',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: true,
  has_more: false,
  total: 1,
  queue: [],
  project: PROJECT,
  messages: [{
    role: 'user',
    ts: Date.now() / 1000 - 20,
    content: 'The option pills just blink in — give them an entrance.',
  }],
}

const PROSE = 'Staggered the chips onto the shared entrance ladder: each one rises past its resting line and settles, 55ms apart.'
// Five options so the ladder is legible; the labels are user-voice, as the
// options protocol requires.
const MARKER = '\n\n[OPTIONS: Ship it | Show me the diff | Make it subtler | Slow the ladder down | Revert it]'

/** Cut a string into ~n-char chunks, the granularity a real delta arrives at. */
const chunks = (s, n) => s.match(new RegExp(`[\\s\\S]{1,${n}}`, 'g')) ?? []

async function main() {
  const harness = await openTranscriptHarness({
    slot: SLOT,
    project: PROJECT,
    slots,
    detail,
    viewport: VIEWPORT,
    recordVideo: { dir: OUT, size: VIEWPORT },
  })
  const { page } = harness
  await harness.load('dark')
  const ws = harness.ws()
  if (!ws) throw new Error('websocket route never bound')

  let seq = 0
  /** One `chat_chunk` frame — the same shape useWebSocket's handler consumes. */
  const push = async (content, settle = 90) => {
    ws.send(JSON.stringify({ type: 'chat_chunk', data: { slot: SLOT, content, seq: seq++ } }))
    await page.waitForTimeout(settle)
  }
  const shot = async name => {
    await page.screenshot({ path: `${OUT}/${LABEL}-${name}.png` })
    console.log('wrote', `${OUT}/${LABEL}-${name}.png`)
  }

  // 1. Stream the whole turn, marker included. The composer is still running, so
  //    no chips exist yet — this is the frame the entrance starts from.
  for (const c of chunks(PROSE + MARKER, 12)) await push(c)
  await page.waitForTimeout(1200) // let the smooth reveal drain to the live edge
  await shot('01-before-options')

  // 2. End the turn. The chips mount on this frame; the recording carries the
  //    entrance itself and the two stills bracket it.
  ws.send(JSON.stringify({
    type: 'chat_message',
    data: { slot: SLOT, role: 'assistant', content: PROSE + MARKER, ts: new Date().toISOString() },
  }))
  ws.send(JSON.stringify({ type: 'slots', data: { slots: [{ ...slots[0], running: false }] } }))

  // A screenshot costs tens of ms, so this frame is "early in the entrance"
  // rather than an exact offset — enough to show the ladder mid-flight, with the
  // recording as the authority on timing.
  await page.waitForTimeout(120)
  await shot('02-entrance-mid')

  await page.waitForTimeout(1600)
  await shot('03-settled')

  const video = await harness.close()
  if (video) {
    const dest = join(OUT, `${LABEL}-entrance.webm`)
    if (video !== dest) renameSync(video, dest)
    console.log('wrote', dest)
  }
}

main().catch(err => { console.error(err); process.exit(1) })

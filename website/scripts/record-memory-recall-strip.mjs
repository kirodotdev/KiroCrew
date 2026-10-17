/**
 * Recording harness for the recalled-memory strip's collapsed -> expanded toggle.
 *
 * The sibling `capture-memory-recall-strip.mjs` shoots the two ends of that motion as
 * separate frames, which is the right shape for reading the COPY but says nothing about
 * the transition a reader actually performs: whether the chevron turns, whether the
 * panel appears under the line it belongs to rather than displacing the reply above it,
 * and whether the row settles at a height the virtualized transcript keeps stable. Those
 * are questions only a moving picture answers, so this file exists beside the stills
 * rather than replacing them.
 *
 * Same fixtures, same real built SPA (website/dist), same stubbed network: nothing here
 * is a mock of the strip itself. One narrowed record is placed on one assistant reply,
 * and the recording opens the panel, holds it, closes it and holds again -- so the frame
 * a reader stops on is a full state, not a half-played animation.
 *
 * Playwright writes webm. `--gif` additionally converts with ffmpeg when it is on PATH,
 * because GitHub renders a gif inline in a PR body while a webm becomes an attachment a
 * reader has to click. The conversion is skipped with a note rather than failing: the
 * webm is the artefact, the gif is a convenience.
 *
 * Usage: node scripts/record-memory-recall-strip.mjs [outDir] [--gif]
 */
import { execFileSync } from 'node:child_process'
import { mkdirSync, renameSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { openTranscriptHarness } from './lib/transcript-harness.mjs'

const args = process.argv.slice(2)
const WANT_GIF = args.includes('--gif')
const OUT = args.find(a => !a.startsWith('--')) || '../temp-screenshots/memory-recall-strip-toggle'
const SLOT = 'chat-memory-recall'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

const STRIP = '[data-testid="memory-recall-strip"]'
const TOGGLE = '[data-testid="memory-recall-strip-toggle"]'

/** Consent on, every scope at the value the card records for it. */
const CONSENT = {
  enabled: true,
  endpoint: 'https://api.typesafe.ai/v1/systemone',
  configured_endpoint: 'https://api.typesafe.ai/v1/systemone',
  permits: true,
  tool_args: false,
  compaction: false,
  memory_text: true,
}

/** The common case: six memories recalled by similarity, three kept. */
const NARROWED = {
  turn_id: 'turn-7d1e04',
  ts: '2026-09-21T07:04:11Z',
  point: 'memory.recall',
  latency_ms: 210,
  baseline_keys: ['mem-4f2a9c', 'mem-8b71d0', 'mem-1c34ee', 'mem-90ab52', 'mem-2d6f18', 'mem-55e7c1'],
  jev_keys: ['mem-4f2a9c', 'mem-1c34ee', 'mem-55e7c1'],
  p: 0.81,
  chars_saved: 2100,
  candidates: 6,
  message_chars: 96,
  error: null,
}

const t0 = Date.now() / 1000 - 900

const slots = [
  {
    key: SLOT,
    title: 'Where do we deploy the signer',
    running: false,
    last_message: 'us-west-2, and the rollback runbook is in the ops repo.',
    messages: 2,
    agent: 'kirocrew',
    memory_mode: 'persistent',
    project: PROJECT,
    modified: Math.floor(Date.now() / 1000),
    source_links: [],
    source_links_total: 0,
  },
]

// ONE decided reply, so the recording has exactly one strip and no ambiguity about
// which row the motion belongs to.
const detail = {
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: t0, content: 'Where do we deploy the signer, and who owns the rollback?' },
    {
      role: 'assistant',
      ts: t0 + 22,
      content: 'us-west-2, and the rollback runbook is in the ops repo under `runbooks/signer.md`.',
      meta: { decisions_strip: NARROWED },
    },
  ],
}

async function main() {
  const { page, load, close } = await openTranscriptHarness({
    slot: SLOT,
    project: PROJECT,
    slots,
    detail,
    viewport: { width: 1100, height: 620 },
    // 1x: a video is read at its rendered size, and 2x doubles the file for no gain.
    deviceScaleFactor: 1,
    recordVideo: { dir: OUT, size: { width: 1100, height: 620 } },
  })

  await page.route('**/api/decisions/consent', route => json(route, CONSENT))

  await load('dark', { selector: STRIP })
  // The locale pin has to survive the harness's localStorage clear, which runs on
  // every navigation -- so it is registered after the first load and applied by a
  // reload, the same sequence the stills harness uses.
  await page.addInitScript(() => localStorage.setItem('mc-lang', 'en'))
  await page.reload({ waitUntil: 'domcontentloaded' })
  await page.waitForSelector(STRIP, { timeout: 20000 })
  await page.waitForTimeout(1200)

  const row = page.locator(STRIP).first()
  await row.evaluate(el => el.scrollIntoView({ block: 'center' }))
  await page.waitForTimeout(1200)

  // Dispatched on the node rather than clicked through hit-testing: the transcript is
  // virtualized, so a neighbouring row's box can sit over the strip and a real click
  // times out. This still runs the component's own onClick, which is the surface here.
  const toggle = row.locator(TOGGLE).first()
  for (const hold of [1600, 1600]) {
    await toggle.evaluate(el => el.click())
    await page.waitForTimeout(hold)
  }

  const raw = await close()
  const dest = join(OUT, 'memory-recall-strip-toggle.webm')
  renameSync(raw, dest)
  console.log('wrote', dest)

  if (!WANT_GIF) return
  const gif = join(OUT, 'memory-recall-strip-toggle.gif')
  try {
    execFileSync(
      'ffmpeg',
      ['-y', '-i', dest, '-vf', 'fps=12,scale=1100:-1:flags=lanczos', '-loop', '0', gif],
      { stdio: 'ignore' },
    )
    console.log('wrote', gif)
  } catch {
    console.log('no ffmpeg on PATH; keeping the webm only')
  }
}

main()

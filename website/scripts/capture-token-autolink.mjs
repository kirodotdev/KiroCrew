/**
 * Screenshot harness for the bare-token autolink seam.
 *
 * Runs the REAL built SPA behind the shared transcript harness, so the assistant
 * bubble goes through the actual remark/rehype pipeline. The change is a markdown
 * SOURCE rewrite, so only the real pipeline can photograph it.
 *
 * TWO BUILDS, because the seam is inert on its own. The core registers no rules,
 * so a stock build renders exactly as it did before this change — that is the
 * design, and it means the delta only appears once an edition registers a rule:
 *
 *   before -> dist built with KIROCREW_EDITION_DIR unset (stock, no rules)
 *   after  -> dist built with a demo edition registering one TICKET-\d+ rule
 *
 * The "before" frame is therefore also the evidence for the no-op claim: it is
 * what every stock build keeps rendering.
 *
 * The seeded message carries three lines, because one frame has to answer three
 * questions at once:
 *
 *   1. Does a bare token in prose become a link. (The feature.)
 *   2. Does a token inside a code span stay literal. (It is being SHOWN, not
 *      referenced — rewriting it would corrupt quoted text.)
 *   3. Does a pasted URL that already contains the token survive intact. This is
 *      the load-bearing one: a pasted link is the likeliest place a token
 *      appears, and rewriting inside a live href splices markdown into a URL.
 *
 * Usage: node scripts/capture-token-autolink.mjs <outDir> [label]
 */
import { mkdirSync } from 'node:fs'
import { openTranscriptHarness } from './lib/transcript-harness.mjs'

const OUT = process.argv[2] || '../temp-screenshots/token-autolink'
const LABEL = process.argv[3] || 'after'
const SLOT = 'chat-token-autolink'
const PROJECT = '/home/user/workspace/KiroCrew'
const BUBBLE = '[data-role="assistant"] .msg-content'

const TRANSCRIPT = [
  'Ready for review: TICKET-1234 is the change under test.',
  '',
  'The same token inside a code span stays literal: `TICKET-1234`.',
  '',
  'A pasted link keeps its href intact: https://tickets.example.com/TICKET-1234 — the token inside it is not rewritten.',
].join('\n')

const now = Date.now() / 1000

const slots = [{
  key: SLOT,
  title: 'Bare token autolinking',
  running: false,
  last_message: 'ready for review',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  modified: Math.floor(now),
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: now - 30, content: 'What is the status of TICKET-1234?' },
    { role: 'assistant', ts: now - 10, content: TRANSCRIPT },
  ],
}

async function main() {
  mkdirSync(OUT, { recursive: true })
  // Modest viewport at 1x: the frame is one bubble of prose, and a 2x shot of it
  // lands large enough to wedge an agent session that reads the file back.
  const h = await openTranscriptHarness({
    slot: SLOT,
    project: PROJECT,
    slots,
    detail,
    viewport: { width: 1100, height: 700 },
    deviceScaleFactor: 1,
  })

  for (const theme of ['dark', 'light']) {
    await h.load(theme, { selector: BUBBLE, settle: 2000 })
    // Photograph the bubble, not the whole shell: the delta is a few characters
    // wide and a full-window frame buries it in chrome.
    const path = `${OUT}/${LABEL}-${theme}.png`
    await h.page.locator(BUBBLE).first().screenshot({ path })
    console.log('wrote', path)
  }

  await h.close()
}

main().catch(err => { console.error(err); process.exit(1) })

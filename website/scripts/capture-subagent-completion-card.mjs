/**
 * Screenshot harness for SubagentCompletionCard.
 *
 * Runs the REAL built SPA (website/dist) against a static file server with every
 * /api/** call answered from fixtures (see lib/transcript-harness.mjs). No
 * gateway, no sub-agents actually spawned — the transcript is seeded with the
 * exact text the gateway injects, so the card's parsing and layout are exercised
 * as they are in production.
 *
 * Usage: node scripts/capture-subagent-completion-card.mjs [outDir]
 */
import { mkdirSync } from 'node:fs'
import { openTranscriptHarness } from './lib/transcript-harness.mjs'

const OUT = process.argv[2] || '../temp-screenshots/subagent-completion-card'
const SLOT = 'chat-completion'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Translate the two new UI labels',
  running: false,
  last_message: 'All ten locales are updated.',
  messages: 8,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const t = Date.now() / 1000

/** A per-agent completion event, exactly as gateway._subagent_done composes it. */
const single = [
  '[Subagent completion event]',
  'Agent `53e3e5eb` (kirocrew) completed ✅',
  'Task: Add TWO short UI labels to the GERMAN (de) catalog',
  '',
  'Added `copy_command` and `copied` to de.json and re-ran the parity check — 8340 keys, no strays.',
].join('\n')

/** A failed per-agent completion: the danger state and its inline error detail. */
const failed = [
  '[Subagent completion event]',
  'Agent `7654e2b3` (kirocrew) failed ❌',
  'Task: Add TWO short UI labels to the FRENCH (fr) catalog',
  '',
  'Error: catalog parity check failed — fr.json is missing `copied`.',
].join('\n')

/** A mid-wave digest chunk: results so far, with the rest still running. */
const chunk = [
  '[Subagent batch completion event]',
  'Batch results 1/2 — 10 of 18 delivered, 8 still running.',
  'Process these results now, but do NOT spawn new sub-agents yet — more result batches from this run are still arriving, and spawning now will interleave with them.',
  'Failures are listed first. Full outputs are on disk — read the result paths on demand; do NOT re-run completed agents.',
  '',
  '— `53e3e5eb` ✅ Add TWO short UI labels to the GERMAN (de) catalog',
  '  → /home/user/.kiro/crew/subagents/53e3e5eb/result.txt',
  '— `b8185d65` ✅ Add TWO short UI labels to the SPANISH (es) catalog',
  '  → /home/user/.kiro/crew/subagents/b8185d65/result.txt',
].join('\n')

/** The wave's final digest, carrying the terminal tallies. */
const wave = [
  '[Subagent batch completion event]',
  'Batch results 2/2 — wave finished: 16 ✅ · 1 ❌ · 1 ⏹ of 18 agents. All results delivered.',
  'This run is complete. Finish processing all results before spawning any follow-up sub-agents.',
  'Failures are listed first. Full outputs are on disk — read the result paths on demand; do NOT re-run completed agents.',
  '',
  '— `7654e2b3` failed ❌ · Add TWO short UI labels to the FRENCH (fr) catalog',
  '  Error: catalog parity check failed — fr.json is missing `copied`.',
  '— `c19d0a44` stopped by user ⏹ · Add TWO short UI labels to the RUSSIAN (ru) catalog',
  '  Stopped by the user before completing.',
  '— `a0417f21` ✅ Add TWO short UI labels to the ITALIAN (it) catalog',
  '  → /home/user/.kiro/crew/subagents/a0417f21/result.txt',
  '— `d5c3b210` ✅ Add TWO short UI labels to the PORTUGUESE (pt) catalog',
  '  → /home/user/.kiro/crew/subagents/d5c3b210/result.txt',
].join('\n')

const detail = {
  running: false,
  has_more: false,
  total: 8,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: t - 900, content: 'Translate the two new copy-command labels into every locale.' },
    { role: 'assistant', ts: t - 890, content: 'Spawned 18 sub-agents, one per locale. Waiting for results.' },
    { role: 'subagent', ts: t - 700, content: single },
    { role: 'assistant', ts: t - 690, content: 'German is in. Waiting on the rest of the wave.' },
    { role: 'subagent', ts: t - 600, content: failed },
    { role: 'subagent', ts: t - 500, content: chunk },
    { role: 'subagent', ts: t - 300, content: wave },
    { role: 'assistant', ts: t - 290, content: 'All ten locales are updated. One French run needs a retry and the Russian one was stopped.' },
  ],
}

async function main() {
  const { page, load, close } = await openTranscriptHarness({
    slot: SLOT,
    project: PROJECT,
    slots,
    detail,
    viewport: { width: 1280, height: 1000 },
  })

  const shot = async name => {
    await page.screenshot({ path: `${OUT}/${name}.png` })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  for (const theme of ['dark', 'light']) {
    await load(theme, { selector: '[data-testid="subagent-completion-card"]', settle: 1200 })
    const cards = page.locator('[data-testid="subagent-completion-card"]')
    console.log(`${theme}: ${await cards.count()} card(s) rendered`)
    await shot(`transcript-${theme}`)
  }

  await close()
}

main().catch(err => { console.error(err); process.exit(1) })

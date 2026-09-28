/**
 * Screenshot harness for the MCP-App ui/message transcript rows.
 *
 * Runs the REAL built SPA (website/dist) with every /api/** call answered from
 * fixtures — no gateway, no agent: the inject-row renderer, the app-label
 * pill, the wrapper stripping and the queued twin render exactly as in
 * production.
 *
 * Two scenes, because the diff introduces two delivery states:
 *
 *  - DISPATCHED. The message arrived on an idle slot and became a turn: an
 *    `inject` row with `meta.injectKind: "mcp_app"` / `meta.appLabel`. The
 *    claim a picture must carry: the bubble shows the app's text CLEAN (no
 *    `[MCP app message from …]` machine envelope) under an app-labelled pill,
 *    the way a cron row shows its clock-labelled pill.
 *
 *  - QUEUED. The message arrived while a turn was live: the `queued` twin row
 *    rendered from the queue card.
 *
 * Every frame asserts the words that make it that state: the app label is in
 * frame, the envelope banner is NOT (dispatched scene), and the message text
 * itself is visible. A capture that only writes PNGs fails toward a false
 * pass, so absence of the banner is checked rather than described.
 *
 * Usage: node scripts/capture-mcp-app-message-rows.mjs [outDir]
 */
import { mkdirSync } from 'node:fs'

import { json } from './lib/boot-api.mjs'
import { openTranscriptHarness } from './lib/transcript-harness.mjs'

const OUT = process.argv[2] || '../temp-screenshots/mcp-app-message-rows'
const SLOT = 'chat-app-message'
const PROJECT = '/home/user/workspace/KiroCrew'

// mise's node injects LD_LIBRARY_PATH pointing at its own libstdc++ at process
// startup (even bypassing the shim), and chromium inherits it and fails with
// GLIBCXX version errors against system libs. Scrub it before the launch.
delete process.env.LD_LIBRARY_PATH

mkdirSync(OUT, { recursive: true })

const t0 = Math.floor(Date.now() / 1000) - 600
const APP_LABEL = 'deploy-status/notify'
const BANNER_OPEN = `[MCP app message from "${APP_LABEL}"]`
const BANNER_CLOSE = '[End of MCP app message]'
const ACK_TEXT = '✅ Acknowledged: "Deploy summary ready"'
const WRAPPED = `${BANNER_OPEN}\n${ACK_TEXT}\n${BANNER_CLOSE}`

/** Dispatched-as-a-turn: the inject row + the assistant's reaction. */
const DISPATCHED = [
  { role: 'user', ts: t0, content: 'Render the deploy summary card for me.' },
  { role: 'assistant', ts: t0 + 8, content: 'Here is the deploy summary — the card above has an Acknowledge button when you are done reading.' },
  {
    role: 'inject',
    ts: t0 + 95,
    content: WRAPPED,
    cls: JSON.stringify({ appLabel: APP_LABEL }),
    meta: { injectKind: 'mcp_app', appLabel: APP_LABEL },
  },
  { role: 'assistant', ts: t0 + 101, content: 'Noted — you acknowledged the deploy summary, so I will proceed with the rollout.' },
]

/** Queued behind a live turn: the queued twin rendered from the slot-detail
 *  `queue` field (the store strips role-queued MESSAGE rows and hydrates
 *  bubbles from that field, so the fixture carries the state where the
 *  frontend actually reads it). */
const QUEUED = [
  { role: 'user', ts: t0, content: 'Start the long migration and keep me posted.' },
  { role: 'assistant', ts: t0 + 8, content: 'Migration running — step 3 of 9, writing the schema shims now…' },
]
const QUEUED_QUEUE = [
  // API wire shape: `id` + `meta.{kind,appLabel}` (fetchSlotDetail normalizes these).
  { content: WRAPPED, id: 'q-1', meta: { kind: 'mcp_app_message', appLabel: 'deploy-status/notify' } },
]

const SCENES = { '01-dispatched-turn': DISPATCHED, '02-queued-behind-turn': QUEUED }

const slots = [{
  key: SLOT, title: 'MCP App message rows', running: false, unread: 0,
  messages: 4, agent: 'kirocrew', memory_mode: 'persistent', project: PROJECT,
  modified: Math.floor(Date.now() / 1000), source_links: [], source_links_total: 0,
}]

// MUTABLE: the harness's /api/** route reads this at request time, so swapping
// `messages` and cold-loading serves the next scene.
const detail = { running: false, has_more: false, total: 4, key: SLOT, title: 'MCP App message rows', messages: [] }

const failures = []
const check = (ok, msg) => {
  if (!ok) failures.push(msg)
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${msg}`)
}

const h = await openTranscriptHarness({ slot: SLOT, project: PROJECT, slots, detail })

for (const theme of ['light', 'dark']) {
  for (const [name, messages] of Object.entries(SCENES)) {
    detail.messages = messages
    detail.total = messages.length
    // The queued twin is a composer-area card that only renders while a turn
    // is LIVE — that is the state it exists in — so scene 02 must say so, and
    // carries its entry in the detail's `queue` field where the store reads it.
    const running = name === '02-queued-behind-turn'
    detail.running = running
    detail.queue = running ? QUEUED_QUEUE : []
    slots[0].running = running
    await h.load(theme, { settle: 1200 })
    const body = await h.page.locator('body').innerText()
    // Both states wear the same honest attribution: "Sent by the {app} app"
    // (host-authored chrome states only what the host verified — no gesture
    // claim), on the transcript row AND the queued card.
    const pillOk = body.includes('Sent by the deploy-status app')
    check(pillOk, `${name} ${theme}: the row carries the app attribution (honest wording)`)
    check(body.includes('Deploy summary ready'), `${name} ${theme}: the message text itself is visible`)
    check(!body.includes(BANNER_OPEN), `${name} ${theme}: the machine envelope is stripped`)
    check(!body.includes(BANNER_CLOSE), `${name} ${theme}: the end banner is stripped too`)
    const file = `${OUT}/${name}-${theme}.png`
    await h.page.screenshot({ path: file, fullPage: false })
    console.log(`wrote ${file}`)
  }
}

await h.close()
if (failures.length) {
  console.error(`\n${failures.length} assertion(s) failed`)
  process.exit(1)
}
console.log('\nall scenes verified')

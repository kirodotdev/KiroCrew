/**
 * Screenshot harness for the purpose-less shell pill re-label.
 *
 * kiro-cli injects a `__tool_use_purpose` argument into tool schemas and the
 * dashboard shows that prose as the pill label. The injection is inconsistent
 * for built-in shell calls, and when it is absent kiro-cli's auto-title is a
 * digest of ARGUMENT FRAGMENTS ("--title, --text, Three, 1. ...") — the pill
 * used to render that verbatim. The fix labels such pills from the command in
 * the call's raw input instead: verbatim when short, the binary digest when
 * flood-length.
 *
 * The transcript rendered here mixes the three shapes a real session carries:
 *   1. a tool call WITH a purpose            → prose label (unchanged)
 *   2. a purpose-less call, soup title,
 *      short command                         → the command, verbatim (fixed)
 *   3. a purpose-less call, soup title,
 *      flood-length command                  → derived digest (fixed)
 *
 * Runs the REAL built SPA (website/dist) behind the shared transcript harness
 * with every /api/** call answered from fixtures — no gateway, no token.
 *
 * Usage: node scripts/capture-purposeless-shell-pill.mjs [outDir]
 */
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { openTranscriptHarness } from './lib/transcript-harness.mjs'

const OUT = process.argv[2] || '../temp-screenshots/purposeless-shell-pill'
const SLOT = 'chat-purposeless-shell-pill'
const PROJECT = '/home/user/workspace/oncall-context'

mkdirSync(OUT, { recursive: true })

const SHORT_CMD =
  'cd ~/apps/oncall-radar/backend && $PY ledger.py ticket-log --id P503465049 --type root-cause --title "FetchTranscript TPS" --text "Three findings: 1. ..."'
const LONG_CMD = `export PATH="/usr/local/bin:$PATH"\ncd ~/apps/oncall-radar/backend\npython3 ledger.py ticket-deps --id P503465049 --deps '${'x'.repeat(240)}' | tee /tmp/deps.log`

const t0 = Date.now() / 1000 - 600

const slots = [
  {
    key: SLOT,
    title: 'Triage P503465049',
    running: false,
    last_message: 'Findings logged to the ledger.',
    messages: 6,
    agent: 'kirocrew',
    memory_mode: 'persistent',
    project: PROJECT,
    modified: Math.floor(Date.now() / 1000),
    source_links: [],
    source_links_total: 0,
  },
]

/** Historical rows (meta.kind/input/purpose, see _tool_meta in chat_runner.py):
 *  exactly what a restored session carries, which is also the path old
 *  transcripts take through the re-label. */
const detail = {
  running: false,
  has_more: false,
  total: 6,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: t0, content: 'Investigate the FetchTranscript TPS ticket and log findings.' },
    { role: 'assistant', ts: t0 + 5, content: 'Reading the ticket, then logging the root cause to the ledger.' },
    {
      role: 'tool',
      ts: t0 + 9,
      content: '🔧 Read the full ticket P503465049 including all comments',
      meta: {
        tool_call_id: 'tc_a',
        kind: 'unknown',
        purpose: 'Read the full ticket P503465049 including all comments',
        input: JSON.stringify({ action: 'get-ticket', __tool_use_purpose: 'Read the full ticket P503465049 including all comments' }),
        output: '{"status": "Assigned"}',
      },
    },
    {
      role: 'tool',
      ts: t0 + 30,
      content: '🔧 --title, --text, Three, 1. ...',
      meta: {
        tool_call_id: 'tc_b',
        kind: 'execute',
        input: JSON.stringify({ command: SHORT_CMD }),
        output: '{"ok": true, "entries": 5}',
      },
    },
    {
      role: 'tool',
      ts: t0 + 41,
      content: '🔧 --deps, xxxxxxxx, ...',
      meta: {
        tool_call_id: 'tc_c',
        kind: 'execute',
        input: JSON.stringify({ command: LONG_CMD }),
        output: '{"ok": true}',
      },
    },
    { role: 'assistant', ts: t0 + 50, content: 'Findings logged to the ledger. Root cause and deps recorded.' },
  ],
}

const h = await openTranscriptHarness({ slot: SLOT, slots, detail, project: PROJECT })

// Simplified tool names ON — the mode whose fallback this change fixes.
await h.page.addInitScript(() => {
  localStorage.setItem('mc-chat-config', JSON.stringify({ simplifiedToolNames: true }))
})

await h.load('dark', { selector: 'textarea', settle: 1200 })
// The tool rows live inside a collapsed "Worked through N steps" group —
// expand it so the pills are the subject of the shot.
await h.page.getByText(/Worked through/, { exact: false }).first().click()
const shortPill = h.page.getByText('Run cd ~/apps/oncall-radar/backend', { exact: false }).first()
await shortPill.waitFor({ timeout: 10000 })
await h.page.waitForTimeout(400)
const box = await shortPill.boundingBox()
await h.page.screenshot({
  path: join(OUT, 'pills-dark.png'),
  clip: { x: Math.max(0, box.x - 620), y: Math.max(0, box.y - 240), width: 1280, height: 420 },
})
console.log('DARK shot taken')

await h.load('light', { selector: 'textarea', settle: 1200 })
await h.page.getByText(/Worked through/, { exact: false }).first().click()
const lightPill = h.page.getByText('Run cd ~/apps/oncall-radar/backend', { exact: false }).first()
await lightPill.waitFor({ timeout: 10000 })
await h.page.waitForTimeout(400)
const lightBox = await lightPill.boundingBox()
await h.page.screenshot({
  path: join(OUT, 'pills-light.png'),
  clip: { x: Math.max(0, lightBox.x - 620), y: Math.max(0, lightBox.y - 240), width: 1280, height: 420 },
})
console.log('LIGHT shot taken')

console.log('DONE', OUT)
await h.close()

/**
 * Screenshot harness for the purpose-less shell pill label.
 *
 * kiro-cli injects a `__tool_use_purpose` argument into tool schemas and the
 * dashboard shows that prose as the pill label. The injection is inconsistent
 * for built-in shell calls, and when it is absent kiro-cli's auto-title is a
 * digest of ARGUMENT FRAGMENTS ("--title, --text, Three, 1. ...") — a title
 * that is not the command, so R0.0 in utils/toolCallTitle took it as the
 * model's description and the pill rendered it verbatim. The fix recognizes a
 * title assembled from the command's own pieces as a digest, not a
 * description, so the row derives its label from `rawInput.command` like any
 * other purpose-less shell call: a classified action when the command parses,
 * the command itself in the code face when it is short, the binary digest when
 * it is flood-length.
 *
 * The transcript rendered here mixes the three shapes a real session carries:
 *   1. a tool call WITH a purpose            → prose label (unchanged)
 *   2. a purpose-less call, soup title,
 *      short `$PY` command                   → the command, verbatim (fixed)
 *   3. a purpose-less call, soup title,
 *      flood-length multi-line script        → the binary digest (fixed)
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

// 79 characters: inside the 80-character raw-title budget, so the row shows the
// command itself. The `$PY` head is what the shell classifier refuses.
const SHORT_CMD = 'cd backend && $PY ledger.py ticket-log --title "Three findings" --text "1. TPS"'
const SHORT_TITLE = '--title, --text, Three, 1. ...'
// Multi-line, flood-length: the first line is bookkeeping and the real work
// names two binaries, so the digest (`ledger.py ticket-deps, tee`) outranks a
// raw first-line cut.
const LONG_CMD = `export PATH="/usr/local/bin:$PATH"\ncd ~/apps/oncall-radar/backend\npython3 ledger.py ticket-deps --id TCK-84213 --deps '${'x'.repeat(240)}' | tee /tmp/deps.log`
const LONG_TITLE = '--deps, xxxxxxxx, ...'
const LONG_DIGEST = 'ledger.py ticket-deps, tee'

const t0 = Date.now() / 1000 - 600

const slots = [
  {
    key: SLOT,
    title: 'Triage TCK-84213',
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
 *  exactly what a restored session carries. A live kiro-cli row already has
 *  the command on its title (_select_tool_title), so the persisted row is
 *  where the argument digest survives — and the path the fix must cover. */
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
      content: '🔧 Read the full ticket TCK-84213 including all comments',
      meta: {
        tool_call_id: 'tc_a',
        kind: 'unknown',
        purpose: 'Read the full ticket TCK-84213 including all comments',
        input: JSON.stringify({ action: 'get-ticket', __tool_use_purpose: 'Read the full ticket TCK-84213 including all comments' }),
        output: '{"status": "Assigned"}',
      },
    },
    {
      role: 'tool',
      ts: t0 + 30,
      content: `🔧 ${SHORT_TITLE}`,
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
      content: `🔧 ${LONG_TITLE}`,
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

// The pills below render under simplified tool names, the mode whose fallback
// this change fixes. That mode is the DEFAULT (`DEFAULTS.simplifiedToolNames`
// in src/pages/chat/ChatSettings.tsx), and `h.load()` runs
// `localStorage.clear()` in its own init script on every navigation before
// seeding the theme and slot, so no `mc-chat-config` seed is registered here:
// a seed registered ahead of load() is wiped by that clear(). `pillsReady`
// waits for the derived labels themselves, so a flipped default fails this
// script loudly instead of capturing raw titles.

/** Expand the collapsed "Worked through N steps" group, then assert the three
 *  pill labels and return the short pill for the clip anchor. */
async function pillsReady(page) {
  await page.getByText(/Worked through/, { exact: false }).first().click()
  const shortPill = page.getByText(SHORT_CMD, { exact: true }).first()
  await shortPill.waitFor({ timeout: 10000 })
  await page.getByText(LONG_DIGEST, { exact: true }).first().waitFor({ timeout: 10000 })
  await page.getByText('Read the full ticket TCK-84213 including all comments', { exact: true }).first().waitFor({ timeout: 10000 })
  for (const soup of [SHORT_TITLE, LONG_TITLE]) {
    if ((await page.getByText(soup, { exact: true }).count()) > 0) throw new Error(`argument digest still on a pill: ${soup}`)
  }
  await page.waitForTimeout(400)
  return shortPill
}

await h.load('dark', { selector: 'textarea', settle: 1200 })
const shortPill = await pillsReady(h.page)
const box = await shortPill.boundingBox()
await h.page.screenshot({
  path: join(OUT, 'pills-dark.png'),
  clip: { x: Math.max(0, box.x - 620), y: Math.max(0, box.y - 240), width: 1280, height: 420 },
})
console.log('DARK shot taken')

await h.load('light', { selector: 'textarea', settle: 1200 })
const lightPill = await pillsReady(h.page)
const lightBox = await lightPill.boundingBox()
await h.page.screenshot({
  path: join(OUT, 'pills-light.png'),
  clip: { x: Math.max(0, lightBox.x - 620), y: Math.max(0, lightBox.y - 240), width: 1280, height: 420 },
})
console.log('LIGHT shot taken')

console.log('DONE', OUT)
await h.close()

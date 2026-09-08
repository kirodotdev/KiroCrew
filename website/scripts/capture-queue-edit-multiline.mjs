/**
 * Screenshot evidence for the queued-message inline editor holding a
 * MULTI-LINE entry: the attachment serializer writes one `[attached_file N]
 * path` marker per line, and the editor used to be a single-line <input>,
 * which drops every newline from its value. Editing a two-attachment entry
 * therefore glued the markers onto one line, and the queue edit's
 * whitespace-bounded marker match then pruned every attachment but the last.
 *
 * Runs the REAL SPA (same fixture shape as capture-chatpane-queue-edit.mjs):
 * a persisted 2-pane split, the background pane carrying ONE queued entry
 * whose content is a caption plus two attachment markers on their own lines.
 * The frame ASSERTS the editor state before it is written:
 *   MODE=after  (default) the editor is a <textarea>, its value still carries
 *               both newlines, and stays one visible row (the card slot is
 *               fixed-height)
 *   MODE=before run against a checkout of the base commit: the editor is an
 *               <input> and its value has NO newline left -- the defect
 *
 * Usage (from website/):
 *   node scripts/capture-queue-edit-multiline.mjs http://127.0.0.1:6841 ../temp-screenshots/queue-edit-multiline
 *   MODE=before node scripts/capture-queue-edit-multiline.mjs http://127.0.0.1:6842 ../temp-screenshots/queue-edit-multiline
 */
import { chromium } from 'playwright'
import { prepareSplitChatPage } from './lib/prepare-split-chat-page.mjs'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6841'
const OUT = process.argv[3] || '../temp-screenshots/queue-edit-multiline'
const MODE = process.env.MODE === 'before' ? 'before' : 'after'
mkdirSync(OUT, { recursive: true })

const now = Date.now() / 1000
const QUEUED = 'Summarize both reports.\n[attached_file 1] /home/user/.kiro/crew/uploads/Q3 roster report.pdf\n[attached_file 2] /home/user/.kiro/crew/uploads/Q4 plan.pdf'

const slots = [
  { key: 'pane-a', title: 'Design notes', running: false, last_message: 'Working through phase one…', messages: 2, agent: 'kirocrew', memory_mode: 'persistent', modified: Math.floor(now) },
  { key: 'pane-b', title: 'Release checklist', running: true, last_message: 'Summarized the layout options.', messages: 3, agent: 'kirocrew', memory_mode: 'persistent', modified: Math.floor(now) - 60 },
]
const detailA = {
  running: false, has_more: false, total: 2, queue: [],
  messages: [
    { role: 'user', ts: now - 300, content: 'Compare the two layout options.', cls: 'msg msg-user' },
    { role: 'assistant', ts: now - 240, content: 'Option A keeps the sidebar fixed; option B collapses it under 900px.', cls: 'msg msg-assistant' },
  ],
}
const detailB = {
  running: true, has_more: false, total: 3, queue: [],
  messages: [
    { role: 'user', ts: now - 120, content: 'Run the deployment checklist for the new release.', cls: 'msg msg-user' },
    { role: 'assistant', ts: now - 60, content: 'Starting phase one: build verification. This will take a few minutes.', cls: 'msg msg-assistant' },
    { role: 'queued', ts: now - 10, content: QUEUED, cls: 'msg msg-queued', meta: { queueId: 'q-demo-1' } },
  ],
}
const splitLayouts = {
  'pane-a': {
    type: 'split', id: 'seed-split', dir: 'col',
    children: [
      { type: 'leaf', id: 'seed-a', kind: 'session', slot: 'pane-a' },
      { type: 'leaf', id: 'seed-b', kind: 'session', slot: 'pane-b' },
    ],
    sizes: [0.5, 0.5],
  },
}
const json = (route, body) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
const FIXTURES = {
  '/api/chat/slots': slots,
  '/api/kiro-prerequisite': {
    platform: 'linux', installed: true, authenticated: true, ready: true,
    initial_setup_complete: true, can_auto_install: false, can_login: false,
    repair_required: false, docs_url: '', setup_allowed: false,
    operation: { kind: '', status: 'idle', message: '', detail: '', url: '', error: '' },
  },
  '/api/dashboard/config': { session_grid: true },
}

let failed = false
function check(name, ok, detail) {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
}

async function capture(theme) {
  const browser = await chromium.launch()
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2, colorScheme: theme })
  const page = await prepareSplitChatPage(ctx, { base: BASE, fixtures: FIXTURES, detailA, detailB, splitLayouts, json, theme })
  const pencil = page.getByLabel('Edit queued message')
  await pencil.waitFor({ state: 'visible', timeout: 15000 })
  await pencil.first().click()
  const editor = page.getByRole('textbox', { name: 'Edit queued message' })
  await editor.waitFor({ state: 'visible', timeout: 5000 })
  const tag = await editor.evaluate(el => el.tagName)
  const value = await editor.inputValue()
  const rows = await editor.evaluate(el => (el instanceof HTMLTextAreaElement ? el.rows : 1))
  const newlines = value.split('\n').length - 1
  const applied = await page.evaluate(() => document.documentElement.dataset.mode)
  check(`${theme} theme applied`, applied === theme, `data-mode=${applied}`)
  if (MODE === 'after') {
    check(`${theme} editor tag`, tag === 'TEXTAREA', tag)
    check(`${theme} newlines kept`, newlines === 2, `${newlines} newline(s) in value`)
    check(`${theme} rows`, rows === 1, `rows=${rows}`)
    const sel = await editor.evaluate(el => [el.selectionStart, el.selectionEnd])
    check(`${theme} first line selected`, sel[0] === 0 && sel[1] === QUEUED.indexOf('\n'), `selection=${sel}`)
    const cue = await page.getByTestId('queue-edit-hidden-lines').textContent()
    check(`${theme} hidden-lines cue`, cue === '+2 attachments', JSON.stringify(cue))
  } else {
    check(`${theme} editor tag`, tag === 'INPUT', tag)
    check(`${theme} newlines dropped`, newlines === 0, `${newlines} newline(s) in value`)
  }
  // Frame the background pane (the one holding the editor) so the composite
  // shows the editor and the card around it, not the whole split.
  const pane = page.locator('[data-chat-pane]').nth(1)
  await pane.screenshot({ path: `${OUT}/${theme}-${MODE}.png` })
  await ctx.close()
  await browser.close()
}

await capture('light')
await capture('dark')
if (failed) { console.error('frame assertions failed'); process.exit(1) }
console.log(`all frames verified (${MODE}) →`, OUT)

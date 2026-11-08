/**
 * Screenshot harness for "the knowledge block never shows as the user's own
 * words" (issue #18075).
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures: no gateway, no token.
 * The chat is opened cold, the way a reload serves it: the first user row is
 * the wire text the gateway stored, opening with the injected
 * `[KNOWLEDGE CONTEXT ...]` block, plus its persisted `meta.knowledge`.
 *
 * Frames (per theme):
 *   before-<theme>.png  the row with no `meta.knowledge.envelope` fingerprint:
 *                       rendered exactly as main renders every row (block shown)
 *   after-<theme>.png   the row a send on this PR writes (fingerprint present):
 *                       chip + typed text, block gone
 *
 * Usage (in website/): node scripts/capture-18075-knowledge-envelope.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'

import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = resolve(process.argv[2] || '../temp-screenshots/18075-knowledge-envelope')
mkdirSync(OUT, { recursive: true })
const ACTIVE = 'kb-envelope'
const TYPED = 'Using the runbook above, which step do I skip on a fresh host?'
const ITEM = { title: 'Gateway restart runbook', source: 'runbooks/gateway.md', content: '1. Drain the queue.\n2. Stop the gateway.\n3. Clear the socket file.\n4. Start the gateway.' }

// Same text `expandKnowledgeBlock` writes (website/src/pages/chat/useKnowledgeFetch.ts).
const BLOCK = [
  '[KNOWLEDGE CONTEXT — injected by user from knowledge library]\n',
  `## ${ITEM.title} (${ITEM.source})\n${ITEM.content}\n`,
  '[END KNOWLEDGE CONTEXT]\n',
].join('\n')
const WIRE = (BLOCK + '\n' + TYPED).trimEnd()

// Pinned contentHash(BLOCK.trimEnd()), the fingerprint a real send records.
const head = BLOCK.trimEnd()
const KNOWLEDGE = { items: 1, tokens: 42, titles: [ITEM.title], content: [{ title: ITEM.title, text: ITEM.content }] }
const SCENES = [
  ['before', KNOWLEDGE],
  ['after', { ...KNOWLEDGE, envelope: { len: head.length, hash: '1gcuwkd' } }],
]

const now = Math.floor(Date.now() / 1000)
const SLOTS = [{
  key: ACTIVE, title: 'Gateway restart question', running: false, messages: 2, agent: 'kirocrew',
  memory_mode: 'persistent', modified: now, last_ts: '2026-10-24T04:00:00Z', folder_id: '',
  last_message: 'Skip step 3.', source_links: [], source_links_total: 0,
}]
const detailFor = knowledge => ({
  running: false, has_more: false, total: 2, queue: [],
  messages: [
    { role: 'user', ts: now - 40, content: WIRE, meta: { knowledge, sendId: 's-1' } },
    { role: 'assistant', ts: now - 30, content: 'Skip step 3: a fresh host has no socket file to clear.' },
  ],
})

let failures = 0
const check = (label, ok) => { console.log(`${ok ? 'PASS' : 'FAIL'} ${label}`); if (!ok) failures++ }

const { srv, base } = await serveDist()
const browser = await chromium.launch()
try {
  for (const theme of ['dark', 'light']) {
    for (const [name, knowledge] of SCENES) {
      const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 2 })
      const page = await context.newPage()
      const errors = []
      page.on('pageerror', e => errors.push(String(e)))
      logPageProblems(page)
      const detail = detailFor(knowledge)
      await stubDashboardApi(page, {
        theme,
        slots: SLOTS,
        extra: async (path, route) => {
          if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
          return false
        },
        localStorageEntries: { 'mc-lang': 'en', 'mc-active-slot': ACTIVE, 'mc-privacy-notice-v1': '1' },
      })
      await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
      await page.getByText('a fresh host has no socket file', { exact: false }).last().waitFor({ timeout: 30000 })
      await page.waitForTimeout(400)
      const blockShown = await page.getByText('KNOWLEDGE CONTEXT', { exact: false }).count()
      const typedShown = await page.getByText(TYPED, { exact: false }).count()
      check(`[${theme}/${name}] block drawn in the bubble: ${blockShown > 0}`, name === 'before' ? blockShown > 0 : blockShown === 0)
      check(`[${theme}/${name}] typed text drawn`, typedShown > 0)
      check(`[${theme}/${name}] no page errors`, errors.length === 0)
      await page.screenshot({ path: resolve(OUT, `${name}-${theme}.png`) })
      await context.close()
    }
  }
} finally {
  await browser.close()
  srv.close()
}
if (failures) { console.error(`${failures} check(s) failed`); process.exit(1) }
console.log(`wrote ${OUT}`)

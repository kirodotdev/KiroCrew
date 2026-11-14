/**
 * Screenshot harness for the memory provider picker rows and the move sheet.
 *
 * Runs the REAL built SPA (website/dist) behind `serveDist` and answers every
 * /api/** call through `stubDashboardApi`, plus the provider overview and the
 * move job routes. Shots, light and dark:
 *   01  Settings > Memory > Memory provider: a provider that can replace Crew
 *       memory and one that cannot (its gaps line), each with "Move Crew memory here";
 *   02  the move sheet's plan (counts, samples per kind);
 *   03  the move sheet after the run, with "Undo this move".
 *
 * Usage: node scripts/capture-memory-move-sheet.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/37204-memory-move'
mkdirSync(OUT, { recursive: true })

const caps = over => ({
  api_version: 1,
  provider: { name: 'Example', version: '1.0.0' },
  egress: 'local',
  read_only: false,
  ops: ['context', 'search', 'get', 'list', 'put', 'delete', 'put_batch', 'changes'],
  kinds: ['preference', 'fact', 'episode', 'summary', 'rule', 'finding', 'document'],
  search: { lexical: true, semantic: true, filters: ['kind'] },
  writes: { verbatim: true, preserves_timestamps: true, keeps_origin: true, idempotent: true, dry_run_put: false, max_batch: 50, max_text: 64000 },
  history: { revisions: false, retired: true, changes: true },
  ingest: 'raw',
  isolation: { multi_store: true, principal_auth: false },
  documents: 'readwrite',
  steering: 'none',
  max_page: 100,
  ...over,
})

const OVERVIEW = {
  mode: 'builtin',
  id: '',
  timeout_secs: 3,
  switched_at: 0,
  stop_learning_ack: false,
  store: 'default',
  builtin: { id: 'builtin', display_name: 'Crew memory' },
  providers: [
    {
      id: 'app:cloud-memory/cloud', app: 'cloud-memory', app_version: '1.0.0',
      display_name: 'Cloud memory', description: 'Keeps memory in a cloud memory service.',
      settings_page: '', enabled: true, capabilities: caps({}), replace_gaps: [],
      mode_gaps: { augment: [], replace: [] }, egress_consented: true,
      latency: { calls: 12, ops: { context: { p50_ms: 80, p95_ms: 140, count: 12 } } },
    },
    {
      id: 'app:graph-memory/graph', app: 'graph-memory', app_version: '0.4.0',
      display_name: 'Graph memory', description: 'An MCP memory server, through its bridge.',
      settings_page: '', enabled: true,
      capabilities: caps({ ops: ['context', 'search', 'list', 'put'], writes: { ...caps({}).writes, verbatim: false, idempotent: false }, documents: 'none', history: { revisions: false, retired: false, changes: false } }),
      replace_gaps: ['op:get', 'op:delete', 'writes.verbatim', 'writes.idempotent'],
      mode_gaps: { augment: [], replace: ['op:get', 'op:delete', 'writes.verbatim', 'writes.idempotent'] },
      egress_consented: true,
    },
  ],
  shared_crew: false,
  policy_allows: true,
  active: { mode: 'builtin', id: 'builtin', unavailable: '' },
}

const sample = (id, kind, text) => ({ id, kind, text, planned: 'would_write' })
const PLAN = {
  id: 'a1b2c3d4e5f60718', from: 'builtin', to: 'app:cloud-memory/cloud', store: 'default', dry_run: true,
  state: 'planned', checkpoint: { cursor: null, listed: 1214 },
  by_kind: { rule: 61, finding: 25, preference: 40, fact: 1078, document: 2, summary: 8 },
  outcomes: { would_write: 1206, 'refused:content_screen': 3, 'skipped:retired': 5 },
  skipped_kinds: {}, notes: [], verify: {}, rolled_back: 0, error: '',
  samples: {
    rule: [sample('lesson:1', 'rule', 'Keep commit subjects under 72 characters.'), sample('lesson:2', 'rule', 'Run the formatter only on the files you changed.')],
    preference: [sample('fact:pref.reply', 'preference', 'Prefers short replies with the conclusion first.')],
    document: [sample('doc:preferences', 'document', '# Preferences - Tabs over spaces - Short replies')],
  },
}
const DONE = {
  ...PLAN, id: 'b1b2c3d4e5f60718', dry_run: false, state: 'done',
  outcomes: { inserted: 1180, deduped: 26, 'refused:content_screen': 3, 'skipped:retired': 5 },
  verify: { checked: 24, matched: 24, mismatched: 0 }, samples: undefined,
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()

for (const theme of ['light', 'dark']) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  let ran = false
  await stubDashboardApi(page, {
    theme,
    localStorageEntries: { 'mc-lang': 'en' },
    extra: async (path, route) => {
      const method = route.request().method()
      const json = body => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
      if (path === '/api/memory/stores') return json({ stores: [], active: 'default' }), true
      if (path === '/api/memory/retired') return json({ retired: [] }), true
      if (path === '/api/memory/backups') return json({ backups: [] }), true
      if (path === '/api/memory/stats' || path === '/api/memory/settings') return json({}), true
      if (path === '/api/memory/semantic') return json({ items: [], total: 0 }), true
      if (['/api/memory/preferences', '/api/memory/projects', '/api/memory/history'].includes(path))
        return json({ content: '' }), true
      if (path === '/api/memory/provider') return json(OVERVIEW), true
      if (path === '/api/memory/provider/migrations' && method === 'POST') {
        const body = route.request().postDataJSON()
        if (!body.dry_run) ran = true
        return json(body.dry_run ? PLAN : DONE), true
      }
      if (path.startsWith('/api/memory/provider/migrations/')) return json(path.includes(DONE.id) ? DONE : PLAN), true
      return false
    },
  })
  await page.goto(base + '/settings/overview?view=memory', { waitUntil: 'domcontentloaded' })
  const picker = page.getByTestId('memory-provider-picker')
  await picker.waitFor({ state: 'visible', timeout: 20000 })
  await page.getByText('Graph memory').waitFor({ state: 'visible', timeout: 10000 })
  await picker.screenshot({ path: join(OUT, `${theme}-01-picker.png`) })

  await picker.getByRole('button', { name: 'Move Crew memory here' }).first().click()
  const sheet = page.getByTestId('memory-move-sheet')
  await sheet.waitFor({ state: 'visible', timeout: 10000 })
  await page.getByRole('button', { name: 'Move', exact: true }).waitFor({ state: 'visible' })
  await page.waitForTimeout(600)
  await sheet.locator('details').first().evaluate(el => { el.open = true })
  await page.waitForTimeout(200)
  await page.getByRole('dialog').screenshot({ path: join(OUT, `${theme}-02-plan.png`) })

  await page.getByRole('button', { name: 'Move', exact: true }).click()
  await page.getByRole('button', { name: 'Undo this move' }).waitFor({ state: 'visible', timeout: 10000 })
  await page.waitForTimeout(600)
  await page.getByRole('dialog').screenshot({ path: join(OUT, `${theme}-03-done.png`) })
  if (!ran) throw new Error('the run was never started')
  await context.close()
}

await browser.close()
srv.close()
console.log('wrote', OUT)

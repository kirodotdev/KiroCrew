/**
 * Screenshot + overflow harness for the Settings > Memory page tabs (Recovery,
 * Failures, Audit).
 *
 * Serves a built SPA and answers /api/** from the shared stub map plus synthetic
 * memory fixtures shaped like the owner's report: a long `pref.*` key and long
 * audit details, replaced experiences with no `superseded_by`, and recurring
 * failures that mix short names, sub-labelled names and the `unknown` tool kind.
 *
 * For each width x theme x tab it writes a PNG and prints whether anything in the
 * tab panel overflows sideways: a scroller whose scrollWidth exceeds its
 * clientWidth, or a table/button whose right edge passes its box.
 *
 * Usage: node scripts/capture-memory-page-layout.mjs <distDir> <outDir> <prefix>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const DIST = process.argv[2]
const OUT = process.argv[3] || '../temp-screenshots/memory-page-layout'
const PREFIX = process.argv[4] || 'after'
const WIDTHS = (process.env.WIDTHS || '1280,1600,900').split(',').map(Number)
const THEMES = (process.env.THEMES || 'dark,light').split(',')
mkdirSync(OUT, { recursive: true })

const iso = minutesAgo => new Date(Date.UTC(2026, 10, 26, 20, 5, 40) - minutesAgo * 60000).toISOString()
const LONG = 'Internal package sync lacks PR #43132 (installer fix); a second worker backports the branch per internal rules, adopting the managed environment pin and the release candidate rc.5 that contains items A-F.'
const EVENTS = [
  { event_type: 'create', memory_type: 'episodic', memory_key: 'e1', new_value: LONG, created_at: iso(1) },
  { event_type: 'create', memory_type: 'episodic', memory_key: 'e2', new_value: 'User clarified the client name in Settings on the current build. User wants: (1) hidden in public builds, (2) visible in internal ones.', created_at: iso(2) },
  { event_type: 'create', memory_type: 'semantic', memory_key: 'pref.managed_env_internal_package_sync_dispatched_v2', new_value: '"2026-11-27 06:19 UTC sync managed environment internal package. Target rc.5 (contains A-F + #43132 installer fix) dispatched to backport worker under internal rules. PR requires rules review."', created_at: iso(3) },
  ...Array.from({ length: 9 }, (_, i) => ({ event_type: i % 3 ? 'update' : 'create', memory_type: 'semantic', memory_key: `project.example.item_${i}`, new_value: `"Value number ${i}: ${LONG.slice(0, 80 + i * 10)}"`, created_at: iso(4 + i) })),
]
const RETIRED = [
  { id: 'r1', text: 'Audit methodology: static code review only, no live service probing. Reports cover code paths, evidence, reproduction reasoning, impact, and suggested fixes. Findings on the public main branch; no PR or issue opened publicly until the fix lands and the advisory is drafted.', retired_times: 1, ts: iso(0) },
  { id: 'r2', text: 'Code review report shows 11 new problems found (preview scope, engine persistence, engine version, login routing, shared model selection).', retired_times: 1, ts: iso(0) },
  { id: 'r3', text: 'Review job: check claims in two notes against a pinned commit. Priority: all category-1 and high-impact items; sample 10+ category-2 from each. Output: verdict table, context and the commit read.', retired_times: 3, ts: iso(1) },
]
const FAILURES = [
  { signature: 'unknown|||', tool_kind: 'unknown', tool: '', rule_id: '', error: '', source: 'dashboard', hits: 68, sessions: 3, days: 1, previous_hits: 0, trend: 'new' },
  { signature: 'mcp|work_brief||execution_failed', tool_kind: 'mcp', tool: 'work_brief', rule_id: '', error: 'execution_failed', source: 'agent', hits: 20, sessions: 20, days: 1, previous_hits: 0, trend: 'new' },
  { signature: 'mcp|cron_list||', tool_kind: 'mcp', tool: 'cron_list', rule_id: '', error: '', source: 'agent', hits: 2, sessions: 2, days: 1, previous_hits: 0, trend: 'new' },
  { signature: 'execute_bash||exfil|hook_deny', tool_kind: 'execute_bash', tool: '', rule_id: 'exfil', error: 'hook_deny', source: 'agent', hits: 2, sessions: 2, days: 1, previous_hits: 0, trend: 'new' },
]
const STORE = { name: 'default', is_default: true, lineage: 'v1', exists: true, memory_version: 1, semantic_count: 120, episodic_count: 400, lessons_count: 5, facets_supported: false, backup_count: 0, newest_backup: null }

/** Everything in the visible tab panel that runs past its own box. */
function measure() {
  const panel = [...document.querySelectorAll('[role="tabpanel"]')].find(p => !p.hidden)
  if (!panel) return { error: 'no visible tabpanel' }
  const problems = []
  for (const el of panel.querySelectorAll('*')) {
    const style = getComputedStyle(el)
    if (/(auto|scroll|hidden)/.test(style.overflowX) && el.scrollWidth > el.clientWidth + 1) {
      problems.push(`scroller ${el.tagName.toLowerCase()}.${String(el.className).slice(0, 40)} scrollW=${el.scrollWidth} clientW=${el.clientWidth}`)
    }
  }
  for (const box of panel.querySelectorAll('[data-settings-box]')) {
    const right = box.getBoundingClientRect().right
    for (const el of box.querySelectorAll('table, button')) {
      const r = el.getBoundingClientRect()
      if (r.width && r.right > right + 1) problems.push(`${el.tagName.toLowerCase()} "${(el.textContent || '').trim().slice(0, 30)}" right=${Math.round(r.right)} > box ${Math.round(right)}`)
    }
  }
  const tabs = document.querySelector('[role="tablist"]')?.getBoundingClientRect()
  const boxes = [...panel.querySelectorAll('[data-settings-box]')].map(b => Math.round(b.getBoundingClientRect().width))
  const page = document.documentElement.scrollWidth > window.innerWidth
  return { problems, tabsWidth: tabs ? Math.round(tabs.width) : null, boxes, pageOverflow: page }
}

async function main() {
  const { srv, base } = await serveDist(DIST)
  const browser = await chromium.launch({ executablePath: chromiumExecutable() })
  let failures = 0
  for (const theme of THEMES) for (const width of WIDTHS) {
    const context = await browser.newContext({ viewport: { width, height: 1100 }, deviceScaleFactor: 1, reducedMotion: 'reduce' })
    const page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, {
      theme,
      extra: async (path, route) => {
        if (path === '/api/theme/boot') return json(route, { mode: theme, theme: '' }), true
        if (path === '/api/acp-backends') return json(route, { backends: [] }), true
        if (path === '/api/memory/stores') return json(route, { stores: [STORE], active: 'default' }), true
        if (path === '/api/memory/stats') return json(route, { migrated: true, semantic_active: 120, episodic_active: 400, embedded_count: 520, faiss_index_size: 520 }), true
        if (path === '/api/memory/embedding-status') return json(route, { provider: 'local', model: 'all-MiniLM-L6-v2', embedded: 520, total: 520 }), true
        if (path === '/api/memory/events') return json(route, { events: EVENTS }), true
        if (path === '/api/memory/retired') return json(route, { retired: RETIRED }), true
        if (path === '/api/sel/recurring-failures') return json(route, { failures: FAILURES, window_days: 7 }), true
        if (path === '/api/memory/records') return json(route, { entries: [], total: 0, has_more: false }), true
        if (path === '/api/memory/backups') return json(route, { backups: [] }), true
        if (path === '/api/memory/settings') return json(route, { idle_hours: 2, max_days: 30 }), true
        if (path.startsWith('/api/chat/slots/')) return json(route, { running: false, has_more: false, total: 0, queue: [], messages: [] }), true
        return false
      },
      localStorageEntries: { 'mc-theme-mode': theme, 'mc-onboarded': '1', 'mc-lang': 'en' },
    })
    await page.goto(`${base}/settings/memory`, { waitUntil: 'domcontentloaded' })
    await page.getByRole('tab', { name: 'Recovery' }).waitFor({ state: 'visible', timeout: 30000 })
    for (const [tab, ready] of [['Recovery', 'Replaced experiences'], ['Failures', 'Repeated failures'], ['Audit', null]]) {
      await page.getByRole('tab', { name: tab, exact: true }).click()
      if (ready) await page.getByText(ready, { exact: true }).first().waitFor({ state: 'visible', timeout: 20000 })
      await page.waitForTimeout(ready ? 400 : 1500)
      const m = await page.evaluate(measure)
      const bad = m.problems?.length || m.pageOverflow
      console.log(`${PREFIX} ${theme} w=${width} ${tab}: tabs=${m.tabsWidth} boxes=${JSON.stringify(m.boxes)} pageOverflow=${m.pageOverflow} ${bad ? 'OVERFLOW ' + JSON.stringify(m.problems) : 'fits'}`)
      if (bad) failures++
      await page.screenshot({ path: `${OUT}/${PREFIX}-${tab.toLowerCase()}-${width}-${theme}.png`, fullPage: true })
    }
    await context.close()
  }
  await browser.close()
  srv.close()
  console.log(failures ? `${failures} tab(s) overflow` : 'no overflow')
}

main().catch(err => { console.error(err); process.exit(1) })

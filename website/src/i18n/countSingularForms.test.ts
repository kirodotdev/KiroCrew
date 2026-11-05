import fs from 'node:fs'
import path from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'

import { i18next } from './index'
import './all'
import { fmtNumber } from './format'
import { i18nT } from './t'

const SRC = path.resolve(__dirname, '..')
const SESSIONS = 'pages.performanceTab.session_storage_detail'
const TASKS = 'apps.specBuilder.components.taskList.done_of_total'
const GRAPH = 'apps.issueRadar.views.graphView'
const AWS = 'apps.awsControl.page'

const sessions = (count: number) => i18nT(SESSIONS, { count, sessions: fmtNumber(count), reclaimable: '2 MB' })
const tasks = (done: number, total: number) => i18nT(TASKS, { count: total, done: fmtNumber(done), total: fmtNumber(total) })
const tree = (key: string, nodes: number, edges: number) => i18nT(`${GRAPH}.${key}`, {
  kind: 'IS', number: 7, count: nodes, nodes: fmtNumber(nodes),
  edges: i18nT(`${GRAPH}.live_edges`, { count: edges, n: fmtNumber(edges) }),
})
const totals = (accounts: number, keys: number, healthy: number) => i18nT(`${AWS}.totals_summary`, {
  count: accounts, accounts: fmtNumber(accounts),
  keys: i18nT(`${AWS}.keys_healthy`, { count: keys, n: fmtNumber(keys), healthy: fmtNumber(healthy) }),
})

afterEach(async () => {
  if (i18next.language !== 'en') await i18next.changeLanguage('en')
})

describe('singular forms on session storage, spec tasks, the dependency graph and AWS totals', () => {
  it('says one, not ones, in English', () => {
    expect(sessions(1)).toBe('1 session · 2 MB reclaimable')
    expect(sessions(4)).toBe('4 sessions · 2 MB reclaimable')
    expect(tasks(1, 1)).toBe('1 of 1 task done')
    expect(tasks(1, 3)).toBe('1 of 3 tasks done')
    expect(tree('tree_stat', 1, 1)).toBe('tree of IS-7: 1 node · 1 live edge')
    expect(tree('aria_tree', 3, 2)).toBe('Dependency tree of IS-7: 3 nodes, 2 live edges')
    expect(totals(1, 1, 1)).toBe('1 account · 1 of 1 key healthy')
    expect(totals(2, 5, 4)).toBe('2 accounts · 4 of 5 keys healthy')
  })

  it('uses every Russian plural form', async () => {
    await i18next.changeLanguage('ru')
    expect(sessions(1)).toBe('1 сессия · 2 MB можно освободить')
    expect(sessions(3)).toBe('3 сессии · 2 MB можно освободить')
    expect(sessions(5)).toBe('5 сессий · 2 MB можно освободить')
    expect(tasks(1, 21)).toBe('Готово 1 из 21 задачи')
    expect(tree('tree_stat', 2, 5)).toBe('дерево IS-7: 2 узла · 5 активных рёбер')
  })

  it('keeps French agreement right when the done count differs from the total', async () => {
    await i18next.changeLanguage('fr')
    expect(tasks(0, 1)).toBe('Tâches terminées\u202f: 0/1')
    expect(totals(1, 2, 1)).toBe('1 compte · clés en bon état\u202f: 1/2')
  })

  it('writes Bengali digits on the graph', async () => {
    await i18next.changeLanguage('bn')
    expect(tree('tree_stat', 12, 3)).toBe('IS-7 এর ট্রি: ১২টি নোড · ৩টি সক্রিয় প্রান্ত')
  })

  it.each([
    ['apps/spec-builder/components/TaskList.tsx', "done_of_total', { done, total })"],
    ['apps/issue-radar/views/GraphView.tsx', 'nodes: model.ids.size, edges: model.edges.length'],
    ['apps/aws-control/AwsControlPage.tsx', 'keys: fmtNumber(data.totals.profiles)'],
  ])('%s passes the count into the catalog (%s)', (file, glued) => {
    expect(fs.readFileSync(path.join(SRC, file), 'utf8')).not.toContain(glued)
  })

  it('passes the session count into the storage line', () => {
    const src = fs.readFileSync(path.join(SRC, 'pages/system/PerformanceTab.tsx'), 'utf8')
    expect(src).toMatch(/session_storage_detail', \{\s*count: data\.total_sessions,/)
  })
})

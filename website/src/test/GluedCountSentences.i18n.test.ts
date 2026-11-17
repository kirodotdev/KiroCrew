/**
 * Count and label sentences worded as one message (#34312).
 *
 * Eleven lines joined two or more separately translated catalog pieces in code
 * with an English space: a size label, value and unit; a sub-agent result count
 * and "ready"; the deploy total and the pending-site line; the chart hint and
 * note; "blocked, waiting on" and a task count; "context from", a turn count and
 * "ago"; "summarized" and a session count; "loaded by" and an agent count; the
 * token chart's in and out labels; and the deny list's category and rule counts.
 * No language could word those sentences its own way, and Japanese and Chinese
 * read ASCII spaces between full-width text. Each is now one catalog message.
 */
import fs from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'

// `/all` registers every authored catalog; `../i18n` registers English only.
import { i18next } from '../i18n/all'

const SRC = path.resolve(__dirname, '..')
const LANGS = ['de', 'es', 'fr', 'it', 'pt', 'ru', 'ja', 'ko', 'zh-CN', 'hi', 'bn']
const t = (key: string, opts?: Record<string, unknown>) => i18next.t(key, { lng: 'en', ...opts }) as string

// key -> [English options, English text, placeholders every language must carry once]
const SENTENCES: Record<string, [Record<string, unknown>, string, string[]]> = {
  'components.publishHub.size_kb': [{ size: '1.5' }, 'Size: 1.5 KB', ['{{size}}']],
  'components.queueStack.sub_agent_results_ready': [{ count: 2 }, '2 sub-agent results ready — processing after the current turn', ['{{count}}']],
  'pages.artifactDeployPage.estimated_total_line': [{ sites: '1 static site', webapps: '2 webapps' }, 'Estimated total — 1 static site + 2 webapps: <cost/>', ['{{sites}}', '{{webapps}}', '<cost/>']],
  'pages.artifactDeployPage.pending_site_line': [
    { source: 'kanban', profile: 'ship-prod', ttl: 24, scan: 'clean', age: 10 },
    'Source: kanban · Profile: ship-prod · TTL: 24h · Scan: clean · 10m ago',
    ['{{source}}', '{{profile}}', '{{ttl}}', '{{scan}}', '{{age}}'],
  ],
  'pages.contextBreakdown.pick_hint_chart_note': [{}, 'Click or use the arrow keys to pick a turn. Grey is the rest of the context, on turns where it was measured.', []],
  'pages.aidlc.taskDetailPanel.blocked_waiting_on_tasks': [{ count: 1 }, 'Blocked — waiting on 1 task', ['{{count}}']],
  'pages.chat.sideChat.context_from_turns_ago': [{ count: 3 }, 'Context from 3 turns ago', ['{{count}}']],
  'pages.overview.memoryTab.consolidated_count': [{ count: 1 }, 'Summarized 1 session', ['{{count}}']],
  'pages.overview.skillsTab.loaded_by_agents': [{ count: 2 }, 'Loaded by 2 agents', ['{{count}}']],
  'pages.overview.tokenDailyChart.in_out': [{ input: '1.2K', output: '300' }, 'In: 1.2K · Out: 300', ['{{input}}', '{{output}}']],
  'pages.settings.securityPanel.categories_rules_summary': [{ categories: '1 category', rules: '30 rules' }, '1 category · 30 rules', ['{{categories}}', '{{rules}}']],
}

// Every value the key resolves to in one language: the plain key, or each plural form.
const values = (lng: string, key: string): string[] => {
  const leaf = key.slice(key.lastIndexOf('.') + 1)
  const parent = i18next.getResource(lng, 'translation', key.slice(0, key.lastIndexOf('.'))) as Record<string, unknown> | undefined
  if (!parent) return []
  if (typeof parent[leaf] === 'string') return [parent[leaf] as string]
  return Object.entries(parent)
    .filter(([k, v]) => k.startsWith(`${leaf}_`) && ['zero', 'one', 'two', 'few', 'many', 'other'].includes(k.slice(leaf.length + 1)) && typeof v === 'string')
    .map(([, v]) => v as string)
}

describe('count and label sentences as one message', () => {
  it('keeps the English text the same', () => {
    for (const [key, [opts, en]] of Object.entries(SENTENCES)) expect(t(key, opts), key).toBe(en)
    expect(t('components.queueStack.sub_agent_results_ready', { count: 1 })).toBe('1 sub-agent result ready — processing after the current turn')
    expect(t('pages.aidlc.taskDetailPanel.blocked_waiting_on_tasks', { count: 2 })).toBe('Blocked — waiting on 2 tasks')
    expect(t('pages.chat.sideChat.context_from_turns_ago', { count: 1 })).toBe('Context from 1 turn ago')
    expect(t('pages.overview.memoryTab.consolidated_count', { count: 4 })).toBe('Summarized 4 sessions')
    expect(t('pages.overview.skillsTab.loaded_by_agents', { count: 1 })).toBe('Loaded by 1 agent')
  })

  it('carries every placeholder exactly once in every shipped language', () => {
    for (const lng of LANGS) {
      for (const [key, [, , parts]] of Object.entries(SENTENCES)) {
        const forms = values(lng, key)
        expect(forms.length, `${lng} ${key}`).toBeGreaterThan(0)
        for (const value of forms) for (const part of parts) expect(value.split(part), `${lng} ${key} ${part}`).toHaveLength(2)
      }
    }
  })

  it('no longer glues two catalog pieces with a space in code', () => {
    const files = [
      'components/PublishHub.tsx',
      'components/QueueStack.tsx',
      'pages/ArtifactDeployPage.tsx',
      'pages/ContextBreakdownPanel.tsx',
      'pages/aidlc/TaskDetailPanel.tsx',
      'pages/chat/SideChat.tsx',
      'pages/overview/MemoryTab.tsx',
      'pages/overview/SkillsTab.tsx',
      'pages/overview/TokenDailyChart.tsx',
      'pages/settings/SecurityPanelDeniedCommands.tsx',
    ]
    // The shapes the issue lists: two translate calls side by side with a space
    // between them, and a translated label followed by a bare value.
    const glued = [")} {i18nT('", "{i18nT('components.publishHub.size')}", "{i18nT('pages.overview.tokenDailyChart.in')}"]
    for (const file of files) {
      const lines = fs.readFileSync(path.join(SRC, file), 'utf8').split('\n')
      const left = lines.filter((line) => glued.some((shape) => line.includes(shape))).map((line) => line.trim())
      expect(left, file).toEqual([])
    }
  })
})

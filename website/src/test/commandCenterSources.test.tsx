// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, waitFor } from '@testing-library/react'
import { focusManager, useQueryClient } from '@tanstack/react-query'
import { createTestStore, renderHookWithProviders, renderWithProviders } from './helpers'
import { api } from '../api/client'
import { missingSourcesNotice, useCommandCenter } from '../pages/chat/command-center/useCommandCenter'
import { fmtList } from '../i18n/format'
import { i18nT } from '../i18n/t'
import { teamRoots } from '../pages/chat/command-center/model'
import TaskDashboardFrame, { TASK_DASHBOARD_SANDBOX } from '../pages/chat/command-center/TaskDashboardFrame'
import type { Artifact } from '../types'

const artifact = (slug: string, session: string, content = '<h1>Task-specific map</h1>'): Artifact => ({
  slug, session_key: session, name: slug, kind: 'html', source: 'chat', description: '', tags: ['task-dashboard'],
  version: 1, created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z', content,
})
function store() {
  const initial = createTestStore().getState()
  return createTestStore({ ...initial, dashboard: { ...initial.dashboard, connected: true, slots: [
    { key: 'root', title: 'Conductor', messages: 0, running: true },
    { key: 'builder', created_by: 'root', messages: 0, running: false },
    { key: 'unrelated', messages: 0, running: true },
  ] } })
}

describe('task dashboard sources and containment', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    vi.spyOn(api, 'pendingQuestions').mockResolvedValue([])
    vi.spyOn(api, 'approvals').mockResolvedValue([])
    vi.spyOn(api, 'workflowRuns').mockResolvedValue({ runs: [] })
    vi.spyOn(api, 'sessionWorkProjection').mockResolvedValue({ value: { items: [] } })
    vi.spyOn(api, 'artifacts').mockResolvedValue({ artifacts: [artifact('own', 'dashboard:root'), artifact('child', 'builder'), artifact('foreign', 'unrelated'), artifact('unbound', '')] })
  })

  it('admits arbitrary authored layouts from the owning team, not similarly tagged unrelated sessions', async () => {
    const { result } = renderHookWithProviders(() => useCommandCenter('root'), { store: store() })
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.dashboards.map(a => a.slug)).toEqual(['own', 'child'])
    expect(result.current.nodes.map(n => n.slot)).toEqual(['root', 'builder'])
    expect(result.current.relevant).toBe(true)
    expect(result.current.stale).toBe(false)
  })

  it('admits channel-qualified artifacts only for the owning slot', async () => {
    const initial = store().getState()
    const channelStore = createTestStore({ ...initial, dashboard: { ...initial.dashboard, slots: [{ key: 'slack_1785.12', messages: 0, running: false }] } })
    vi.mocked(api.artifacts).mockResolvedValue({ artifacts: [artifact('own', 'slack:1785.12'), artifact('canonical', 'slack_1785.12'), artifact('foreign', 'slack:999'), artifact('unknown', 'other:1785.12')] })
    const { result } = renderHookWithProviders(() => useCommandCenter('slack_1785.12'), { store: channelStore })
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.dashboards.map(a => a.slug)).toEqual(['own', 'canonical'])
  })

  it('never falls back to the whole fleet while the owning slot is unresolved', () => {
    const { result } = renderHookWithProviders(() => useCommandCenter(null), { store: store() })
    expect(result.current.nodes).toEqual([])
    expect(result.current.attention).toEqual([])
    expect(result.current.dashboards).toEqual([])
    expect(api.pendingQuestions).not.toHaveBeenCalled()
  })

  it('reads the fleet only when explicitly requested, without per-session work queries', async () => {
    const { result } = renderHookWithProviders(() => useCommandCenter(null, true, 'fleet'), { store: store() })
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.nodes.map(n => n.slot)).toEqual(['root', 'builder', 'unrelated'])
    expect(result.current.dashboards.map(a => a.slug)).toEqual(['own', 'child', 'foreign'])
    expect(api.sessionWorkProjection).not.toHaveBeenCalled()
    expect(result.current.stale).toBe(false)
    expect(result.current.updatedAt).toBeGreaterThan(0)
  })

  it('reports unavailable sources instead of claiming no questions are pending', async () => {
    vi.mocked(api.pendingQuestions).mockRejectedValue(new Error('offline'))
    const { result } = renderHookWithProviders(() => useCommandCenter('root'), { store: store() })
    await waitFor(() => expect(result.current.stale).toBe(true))
    expect(result.current.updatedAt).toBe(0)
  })

  it('discovers a later publication and question for an isolated idle slot without a store update', async () => {
    const initial = store().getState()
    const idleStore = createTestStore({ ...initial, dashboard: { ...initial.dashboard, slots: [{ key: 'root', messages: 0, running: false }] } })
    vi.mocked(api.artifacts).mockResolvedValue({ artifacts: [] })
    const { result } = renderHookWithProviders(() => ({ ...useCommandCenter('root'), queryClient: useQueryClient() }), { store: idleStore })
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.relevant).toBe(false)
    const unchangedState = idleStore.getState()
    vi.mocked(api.artifacts).mockResolvedValue({ artifacts: [artifact('later', 'root')] })
    vi.mocked(api.pendingQuestions).mockResolvedValue([{ slot: 'root', card_id: 'later-question', questions: [{ question: 'Which scope?', options: [{ label: 'Stable' }] }] }])
    await act(async () => { await result.current.queryClient.refetchQueries({ queryKey: ['command-center'] }) })
    await waitFor(() => expect(result.current.relevant).toBe(true))
    await waitFor(() => expect(result.current.attention.map(a => a.id)).toEqual(['question:root:later-question']))
    expect(idleStore.getState()).toBe(unchangedState)
    expect(result.current.dashboards.map(a => a.slug)).toEqual(['later'])
  })

  it('polls no source in any scope, leaving refresh to the frames that announce changes', async () => {
    const intervals = (client: ReturnType<typeof useQueryClient>) => client.getQueryCache().findAll({ queryKey: ['command-center'] })
      .flatMap(q => q.observers.map(o => o.options.refetchInterval))
    for (const [root, scope] of [['root', 'task'], [null, 'fleet']] as const) {
      const view = renderHookWithProviders(() => ({ ...useCommandCenter(root, true, scope), queryClient: useQueryClient() }), { store: store() })
      await waitFor(() => expect(view.result.current.loading).toBe(false))
      const seen = intervals(view.result.current.queryClient)
      expect(seen.length).toBeGreaterThanOrEqual(4)
      expect(seen.every(i => !i)).toBe(true)
      view.unmount()
    }
    expect(api.pendingQuestions).toHaveBeenCalledTimes(2)
  })

  it('finds a slot\'s team roots by walking its creators, stopping on a cycle', () => {
    const slots = [
      { key: 'root', messages: 0, running: false }, { key: 'mid', messages: 0, running: false, created_by: 'dashboard:root' },
      { key: 'leaf', messages: 0, running: false, created_by: 'mid' },
      { key: 'a', messages: 0, running: false, created_by: 'b' }, { key: 'b', messages: 0, running: false, created_by: 'a' },
    ]
    expect(teamRoots(slots, 'dashboard:leaf')).toEqual(['leaf', 'mid', 'root'])
    expect(teamRoots(slots, 'root')).toEqual(['root'])
    expect(teamRoots(slots, 'unknown')).toEqual(['unknown'])
    expect(teamRoots(slots, 'a')).toEqual(['a', 'b'])
  })

  it('reads the work board from the dock only for a team or a slot with a published view, and never refetches on focus', async () => {
    const initial = store().getState()
    const solo = createTestStore({ ...initial, dashboard: { ...initial.dashboard, slots: [{ key: 'root', messages: 0, running: true }] } })
    // A lone slot with no published view: nothing keeps the dock on screen, so
    // the whole-log fold stays unread.
    vi.mocked(api.artifacts).mockResolvedValue({ artifacts: [] })
    const dock = renderHookWithProviders(() => ({ ...useCommandCenter('root', true, 'task', { dock: true }), queryClient: useQueryClient() }), { store: solo })
    await waitFor(() => expect(dock.result.current.loading).toBe(false))
    expect(api.sessionWorkProjection).not.toHaveBeenCalled()
    const own = (key: readonly unknown[]) => key[0] === 'command-center' || key[0] === 'global-approvals'
    const observers = dock.result.current.queryClient.getQueryCache().getAll().filter(q => own(q.queryKey)).flatMap(q => q.observers)
    expect(observers.length).toBeGreaterThan(0)
    // A healthy source is left to its frames; only a failed one re-reads on focus.
    const onFocus = (o: (typeof observers)[number]) => {
      const option = o.options.refetchOnWindowFocus
      return typeof option === 'function' ? option(o.getCurrentQuery()) : option
    }
    expect(observers.every(o => onFocus(o) === false)).toBe(true)
    dock.unmount()
    // The same lone slot with its own published view: the dock is on screen for
    // that view, so its verdict needs the board too.
    vi.mocked(api.artifacts).mockResolvedValue({ artifacts: [artifact('own', 'dashboard:root')] })
    const viewed = renderHookWithProviders(() => useCommandCenter('root', true, 'task', { dock: true }), { store: solo })
    await waitFor(() => expect(viewed.result.current.loading).toBe(false))
    expect(api.sessionWorkProjection).toHaveBeenCalledWith('root')
    viewed.unmount()
    vi.mocked(api.sessionWorkProjection).mockClear()
    const team = renderHookWithProviders(() => useCommandCenter('root', true, 'task', { dock: true }), { store: store() })
    await waitFor(() => expect(team.result.current.loading).toBe(false))
    expect(api.sessionWorkProjection).toHaveBeenCalledWith('root')
    team.unmount()
    vi.mocked(api.sessionWorkProjection).mockClear()
    const panel = renderHookWithProviders(() => useCommandCenter('root'), { store: solo })
    await waitFor(() => expect(panel.result.current.loading).toBe(false))
    expect(api.sessionWorkProjection).toHaveBeenCalledWith('root')
  })

  it('never shows a board the panel cached once the dock stops reading it', async () => {
    const initial = store().getState()
    const solo = createTestStore({ ...initial, dashboard: { ...initial.dashboard, slots: [{ key: 'root', messages: 0, running: true }] } })
    vi.mocked(api.artifacts).mockResolvedValue({ artifacts: [] })
    vi.mocked(api.sessionWorkProjection).mockResolvedValue({ value: { items: [{ item_id: 'one', title: 'Old item', state: 'accepted' }] } })
    const both = renderHookWithProviders(() => ({ panel: useCommandCenter('root'), dock: useCommandCenter('root', true, 'task', { dock: true }) }), { store: solo })
    await waitFor(() => expect(both.result.current.panel.workItems).toHaveLength(1))
    expect(both.result.current.dock.workItems).toEqual([])
  })

  it('keeps decisions fresh when an optional source fails, and shares the app approvals cache', async () => {
    vi.mocked(api.workflowRuns).mockRejectedValue(new Error('workflows not available'))
    const { result } = renderHookWithProviders(() => ({ ...useCommandCenter('root'), queryClient: useQueryClient() }), { store: store() })
    await waitFor(() => expect(result.current.loading).toBe(false))
    await waitFor(() => expect(result.current.queryClient.getQueryState(['command-center', 'workflows'])?.status).toBe('error'))
    expect(result.current.stale).toBe(false)
    // The missing source is still reported, not passed off as "no runs".
    expect(result.current.missing).toEqual(['runs'])
    expect(result.current.updatedAt).toBeGreaterThan(0)
    expect(result.current.queryClient.getQueryState(['global-approvals'])?.status).toBe('success')
    // No workflow frame may ever come, so the failed source re-reads on focus.
    vi.mocked(api.workflowRuns).mockResolvedValue({ runs: [] })
    vi.mocked(api.pendingQuestions).mockClear()
    act(() => { focusManager.setFocused(false); focusManager.setFocused(true) })
    await waitFor(() => expect(result.current.missing).toEqual([]))
    expect(api.pendingQuestions).not.toHaveBeenCalled()
    vi.mocked(api.pendingQuestions).mockRejectedValue(new Error('offline'))
    await act(async () => { await result.current.queryClient.refetchQueries({ queryKey: ['command-center', 'questions'] }) })
    await waitFor(() => expect(result.current.stale).toBe(true))
    expect(result.current.missing).toEqual([])
  })

  it('names no missing source until the decision reads have answered', async () => {
    vi.mocked(api.workflowRuns).mockRejectedValue(new Error('workflows not available'))
    vi.mocked(api.pendingQuestions).mockReturnValue(new Promise(() => {}))
    const { result } = renderHookWithProviders(() => ({ ...useCommandCenter('root'), queryClient: useQueryClient() }), { store: store() })
    await waitFor(() => expect(result.current.queryClient.getQueryState(['command-center', 'workflows'])?.status).toBe('error'))
    expect(result.current.missing).toEqual([])
  })

  it('names every failed optional source in one notice, saying the reassurance once', () => {
    expect(missingSourcesNotice([])).toBeNull()
    const both = missingSourcesNotice(['runs', 'views'])!
    expect(both).toBe(i18nT('commandCenter.partial_sources', { sources: fmtList([i18nT('commandCenter.source_runs'), i18nT('commandCenter.source_views')]) }))
    expect(both).toContain(i18nT('commandCenter.source_runs'))
    expect(both).toContain(i18nT('commandCenter.source_views'))
    expect(both).not.toContain(i18nT('commandCenter.source_work'))
  })

  it('retains only stateless drafts by exact normalized slot and card, clearing on scope changes', async () => {
    let root: string | null = 'root'
    let scope: 'task' | 'fleet' = 'task'
    const { result, rerender } = renderHookWithProviders(() => useCommandCenter(root, true, scope), { store: store() })
    await waitFor(() => expect(result.current.loading).toBe(false))
    const questions = [{ question: 'Which scope?', options: [{ label: 'Stable' }] }]
    const own = { slot: 'dashboard:root', card_id: 'same', questions }
    act(() => {
      result.current.onQuestionDraftChange(own, true)
      result.current.onQuestionDraftChange({ slot: 'builder', card_id: 'same', questions }, true)
      result.current.onQuestionDraftChange({ slot: 'root', card_id: 'other', questions }, true)
      result.current.onQuestionDraftChange({ slot: 'root', ask_id: 'blocked', card_id: 'blocked-card', questions }, true)
      result.current.onQuestionDraftChange({ slot: 'root', questions }, true)
    })
    expect(result.current.attention.map(a => a.id)).toEqual(['question:root:same', 'question:builder:same', 'question:root:other'])
    const departingCallback = result.current.onQuestionDraftChange
    act(() => result.current.onQuestionDraftChange({ ...own, slot: 'root' }, false))
    expect(result.current.attention.map(a => a.id)).toEqual(['question:builder:same', 'question:root:other'])
    root = 'unrelated'
    rerender()
    expect(result.current.attention).toEqual([])
    root = 'root'
    rerender()
    expect(result.current.attention).toEqual([])
    root = null
    scope = 'fleet'
    rerender()
    act(() => result.current.onQuestionDraftChange(own, true))
    act(() => departingCallback(own, false))
    expect(result.current.attention.map(a => a.id)).toEqual(['question:root:same'])
    scope = 'task'
    rerender()
    expect(result.current.attention).toEqual([])
  })

  it('renders model HTML through the sandbox document service without a privileged bridge', async () => {
    const modelHtml = '<article><h1>Dependency map</h1><script>window.taskSpecific=true</script></article>'
    vi.spyOn(api, 'artifact').mockResolvedValue(artifact('own', 'root', modelHtml))
    const mint = vi.spyOn(api, 'sandboxDocUrl').mockResolvedValue({ url: '/sandbox-doc/test/token' })
    const { container } = renderWithProviders(<TaskDashboardFrame artifact={artifact('own', 'root')} active />)
    await waitFor(() => expect(container.querySelector('iframe')).not.toBeNull())
    const frame = container.querySelector('iframe')!
    expect(frame.getAttribute('sandbox')).toBe(TASK_DASHBOARD_SANDBOX)
    expect(frame.getAttribute('sandbox')).toBe('')
    expect(frame.getAttribute('referrerpolicy')).toBe('no-referrer')
    expect(mint).toHaveBeenCalledWith(expect.stringContaining('Dependency map'))
    expect(mint.mock.calls[0][0]).toContain("connect-src 'none'")
    expect(mint.mock.calls[0][0]).toContain("script-src 'none'")
    expect(mint.mock.calls[0][0]).not.toContain('window.taskSpecific')
    expect(container.querySelector('script')).toBeNull()
  })
})

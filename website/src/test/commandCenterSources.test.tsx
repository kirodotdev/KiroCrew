// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, waitFor } from '@testing-library/react'
import { useQueryClient } from '@tanstack/react-query'
import { createTestStore, renderHookWithProviders, renderWithProviders } from './helpers'
import { api } from '../api/client'
import { useCommandCenter } from '../pages/chat/command-center/useCommandCenter'
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

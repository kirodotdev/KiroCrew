// @vitest-environment jsdom
import { act, waitFor } from '@testing-library/react'
import { useQueryClient } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { resolveAskAfterSend } from '../lib/resolveAskAfterSend'
import { useCommandCenter } from '../pages/chat/command-center/useCommandCenter'
import { registerMainComposer } from '../utils/composerRestore'
import { createTestStore, renderHookWithProviders } from './helpers'

const blocking = {
  slot: 'root',
  ask_id: 'queued-ask',
  questions: [{ question: 'Which scope?', options: [{ label: 'Stable' }] }],
}

function store() {
  const initial = createTestStore().getState()
  return createTestStore({
    ...initial,
    dashboard: {
      ...initial.dashboard,
      connected: true,
      slots: [{ key: 'root', messages: 0, running: true }],
    },
  })
}

describe('Command Center draft reconciliation after a composer release', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    vi.spyOn(api, 'pendingQuestions').mockResolvedValue([blocking])
    vi.spyOn(api, 'approvals').mockResolvedValue([])
    vi.spyOn(api, 'workflowRuns').mockResolvedValue({ runs: [] })
    vi.spyOn(api, 'sessionWorkProjection').mockResolvedValue({ value: { items: [] } })
    vi.spyOn(api, 'artifacts').mockResolvedValue({ artifacts: [] })
  })

  it('restores a Command Center-only draft after an accepted queued send settles the ask', async () => {
    let finishAnswer!: () => void
    vi.spyOn(api, 'answerQuestion').mockReturnValue(new Promise(resolve => {
      finishAnswer = () => resolve({ ok: true })
    }))
    const taskStore = store()
    const restored: string[] = []
    const unregister = registerMainComposer((slot, text) => {
      restored.push(`${slot}:${text}`)
      return true
    })
    const view = renderHookWithProviders(() => ({
      ...useCommandCenter('root'),
      queryClient: useQueryClient(),
    }), { store: taskStore })

    try {
      await waitFor(() => expect(view.result.current.loading).toBe(false))
      act(() => view.result.current.onQuestionDraftChange(
        blocking,
        { 'Which scope?': 'Stable' },
      ))
      let release!: Promise<boolean>
      act(() => {
        release = resolveAskAfterSend(
          { ok: true, queued: true },
          blocking.ask_id,
          taskStore.dispatch,
          blocking.slot,
          taskStore.getState,
        )
      })
      await waitFor(() => expect(taskStore.getState().chat.questionRequestsInFlight[blocking.ask_id]).toBe('queued'))
      await act(async () => {
        finishAnswer()
        await release
      })
      expect(taskStore.getState().chat.questionsSettled[blocking.ask_id]).toBe(true)
      expect(taskStore.getState().chat.questionRequestsInFlight[blocking.ask_id]).toBeUndefined()

      vi.mocked(api.pendingQuestions).mockResolvedValue([])
      await act(async () => {
        await view.result.current.queryClient.refetchQueries({ queryKey: ['command-center', 'questions'] })
      })
      await waitFor(() => expect(view.result.current.hasQuestionDraft).toBe(false))
      expect(restored).toEqual(['root:Stable'])
      expect(view.result.current.restoredQuestionNotices).toEqual([
        expect.objectContaining({ slot: 'root', question: 'Which scope?' }),
      ])
    } finally {
      unregister()
    }
  })

  it('leaves settlement to the composer release when the inventory reports the ask answered first', async () => {
    let finishAnswer!: () => void
    vi.spyOn(api, 'answerQuestion').mockReturnValue(new Promise(resolve => {
      finishAnswer = () => resolve({ ok: true })
    }))
    const taskStore = store()
    const unregister = registerMainComposer(() => true)
    const view = renderHookWithProviders(() => ({
      ...useCommandCenter('root'),
      queryClient: useQueryClient(),
    }), { store: taskStore })

    try {
      await waitFor(() => expect(view.result.current.loading).toBe(false))
      act(() => view.result.current.onQuestionDraftChange(
        blocking,
        { 'Which scope?': 'Stable' },
      ))
      let release!: Promise<boolean>
      act(() => {
        release = resolveAskAfterSend(
          { ok: true },
          blocking.ask_id,
          taskStore.dispatch,
          blocking.slot,
          taskStore.getState,
        )
      })
      await waitFor(() => expect(taskStore.getState().chat.questionRequestsInFlight[blocking.ask_id]).toBe('composer'))

      // The refetch lands while the composer's answer request is still in flight.
      vi.mocked(api.pendingQuestions).mockResolvedValue(Object.assign([], {
        resolved: { [blocking.ask_id]: 'composer' },
      }))
      await act(async () => {
        await view.result.current.queryClient.refetchQueries({ queryKey: ['command-center', 'questions'] })
      })
      await waitFor(() => expect(view.result.current.hasQuestionDraft).toBe(false))
      // Settling here would let the chat card drop its retained draft before the
      // release could hand it back, so only the release may settle the ask.
      expect(taskStore.getState().chat.questionsSettled[blocking.ask_id]).toBeUndefined()

      await act(async () => {
        finishAnswer()
        await release
      })
      expect(taskStore.getState().chat.questionsSettled[blocking.ask_id]).toBe(true)
    } finally {
      unregister()
    }
  })
})

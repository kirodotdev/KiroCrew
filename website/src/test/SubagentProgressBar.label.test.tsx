/**
 * A `spawn_run` label names the task-box row.
 *
 * The row is named by the first 80 characters of the task text, and an
 * orchestrator's task is a long prompt, so two parallel rows often read the
 * same. A caller-given `label` rides on the spawn frame and names the row
 * instead; a run without one keeps the task text. The label also survives the
 * terminal frame and a reconnect snapshot that rebuild the row.
 */

import { describe, it, expect, vi } from 'vitest'
import { render } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, { setActiveSlot, sseSubagentDone, sseSubagentSnapshot, sseSubagentSpawn } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

vi.mock('../api/client', () => ({
  api: {
    spawnDelete: vi.fn().mockResolvedValue({}),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
  },
}))

import SubagentProgressBar from '../pages/chat/SubagentProgressBar'

const SLOT = 'label-slot'
const LONG_TASK = 'Review the retry ladder in the uploader and report every path that retries without a cap, with file and line'

function makeStore() {
  const store = configureStore({
    reducer: { chat: chatReducer, dashboard: dashboardReducer, notifications: notificationsReducer },
  })
  store.dispatch(setActiveSlot(SLOT))
  return store
}

function renderBar(store: ReturnType<typeof makeStore>) {
  const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <Provider store={store}>
        <SubagentProgressBar slot={SLOT} />
      </Provider>
    </QueryClientProvider>,
  )
}

describe('SubagentProgressBar — spawn_run label', () => {
  it('names a labelled row by its label, and an unlabelled one by its task text', () => {
    const store = makeStore()
    store.dispatch(sseSubagentSpawn({ slot: SLOT, id: 'a1', task: LONG_TASK, agent: 'kirocrew', label: 'Retry ladder audit' }))
    store.dispatch(sseSubagentSpawn({ slot: SLOT, id: 'a2', task: LONG_TASK, agent: 'kirocrew' }))
    const { container } = renderBar(store)
    const names = [...container.querySelectorAll('span.truncate.text-text')].map(s => s.textContent || '')
    const preview = `${LONG_TASK.slice(0, 80)}…`
    // The labelled row leads with its label, then the start of its task.
    expect(names).toContain(`Retry ladder audit · ${preview}`)
    // The unlabelled row is named by the start of its task alone.
    expect(names).toContain(preview)
    // The labelled row keeps the whole task one hover away.
    const named = [...container.querySelectorAll('span[title]')].find(s => (s.textContent || '').startsWith('Retry ladder audit'))
    expect(named?.getAttribute('title')).toBe(LONG_TASK)
    // The task part is muted, so the label is what reads first.
    expect(named?.querySelector('span.text-muted')?.textContent).toBe(` · ${preview}`)
  })

  it('keeps the label through the terminal frame and a reconnect snapshot', () => {
    const store = makeStore()
    store.dispatch(sseSubagentSnapshot({
      id: 'a3', slot: SLOT, task: LONG_TASK, agent: 'kirocrew', label: 'From snapshot',
      streaming: '', last_tool: '', started: 1_760_000_000,
    }))
    expect(store.getState().chat.subagents.a3.label).toBe('From snapshot')
    store.dispatch(sseSubagentDone({ slot: SLOT, id: 'a4', elapsed: 3, task: LONG_TASK, agent: 'kirocrew', label: 'From done' }))
    expect(store.getState().chat.subagents.a4.label).toBe('From done')
  })
})

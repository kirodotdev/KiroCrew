/**
 * The scene agent list names an untitled chat "Untitled session", never its
 * slot key (#23084). The row id still carries the key.
 */
import { describe, it, expect, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import i18next from 'i18next'
import '../i18n/all'
import { createElement, type ReactNode } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from '../test/helpers'
import type { RootState } from '../store'

vi.mock('../api/client', () => ({
  api: {
    crons: vi.fn().mockResolvedValue([]),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    defaultAgent: vi.fn().mockResolvedValue({ default_agent: '', default_template: '', overridden: false }),
  },
}))

import { useAgentSync } from './useAgentSync'

function renderAgents(slots: Array<{ key: string; title?: string }>) {
  const defaults = createTestStore().getState()
  const store = createTestStore({
    dashboard: {
      ...defaults.dashboard,
      slots: slots.map(s => ({ ...s, messages: 1, running: false })),
    } as unknown as RootState['dashboard'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(Provider, { store }, createElement(QueryClientProvider, { client: qc }, children))
  return renderHook(() => useAgentSync(), { wrapper })
}

function agentsFor(slots: Array<{ key: string; title?: string }>) {
  return renderAgents(slots).result.current.agents
}

describe('useAgentSync slot names', () => {
  it.each([
    ['an untitled slot (title equals the key)', 'chat-9-1785440411', 'Untitled session'],
    ['a slot with no title', undefined, 'Untitled session'],
    ['a titled slot', 'Fix the build', 'Fix the build'],
  ])('names %s by its label', (_case, title, shown) => {
    const [row] = agentsFor([{ key: 'chat-9-1785440411', title }])
    expect(row.id).toBe('slot-chat-9-1785440411')
    expect(row.name).toBe(shown)
  })

  it('re-names an untitled slot on a language switch with the slot list unchanged', async () => {
    const hook = renderAgents([{ key: 'chat-9-1785440411', title: 'chat-9-1785440411' }])
    expect(hook.result.current.agents[0].name).toBe('Untitled session')
    try {
      await act(async () => { await i18next.changeLanguage('zh-CN') })
      hook.rerender()
      expect(hook.result.current.agents[0].name).toBe('未命名会话')
    } finally {
      await act(async () => { await i18next.changeLanguage('en') })
    }
  })
})

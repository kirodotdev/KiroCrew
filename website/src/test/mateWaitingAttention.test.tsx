/**
 * Session Dashboards: a Bot's session leaves the page while its Bot thread is
 * live, so when it waits on the person the Bot's own row carries the wait and
 * links to the Bot's chat on its Sessions tab. A session whose Bot thread is
 * closed is a row again and keeps its own card.
 */
import { render, screen } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, expect, it } from 'vitest'
import { crewModeView } from '../lib/crewMode'
import { buildCommandCenter, mateWaitingThreads } from '../pages/chat/command-center/model'
import AttentionCard from '../pages/chat/command-center/AttentionCard'
import type { ChatSlot } from '../types'
import { createTestStore } from './helpers'

const slot = (key: string, extra: Partial<ChatSlot> = {}): ChatSlot => ({ key, messages: 0, running: false, ...extra })
const rows = (mw: Partial<ChatSlot>): ChatSlot[] => [
  slot('mate', { mode: 'member', agent: 'scout' }),
  slot('mw', { created_by: 'mate', mate_root: 'mate', ...mw }),
]
const fleet = (slots: ChatSlot[]) => buildCommandCenter({
  root: null, slots: crewModeView(slots), subagents: {}, workflows: [], questions: [], approvals: [], mateWaiting: mateWaitingThreads(slots),
})

describe("Session Dashboards: a Bot's session waiting on the person", () => {
  it("puts its approval or question on the Bot's row", () => {
    const model = fleet(rows({ pending_approval: true }))
    expect(model.attention.map(a => [a.slot, a.kind, a.mateChat])).toEqual([['mate', 'open_session', true]])
    expect(model.nodes.find(n => n.slot === 'mate')?.state).toBe('needs_input')
    expect(fleet(rows({ needs_input: true })).attention.map(a => a.slot)).toEqual(['mate'])
    // Nothing waits: no card. A closed Bot thread leaves its session on the page with its own card.
    expect(fleet(rows({})).attention).toEqual([])
    expect(fleet(rows({ pending_approval: true }).slice(1)).attention.map(a => [a.slot, a.kind])).toEqual([['mw', 'approval']])
  })

  it('names the Bot and opens its chat', () => {
    const slots = rows({ pending_approval: true })
    const item = fleet(slots).attention[0]
    render(<QueryClientProvider client={new QueryClient()}><Provider store={createTestStore({ dashboard: { slots } } as never)}><MemoryRouter>
      <AttentionCard item={item} title="Session 1" />
    </MemoryRouter></Provider></QueryClientProvider>)
    expect(screen.getByTestId('mate-chat-notice')).toHaveTextContent('scout')
    expect(screen.getByRole('link', { name: /Open chat/ })).toHaveAttribute('href', '/members?member=scout')
    expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull()
  })
})

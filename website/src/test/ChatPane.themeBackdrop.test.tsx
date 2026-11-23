import { afterEach, expect, it, vi } from 'vitest'
import { act, cleanup } from '@testing-library/react'
import { createConnectedTestStore, renderWithProviders } from './helpers'
import { api } from '../api/client'
import ChatPane from '../components/ChatPane'

vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: 'default' }) }))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.clearAllMocks() })

// A Bot's chat (frameless) and a split-view pane (framed) both leave the
// background to their host, so a `layer: "behind"` theme overlay under the
// shell is not painted over by the pane.
it.each([
  ['a Bot chat', true],
  ['a split-view pane', false],
])('%s paints no background of its own', async (_label, frameless) => {
  vi.spyOn(api, 'chatSlotDetail').mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 })
  const view = renderWithProviders(<ChatPane slotKey="member-radar" frameless={frameless} />, {
    store: createConnectedTestStore(),
  })
  const pane = view.container.querySelector<HTMLElement>('[data-chat-pane]')
  expect(pane).not.toBeNull()
  expect([...pane!.classList].filter((c) => c.startsWith('bg-'))).toEqual([])
  view.unmount()
  // ChatPane releases its observed slot in a microtask on unmount.
  await act(async () => { await Promise.resolve() })
  view.queryClient.clear()
})

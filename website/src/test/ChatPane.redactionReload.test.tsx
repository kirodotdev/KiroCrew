import { afterEach, expect, it, vi } from 'vitest'
import { act, cleanup } from '@testing-library/react'
import { createConnectedTestStore, renderWithProviders } from './helpers'
import { api } from '../api/client'
import ChatPane from '../components/ChatPane'
import { useRedactionHostsReload } from '../components/chat/useRedactionHostsReload'

vi.mock('../components/chat/useRedactionHostsReload', () => ({ useRedactionHostsReload: vi.fn() }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: 'default' }) }))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.clearAllMocks() })

it('wires redaction reloads to the slot the Bot pane currently shows', async () => {
  vi.spyOn(api, 'chatSlotDetail').mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 })
  const view = renderWithProviders(<ChatPane slotKey="member-radar" crewmate={{ name: 'Radar' }} />, {
    store: createConnectedTestStore(),
  })
  expect(useRedactionHostsReload).toHaveBeenLastCalledWith('member-radar')
  view.rerender(<ChatPane slotKey="member-sonar" crewmate={{ name: 'Sonar' }} />)
  expect(useRedactionHostsReload).toHaveBeenLastCalledWith('member-sonar')
  view.unmount()
  // ChatPane releases its observed slot in a microtask on unmount.
  await act(async () => { await Promise.resolve() })
  view.queryClient.clear()
})

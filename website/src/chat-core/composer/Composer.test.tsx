import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer from '../../store/chatSlice'
import dashboardReducer from '../../store/dashboardSlice'
import notificationsReducer from '../../store/notificationsSlice'
import type { RootState } from '../../store'

/* chat-core P3-b: `ChatInput` gets its microphone from the Composer root's Voice
 * atom, not from props. These tests pin the contract at the root: standalone =
 * no mic; under a root = mic; the host handle reaches the atom's controls. */

const engine = {
  recording: false, transcribing: false, sessionOwner: null as string | null, streamEnabled: false,
  toggle: vi.fn(), start: vi.fn().mockResolvedValue(undefined), stop: vi.fn(), cancel: vi.fn(), prewarm: vi.fn(),
  error: null as string | null, level: 0, deviceLabel: '', deviceId: '', clearError: vi.fn(), partial: '',
  download: null, sampleRef: { current: {} }, switchDevice: vi.fn(), deviceSwitchIsLive: false,
}
vi.mock('../../hooks/useVoiceInput', () => ({ useVoiceInput: () => engine, voiceInputSupported: true }))
vi.mock('../../hooks/usePushToTalk', () => ({ usePushToTalk: () => undefined }))
vi.mock('../../api/client', () => ({
  api: {
    sttConfig: vi.fn().mockResolvedValue({ enabled: true, available: true, streaming: false, dictation_panel: true, provider: 'local' }),
    fileSearch: vi.fn().mockResolvedValue({ root: '/repo', results: [] }),
    agents: vi.fn().mockResolvedValue([]),
    models: vi.fn().mockResolvedValue([]),
    dashboardConfig: vi.fn().mockResolvedValue({}),
  },
  SEARCH_MIN_CHARS: 2,
  ApiError: class ApiError extends Error {},
}))
vi.mock('../../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatInput from '../../components/ChatInput'
import { Composer, type ComposerHandle } from './Composer'
import { SlotProvider } from '../../providers/SlotContext'

function makeStore() {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true, slots: [], unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
}

function Providers({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  qc.setQueryData(['sttConfig'], { enabled: true, available: true, streaming: false, dictation_panel: true, provider: 'local' })
  return (
    <Provider store={makeStore()}>
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <SlotProvider slotId="chat-1-x">{children}</SlotProvider>
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>
  )
}

const input = { value: '', onChange: () => {}, onSend: () => {} }
const mic = () => screen.queryByRole('button', { name: 'Voice input' })

beforeEach(() => { engine.start.mockClear() })

describe('Composer root + Voice atom', () => {
  it('a standalone ChatInput has no microphone', () => {
    render(<Providers><ChatInput {...input} /></Providers>)
    expect(mic()).toBeNull()
  })

  it('under a Composer root the same ChatInput renders the microphone, with zero voice props', async () => {
    await act(async () => {
      render(
        <Providers>
          <Composer slotKey="chat-1-x" value="" onChange={() => {}}>
            <ChatInput {...input} />
          </Composer>
        </Providers>,
      )
    })
    expect(mic()).toBeTruthy()
    await act(async () => { fireEvent.click(mic()!) })
    expect(engine.start).toHaveBeenCalledTimes(1)
  })

  it('the host handle reaches the atom controls (disarmForSend for the send path)', async () => {
    const ref = { current: null as ComposerHandle | null }
    await act(async () => {
      render(
        <Providers>
          <Composer ref={ref} slotKey="chat-1-x" value="" onChange={() => {}}>
            <ChatInput {...input} />
          </Composer>
        </Providers>,
      )
    })
    expect(typeof ref.current?.voice()?.disarmForSend).toBe('function')
  })
})

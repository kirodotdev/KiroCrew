import { describe, it, expect, vi, beforeEach } from 'vitest'
import { useState, type ReactNode } from 'react'
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
import { ComposerEditor, ComposerSend, composerSendState } from './atoms'
import { createComposerDraftStore } from './draftStore'
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

describe('Composer root with a draft store (P3-f)', () => {
  it('ChatInput shows and edits the store text while the host does not re-render', async () => {
    const draft = createComposerDraftStore('hello')
    let hostRenders = 0
    const onChange = (v: string) => draft.set(v)
    function Host() {
      hostRenders++
      return (
        <Composer slotKey="chat-1-x" draft={draft} onChange={onChange}>
          <ChatInput onChange={onChange} onSend={() => {}} />
        </Composer>
      )
    }
    await act(async () => { render(<Providers><Host /></Providers>) })
    const box = screen.getByRole('textbox', { name: 'Message input' }) as HTMLTextAreaElement
    expect(box.value).toBe('hello')
    const before = hostRenders
    await act(async () => { draft.set('hello world') })
    expect(box.value).toBe('hello world')
    await act(async () => { fireEvent.change(box, { target: { value: 'typed by the user' } }) })
    expect(draft.get()).toBe('typed by the user')
    expect(box.value).toBe('typed by the user')
    // Every text change above reached the editor; none re-rendered the host.
    expect(hostRenders).toBe(before)
  })
})

describe('Composer capabilities (embedded preset)', () => {
  it('voice: false keeps the Voice atom unmounted, so ChatInput gets no microphone', async () => {
    await act(async () => {
      render(
        <Providers>
          <Composer slotKey="chat-1-x" value="" onChange={() => {}} capabilities={{ voice: false }}>
            <ChatInput {...input} />
          </Composer>
        </Providers>,
      )
    })
    expect(mic()).toBeNull()
  })

  it('voice: false needs no router: the root mounts outside one', async () => {
    await act(async () => {
      render(
        <Composer slotKey="chat-1-x" value="" onChange={() => {}} capabilities={{ voice: false }}>
          <ComposerEditor aria-label="Editor" />
        </Composer>,
      )
    })
    expect(screen.getByRole('textbox', { name: 'Editor' })).toBeTruthy()
  })
})

describe('Composer Editor + Send atoms (P3-f)', () => {
  function Host({ running = false, hasAttachments = false, uploading = false }: { running?: boolean; hasAttachments?: boolean; uploading?: boolean }) {
    const [text, setText] = useState('')
    const [sent, setSent] = useState<string[]>([])
    return (
      <Composer slotKey="s" value={text} onChange={setText} capabilities={{ voice: false }}>
        <ComposerEditor aria-label="Editor" />
        <ComposerSend onSend={() => setSent(s => [...s, text])} running={running} hasAttachments={hasAttachments} uploading={uploading} />
        <output data-testid="sent">{sent.join('|')}</output>
      </Composer>
    )
  }
  const send = () => document.querySelector<HTMLButtonElement>('[data-composer-send]')!

  it('the editor writes the root text and Send enables only with content', async () => {
    render(<Host />)
    expect(send().dataset.composerSend).toBe('idle')
    expect(send().disabled).toBe(true)
    fireEvent.change(screen.getByRole('textbox', { name: 'Editor' }), { target: { value: 'hi' } })
    expect(send().disabled).toBe(false)
    fireEvent.click(send())
    expect(screen.getByTestId('sent').textContent).toBe('hi')
  })

  it('a running turn turns Send into Queue once there is text, with the word shown', () => {
    render(<Host running />)
    expect(send().dataset.composerSend).toBe('idle')
    fireEvent.change(screen.getByRole('textbox', { name: 'Editor' }), { target: { value: 'later' } })
    expect(send().dataset.composerSend).toBe('queue')
    expect(screen.getByRole('button', { name: 'Queue message' })).toBe(send())
    // Visible text, not only colour, glyph and a tooltip.
    expect(send().textContent).toBe('Queue')
    expect(send().title).toBe('Run after the current work finishes')
  })

  it('staged files alone make a sendable turn, and queue while a turn runs', () => {
    const { rerender } = render(<Host hasAttachments />)
    expect(send().disabled).toBe(false)
    expect(send().dataset.composerSend).toBe('idle')
    rerender(<Host hasAttachments running />)
    expect(send().dataset.composerSend).toBe('queue')
    expect(send().disabled).toBe(false)
  })

  it('an upload still running refuses the press, with or without text', () => {
    const { rerender } = render(<Host uploading hasAttachments />)
    expect(send().disabled).toBe(true)
    fireEvent.change(screen.getByRole('textbox', { name: 'Editor' }), { target: { value: 'see attached' } })
    expect(send().disabled).toBe(true)
    rerender(<Host hasAttachments />)
    expect(send().disabled).toBe(false)
  })

  it('reads a draft store without the host re-rendering', async () => {
    const draft = createComposerDraftStore('')
    render(
      <Composer slotKey="s" draft={draft} onChange={v => draft.set(v)} capabilities={{ voice: false }}>
        <ComposerEditor aria-label="Editor" />
        <ComposerSend onSend={() => {}} />
      </Composer>,
    )
    expect(send().disabled).toBe(true)
    await act(async () => { draft.set('from the store') })
    expect((screen.getByRole('textbox', { name: 'Editor' }) as HTMLTextAreaElement).value).toBe('from the store')
    expect(send().disabled).toBe(false)
  })

  it('an atom outside a root fails loudly', () => {
    const err = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      expect(() => render(<ComposerEditor />)).toThrow(/inside <Composer>/)
    } finally { err.mockRestore() }
  })
})

describe('composerSendState', () => {
  it.each([
    [{ running: false, hasContent: false }, 'idle'],
    [{ running: false, hasContent: true }, 'idle'],
    [{ running: true, hasContent: true }, 'queue'],
    [{ running: true, hasContent: false }, 'idle'],
  ] as const)('%o -> %s', (args, want) => {
    expect(composerSendState(args)).toBe(want)
  })
})

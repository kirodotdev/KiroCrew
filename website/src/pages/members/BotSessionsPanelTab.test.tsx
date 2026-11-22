/**
 * The Bot page side panel's Sessions tab, for a session that is not open: it says
 * so and offers a reopen through the chat page's own resume path, then shows the
 * reopened session in the tab. A failed reopen stays put and says why.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createTestStore } from '../../test/helpers'

const api = vi.hoisted(() => ({ crewWorkers: vi.fn(), crewWorkerRecap: vi.fn(), resumeChatSlot: vi.fn() }))
vi.mock('../../api/client', () => ({ api }))
vi.mock('../../components/ChatPane', async () => {
  const { useContext } = await import('react')
  const { RunInTerminalScope } = await import('../../components/runInTerminalScope')
  return {
    default: function ChatPaneStub({ slotKey, focused, pushToTalk, onFocus }: { slotKey: string; focused?: boolean; pushToTalk?: boolean; onFocus?: () => void }) {
      const scope = useContext(RunInTerminalScope)
      return <button type="button" data-testid="chat-pane-stub" data-chat-pane={focused ? 'focused' : ''} data-push-to-talk={pushToTalk ? '1' : '0'} data-scope={scope ?? ''} onFocus={onFocus}>{slotKey}</button>
    },
  }
})

import BotSessionsPanelTab from './BotSessionsPanelTab'
import { RunInTerminalScope } from '../../components/runInTerminalScope'

const ROOT = 'dm-ada'

function mount(onOpenKey = vi.fn()) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = createTestStore({ dashboard: { slots: [] } } as never)
  const view = render(
    <QueryClientProvider client={qc}><Provider store={store}>
      <BotSessionsPanelTab root={ROOT} mateName="Ada" liveSlots={[]} slotsLoaded openKey="chat-old"
        onOpenKey={onOpenKey} onOpenInChat={vi.fn()} />
    </Provider></QueryClientProvider>,
  )
  return { onOpenKey, store, view }
}

beforeEach(() => {
  api.crewWorkers.mockResolvedValue({ root: ROOT, workers: [{ key: 'chat-old', title: 'Old task', agent: 'kirocrew', status: 'closed', group: 'done', last_activity: '', ledger_item: '', created_by: ROOT }] })
  api.resumeChatSlot.mockReset()
})

describe('BotSessionsPanelTab Escape', () => {
  it('goes Back on Escape, but not when an owner inside took the press', async () => {
    const onOpenKey = vi.fn()
    mount(onOpenKey)
    const reopen = await screen.findByTestId('bot-session-reopen')
    reopen.addEventListener('keydown', e => e.preventDefault(), { once: true })
    fireEvent.keyDown(reopen, { key: 'Escape' })
    expect(onOpenKey).not.toHaveBeenCalled()
    fireEvent.keyDown(reopen, { key: 'Escape' })
    expect(onOpenKey).toHaveBeenCalledWith(null)
  })

  it('leaves an Escape that ends an IME composition alone', async () => {
    const onOpenKey = vi.fn()
    mount(onOpenKey)
    const reopen = await screen.findByTestId('bot-session-reopen')
    fireEvent.keyDown(reopen, { key: 'Escape', isComposing: true })
    fireEvent.keyDown(reopen, { key: 'Escape', keyCode: 229 })
    expect(onOpenKey).not.toHaveBeenCalled()
  })
})

describe('BotSessionsPanelTab open session pane', () => {
  function mountLive(onPaneFocusChange = vi.fn()) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const store = createTestStore({ dashboard: { slots: [{ key: 'chat-live', title: 'Live task', mate_root: ROOT, project: '/w/live', messages: 1, running: false }] } } as never)
    const tree = (panelHidden: boolean) => (
      <QueryClientProvider client={qc}><Provider store={store}>
        <RunInTerminalScope.Provider value="page-scope">
          <BotSessionsPanelTab root={ROOT} mateName="Ada" liveSlots={[]} slotsLoaded openKey="chat-live"
            onOpenKey={vi.fn()} onOpenInChat={vi.fn()} onPaneFocusChange={onPaneFocusChange} panelHidden={panelHidden} />
          <div data-chat-pane=""><input aria-label="bot composer" /></div>
          <p>Bot reply text</p>
        </RunInTerminalScope.Provider>
      </Provider></QueryClientProvider>
    )
    const { rerender } = render(tree(false))
    return { hide: (hidden: boolean) => rerender(tree(hidden)) }
  }

  it('gives the dictation key back while the side panel is hidden with focus still inside', async () => {
    const onPaneFocusChange = vi.fn()
    const { hide } = mountLive(onPaneFocusChange)
    const pane = await screen.findByTestId('chat-pane-stub')
    act(() => { pane.focus() })
    expect(pane).toHaveAttribute('data-push-to-talk', '1')
    hide(true)
    expect(pane).toHaveAttribute('data-push-to-talk', '0')
    expect(pane).toHaveAttribute('data-chat-pane', '')
    expect(onPaneFocusChange).toHaveBeenLastCalledWith(false)
    hide(false)
    expect(pane).toHaveAttribute('data-push-to-talk', '1')
    expect(onPaneFocusChange).toHaveBeenLastCalledWith(true)
  })

  it('answers Run in terminal with its own bridge, not the page\'s', async () => {
    mountLive()
    const pane = await screen.findByTestId('chat-pane-stub')
    expect(pane.getAttribute('data-scope')).toBeTruthy()
    expect(pane.getAttribute('data-scope')).not.toBe('page-scope')
  })

  it('is the focused pane, with the dictation key, while focus is in it, and not after focus or a press elsewhere', async () => {
    const onPaneFocusChange = vi.fn()
    mountLive(onPaneFocusChange)
    const pane = await screen.findByTestId('chat-pane-stub')
    expect(pane).toHaveAttribute('data-chat-pane', '')
    expect(pane).toHaveAttribute('data-push-to-talk', '0')
    act(() => { pane.focus() })
    expect(pane).toHaveAttribute('data-chat-pane', 'focused')
    expect(pane).toHaveAttribute('data-push-to-talk', '1')
    expect(onPaneFocusChange).toHaveBeenLastCalledWith(true)
    act(() => { screen.getByLabelText('bot composer').focus() })
    expect(pane).toHaveAttribute('data-chat-pane', '')
    expect(onPaneFocusChange).toHaveBeenLastCalledWith(false)
    act(() => { pane.focus() })
    // A press on text that takes no focus releases it too.
    fireEvent.mouseDown(screen.getByText('Bot reply text'))
    expect(pane).toHaveAttribute('data-chat-pane', '')
  })
})

describe('BotSessionsPanelTab header and reads', () => {
  it('says so when the session list cannot be read', async () => {
    api.crewWorkers.mockRejectedValue(Object.assign(new Error('boom'), { status: 500 }))
    mount()
    expect(await screen.findByText('Could not load the full session list. Showing the open sessions.')).toBeInTheDocument()
  })

  it('opens the chat page through the host on a plain click of Open in chat', async () => {
    const onOpenInChat = vi.fn()
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}><Provider store={createTestStore({ dashboard: { slots: [] } } as never)}>
        <BotSessionsPanelTab root={ROOT} mateName="Ada" liveSlots={[]} slotsLoaded openKey="chat-old"
          onOpenKey={vi.fn()} onOpenInChat={onOpenInChat} />
      </Provider></QueryClientProvider>,
    )
    const link = await screen.findByTestId('bot-session-open-in-chat')
    expect(link).toHaveAttribute('href', '/chat?sid=chat-old')
    fireEvent.click(link)
    expect(onOpenInChat).toHaveBeenCalledWith('chat-old')
  })
})

describe('BotSessionsPanelTab closed session', () => {
  it('reopens it and shows the reopened key in the tab', async () => {
    api.resumeChatSlot.mockResolvedValue({ ok: true, key: 'chat-old-2', mode: 'default', messages: [] })
    const { onOpenKey, store } = mount()
    const activeBefore = store.getState().chat.activeSlot
    await waitFor(() => expect(screen.getByTestId('bot-session-title')).toHaveTextContent('Old task'))
    expect(screen.queryByTestId('chat-pane-stub')).toBeNull()
    fireEvent.click(screen.getByTestId('bot-session-reopen'))
    await waitFor(() => expect(onOpenKey).toHaveBeenCalledWith('chat-old-2'))
    expect(api.resumeChatSlot).toHaveBeenCalledWith('dashboard_chat-old', 'Old task')
    // Listed for the panel, but the chat page's active session stays where it was.
    expect(store.getState().dashboard.slots.map((r: { key: string }) => r.key)).toContain('chat-old-2')
    expect(store.getState().chat.activeSlot).toBe(activeBefore)
    // The button is usable again, not left busy.
    expect(screen.getByTestId('bot-session-reopen')).not.toHaveAttribute('aria-busy')
  })

  it('drops a reopen that answers after the person left the session', async () => {
    let answer: (v: unknown) => void = () => {}
    api.resumeChatSlot.mockReturnValue(new Promise(r => { answer = r }))
    const { onOpenKey, view } = mount()
    fireEvent.click(await screen.findByTestId('bot-session-reopen'))
    view.unmount()
    answer({ ok: true, key: 'chat-old-2', mode: 'default', messages: [] })
    await new Promise(r => setTimeout(r, 0))
    expect(onOpenKey).not.toHaveBeenCalled()
  })

  it('stays on the closed session and says the reopen failed', async () => {
    api.resumeChatSlot.mockResolvedValue({ ok: false, key: 'chat-old', messages: [] })
    const { onOpenKey } = mount()
    fireEvent.click(await screen.findByTestId('bot-session-reopen'))
    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(onOpenKey).not.toHaveBeenCalled()
  })
})

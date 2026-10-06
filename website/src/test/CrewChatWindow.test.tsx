/**
 * The crew chat window drives the PEER's own slot through the hub proxy.
 *
 * Each case pins one exit criterion of the remote-crew sidebar RFC's wave 3:
 * approve resolves the peer's own pending approval (with the row id its strict
 * check needs), a reload mid-turn shows the peer's turn running, and continue /
 * regenerate / rewind go to the peer's routes and render the peer's answer.
 * Redaction of peer text happens in the hub proxy and is pinned there
 * (test/test_instances.py, TestProxyRedactsPeerReplies).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import type { RootState } from '../store'

const mocks = vi.hoisted(() => ({
  crewPeerGet: vi.fn(), crewPeerPost: vi.fn(), listInstances: vi.fn(), instancesCapabilities: vi.fn(),
  // The LOCAL slot routes. A crew window must never reach them: on a remote
  // slot they are what answered 409.
  continueSlot: vi.fn(), regenerateSlot: vi.fn(), rewind: vi.fn(), approveChatSlot: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mocks }))

import CrewChatWindow from '../pages/chat/crew-window/CrewChatWindow'
import {
  openCrewWindow, closeCrewWindow, useCrewWindow, reloadCrewWindowForTest, writeCrewDraft,
} from '../pages/chat/crew-window/crewWindowStore'

class FakeEventSource {
  static all: FakeEventSource[] = []
  static CLOSED = 2
  readyState = 0
  onopen?: () => void
  onerror?: () => void
  listeners: Record<string, ((e: MessageEvent) => void)[]> = {}
  close = vi.fn()
  constructor(public url: string) { FakeEventSource.all.push(this) }
  addEventListener(type: string, fn: (e: MessageEvent) => void) { (this.listeners[type] ||= []).push(fn) }
  emit(type: string, data: unknown) {
    for (const fn of this.listeners[type] || []) fn({ data: JSON.stringify(data) } as MessageEvent)
  }
}

function Host() {
  const target = useCrewWindow()
  return target ? <CrewChatWindow target={target} /> : <div data-testid="no-window" />
}

let slotRow: Record<string, unknown>
let detail: Record<string, unknown>

function renderWindow() {
  const store = createTestStore({
    instances: { warm: { 'cd-1': { local_port: 1, token: 't' } } } as unknown as RootState['instances'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}><Provider store={store}><Host /></Provider></QueryClientProvider>,
  )
}

const posted = () => mocks.crewPeerPost.mock.calls.map(c => [c[1], c[2]])

beforeEach(() => {
  FakeEventSource.all = []
  vi.stubGlobal('EventSource', FakeEventSource)
  sessionStorage.clear()
  writeCrewDraft({ instanceId: 'cd-1', key: 'k1' }, '')
  slotRow = { key: 'k1', title: 'Build fix', running: false, interrupted: false }
  detail = { running: false, messages: [{ role: 'user', content: 'hi', ts: 't1' }, { role: 'assistant', content: 'hello', ts: 't2' }] }
  mocks.listInstances.mockResolvedValue({ instances: [{ id: 'cd-1', name: 'devbox' }] })
  mocks.crewPeerGet.mockImplementation((_id: string, path: string) =>
    Promise.resolve(path === 'api/chat/slots' ? [{ key: 'other' }, slotRow] : detail))
  mocks.crewPeerPost.mockResolvedValue({ ok: true })
  mocks.instancesCapabilities.mockResolvedValue({ version_match: true, version: '0.9.0', local_version: '0.9.0' })
  openCrewWindow({ instanceId: 'cd-1', key: 'k1' })
})

afterEach(() => {
  closeCrewWindow()
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

describe('CrewChatWindow', () => {
  it('approves the PEER\'s pending approval with the row id its strict check needs', async () => {
    slotRow = { ...slotRow, running: true, pending_approval_info: { origin: 'native', request_id: '7', request_mid: 'm-7', tool: 'shell', tool_input: 'ls' } }
    renderWindow()
    fireEvent.click(await screen.findByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(posted()).toContainEqual([
      'api/chat/slots/k1/approve', { action: 'approved', request_id: '7', request_mid: 'm-7', origin: 'native' },
    ]))
    expect(mocks.crewPeerPost.mock.calls[0][0]).toBe('cd-1')
    expect(mocks.approveChatSlot).not.toHaveBeenCalled()
  })

  it('keeps Stop reachable while an approval is pending', async () => {
    slotRow = { ...slotRow, running: true, pending_approval_info: { origin: 'native', request_id: '7', request_mid: 'm-7', tool: 'shell' } }
    renderWindow()
    await screen.findByTestId('crew-window-approval')
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }))
    await waitFor(() => expect(posted()).toContainEqual(['api/chat/slots/k1/stop', undefined]))
  })

  it('makes the covered local pane inert, so an unseen local approval is unreachable', async () => {
    const store = createTestStore({
      instances: { warm: { 'cd-1': { local_port: 1, token: 't' } } } as unknown as RootState['instances'],
    })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const pane = (open: boolean, late: boolean) => (
      <QueryClientProvider client={qc}><Provider store={store}><div>
        {open && <div data-crew-cover><CrewChatWindow target={{ instanceId: 'cd-1', key: 'k1' }} /></div>}
        <div data-testid="local-composer"><button>Allow once</button></div>
        {late && <div data-testid="late-sibling" />}
      </div></Provider></QueryClientProvider>
    )
    const { rerender } = render(pane(true, false))
    await screen.findByTestId('crew-chat-window')
    expect(screen.getByTestId('local-composer')).toHaveAttribute('inert')
    expect(document.activeElement).toBe(screen.getByTestId('crew-chat-window'))
    rerender(pane(true, true))
    await waitFor(() => expect(screen.getByTestId('late-sibling')).toHaveAttribute('inert'))
    rerender(pane(false, true))
    expect(screen.getByTestId('local-composer')).not.toHaveAttribute('inert')
    expect(screen.getByTestId('late-sibling')).not.toHaveAttribute('inert')
  })

  it('re-reads the version after a tunnel-down answer, so a reconnect unlocks the window', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      mocks.instancesCapabilities
        .mockResolvedValueOnce({ version_match: false, version: '', local_version: '0.9.0' })
        .mockResolvedValue({ version_match: true, version: '0.9.0', local_version: '0.9.0' })
      renderWindow()
      await waitFor(() => expect(mocks.instancesCapabilities).toHaveBeenCalledTimes(1))
      expect(mocks.crewPeerGet).not.toHaveBeenCalled()
      await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
      await waitFor(() => expect(mocks.crewPeerGet).toHaveBeenCalled())
      expect(await screen.findByText('hello')).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })

  it('saves an unsent draft to session storage, so it survives a reload', async () => {
    renderWindow()
    const box = await screen.findByRole('textbox')
    fireEvent.change(box, { target: { value: 'half a thought' } })
    await waitFor(() => expect(sessionStorage.getItem('kirocrew.crewDrafts') ?? '').toContain('half a thought'))
  })

  it('offers no card for an approval it cannot tie to one peer row', async () => {
    // A bare id can decide the wrong request (ids are per connection and get
    // reused), so a coordinator approval with no row id gets no buttons here.
    slotRow = { ...slotRow, running: true, pending_approval_info: { origin: 'coordinator', request_id: 'spawn:1', tool: 'spawn' } }
    renderWindow()
    await screen.findByTestId('crew-window-approval-elsewhere')
    expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull()
  })

  it('shows the peer turn still running after a reload, not "Turn interrupted"', async () => {
    slotRow = { ...slotRow, running: true }
    detail = { running: true, messages: [{ role: 'user', content: 'long job', ts: 't1' }] }
    // A reload re-reads the open window from sessionStorage.
    reloadCrewWindowForTest()
    renderWindow()
    expect(await screen.findByTestId('crew-window-running')).toHaveTextContent('Thinking…')
    expect(screen.queryByText('Turn interrupted')).toBeNull()
  })

  it('continues an interrupted turn on the PEER and shows the peer\'s answer', async () => {
    slotRow = { ...slotRow, interrupted: true }
    detail = { running: false, messages: [{ role: 'user', content: 'go', ts: 't1' }] }
    renderWindow()
    fireEvent.click(await screen.findByRole('button', { name: 'Continue' }))
    detail = { running: false, messages: [{ role: 'user', content: 'go', ts: 't1' }, { role: 'assistant', content: 'peer answer', ts: 't2' }] }
    slotRow = { ...slotRow, interrupted: false }
    await waitFor(() => expect(posted()).toContainEqual(['api/chat/slots/k1/continue', undefined]))
    expect(await screen.findByText('peer answer')).toBeTruthy()
    expect(mocks.continueSlot).not.toHaveBeenCalled()
  })

  it('regenerates on the PEER', async () => {
    renderWindow()
    fireEvent.click(await screen.findByRole('button', { name: 'Regenerate' }))
    await waitFor(() => expect(posted()).toContainEqual(['api/chat/slots/k1/regenerate', undefined]))
    expect(mocks.regenerateSlot).not.toHaveBeenCalled()
  })

  it('rewinds on the PEER with the edited text', async () => {
    renderWindow()
    fireEvent.click(await screen.findByRole('button', { name: 'Rewind to here' }))
    const box = screen.getByRole('textbox', { name: 'Message the agent on devbox…' })
    expect(box).toHaveValue('hi')
    fireEvent.change(box, { target: { value: 'hi again' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(posted()).toContainEqual(['api/chat/slots/k1/rewind', { ts: 't1', content: 'hi again' }]))
    expect(mocks.rewind).not.toHaveBeenCalled()
  })

  it('sends to the PEER slot and stops the PEER turn', async () => {
    renderWindow()
    const box = await screen.findByRole('textbox', { name: 'Message the agent on devbox…' })
    fireEvent.change(box, { target: { value: 'next' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    await waitFor(() => expect(posted()).toContainEqual(['api/chat?ws=1', { message: 'next', slot: 'k1' }]))
    const es = FakeEventSource.all[0]
    act(() => es.emit('slots', [{ ...slotRow, running: true }]))
    fireEvent.click(await screen.findByRole('button', { name: 'Stop' }))
    await waitFor(() => expect(posted()).toContainEqual(['api/chat/slots/k1/stop', undefined]))
  })

  it('keeps text typed while a send is still in flight', async () => {
    let finish: (v: unknown) => void = () => {}
    mocks.crewPeerPost.mockReturnValue(new Promise(r => { finish = r }))
    renderWindow()
    const box = await screen.findByRole('textbox', { name: 'Message the agent on devbox…' })
    fireEvent.change(box, { target: { value: 'first' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    expect(box).toHaveValue('')
    fireEvent.change(box, { target: { value: 'second, not sent yet' } })
    await act(async () => { finish({ ok: true }) })
    expect(box).toHaveValue('second, not sent yet')
  })

  it('restores a failed send into an empty composer', async () => {
    mocks.crewPeerPost.mockRejectedValue(new Error('peer refused'))
    renderWindow()
    const box = await screen.findByRole('textbox', { name: 'Message the agent on devbox…' })
    fireEvent.change(box, { target: { value: 'try this' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    await waitFor(() => expect(box).toHaveValue('try this'))
  })

  it('says so when the peer feed is lost, and retries on demand', async () => {
    renderWindow()
    await screen.findByText('hello')
    const es = FakeEventSource.all[0]
    act(() => { es.readyState = 2; es.onerror?.() })
    expect(await screen.findByText('Lost live updates from devbox.')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(FakeEventSource.all).toHaveLength(2))
    expect(es.close).toHaveBeenCalled()
  })

  it('keeps the rewind target after a failed rewind, so Send retries the rewind', async () => {
    renderWindow()
    fireEvent.click(await screen.findByRole('button', { name: 'Rewind to here' }))
    mocks.crewPeerPost.mockRejectedValueOnce(new Error('peer refused'))
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(screen.getByRole('textbox', { name: 'Message the agent on devbox…' })).toHaveValue('hi'))
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(posted().filter(p => p[0] === 'api/chat/slots/k1/rewind')).toHaveLength(2))
    expect(posted().some(p => p[0] === 'api/chat?ws=1')).toBe(false)
  })

  it('shows the approval, not a second running label, while one is pending', async () => {
    slotRow = { ...slotRow, running: true, pending_approval_info: { origin: 'native', request_id: '7', request_mid: 'm-7', tool: 'shell' } }
    renderWindow()
    await screen.findByTestId('crew-window-approval')
    expect(screen.queryByTestId('crew-window-running')).toBeNull()
  })

  it('offers no rewind on a message the hub redacted', async () => {
    detail = { running: false, messages: [{ role: 'user', content: 'key [REDACTED:aws-access-key]', ts: 't1' }, { role: 'assistant', content: 'ok', ts: 't2' }] }
    renderWindow()
    await screen.findByText('ok')
    expect(screen.queryByRole('button', { name: 'Rewind to here' })).toBeNull()
  })

  it('reads and drives nothing on a peer a release apart', async () => {
    mocks.instancesCapabilities.mockResolvedValue({ version_match: false, version: '0.6.0', local_version: '0.9.0' })
    renderWindow()
    expect(await screen.findByText(/0\.6\.0/)).toBeTruthy()
    expect(mocks.crewPeerGet).not.toHaveBeenCalled()
    expect(FakeEventSource.all).toHaveLength(0)
    expect(screen.getByRole('textbox', { name: 'Message the agent on devbox…' })).toBeDisabled()
  })

  it('keeps an unsent draft across switching to another crew session and back', async () => {
    const view = renderWindow()
    const box = await screen.findByRole('textbox', { name: 'Message the agent on devbox…' })
    fireEvent.change(box, { target: { value: 'half written' } })
    view.unmount()
    renderWindow()
    expect(await screen.findByRole('textbox', { name: 'Message the agent on devbox…' })).toHaveValue('half written')
  })

  it('merges a failed send back beside text typed meanwhile', async () => {
    let fail: (e: unknown) => void = () => {}
    mocks.crewPeerPost.mockReturnValue(new Promise((_r, rej) => { fail = rej }))
    renderWindow()
    const box = await screen.findByRole('textbox', { name: 'Message the agent on devbox…' })
    fireEvent.change(box, { target: { value: 'first' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    fireEvent.change(box, { target: { value: 'second' } })
    await act(async () => { fail(new Error('peer refused')) })
    await waitFor(() => expect((box as HTMLTextAreaElement).value).toContain('first'))
    expect((box as HTMLTextAreaElement).value).toContain('second')
  })

  it('keeps a failed send even when the window closed while it was in flight', async () => {
    let fail: (e: unknown) => void = () => {}
    mocks.crewPeerPost.mockReturnValue(new Promise((_r, rej) => { fail = rej }))
    const view = renderWindow()
    const box = await screen.findByRole('textbox', { name: 'Message the agent on devbox…' })
    fireEvent.change(box, { target: { value: 'do not lose me' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    await waitFor(() => expect(mocks.crewPeerPost).toHaveBeenCalled())
    view.unmount()
    await act(async () => { fail(new Error('peer refused')) })
    renderWindow()
    expect(await screen.findByRole('textbox', { name: 'Message the agent on devbox…' })).toHaveValue('do not lose me')
  })

  it('shows a failed send recovered after the window was reopened', async () => {
    let fail: (e: unknown) => void = () => {}
    mocks.crewPeerPost.mockReturnValue(new Promise((_r, rej) => { fail = rej }))
    const first = renderWindow()
    const box = await screen.findByRole('textbox', { name: 'Message the agent on devbox…' })
    fireEvent.change(box, { target: { value: 'late failure' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    await waitFor(() => expect(mocks.crewPeerPost).toHaveBeenCalled())
    first.unmount()
    renderWindow()
    const reopened = await screen.findByRole('textbox', { name: 'Message the agent on devbox…' })
    expect(reopened).toHaveValue('')
    await act(async () => { fail(new Error('peer refused')) })
    await waitFor(() => expect(reopened).toHaveValue('late failure'))
  })

  it('keeps the unsent draft through a rewind that is cancelled or sent', async () => {
    renderWindow()
    const box = await screen.findByRole('textbox', { name: 'Message the agent on devbox…' })
    fireEvent.change(box, { target: { value: 'my own draft' } })
    fireEvent.click(screen.getByRole('button', { name: 'Rewind to here' }))
    expect(box).toHaveValue('hi')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(box).toHaveValue('my own draft')
    fireEvent.click(screen.getByRole('button', { name: 'Rewind to here' }))
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(posted().some(p => p[0] === 'api/chat/slots/k1/rewind')).toBe(true))
    expect(box).toHaveValue('my own draft')
  })

  it('restores the ordinary draft on Cancel after a rejected rewind', async () => {
    renderWindow()
    const box = await screen.findByRole('textbox', { name: 'Message the agent on devbox…' })
    fireEvent.change(box, { target: { value: 'my own draft' } })
    fireEvent.click(screen.getByRole('button', { name: 'Rewind to here' }))
    mocks.crewPeerPost.mockRejectedValueOnce(new Error('peer refused'))
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(box).toHaveValue('hi'))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(box).toHaveValue('my own draft')
  })

  it('keeps the persisted draft when the window closes mid-rewind', async () => {
    const view = renderWindow()
    const box = await screen.findByRole('textbox', { name: 'Message the agent on devbox…' })
    fireEvent.change(box, { target: { value: 'my own draft' } })
    fireEvent.click(screen.getByRole('button', { name: 'Rewind to here' }))
    expect(box).toHaveValue('hi')
    view.unmount()
    renderWindow()
    expect(await screen.findByRole('textbox', { name: 'Message the agent on devbox…' })).toHaveValue('my own draft')
  })

  it('stays usable for a connected crew whose pane was evicted from the warm set', async () => {
    mocks.listInstances.mockResolvedValue({ instances: [{ id: 'cd-1', name: 'devbox', status: { state: 'connected' } }] })
    const store = createTestStore({ instances: { warm: {} } as unknown as RootState['instances'] })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
    render(<QueryClientProvider client={qc}><Provider store={store}><Host /></Provider></QueryClientProvider>)
    const box = await screen.findByRole('textbox', { name: 'Message the agent on devbox…' })
    await waitFor(() => expect(box).not.toBeDisabled())
  })

  it('offers no new rewind while one is still in flight', async () => {
    mocks.crewPeerPost.mockReturnValue(new Promise(() => {}))
    renderWindow()
    fireEvent.click(await screen.findByRole('button', { name: 'Rewind to here' }))
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Rewind to here' })).toBeDisabled())
  })

  it('holds the peer event feed only while the window is open', async () => {
    renderWindow()
    await screen.findByTestId('crew-chat-window')
    await waitFor(() => expect(FakeEventSource.all.map(e => e.url)).toEqual(['/api/instances/cd-1/proxy/api/stream']))
    fireEvent.click(screen.getByRole('button', { name: 'Close crew chat' }))
    await screen.findByTestId('no-window')
    expect(FakeEventSource.all[0].close).toHaveBeenCalled()
  })
})

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
import { render, screen, fireEvent, waitFor, act, within } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import type { RootState } from '../store'

const mocks = vi.hoisted(() => ({
  crewPeerGet: vi.fn(), crewPeerPost: vi.fn(), listInstances: vi.fn(), instancesCapabilities: vi.fn(),
  // The LOCAL slot routes. A crew window must never reach them: on a remote
  // slot they are what answered 409.
  continueSlot: vi.fn(), regenerateSlot: vi.fn(), rewind: vi.fn(), approveChatSlot: vi.fn(),
  chatSlotModel: vi.fn(), chatSlotAgent: vi.fn(),
}))
// Every other client call (the shared composer's own reads) answers empty.
vi.mock('../api/client', async () => ({
  ApiError: (await vi.importActual<typeof import('../api/client')>('../api/client')).ApiError,
  api: new Proxy(mocks as Record<string | symbol, unknown>, {
    get: (t, prop) => {
      if (!(prop in t)) t[prop] = vi.fn().mockResolvedValue(prop === 'slashCommands' ? [] : {})
      return t[prop]
    },
  }),
  SEARCH_MIN_CHARS: 2,
}))

import CrewChatWindow from '../pages/chat/crew-window/CrewChatWindow'
import { createCrewWindowRenderers, PEER_SAFE_ROWS } from '../pages/chat/crew-window/crewWindowRenderers'
import { createTranscriptRenderers } from '../pages/chat/transcriptRenderers'
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

/** The user bubble's More menu (Quote holds the row seat, as locally). */
async function openMore(row = 0) {
  await waitFor(() => expect(screen.getAllByTestId('user-more-actions').length).toBeGreaterThan(row), PEER_ROW_WAIT)
  fireEvent.pointerDown(screen.getAllByTestId('user-more-actions')[row], { button: 0, ctrlKey: false, pointerType: 'mouse' })
}
async function openEdit(row = 0) {
  await openMore(row)
  fireEvent.click(await screen.findByRole('menuitem', { name: 'Edit & Resend' }))
}
/** Whether the first user row offers Edit & Resend. */
async function editOffered() {
  await openMore()
  const offered = !!screen.queryByRole('menuitem', { name: 'Edit & Resend' })
  fireEvent.keyDown(document.activeElement || document.body, { key: 'Escape' })
  return offered
}

/** Edit & Resend on the user bubble, the way the local chat rewinds: the
 *  bubble's own editor, submitted with Enter. */
async function editAndResend(text: string, row = 0) {
  await openEdit(row)
  const editor = screen.getByRole('textbox', { name: 'Edit message' })
  fireEvent.change(editor, { target: { value: text } })
  fireEvent.keyDown(editor, { key: 'Enter' })
}

/** A peer row lands only after two chained queries (capabilities, then the
 *  slot detail), so its waits get an explicit deadline, not the default. */
const PEER_ROW_WAIT = { timeout: 5000 }

/** A pending native approval row as the peer's slot detail sends it: the
 *  runner's request data in `cls` (parsed into `meta` by the peer's
 *  `_prepare_messages`), plus the row's own `mid`, which that serializer
 *  carries over from the stored row because the approval is bound to it. */
const permissionRow = (id: string, mid = 'm-' + id) => {
  const cls = JSON.stringify({ request_id: id, approval_id: id, tool_call_id: 'tc', tool_input: 'ls' })
  return { role: 'permission', content: 'shell', cls, ts: 'tp', meta: { ...JSON.parse(cls), mid } }
}

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
    detail = { running: true, messages: [{ role: 'user', content: 'hi', ts: 't1' }, permissionRow('7')] }
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
    detail = { running: true, messages: [{ role: 'user', content: 'hi', ts: 't1' }, permissionRow('7')] }
    renderWindow()
    await screen.findByRole('button', { name: 'Approve' })
    fireEvent.click(screen.getByRole('button', { name: 'Stop generation' }))
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
    expect(await screen.findByRole('status', { name: 'Thinking…' })).toBeInTheDocument()
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
    fireEvent.click(await screen.findByRole('button', { name: 'Regenerate response' }, PEER_ROW_WAIT))
    await waitFor(() => expect(posted()).toContainEqual(['api/chat/slots/k1/regenerate', undefined]))
    expect(mocks.regenerateSlot).not.toHaveBeenCalled()
  })

  it('rewinds on the PEER with the edited text', async () => {
    renderWindow()
    await editAndResend('hi again')
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
    fireEvent.click(await screen.findByRole('button', { name: 'Stop generation' }))
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

  it('retries a second refused rewind from ITS row, not an earlier refused one', async () => {
    detail = { running: false, messages: [
      { role: 'user', content: 'first', ts: 't1' }, { role: 'assistant', content: 'a1', ts: 't2' },
      { role: 'user', content: 'second', ts: 't3' }, { role: 'assistant', content: 'a2', ts: 't4' },
    ] }
    renderWindow()
    const edit = (row: number, text: string) => editAndResend(text, row)
    const box = await screen.findByRole('textbox', { name: 'Message the agent on devbox…' })
    mocks.crewPeerPost.mockRejectedValueOnce(new Error('peer refused'))
    await edit(0, 'first edit')
    await waitFor(() => expect(box).toHaveValue('first edit'))
    mocks.crewPeerPost.mockRejectedValueOnce(new Error('peer refused'))
    await edit(1, 'second edit')
    await waitFor(() => expect(box).toHaveValue('second edit'))
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(posted().filter(c => c[0] === 'api/chat/slots/k1/rewind')).toHaveLength(3))
    expect(posted().filter(c => c[0] === 'api/chat/slots/k1/rewind').pop()).toEqual(['api/chat/slots/k1/rewind', { ts: 't3', content: 'second edit' }])
  })

  it('holds a rewind edit while the peer turn runs, and sends it once idle', async () => {
    renderWindow()
    mocks.crewPeerPost.mockRejectedValueOnce(new Error('peer refused'))
    await editAndResend('hi again')
    const box = screen.getByRole('textbox', { name: 'Message the agent on devbox…' })
    await waitFor(() => expect(box).toHaveValue('hi again'))
    const es = FakeEventSource.all[0]
    act(() => es.emit('slots', [{ ...slotRow, running: true }]))
    fireEvent.keyDown(box, { key: 'Enter' })
    expect(posted().filter(c => c[0] === 'api/chat/slots/k1/rewind')).toHaveLength(1)
    act(() => es.emit('slots', [{ ...slotRow, running: false }]))
    fireEvent.keyDown(box, { key: 'Enter' })
    await waitFor(() => expect(posted().filter(c => c[0] === 'api/chat/slots/k1/rewind')).toHaveLength(2))
  })

  it('sends no edit submitted while another action is in flight, and keeps it in the editor', async () => {
    renderWindow()
    await openEdit()
    mocks.crewPeerPost.mockReturnValueOnce(new Promise(() => {}))
    const box = screen.getByRole('textbox', { name: 'Message the agent on devbox…' })
    fireEvent.change(box, { target: { value: 'next' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    await waitFor(() => expect(posted()).toContainEqual(['api/chat?ws=1', { message: 'next', slot: 'k1' }]))
    const editor = screen.getByRole('textbox', { name: 'Edit message' })
    fireEvent.change(editor, { target: { value: 'too soon' } })
    fireEvent.keyDown(editor, { key: 'Enter' })
    expect(posted().some(c => c[0] === 'api/chat/slots/k1/rewind')).toBe(false)
    expect(screen.queryByText(/Rewinding/)).toBeNull()
    // The editor stays open with the edit, so nothing typed is lost.
    expect(screen.getByRole('textbox', { name: 'Edit message' })).toHaveValue('too soon')
  })

  it('keeps a rewind refused after the window closed in the saved draft', async () => {
    let refuse: (e: Error) => void = () => {}
    mocks.crewPeerPost.mockReturnValueOnce(new Promise((_r, rej) => { refuse = rej }))
    const view = renderWindow()
    await editAndResend('hi again')
    await waitFor(() => expect(posted()).toContainEqual(['api/chat/slots/k1/rewind', { ts: 't1', content: 'hi again' }]))
    view.unmount()
    await act(async () => { refuse(new Error('peer refused')) })
    renderWindow()
    expect(await screen.findByRole('textbox', { name: 'Message the agent on devbox…' })).toHaveValue('hi again')
  })

  it('keeps the rewind target after a failed rewind, so Send retries the rewind', async () => {
    renderWindow()
    mocks.crewPeerPost.mockRejectedValueOnce(new Error('peer refused'))
    await editAndResend('hi again')
    await waitFor(() => expect(screen.getByRole('textbox', { name: 'Message the agent on devbox…' })).toHaveValue('hi again'))
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(posted().filter(p => p[0] === 'api/chat/slots/k1/rewind')).toHaveLength(2))
    expect(posted().some(p => p[0] === 'api/chat?ws=1')).toBe(false)
  })

  it('shows the approval, not a second running label, while one is pending', async () => {
    slotRow = { ...slotRow, running: true, pending_approval_info: { origin: 'native', request_id: '7', request_mid: 'm-7', tool: 'shell' } }
    detail = { running: true, messages: [{ role: 'user', content: 'hi', ts: 't1' }, permissionRow('7')] }
    renderWindow()
    await screen.findByRole('button', { name: 'Approve' })
    expect(screen.queryByRole('status', { name: 'Thinking…' })).toBeNull()
  })

  it('offers no rewind on a message whose paste the hub redacted', async () => {
    detail = { running: false, messages: [{ role: 'user', content: 'see ⌜🗒 Pasted 1 line⌟', ts: 't1', meta: { pastes: [{ id: 1, text: 'key [REDACTED:aws-access-key]', lines: 1 }] } }, { role: 'assistant', content: 'ok', ts: 't2' }] }
    renderWindow()
    await screen.findByText('ok', {}, PEER_ROW_WAIT)
    expect(await editOffered()).toBe(false)
  })

  it('offers no rewind on a message the hub redacted', async () => {
    detail = { running: false, messages: [{ role: 'user', content: 'key [REDACTED:aws-access-key]', ts: 't1' }, { role: 'assistant', content: 'ok', ts: 't2' }] }
    renderWindow()
    await screen.findByText('ok')
    expect(await editOffered()).toBe(false)
  })

  it('reads and drives nothing on a peer a release apart', async () => {
    mocks.instancesCapabilities.mockResolvedValue({ version_match: false, version: '0.6.0', local_version: '0.9.0' })
    renderWindow()
    expect(await screen.findByText(/0\.6\.0/)).toBeTruthy()
    expect(mocks.crewPeerGet).not.toHaveBeenCalled()
    expect(FakeEventSource.all).toHaveLength(0)
    expect(screen.getByRole('textbox', { name: 'Message the agent on devbox…' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
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
    await openEdit()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(box).toHaveValue('my own draft')
    await editAndResend('hi again')
    await waitFor(() => expect(posted().some(p => p[0] === 'api/chat/slots/k1/rewind')).toBe(true))
    expect(box).toHaveValue('my own draft')
  })

  it('restores the ordinary draft on Cancel after a rejected rewind', async () => {
    renderWindow()
    const box = await screen.findByRole('textbox', { name: 'Message the agent on devbox…' })
    fireEvent.change(box, { target: { value: 'my own draft' } })
    mocks.crewPeerPost.mockRejectedValueOnce(new Error('peer refused'))
    await editAndResend('hi again')
    await waitFor(() => expect(box).toHaveValue('hi again'))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(box).toHaveValue('my own draft')
  })

  it('keeps the persisted draft when the window closes mid-rewind', async () => {
    const view = renderWindow()
    const box = await screen.findByRole('textbox', { name: 'Message the agent on devbox…' })
    fireEvent.change(box, { target: { value: 'my own draft' } })
    mocks.crewPeerPost.mockRejectedValueOnce(new Error('peer refused'))
    await editAndResend('hi again')
    await waitFor(() => expect(box).toHaveValue('hi again'))
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
    await editAndResend('hi again')
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Regenerate response' })).toBeNull())
    expect(await editOffered()).toBe(false)
  })

  // The rows below are the shared transcript's, not a copy kept here.
  it('draws a peer tool call as the shared tool line, not its raw text', async () => {
    slotRow = { ...slotRow, running: true }
    detail = { running: true, messages: [
      { role: 'user', content: 'list it', ts: 't1' },
      { role: 'tool', content: '🔧 Running: ls -la', cls: '', ts: 't2', meta: { tool_call_id: 'c1', tool_input: 'ls -la' } },
    ] }
    renderWindow()
    expect(await screen.findByTestId('tool-pill-label', undefined, PEER_ROW_WAIT)).toBeInTheDocument()
    expect(screen.queryByText('🔧 Running: ls -la')).toBeNull()
  })

  it('draws a peer file as its name, never as the hub outbox file of that name', async () => {
    detail = { running: false, messages: [
      { role: 'user', content: 'send it', ts: 't1' },
      { role: 'file', content: JSON.stringify({ filename: 'report.pdf', mime: 'application/pdf' }), cls: '', ts: 't2' },
    ] }
    const { container } = renderWindow()
    expect(await screen.findByTestId('crew-window-file', undefined, PEER_ROW_WAIT)).toHaveTextContent('The agent on devbox sent report.pdf. The file stays on devbox.')
    expect(container.querySelector('[href*="/api/outbox/"], [src*="/api/outbox/"]')).toBeNull()
  })

  it('answers only the approval the peer itself reports pending', async () => {
    // The transcript row names request 9; the peer's slot says 7 is the live one.
    slotRow = { ...slotRow, running: true, pending_approval_info: { origin: 'native', request_id: '7', request_mid: 'm-7', tool: 'shell' } }
    detail = { running: true, messages: [{ role: 'user', content: 'hi', ts: 't1' }, permissionRow('9')] }
    renderWindow()
    fireEvent.click(await screen.findByRole('button', { name: 'Approve' }, PEER_ROW_WAIT))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Approve' })).not.toBeDisabled(), PEER_ROW_WAIT)
    expect(mocks.crewPeerPost).not.toHaveBeenCalled()
  })

  it('answers nothing for a row from an older peer that sends no mid', async () => {
    slotRow = { ...slotRow, running: true, pending_approval_info: { origin: 'native', request_id: '7', request_mid: 'm-7', tool: 'shell' } }
    const { mid: _dropped, ...meta } = permissionRow('7').meta
    const row = { ...permissionRow('7'), meta }
    detail = { running: true, messages: [{ role: 'user', content: 'hi', ts: 't1' }, row] }
    renderWindow()
    fireEvent.click(await screen.findByRole('button', { name: 'Approve' }, PEER_ROW_WAIT))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Approve' })).not.toBeDisabled(), PEER_ROW_WAIT)
    expect(mocks.crewPeerPost).not.toHaveBeenCalled()
  })

  it('answers no stale card whose request id the peer has reused', async () => {
    // The card on screen is the OLD request 7 (row m-old); the peer's live 7 is m-new.
    slotRow = { ...slotRow, running: true, pending_approval_info: { origin: 'native', request_id: '7', request_mid: 'm-new', tool: 'shell' } }
    detail = { running: true, messages: [{ role: 'user', content: 'hi', ts: 't1' }, permissionRow('7', 'm-old')] }
    renderWindow()
    fireEvent.click(await screen.findByRole('button', { name: 'Approve' }, PEER_ROW_WAIT))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Approve' })).not.toBeDisabled(), PEER_ROW_WAIT)
    expect(mocks.crewPeerPost).not.toHaveBeenCalled()
  })

  it('says what Approve and Reject do while an approval is pending', async () => {
    slotRow = { ...slotRow, running: true, pending_approval_info: { origin: 'native', request_id: '7', request_mid: 'm-7', tool: 'shell' } }
    detail = { running: true, messages: [{ role: 'user', content: 'hi', ts: 't1' }, permissionRow('7')] }
    renderWindow()
    expect(await screen.findByTestId('crew-window-approval-hint', undefined, PEER_ROW_WAIT)).toHaveTextContent('Approve runs this one command.')
  })

  it('lets in only shared rows decided safe for a peer (a new row falls back)', () => {
    // A new shared row must be added to PEER_SAFE_ROWS on purpose, or it draws
    // through the store-free default instead of reading this hub's own state.
    const ids = createTranscriptRenderers({}).map(r => r.id)
    // A guide offer row reads the local guide store, which a peer's window has none of.
    const decided = new Set([...PEER_SAFE_ROWS, 'workflow_run_tool', 'subagent_run_tool', 'file', 'conversation_card'])
    expect(ids.filter(id => !decided.has(id))).toEqual([])
    const crew = createCrewWindowRenderers({ instanceId: 'cd-1', key: 'k1', name: 'devbox', canRewind: () => false, onRewind: () => {}, rewindDisabled: false }).map(r => r.id)
    expect(crew).not.toContain('workflow_run_tool')
    expect(crew).not.toContain('subagent_run_tool')
    expect(crew).not.toContain('conversation_card')
  })

  it('draws a peer code fence copy-only, with no Run into this machine', async () => {
    detail = { running: false, messages: [
      { role: 'user', content: 'clean up', ts: 't1' },
      { role: 'assistant', content: '```bash\nrm -f report.txt\n```', cls: '', ts: 't2', meta: { decisions_strip: { turn_id: 'peer-turn', points: [] } } },
    ] }
    renderWindow()
    await waitFor(() => expect(screen.getByTestId('crew-window-assistant')).toHaveTextContent('rm -f report.txt'), PEER_ROW_WAIT)
    expect(screen.queryByRole('button', { name: 'Edit code block' })).toBeNull()
    expect(screen.queryByLabelText(/Run in terminal/)).toBeNull()
  })

  it('draws no decision strip (no verdict thumbs) and no file chips on a peer reply', async () => {
    // A record the strip draws with no consent read: on the shared reply it
    // would carry the thumbs that write THIS gateway's decision records.
    const strip = { turn_id: 'peer-turn', ts: 't', point: 'skills.select', baseline: ['a'], jev: ['a'], agree: true, p: 0.9, tokens_saved: 0, candidates: 1, message_chars: 1, history_chars: 1, latency_ms: 5, dropped: [], error: null }
    detail = { running: false, messages: [
      { role: 'user', content: 'go', ts: 't1' },
      { role: 'assistant', content: 'done', cls: '', ts: 't2', decisions_strip: strip, meta: { decisions_strip: strip, file_changes: [{ path: '/srv/peer/report.txt', before: 'a', after: 'b' }] } },
    ] }
    renderWindow()
    await screen.findByText('done', {}, PEER_ROW_WAIT)
    expect(document.querySelector('[data-testid^="decision-strip"]')).toBeNull()
    // Nor the file-change chips, which would open THIS machine's file.
    expect(screen.queryByText('report.txt')).toBeNull()
  })

  it('draws the replies as the local chat\'s bubbles, actions on their own hover row', async () => {
    renderWindow()
    const reply = await screen.findByTestId('crew-window-assistant', {}, PEER_ROW_WAIT)
    // The shared reply's row: Copy and Regenerate sit in the bubble's footer.
    expect(within(reply).getByRole('button', { name: 'Regenerate response' })).toBeTruthy()
    // No loose window-level buttons under the transcript any more.
    expect(screen.queryByRole('button', { name: 'Regenerate' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Rewind to here' })).toBeNull()
  })

  it('wires the composer\'s model + effort picker to the PEER slot', async () => {
    slotRow = { ...slotRow, model: 'claude-opus', reasoning_effort: 'high' }
    mocks.instancesCapabilities.mockResolvedValue({
      version_match: true, version: '0.9.0', local_version: '0.9.0', unavailable: {}, effort_levels: ['low', 'high'],
      models: [{ model_name: 'claude-opus', display_name: '', description: '', context_window: 0 }, { model_name: 'claude-sonnet', display_name: '', description: '', context_window: 0 }],
    })
    renderWindow()
    await waitFor(() => expect(screen.getByTestId('composer-model-chip')).toHaveTextContent('claude-opus'), PEER_ROW_WAIT)
    fireEvent.click(screen.getByTestId('composer-model-chip'))
    fireEvent.click(await screen.findByRole('option', { name: /claude-sonnet/ }))
    await waitFor(() => expect(posted()).toContainEqual(['api/chat/slots/k1/model', { model: 'claude-sonnet' }]))
    expect(mocks.chatSlotModel).not.toHaveBeenCalled()
  })

  it('quotes a peer reply into the next send, as the local chat does', async () => {
    renderWindow()
    const reply = await screen.findByTestId('crew-window-assistant', {}, PEER_ROW_WAIT)
    fireEvent.pointerDown(within(reply).getByRole('button', { name: /more actions/i }), { button: 0, ctrlKey: false, pointerType: 'mouse' })
    fireEvent.click(await screen.findByRole('menuitem', { name: /quote/i }))
    const box = screen.getByRole('textbox', { name: 'Message the agent on devbox…' })
    fireEvent.change(box, { target: { value: 'why?' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    await waitFor(() => expect(posted().find(c => c[0] === 'api/chat?ws=1')).toBeTruthy())
    const body = posted().find(c => c[0] === 'api/chat?ws=1')![1] as { message: string; meta?: { quote?: { text: string } } }
    expect(body.message).toMatch(/^> hello[\s\S]*why\?$/)
    expect(body.meta?.quote?.text).toBe('hello')
  })

  it('keeps a refused quoted send quote in the draft when the window closed meanwhile', async () => {
    const view = renderWindow()
    const reply = await screen.findByTestId('crew-window-assistant', {}, PEER_ROW_WAIT)
    fireEvent.pointerDown(within(reply).getByRole('button', { name: /more actions/i }), { button: 0, ctrlKey: false, pointerType: 'mouse' })
    fireEvent.click(await screen.findByRole('menuitem', { name: /quote/i }))
    let refuse: (e: Error) => void = () => {}
    mocks.crewPeerPost.mockReturnValueOnce(new Promise((_r, rej) => { refuse = rej }))
    const box = screen.getByRole('textbox', { name: 'Message the agent on devbox…' })
    fireEvent.change(box, { target: { value: 'why?' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    await waitFor(() => expect(posted().some(c => c[0] === 'api/chat?ws=1')).toBe(true))
    view.unmount()
    await act(async () => { refuse(new Error('peer refused')) })
    renderWindow()
    const reopened = await screen.findByRole('textbox', { name: 'Message the agent on devbox…' })
    await waitFor(() => expect((reopened as HTMLTextAreaElement).value).toMatch(/^> hello[\s\S]*why\?$/))
  })

  it('puts the quote back on the stage when a quoted send is refused', async () => {
    renderWindow()
    const reply = await screen.findByTestId('crew-window-assistant', {}, PEER_ROW_WAIT)
    fireEvent.pointerDown(within(reply).getByRole('button', { name: /more actions/i }), { button: 0, ctrlKey: false, pointerType: 'mouse' })
    fireEvent.click(await screen.findByRole('menuitem', { name: /quote/i }))
    const box = screen.getByRole('textbox', { name: 'Message the agent on devbox…' })
    mocks.crewPeerPost.mockRejectedValueOnce(new Error('peer refused'))
    fireEvent.change(box, { target: { value: 'why?' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    await waitFor(() => expect(box).toHaveValue('why?'))
    fireEvent.keyDown(box, { key: 'Enter' })
    await waitFor(() => expect(posted().filter(c => c[0] === 'api/chat?ws=1')).toHaveLength(2))
    const retry = posted().filter(c => c[0] === 'api/chat?ws=1')[1][1] as { meta?: { quote?: { text: string } } }
    expect(retry.meta?.quote?.text).toBe('hello')
  })

  it('asks the peer again after a failed capability read, and shows effort once it answers', async () => {
    slotRow = { ...slotRow, model: 'claude-opus', reasoning_effort: 'high' }
    let fail = true
    mocks.crewPeerGet.mockImplementation((_id: string, path: string) => (
      path === 'api/chat/slots' ? Promise.resolve([slotRow])
        : path.endsWith('/selection-capabilities') ? (fail ? Promise.reject(new Error('peer unreachable')) : Promise.resolve({ known: true, effort_supported: true, effort_levels: ['low', 'high'] }))
          : Promise.resolve(detail)))
    renderWindow()
    await waitFor(() => expect(screen.getByTestId('composer-model-chip')).toHaveTextContent('claude-opus'), PEER_ROW_WAIT)
    expect(screen.getByTestId('composer-model-chip')).not.toHaveTextContent('High')
    fail = false
    await waitFor(() => expect(screen.getByTestId('composer-model-chip')).toHaveTextContent('High'), { timeout: 8000 })
  }, 15000)

  it('draws a peer reply carrying every hub record with only its own row actions', async () => {
    const strip = { turn_id: 'p', ts: 't', point: 'skills.select', baseline: ['a'], jev: ['a'], agree: true, p: 0.9, tokens_saved: 0, candidates: 1, message_chars: 1, history_chars: 1, latency_ms: 5, dropped: [], error: null }
    detail = { running: false, messages: [
      { role: 'user', content: 'hi', ts: 't1' },
      { role: 'assistant', content: 'a reply long enough to have a raw view', ts: 't2', decisions_strip: strip, meta: {
        mid: 'm2', decisions_strip: strip, file_changes: [{ path: '/srv/peer/a.txt', before: 'a', after: 'b' }], file_changes_omitted_files: ['/srv/peer/b.txt'],
        turn_stats: { elapsed_ms: 1000, model: 'claude-opus' }, blocked_links: [], redactions: [],
      } },
    ] }
    renderWindow()
    const reply = await screen.findByTestId('crew-window-assistant', {}, PEER_ROW_WAIT)
    const labels = within(reply).getAllByRole('button').map(b => b.getAttribute('aria-label') || b.textContent)
    // Every control on a peer reply is one of the local reply row's own.
    for (const label of labels) expect(['Regenerate response', 'Copy', 'More actions']).toContain(label)
    expect(document.querySelector('[data-testid^="decision-strip"]')).toBeNull()
    expect(screen.queryByText('a.txt')).toBeNull()
  })

  it('asks the peer again while its session does not know its effort levels yet', async () => {
    slotRow = { ...slotRow, model: 'claude-opus', reasoning_effort: 'high' }
    let known = false
    mocks.crewPeerGet.mockImplementation((_id: string, path: string) => Promise.resolve(
      path === 'api/chat/slots' ? [slotRow]
        : path.endsWith('/selection-capabilities') ? (known ? { known: true, effort_supported: true, effort_levels: ['low', 'high'] } : { known: false })
          : detail))
    renderWindow()
    await waitFor(() => expect(screen.getByTestId('composer-model-chip')).toHaveTextContent('claude-opus'), PEER_ROW_WAIT)
    expect(screen.getByTestId('composer-model-chip')).not.toHaveTextContent('High')
    known = true
    await waitFor(() => expect(screen.getByTestId('composer-model-chip')).toHaveTextContent('High'), { timeout: 8000 })
  }, 15000)

  it('offers Regenerate on the newest reply even when a compaction notice follows it', async () => {
    detail = { running: false, messages: [
      { role: 'user', content: 'hi', ts: 't1' }, { role: 'assistant', content: 'hello', ts: 't2' },
      { role: 'assistant', content: 'SUMMARY', cls: '', ts: 't3', meta: { kind: 'compaction' } },
    ] }
    renderWindow()
    const reply = await screen.findByTestId('crew-window-assistant', {}, PEER_ROW_WAIT)
    expect(within(reply).getByRole('button', { name: 'Regenerate response' })).toBeTruthy()
  })

  it('shows a refused effort pick on the window itself', async () => {
    slotRow = { ...slotRow, model: 'claude-opus', reasoning_effort: 'high' }
    mocks.crewPeerGet.mockImplementation((_id: string, path: string) => Promise.resolve(
      path === 'api/chat/slots' ? [slotRow]
        : path.endsWith('/selection-capabilities') ? { known: true, effort_supported: true, effort_levels: ['low', 'high'] }
          : detail))
    mocks.instancesCapabilities.mockResolvedValue({
      version_match: true, version: '0.9.0', local_version: '0.9.0', unavailable: {}, effort_levels: ['low', 'high'],
      models: [{ model_name: 'claude-opus', display_name: '', description: '', context_window: 0 }],
    })
    mocks.crewPeerPost.mockRejectedValueOnce(new Error('effort_overlay_busy'))
    renderWindow()
    await waitFor(() => expect(screen.getByTestId('composer-model-chip')).toHaveTextContent('High'), PEER_ROW_WAIT)
    fireEvent.click(screen.getByTestId('composer-model-chip'))
    fireEvent.click(await screen.findByRole('switch', { name: 'Use default effort' }))
    await waitFor(() => expect(posted()).toContainEqual(['api/chat/slots/k1/reasoning-effort', { reasoning_effort: '' }]))
    expect(await screen.findByText(/effort_overlay_busy/)).toBeTruthy()
  })

  it('hides the composer controls that would act on this machine', async () => {
    slotRow = { ...slotRow, agent: 'builder' }
    renderWindow()
    await screen.findByRole('textbox', { name: 'Message the agent on devbox…' })
    // Attach (a hub upload), the agent picker (the hub's roster) and the
    // approval-mode picker (the hub's mode) are not drawn at all.
    expect(screen.queryByRole('button', { name: /attach|add files/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /^Agent:/ })).toBeNull()
    expect(screen.queryByRole('button', { name: /approval mode/i })).toBeNull()
  })

  it('draws a code fence in a peer USER row copy-only too', async () => {
    detail = { running: false, messages: [
      { role: 'user', content: 'run this\n```bash\nrm -f report.txt\n```', ts: 't1' },
      { role: 'assistant', content: 'ok', cls: '', ts: 't2' },
    ] }
    renderWindow()
    await screen.findByText('ok')
    await waitFor(() => expect(document.body.textContent).toContain('rm -f report.txt'), PEER_ROW_WAIT)
    expect(screen.queryByRole('button', { name: 'Edit code block' })).toBeNull()
    expect(screen.queryByLabelText(/Run in terminal/)).toBeNull()
  })

  it('still draws a peer system notice as its card, not as a reply', async () => {
    detail = { running: false, messages: [
      { role: 'user', content: 'hi', ts: 't1' },
      { role: 'assistant', content: 'LONG CONTEXT SUMMARY', cls: '', ts: 't2', meta: { kind: 'compaction' } },
    ] }
    renderWindow()
    await screen.findByText('hi')
    expect(screen.queryByTestId('crew-window-assistant')).toBeNull()
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

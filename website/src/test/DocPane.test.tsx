/**
 * The doc pane: loads a doc's markdown into the editor, saves it a moment after an
 * edit naming the version it read, reports an edit made elsewhere instead of
 * overwriting it, and sends a highlight's question to the doc slot's conductor.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { EditorView } from '@codemirror/view'
import { MemoryRouter } from 'react-router-dom'

vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  api: { getDoc: vi.fn(), saveDoc: vi.fn(), resolveApproval: vi.fn(), artifact: vi.fn(() => Promise.reject(new Error('404'))) },
}))
vi.mock('../chat-core/transport/sendTurn', () => ({ sendTurn: vi.fn() }))

import { api, ApiError } from '../api/client'
import { sendTurn } from '../chat-core/transport/sendTurn'
import { store } from '../store'
import { replaceMessages, setActiveSlot } from '../store/chatSlice'
import { sseSlots } from '../store/dashboardSlice'
import type { ChatSlot } from '../types'
import DocPane from '../pages/chat/page/DocPane'
import { parseMarkers } from '../pages/chat/page/docConductor'
import { cycleDirection, shortLabel, stepId } from '../pages/chat/page/ConversationBar'

const getDoc = api.getDoc as unknown as ReturnType<typeof vi.fn>
const saveDoc = api.saveDoc as unknown as ReturnType<typeof vi.fn>
const send = sendTurn as unknown as ReturnType<typeof vi.fn>
const DOC = '0123456789abcdef0123456789abcdef'

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const renderPane = () => render(
  <QueryClientProvider client={queryClient}>
    <Provider store={store}><DocPane docId={DOC} slotKey="doc-slot" title="Plan" /></Provider>
  </QueryClientProvider>,
)

/** The live editor once the doc has loaded. */
async function editor(): Promise<EditorView> {
  const host = await screen.findByTestId('doc-editor')
  const view = EditorView.findFromDOM(host.querySelector('.cm-editor') as HTMLElement)
  if (!view) throw new Error('no editor')
  return view
}

/** Apply *change* in the editor, then step past the save debounce. */
async function typeAndSettle(view: EditorView, change: { from: number; to?: number; insert: string }) {
  vi.useFakeTimers()
  act(() => { view.dispatch({ changes: change }) })
  await act(async () => { vi.advanceTimersByTime(600) })
  vi.useRealTimers()
  await act(async () => { await Promise.resolve() })
}

describe('the conversation bar\'s stepping', () => {
  it('steps through "none" between the last and the first', () => {
    expect(stepId(['a', 'b', 'c'], null, 1)).toBe('a')
    expect(stepId(['a', 'b', 'c'], null, -1)).toBe('c')
    expect(stepId(['a', 'b', 'c'], 'b', 1)).toBe('c')
    expect(stepId(['a', 'b', 'c'], 'c', 1)).toBeNull()
    expect(stepId(['a', 'b', 'c'], 'a', -1)).toBeNull()
    expect(stepId([], null, 1)).toBeNull()
  })
  it('only ⌃⌥ with an arrow steps; ⌥ alone moves the caret by word', () => {
    const k = (key: string, mods: Partial<KeyboardEvent>) => cycleDirection({ key, ctrlKey: false, altKey: false, metaKey: false, shiftKey: false, ...mods })
    expect(k('ArrowRight', { ctrlKey: true, altKey: true })).toBe(1)
    expect(k('ArrowLeft', { ctrlKey: true, altKey: true })).toBe(-1)
    expect(k('ArrowRight', { altKey: true })).toBe(0)
    expect(k('ArrowRight', { ctrlKey: true, altKey: true, metaKey: true })).toBe(0)
  })
  it('labels a conversation by the first line of its text, without markup or markers', () => {
    expect(shortLabel('## Pricing tiers\nmore')).toBe('Pricing tiers')
    expect(shortLabel('<!-- ask:x -->\n**Decide** the three tiers before the deadline on Friday', 20)).toBe('Decide the three ti…')
  })
})

describe('DocPane', () => {
  beforeEach(() => {
    getDoc.mockReset()
    saveDoc.mockReset()
    send.mockReset()
    localStorage.clear()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('loads the doc and saves an edit once it settles, naming the version it read', async () => {
    getDoc.mockResolvedValue({ content: '# Plan\n\nfirst line\n', version: 'v41' })
    saveDoc.mockResolvedValue({ content: '', version: 'v42' })
    renderPane()
    const view = await editor()
    expect(view.state.doc.toString()).toBe('# Plan\n\nfirst line\n')
    await typeAndSettle(view, { from: 8, to: 13, insert: 'second' })
    expect(saveDoc).toHaveBeenCalledWith(DOC, '# Plan\n\nsecond line\n', 'v41')
  })

  it('keeps the text and asks which version wins when the file moved', async () => {
    getDoc.mockResolvedValue({ content: 'mine\n', version: 'v1' })
    saveDoc.mockRejectedValueOnce(new ApiError(409, 'conflict', JSON.stringify({ content: 'theirs\n', version: 'v2' })))
    renderPane()
    const view = await editor()
    await typeAndSettle(view, { from: 4, insert: ', edited' })
    expect(await screen.findByText('This doc changed somewhere else')).toBeTruthy()
    expect(view.state.doc.toString()).toBe('mine, edited\n')
    saveDoc.mockResolvedValueOnce({ content: '', version: 'v3' })
    await act(async () => { fireEvent.click(screen.getByText('Keep my version')) })
    expect(saveDoc).toHaveBeenLastCalledWith(DOC, 'mine, edited\n', 'v2')
  })

  it('"Use the other version" loads the file the server holds', async () => {
    getDoc.mockResolvedValue({ content: 'mine\n', version: 'v1' })
    saveDoc.mockRejectedValueOnce(new ApiError(409, 'conflict', JSON.stringify({ content: 'theirs\n', version: 'v2' })))
    renderPane()
    await typeAndSettle(await editor(), { from: 0, insert: 'x' })
    await act(async () => { fireEvent.click(await screen.findByText('Use the other version')) })
    expect((await editor()).state.doc.toString()).toBe('theirs\n')
  })

  it('shows the conductor\'s approval mode, so Trust can be set without a chat composer', async () => {
    getDoc.mockResolvedValue({ content: 'intro\n', version: 'v1' })
    render(
      <MemoryRouter>
        <QueryClientProvider client={queryClient}>
          <Provider store={store}><DocPane docId={DOC} slotKey="doc-slot" title="Plan" approvalMode="trust" /></Provider>
        </QueryClientProvider>
      </MemoryRouter>,
    )
    const holder = await screen.findByTestId('doc-approval-mode')
    expect(holder.querySelector('button[aria-label]')?.getAttribute('aria-label')).toMatch(/Trust/)
  })

  it('the bar lists the conversations in doc order, and ⌃⌥→ / a chip open each in turn', async () => {
    getDoc.mockResolvedValue({ content: 'intro\n<!-- ask:q1 -->Pricing tiers<!-- /ask:q1 -->\nmiddle\n<!-- ask:q2 -->Launch date<!-- /ask:q2 -->\n', version: 'v1' })
    renderPane()
    await editor()
    const chips = await screen.findAllByTestId('conversation-chip')
    expect(chips.map(c => c.textContent)).toEqual(['Pricing tiers', 'Launch date'])
    const answers = () => screen.getAllByTestId('doc-answer').map(a => a.getAttribute('data-open'))
    expect(answers()).toEqual(['false', 'false'])
    const frame = () => act(() => new Promise<void>(r => requestAnimationFrame(() => r())))

    act(() => { fireEvent.keyDown(window, { key: 'ArrowRight', ctrlKey: true, altKey: true }) })
    await frame()
    expect(answers()).toEqual(['true', 'false'])
    expect(document.activeElement).toBe(screen.getAllByTestId('doc-answer-input')[0])
    expect(screen.getAllByTestId('conversation-chip')[0].getAttribute('data-active')).toBe('true')

    // The next one opens, and the one before it closes.
    act(() => { fireEvent.keyDown(document.activeElement!, { key: 'ArrowRight', ctrlKey: true, altKey: true }) })
    await frame()
    expect(answers()).toEqual(['false', 'true'])

    // Past the last one is "none": everything folds and the caret is back in the doc.
    act(() => { fireEvent.keyDown(document.activeElement!, { key: 'ArrowRight', ctrlKey: true, altKey: true }) })
    await frame()
    expect(answers()).toEqual(['false', 'false'])
    expect(screen.getAllByTestId('conversation-chip').map(c => c.getAttribute('data-active'))).toEqual(['false', 'false'])
    expect(document.activeElement?.closest('.cm-editor')).toBeTruthy()

    fireEvent.click(screen.getAllByTestId('conversation-chip')[0])
    await frame()
    expect(answers()).toEqual(['true', 'false'])
    // Clicking the current chip again puts it away.
    fireEvent.click(screen.getAllByTestId('conversation-chip')[0])
    await frame()
    expect(answers()).toEqual(['false', 'false'])
  })

  it('reports a doc it could not open', async () => {
    getDoc.mockRejectedValue(new Error('boom'))
    renderPane()
    expect(await screen.findByText('Could not open this doc')).toBeTruthy()
  })

  it('a highlight\'s question goes to the doc slot with the doc and the marked text', async () => {
    getDoc.mockResolvedValue({ content: 'intro\nwrite the tests\n', version: 'v1' })
    saveDoc.mockResolvedValue({ content: '', version: 'v2' })
    send.mockResolvedValue({ status: 'dispatched' })
    renderPane()
    const view = await editor()
    act(() => { view.dispatch({ selection: { anchor: 6, head: 21 } }) })
    fireEvent.click(await screen.findByTestId('ask-conductor'))
    const box = await screen.findByTestId('ask-input')
    fireEvent.change(box, { target: { value: 'split this into tasks' } })
    await act(async () => { fireEvent.keyDown(box, { key: 'Enter' }) })
    const [marker] = parseMarkers(view.state.doc.toString())
    expect(marker.kind).toBe('ask')
    expect(send).toHaveBeenCalledTimes(1)
    const { message, slot } = send.mock.calls[0][0] as { message: string; slot: string }
    expect(slot).toBe('doc-slot')
    expect(message.startsWith(`[Doc ask ${marker.id}]\nsplit this into tasks\n`)).toBe(true)
    expect(message).toContain('"""\nwrite the tests\n"""')
    expect(message).toContain(`<<<DOC\n${view.state.doc.toString()}\nDOC>>>`)
  })

  it('a worker\'s tool approval is decided on the doc, its conductor\'s page', async () => {
    getDoc.mockResolvedValue({ content: 'plan\n', version: 'v1' })
    act(() => {
      store.dispatch(sseSlots([
        { key: 'doc-slot', title: 'Plan', messages: 0, running: false, crew_mode_root: true },
        {
          key: 'w1', title: 'Research #1', messages: 0, running: false, created_by: 'doc-slot', crew_root: 'doc-slot',
          pending_approval: true,
          pending_approval_info: { origin: 'native', request_id: 'r1', request_mid: 'm1', tool: 'Shell', tool_input: 'gh issue view 1' },
        },
      ] as ChatSlot[]))
    })
    renderPane()
    const cards = await screen.findByTestId('crew-approval-cards')
    expect(cards.textContent).toContain('Research #1')
    expect(cards.textContent).toContain('gh issue view 1')
    act(() => { store.dispatch(sseSlots([])) })
  })

  it('a finished reply\'s write lands in the doc once, and not again after a reload', async () => {
    getDoc.mockResolvedValue({ content: '# Issues\n', version: 'v1' })
    saveDoc.mockResolvedValue({ content: '', version: 'v2' })
    act(() => {
      store.dispatch(setActiveSlot('doc-slot'))
      store.dispatch(replaceMessages([
        { role: 'user', content: '[Doc ask q1]\nwrite them down\n\n---\nrest', cls: '' },
        { role: 'assistant', content: 'Done.\n```doc-append\n| # | Title |\n| --- | --- |\n| 12 | Fix it |\n```', cls: '', meta: { mid: 'r1' } },
      ]))
    })
    const first = renderPane()
    let view = await editor()
    await vi.waitFor(() => expect(view.state.doc.toString()).toBe('# Issues\n\n| # | Title |\n| --- | --- |\n| 12 | Fix it |\n'))
    first.unmount()
    getDoc.mockResolvedValue({ content: view.state.doc.toString(), version: 'v2' })
    renderPane()
    view = await editor()
    await act(async () => { await Promise.resolve() })
    expect(view.state.doc.toString().match(/Fix it/g)).toHaveLength(1)
    act(() => { store.dispatch(replaceMessages([])); store.dispatch(setActiveSlot(null)) })
  })
})

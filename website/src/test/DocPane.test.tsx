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

  it('the conversation bar shows only while ⌃⌥ steps, and fades a moment after; the approval mode holds the corner', async () => {
    getDoc.mockResolvedValue({ content: 'see <!-- ask:q1 -->this<!-- /ask:q1 --> part\n', version: 'v1' })
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <Provider store={store}><DocPane docId={DOC} slotKey="doc-slot" title="Plan" approvalMode="normal" /></Provider>
        </MemoryRouter>
      </QueryClientProvider>,
    )
    await editor()
    expect(screen.getByTestId('doc-approval-mode').className).toContain('bottom-5 left-5')
    expect(screen.queryByTestId('doc-conversation-bar')).toBeNull()
    vi.useFakeTimers()
    try {
      act(() => { fireEvent.keyDown(window, { key: 'ArrowRight', ctrlKey: true, altKey: true }) })
      expect(screen.getByTestId('doc-conversation-bar')).toBeTruthy()
      await act(async () => { vi.advanceTimersByTime(2600) })
    } finally {
      vi.useRealTimers()
    }
    // Gone after its exit, which runs on real timers.
    await vi.waitFor(() => expect(screen.queryByTestId('doc-conversation-bar')).toBeNull())
  })

  it('keeps the page header out of the editor and writes it back with every save', async () => {
    getDoc.mockResolvedValue({ content: '---\nicon: 🎯\n---\n# Plan\n\nfirst line\n', version: 'v41' })
    saveDoc.mockResolvedValue({ content: '', version: 'v42' })
    renderPane()
    const view = await editor()
    expect(view.state.doc.toString()).toBe('# Plan\n\nfirst line\n')
    expect(screen.getByTestId('doc-icon').textContent).toBe('🎯')
    await typeAndSettle(view, { from: 8, to: 13, insert: 'second' })
    expect(saveDoc).toHaveBeenLastCalledWith(DOC, '---\nicon: 🎯\n---\n# Plan\n\nsecond line\n', 'v41')
    // A header change saves too, with the body as it stands.
    vi.useFakeTimers()
    fireEvent.click(screen.getByTestId('doc-add-cover'))
    await act(async () => { vi.advanceTimersByTime(600) })
    vi.useRealTimers()
    await act(async () => { await Promise.resolve() })
    expect(saveDoc.mock.calls.at(-1)?.[1]).toMatch(/^---\nicon: 🎯\ncover: gradient:\d+\n---\n# Plan\n\nsecond line\n$/)
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
    // Hidden until the shortcut is used.
    expect(screen.queryAllByTestId('conversation-chip')).toEqual([])
    const answers = () => screen.getAllByTestId('doc-answer').map(a => a.getAttribute('data-open'))
    expect(answers()).toEqual(['false', 'false'])
    const frame = () => act(() => new Promise<void>(r => requestAnimationFrame(() => r())))

    act(() => { fireEvent.keyDown(window, { key: 'ArrowRight', ctrlKey: true, altKey: true }) })
    await frame()
    expect(screen.getAllByTestId('conversation-chip').map(c => c.textContent)).toEqual(['Pricing tiers', 'Launch date'])
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

  it('Space on an empty line opens the ask card about the block above; Up and Down choose how much; Backspace puts it away', async () => {
    const text = '# Plan\n\nFirst paragraph.\n\nSecond paragraph\nstill second.\n\n'
    getDoc.mockResolvedValue({ content: text, version: 'v1' })
    renderPane()
    const view = await editor()
    const space = () => act(() => { view.contentDOM.dispatchEvent(new KeyboardEvent('keydown', { key: ' ', code: 'Space', bubbles: true, cancelable: true })) })
    // Not on a line with text: Space is just a space there.
    act(() => { view.dispatch({ selection: { anchor: 3 } }) })
    space()
    expect(screen.queryByTestId('space-ask')).toBeNull()
    act(() => { view.dispatch({ selection: { anchor: text.length } }) })
    space()
    const input = await screen.findByTestId('space-ask-input')
    const sel = () => [...view.dom.querySelectorAll('.cm-space-sel')].map(l => l.textContent)
    expect(sel()).toEqual(['Second paragraph', 'still second.'])
    expect(view.state.doc.toString()).toBe(text)
    expect(screen.getByTestId('space-ask-about').textContent).toBe('About the block above')
    act(() => { fireEvent.keyDown(input, { key: 'ArrowUp' }) })
    expect(sel()).toEqual(['First paragraph.', 'Second paragraph', 'still second.'])
    act(() => { fireEvent.keyDown(input, { key: 'ArrowUp' }) })
    act(() => { fireEvent.keyDown(input, { key: 'ArrowUp' }) })
    // Three blocks is all there is.
    expect(screen.getByTestId('space-ask-about').textContent).toBe('About the 3 blocks above')
    for (let i = 0; i < 4; i++) act(() => { fireEvent.keyDown(screen.getByTestId('space-ask-input'), { key: 'ArrowDown' }) })
    expect(sel()).toEqual([])
    expect(screen.getByTestId('space-ask-about').textContent).toMatch(/Nothing selected/)
    act(() => { fireEvent.keyDown(screen.getByTestId('space-ask-input'), { key: 'Backspace' }) })
    expect(screen.queryByTestId('space-ask')).toBeNull()
    expect(view.state.doc.toString()).toBe(text)
    expect(view.state.selection.main.head).toBe(text.length)
  })

  it('a key that reaches the editor while the card is open works the card, and never moves the caret', async () => {
    const text = 'intro\n\nFirst paragraph.\n\nSecond paragraph.\n\n'
    getDoc.mockResolvedValue({ content: text, version: 'v1' })
    renderPane()
    const view = await editor()
    act(() => { view.dispatch({ selection: { anchor: text.length } }) })
    act(() => { view.contentDOM.dispatchEvent(new KeyboardEvent('keydown', { key: ' ', code: 'Space', bubbles: true, cancelable: true })) })
    // The box exists, and has the focus, the moment the card opens.
    const input = screen.getByTestId('space-ask-input')
    expect(document.activeElement).toBe(input)
    const sel = () => view.dom.querySelectorAll('.cm-space-sel').length
    expect(sel()).toBe(1)
    // Up and Down pressed on the editor itself (the box not yet focused) choose blocks.
    const press = (key: string) => act(() => { view.contentDOM.dispatchEvent(new KeyboardEvent('keydown', { key, code: key, bubbles: true, cancelable: true })) })
    press('ArrowUp')
    expect(sel()).toBe(2)
    press('ArrowDown')
    expect(sel()).toBe(1)
    expect(view.state.selection.main.head).toBe(text.length)
    // The line the card stands on keeps its height, so vertical movement stays well defined.
    expect(view.dom.querySelector('.cm-space-ask-line')).toBeNull()
    press('Escape')
    expect(screen.queryByTestId('space-ask')).toBeNull()
    expect(view.state.doc.toString()).toBe(text)
  })

  it('Enter in the ask card sends it about the chosen blocks, wrapped like a highlight', async () => {
    const text = 'intro\n\nFirst paragraph.\n\nSecond paragraph.\n\n'
    getDoc.mockResolvedValue({ content: text, version: 'v1' })
    saveDoc.mockResolvedValue({ content: '', version: 'v2' })
    send.mockResolvedValue({ status: 'dispatched' })
    renderPane()
    const view = await editor()
    act(() => { view.dispatch({ selection: { anchor: text.length } }) })
    act(() => { view.contentDOM.dispatchEvent(new KeyboardEvent('keydown', { key: ' ', code: 'Space', bubbles: true, cancelable: true })) })
    const input = await screen.findByTestId('space-ask-input')
    act(() => { fireEvent.keyDown(input, { key: 'ArrowUp' }) })
    fireEvent.change(input, { target: { value: 'tighten these' } })
    await act(async () => { fireEvent.keyDown(input, { key: 'Enter' }) })
    expect(screen.queryByTestId('space-ask')).toBeNull()
    const [marker] = parseMarkers(view.state.doc.toString())
    expect(marker.kind).toBe('ask')
    const { message } = send.mock.calls[0][0] as { message: string }
    expect(message.startsWith(`[Doc ask ${marker.id}]\ntighten these\n`)).toBe(true)
    expect(message).toContain('"""\nFirst paragraph.\n\nSecond paragraph.\n"""')
  })

  it('an ask card with nothing chosen marks the spot and says so to the conductor', async () => {
    getDoc.mockResolvedValue({ content: '', version: 'v1' })
    saveDoc.mockResolvedValue({ content: '', version: 'v2' })
    send.mockResolvedValue({ status: 'dispatched' })
    renderPane()
    const view = await editor()
    act(() => { view.contentDOM.dispatchEvent(new KeyboardEvent('keydown', { key: ' ', code: 'Space', bubbles: true, cancelable: true })) })
    const input = await screen.findByTestId('space-ask-input')
    expect(screen.getByTestId('space-ask-about').textContent).toMatch(/Nothing selected/)
    fireEvent.change(input, { target: { value: 'draft an outline' } })
    await act(async () => { fireEvent.keyDown(input, { key: 'Enter' }) })
    const [marker] = parseMarkers(view.state.doc.toString())
    expect(view.state.doc.toString()).toBe(`<!-- ask:${marker.id} --><!-- /ask:${marker.id} -->`)
    const { message } = send.mock.calls[0][0] as { message: string }
    expect(message).toContain('with nothing highlighted')
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

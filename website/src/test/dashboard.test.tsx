/**
 * The dashboard session: its page model (versions, the conductor's page blocks),
 * its turn, the frame document, and the pane driven through the messages its
 * frame helper sends (the frame is sandboxed, so that is the only way in).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  api: { getDoc: vi.fn(), saveDoc: vi.fn(), resolveApproval: vi.fn(), artifact: vi.fn(() => Promise.reject(new Error('404'))) },
}))
vi.mock('../chat-core/transport/sendTurn', () => ({ sendTurn: vi.fn() }))
// The real hook mints a gateway URL; the pane only needs one to frame.
vi.mock('../hooks/useSandboxDoc', () => ({
  useSandboxDoc: (doc: string | null) => ({ url: doc ? 'about:blank' : null, failed: false, pending: false, stalled: false, retry: () => {} }),
}))

import { api } from '../api/client'
import { sendTurn } from '../chat-core/transport/sendTurn'
import { store } from '../store'
import { replaceMessages, setActiveSlot } from '../store/chatSlice'
import DashboardPane from '../pages/chat/page/dashboard/DashboardPane'
import {
  DASHBOARD_ID_PREFIX, applyPageChanges, describeTargets, emptyDashboard, isDashboardId, pageChangesIn, parseDashboard, redoPage, serializeDashboard, undoPage, withPage,
} from '../pages/chat/page/dashboard/dashboardModel'
import { buildDashboardTurn } from '../pages/chat/page/dashboard/dashboardTurn'
import { buildPageDocument } from '../pages/chat/page/dashboard/pageFrame'
import { splitReply } from '../pages/chat/page/docConductor'

const getDoc = api.getDoc as unknown as ReturnType<typeof vi.fn>
const saveDoc = api.saveDoc as unknown as ReturnType<typeof vi.fn>
const send = sendTurn as unknown as ReturnType<typeof vi.fn>
const DOC = `${DASHBOARD_ID_PREFIX}0123456789abcdef0123456789a`
const PAGE = '<!doctype html><html><head></head><body><h1 data-id="title">Ops</h1><section data-id="prs"><p>3 open PRs</p></section></body></html>'

describe('dashboard model', () => {
  it('knows a dashboard id by its prefix', () => {
    expect(isDashboardId(DOC)).toBe(true)
    expect(isDashboardId('0123456789abcdef0123456789abcdef')).toBe(false)
  })

  it('round-trips, and a broken file reads as a blank dashboard', () => {
    const d = { ...emptyDashboard(), html: PAGE, asks: [{ id: 'a', targets: ['el:prs'], label: 'PRs' }] }
    expect(parseDashboard(serializeDashboard(d))).toEqual(d)
    expect(parseDashboard('not json')).toEqual(emptyDashboard())
  })

  it('keeps each page it replaces, so undo and redo step through versions', () => {
    const one = withPage(emptyDashboard(), '<p>one</p>')
    const two = withPage(one, '<p>two</p>')
    expect(withPage(two, '<p>two</p>')).toBe(two)
    const back = undoPage(two)
    expect(back.html).toBe('<p>one</p>')
    expect(redoPage(back).html).toBe('<p>two</p>')
    // A new page after an undo drops what could have been redone.
    expect(withPage(back, '<p>three</p>').future).toEqual([])
  })

  it('reads whole-page and one-element blocks, and hides both from the answer', () => {
    const reply = 'Built it.\n```page-html\n<p>hi</p>\n```\nAnd tweaked:\n```page-replace target="el:prs"\n<section data-id="prs">5</section>\n```'
    expect(pageChangesIn(reply)).toEqual([
      { kind: 'page', html: '<p>hi</p>' },
      { kind: 'replace', target: 'el:prs', html: '<section data-id="prs">5</section>' },
    ])
    // A bare data-id is read as one.
    expect(pageChangesIn('```page-replace target="prs"\n<b>x</b>\n```')[0]).toMatchObject({ target: 'el:prs' })
    expect(splitReply(reply).prose).toBe('Built it.\n\nAnd tweaked:')
  })

  it('replaces only the targeted element, by data-id or selector, and skips one that is gone', () => {
    const swapped = applyPageChanges(PAGE, [{ kind: 'replace', target: 'el:prs', html: '<section data-id="prs"><p>5 open PRs</p></section>' }])
    expect(swapped).toContain('5 open PRs')
    expect(swapped).toContain('<h1 data-id="title">Ops</h1>')
    const bySelector = applyPageChanges(PAGE, [{ kind: 'replace', target: 'css:body > h1:nth-child(1)', html: '<h1 data-id="title">Ops board</h1>' }])
    expect(bySelector).toContain('Ops board')
    expect(applyPageChanges(PAGE, [{ kind: 'replace', target: 'el:gone', html: '<b>no</b>' }])).toBe(PAGE)
  })

  it('describes what was picked by its markup', () => {
    expect(describeTargets(PAGE, [])).toBe('(the whole page)')
    expect(describeTargets(PAGE, ['el:prs'])).toContain('<p>3 open PRs</p>')
    expect(describeTargets(PAGE, ['el:gone'])).toContain('no longer on the page')
  })
})

describe('dashboard turn and frame', () => {
  it('tells the conductor the page is the object and the overlay is the state', () => {
    const turn = buildDashboardTurn({ id: 'q1', request: 'add CI status', html: PAGE, targets: ['el:prs'], followUp: false, docId: DOC, title: 'Ops' })
    expect(turn.startsWith('[Doc ask q1]\nadd CI status')).toBe(true)
    expect(turn).toContain('THE PAGE is the object')
    expect(turn).toContain('THE OVERLAY is the state')
    // Only the conductor can write the page, so it builds it itself and fetches public data itself.
    expect(turn).toContain('Build the page YOURSELF, in this reply')
    expect(turn).toContain('never hand building or restyling the page to one')
    expect(turn).toContain('https://api.github.com')
    expect(turn).toContain('```page-replace target="el:<data-id>"')
    expect(turn).toContain(`slug "doc-overlay-${DOC}"`)
    expect(turn).toContain('<p>3 open PRs</p>')
    expect(buildDashboardTurn({ id: 'q', request: 'x', html: '', targets: [], followUp: false, docId: DOC })).toContain('(empty: nothing has been built yet)')
  })

  it('a main thread turn is about the whole dashboard and is answered in the main thread', () => {
    const turn = buildDashboardTurn({ id: 'main', request: 'what is on here?', html: PAGE, targets: [], followUp: false, docId: DOC })
    expect(turn.startsWith('[Doc ask main]\nwhat is on here?')).toBe(true)
    expect(turn).toContain('This is the main thread: they mean the whole dashboard')
    expect(turn).toContain('Your answer is shown in the main thread')
    expect(turn).not.toContain('a small pin on the element they picked')
  })

  it('wraps the page with a network-closed CSP, the theme and the helper', () => {
    const doc = buildPageDocument(PAGE, { '--bg': '#fff' })
    expect(doc.startsWith('<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy"')).toBe(true)
    expect(doc).toContain("connect-src 'none'")
    expect(doc).toContain('--bg:#fff')
    expect(doc).toContain("dw: 'pick'")
    // The page's own doctype is dropped, so the helper's comes first.
    expect(doc.match(/<!doctype/gi)).toHaveLength(1)
  })
})

describe('DashboardPane', () => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const renderPane = () => render(
    <QueryClientProvider client={queryClient}>
      <Provider store={store}><DashboardPane docId={DOC} slotKey="dash-slot" title="Ops" /></Provider>
    </QueryClientProvider>,
  )
  /** A message from the page's frame helper. */
  const fromFrame = (data: unknown) => {
    const frame = screen.getByTestId('dashboard-frame') as HTMLIFrameElement
    act(() => { window.dispatchEvent(new MessageEvent('message', { data, source: frame.contentWindow })) })
  }

  beforeEach(() => {
    getDoc.mockReset()
    saveDoc.mockReset()
    saveDoc.mockResolvedValue({ content: '', version: 'v2' })
    send.mockReset()
    send.mockResolvedValue({ status: 'sent' })
    localStorage.clear()
  })
  afterEach(() => {
    vi.useRealTimers()
    act(() => { store.dispatch(replaceMessages([])); store.dispatch(setActiveSlot(null)) })
  })

  it('opens blank with a placeholder whose box asks about the whole page', async () => {
    getDoc.mockResolvedValue({ content: '', version: '' })
    renderPane()
    expect(await screen.findByTestId('dashboard-empty')).toBeTruthy()
    expect(screen.queryByTestId('dashboard-frame')).toBeNull()
    const box = screen.getByTestId('dashboard-ask-input')
    fireEvent.change(box, { target: { value: 'A board of my open PRs' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    await act(async () => { await Promise.resolve() })
    expect(send).toHaveBeenCalledTimes(1)
    const message = send.mock.calls[0][0].message as string
    expect(message).toMatch(/^\[Doc ask \w+\]\nA board of my open PRs/)
    expect(message).toContain('nothing in particular (the whole page)')
    expect(screen.getByTestId('conversation-bar')).toBeTruthy()
  })

  it('picks an element the frame reports, outlines it, and asks about its markup', async () => {
    getDoc.mockResolvedValue({ content: serializeDashboard({ ...emptyDashboard(), html: PAGE }), version: 'v1' })
    renderPane()
    await screen.findByTestId('dashboard-frame')
    fromFrame({ dw: 'hover', target: 'el:prs', rect: { x: 10, y: 80, w: 300, h: 60 }, label: '3 open PRs' })
    expect(screen.getByTestId('dashboard-hover').textContent).toBe('prs')
    fromFrame({ dw: 'pick', target: 'el:prs', label: '3 open PRs', shift: false })
    fromFrame({ dw: 'rects', rects: { 'el:prs': { x: 10, y: 80, w: 300, h: 60 } } })
    const outline = screen.getByTestId('dashboard-picked')
    expect(outline.style.top).toBe('78px')
    // C pressed inside the page opens the ask box under the element.
    fromFrame({ dw: 'key', key: 'c', meta: false, ctrl: false, alt: false, shift: false })
    const box = screen.getByTestId('dashboard-ask-input')
    expect((box.closest('[style]') as HTMLElement).style.top).toBe('150px')
    fireEvent.change(box, { target: { value: 'add CI status' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    await act(async () => { await Promise.resolve() })
    const message = send.mock.calls[0][0].message as string
    expect(message).toContain('They picked el:prs')
    expect(message).toContain('el:prs:\n<section data-id="prs"><p>3 open PRs</p></section>')
    expect(screen.queryByTestId('dashboard-picked')).toBeNull()
  })

  it('ignores messages from anything but its own frame', async () => {
    getDoc.mockResolvedValue({ content: serializeDashboard({ ...emptyDashboard(), html: PAGE }), version: 'v1' })
    renderPane()
    await screen.findByTestId('dashboard-frame')
    act(() => { window.dispatchEvent(new MessageEvent('message', { data: { dw: 'hover', target: 'el:prs', rect: { x: 0, y: 0, w: 1, h: 1 }, label: '' }, source: window })) })
    expect(screen.queryByTestId('dashboard-hover')).toBeNull()
  })

  it('lands a finished reply\'s page change as one undo step', async () => {
    getDoc.mockResolvedValue({ content: serializeDashboard({ ...emptyDashboard(), html: PAGE }), version: 'v1' })
    vi.useFakeTimers({ shouldAdvanceTime: true })
    act(() => {
      store.dispatch(setActiveSlot('dash-slot'))
      store.dispatch(replaceMessages([
        { role: 'user', content: '[Doc ask q1]\nshow 5\n\n---\nrest', cls: '' },
        { role: 'assistant', content: 'Updated.\n```page-replace target="el:prs"\n<section data-id="prs"><p>5 open PRs</p></section>\n```', cls: '', meta: { mid: 'r1' } },
      ]))
    })
    renderPane()
    await screen.findByTestId('dashboard-frame')
    await act(async () => { vi.advanceTimersByTime(600) })
    const saved = parseDashboard(saveDoc.mock.calls.at(-1)![1] as string)
    expect(saved.html).toContain('5 open PRs')
    expect(saved.past).toEqual([PAGE])
    // Undo goes back to the page the reply replaced.
    fireEvent.click(screen.getByRole('button', { name: 'Undo' }))
    await act(async () => { vi.advanceTimersByTime(600) })
    expect(parseDashboard(saveDoc.mock.calls.at(-1)![1] as string).html).toBe(PAGE)
  })
})

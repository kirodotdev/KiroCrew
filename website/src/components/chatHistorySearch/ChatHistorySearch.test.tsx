import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { renderWithProviders } from '../../test/helpers'

const mocks = vi.hoisted(() => ({
  chatHistorySearch: vi.fn(),
  chatHistoryAround: vi.fn(),
  chatHistoryCounts: vi.fn(),
  mobile: false,
}))

vi.mock('../../hooks/useIsMobile', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../hooks/useIsMobile')>()),
  useIsMobile: () => mocks.mobile,
}))

vi.mock('../../api/client', async (importOriginal) => {
  const real = await importOriginal<typeof import('../../api/client')>()
  return {
    ...real,
    api: new Proxy(real.api, {
      get(target, prop, receiver) {
        if (prop === 'chatHistorySearch') return mocks.chatHistorySearch
        if (prop === 'chatHistoryAround') return mocks.chatHistoryAround
        if (prop === 'chatHistoryCounts') return mocks.chatHistoryCounts
        return Reflect.get(target, prop, receiver)
      },
    }),
  }
})

import { fmtDate } from '../../i18n/format'
import { installHighlightApiStub, registeredRanges } from '../../test/highlightApiStub'
import { SEARCH_HL_MATCH } from '../../utils/domHighlight'
import ChatHistorySearch, { dayEpoch, hitTimeLabel } from './ChatHistorySearch'
import { dayKey } from './MonthCalendar'

const HITS = {
  hits: [
    { index: 40, role: 'assistant', ts: '2026-11-20T10:00:05Z', mid: 'm-40', snippet: 'the plum report is ready', image_count: 1, images: ['/tmp/chart.png'] },
    { index: 12, role: 'user', ts: '2026-11-18T09:00:00Z', mid: 'm-12', snippet: 'where is the plum report', file_count: 2 },
  ],
  next_before: null,
  total: 50,
}

// One fixed day, so the calendar and the expected label always agree.
const NOW = new Date(2026, 10, 23, 15, 0)

const AROUND = {
  messages: [
    { role: 'user', content: 'before it', cls: 'msg msg-u', ts: '2026-11-20T10:00:00Z', meta: { mid: 'm-39' } },
    { role: 'assistant', content: 'the plum report is ready', cls: 'msg msg-a', ts: '2026-11-20T10:00:05Z', meta: { mid: 'm-40' } },
  ],
  start: 39,
  end: 41,
  total: 50,
  has_earlier: true,
}

installHighlightApiStub()

beforeEach(() => {
  // Only Date is faked: the debounce and the request timers stay real.
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(NOW)
  mocks.chatHistorySearch.mockReset().mockResolvedValue(HITS)
  mocks.chatHistoryAround.mockReset().mockResolvedValue(AROUND)
  // No counts unless a test answers them: the tabs keep their bare names.
  mocks.chatHistoryCounts.mockReset().mockReturnValue(new Promise(() => {}))
  mocks.mobile = false
})

afterEach(() => { vi.useRealTimers(); vi.clearAllMocks() })

function open(onClose = vi.fn()) {
  renderWithProviders(<ChatHistorySearch slotKey="chat-7" bot={{ name: 'Scout', seed: 'scout' }} onClose={onClose} />)
  return { onClose, input: screen.getByRole('textbox', { name: 'Search this chat' }) }
}

describe('ChatHistorySearch', () => {
  it('starts on the quick entries and searches nothing', () => {
    open()
    for (const name of ['Date', 'Images', 'Files', 'Links', 'Videos', 'PDFs', 'HTML', 'Widgets']) expect(screen.getByRole('button', { name })).toBeInTheDocument()
    // A bot chat cannot pin a message, so it offers no Pinned.
    expect(screen.queryByRole('button', { name: 'Pinned' })).toBeNull()
    expect(mocks.chatHistorySearch).not.toHaveBeenCalled()
  })

  it('searches on the server and lists hits with speaker, badges and the match in the accent colour', async () => {
    const { input } = open()
    fireEvent.change(input, { target: { value: 'plum' } })

    const options = await screen.findAllByRole('option')
    expect(options).toHaveLength(2)
    expect(options[0]).toHaveTextContent('Scout')
    expect(options[0]).toHaveTextContent('1 image')
    expect(options[1]).toHaveTextContent('You')
    expect(options[1]).toHaveTextContent('2 files')
    expect(options[0].querySelector('.text-accent.font-medium')).toHaveTextContent('plum')
    // A bot chat marks the bot's messages with its small avatar on the sender
    // line, beside its name, and the person's with none.
    const avatar = options[0].querySelector('[data-testid="chat-history-bot-avatar"]')
    expect(avatar).not.toBeNull()
    expect(avatar?.parentElement).toHaveTextContent('Scout')
    expect(options[1].querySelector('[data-testid="chat-history-bot-avatar"]')).toBeNull()
    expect(screen.queryByText('Searched back to the first message')).toBeNull()
    expect(screen.getByRole('tab', { name: 'All', selected: true })).toBeInTheDocument()
    expect(mocks.chatHistorySearch).toHaveBeenCalledWith('chat-7', expect.objectContaining({ q: 'plum' }), expect.anything())
  })

  it('lists one type from its entry, under that tab', async () => {
    open()
    fireEvent.click(screen.getByRole('button', { name: 'Images' }))

    expect(await screen.findByRole('tab', { name: 'Images', selected: true })).toBeInTheDocument()
    await waitFor(() => expect(mocks.chatHistorySearch).toHaveBeenLastCalledWith('chat-7', expect.objectContaining({ kind: 'images', q: undefined }), expect.anything()))
    fireEvent.click(screen.getByRole('tab', { name: 'Links' }))
    await waitFor(() => expect(mocks.chatHistorySearch).toHaveBeenLastCalledWith('chat-7', expect.objectContaining({ kind: 'links' }), expect.anything()))
  })

  it.each(['Images', 'Files', 'Links'])('returns to quick entries when All removes the last %s filter', async (kind) => {
    open()
    fireEvent.click(screen.getByRole('button', { name: kind }))
    await screen.findAllByRole('option')
    expect(mocks.chatHistorySearch).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole('tab', { name: 'All' }))

    expect(await screen.findByTestId('chat-history-entries')).toBeVisible()
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    expect(mocks.chatHistorySearch).toHaveBeenCalledTimes(1)
  })

  it('keeps searching the query when All removes only the type filter', async () => {
    const { input } = open()
    fireEvent.click(screen.getByRole('button', { name: 'Images' }))
    await screen.findAllByRole('option')
    fireEvent.change(input, { target: { value: 'plum' } })
    await waitFor(() => expect(mocks.chatHistorySearch).toHaveBeenLastCalledWith(
      'chat-7', expect.objectContaining({ q: 'plum', kind: 'images' }), expect.anything(),
    ))

    fireEvent.click(screen.getByRole('tab', { name: 'All' }))

    await waitFor(() => expect(mocks.chatHistorySearch).toHaveBeenLastCalledWith(
      'chat-7', expect.objectContaining({ q: 'plum', kind: undefined }), expect.anything(),
    ))
    expect(screen.getByRole('listbox')).toBeVisible()
    expect(screen.queryByTestId('chat-history-entries')).not.toBeInTheDocument()
  })

  it('picks one day on the calendar and lists only that day', async () => {
    open()
    fireEvent.click(screen.getByRole('button', { name: 'Date' }))
    const today = NOW
    const label = today.toLocaleDateString('en-US', { year: 'numeric', month: 'long', day: 'numeric' })
    fireEvent.click(await screen.findByRole('gridcell', { name: label }))

    const day = dayKey(today)
    await waitFor(() => expect(mocks.chatHistorySearch).toHaveBeenLastCalledWith(
      'chat-7',
      expect.objectContaining({ since: dayEpoch(day), until: dayEpoch(day, 1) }),
      expect.anything(),
    ))
    fireEvent.click(screen.getByRole('button', { name: 'Show every day' }))
    expect(await screen.findByTestId('chat-history-entries')).toBeInTheDocument()
  })

  it('previews a hit in the panel with the rows around it and reads more on request', async () => {
    const { input } = open()
    fireEvent.change(input, { target: { value: 'plum' } })
    fireEvent.click((await screen.findAllByRole('option'))[0])

    expect(await screen.findByText('before it')).toBeInTheDocument()
    expect(mocks.chatHistoryAround).toHaveBeenCalledWith('chat-7', 32, 49, expect.anything(), 'm-40')
    expect(screen.queryByRole('button', { name: 'Show in chat' })).toBeNull()
    // The preview names the hit's day and marks the query inside the hit's own row.
    expect(screen.getByRole('heading', { name: `Around ${fmtDate(HITS.hits[0].ts)}` })).toBeVisible()
    const target = screen.getByTestId('chat-history-preview').querySelector('[data-jump-target]')
    await waitFor(() => expect(registeredRanges(SEARCH_HL_MATCH).filter(r => target?.contains(r.startContainer)).map(r => r.toString())).toEqual(['plum']))

    mocks.chatHistoryAround.mockResolvedValueOnce({ messages: [{ role: 'user', content: 'much earlier', cls: 'msg msg-u', ts: '2026-11-20T09:00:00Z', meta: { mid: 'm-19' } }], start: 19, end: 39, total: 50, has_earlier: false })
    fireEvent.click(screen.getByRole('button', { name: 'Show earlier messages' }))
    expect(await screen.findByText('much earlier')).toBeInTheDocument()
    expect(mocks.chatHistoryAround).toHaveBeenLastCalledWith('chat-7', 19, 39, expect.anything(), undefined)
    expect(screen.queryByRole('button', { name: 'Show earlier messages' })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Back to results' }))
    expect(screen.getAllByRole('option')).toHaveLength(2)
  })

  it('uses the hit id after rotation and pages from the relocated range', async () => {
    mocks.chatHistoryAround.mockResolvedValueOnce({ ...AROUND, start: 19, end: 21, total: 30 })
    const { input } = open()
    fireEvent.change(input, { target: { value: 'plum' } })
    fireEvent.click((await screen.findAllByRole('option'))[0])
    expect(await screen.findByText('before it')).toBeInTheDocument()
    expect(mocks.chatHistoryAround).toHaveBeenLastCalledWith('chat-7', 32, 49, expect.anything(), 'm-40')
    fireEvent.click(screen.getByRole('button', { name: 'Show earlier messages' }))
    await waitFor(() => expect(mocks.chatHistoryAround).toHaveBeenLastCalledWith('chat-7', 0, 19, expect.anything(), undefined))
  })

  it('shows the preview error when a saved hit no longer exists', async () => {
    mocks.chatHistoryAround.mockRejectedValueOnce(new Error('message_not_found'))
    const { input } = open()
    fireEvent.change(input, { target: { value: 'plum' } })
    fireEvent.click((await screen.findAllByRole('option'))[0])
    expect(await screen.findByTestId('chat-history-preview-error')).toBeVisible()
    expect(screen.queryByText('before it')).not.toBeInTheDocument()
  })

  it.each([['Videos', 'videos'], ['PDFs', 'pdfs'], ['HTML', 'html'], ['Widgets', 'widgets']])('the %s entry and tab send kind=%s', async (name, kind) => {
    open()
    fireEvent.click(screen.getByRole('button', { name }))
    expect(await screen.findByRole('tab', { name, selected: true })).toBeInTheDocument()
    await waitFor(() => expect(mocks.chatHistorySearch).toHaveBeenLastCalledWith('chat-7', expect.objectContaining({ kind }), expect.anything()))
    // From another tab, the tab itself sends the kind too.
    fireEvent.click(screen.getByRole('tab', { name: 'Links' }))
    await waitFor(() => expect(mocks.chatHistorySearch).toHaveBeenLastCalledWith('chat-7', expect.objectContaining({ kind: 'links' }), expect.anything()))
    mocks.chatHistorySearch.mockClear()
    fireEvent.change(screen.getByRole('textbox', { name: 'Search this chat' }), { target: { value: 'plum' } })
    await screen.findAllByRole('option')
    fireEvent.click(screen.getByRole('tab', { name }))
    await waitFor(() => expect(mocks.chatHistorySearch).toHaveBeenLastCalledWith('chat-7', expect.objectContaining({ kind, q: 'plum' }), expect.anything()))
  })

  // Each rendered tab's count, keyed by the kind its count names (`all` for All).
  const tabCounts = () => Object.fromEntries(screen.getAllByRole('tab').map(tab => {
    const badge = tab.querySelector('[data-testid^="chat-history-tab-count-"]')
    return [badge?.getAttribute('data-testid')?.replace('chat-history-tab-count-', ''), badge?.textContent?.trim()]
  }))

  it("shows every tab's whole-chat count from one read, unchanged by a tab switch", async () => {
    // Each tab's own list pages differently, and its replies' counts differ:
    // the tabs must not show them.
    mocks.chatHistorySearch.mockImplementation(async (_slot: string, p: { kind?: string }) => ({ ...HITS, next_before: null, counts: { '': p.kind === 'links' ? 125 : 423, images: 11 } }))
    mocks.chatHistoryCounts.mockResolvedValue({ counts: { '': 900, images: 30, files: 1, links: 104, videos: 0 }, total: 5000, complete: true })
    open()
    fireEvent.click(screen.getByRole('button', { name: 'Images' }))
    await waitFor(() => expect(screen.getByTestId('chat-history-tab-count-all')).toHaveTextContent('900'))
    const first = tabCounts()
    // Every rendered tab shows the counts read's number for its kind, 0 when absent.
    const counts: Record<string, number> = { '': 900, images: 30, files: 1, links: 104, videos: 0 }
    expect(Object.keys(first).length).toBe(screen.getAllByRole('tab').length)
    for (const [kind, shown] of Object.entries(first)) expect(shown).toBe(String(counts[kind === 'all' ? '' : kind] ?? 0))

    for (const name of [/^Links/, /^Files/, /^Videos/, /^Images/]) {
      fireEvent.click(screen.getByRole('tab', { name }))
      await waitFor(() => expect(screen.getByRole('tab', { name, selected: true })).toBeInTheDocument())
      expect(tabCounts()).toEqual(first)
    }
    // One counts read for the chat and its (empty) query, never one per tab.
    expect(mocks.chatHistoryCounts).toHaveBeenCalledTimes(1)
    expect(mocks.chatHistoryCounts).toHaveBeenCalledWith('chat-7', { q: undefined, since: undefined, until: undefined }, expect.anything())
  })

  it('reads the counts again when the query changes, and keeps no stale reply', async () => {
    const replies: Record<string, (v: unknown) => void> = {}
    mocks.chatHistoryCounts.mockImplementation((_slot: string, p: { q?: string }) => new Promise(r => { replies[p.q ?? ''] = r }))
    const { input } = open()
    fireEvent.change(input, { target: { value: 'plum' } })
    await waitFor(() => expect(replies.plum).toBeDefined())
    fireEvent.change(input, { target: { value: 'pear' } })
    await waitFor(() => expect(replies.pear).toBeDefined())
    await act(async () => { replies.pear({ counts: { '': 4, links: 2 }, total: 50, complete: true }) })
    await waitFor(() => expect(screen.getByTestId('chat-history-tab-count-all')).toHaveTextContent('4'))
    // The older query answers last: its numbers never replace the newer ones.
    await act(async () => { replies.plum({ counts: { '': 77, links: 70 }, total: 50, complete: true }) })
    expect(screen.getByTestId('chat-history-tab-count-all')).toHaveTextContent('4')
    expect(screen.getByTestId('chat-history-tab-count-links')).toHaveTextContent('2')
    expect(mocks.chatHistoryCounts).toHaveBeenLastCalledWith('chat-7', expect.objectContaining({ q: 'pear' }), expect.anything())
  })

  it('shows no number from a count walk the server cut short', async () => {
    mocks.chatHistoryCounts.mockResolvedValue({ counts: { '': 9, links: 0 }, total: 200_000, complete: false })
    const { input } = open()
    fireEvent.change(input, { target: { value: 'plum' } })
    await screen.findAllByRole('option')
    await waitFor(() => expect(mocks.chatHistoryCounts).toHaveBeenCalled())
    expect(screen.queryByTestId('chat-history-tab-count-all')).toBeNull()
    expect(screen.queryByTestId('chat-history-tab-count-links')).toBeNull()
  })

  it('reads the counts again while the chat is being indexed, until it is not', async () => {
    vi.useFakeTimers({ toFake: ['Date', 'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'] })
    vi.setSystemTime(NOW)
    mocks.chatHistoryCounts
      .mockResolvedValueOnce({ counts: { '': 3 }, total: 50, complete: true, indexing: true })
      .mockResolvedValueOnce({ counts: { '': 3 }, total: 50, complete: true, indexing: true })
      .mockResolvedValue({ counts: { '': 40, links: 7 }, total: 50, complete: true })
    const { input } = open()
    fireEvent.change(input, { target: { value: 'plum' } })
    await act(async () => { await vi.advanceTimersByTimeAsync(300) })
    expect(mocks.chatHistoryCounts).toHaveBeenCalledTimes(1)
    // 1 s, then 2 s later: the third reply is the built cache's.
    await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
    expect(mocks.chatHistoryCounts).toHaveBeenCalledTimes(2)
    await act(async () => { await vi.advanceTimersByTimeAsync(2000) })
    expect(mocks.chatHistoryCounts).toHaveBeenCalledTimes(3)
    // The reply lands, then the tabs render it.
    await act(async () => { await vi.advanceTimersByTimeAsync(50) })
    expect(screen.getByTestId('chat-history-tab-count-all')).toHaveTextContent('40')
    expect(screen.getByTestId('chat-history-tab-count-links')).toHaveTextContent('7')
    // A reply without `indexing` ends the re-reads.
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
    expect(mocks.chatHistoryCounts).toHaveBeenCalledTimes(3)
  })

  it('stops reading the counts again after three tries', async () => {
    vi.useFakeTimers({ toFake: ['Date', 'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'] })
    vi.setSystemTime(NOW)
    mocks.chatHistoryCounts.mockResolvedValue({ counts: { '': 3 }, total: 50, complete: true, indexing: true })
    const { input } = open()
    fireEvent.change(input, { target: { value: 'plum' } })
    await act(async () => { await vi.advanceTimersByTimeAsync(300) })
    for (const wait of [1000, 2000, 4000]) {
      await act(async () => { await vi.advanceTimersByTimeAsync(wait) })
    }
    expect(mocks.chatHistoryCounts).toHaveBeenCalledTimes(4)
    await act(async () => { await vi.advanceTimersByTimeAsync(50_000) })
    expect(mocks.chatHistoryCounts).toHaveBeenCalledTimes(4)
  })

  it('keeps the list and flags a failed count read through ErrorNotice', async () => {
    mocks.chatHistoryCounts.mockRejectedValue(new Error('503'))
    const { input } = open()
    fireEvent.change(input, { target: { value: 'plum' } })
    expect(await screen.findByTestId('chat-history-counts-error', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(screen.getAllByRole('option')).toHaveLength(2)
    expect(screen.queryByTestId('chat-history-tab-count-all')).toBeNull()
  })

  it('names the app that sent a user message instead of You', async () => {
    mocks.chatHistorySearch.mockResolvedValue({ hits: [{ index: 3, role: 'user', ts: '2026-11-20T10:00:05Z', mid: 'm-3', snippet: 'plum from notes', meta: { turnActor: 'app', appLabel: 'notes' } }], next_before: null, total: 10 })
    const { input } = open()
    fireEvent.change(input, { target: { value: 'plum' } })
    const [row] = await screen.findAllByRole('option')
    await waitFor(() => expect(row).toHaveTextContent('notes'))
    expect(row).not.toHaveTextContent('You')
  })

  it('keeps the image count on a picture-only hit whose thumbnail the server withheld', async () => {
    mocks.chatHistorySearch.mockResolvedValue({ hits: [{ index: 3, role: 'user', ts: '2026-11-20T10:00:05Z', mid: 'm-3', snippet: '', image_count: 1 }], next_before: null, total: 10 })
    const { input } = open()
    fireEvent.change(input, { target: { value: 'plum' } })
    const [row] = await screen.findAllByRole('option')
    expect(row).toHaveTextContent('1 image')
    expect(row.querySelector('img')).toBeNull()
  })

  it('draws each type with its icon and name, and a sent file as its card', async () => {
    mocks.chatHistorySearch.mockResolvedValue({
      hits: [
        { index: 9, role: 'assistant', ts: '2026-11-20T10:00:05Z', mid: 'm-9', snippet: 'here you go', video_count: 1, video_names: ['demo.webm'], pdf_count: 1, pdf_names: ['spec.pdf'], html_count: 1, html_names: ['page.html'], widget_count: 1, widget_titles: ['Sales chart'] },
        { index: 8, role: 'file', ts: '2026-11-20T09:00:05Z', mid: 'm-8', snippet: 'talk.mov', video_count: 1, video_names: ['talk.mov'] },
      ],
      next_before: null,
      total: 10,
    })
    open()
    fireEvent.click(screen.getByRole('button', { name: 'Videos' }))
    const [reply, sent] = await screen.findAllByRole('option')
    expect(within(reply).getByTestId('chat-history-video-badge')).toHaveTextContent('demo.webm')
    expect(within(reply).getByTestId('chat-history-pdf-badge')).toHaveTextContent('spec.pdf')
    expect(within(reply).getByTestId('chat-history-html-badge')).toHaveTextContent('page.html')
    expect(within(reply).getByTestId('chat-history-widget-badge')).toHaveTextContent('Sales chart')
    // An icon only: no video element that would load or play it.
    expect(reply.querySelector('video')).toBeNull()
    expect(within(sent).getByTestId('chat-history-sent-badge')).toHaveTextContent('talk.mov')
    // The name is said once, on the card, not again as an excerpt.
    expect(within(sent).getAllByText('talk.mov')).toHaveLength(1)
  })

  // jsdom lays nothing out: each tab is 100px wide, side by side in a 250px scroller.
  function layOutTabs() {
    const real = Element.prototype.getBoundingClientRect
    const into = Element.prototype.scrollIntoView
    const intoCalls: Element[] = []
    Element.prototype.scrollIntoView = function (this: Element) { intoCalls.push(this) }
    Element.prototype.getBoundingClientRect = function (this: Element) {
      const scroller = this.closest('[data-testid="chat-history-tabs"]')
      if (this.getAttribute('data-testid') === 'chat-history-tabs') return { left: 0, width: 250, top: 0, height: 30, right: 250, bottom: 30, x: 0, y: 0, toJSON() {} } as DOMRect
      if (scroller && this.getAttribute('role') === 'tab') {
        const i = Array.from(scroller.children).indexOf(this)
        const left = i * 100 - (scroller as HTMLElement).scrollLeft
        return { left, width: 100, top: 0, height: 30, right: left + 100, bottom: 30, x: left, y: 0, toJSON() {} } as DOMRect
      }
      return real.call(this)
    }
    Object.defineProperty(HTMLElement.prototype, 'clientWidth', { configurable: true, get() { return this.getAttribute?.('data-testid') === 'chat-history-tabs' ? 250 : 0 } })
    return {
      intoCalls,
      restore() {
        Element.prototype.getBoundingClientRect = real
        Element.prototype.scrollIntoView = into
        delete (HTMLElement.prototype as { clientWidth?: number }).clientWidth
      },
    }
  }

  it('keeps every tab on one sideways-scrolling row and scrolls only that row to the chosen tab', async () => {
    const layout = layOutTabs()
    try {
      open()
      fireEvent.click(screen.getByRole('button', { name: 'Widgets' }))
      const tabs = await screen.findByTestId('chat-history-tabs')
      expect(tabs).toHaveClass('flex-nowrap', 'overflow-x-auto', 'scrollbar-none')
      const widgets = screen.getByRole('tab', { name: 'Widgets', selected: true })
      expect(widgets).toHaveClass('shrink-0', 'whitespace-nowrap')
      // Widgets is the 7th tab in the scroller (600-700px): its right edge is brought in.
      await waitFor(() => expect(tabs.scrollLeft).toBe(450))
      fireEvent.click(screen.getByRole('tab', { name: 'Images' }))
      await waitFor(() => expect(tabs.scrollLeft).toBe(0))
      // scrollIntoView would scroll the row's ancestors too and clip the pinned All.
      expect(layout.intoCalls.filter(el => el.getAttribute('role') === 'tab')).toEqual([])
    } finally {
      layout.restore()
    }
  })

  it('keeps All pinned outside the sideways scroller, which fades its clipped edges', async () => {
    open()
    fireEvent.click(screen.getByRole('button', { name: 'Widgets' }))
    const scroller = await screen.findByTestId('chat-history-tabs')
    const all = screen.getByRole('tab', { name: 'All' })
    // All never scrolls away: it is the tab list's first child, beside the scroller.
    expect(scroller.contains(all)).toBe(false)
    expect(all.parentElement).toBe(scroller.parentElement)
    expect(scroller.parentElement).toHaveAttribute('role', 'tablist')
    expect(within(scroller).getByRole('tab', { name: 'Widgets', selected: true })).toBeInTheDocument()
    expect(scroller).toHaveClass('edge-fade-x')
  })

  it('the tab row ends where the Date chip starts: a flex-1 min-w-0 sibling, neither one an overlay', async () => {
    open()
    fireEvent.click(screen.getByRole('button', { name: 'Widgets' }))
    const tabs = await screen.findByTestId('chat-history-tablist')
    expect(tabs).toHaveClass('flex-1', 'min-w-0')
    const chip = tabs.nextElementSibling as HTMLElement
    expect(chip).toHaveAccessibleName('Date')
    expect(chip).toHaveClass('shrink-0')
    expect(tabs.parentElement).toHaveClass('flex')
    for (const el of [tabs, chip]) expect(el.className).not.toMatch(/\b(absolute|fixed|sticky)\b/)
  })

  it('scrolls the row to the chosen tab again once the counts arrive and widen the tabs before it', async () => {
    const layout = layOutTabs()
    let reply: (v: unknown) => void = () => {}
    mocks.chatHistoryCounts.mockReset().mockReturnValue(new Promise(r => { reply = r }))
    try {
      open()
      fireEvent.click(screen.getByRole('button', { name: 'Widgets' }))
      const tabs = await screen.findByTestId('chat-history-tabs')
      await waitFor(() => expect(tabs.scrollLeft).toBe(450))
      tabs.scrollLeft = 0
      await act(async () => { reply({ counts: { '': 2, files: 8, videos: 1, widgets: 2 }, total: 50, complete: true }) })
      await screen.findByTestId('chat-history-tab-count-files')
      await waitFor(() => expect(tabs.scrollLeft).toBe(450))
    } finally {
      layout.restore()
    }
  })

  it.each([[true, 1], [false, 0]])('on a phone (%s) Quote in the preview ends the search', async (mobile, closes) => {
    mocks.mobile = mobile
    const onQuote = vi.fn()
    const onClose = vi.fn()
    renderWithProviders(<ChatHistorySearch slotKey="chat-7" onClose={onClose} onQuoteMessage={onQuote} />)
    fireEvent.change(screen.getByRole('textbox', { name: 'Search this chat' }), { target: { value: 'plum' } })
    fireEvent.click((await screen.findAllByRole('option'))[0])
    const target = await waitFor(() => {
      const el = screen.getByTestId('chat-history-preview').querySelector('[data-jump-target]')
      expect(el).not.toBeNull()
      return el as HTMLElement
    })
    fireEvent.click(within(target).getByTestId('quote-message'))
    expect(onQuote).toHaveBeenCalledWith('assistant', 'the plum report is ready', '2026-11-20T10:00:05Z', 'm-40')
    expect(onClose).toHaveBeenCalledTimes(closes)
  })

  it('closes on Cancel and on Escape', () => {
    const { onClose, input } = open()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(2)
  })

  it('owns Escape unless its host carries the mark', () => {
    const view = renderWithProviders(<ChatHistorySearch slotKey="chat-7" onClose={vi.fn()} />)
    expect(screen.getByTestId('chat-history-search')).toHaveAttribute('data-escape-owner')
    view.unmount()
    renderWithProviders(<ChatHistorySearch slotKey="chat-7" onClose={vi.fn()} escapeOwner={false} />)
    expect(screen.getByTestId('chat-history-search')).not.toHaveAttribute('data-escape-owner')
  })

  it('does not open a hit on the Enter that commits an IME composition', async () => {
    const { input } = open()
    fireEvent.change(input, { target: { value: 'plum' } })
    await screen.findAllByRole('option')
    fireEvent.compositionStart(input)
    fireEvent.compositionEnd(input)
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(mocks.chatHistoryAround).not.toHaveBeenCalled()
  })
})

describe('ChatHistorySearch in a session chat', () => {
  it('lists pinned messages under Pinned with its count, with the query, and says how to pin when there are none', async () => {
    mocks.chatHistorySearch.mockResolvedValue(HITS)
    mocks.chatHistoryCounts.mockResolvedValue({ counts: { '': 9, pinned: 2 }, total: 50, complete: true })
    renderWithProviders(<ChatHistorySearch slotKey="chat-7" onClose={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Pinned' }))

    expect(await screen.findByRole('tab', { name: /^Pinned/, selected: true })).toBeInTheDocument()
    await waitFor(() => expect(mocks.chatHistorySearch).toHaveBeenLastCalledWith('chat-7', expect.objectContaining({ kind: 'pinned' }), expect.anything()))
    expect(await screen.findByTestId('chat-history-tab-count-pinned')).toHaveTextContent('2')
    expect(await screen.findAllByRole('option')).toHaveLength(2)

    fireEvent.change(screen.getByRole('textbox', { name: 'Search this chat' }), { target: { value: 'plum' } })
    await waitFor(() => expect(mocks.chatHistorySearch).toHaveBeenLastCalledWith('chat-7', expect.objectContaining({ kind: 'pinned', q: 'plum' }), expect.anything()))

    mocks.chatHistorySearch.mockResolvedValue({ hits: [], next_before: null, total: 50, counts: {} })
    fireEvent.change(screen.getByRole('textbox', { name: 'Search this chat' }), { target: { value: 'pear' } })
    expect(await screen.findByText("No pinned messages. Pin one from a message's More actions menu.")).toBeInTheDocument()
  })

  it('draws no avatar on any row', async () => {
    renderWithProviders(<ChatHistorySearch slotKey="chat-7" onClose={vi.fn()} />)
    fireEvent.change(screen.getByRole('textbox', { name: 'Search this chat' }), { target: { value: 'plum' } })
    await screen.findAllByRole('option')
    expect(screen.queryByTestId('chat-history-bot-avatar')).toBeNull()
  })
})

describe('ChatHistorySearch, older history with no recent hit', () => {
  it('stops automatic paging after an error and retries only on request', async () => {
    let rejectPage!: (error: Error) => void
    const failedPage = new Promise<never>((_, reject) => { rejectPage = reject })
    // A stray automatic retry remains pending, so the regression cannot spin.
    mocks.chatHistorySearch
      .mockResolvedValueOnce({ hits: [], next_before: 5, total: 50 })
      .mockReturnValueOnce(failedPage)
      .mockImplementation(() => new Promise(() => {}))
    open()
    fireEvent.click(screen.getByRole('button', { name: 'Images' }))
    await waitFor(() => expect(mocks.chatHistorySearch).toHaveBeenCalledTimes(2))
    await act(async () => { rejectPage(new Error('page unavailable')) })

    expect(await screen.findByTestId('chat-history-search-error')).toBeInTheDocument()
    const retry = screen.getByRole('button', { name: 'Search older messages' })
    expect(mocks.chatHistorySearch).toHaveBeenCalledTimes(2)
    mocks.chatHistorySearch.mockResolvedValue(HITS)
    fireEvent.click(retry)
    expect(await screen.findAllByRole('option')).toHaveLength(2)
    expect(mocks.chatHistorySearch).toHaveBeenCalledTimes(3)
    expect(screen.queryByTestId('chat-history-search-error')).toBeNull()
  })

  it('offers to search older messages when the steps it runs alone found nothing', async () => {
    mocks.chatHistorySearch.mockResolvedValue({ hits: [], next_before: 5, total: 50000 })
    const { input } = open()
    fireEvent.change(input, { target: { value: 'plum' } })

    expect(await screen.findByRole('button', { name: 'Search older messages' })).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('No recent messages match')
    expect(mocks.chatHistorySearch).toHaveBeenCalledTimes(8)
  })

  it('ignores the arrow keys while there is no hit', async () => {
    const { input } = open()
    fireEvent.change(input, { target: { value: 'pl' } })
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    fireEvent.change(input, { target: { value: 'plum' } })
    await screen.findAllByRole('option')
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(await screen.findByText('before it')).toBeInTheDocument()
  })
})

describe('hitTimeLabel', () => {
  const now = new Date(2026, 10, 23, 15, 0)
  it('is the time today, the weekday this week and the date before that', () => {
    expect(hitTimeLabel(new Date(2026, 10, 23, 9, 5).toISOString(), now)).toBe('9:05 AM')
    expect(hitTimeLabel(new Date(2026, 10, 21, 9, 5).toISOString(), now)).toBe('Saturday')
    expect(hitTimeLabel(new Date(2026, 10, 3, 9, 5).toISOString(), now)).toBe('11/3/2026')
  })
})

describe('dayEpoch', () => {
  it('is local midnight, and the next day for an exclusive end', () => {
    expect(dayEpoch('2026-11-18')).toBe(new Date(2026, 10, 18).getTime() / 1000)
    expect(dayEpoch('2026-11-18', 1)).toBe(new Date(2026, 10, 19).getTime() / 1000)
    expect(dayEpoch('bad')).toBeUndefined()
  })
})

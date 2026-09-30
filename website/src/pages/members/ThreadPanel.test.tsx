import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { ApiError } from '../../api/apiError'
import { threadLiveStore } from '../../state/threadLiveStore'
import { createTestStore } from '../../test/helpers'
import type { RootState } from '../../store'

const mockDetail = vi.fn()

vi.mock('../../api/threads', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/threads')>()
  return {
    ...actual,
    threadsApi: {
      summary: vi.fn(),
      detail: (...args: unknown[]) => mockDetail(...args),
      open: vi.fn(),
    },
  }
})

// The thread's own surface IS the ordinary chat pane, on the thread's own slot.
// That is the whole claim this file checks, and the pane is a 2000-line
// component with a store, a provider and a socket behind it — so it is stubbed
// down to the one thing the panel decides: WHICH slot it mounts.
vi.mock('../../components/ChatPane', () => ({
  default: ({ slotKey, frameless }: { slotKey: string; frameless?: boolean }) => (
    <div data-testid="chat-pane" data-slot={slotKey} data-frameless={frameless ? '1' : '0'}>
      chat pane
    </div>
  ),
}))

// The markdown renderer pulls in the whole highlight/mermaid stack; the panel's
// contract is the rows, not markdown rendering.
vi.mock('../../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <span>{content}</span>,
}))

import ThreadPanel from './ThreadPanel'

const PARENT = { mid: 'm-1', role: 'assistant', content: 'Overnight triage: 9 new issues.', ts: '2026-09-22T07:02:00Z' }
const ANCHOR = {
  kind: 'session' as const,
  thread_slot: 'chat-77-1758524400',
  title: 'The other eight',
  opened_by: 'user',
  opened_at: '2026-09-22T07:40:00Z',
  closed_at: null,
  summary_mid: null,
}
const LEGACY = [
  { id: 'r1', role: 'user' as const, content: 'What about the other 8?', ts: '2026-09-22T07:41:00Z' },
  { id: 'r2', role: 'assistant' as const, content: '5 are covered by open PRs.', ts: '2026-09-22T07:41:30Z' },
  { id: 'r3', role: 'assistant' as const, content: '3 are queued for Fixer.', ts: '2026-09-22T07:41:40Z' },
]

let qc: QueryClient
/** The panel reads the PARENT slot's live rows off the store for its read-only
 *  mirror, so every render needs one. `chat` preloads the rows a case wants the
 *  parent to be streaming. */
const renderPanel = (
  props: Partial<React.ComponentProps<typeof ThreadPanel>> = {},
  chat: Partial<RootState['chat']> = {},
) =>
  render(
    <Provider store={createTestStore(chat && Object.keys(chat).length ? { chat: { ...createTestStore().getState().chat, ...chat } } as Partial<RootState> : undefined)}>
      <QueryClientProvider client={qc}>
        <ThreadPanel slot="member-radar" mid="m-1" crewmateName="Radar" onClose={vi.fn()} {...props} />
      </QueryClientProvider>
    </Provider>,
  )

beforeEach(() => {
  vi.clearAllMocks()
  qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  mockDetail.mockResolvedValue({ parent: PARENT, anchor: ANCHOR, legacy_replies: [] })
  threadLiveStore.reset()
})

describe('ThreadPanel', () => {
  it('quotes the anchor and mounts the ordinary chat pane on the THREAD slot', async () => {
    renderPanel()
    expect(screen.getByRole('complementary', { name: 'Thread' })).toBeInTheDocument()
    await screen.findByTestId('thread-parent')
    expect(screen.getByTestId('thread-parent-bubble')).toHaveTextContent('Overnight triage')
    const pane = await screen.findByTestId('chat-pane')
    // The parent's slot is the ANCHOR's conversation; the pane renders the
    // thread's own session, which is a different slot. Getting these two the
    // wrong way round would show the parent chat inside its own thread.
    expect(pane).toHaveAttribute('data-slot', 'chat-77-1758524400')
    expect(pane).not.toHaveAttribute('data-slot', 'member-radar')
    // Framed by this panel, so the pane draws no title bar of its own.
    expect(pane).toHaveAttribute('data-frameless', '1')
  })

  it('mirrors the parent\'s live reply, read-only, while the parent turn runs', async () => {
    // A thread opened mid-flight otherwise quotes a sentence and a half and sends
    // the reader back to the main chat to see how the thing they are discussing
    // ended. The mirror is the parent slot's OWN streaming row off the store, so it
    // cannot drift from the main chat and carries no control that could reach the
    // parent's turn.
    renderPanel({}, {
      slotMessages: { 'member-radar': [{ role: 'streaming', content: 'Because three of them are', cls: 'msg msg-a' }] },
      slotRun: { 'member-radar': { state: 'streaming' } },
    } as Partial<RootState['chat']>)
    const live = await screen.findByTestId('thread-parent-live')
    expect(live).toBeInTheDocument()
    expect(screen.getByTestId('thread-parent-live-bubble')).toHaveTextContent('Because three of them are')
    // Read-only: no composer, no send, no stop inside the mirror.
    expect(live.querySelectorAll('button, textarea, input').length).toBe(0)
  })

  it('shows no mirror once the parent turn is idle', async () => {
    // The row it mirrors becomes an ordinary assistant row, which the quoted parent
    // above already covers -- two copies of one reply would read as two replies.
    renderPanel({}, {
      slotMessages: { 'member-radar': [{ role: 'assistant', content: 'Because three of them are dupes.', cls: 'msg msg-a' }] },
      slotRun: { 'member-radar': { state: 'idle' } },
    } as Partial<RootState['chat']>)
    await screen.findByTestId('thread-parent')
    expect(screen.queryByTestId('thread-parent-live')).toBeNull()
  })

  it('calls the version 1 fold "Earlier replies", never "Thread"', async () => {
    // "Thread" names a live session. A reader who meets the fold headed the same
    // word carries that meaning into every real thread after it.
    mockDetail.mockResolvedValue({ parent: PARENT, anchor: null, legacy_replies: LEGACY })
    renderPanel()
    await screen.findByTestId('thread-legacy')
    expect(screen.getByRole('complementary', { name: 'Earlier replies' })).toBeInTheDocument()
    expect(screen.queryByRole('complementary', { name: 'Thread' })).toBeNull()
  })

  it('keeps no composer, no reply lock and no byte cap of its own', async () => {
    renderPanel()
    await screen.findByTestId('chat-pane')
    // The composer, the send button, the "N replies" hairline, the streaming
    // bubble and the typing row all belonged to the bespoke bubble list. The
    // thread's composer is the PANE's, under the pane's own rules — so a reply
    // is not capped at 32 KB here and not gated on one answer at a time.
    for (const id of ['thread-composer', 'thread-reply-count', 'thread-reply-live', 'thread-replying', 'thread-too-long']) {
      expect(screen.queryByTestId(id)).toBeNull()
    }
    expect(screen.queryByRole('button', { name: 'Send reply' })).toBeNull()
  })

  it('takes the thread slot the host just opened, without waiting for the read', () => {
    // A thread opened a moment ago: the host has its slot from the 201 while the
    // anchor read is still in flight, and the pane must mount now rather than
    // after a round trip that says what the host already knows.
    mockDetail.mockReturnValue(new Promise(() => {}))
    renderPanel({ threadSlot: 'chat-90-1758524999' })
    expect(screen.getByTestId('chat-pane')).toHaveAttribute('data-slot', 'chat-90-1758524999')
  })

  it('takes the slot an anchor announcement names', async () => {
    // Another tab (or an agent) opened the thread. The frame carries the slot,
    // so this panel does not need its own refetch to render it.
    mockDetail.mockResolvedValue({ parent: PARENT, anchor: null, legacy_replies: [] })
    renderPanel()
    await screen.findByTestId('thread-parent')
    expect(screen.queryByTestId('chat-pane')).toBeNull()
    threadLiveStore.apply({
      slot: 'member-radar', mid: 'm-1', thread_slot: 'chat-31-1758525000', event: 'opened', title: 'From another tab',
    })
    expect((await screen.findByTestId('chat-pane')).getAttribute('data-slot')).toBe('chat-31-1758525000')
  })

  it('a REPLACEMENT thread supersedes the slot the host opened with', async () => {
    // The host's prop is captured when the panel opens and never changes. Another
    // tab closing and reopening this message mints a different session, and trusting
    // the stale prop would keep the pane on the ENDED transcript -- where the
    // reader's next message would land.
    renderPanel({ threadSlot: 'chat-ended-1758524000' })
    expect(screen.getByTestId('chat-pane')).toHaveAttribute('data-slot', 'chat-ended-1758524000')
    threadLiveStore.apply({
      slot: 'member-radar', mid: 'm-1', thread_slot: 'chat-replacement-1758526000', event: 'opened', title: 'Reopened elsewhere',
    })
    expect((await screen.findByTestId('chat-pane')).getAttribute('data-slot')).toBe('chat-replacement-1758526000')
  })

  it('an announcement that agrees with the host changes nothing', async () => {
    // The ordinary case: the prop still leads, so there is no flash and no churn
    // when the read or the frame simply confirms what the host already opened.
    renderPanel({ threadSlot: ANCHOR.thread_slot })
    threadLiveStore.apply({
      slot: 'member-radar', mid: 'm-1', thread_slot: ANCHOR.thread_slot, event: 'opened', title: 'The other eight',
    })
    expect((await screen.findByTestId('chat-pane')).getAttribute('data-slot')).toBe(ANCHOR.thread_slot)
  })

  it('renders a version 1 thread read-only, with one action and no session', async () => {
    mockDetail.mockResolvedValue({ parent: PARENT, anchor: null, legacy_replies: LEGACY })
    const onStartNew = vi.fn()
    renderPanel({ onStartNew })
    await screen.findByTestId('thread-legacy')
    // No pane: there is no session to render, and minting one to replay these
    // replies would assert a history that session never had.
    expect(screen.queryByTestId('chat-pane')).toBeNull()
    expect(screen.getByTestId('thread-legacy-notice')).toHaveTextContent('read-only')
    const rows = screen.getAllByTestId('thread-reply')
    expect(rows.map((r) => r.getAttribute('data-reply-from'))).toEqual(['user', 'assistant', 'assistant'])
    // Two consecutive crewmate replies share one run on the main chat's rule.
    expect(rows.map((r) => r.getAttribute('data-thread-run'))).toEqual([null, 'start', 'end'])
    expect(screen.getByText('You')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('thread-start-new'))
    expect(onStartNew).toHaveBeenCalledTimes(1)
  })

  it('shows both when a message has version 1 replies AND a live thread', async () => {
    mockDetail.mockResolvedValue({ parent: PARENT, anchor: ANCHOR, legacy_replies: LEGACY })
    renderPanel({ onStartNew: vi.fn() })
    await screen.findByTestId('thread-legacy')
    // The old replies are still reachable, and the live thread is the one being
    // typed into. "Start a new thread here" is gone: there is one already.
    expect(screen.getByTestId('chat-pane')).toHaveAttribute('data-slot', 'chat-77-1758524400')
    expect(screen.queryByTestId('thread-start-new')).toBeNull()
  })

  it('pops out to the thread slot, not the parent', async () => {
    const onOpenFull = vi.fn()
    renderPanel({ onOpenFull })
    await screen.findByTestId('thread-open-full')
    fireEvent.click(screen.getByTestId('thread-open-full'))
    expect(onOpenFull).toHaveBeenCalledWith('chat-77-1758524400')
  })

  it('offers no pop-out for a thread with no session to open', async () => {
    mockDetail.mockResolvedValue({ parent: PARENT, anchor: null, legacy_replies: LEGACY })
    renderPanel({ onOpenFull: vi.fn() })
    await screen.findByTestId('thread-legacy')
    expect(screen.queryByTestId('thread-open-full')).toBeNull()
  })

  it('says a closed thread is closed', async () => {
    mockDetail.mockResolvedValue({
      parent: PARENT,
      anchor: { ...ANCHOR, closed_at: '2026-09-22T09:00:00Z', summary_mid: 'm-9' },
      legacy_replies: [],
    })
    renderPanel()
    expect(await screen.findByTestId('thread-closed')).toHaveTextContent('Ended')
  })

  it('renders the display label wherever it names the speaker, while the name keeps seeding', async () => {
    mockDetail.mockResolvedValue({ parent: PARENT, anchor: null, legacy_replies: LEGACY })
    renderPanel({ crewmateName: 'radar', crewmateLabel: 'Radar Watch' })
    await screen.findByTestId('thread-parent')
    // The anchor's author line and the version 1 run's author line. The
    // immutable name appears nowhere as text -- it survives as the avatar seed.
    expect(screen.getAllByText('Radar Watch').length).toBeGreaterThanOrEqual(2)
    expect(screen.queryByText('radar')).not.toBeInTheDocument()
  })

  it('names a failed anchor read, keeps the thread itself, and retries in place', async () => {
    mockDetail.mockRejectedValueOnce(new ApiError(500, 'boom', ''))
    // The thread's slot came from the host, so the thread is usable even while
    // the anchor above it is unreadable: they are two different reads.
    renderPanel({ threadSlot: 'chat-77-1758524400' })
    await screen.findByTestId('thread-load-error')
    expect(screen.getByTestId('thread-load-error')).toHaveTextContent("Couldn't load this thread.")
    expect(screen.getByTestId('chat-pane')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('thread-load-retry'))
    await screen.findByTestId('thread-parent')
    expect(screen.queryByTestId('thread-load-error')).toBeNull()
    expect(mockDetail).toHaveBeenCalledTimes(2)
  })

  it('close hands the panel back', () => {
    const onClose = vi.fn()
    renderPanel({ onClose })
    fireEvent.click(screen.getByRole('button', { name: 'Close panel' }))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('Escape closes the panel and focus returns to the opener on unmount', async () => {
    const opener = document.createElement('button')
    opener.textContent = 'Reply in thread'
    document.body.appendChild(opener)
    opener.focus()
    const onClose = vi.fn()
    const view = renderPanel({ onClose })
    await screen.findByTestId('thread-parent')
    fireEvent.keyDown(screen.getByTestId('thread-panel'), { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
    view.unmount()
    expect(document.activeElement).toBe(opener)
    opener.remove()
  })

  it('leaves an IME composition to the composition, not to the panel', async () => {
    const onClose = vi.fn()
    renderPanel({ onClose })
    await screen.findByTestId('thread-parent')
    fireEvent.keyDown(screen.getByTestId('thread-panel'), { key: 'Escape', isComposing: true })
    expect(onClose).not.toHaveBeenCalled()
  })
})


describe('ThreadPanel end control', () => {
  it('offers End thread for a live session thread and calls the host', async () => {
    const onEnd = vi.fn()
    renderPanel({ threadSlot: 'chat-thread-1', onEnd })
    fireEvent.click(await screen.findByTestId('thread-end'))
    expect(onEnd).toHaveBeenCalledTimes(1)
  })

  it('ending and dismissing are two different controls', async () => {
    // The X shuts the drawer and reaches no thread; End closes the anchor and
    // posts the summary card. One control doing both would either end a thread
    // the reader only wanted out of the way, or leave a route nothing calls.
    const onEnd = vi.fn()
    const onClose = vi.fn()
    renderPanel({ threadSlot: 'chat-thread-1', onEnd, onClose })
    fireEvent.click(await screen.findByTestId('thread-end'))
    fireEvent.click(screen.getByRole('button', { name: 'Close panel' }))
    expect(onEnd).toHaveBeenCalledTimes(1)
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('says beside the button what ending does, not only in a tooltip', async () => {
    // "End" reads final. Whether it can be undone is what decides the press, so
    // it has to be readable without hovering -- a touch reader never gets a
    // tooltip at all, and a reader who cannot tell leaves the control alone.
    //
    // And it says it as an OUTCOME. The hint used to read "frees the message for
    // a new thread", which readers reported not understanding at all: "frees"
    // names a mechanism, and the question they were asking was what they would
    // still be able to do afterwards.
    renderPanel({ threadSlot: 'chat-thread-1', onEnd: vi.fn() })
    const hint = await screen.findByTestId('thread-end-hint')
    expect(hint.textContent).toContain('stays readable')
    expect(hint.textContent).toContain('start a new one')
    expect(screen.getByTestId('thread-end').getAttribute('title')).toBeNull()
  })

  it('withholds End for a version 1 fold, which has no session to end', async () => {
    mockDetail.mockResolvedValue({ parent: PARENT, anchor: null, legacy_replies: LEGACY })
    renderPanel({ onEnd: vi.fn() })
    await screen.findByTestId('thread-parent')
    expect(screen.queryByTestId('thread-end')).toBeNull()
  })

  it('withholds End once the thread is closed', async () => {
    mockDetail.mockResolvedValue({
      parent: PARENT,
      anchor: { ...ANCHOR, closed_at: '2026-09-29T08:00:00Z', summary_mid: 'm-card000000000001' },
      legacy_replies: [],
    })
    renderPanel({ threadSlot: 'chat-thread-1', onEnd: vi.fn() })
    await screen.findByTestId('thread-closed')
    expect(screen.queryByTestId('thread-end')).toBeNull()
  })

  it('a refused end says so and leaves the thread on screen', async () => {
    renderPanel({ threadSlot: 'chat-thread-1', onEnd: vi.fn(), endError: 'pages.chat.thread.err_end_failed' })
    expect(await screen.findByTestId('thread-end-error')).toBeTruthy()
    expect(screen.getByTestId('thread-end')).toBeTruthy()
  })
})


describe('ThreadPanel header shape', () => {
  it('keeps the header action row at two buttons, with End on its own row', async () => {
    // `max-two-buttons-per-row`. End is a peer action but not an icon button, so it
    // sits below the header beside its own refusal rather than making a third.
    renderPanel({ threadSlot: 'chat-thread-1', onEnd: vi.fn(), onOpenFull: vi.fn() })
    const end = await screen.findByTestId('thread-end')
    const openFull = screen.getByTestId('thread-open-full')
    expect(end.parentElement).not.toBe(openFull.parentElement)
    expect(openFull.parentElement?.querySelectorAll('button').length).toBe(2)
  })

  it('says the quoted anchor comes from the main chat', async () => {
    renderPanel({ threadSlot: 'chat-thread-1' })
    await screen.findByTestId('thread-parent')
    expect(screen.getByTestId('thread-anchor-origin').textContent).toBe('From the main chat')
  })
})

/**
 * The anchor region cannot be allowed to take the panel.
 *
 * Measured on a pod build before this: a thread opened on a long reply rendered
 * 5 242 px of quoted anchor inside a 940 px drawer, which left `thread-surface`
 * -- the thread's transcript AND its composer -- 0 px tall with the composer
 * scrolled off the panel. Two probes typed into it and both landed nothing. So
 * this is not about looks: an unbounded anchor makes a thread on any long message
 * unusable.
 *
 * jsdom has no layout, so heights here are all 0 and cannot be the assertion.
 * What IS assertable is the contract that produces the height: the region is
 * capped and scrolls its own overflow, the quote is clamped until asked, and the
 * live mirror -- the one part that grows while the reader watches -- is a capped
 * scroll box of its own.
 */
describe('ThreadPanel anchor bounds', () => {
  const LONG = 'x'.repeat(10_000)

  it('caps the anchor region and scrolls it, so the surface keeps a share of the panel', async () => {
    mockDetail.mockResolvedValue({
      parent: { ...PARENT, content: LONG },
      anchor: ANCHOR,
      legacy_replies: [],
    })
    renderPanel({ threadSlot: 'chat-thread-1' })
    const region = await screen.findByTestId('thread-anchor')
    expect(region.className).toMatch(/max-h-\[45%\]/)
    expect(region.className).toMatch(/overflow-y-auto/)
    // The thread's own surface is still rendered as a flex child that can grow,
    // which is what it has to be for the cap above to leave it anything.
    expect(screen.getByTestId('thread-surface').className).toMatch(/flex-1/)
  })

  it('clamps the quote until the reader asks for the rest', async () => {
    mockDetail.mockResolvedValue({
      parent: { ...PARENT, content: LONG },
      anchor: ANCHOR,
      legacy_replies: [],
    })
    renderPanel({ threadSlot: 'chat-thread-1' })
    const quote = await screen.findByTestId('thread-anchor-quote')
    expect(quote).toHaveAttribute('data-expanded', 'false')
    expect(quote.className).toMatch(/overflow-hidden/)

    const toggle = screen.getByTestId('thread-anchor-toggle')
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(toggle)
    expect(screen.getByTestId('thread-anchor-quote')).toHaveAttribute('data-expanded', 'true')
    expect(screen.getByTestId('thread-anchor-quote').className || '').not.toMatch(/overflow-hidden/)
    expect(screen.getByTestId('thread-anchor-toggle')).toHaveAttribute('aria-expanded', 'true')

    // And back: the clamp is a toggle, not a one-way door.
    fireEvent.click(screen.getByTestId('thread-anchor-toggle'))
    expect(screen.getByTestId('thread-anchor-quote')).toHaveAttribute('data-expanded', 'false')
  })

  it('offers the toggle on a short anchor too, rather than measuring whether it is needed', async () => {
    // The height a clamped box WOULD have is a layout read. Gating the only route
    // to the full text on one means a message that measures short by a pixel
    // silently loses its control, and the reader has no way to know it existed.
    renderPanel({ threadSlot: 'chat-thread-1' })
    await screen.findByTestId('thread-parent')
    expect(screen.getByTestId('thread-anchor-toggle')).toBeInTheDocument()
  })

  it('gives the live mirror its own capped scroll box', async () => {
    renderPanel({ threadSlot: 'chat-thread-1' }, {
      slotMessages: { 'member-radar': [{ role: 'streaming', content: LONG, cls: 'msg msg-a' }] },
      slotRun: { 'member-radar': { state: 'streaming' } },
    } as Partial<RootState['chat']>)
    const scroll = await screen.findByTestId('thread-parent-live-scroll')
    expect(scroll.className).toMatch(/max-h-40/)
    expect(scroll.className).toMatch(/overflow-y-auto/)
  })
})

/**
 * A thread opened on a reply that is still streaming anchors to the USER message
 * that started the turn, because a streaming row has no `mid` to hang an anchor
 * off yet. That is deliberate. What was wrong was the reading order: the reader
 * pressed the opener under an ANSWER and the panel's first line was their own
 * question, so the pane led with the thing they had not clicked.
 */
describe('ThreadPanel mid-stream reading order', () => {
  const streaming = {
    slotMessages: { 'member-radar': [{ role: 'streaming', content: 'Because three of them are', cls: 'msg msg-a' }] },
    slotRun: { 'member-radar': { state: 'streaming' } },
  } as Partial<RootState['chat']>

  it('leads with the streaming reply when the anchor is the user message', async () => {
    mockDetail.mockResolvedValue({
      parent: { ...PARENT, role: 'user', content: 'Explain in 40 numbered points.' },
      anchor: ANCHOR,
      legacy_replies: [],
    })
    renderPanel({ threadSlot: 'chat-thread-1' }, streaming)
    // The mirror renders off the store immediately, the anchor arrives from the
    // query -- and the order depends on the anchor's role, so wait for it.
    await screen.findByTestId('thread-parent')
    const live = screen.getByTestId('thread-parent-live')
    expect(live).toHaveAttribute('data-lead', 'true')
    expect(live.className).toMatch(/order-first/)
    // The question is still there, underneath, as the context it is.
    expect(screen.getByTestId('thread-parent-bubble')).toHaveTextContent('Explain in 40 numbered points.')
  })

  it('does not reorder when the anchor is the reply itself', async () => {
    // A finished reply quotes that reply, so the mirror is the continuation of
    // what the quote already shows and leading with it would say it twice.
    renderPanel({ threadSlot: 'chat-thread-1' }, streaming)
    await screen.findByTestId('thread-parent')
    const live = screen.getByTestId('thread-parent-live')
    expect(live).toHaveAttribute('data-lead', 'false')
    expect(live.className).not.toMatch(/order-first/)
  })
})

describe('ThreadPanel title', () => {
  it('does not render the word Thread twice for an untitled thread', async () => {
    // `_default_title` stores "Thread: <first words>" and the heading beside it
    // already says "Thread", so the two rendered together read
    // "Thread  Thread: Amazon".
    mockDetail.mockResolvedValue({
      parent: PARENT,
      anchor: { ...ANCHOR, title: 'Thread: Amazon' },
      legacy_replies: [],
    })
    renderPanel({ threadSlot: 'chat-thread-1', threadTitle: 'Thread: Amazon' })
    await screen.findByTestId('thread-parent')
    const panel = screen.getByRole('complementary', { name: 'Thread' })
    expect(panel.textContent).toContain('Amazon')
    expect(panel.textContent).not.toContain('Thread: Amazon')
  })
})

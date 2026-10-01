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
  mockDetail.mockResolvedValue({ parent: PARENT, anchor: ANCHOR })
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
    mockDetail.mockResolvedValue({ parent: PARENT, anchor: null })
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

  it('a late close for a replaced thread does not drag the pane onto the ended one', async () => {
    // The close frame can arrive AFTER the replacement is open. Taken as the newer
    // truth it would mount the pane on the finished session and send the reader's
    // next message there, so the anchor's answer wins when the frame disagrees and
    // reports itself closed.
    renderPanel({ threadSlot: ANCHOR.thread_slot })
    threadLiveStore.apply({
      slot: 'member-radar', mid: 'm-1', thread_slot: 'chat-ended-1758524000', event: 'closed', title: 'Ended elsewhere',
    })
    expect((await screen.findByTestId('chat-pane')).getAttribute('data-slot')).toBe(ANCHOR.thread_slot)
  })

  it('says a version 1 anchor is not shown here yet, and offers a real thread', async () => {
    // Its replies live in a sidecar with no session to render, and the read-only
    // fold that draws them ships separately (NOTES D3). The sidecar is untouched,
    // so this states what the panel holds and offers the one action that works.
    mockDetail.mockResolvedValue({ parent: PARENT, anchor: null })
    const onStartNew = vi.fn()
    renderPanel({ onStartNew })
    await screen.findByTestId('thread-empty')
    expect(screen.queryByTestId('chat-pane')).toBeNull()
    expect(screen.queryByTestId('thread-reply')).toBeNull()
    fireEvent.click(await screen.findByTestId('thread-start-new'))
    expect(onStartNew).toHaveBeenCalledTimes(1)
  })
  it('pops out to the thread slot, not the parent', async () => {
    const onOpenFull = vi.fn()
    renderPanel({ onOpenFull })
    await screen.findByTestId('thread-open-full')
    fireEvent.click(screen.getByTestId('thread-open-full'))
    expect(onOpenFull).toHaveBeenCalledWith('chat-77-1758524400')
  })

  it('renders the display label wherever it names the speaker, while the name keeps seeding', async () => {
    mockDetail.mockResolvedValue({ parent: PARENT, anchor: ANCHOR })
    renderPanel({ crewmateName: 'radar', crewmateLabel: 'Radar Watch' })
    await screen.findByTestId('thread-parent')
    // The anchor's author line names the label; the immutable name appears nowhere
    // as text -- it survives as the avatar seed.
    expect(screen.getAllByText('Radar Watch').length).toBeGreaterThanOrEqual(1)
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

  it('withholds End for a version 1 fold, which has no session to end', async () => {
    mockDetail.mockResolvedValue({ parent: PARENT, anchor: null })
    renderPanel({ onEnd: vi.fn() })
    await screen.findByTestId('thread-parent')
    expect(screen.queryByTestId('thread-end')).toBeNull()
  })

  it('a refused end says so and leaves the thread on screen', async () => {
    renderPanel({ threadSlot: 'chat-thread-1', onEnd: vi.fn(), endError: 'pages.chat.thread.err_end_failed' })
    expect(await screen.findByTestId('thread-end-error')).toBeTruthy()
    expect(screen.getByTestId('thread-end')).toBeTruthy()
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


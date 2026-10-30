/**
 * What the panel TELLS the user when a send is blocked because the restore read
 * has not landed.
 *
 * The behaviour it is blocked by lives in `SideChat.restore.test.tsx`: a question
 * is never sent ahead of the read, and it is handed back. These cases are about
 * the words, because the two notices the panel used to show were both wrong. The
 * failed-read notice promised a wait the code does not do ("New questions wait
 * until it loads", read as queued), and the send itself reported
 * `pages.chatPage.send_failed_connection` -- sending the user to check a
 * connection that is working, for a send the connection never touched.
 *
 * The third case the fix needs, "Retry loads the conversation and a question
 * asked after it goes through", is already pinned by
 * `SideChat.restore.test.tsx`'s `it('holds questions back while the read has
 * failed, and lets them through once Retry loads it')`, so it is not repeated
 * here.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import type { SideSnapshot } from '../types'
import { api } from '../api/client'
import { clearSideChatDrafts, readSideChatDraft, writeSideChatDraft } from '../chat-core/composer/sideChatDrafts'
import { SEND_ABORT_MS } from '../chat-core/transport/sendTurn'
import { i18nT } from '../i18n/t'
import SideChat from '../pages/chat/SideChat'
import { renderWithProviders, createConnectedTestStore } from './helpers'

vi.mock('../api/client', () => ({
  api: {
    sideState: vi.fn(),
    sideTools: vi.fn().mockResolvedValue({ read_only_tools: true, claude_adapter_outdated: false, config_unavailable: false }),
    sideOpen: vi.fn().mockResolvedValue({ ok: true, open: true, messages: 0, last_run_id: '', created_at: '' }),
    sideTurn: vi.fn().mockResolvedValue({ ok: true, run_id: 'r9', messages: 1 }),
    sideClose: vi.fn().mockResolvedValue({ ok: true, was_open: true }),
  },
}))

const SLOT = 'slot-1'

/** The copy this fix exists to keep OFF a blocked send, read from the catalog so
 *  the assertion tracks the key rather than a transcribed sentence. */
const CONNECTION_COPY = i18nT('pages.chatPage.send_failed_connection') as string
/** The cause a blocked send reports instead. */
const BLOCKED_REASON = i18nT('pages.chat.sideChat.send_blocked_not_loaded') as string

describe('SideChat: a send blocked by the restore read says why', () => {
  beforeEach(() => {
    clearSideChatDrafts()
    vi.mocked(api.sideState).mockReset()
    vi.mocked(api.sideTurn).mockClear()
    vi.mocked(api.sideOpen).mockClear()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  /** The footer's tool note is the panel's last asynchronous update on mount. */
  const footerSettled = async () => { expect(await screen.findByRole('note')).toBeInTheDocument() }
  const flush = () => act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })

  it('tells a failed read what it does to a question instead of promising it a wait', async () => {
    vi.mocked(api.sideState).mockRejectedValue(new Error('offline'))
    const store = createConnectedTestStore()
    const { container } = renderWithProviders(<SideChat slot={SLOT} />, { store, queryDefaults: { retry: false } })
    const notice = await screen.findByTestId('side-chat-restore-failed')
    await footerSettled()
    // Says what to do and what happens if you do not. No wait, no queue.
    expect(notice).toHaveTextContent('Retry before asking')
    expect(notice).toHaveTextContent('comes back to the composer instead')
    expect(notice).not.toHaveTextContent('wait until it loads')

    act(() => { writeSideChatDraft(SLOT, 'what did I ask?') })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))
    // The send tries the read again; it fails again, so nothing is asked.
    await waitFor(() => expect(readSideChatDraft(SLOT)).toBe('what did I ask?'))
    await waitFor(() => expect(container.textContent).toContain(BLOCKED_REASON))
    expect(container.textContent).not.toContain(CONNECTION_COPY)
    expect(api.sideTurn).not.toHaveBeenCalled()
    await flush()
  })

  it('says the conversation has not loaded when the send outruns a read still in flight', async () => {
    // No restore notice here: the read has not failed, it simply has not
    // answered, so the send's own row is the only thing that can say why the
    // text came back.
    vi.useFakeTimers()
    // Held, not left hanging: `restoreSideConversation` shares a read in flight
    // per slot until it settles, so a read this test never settled would be the
    // read every later test in this file awaits.
    let failRead: (e: unknown) => void = () => {}
    vi.mocked(api.sideState).mockReturnValue(new Promise<SideSnapshot>((_resolve, reject) => { failRead = reject }))
    const store = createConnectedTestStore()
    const { container } = renderWithProviders(<SideChat slot={SLOT} />, { store, queryDefaults: { retry: false } })
    await act(async () => { await vi.advanceTimersByTimeAsync(50) })

    act(() => { writeSideChatDraft(SLOT, 'asked too early') })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /send/i })) })
    expect(api.sideTurn).not.toHaveBeenCalled()

    await act(async () => { await vi.advanceTimersByTimeAsync(SEND_ABORT_MS + 50) })
    expect(readSideChatDraft(SLOT)).toBe('asked too early')
    expect(screen.queryByTestId('side-chat-restore-failed')).toBeNull()
    expect(container.textContent).toContain(BLOCKED_REASON)
    expect(container.textContent).not.toContain(CONNECTION_COPY)
    expect(api.sideTurn).not.toHaveBeenCalled()
    expect(store.getState().chat.slotSide[SLOT]?.messages ?? []).toEqual([])

    await act(async () => {
      failRead(new Error('offline'))
      await vi.advanceTimersByTimeAsync(50)
    })
    expect(screen.getByTestId('side-chat-restore-failed')).toBeInTheDocument()
  })

  it('asks the gateway again on the next send after one outran a read still in flight', async () => {
    vi.useFakeTimers()
    let failRead: (e: unknown) => void = () => {}
    vi.mocked(api.sideState)
      .mockReturnValueOnce(new Promise<SideSnapshot>((_resolve, reject) => { failRead = reject }))
      .mockResolvedValue({ open: false })
    try {
      const store = createConnectedTestStore()
      renderWithProviders(<SideChat slot={SLOT} />, { store, queryDefaults: { retry: false } })
      await act(async () => { await vi.advanceTimersByTimeAsync(50) })

      act(() => { writeSideChatDraft(SLOT, 'asked too early') })
      await act(async () => { fireEvent.click(screen.getByRole('button', { name: /send/i })) })
      await act(async () => { await vi.advanceTimersByTimeAsync(SEND_ABORT_MS + 50) })
      expect(readSideChatDraft(SLOT)).toBe('asked too early')
      expect(api.sideTurn).not.toHaveBeenCalled()

      await act(async () => { fireEvent.click(screen.getByRole('button', { name: /send/i })) })
      await act(async () => { await vi.advanceTimersByTimeAsync(50) })
      expect(api.sideState).toHaveBeenCalledTimes(2)
      expect(api.sideTurn).toHaveBeenCalledWith(SLOT, 'asked too early', undefined)

      await act(async () => {
        failRead(new Error('offline'))
        await vi.advanceTimersByTimeAsync(50)
      })
      // The fresh read settled the slot, so the stalled read's failure shows no notice.
      expect(screen.queryByTestId('side-chat-restore-failed')).toBeNull()
    } finally {
      // Settle the stalled read, so a read in flight never outlives this test.
      failRead(new Error('test over'))
    }
  })
})

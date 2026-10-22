/**
 * useSessionActions — the single hook backing the surface-agnostic session
 * actions (duplicate / mark-read / pin / copy-link / move / close) shared by all
 * four session-menu surfaces AND the sidebar row's non-menu Duplicate/Close
 * buttons. It is the highest-fan-in unit, so it gets its own test (its siblings
 * collapseGroups / orderFoldersWithPaths / useMoveSlotToFolder already have
 * theirs).
 *
 * Priority: the two behaviors whose state source matters —
 *   - toggleRead reads store.getState().dashboard.unreadSlots
 *   - pin rollback reads store.getState().dashboard.slots[].pinned
 *
 * Pattern mirrors ChatSidebar.moveToFolder.test.tsx: renderHook wrapped in a
 * Provider over the app's singleton store (so the hook's store.getState() reads
 * the seeded state) plus a QueryClientProvider for the useMutation calls.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import type { ChatSlot } from '../types'
import enManual from '../i18n/locales/en.manual.json'

const mocks = vi.hoisted(() => ({ setSlotPin: vi.fn(), forkChatSlot: vi.fn(), chatSlots: vi.fn(), dashboardConfig: vi.fn() }))
// The client mock re-exports the real ApiError, so the hook's instanceof check
// and apiErrorCode's both see the refusals these tests build.
vi.mock('../api/client', async () => {
  const { ApiError } = await import('../api/apiError')
  return {
    SEARCH_MIN_CHARS: 2,
    ApiError,
    api: new Proxy(mocks as Record<string, unknown>, {
      get: (t, p: string) => (p in t ? t[p] : vi.fn().mockResolvedValue([])),
    }),
  }
})

// close() gates on loadChatConfig().confirmCloseSession — mock it so each test
// controls the branch deterministically (no localStorage dependency).
const cfgMock = vi.hoisted(() => ({ loadChatConfig: vi.fn(() => ({ confirmCloseSession: false })) }))
vi.mock('../pages/chat/ChatSettings', () => cfgMock)

import { store } from '../store'
import { ApiError } from '../api/apiError'
import { sseSlots, sseSlotPatch, markSlotUnread, updateSlotPin } from '../store/dashboardSlice'
import { useSessionActions } from '../hooks/useSessionActions'
import { recordError, __resetErrorJournalForTests } from '../utils/errorReport'

/** A refusal as apiFailure (api/client.ts) builds it: `.message` is the friendly text, `.body` the raw body. */
const refusal = (status: number, body: string, message: string) => new ApiError(status, message, body)

const SLOT = 'chat-actions-1'

function seed(pinned = false) {
  const slot: ChatSlot = { key: SLOT, title: SLOT, messages: 0, running: false, folder_id: '' }
  store.dispatch(sseSlots([slot]))
  store.dispatch(updateSlotPin({ key: SLOT, pinned }))
}
const slotOf = () => store.getState().dashboard.slots.find(s => s.key === SLOT)
const unread = () => store.getState().dashboard.unreadSlots.includes(SLOT)

function renderActions() {
  const qc = new QueryClient({ defaultOptions: { mutations: { retry: false }, queries: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <Provider store={store}><QueryClientProvider client={qc}>{children}</QueryClientProvider></Provider>
  )
  return renderHook(() => useSessionActions('personal'), { wrapper }).result
}

beforeEach(() => {
  mocks.setSlotPin.mockResolvedValue({})
  mocks.forkChatSlot.mockResolvedValue({ ok: true, key: 'forked' })
  mocks.chatSlots.mockResolvedValue([])
  mocks.dashboardConfig.mockResolvedValue({})
  cfgMock.loadChatConfig.mockReturnValue({ confirmCloseSession: false })
  vi.stubGlobal('confirm', vi.fn(() => true))
})
afterEach(() => {
  vi.clearAllMocks()
  vi.unstubAllGlobals()
  store.dispatch(sseSlots([]))
})

describe('useSessionActions', () => {
  function overCapacity() {
    return new ApiError(400, 'too large', JSON.stringify({ code: 'fork_corpus_too_large' }))
  }

  it('reports a refused duplicate as state for the caller to render, raising no dialog', async () => {
    seed()
    mocks.forkChatSlot.mockRejectedValue(overCapacity())
    const nativeDialog = vi.fn()
    vi.stubGlobal('alert', nativeDialog)
    const result = renderActions()

    act(() => result.current.duplicate(SLOT))

    await waitFor(() => expect(result.current.forkError).toBeTruthy())
    expect(result.current.forkError?.message).toContain('is too large to duplicate whole')
    // Attributed to the row it was invoked on, which is where it renders.
    expect(result.current.forkError?.slotKey).toBe(SLOT)
    expect(nativeDialog).not.toHaveBeenCalled()
  })

  it('clears a stale refusal once a later duplicate succeeds', async () => {
    seed()
    mocks.forkChatSlot.mockRejectedValue(overCapacity())
    const result = renderActions()

    act(() => result.current.duplicate(SLOT))
    await waitFor(() => expect(result.current.forkError).toBeTruthy())

    // The advised recovery is to fork at a message, which succeeds. A refusal left
    // under the row outlives the condition it described and contradicts the new tab.
    mocks.forkChatSlot.mockResolvedValue({ ok: true, key: 'chat-2' })
    act(() => result.current.duplicate(SLOT))

    await waitFor(() => expect(result.current.forkError).toBeNull())
  })

  it('names the refused session, since the notice renders away from its row', async () => {
    seed()
    mocks.forkChatSlot.mockRejectedValue(overCapacity())
    const result = renderActions()

    act(() => result.current.duplicate(SLOT))

    await waitFor(() => expect(result.current.forkError).toBeTruthy())
    // Without a referent "Open it" points at nothing: the notice sits at the panel root,
    // not under the row that was pressed. The seeded session is untitled (its title is
    // its key), so it is named "Untitled session", never by its raw key (#23084).
    expect(result.current.forkError?.message).toContain('Untitled session')
    expect(result.current.forkError?.message).not.toContain(SLOT)
    expect(result.current.forkError?.message).not.toContain('{{title}}')
  })

  it('names a titled refused session by its title', async () => {
    // Its own key: a close in an earlier test can leave a close hold on SLOT that
    // filters SLOT's row out of a later slot list.
    const TITLED = 'chat-actions-titled'
    store.dispatch(sseSlots([{ key: TITLED, title: 'Fix the build', messages: 0, running: false, folder_id: '' }]))
    mocks.forkChatSlot.mockRejectedValue(overCapacity())
    const result = renderActions()

    act(() => result.current.duplicate(TITLED))

    await waitFor(() => expect(result.current.forkError).toBeTruthy())
    expect(result.current.forkError?.message).toContain('Fix the build')
  })

  it('opens with the verb of the Duplicate control the reader pressed', async () => {
    seed()
    mocks.forkChatSlot.mockRejectedValue(overCapacity())
    const result = renderActions()

    act(() => result.current.duplicate(SLOT))

    await waitFor(() => expect(result.current.forkError).toBeTruthy())
    const msg = result.current.forkError!.message
    // Answering a press on Duplicate with "too large to fork" sends the reader hunting for
    // a control they never used; the recovery step still names the one that exists.
    expect(msg).toContain('too large to duplicate whole')
    expect(msg).not.toContain('too large to fork')
    expect(msg).toContain('Open it')
    expect(msg).toContain('Fork conversation from here')
  })

  it('advises neutrally without reading config, whatever the fork direction', async () => {
    seed()
    mocks.forkChatSlot.mockRejectedValue(overCapacity())
    mocks.dashboardConfig.mockResolvedValue({ tail_fork_enabled: true })
    const result = renderActions()

    act(() => result.current.duplicate(SLOT))

    await waitFor(() => expect(result.current.forkError).toBeTruthy())
    expect(result.current.forkError?.message).not.toContain('later message')
    expect(result.current.forkError?.message).not.toContain('earlier message')
    expect(result.current.forkError?.message).toContain('end you want to keep')
    expect(mocks.dashboardConfig).not.toHaveBeenCalled()
  })

  it('still surfaces the refusal when the config lookup fails too', async () => {
    seed()
    mocks.forkChatSlot.mockRejectedValue(overCapacity())
    mocks.dashboardConfig.mockRejectedValue(new Error('config unavailable'))
    const result = renderActions()

    act(() => result.current.duplicate(SLOT))

    await waitFor(() => expect(result.current.forkError).toBeTruthy())
    expect(result.current.forkError?.message).toContain('is too large to duplicate whole')
    expect(result.current.forkError?.message).not.toContain('earlier message')
    expect(result.current.forkError?.message).not.toContain('later message')
  })

  it('keeps the structured report the localized line can no longer be matched to', async () => {
    seed()
    __resetErrorJournalForTests()
    // The journal is keyed on the RAW wire text, which the localized copy replaces.
    const journalled = recordError({
      source: 'api', message: 'too large', status: 400,
      code: 'fork_corpus_too_large', endpoint: '/api/chat/slots/fork',
    })
    mocks.forkChatSlot.mockRejectedValue(overCapacity())
    const result = renderActions()

    act(() => result.current.duplicate(SLOT))

    await waitFor(() => expect(result.current.forkError).toBeTruthy())
    expect(result.current.forkError?.message).toContain('is too large to duplicate whole')
    expect(result.current.forkError?.report?.id).toBe(journalled.id)
    expect(result.current.forkError?.report?.code).toBe('fork_corpus_too_large')
    expect(result.current.forkError?.report?.endpoint).toBe('/api/chat/slots/fork')
  })

  it('does not tell an off-transcript reader to pick a message that is not there', async () => {
    seed()
    mocks.forkChatSlot.mockRejectedValue(overCapacity())
    const result = renderActions()

    act(() => result.current.duplicate(SLOT))

    await waitFor(() => expect(result.current.forkError).toBeTruthy())
    // The session list shows no message list, so "Pick an earlier message" names an
    // affordance the reader cannot reach; and it must not answer Duplicate with "fork".
    expect(result.current.forkError?.message).not.toContain('Pick an')
    expect(result.current.forkError?.message).not.toContain('too large to fork')
    expect(result.current.forkError?.message).toContain('Open it')
    // The recovery must still name a FORK, not the clipboard Copy the Duplicate icon
    // otherwise suggests -- and it names the control, which is what the reader clicks.
    expect(result.current.forkError?.message).toContain('Fork conversation from here')
  })

  it('clears a refused duplicate on dismiss', async () => {
    seed()
    mocks.forkChatSlot.mockRejectedValue(overCapacity())
    const result = renderActions()
    act(() => result.current.duplicate(SLOT))
    await waitFor(() => expect(result.current.forkError).toBeTruthy())

    act(() => result.current.clearForkError())

    expect(result.current.forkError).toBeNull()
  })

  it('toggleRead flips based on dashboard.unreadSlots', () => {
    seed()
    store.dispatch(markSlotUnread(SLOT))          // start unread
    expect(unread()).toBe(true)
    const a = renderActions()
    act(() => a.current.toggleRead(SLOT))          // unread -> read
    expect(unread()).toBe(false)
    act(() => a.current.toggleRead(SLOT))          // read -> unread
    expect(unread()).toBe(true)
  })

  it('togglePin optimistically pins then rolls back when setSlotPin rejects', async () => {
    mocks.setSlotPin.mockRejectedValueOnce(new Error('boom'))
    mocks.chatSlots.mockResolvedValue([
      { key: SLOT, title: SLOT, messages: 0, running: false, folder_id: '', pinned: false } as ChatSlot,
    ])
    seed(false)
    const a = renderActions()
    act(() => a.current.togglePin(SLOT))
    expect(slotOf()?.pinned).toBe(true)                        // optimistic update
    await waitFor(() => expect(slotOf()?.pinned).toBe(false))  // rolled back on failure
  })

  it('togglePin persists when setSlotPin succeeds', async () => {
    mocks.chatSlots.mockResolvedValue([
      { key: SLOT, title: SLOT, messages: 0, running: false, folder_id: '', pinned: true } as ChatSlot,
    ])
    seed(false)
    const a = renderActions()
    act(() => a.current.togglePin(SLOT))
    expect(slotOf()?.pinned).toBe(true)
    await waitFor(() => expect(mocks.setSlotPin).toHaveBeenCalledWith(SLOT, true))
    expect(slotOf()?.pinned).toBe(true)                        // no rollback
  })

  it('togglePin reconciles from the PATCH answer without re-reading the slot list', async () => {
    mocks.setSlotPin.mockResolvedValue({ ok: true, pinned: true })
    seed(false)
    const a = renderActions()
    act(() => a.current.togglePin(SLOT))
    await waitFor(() => expect(mocks.setSlotPin).toHaveBeenCalledWith(SLOT, true))
    await waitFor(() => expect(slotOf()?.pinned).toBe(true))
    await new Promise(resolve => setTimeout(resolve, 20))
    expect(mocks.chatSlots).not.toHaveBeenCalled()
  })

  it('togglePin re-reads the slot list after a stale full frame reverts the row', async () => {
    let answer: (value: unknown) => void = () => {}
    mocks.setSlotPin.mockReturnValue(new Promise(resolve => { answer = resolve }))
    mocks.chatSlots.mockResolvedValue([
      { key: SLOT, title: SLOT, messages: 0, running: false, folder_id: '', pinned: true } as ChatSlot,
    ])
    seed(false)
    const a = renderActions()
    act(() => a.current.togglePin(SLOT))
    await waitFor(() => expect(mocks.setSlotPin).toHaveBeenCalled())
    // A list serialized before the PATCH committed lands while it is in flight.
    act(() => { store.dispatch(sseSlots([{ key: SLOT, title: SLOT, messages: 0, running: false, folder_id: '', pinned: false }])) })
    expect(slotOf()?.pinned).toBe(false)
    await act(async () => { answer({ ok: true, pinned: true }) })
    await waitFor(() => expect(mocks.chatSlots).toHaveBeenCalled())
    await waitFor(() => expect(slotOf()?.pinned).toBe(true))
  })

  it('togglePin keeps a later full-frame unpin over the PATCH answer', async () => {
    let answer: (value: unknown) => void = () => {}
    mocks.setSlotPin.mockReturnValue(new Promise(resolve => { answer = resolve }))
    mocks.chatSlots.mockResolvedValue([
      { key: SLOT, title: SLOT, messages: 0, running: false, folder_id: '', pinned: false } as ChatSlot,
    ])
    seed(false)
    const a = renderActions()
    act(() => a.current.togglePin(SLOT))
    await waitFor(() => expect(mocks.setSlotPin).toHaveBeenCalled())
    // Another tab's full snapshot unpins after this tab's PATCH committed.
    act(() => { store.dispatch(sseSlots([{ key: SLOT, title: SLOT, messages: 0, running: false, folder_id: '', pinned: false }])) })
    await act(async () => { answer({ ok: true, pinned: true }) })
    await waitFor(() => expect(mocks.chatSlots).toHaveBeenCalled())
    await waitFor(() => expect(slotOf()?.pinned).toBe(false))
  })

  it('togglePin keeps a newer single-row write over its own answer', async () => {
    let answer: (value: unknown) => void = () => {}
    mocks.setSlotPin.mockReturnValue(new Promise(resolve => { answer = resolve }))
    seed(false)
    const a = renderActions()
    act(() => a.current.togglePin(SLOT))
    await waitFor(() => expect(mocks.setSlotPin).toHaveBeenCalled())
    // Another tab unpinned after this PATCH committed; its slot_patch arrives
    // before this tab's HTTP answer.
    act(() => { store.dispatch(sseSlotPatch({ slots: [{ key: SLOT, pinned: false }] })) })
    await act(async () => { answer({ ok: true, pinned: true }) })
    await new Promise(resolve => setTimeout(resolve, 20))
    expect(slotOf()?.pinned).toBe(false)
    expect(mocks.chatSlots).not.toHaveBeenCalled()
  })

  it('duplicate alerts the keyed slot-cap copy on a 429 refusal instead of failing silently', async () => {
    // The slot-cap 429 is matched on its body code; the shared slot-cap guidance
    // that friendlyErrText puts on err.message follows the Duplicate subject, so
    // the raw "slot cap reached" reason never shows.
    const guidance = 'Too many sessions are open. Close a few old sessions (they stay in History and reopen anytime), then try again.'
    mocks.forkChatSlot.mockRejectedValueOnce(refusal(429, JSON.stringify({ error: 'slot cap reached (500)', code: 'slot_cap_reached' }), guidance))
    const alertSpy = vi.fn()
    vi.stubGlobal('alert', alertSpy)
    seed()
    const a = renderActions()
    act(() => a.current.duplicate(SLOT))
    await waitFor(() => expect(alertSpy).toHaveBeenCalledTimes(1))
    expect(alertSpy.mock.calls[0][0]).toBe(
      "Couldn't duplicate this session. Too many sessions are open. Close a few old sessions (they stay in History and reopen anytime), then try again.",
    )
  })

  it('duplicate follows a sign-in denial with the shared sign-in remedy and no retry sentence', async () => {
    // apiFailure sets authRequired and puts the shared sign-in text on
    // err.message; the alert carries that one copy after the subject, and no
    // retry succeeds until the user signs in, so there is no "Try again in a moment".
    const signIn = enManual.api.client.session_expired_sign_in_again
    mocks.forkChatSlot.mockRejectedValueOnce(new ApiError(403, signIn, 'invalid signature', true))
    const alertSpy = vi.fn()
    vi.stubGlobal('alert', alertSpy)
    seed()
    const a = renderActions()
    act(() => a.current.duplicate(SLOT))
    await waitFor(() => expect(alertSpy).toHaveBeenCalledTimes(1))
    expect(alertSpy.mock.calls[0][0]).toBe(`Couldn't duplicate this session. ${signIn}`)
    expect(alertSpy.mock.calls[0][0]).not.toContain('Try again in a moment')
    // The shared text names the sign-in as what lapsed. "Session expired" right
    // after "Couldn't duplicate this session." reads as the chat session having
    // died, which sends the reader to History instead of to sign in again.
    expect(alertSpy.mock.calls[0][0]).toContain('Your sign-in is no longer valid.')
    expect(alertSpy.mock.calls[0][0]).not.toMatch(/session expired/i)
  })

  it('duplicate keeps the stale-owner explanation on a stale-owner sign-in denial', async () => {
    // A stale-owner 401 is a gateway sign-in denial too; apiFailure puts the
    // stale-owner text on err.message, and the alert keeps that explanation
    // rather than a generic sign-in sentence.
    const staleOwner = enManual.api.client.stale_owner_session_sign_in_again
    mocks.forkChatSlot.mockRejectedValueOnce(
      new ApiError(401, staleOwner, JSON.stringify({ error: 'stale session', code: 'stale_session_reauth' }), true),
    )
    const alertSpy = vi.fn()
    vi.stubGlobal('alert', alertSpy)
    seed()
    const a = renderActions()
    act(() => a.current.duplicate(SLOT))
    await waitFor(() => expect(alertSpy).toHaveBeenCalledTimes(1))
    expect(alertSpy.mock.calls[0][0]).toBe(`Couldn't duplicate this session. ${staleOwner}`)
    expect(alertSpy.mock.calls[0][0]).toContain('predates the configured owner')
    expect(alertSpy.mock.calls[0][0]).not.toContain('Try again in a moment')
  })

  it('duplicate follows an access-proxy challenge with the proxy remedy and no retry sentence', async () => {
    // A proxy in front of the gateway answered with its own sign-in page: the
    // gateway's sign-in does not clear it, and err.message already names the
    // proxy and what to do.
    const proxy = 'Reload this browser tab — if a sign-in page appears, your access proxy’s session expired. If not, a proxy or firewall in front of this dashboard refused the request; check the proxy’s logs, or retry later.'
    mocks.forkChatSlot.mockRejectedValueOnce(new ApiError(401, proxy, '<html></html>', true, true))
    const alertSpy = vi.fn()
    vi.stubGlobal('alert', alertSpy)
    seed()
    const a = renderActions()
    act(() => a.current.duplicate(SLOT))
    await waitFor(() => expect(alertSpy).toHaveBeenCalledTimes(1))
    expect(alertSpy.mock.calls[0][0]).toBe(`Couldn't duplicate this session. ${proxy}`)
    expect(alertSpy.mock.calls[0][0]).not.toContain('Try again in a moment')
  })

  it('duplicate alerts the keyed not-found copy on a slot_not_found 404, not the raw "not found"', async () => {
    // The fork endpoint answers 404 {error:"not found", code:"slot_not_found"}
    // when the session is not open on the gateway; the raw reason says neither
    // what is missing nor what to do.
    mocks.forkChatSlot.mockRejectedValueOnce(refusal(404, JSON.stringify({ error: 'not found', code: 'slot_not_found' }), 'not found'))
    const alertSpy = vi.fn()
    vi.stubGlobal('alert', alertSpy)
    seed()
    const a = renderActions()
    act(() => a.current.duplicate(SLOT))
    await waitFor(() => expect(alertSpy).toHaveBeenCalledTimes(1))
    expect(alertSpy.mock.calls[0][0]).toBe(
      "Couldn't duplicate this session. It's no longer open. Reload the page, or reopen it from History and try again.",
    )
    expect(alertSpy.mock.calls[0][0]).not.toContain(': not found')
  })

  it('duplicate wraps an ordinary 429 reason in the subject template', async () => {
    // Only the slot-cap codes get the keyed copy; a tunnel-edge 429 still names its reason.
    mocks.forkChatSlot.mockRejectedValueOnce(refusal(429, JSON.stringify({ error: 'x', code: 'throttled' }), 'Rate limited'))
    const alertSpy = vi.fn()
    vi.stubGlobal('alert', alertSpy)
    seed()
    const a = renderActions()
    act(() => a.current.duplicate(SLOT))
    await waitFor(() => expect(alertSpy).toHaveBeenCalledTimes(1))
    expect(alertSpy.mock.calls[0][0]).toBe("Couldn't duplicate this session. Reason: Rate limited. Try again in a moment.")
  })

  it('duplicate wraps the server reason in the subject template on a non-429 ApiError', async () => {
    // A 404 without the slot_not_found code still names the subject; a reason's
    // own end punctuation is dropped so the sentence does not end in "..".
    mocks.forkChatSlot.mockRejectedValueOnce(refusal(404, 'not found body', 'the source session was permanently deleted.'))
    const alertSpy = vi.fn()
    vi.stubGlobal('alert', alertSpy)
    seed()
    const a = renderActions()
    act(() => a.current.duplicate(SLOT))
    await waitFor(() => expect(alertSpy).toHaveBeenCalledTimes(1))
    expect(alertSpy.mock.calls[0][0]).toBe(
      "Couldn't duplicate this session. Reason: the source session was permanently deleted. Try again in a moment.",
    )
  })

  it('duplicate drops a Devanagari full stop from the reason so the sentence does not end twice', async () => {
    // A Hindi or Bengali reason ends in the danda, which the template's own
    // end mark follows; leaving it in would read "।।" in those locales.
    mocks.forkChatSlot.mockRejectedValueOnce(refusal(500, 'body', 'सर्वर त्रुटि।'))
    const alertSpy = vi.fn()
    vi.stubGlobal('alert', alertSpy)
    seed()
    const a = renderActions()
    act(() => a.current.duplicate(SLOT))
    await waitFor(() => expect(alertSpy).toHaveBeenCalledTimes(1))
    expect(alertSpy.mock.calls[0][0]).toBe("Couldn't duplicate this session. Reason: सर्वर त्रुटि. Try again in a moment.")
  })

  it('duplicate alerts the connection copy when fetch itself rejects (non-ApiError)', async () => {
    // A gateway that can't be reached rejects with a TypeError, not an ApiError,
    // so there is no server reason to wrap — the handler shows the keyed
    // connection message instead.
    mocks.forkChatSlot.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    const alertSpy = vi.fn()
    vi.stubGlobal('alert', alertSpy)
    seed()
    const a = renderActions()
    act(() => a.current.duplicate(SLOT))
    await waitFor(() => expect(alertSpy).toHaveBeenCalledTimes(1))
    // Real i18n string (not mocked): names Kiro Crew, and is NOT the reason template.
    expect(alertSpy.mock.calls[0][0]).toBe(
      "Couldn't duplicate this session. Kiro Crew couldn't be reached. Check your connection and try again.",
    )
    expect(alertSpy.mock.calls[0][0]).not.toContain('Failed to fetch')
  })

  it('close honours confirmCloseSession', () => {
    seed()
    // disabled -> no confirm prompt, deleteSlot dispatched
    cfgMock.loadChatConfig.mockReturnValue({ confirmCloseSession: false })
    const skipConfirm = vi.fn(() => true)
    vi.stubGlobal('confirm', skipConfirm)
    const dispatchSpy = vi.spyOn(store, 'dispatch')
    const a = renderActions()
    dispatchSpy.mockClear()
    act(() => a.current.close(SLOT))
    expect(skipConfirm).not.toHaveBeenCalled()
    expect(dispatchSpy).toHaveBeenCalled()

    // enabled + user declines -> confirm prompted, nothing dispatched
    cfgMock.loadChatConfig.mockReturnValue({ confirmCloseSession: true })
    const decline = vi.fn(() => false)
    vi.stubGlobal('confirm', decline)
    dispatchSpy.mockClear()
    act(() => a.current.close(SLOT))
    expect(decline).toHaveBeenCalled()
    expect(dispatchSpy).not.toHaveBeenCalled()
    dispatchSpy.mockRestore()
  })

  it('close asks first for a session with a turn in flight, even with confirmCloseSession off', () => {
    // The sidebar row ✕ and the session menu share this close. It must ask on
    // the same busy rule Cmd+W uses, or a click stops live work with no warning.
    // Its own key: an earlier test's close of SLOT is still settling.
    const BUSY = 'chat-actions-busy'
    store.dispatch(sseSlots([{ key: BUSY, title: BUSY, messages: 1, running: true, folder_id: '' }]))
    cfgMock.loadChatConfig.mockReturnValue({ confirmCloseSession: false })
    const decline = vi.fn(() => false)
    vi.stubGlobal('confirm', decline)
    const dispatchSpy = vi.spyOn(store, 'dispatch')
    const a = renderActions()
    dispatchSpy.mockClear()
    act(() => a.current.close(BUSY))
    expect(decline).toHaveBeenCalledTimes(1)
    expect(dispatchSpy).not.toHaveBeenCalled()

    // Accepting the ask closes it.
    vi.stubGlobal('confirm', vi.fn(() => true))
    act(() => a.current.close(BUSY))
    expect(dispatchSpy).toHaveBeenCalled()
    dispatchSpy.mockRestore()
  })

  it('close names why it asks: live work, or the setting when nothing is running', () => {
    const BUSY = 'chat-actions-reason-busy'
    const IDLE = 'chat-actions-reason-idle'
    store.dispatch(sseSlots([
      { key: BUSY, title: BUSY, messages: 1, running: true, folder_id: '' },
      { key: IDLE, title: IDLE, messages: 1, running: false, folder_id: '' },
    ]))
    cfgMock.loadChatConfig.mockReturnValue({ confirmCloseSession: true })
    const decline = vi.fn((_msg?: string) => false)
    vi.stubGlobal('confirm', decline)
    const a = renderActions()

    act(() => a.current.close(BUSY))
    expect(decline.mock.calls[0][0]).toContain('It is still working on a reply. Closing stops that reply.')
    expect(decline.mock.calls[0][0]).not.toContain('Confirm Before Closing Session')

    act(() => a.current.close(IDLE))
    expect(decline.mock.calls[1][0]).toContain('Nothing is running in it.')
    expect(decline.mock.calls[1][0]).toContain('Confirm Before Closing Session')
    expect(decline.mock.calls[1][0]).toContain('Chat › Confirm Before Closing Session')
  })

  it('close stays instant for a session waiting on the user (options card, question, interrupted turn)', () => {
    // These read as the Waiting lane, but nothing is running in them: a close
    // retires no work. Asking here is a prompt the setting cannot turn off.
    const WAITING = 'chat-actions-waiting'
    store.dispatch(sseSlots([
      { key: WAITING, title: WAITING, messages: 1, running: false, has_options: true, needs_input: true, interrupted: true, folder_id: '' },
    ]))
    cfgMock.loadChatConfig.mockReturnValue({ confirmCloseSession: false })
    const ask = vi.fn(() => false)
    vi.stubGlobal('confirm', ask)
    const dispatchSpy = vi.spyOn(store, 'dispatch')
    const a = renderActions()
    dispatchSpy.mockClear()
    act(() => a.current.close(WAITING))
    expect(ask).not.toHaveBeenCalled()
    expect(dispatchSpy).toHaveBeenCalled()
    dispatchSpy.mockRestore()
  })

  it('close stays instant for an idle session with confirmCloseSession off', () => {
    seed()
    cfgMock.loadChatConfig.mockReturnValue({ confirmCloseSession: false })
    const ask = vi.fn(() => false)
    vi.stubGlobal('confirm', ask)
    const dispatchSpy = vi.spyOn(store, 'dispatch')
    const a = renderActions()
    dispatchSpy.mockClear()
    act(() => a.current.close(SLOT))
    expect(ask).not.toHaveBeenCalled()
    expect(dispatchSpy).toHaveBeenCalled()
    dispatchSpy.mockRestore()
  })
})

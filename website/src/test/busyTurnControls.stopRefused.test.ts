import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { configureStore } from '@reduxjs/toolkit'
import { createElement, type MutableRefObject, type ReactNode } from 'react'
import { Provider } from 'react-redux'

import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import { store } from '../store'
import chatReducer, { replaceMessages, requestStop, setActiveSlot, sseChatMessage, type StopFailure } from '../store/chatSlice'
import { i18nT } from '../i18n/t'
import { useBusyTurnControls } from '../pages/chat/page/busyTurnControls'
import type { ComposerDraftStores } from '../pages/chat/page/composerDrafts'
import type { ComposerStaging } from '../pages/chat/page/composerStaging'
import type { ChatMessage } from '../types'

// The main chat's Stop shows a refused press on the page's action-error notice,
// as ChatPane does. A non-owner's Stop on a running cron tab is refused with
// `owner_only`; before, the main chat dropped that refusal and showed nothing.

const SLOT = 'cron-nightly'

const ref = <T,>(current: T) => ({ current }) as MutableRefObject<T>

function renderControls(slot: string, dispatch: (action: unknown) => unknown) {
  const showActionError = vi.fn()
  const activeSlotRef = ref<string | null>(slot)
  const softStopAtMapRef = ref(new Map<string, number>())
  const { result } = renderHook(() => useBusyTurnControls({
    activeSlot: slot,
    currentSlot: undefined,
    connected: true,
    activeSlotRef,
    slotRunning: true,
    messages: [],
    send: vi.fn(async () => true),
    steerMutation: { mutate: vi.fn() },
    messageQuote: { consume: () => ({ quote: null }), recoverInto: (t: string) => t } as never,
    composerRef: ref(null),
    composerSlotRef: ref<string | null>(slot),
    composerMountedRef: ref(true),
    forkUnavailable: false,
    inputRef: ref(''),
    setInput: vi.fn(),
    stores: { drafts: ref({}), fileDrafts: ref({}), pasteDrafts: ref({}), sessionRefDrafts: ref({}), saveDrafts: vi.fn() } as unknown as ComposerDraftStores,
    staging: { pendingFilesRef: ref([]), pasteBlocksRef: ref([]), pendingSessionsRef: ref([]), pickedFileTokens: ref({}) } as unknown as ComposerStaging,
    pendingQuestion: undefined as never,
    softStopAtMapRef,
    dispatch: dispatch as never,
    showActionError,
  }), { wrapper: ({ children }: { children: ReactNode }) => createElement(Provider, { store }, children) })
  return { result, showActionError, activeSlotRef, softStopAtMapRef }
}

function setup(failure: StopFailure) {
  // The promise the press's own `.then` hangs on. Callbacks on one promise run
  // in the order they were added, so awaiting it after the press runs after it.
  let pressed: Promise<unknown> = Promise.resolve()
  const dispatch = vi.fn((action: unknown) => {
    if (typeof action !== 'function') return action
    pressed = Promise.resolve(requestStop.fulfilled(failure, 'req', { slotId: SLOT, force: false }))
    return pressed
  })
  return { ...renderControls(SLOT, dispatch), pressed: () => pressed }
}

describe('a Stop the server refuses on the main chat', () => {
  it('is shown on the action-error notice and disarms the force window', async () => {
    const h = setup({ error: 'only the dashboard owner can stop a scheduled run', code: 'owner_only' })
    act(() => h.result.current.stopTurn())
    // Its own copy, not "Press Stop again": a second press is refused the same way.
    await vi.waitFor(() => expect(h.showActionError).toHaveBeenCalledWith(
      i18nT('components.chatPane.stop_failed_owner_only'),
      undefined,
    ))
    expect(h.softStopAtMapRef.current.get(SLOT)).toBe(0)
  })

  it('is not shown once another slot is on screen', async () => {
    const h = setup({ error: 'only the dashboard owner can stop a scheduled run', code: 'owner_only' })
    act(() => h.result.current.stopTurn())
    h.activeSlotRef.current = 'dashboard:other'
    await h.pressed()
    expect(h.showActionError).not.toHaveBeenCalled()
  })

  it('shows nothing for a stop that landed', async () => {
    const h = setup(null)
    act(() => h.result.current.stopTurn())
    await h.pressed()
    expect(h.showActionError).not.toHaveBeenCalled()
  })
})

// The approval cards a Stop press leaves behind. A refused stop leaves the turn
// running and waiting on its card, so the card must keep its buttons; a stop
// that lands rejects the turn's card, but not a coordinator card, which waits
// on the registry rather than on this turn.

const perm = (id: string, meta: Record<string, unknown> = {}): ChatMessage =>
  ({ role: 'permission', content: id, cls: '', meta: { approval_id: id, ...meta } }) as ChatMessage

function setupWithCards(stopReply: () => Promise<unknown>) {
  const s = configureStore({ reducer: { chat: chatReducer }, middleware: (d) => d({ serializableCheck: false, immutableCheck: false }) })
  s.dispatch(setActiveSlot(SLOT))
  s.dispatch(replaceMessages([perm('turn'), perm('coord', { registry: 'coordinator' })]))
  vi.spyOn(api, 'stopChatSlot').mockImplementation(stopReply as never)
  let pressed: PromiseLike<unknown> = Promise.resolve()
  const dispatch = (action: unknown) => {
    const r = s.dispatch(action as never)
    if (typeof action === 'function') pressed = r as PromiseLike<unknown>
    return r
  }
  const resolved = () => s.getState().chat.messages.map(m => m.meta?.resolved ?? null)
  return { ...renderControls(SLOT, dispatch), s, resolved, pressed: () => pressed }
}

afterEach(() => { vi.restoreAllMocks() })

describe('approval cards after the main chat Stop', () => {
  it.each([
    ['a 500', () => Promise.reject(new ApiError(500, 'internal error'))],
    ['a 2xx {ok:false}', () => Promise.resolve({ ok: false, error: 'could not reach the crew', code: 'remote_stop_unreachable' })],
  ])('stay answerable when the stop fails with %s', async (_label, reply) => {
    const h = setupWithCards(reply)
    act(() => h.result.current.stopTurn())
    await h.pressed()
    expect(h.resolved()).toEqual([null, null])
  })

  it('reject the turn card, not the coordinator card, once the stop lands', async () => {
    const h = setupWithCards(() => Promise.resolve({ ok: true }))
    act(() => h.result.current.stopTurn())
    await h.pressed()
    expect(h.resolved()).toEqual(['rejected', null])
  })

  it('of a turn that started after the press stay answerable', async () => {
    let land: (v: unknown) => void = () => {}
    const h = setupWithCards(() => new Promise((r) => { land = r }))
    act(() => h.result.current.stopTurn())
    // The stopped turn ends and a new one raises its own card before the reply lands.
    h.s.dispatch(sseChatMessage({ slot: SLOT, role: 'user', content: 'next', meta: { mid: 'm-next' } }))
    h.s.dispatch(replaceMessages([perm('next')]))
    land({ ok: true })
    await h.pressed()
    expect(h.resolved()).toEqual([null])
  })

  it('of another chat on screen when the reply lands stay answerable', async () => {
    let land: (v: unknown) => void = () => {}
    const h = setupWithCards(() => new Promise((r) => { land = r }))
    act(() => h.result.current.stopTurn())
    // A switch changes the chat on screen, not the stopped chat's turn count.
    h.s.dispatch(setActiveSlot('dashboard:other'))
    h.s.dispatch(replaceMessages([perm('other')]))
    land({ ok: true })
    await h.pressed()
    expect(h.resolved()).toEqual([null])
  })
})

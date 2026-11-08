import { describe, expect, it, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { createElement, type MutableRefObject, type ReactNode } from 'react'
import { Provider } from 'react-redux'

import { store } from '../store'
import { requestStop, type StopFailure } from '../store/chatSlice'
import { i18nT } from '../i18n/t'
import { useBusyTurnControls } from '../pages/chat/page/busyTurnControls'
import type { ComposerDraftStores } from '../pages/chat/page/composerDrafts'
import type { ComposerStaging } from '../pages/chat/page/composerStaging'

// The main chat's Stop shows a refused press on the page's action-error notice,
// as ChatPane does. A non-owner's Stop on a running cron tab is refused with
// `owner_only`; before, the main chat dropped that refusal and showed nothing.

const SLOT = 'cron-nightly'

const ref = <T,>(current: T) => ({ current }) as MutableRefObject<T>

function setup(failure: StopFailure) {
  const showActionError = vi.fn()
  const activeSlotRef = ref<string | null>(SLOT)
  const softStopAtMapRef = ref(new Map<string, number>())
  // The promise the press's own `.then` hangs on. Callbacks on one promise run
  // in the order they were added, so awaiting it after the press runs after it.
  let pressed: Promise<unknown> = Promise.resolve()
  const dispatch = vi.fn((action: unknown) => {
    if (typeof action !== 'function') return action
    pressed = Promise.resolve(requestStop.fulfilled(failure, 'req', { slotId: SLOT, force: false }))
    return pressed
  })
  const { result } = renderHook(() => useBusyTurnControls({
    activeSlot: SLOT,
    currentSlot: undefined,
    connected: true,
    activeSlotRef,
    slotRunning: true,
    messages: [],
    send: vi.fn(async () => true),
    steerMutation: { mutate: vi.fn() },
    messageQuote: { consume: () => ({ quote: null }), recoverInto: (t: string) => t } as never,
    composerRef: ref(null),
    composerSlotRef: ref<string | null>(SLOT),
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
  return { result, showActionError, activeSlotRef, softStopAtMapRef, pressed: () => pressed }
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

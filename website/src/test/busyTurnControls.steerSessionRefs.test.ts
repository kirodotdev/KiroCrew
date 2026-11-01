import { describe, expect, it, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { createElement, type MutableRefObject, type ReactNode } from 'react'
import { Provider } from 'react-redux'

import { store } from '../store'
import { useBusyTurnControls } from '../pages/chat/page/busyTurnControls'
import type { ComposerDraftStores } from '../pages/chat/page/composerDrafts'
import type { ComposerStaging } from '../pages/chat/page/composerStaging'
import { formatSessionRefLink, type SessionRef } from '../utils/sessionRefs'

// Steering into a RUNNING turn must carry a session dropped onto the composer,
// the same way a normal send does: as its link, appended to the steered text.
// It used to steer only the typed text and leave the chip behind (#24471).

const SLOT = 'dashboard:running'
const REF: SessionRef = { key: 'dashboard:other', title: 'Other chat', messages: 4 }

const ref = <T,>(current: T) => ({ current }) as MutableRefObject<T>

function setup(opts: { text: string; refs: SessionRef[] }) {
  const mutate = vi.fn()
  const setInput = vi.fn()
  const setPendingSessions = vi.fn()
  const sessionRefDrafts = ref<Record<string, SessionRef[]>>({ [SLOT]: opts.refs })
  const stores = {
    drafts: ref<Record<string, string>>({ [SLOT]: opts.text }),
    fileDrafts: ref<Record<string, string[]>>({}),
    pasteDrafts: ref({}),
    sessionRefDrafts,
    saveDrafts: vi.fn(),
  } as unknown as ComposerDraftStores
  const staging = {
    pendingFilesRef: ref<string[]>([]),
    pasteBlocksRef: ref([]),
    setPasteBlocks: vi.fn(),
    setPendingFiles: vi.fn(),
    pendingSessionsRef: ref(opts.refs),
    setPendingSessions,
    pickedFileTokens: ref({}),
    mergeSlotTokens: vi.fn(),
  } as unknown as ComposerStaging
  const { result } = renderHook(() => useBusyTurnControls({
    activeSlot: SLOT,
    currentSlot: undefined,
    connected: true,
    activeSlotRef: ref<string | null>(SLOT),
    slotRunning: true,
    messages: [],
    send: vi.fn(async () => true),
    steerMutation: { mutate },
    messageQuote: { consume: () => ({ quote: null }), recoverInto: (t: string) => t } as never,
    composerRef: ref(null),
    composerSlotRef: ref<string | null>(SLOT),
    inputRef: ref(opts.text),
    setInput,
    stores,
    staging,
    pendingQuestion: undefined as never,
    softStopAtMapRef: ref(new Map<string, number>()),
    dispatch: vi.fn() as never,
  }), { wrapper: ({ children }: { children: ReactNode }) => createElement(Provider, { store }, children) })
  return { result, mutate, setInput, setPendingSessions, sessionRefDrafts }
}

describe('steering a running turn with a dropped session', () => {
  it('appends the session link to the steered text and clears the chip with the text', () => {
    const h = setup({ text: 'The attached session contains useful information', refs: [REF] })
    act(() => h.result.current.steer())
    expect(h.mutate).toHaveBeenCalledTimes(1)
    expect(h.mutate.mock.calls[0][0].text).toBe(`The attached session contains useful information\n\n${formatSessionRefLink(REF)}`)
    expect(h.setInput).toHaveBeenCalledWith('')
    expect(h.setPendingSessions).toHaveBeenCalledWith([])
    expect(h.sessionRefDrafts.current[SLOT]).toBeUndefined()
  })

  it('leaves a dropped session staged when there is no typed text to steer', () => {
    const h = setup({ text: '', refs: [REF] })
    act(() => h.result.current.steer())
    expect(h.mutate).not.toHaveBeenCalled()
    expect(h.setPendingSessions).not.toHaveBeenCalled()
    expect(h.sessionRefDrafts.current[SLOT]).toEqual([REF])
  })
})

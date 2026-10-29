import { describe, expect, it, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { createElement, type MutableRefObject, type ReactNode } from 'react'
import { Provider } from 'react-redux'

import { store } from '../store'
import { useBusyTurnControls } from '../pages/chat/page/busyTurnControls'
import type { ComposerDraftStores } from '../pages/chat/page/composerDrafts'
import type { ComposerStaging } from '../pages/chat/page/composerStaging'

// A steer into a running turn carries the ordered `meta.files` list on both
// the optimistic bubble and the POST, so the `[attached_file N]` chip resolves
// a path containing spaces instead of cutting it at the first space.

const SLOT = 'dashboard:running'
const SPACED = '/x/My Report.pdf'

const ref = <T,>(current: T) => ({ current }) as MutableRefObject<T>

function setup(opts: { text: string; files: string[] }) {
  const mutate = vi.fn()
  const dispatch = vi.fn()
  const stores = {
    drafts: ref<Record<string, string>>({ [SLOT]: opts.text }),
    fileDrafts: ref<Record<string, string[]>>({ [SLOT]: opts.files }),
    pasteDrafts: ref({}),
    sessionRefDrafts: ref({}),
    saveDrafts: vi.fn(),
  } as unknown as ComposerDraftStores
  const staging = {
    pendingFilesRef: ref<string[]>(opts.files),
    pasteBlocksRef: ref([]),
    setPasteBlocks: vi.fn(),
    setPendingFiles: vi.fn(),
    pendingSessionsRef: ref([]),
    setPendingSessions: vi.fn(),
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
    setInput: vi.fn(),
    stores,
    staging,
    pendingQuestion: undefined as never,
    softStopAtMapRef: ref(new Map<string, number>()),
    dispatch: dispatch as never,
  }), { wrapper: ({ children }: { children: ReactNode }) => createElement(Provider, { store }, children) })
  return { result, mutate, dispatch }
}

function bubbleMeta(dispatch: ReturnType<typeof vi.fn>) {
  const call = dispatch.mock.calls.find(([a]) => a?.payload?.meta?.steer)
  return call?.[0].payload.meta
}

describe('steering a running turn with an attached file', () => {
  it('sends meta.files on the bubble and the POST so a spaced path stays whole', () => {
    const h = setup({ text: 'read this', files: [SPACED] })
    act(() => h.result.current.steer())
    expect(h.mutate).toHaveBeenCalledTimes(1)
    const vars = h.mutate.mock.calls[0][0]
    expect(vars.text).toContain(`[attached_file 1] ${SPACED}`)
    expect(vars.files).toEqual([SPACED])
    expect(bubbleMeta(h.dispatch)?.files).toEqual([SPACED])
  })

  it('leaves meta.files off a steer with no attached file', () => {
    const h = setup({ text: 'just text', files: [] })
    act(() => h.result.current.steer())
    expect(h.mutate.mock.calls[0][0].files).toBeUndefined()
    expect(bubbleMeta(h.dispatch)).not.toHaveProperty('files')
  })
})

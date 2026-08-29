/**
 * The close action's gate is asked of the SLOT, not of the clicking host.
 *
 * Two mounted hosts can display one slot, and each keeps its draft in its own
 * `useState`, so a gate that reads only the clicking host's composer strands the
 * other one's draft. Nothing in the pane tree forbids the arrangement either:
 * `fillLeaf` applies a slot with no duplicate check and only a render-time
 * `.filter()` keeps one slot out of two panes.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'

const mockChatSlotNote = vi.fn()
const mockDeleteSlot = vi.fn()

vi.mock('../api/client', () => ({
  api: { chatSlotNote: (...a: unknown[]) => mockChatSlotNote(...a) },
  SEARCH_MIN_CHARS: 2,
}))

vi.mock('../store/chatSlice', async (orig) => {
  const actual = (await orig()) as Record<string, unknown>
  return {
    ...actual,
    deleteSlot: (key: string) => {
      mockDeleteSlot(key)
      return { type: 'test/deleteSlot', payload: key }
    },
  }
})

import { useOptionActionDispatch } from '../hooks/useOptionActionDispatch'
import { registerSlotComposer } from '../utils/slotComposerRegistry'
import { createTestStore } from './helpers'
import type { ComposerWork } from '../utils/composerWork'
import type { OptionAction } from '../app-sdk/protocol/options'

const SLOT = 'slot-shared-1'
const ACTION: OptionAction = { action: 'close', label: "That's all" }

const EMPTY: ComposerWork = {
  text: '',
  files: [],
  dirs: [],
  sessionRefs: [],
  pasteBlocks: [],
  knowledge: false,
  uploading: false,
  voiceCapture: false,
}

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <Provider store={createTestStore({})}>
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    </Provider>
  )
}

/** Mount one host on SLOT with the given composer state. */
function mountHost(work: ComposerWork, slot: string = SLOT) {
  return renderHook(
    () => useOptionActionDispatch({ resolveSlot: () => slot, composerWork: work, sourceKey: 'row-1' }),
    { wrapper },
  )
}

describe('close action gate spans every host bound to the slot', () => {
  let confirmSpy: ReturnType<typeof vi.spyOn>
  /** Deregisters the simulated late draft. The registry is module state, so leaving
   *  it registered would answer for every later test in this file. */
  let dropLateDraft: (() => void) | null = null

  beforeEach(() => {
    vi.clearAllMocks()
    mockChatSlotNote.mockResolvedValue({ appended: true })
    confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
  })

  afterEach(() => {
    confirmSpy.mockRestore()
    dropLateDraft?.()
    dropLateDraft = null
  })

  it('closes when NO host on the slot holds unsent work', async () => {
    // Positive control: without this, the abort test below could pass because the
    // close never works at all rather than because the gate caught the draft.
    const a = mountHost(EMPTY)
    await act(async () => {
      await a.result.current.dispatchFollowUpAction(ACTION, 'row-1')
    })
    expect(mockDeleteSlot).toHaveBeenCalledWith(SLOT)
  })

  it('ABORTS on a sibling-pane draft WITHOUT asking for consent first', async () => {
    // Pane B: same slot, unsent draft, and it is not the pane that gets clicked.
    mountHost({ ...EMPTY, text: 'half-written thought' })
    // Pane A: same slot, empty composer — its own gate has nothing to see.
    const paneA = mountHost(EMPTY)

    await act(async () => {
      await paneA.result.current.dispatchFollowUpAction(ACTION, 'row-1')
    })

    expect(mockDeleteSlot).not.toHaveBeenCalled()

    // The dead end was asking first: the prompt promised the draft would be lost,
    // then the close was refused anyway. Refusing early takes no false consent.
    expect(confirmSpy).not.toHaveBeenCalled()

    // And writes nothing, so a retry cannot litter the transcript.
    expect(mockChatSlotNote).not.toHaveBeenCalled()
  })

  it('ABORTS on a draft that appears AFTER consent, during the breadcrumb POST', async () => {
    // The blind spot the recheck exists for: the render-time gate cannot see a
    // draft typed inside the POST window, and that one was never consented to.
    mockChatSlotNote.mockImplementation(async () => {
      dropLateDraft = registerSlotComposer('late-draft', { getSlot: () => SLOT, hasWork: () => true })
      return { appended: true }
    })
    const paneA = mountHost(EMPTY)

    await act(async () => {
      await paneA.result.current.dispatchFollowUpAction(ACTION, 'row-1')
    })

    expect(mockDeleteSlot).not.toHaveBeenCalled()
  })

  it('REFUSES to close when the breadcrumb delivery is only CONDITIONAL', async () => {
    // The handler computes `delivery_conditional = deferred or not
    // slot.linked_session_key`, so `appended` alone is not durability.

    // An UNBOUND slot answers appended=true WITH deliveryConditional=true, and a
    // close on that persists the breadcrumb into whichever session binds next.
    mockChatSlotNote.mockResolvedValue({ appended: true, deliveryConditional: true })
    const paneA = mountHost(EMPTY)

    await act(async () => {
      await paneA.result.current.dispatchFollowUpAction(ACTION, 'row-1')
    })

    expect(mockDeleteSlot).not.toHaveBeenCalled()
  })

  it('ignores a draft held by a host on a DIFFERENT slot', async () => {
    // Scoping control: the gate must not become "any composer anywhere", which would
    // make the action permanently undispatchable on a busy dashboard.
    mountHost({ ...EMPTY, text: 'unrelated draft' }, 'slot-other-9')
    const paneA = mountHost(EMPTY)

    await act(async () => {
      await paneA.result.current.dispatchFollowUpAction(ACTION, 'row-1')
    })

    expect(mockDeleteSlot).toHaveBeenCalledWith(SLOT)
  })

  it('stops counting a host once it unmounts', async () => {
    const paneB = mountHost({ ...EMPTY, text: 'draft that leaves with its pane' })
    const paneA = mountHost(EMPTY)
    paneB.unmount()

    await act(async () => {
      await paneA.result.current.dispatchFollowUpAction(ACTION, 'row-1')
    })

    expect(mockDeleteSlot).toHaveBeenCalledWith(SLOT)
  })

  it('confirms with the chip label and where the transcript goes', async () => {
    const a = mountHost(EMPTY)
    await act(async () => {
      await a.result.current.dispatchFollowUpAction(ACTION, 'row-1')
    })

    expect(confirmSpy).toHaveBeenCalledTimes(1)
    const prompt = String(confirmSpy.mock.calls[0][0])
    // The label the user actually clicked, and the destination — the generic
    // "Close this session?" restated neither.
    expect(prompt).toContain("That's all")
    expect(prompt).toMatch(/older sessions/i)
  })
})

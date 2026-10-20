/**
 * `restoreSentAside`: a recovered page send whose text survived the reload gets back only what the
 * saved draft lost, never its text.
 */
import { describe, expect, it, vi } from 'vitest'
import { renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import { Provider } from 'react-redux'
import { store } from '../store'
import { useComposerSend } from '../pages/chat/page/composerSend'
import { formatToken, type PasteBlock } from '../utils/pasteTokens'

const wrapper = ({ children }: { children: ReactNode }) => <Provider store={store}>{children}</Provider>

function setup(slot: string, text: string, held: PasteBlock[]) {
  let live = held
  const installed: PasteBlock[][] = []
  const opts = {
    dispatch: store.dispatch, slots: [], connected: true, activeSlot: slot, slotRunning: false,
    knowledgeFetchRef: { current: { pendingKnowledge: null, inject: vi.fn() } },
    drafts: { current: { [slot]: text } }, fileDrafts: { current: {} }, pasteDrafts: { current: { [slot]: held } },
    saveDrafts: vi.fn(), sessionRefDrafts: { current: {} }, activeSlotRef: { current: slot },
    composerSlotRef: { current: slot }, composerMountedRef: { current: true }, setInput: vi.fn(),
    inputRef: { current: text }, sendingRef: { current: false }, messageQuote: {}, mergeSlotTokens: vi.fn(),
    pasteBlocksRef: { current: held }, pendingFilesRef: { current: [] }, pendingSessionsRef: { current: [] },
    readPasteBlocks: () => live, installPasteBlocks: (next: PasteBlock[]) => { live = next; installed.push(next) },
  } as unknown as Parameters<typeof useComposerSend>[0]
  const { result } = renderHook(() => useComposerSend(opts), { wrapper })
  return { aside: result.current.restoreSentAside, installed, opts }
}

describe('restoreSentAside', () => {
  const block: PasteBlock = { id: 'kc1abc', seq: 1, lines: 4, content: 'a\nb\nc\nd' }

  it('puts back a paste block the saved drafts lost while its token survived, numbered as shown', () => {
    const shown = { ...block, seq: 3 }
    const text = `fix this ${formatToken(shown)} please`
    const s = setup('aside-a', text, [])
    s.aside('aside-a', { inlined: `fix this ${formatToken(block)} please`, pastes: [block], sessionRefs: [], quote: null })
    expect(s.installed).toEqual([[shown]])
    expect(s.opts.pasteDrafts.current['aside-a']).toEqual([shown])
    expect(s.opts.setInput).not.toHaveBeenCalled()
  })

  it('adds nothing when the block is still held', () => {
    const text = `fix this ${formatToken(block)} please`
    const s = setup('aside-b', text, [block])
    s.aside('aside-b', { inlined: text, pastes: [block], sessionRefs: [], quote: null })
    expect(s.installed).toEqual([])
  })
})

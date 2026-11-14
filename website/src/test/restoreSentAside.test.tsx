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
import { _resetDictationProvenance, isDictated } from '../chat-core/composer/dictationProvenance'

const wrapper = ({ children }: { children: ReactNode }) => <Provider store={store}>{children}</Provider>

function setup(slot: string, text: string, held: PasteBlock[]) {
  let live = held
  const inject = vi.fn()
  const installed: PasteBlock[][] = []
  const opts = {
    dispatch: store.dispatch, slots: [], connected: true, activeSlot: slot, slotRunning: false,
    knowledgeFetchRef: { current: { pendingKnowledge: null, inject } },
    drafts: { current: { [slot]: text } }, fileDrafts: { current: {} }, pasteDrafts: { current: { [slot]: held } },
    saveDrafts: vi.fn(), sessionRefDrafts: { current: {} }, activeSlotRef: { current: slot },
    composerSlotRef: { current: slot }, composerMountedRef: { current: true }, setInput: vi.fn(),
    inputRef: { current: text }, sendingRef: { current: false }, messageQuote: {}, mergeSlotTokens: vi.fn(),
    pasteBlocksRef: { current: held }, pendingFilesRef: { current: [] }, pendingSessionsRef: { current: [] },
    readPasteBlocks: () => live, installPasteBlocks: (next: PasteBlock[]) => { live = next; installed.push(next) },
  } as unknown as Parameters<typeof useComposerSend>[0]
  const { result, rerender } = renderHook(() => useComposerSend(opts), { wrapper })
  return { aside: result.current.restoreSentAside, installed, opts, inject, rerender }
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

  it('keeps both sends\u2019 knowledge when two restores land in one batch', () => {
    const k = (id: string) => ({ id, title: id, source: null, match_type: 'exact', tokens: 1, summary: '', content: '' })
    const s = setup('aside-k', 'first\n\nsecond', [])
    s.aside('aside-k', { inlined: 'first', pastes: [], sessionRefs: [], quote: null, knowledge: [k('k1')] })
    s.aside('aside-k', { inlined: 'second', pastes: [], sessionRefs: [], quote: null, knowledge: [k('k2')] })
    expect(s.inject.mock.calls.at(-1)?.[0].map((i: { id: string }) => i.id).sort()).toEqual(['k1', 'k2'])
  })

  it('does not bring back knowledge from an earlier render once the selection was cleared', () => {
    const k = (id: string) => ({ id, title: id, source: null, match_type: 'exact', tokens: 1, summary: '', content: '' })
    const s = setup('aside-k2', 'first\n\nlater', [])
    s.aside('aside-k2', { inlined: 'first', pastes: [], sessionRefs: [], quote: null, knowledge: [k('k1')] })
    s.rerender()
    s.aside('aside-k2', { inlined: 'later', pastes: [], sessionRefs: [], quote: null, knowledge: [k('k2')] })
    expect(s.inject.mock.calls.at(-1)?.[0].map((i: { id: string }) => i.id)).toEqual(['k2'])
  })

  it('marks a surviving dictated send as dictated', () => {
    _resetDictationProvenance()
    const s = setup('aside-d', 'spoken', [])
    s.aside('aside-d', { inlined: 'spoken', pastes: [], sessionRefs: [], quote: null, dictated: true })
    expect(isDictated('aside-d')).toBe(true)
  })

  it('adds nothing when the block is still held', () => {
    const text = `fix this ${formatToken(block)} please`
    const s = setup('aside-b', text, [block])
    s.aside('aside-b', { inlined: text, pastes: [block], sessionRefs: [], quote: null })
    expect(s.installed).toEqual([])
  })
})

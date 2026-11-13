/**
 * The composer's explicit undo history when a typing burst ends on the text
 * of the entry before it: "hello", a pause, " w", then two quick backspaces.
 * The burst merges into the tip, so the tip and the entry below it both hold
 * "hello". An undo must reach the text before "hello" instead of re-applying
 * the text already on screen, and an entry with the same text but a different
 * paste block must still be restorable.
 */
import { useRef, useState } from 'react'
import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { PasteBlock } from '../../utils/pasteTokens'
import { useUndoHistory } from './undoHistory'

/** The one clock the history reads, advanced by hand. */
let now = 1_000

afterEach(() => {
  vi.restoreAllMocks()
})

function mountComposerHistory() {
  vi.spyOn(Date, 'now').mockImplementation(() => now)
  return renderHook(() => {
    const [value, setValue] = useState('')
    const [blocks, setBlocks] = useState<PasteBlock[]>([])
    const pasteBlocksRef = useRef<PasteBlock[]>(blocks)
    pasteBlocksRef.current = blocks
    const valueFromUserRef = useRef(false)
    const optimizingRef = useRef(false)
    const inputRef = useRef<HTMLTextAreaElement>(null)
    const undo = useUndoHistory({
      value,
      pasteBlocks: blocks,
      autoFocusKey: 'slot-a',
      composerControl: () => null,
      pasteBlocksRef,
      valueFromUserRef,
      optimizingRef,
      onChange: setValue,
      onPasteBlocksChange: setBlocks,
      inputRef,
      ime: { isComposing: () => false },
    })
    const type = (next: string, gapMs = 50, nextBlocks: PasteBlock[] = pasteBlocksRef.current) => {
      now += gapMs
      valueFromUserRef.current = true
      pasteBlocksRef.current = nextBlocks
      setBlocks(nextBlocks)
      setValue(next)
    }
    return { value, blocks, type, undo }
  })
}

describe('useUndoHistory', () => {
  it('undoes past a tip that a typing burst brought back to the entry below it', () => {
    const { result } = mountComposerHistory()
    for (const text of ['h', 'he', 'hel', 'hell', 'hello']) act(() => result.current.type(text))
    act(() => result.current.type('hello ', 1_000)) // after a pause: a new entry
    act(() => result.current.type('hello w'))
    act(() => result.current.type('hello '))
    act(() => result.current.type('hello'))

    let stepped = false
    act(() => { stepped = result.current.undo.stepUndoHistory('undo') })

    expect(stepped).toBe(true)
    expect(result.current.value).toBe('')
  })

  it('restores a paste block from an entry with the same text, and keeps recording after it', () => {
    // A token edited and restored inside the coalescing window: the text comes
    // back, but the block behind the token was pruned on the edit.
    const block: PasteBlock = { id: 'p1', seq: 1, lines: 4, content: 'pasted\nlines\nof\ntext' }
    const { result } = mountComposerHistory()
    act(() => result.current.type('[Paste #1]', 50, [block]))
    act(() => result.current.type('[Paste #1', 1_000, []))
    act(() => result.current.type('[Paste #1]', 50, []))

    let stepped = false
    act(() => { stepped = result.current.undo.stepUndoHistory('undo') })
    expect(stepped).toBe(true)
    expect(result.current.value).toBe('[Paste #1]')
    expect(result.current.blocks).toEqual([block])

    act(() => result.current.type('[Paste #1]!', 1_000))
    act(() => { result.current.undo.stepUndoHistory('undo') })
    expect(result.current.value).toBe('[Paste #1]')
  })

  it('reports nothing to undo when every earlier entry holds the current text', () => {
    const { result } = mountComposerHistory()
    let stepped = true
    act(() => { stepped = result.current.undo.stepUndoHistory('undo') })
    expect(stepped).toBe(false)
    expect(result.current.value).toBe('')
  })
})

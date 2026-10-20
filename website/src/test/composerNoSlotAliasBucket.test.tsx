import { describe, it, expect, beforeEach } from 'vitest'
import { useRef, type MutableRefObject } from 'react'
import { renderHook, act } from '@testing-library/react'

import { useFileMentionTokens, useFileMentionActions } from '../pages/chat/page/composerFileMentions'
import { NO_SLOT_TOKEN_KEY } from '../utils/chatFileTokenDrafts'

/**
 * #14571: the new-chat composer has no slot yet (`composerSlotRef.current` is
 * null), so before the fix every alias write no-op'd and removing a picked
 * file's chip left its `@mention` behind. These tests drive the real mention
 * plumbing with no slot and assert it now behaves like a real slot does.
 *
 * The hooks are exercised directly (not through a full ChatPage render) so the
 * no-slot path is isolated from session/store machinery. `inputRef` + `setInput`
 * stand in for the composer draft store: a plain object whose value the handlers
 * read and write, exactly as ChatPage's draft store presents it.
 */

type Harness = ReturnType<typeof useHarness>

function useHarness(composerSlotRef: MutableRefObject<string | null>) {
  const pickedFileTokens = useRef<Record<string, Record<string, string[]>>>({})
  const tokens = useFileMentionTokens(composerSlotRef, pickedFileTokens)

  // A minimal state-backed composer: inputRef holds the live text; setInput is
  // the draft store's setter (value or updater), kept in sync with inputRef.
  const inputRef = useRef('')
  const setInput = (next: string | ((prev: string) => string)) => {
    inputRef.current = typeof next === 'function' ? next(inputRef.current) : next
  }

  const pendingFiles: string[] = []
  const pendingFilesRef = useRef<string[]>([])
  const setPendingFiles = (next: string[] | ((prev: string[]) => string[])) => {
    pendingFilesRef.current = typeof next === 'function' ? next(pendingFilesRef.current) : next
  }

  const currentProjectRef = useRef<string | undefined>('/repo')
  const pasteBlocksRef = useRef<never[]>([])
  const voiceCaretRef = useRef<{ start: number; end: number } | null>(null)
  const voicePendingCaretRef = useRef<number | null>(null)
  const reconcileFileChipsRef = useRef<((t: string) => void) | null>(null)

  const staging = {
    pendingFiles,
    setPendingFiles,
    pendingFilesRef,
    pasteBlocksRef,
    currentSlotTokens: tokens.currentSlotTokens,
    recordSlotToken: tokens.recordSlotToken,
  }

  const actions = useFileMentionActions({
    // eslint-disable-next-line @typescript-eslint/no-explicit-any -- minimal staging slice for the file-mention handlers under test
    staging: staging as any,
    inputRef,
    setInput,
    currentProjectRef,
    voiceCaretRef,
    voicePendingCaretRef,
    reconcileFileChipsRef,
  })

  return { pickedFileTokens, inputRef, pendingFilesRef, ...actions }
}

describe('new-chat composer alias bucket (#14571)', () => {
  beforeEach(() => sessionStorage.clear())

  it('records a picked file under the reserved no-slot bucket', () => {
    const { result } = renderHook(() => {
      const composerSlotRef = useRef<string | null>(null)
      return useHarness(composerSlotRef)
    })
    act(() => { result.current.selectPickedFile('/repo/src/main.ts', 'file', '@src/main.ts') })
    const h = result.current as Harness
    expect(h.pickedFileTokens.current[NO_SLOT_TOKEN_KEY]).toEqual({
      '/repo/src/main.ts': ['@src/main.ts'],
    })
  })

  it('removing the chip strips the mention when there is no slot', () => {
    const { result } = renderHook(() => {
      const composerSlotRef = useRef<string | null>(null)
      return useHarness(composerSlotRef)
    })
    // Pick a file via the tree "Add to context" entry point: it inserts the
    // @mention into the composer AND records the alias.
    act(() => { result.current.handleAddToContext('/repo/src/main.ts', 'file') })
    let h = result.current as Harness
    expect(h.inputRef.current).toContain('@src/main.ts')
    expect(h.pendingFilesRef.current).toContain('/repo/src/main.ts')

    // Removing the chip must strip the @mention from the text -- the bug was
    // that with no slot the alias was never recorded, so remove left it behind.
    act(() => { result.current.removeFileChip('/repo/src/main.ts') })
    h = result.current as Harness
    expect(h.inputRef.current).not.toContain('@src/main.ts')
    expect(h.pendingFilesRef.current).not.toContain('/repo/src/main.ts')
  })
})

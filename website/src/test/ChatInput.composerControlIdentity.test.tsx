import { createRef } from 'react'
import { describe, expect, it, vi } from 'vitest'
import ChatInput from '../components/ChatInput'
import { ComposerVoiceSliceOverride } from '../chat-core/composer/Composer'
import { renderWithProviders } from './helpers'

// ChatPage passes an inline `onChange` and re-renders once per SSE chunk while
// the agent streams. The textarea `ComposerControl` must not take a new identity
// on those renders: the dictation caret-restore effect depends on
// `composerControl`, and a re-run inside the restore frame cancels the frame,
// then overwrites `voiceCaretRef` with the live end-of-text selection — so the
// spliced transcript loses its caret and the NEXT transcript lands at the wrong
// offset.
describe('ChatInput textarea composer control identity', () => {
  it('keeps a pending dictation caret restore alive across an onChange identity change', async () => {
    const caretRef = createRef<{ start: number; end: number } | null>()
    const pendingRef = createRef<number | null>()
    caretRef.current = { start: 2, end: 2 }
    pendingRef.current = 2
    const inputProps = { voiceCaretRef: caretRef, voicePendingCaretRef: pendingRef }
    const { rerender } = renderWithProviders(
      <ComposerVoiceSliceOverride inputProps={inputProps}>
        <ChatInput value="hello" onChange={vi.fn()} onSend={vi.fn()} />
      </ComposerVoiceSliceOverride>,
    )
    // Mount consumed the pending caret and armed the restore frame.
    expect(pendingRef.current).toBeNull()
    // An unrelated parent render (new inline onChange, same value) before the
    // frame fires — the streaming case.
    rerender(
      <ComposerVoiceSliceOverride inputProps={inputProps}>
        <ChatInput value="hello" onChange={vi.fn()} onSend={vi.fn()} />
      </ComposerVoiceSliceOverride>,
    )
    await new Promise<void>(resolve => requestAnimationFrame(() => resolve()))
    expect(caretRef.current).toEqual({ start: 2, end: 2 })
  })
})

import { useEffect } from 'react'
import type { SendMode } from '../../pages/chat/ChatSettings'
import type { useImeGuard } from '../../hooks/useImeGuard'
import type { ComposerEscapeKey } from './optimizeEscape'

/**
 * The send key a non-editable rich composer still answers. A running-turn
 * typing lock routes it to steer; a streaming dictation lock routes it to send.
 * Neither path edits the draft.
 *
 * Lexical drops key events once it is non-editable, so this listener accepts a
 * press aimed inside that editor, or at the page body while it last held focus.
 */
export function useTypingLockKeys({ lockedRich, isRunning, allowIdleSend = false, sendOnEnter, connected, fireComposer, wrapperRef, ownsFocusRef, optimizingRef, escapeKey, ime }: {
  lockedRich: boolean
  isRunning: boolean
  allowIdleSend?: boolean
  sendOnEnter: SendMode
  connected: boolean
  fireComposer: (alternate?: boolean) => void
  wrapperRef: React.RefObject<HTMLDivElement>
  ownsFocusRef: React.MutableRefObject<boolean>
  optimizingRef: React.MutableRefObject<boolean>
  /** The composer's bare-Escape gate: it cancels an in-flight optimize, never the turn. */
  escapeKey: ComposerEscapeKey
  ime: ReturnType<typeof useImeGuard>
}): (e: React.KeyboardEvent<HTMLTextAreaElement>) => void {
  useEffect(() => {
    if (!lockedRich || (!isRunning && !allowIdleSend)) return
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented) return
      const target = e.target as Element | null
      // Only the editor itself, never a control beside it: Enter on Take over
      // or Stop must activate that button, not steer the turn.
      const editor = target?.closest?.('[data-lexical-composer]') ?? null
      const inEditor = !!editor && !!wrapperRef.current?.contains(editor)
      if (!inEditor && !(target === document.body && ownsFocusRef.current)) return
      const command = e.metaKey || e.ctrlKey
      const sendKey = sendOnEnter === 'ctrl-enter'
        ? e.key === 'Enter' && command && !e.shiftKey
        : e.key === 'Enter' && !e.shiftKey && !(sendOnEnter === 'enter-ctrl-newline' && command)
      if (!sendKey || e.isComposing || e.keyCode === 229) return
      e.preventDefault()
      if (!e.repeat && connected && !optimizingRef.current) fireComposer(sendOnEnter === 'enter' && command)
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [lockedRich, isRunning, allowIdleSend, sendOnEnter, connected, fireComposer, wrapperRef, ownsFocusRef, optimizingRef])
  return (e) => {
    if (!isRunning) return
    const command = e.metaKey || e.ctrlKey
    if (e.key === 'Escape' && !command && !e.altKey && !e.shiftKey) {
      escapeKey(e, { native: e.nativeEvent, composing: ime.isComposing(e) })
      return
    }
    const sendKey = sendOnEnter === 'ctrl-enter'
      ? e.key === 'Enter' && command && !e.shiftKey
      : e.key === 'Enter' && !e.shiftKey && !(sendOnEnter === 'enter-ctrl-newline' && command)
    if (!sendKey || !ime.claimEnter(e)) return
    if (!e.repeat && connected && !optimizingRef.current) fireComposer(sendOnEnter === 'enter' && command)
  }
}

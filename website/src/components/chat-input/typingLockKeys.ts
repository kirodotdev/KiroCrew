import { useEffect } from 'react'
import type { SendMode } from '../../pages/chat/ChatSettings'
import type { useImeGuard } from '../../hooks/useImeGuard'
import type { StopTurnOnEscape } from './stopKey'

/**
 * The keys a typing-locked composer still answers, and only for a running
 * turn: a bare Escape stops it and the send binding steers it. Nothing here
 * edits the draft.
 *
 * A typing-locked rich editor is not editable either, so Lexical drops its
 * keys and focus falls to the page body. For a running turn the composer still
 * answers the send binding (steer), as the textarea's handler does: a press
 * aimed inside it, or at the body while it last held focus.
 */
export function useTypingLockKeys({ lockedRich, isRunning, sendOnEnter, connected, fireComposer, wrapperRef, ownsFocusRef, optimizingRef, stopTurnOnEscape, ime }: {
  lockedRich: boolean
  isRunning: boolean
  sendOnEnter: SendMode
  connected: boolean
  fireComposer: () => void
  wrapperRef: React.RefObject<HTMLDivElement>
  ownsFocusRef: React.MutableRefObject<boolean>
  optimizingRef: React.MutableRefObject<boolean>
  stopTurnOnEscape: StopTurnOnEscape
  ime: ReturnType<typeof useImeGuard>
}): (e: React.KeyboardEvent<HTMLTextAreaElement>) => void {
  useEffect(() => {
    if (!lockedRich || !isRunning) return
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented) return
      const target = e.target as Element | null
      // Only the editor itself, never a control beside it: Enter on Take over
      // or Stop must activate that button, not steer the turn.
      const editor = target?.closest?.('[data-lexical-composer]') ?? null
      const inEditor = !!editor && !!wrapperRef.current?.contains(editor)
      if (!inEditor && !(target === document.body && ownsFocusRef.current)) return
      const bare = !e.metaKey && !e.ctrlKey && !e.altKey && !e.shiftKey
      const sendKey = sendOnEnter === 'ctrl-enter'
        ? e.key === 'Enter' && (e.metaKey || e.ctrlKey) && !e.shiftKey
        : e.key === 'Enter' && bare
      if (!sendKey || e.isComposing || e.keyCode === 229) return
      e.preventDefault()
      if (!e.repeat && connected && !optimizingRef.current) fireComposer()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [lockedRich, isRunning, sendOnEnter, connected, fireComposer, wrapperRef, ownsFocusRef, optimizingRef])
  return (e) => {
    if (!isRunning) return
    const bare = !e.metaKey && !e.ctrlKey && !e.altKey && !e.shiftKey
    if (e.key === 'Escape' && bare) {
      stopTurnOnEscape(e, { native: e.nativeEvent, composing: ime.isComposing(e) })
      return
    }
    const sendKey = sendOnEnter === 'ctrl-enter'
      ? e.key === 'Enter' && (e.metaKey || e.ctrlKey) && !e.shiftKey
      : e.key === 'Enter' && bare
    if (!sendKey || !ime.claimEnter(e)) return
    if (!e.repeat && connected && !optimizingRef.current) fireComposer()
  }
}

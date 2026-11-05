/** A content-free "the main composer changed" signal. The composer owner calls
 *  `noteComposerInput(draftEmpty)` on the user's own edit and
 *  `noteComposerSent(sessionKey)` when that draft leaves as a send; the presence reporter
 *  subscribes to both. Nothing typed is passed along: only that a change
 *  happened and whether the draft is now empty (which ends the typing lease). */
import { useCallback } from 'react'

type Listener = (draftEmpty?: boolean) => void
type SentListener = (sessionKey: string | null) => void
const listeners = new Set<Listener>()
const sentListeners = new Set<SentListener>()

export const composerTypingSignal = {
  subscribe(listener: Listener): () => void {
    listeners.add(listener)
    return () => { listeners.delete(listener) }
  },
  /** Called when the composer's draft is sent: the typing it announced is over. */
  subscribeSent(listener: SentListener): () => void {
    sentListeners.add(listener)
    return () => { sentListeners.delete(listener) }
  },
}

export function noteComposerInput(draftEmpty = false): void {
  for (const listener of listeners) listener(draftEmpty)
}

/** The named session's composer draft left as a send. Carries no draft text. */
export function noteComposerSent(sessionKey: string | null): void {
  for (const listener of sentListeners) listener(sessionKey)
}

/** The composer's user-edit handler, also signalling that the user typed.
 *  ChatInput calls its `onChange` only for the user's own edits (typing, paste,
 *  undo, picker inserts), never for a draft a session switch restores, so that
 *  handler is the one signal that means "someone is typing". The draft store
 *  itself is not: its identity never changes, and a restore writes it too. */
export function useComposerEditWithTyping(edit: (value: string) => void): (value: string) => void {
  return useCallback((value: string) => { noteComposerInput(value.trim() === ''); edit(value) }, [edit])
}

import { useCallback } from 'react'
import { useChatReactions, type ReactionError } from '../../../hooks/useChatReactions'

/**
 * Message reactions for the active slot.
 *
 * A thin wrapper over `useChatReactions`, the same split `useTranscriptPins`
 * makes over `useChatPins`: the page-level hook owns the per-message handler
 * shape (`mid, emoji`) that `transcriptRendering.tsx`'s render call sites pass
 * straight to `UserMessage`/`AssistantMessage`, so the row components stay
 * ignorant of React Query and the API client.
 *
 * The wrapper also forwards the hook's `error`/`clearError`: a failed reaction
 * must not disappear silently (`errors-use-error-notice`). The error is scoped
 * to the slot and message it happened on, so only that row's bar renders the
 * notice.
 */
export function useTranscriptReactions(activeSlot: string | null) {
  const { reactions, loading: reactionsLoading, loadFailed, reactionsFor, toggleReaction, error, clearError } =
    useChatReactions(activeSlot ?? undefined)

  /** Fire-and-forget toggle for the row components, whose `onToggleReaction`
   *  prop is typed `(emoji) => void` and never awaits. `toggleReaction`
   *  rejects on failure; that rejection is consumed here because the hook has
   *  already set `error`, which the row renders as its inline notice. Passing
   *  the promise through would leave every failed toggle as an unhandled
   *  rejection. */
  const handleToggleReactionForMessage = useCallback(
    (mid: string, emoji: string): void => {
      toggleReaction(mid, emoji).catch(() => {})
    },
    [toggleReaction],
  )

  /** The error for `mid`, when the last failed reaction was on THIS message in
   *  THIS slot. Matching on `mid` alone would leak a slot A quota error onto
   *  the identical message in a fork of A (fork_slot copies the mid), so the
   *  active slot must match too. */
  const reactionErrorFor = useCallback(
    (mid: string): ReactionError | null =>
      error && error.mid === mid && error.slot === activeSlot ? error : null,
    [error, activeSlot],
  )

  return {
    reactions,
    reactionsLoading,
    /** The reactions list GET failed — the page shows a notice rather than
     *  letting an empty list read as "no reactions". */
    reactionsLoadFailed: loadFailed,
    reactionsFor,
    handleToggleReactionForMessage,
    reactionErrorFor,
    clearReactionError: clearError,
  }
}

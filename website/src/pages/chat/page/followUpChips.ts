import { useCallback, useEffect, useMemo, useRef, useState, type Dispatch, type MutableRefObject, type SetStateAction } from 'react'

import { deriveFollowUpOptions } from '../../../app-sdk/protocol'
import type { ComposerDraftStore } from '../../../chat-core/composer/draftStore'
import type { pendingQuestionFor } from '../../../store/chatSlice'
import { appendFollowUpOption, removeFollowUpOption, selectSingleFollowUpOption, type OwnedSuffix } from '../../../lib/followUpToggle'
import type { ChatMessage } from '../../../types'

interface FollowUpChipsOptions {
  messages: ChatMessage[]
  isStreaming: boolean
  /** A pending question card suppresses the chips: only the card can answer it. */
  pendingQuestion: ReturnType<typeof pendingQuestionFor>
  activeSlot: string | null
  inputRef: MutableRefObject<string>
  setInput: ComposerDraftStore['set']
  prefillEdited: boolean
  setPrefillEdited: Dispatch<SetStateAction<boolean>>
}

/**
 * The follow-up option chips under the newest reply: which options apply,
 * which chips the user picked, and ownership of the text they appended
 * (#7616) -- which any direct edit of
 * the composer invalidates, so the composer's change handlers live here too.
 */
export function useFollowUpChips({
  messages,
  isStreaming,
  pendingQuestion,
  activeSlot,
  inputRef,
  setInput,
  prefillEdited,
  setPrefillEdited,
}: FollowUpChipsOptions) {
  // Follow-up options derived from the last assistant message in the current chat.
  // Swapping chats (activeSlot change) → messages change → memo recomputes fresh.
  // A pending question card suppresses them: both would offer the same choices in
  // the same band, and only the card can answer the blocked tool call.
  const { followUpOptions, followUpSourceKey, followUpMulti } = useMemo(
    () => deriveFollowUpOptions(messages, isStreaming, !!pendingQuestion),
    [messages, isStreaming, pendingQuestion],
  )
  // Visual-only highlight state; text in the input is the source of truth for
  // what gets sent. Cleared whenever the options list changes (new assistant
  // message) or the active chat switches — both signal a fresh turn.
  const [followUpPicked, setFollowUpPicked] = useState<Set<string>>(() => new Set())
  // Read by the option handler instead of the state: two clicks landing before a
  // re-render would both see the same set and both take the append branch.
  const followUpPickedRef = useRef(followUpPicked); followUpPickedRef.current = followUpPicked
  // Ownership of the appended suffix, not content-matching (#7616). See
  // lib/followUpToggle (shared with ChatPane): the chips own a recorded
  // (base, options) span, options kept as an ARRAY so a comma-bearing label is
  // one element. Advanced SYNCHRONOUSLY in the click handler, never in a
  // render-time state updater, so StrictMode's double-invocation cannot rebase
  // it on stale state (the #7616 F2 defect).
  const followUpInsertedRef = useRef<OwnedSuffix | null>(null)
  // Any DIRECT user edit of the composer invalidates chip ownership (#7616) —
  // the recorded span describes a chip-produced draft, so once the user types
  // it no longer maps to the live text (even an edit-then-restore). Chip
  // append/remove set the ref themselves and call setInput directly, bypassing
  // this handler, so they are unaffected.
  const clearFollowUpOwnership = useCallback(() => { followUpInsertedRef.current = null }, [])
  // Stable identities for the composer's change handlers. An inline arrow is a
  // new `onChange` on every page render, which re-keys the Composer root's
  // context and re-renders every atom under it.
  const composerRootChange = useCallback((v: string) => { clearFollowUpOwnership(); setInput(v) }, [clearFollowUpOwnership, setInput])
  const prefillEditedRef = useRef(prefillEdited); prefillEditedRef.current = prefillEdited
  const composerUserEdit = useCallback((v: string) => {
    clearFollowUpOwnership(); setInput(v)
    if (!prefillEditedRef.current) setPrefillEdited(true)
  }, [clearFollowUpOwnership, setInput, setPrefillEdited])
  const followUpOptionsKey = followUpOptions.join('\x00')
  // A single-select offer is keyed on its source row too: a newer reply can repeat
  // the same labels, and a single-select pick must not replace text the user picked
  // from the older offer. Multi-select rows keep the label-only reset.
  const singleSourceKey = followUpMulti ? null : followUpSourceKey
  useEffect(() => { setFollowUpPicked(new Set()); followUpInsertedRef.current = null }, [followUpOptionsKey, singleSourceKey, followUpMulti, activeSlot])
  const toggleFollowUpOption = (o: string) => {
    // Regular options: toggle. Click unpicked → append + mark; click
    // picked → try to remove text + unmark (if the user edited the
    // text so it no longer matches, leave text alone — the chip
    // still un-highlights for consistency).
    if (followUpPickedRef.current.has(o)) {
      const next = new Set(followUpPickedRef.current); next.delete(o)
      followUpPickedRef.current = next
      // Synchronous transform on the live draft + ownership refs
      // (#7616): advance both refs and set the value in the click
      // handler, never in a render-time updater, so StrictMode's
      // double-invocation cannot rebase ownership on stale state.
      const r = removeFollowUpOption(inputRef.current, followUpInsertedRef.current, o)
      followUpInsertedRef.current = r.owned
      inputRef.current = r.value
      setInput(r.value)
      setFollowUpPicked(next)
    } else {
      // `[OPTION:]` is single-select: the new pick replaces the previous one.
      const next = followUpMulti ? new Set(followUpPickedRef.current) : new Set<string>(); next.add(o)
      followUpPickedRef.current = next
      const r = followUpMulti
        ? appendFollowUpOption(inputRef.current, followUpInsertedRef.current, o)
        : selectSingleFollowUpOption(inputRef.current, followUpInsertedRef.current, o)
      followUpInsertedRef.current = r.owned
      inputRef.current = r.value
      setInput(r.value)
      setFollowUpPicked(next)
    }
  }
  return {
    followUpOptions, followUpSourceKey, followUpMulti,
    followUpPicked, followUpPickedRef, toggleFollowUpOption,
    composerRootChange, composerUserEdit,
  }
}

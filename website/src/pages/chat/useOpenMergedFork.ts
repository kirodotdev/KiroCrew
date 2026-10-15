import { useCallback } from 'react'
import { useAppDispatch, useAppStore } from '../../store'
import { resumeFromHistory, switchSlot } from '../../store/chatSlice'
import type { MergedFromBlock } from '../../types/mergeBack'

/**
 * Open the fork a merge card came from: switch to it when the open chat on its
 * key is the transcript the card recorded, and otherwise reopen it from History
 * with that identity, so the server refuses a missing, unreadable or different
 * transcript, or an empty identity, before anything opens. The open state is
 * read from the store at click time, so the callback keeps one identity for the
 * transcript renderers that list it as a dependency.
 */
export function useOpenMergedFork(): (block: MergedFromBlock) => void {
  const dispatch = useAppDispatch()
  const store = useAppStore()
  return useCallback((block: MergedFromBlock) => {
    const open = store.getState().dashboard.slots.find(slot => slot.key === block.slot)
    if (open && block.createdAt && open.created === block.createdAt) {
      void dispatch(switchSlot({ key: block.slot, announceOnMissing: true }))
    } else {
      void dispatch(resumeFromHistory({
        key: block.session,
        title: block.title,
        expectedCreatedAt: block.createdAt,
      }))
    }
  }, [dispatch, store])
}

import { useCallback } from 'react'
import { useAppDispatch } from '../../store'
import { resumeFromHistory } from '../../store/chatSlice'
import type { MergedFromBlock } from '../../types/mergeBack'
import { resumeKeyFor } from '../../utils/resumeKey'

/**
 * Open the fork a merge card came from, always through the resume route with
 * the identity the card recorded. That route answers an open fork with its
 * live slot only when the slot holds that transcript, and refuses a missing,
 * unreadable or different transcript, or an empty identity, before anything
 * opens. A local switch cannot: the dashboard's slot list can lag a delete and
 * a recreate on the same key, and a switch opens whatever holds the key now.
 */
export function useOpenMergedFork(): (block: MergedFromBlock) => void {
  const dispatch = useAppDispatch()
  return useCallback((block: MergedFromBlock) => {
    void dispatch(resumeFromHistory({
      key: resumeKeyFor(block.session),
      title: block.title,
      expectedCreatedAt: block.createdAt,
    }))
  }, [dispatch])
}

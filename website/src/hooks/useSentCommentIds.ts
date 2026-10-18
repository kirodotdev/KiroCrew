import { useCallback, useEffect, useState } from 'react'
import { safeSetItem } from '../utils/safeStorage'

/** Sent-to-chat comment ids for one artifact. A corrupt or absent entry reads
 *  as "nothing sent yet", which only ever over-counts the pending batch. */
function readSentIds(key: string): Set<string> {
  try { return new Set<string>(JSON.parse(localStorage.getItem(key) || '[]')) }
  catch { return new Set<string>() }
}

/** Which of an artifact's comments a batch Submit already sent to a chat.
 *
 *  Durable artifact comments survive a submission, so without per-id tracking
 *  every Submit would re-send the whole comment history and the "N comments to
 *  send" count would never reset. The ids persist per artifact under
 *  `mc-cmt-sent:<slug>` (mirroring the `mc-cmt-read:` key) so a remount or a
 *  slot switch does not resurrect a sent batch.
 *
 *  One hook for both Submit surfaces — the chat side panel's `ArtifactPanel`
 *  and the standalone artifact page — so the two cannot drift apart again.
 *  The set is append-only: ids of since-deleted comments linger harmlessly
 *  (pruning against a possibly-stale comment list could resurrect sent ids),
 *  other windows are picked up on remount only, and an edit to a sent comment
 *  does not re-queue it. */
export function useSentCommentIds(slug: string) {
  const sentKey = `mc-cmt-sent:${slug}`
  const [sentIds, setSentIds] = useState<Set<string>>(() => readSentIds(sentKey))
  // Re-read when the host is reused for a different artifact.
  useEffect(() => { setSentIds(readSentIds(sentKey)) }, [sentKey])
  /** Record a delivered batch. Call it only once delivery is proven: nothing
   *  ever removes an id, so a batch marked here is never offered again. */
  const markSent = useCallback((batch: readonly { id: string }[]) => {
    setSentIds(prev => {
      const next = new Set(prev)
      for (const c of batch) next.add(c.id)
      safeSetItem(sentKey, JSON.stringify([...next]))
      return next
    })
  }, [sentKey])
  return { sentIds, markSent }
}

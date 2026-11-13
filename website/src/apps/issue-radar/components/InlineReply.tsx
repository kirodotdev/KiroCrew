// InlineReply — what sits under one timeline comment: the replies to it already
// held in the pending batch, and the reply box when Reply was clicked on it.
//
// A reply with a thread handle goes into that thread; without one (a GitHub
// conversation comment) the box opens pre-filled with a quote of the comment, and
// the reply posts as a new comment. Either way it is written right here, next to
// what it answers, the way the forge web UIs do it.

import { Layers } from 'lucide-react'

import { i18nT } from '../../../i18n/t'
import type { TimelineEvent } from '../api'
import { anchorKeyOf } from '../lib/commentBatch'
import CommentBox from './CommentBox'
import { useOptionalCommentBatch } from './CommentBatchProvider'
import { PendingRow } from './TimelineComposer'

export default function InlineReply({ ev }: { ev: TimelineEvent }) {
  const batch = useOptionalCommentBatch()
  if (!batch?.canWrite) return null
  const key = anchorKeyOf(ev)
  const held = batch.pending.filter((p) => p.anchor === key)
  const open = Boolean(batch.drafts[key])
  if (!held.length && !open) return null

  return (
    <div className="mt-2 ml-4 space-y-2 border-l-2 border-border pl-3">
      {held.length > 0 && (
        <div data-testid="inline-pending">
          <div className="flex items-center gap-1.5 text-[11px] uppercase tracking-wider text-muted font-medium">
            <Layers size={11} /> {i18nT('apps.issueRadar.components.timelineComposer.pending_badge')}
          </div>
          <ul className="py-1 space-y-1">
            {held.map((item) => <PendingRow key={item.id} item={item} place="inline" showTarget={false} />)}
          </ul>
        </div>
      )}
      {open && <CommentBox draftKey={key} />}
    </div>
  )
}

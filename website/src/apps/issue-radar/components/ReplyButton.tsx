// The Reply affordance on a timeline comment card. It opens the inline reply box
// under that comment (see InlineReply); whether the reply threads or quotes is
// decided from the comment's `thread_id`.

import { Reply } from 'lucide-react'

import { i18nT } from '../../../i18n/t'
import type { TimelineEvent } from '../api'
import { useOptionalCommentBatch } from './CommentBatchProvider'

export default function ReplyButton({ ev }: { ev: TimelineEvent }) {
  const batch = useOptionalCommentBatch()
  if (!batch?.canWrite) return null
  const hint = ev.thread_id
    ? i18nT('apps.issueRadar.components.timelineComposer.reply_in_thread_hint')
    : i18nT('apps.issueRadar.components.timelineComposer.quote_reply_hint')
  return (
    <button
      type="button"
      onClick={() => batch.openReply(ev)}
      title={hint}
      aria-label={hint}
      className="ml-auto inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11.5px] text-muted hover:text-text hover:bg-bg-elevated focus-ring"
    >
      <Reply size={12} />
      {i18nT('apps.issueRadar.components.timelineComposer.reply')}
    </button>
  )
}

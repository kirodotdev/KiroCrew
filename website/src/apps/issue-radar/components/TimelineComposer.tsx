// TimelineComposer — the head of an issue's or a PR's timeline: the box for a new
// comment, and the pending batch with Submit / Discard.
//
// Replies are written INLINE, under the comment they answer (see InlineReply);
// every box feeds the same batch, owned by CommentBatchProvider. Submitting:
// on a GitLab merge request the forge publishes the batch as ONE review (draft
// notes); elsewhere items post in order and stop at the first failure, and
// whatever did not post stays pending so nothing is lost and nothing is sent twice.

import { useEffect, useRef, useState } from 'react'
import { Check, CornerDownRight, Layers, Loader2, Pencil, RotateCcw, Trash2, X } from 'lucide-react'

import { Btn } from '../../../components/ui'
import ErrorNotice from '../../../components/ErrorNotice'
import { useImeGuard } from '../../../hooks/useImeGuard'
import { i18nT } from '../../../i18n/t'
import type { PendingComment } from '../lib/commentBatch'
import CommentBox from './CommentBox'
import { TOP, useCommentBatch, type RowPlace } from './CommentBatchProvider'

/** First line of a pending comment, for its row. */
export function preview(body: string): string {
  const line = body.trim().split('\n').find((l) => l.trim() && !l.startsWith('>')) ?? body.trim()
  return line.length > 120 ? `${line.slice(0, 119)}…` : line
}

/** The editor a pending row turns into on Edit: the stored comment is replaced
 * only on Save, so Cancel leaves it as it was. */
function RowEditor({ id, text, saving }: { id: string; text: string; saving: boolean }) {
  const batch = useCommentBatch()
  const textRef = useRef<HTMLTextAreaElement>(null)
  const ime = useImeGuard()
  useEffect(() => {
    const el = textRef.current
    if (!el) return
    el.focus()
    el.setSelectionRange(el.value.length, el.value.length)
  }, [])
  return (
    <div className="rounded-md border border-border bg-card overflow-hidden" data-testid="pending-row-editor">
      <textarea
        ref={textRef}
        value={text}
        onChange={(e) => batch.setEditText(id, e.target.value)}
        // Fixed while Save waits on the batch write, so nothing typed is dropped.
        readOnly={saving}
        aria-busy={saving}
        {...ime.bindComposition<HTMLTextAreaElement>()}
        onKeyDown={(e) => {
          if (e.key === 'Escape') { e.preventDefault(); batch.cancelEdit(id) }
          if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && ime.claimEnter(e)) batch.saveEdit(id)
        }}
        aria-label={i18nT('apps.issueRadar.components.timelineComposer.edit_label')}
        rows={2}
        className="block w-full bg-transparent px-2.5 py-1.5 text-[12.5px] text-text outline-hidden resize-y focus-ring font-body"
      />
      <div className="flex flex-wrap items-center justify-end gap-1.5 px-2.5 py-1.5 border-t border-border bg-bg-elevated/40">
        <Btn onClick={() => batch.cancelEdit(id)} disabled={saving}>
          {i18nT('apps.issueRadar.components.timelineComposer.cancel')}
        </Btn>
        <Btn primary onClick={() => batch.saveEdit(id)} disabled={saving || !text.trim()}>
          <Check className="lucide-inline" />
          {i18nT('apps.issueRadar.components.timelineComposer.save_edit')}
        </Btn>
      </div>
    </div>
  )
}

/** One pending comment: who it answers, its first line, Edit and Remove. On Edit
 * the row itself becomes the editor, so the comment never moves. */
export function PendingRow({
  item, place, showTarget = true,
}: { item: PendingComment; place: RowPlace; showTarget?: boolean }) {
  const batch = useCommentBatch()
  const edit = batch.rowEdits[item.id]
  if (edit && edit.place === place) {
    return <li className="min-w-0"><RowEditor id={item.id} text={edit.text} saving={Boolean(edit.saving)} /></li>
  }
  // A pending reply is listed twice; the copy not being edited says so.
  const editedElsewhere = Boolean(edit)
  const locked = batch.batchBusy || batch.isLocked(item.id)
  // A submit marks what it sends as unconfirmed before the request leaves; while
  // that request is in flight here the row is simply locked, not yet in doubt.
  const unconfirmed = Boolean(item.unconfirmed) && !locked
  return (
    <li className="flex items-center gap-2 text-[12.5px] min-w-0">
      {showTarget && (
        <span className="shrink-0 text-muted">
          {item.reply_to
            ? <><CornerDownRight size={11} className="lucide-inline" /> {item.reply_to}</>
            : i18nT('apps.issueRadar.components.timelineComposer.new_comment')}
        </span>
      )}
      <span className="min-w-0 truncate text-text">{preview(item.body)}</span>
      {unconfirmed && (
        <span
          className="shrink-0 text-[11px] text-warn-text"
          title={i18nT('apps.issueRadar.components.timelineComposer.unconfirmed_hint')}
          data-testid="pending-unconfirmed"
        >
          {i18nT('apps.issueRadar.components.timelineComposer.unconfirmed')}
        </span>
      )}
      {editedElsewhere ? (
        <span className="ml-auto shrink-0 text-[11px] text-muted italic">
          {i18nT('apps.issueRadar.components.timelineComposer.being_edited')}
        </span>
      ) : (
      <span className="ml-auto flex shrink-0 gap-0.5">
        {unconfirmed ? (
          // Its text is kept, but it is sent again only once the user has checked
          // the timeline and says it did not post. Remove dismisses it.
          <Btn
            onClick={() => batch.readdPending(item.id)}
            disabled={locked}
            aria-label={i18nT('apps.issueRadar.components.timelineComposer.readd')}
            title={i18nT('apps.issueRadar.components.timelineComposer.readd_hint')}
            className="px-1 py-0"
          >
            <RotateCcw className="lucide-inline" />
          </Btn>
        ) : (
          <Btn
            onClick={() => batch.startEdit(item, place)}
            disabled={locked}
            aria-label={i18nT('apps.issueRadar.components.timelineComposer.edit')}
            title={i18nT('apps.issueRadar.components.timelineComposer.edit')}
            className="px-1 py-0"
          >
            <Pencil className="lucide-inline" />
          </Btn>
        )}
        <Btn
          onClick={() => batch.removePending(item.id)}
          disabled={locked}
          aria-label={i18nT('apps.issueRadar.components.timelineComposer.remove')}
          title={i18nT('apps.issueRadar.components.timelineComposer.remove')}
          className="px-1 py-0"
        >
          <X className="lucide-inline" />
        </Btn>
      </span>
      )}
    </li>
  )
}

export default function TimelineComposer() {
  const batch = useCommentBatch()
  const [confirmDiscard, setConfirmDiscard] = useState(false)

  if (!batch.canWrite) return null
  const { pending } = batch
  const editing = Object.keys(batch.rowEdits).length > 0
  // Unconfirmed comments wait for the user, so Submit needs at least one other.
  const sendable = pending.some((p) => !p.unconfirmed)
  // An edit whose comment left the batch meanwhile (another tab removed or sent
  // it) has no row to live in. It keeps its editor here, so the text is not lost:
  // Save adds it back as a pending comment, Cancel drops it.
  const orphans = Object.entries(batch.rowEdits).filter(([id]) => !pending.some((p) => p.id === id))

  const discardAll = () => {
    if (!confirmDiscard) { setConfirmDiscard(true); return }
    batch.discardAll()
    setConfirmDiscard(false)
  }

  return (
    <div className="mb-5 space-y-2" data-testid="timeline-composer">
      <CommentBox draftKey={TOP} />

      {(pending.length > 0 || orphans.length > 0) && (
        <div className="rounded-lg border border-border bg-card overflow-hidden" data-testid="pending-batch">
          <div className="flex items-center gap-1.5 px-3 pt-2 text-[11px] uppercase tracking-wider text-muted font-medium">
            <Layers size={12} /> {i18nT('apps.issueRadar.components.timelineComposer.pending_heading')}
            <span className="normal-case tracking-normal">· {pending.length}</span>
          </div>
          <ul className="px-3 py-1.5 space-y-1">
            {pending.map((item) => <PendingRow key={item.id} item={item} place="list" />)}
            {orphans.map(([id, edit]) => (
              <li key={id} className="min-w-0"><RowEditor id={id} text={edit.text} saving={Boolean(edit.saving)} /></li>
            ))}
          </ul>
          {/* No hand-off: the pending batch, held only in this page's memory
              because storage refused it, and any unsaved drafts in the boxes. */}
          <ErrorNotice
            message={batch.batchUnsaved
              ? i18nT('apps.issueRadar.components.timelineComposer.batch_unsaved')
              : undefined}
            className="mx-3 mb-2"
            testId="batch-unsaved"
          />
          {/* No hand-off: the pending batch above, and the comment draft below. */}
          <ErrorNotice message={batch.batchError ?? undefined} onDismiss={batch.clearBatchError} className="mx-3 mb-2" />
          <div className="flex items-center gap-1.5 px-3 pb-2 flex-wrap">
            <span className="text-[11px] text-muted mr-auto">
              {editing
                ? i18nT('apps.issueRadar.components.timelineComposer.finish_edit_first')
                : batch.nativeReview
                ? i18nT('apps.issueRadar.components.timelineComposer.native_hint')
                : i18nT('apps.issueRadar.components.timelineComposer.sequential_hint')}
            </span>
            <Btn onClick={discardAll} disabled={batch.batchBusy}>
              <Trash2 className="lucide-inline" />
              {confirmDiscard
                ? i18nT('apps.issueRadar.components.timelineComposer.confirm_discard')
                : i18nT('apps.issueRadar.components.timelineComposer.discard_all')}
            </Btn>
            <Btn
              primary
              onClick={() => { setConfirmDiscard(false); void batch.submitBatch() }}
              disabled={batch.batchBusy || editing || !sendable}
            >
              {batch.batchBusy
                ? <Loader2 className="lucide-inline animate-spin" />
                : <Check className="lucide-inline" />}
              {i18nT('apps.issueRadar.components.timelineComposer.submit_batch')}
            </Btn>
          </div>
        </div>
      )}
      {/* A submit can empty the batch and still have something to say: comments
          that could not be confirmed leave the batch, and the notice telling the
          user to check the forge must outlive the panel. No hand-off: the comment
          draft in the box above. */}
      {pending.length === 0 && orphans.length === 0 && (
        <ErrorNotice message={batch.batchError ?? undefined} onDismiss={batch.clearBatchError} />
      )}
    </div>
  )
}

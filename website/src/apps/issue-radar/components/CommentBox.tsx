// CommentBox — one comment box: the textarea, Comment/Reply now and Add to batch.
//
// Rendered at the head of the timeline (`draftKey = TOP`, a new comment) and
// inline under a comment being replied to. All state lives in the shared
// CommentBatchProvider, so every box feeds the same pending batch.

import { useEffect, useRef } from 'react'
import { Layers, Loader2, Send, X } from 'lucide-react'

import { Btn } from '../../../components/ui'
import ErrorNotice from '../../../components/ErrorNotice'
import { useImeGuard } from '../../../hooks/useImeGuard'
import { i18nT } from '../../../i18n/t'
import { MAX_PENDING } from '../lib/commentBatch'
import { TOP, useCommentBatch } from './CommentBatchProvider'

export default function CommentBox({ draftKey }: { draftKey: string }) {
  const batch = useCommentBatch()
  const draft = batch.drafts[draftKey]
  const textRef = useRef<HTMLTextAreaElement>(null)
  const rootRef = useRef<HTMLDivElement>(null)
  const ime = useImeGuard()
  const inline = draftKey !== TOP
  const focusSeq = draft?.focusSeq ?? 0

  // A Reply click asks for focus by bumping the seq.
  useEffect(() => {
    if (!focusSeq) return
    rootRef.current?.scrollIntoView?.({ block: 'nearest' })
    requestAnimationFrame(() => {
      const el = textRef.current
      if (!el) return
      el.focus()
      el.setSelectionRange(el.value.length, el.value.length)
    })
  }, [focusSeq])

  if (!draft) return null
  const body = draft.text.trim()
  const batching = batch.pending.length > 0
  const shortcut = batching
    ? i18nT('apps.issueRadar.components.timelineComposer.shortcut_hint_batch')
    : i18nT('apps.issueRadar.components.timelineComposer.shortcut_hint')

  return (
    <div
      ref={rootRef}
      className="rounded-lg border border-border bg-card overflow-hidden"
      data-testid={inline ? 'inline-reply' : 'timeline-composer-box'}
    >
      <div className="relative">
      <textarea
        ref={textRef}
        value={draft.text}
        onChange={(e) => batch.setText(draftKey, e.target.value)}
        {...ime.bindComposition<HTMLTextAreaElement>()}
        onKeyDown={(e) => {
          // Escape closes an inline box only while it holds nothing worth keeping.
          if (e.key === 'Escape' && inline && !body) { e.preventDefault(); batch.close(draftKey) }
          // Cmd/Ctrl+Enter: post now, or, once a batch is pending, add to it — the
          // forge web UIs behave the same way after "Start a review".
          if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
            if (ime.claimEnter(e)) {
              if (batching) batch.addToBatch(draftKey)
              else void batch.post(draftKey)
            }
          }
        }}
        readOnly={draft.busy}
        placeholder={inline
          ? i18nT('apps.issueRadar.components.timelineComposer.reply_placeholder', { author: draft.reply_to ?? 'ghost' })
          : i18nT('apps.issueRadar.components.timelineComposer.placeholder')}
        aria-label={inline
          ? i18nT('apps.issueRadar.components.timelineComposer.reply_label', { author: draft.reply_to ?? 'ghost' })
          : i18nT('apps.issueRadar.components.timelineComposer.title')}
        rows={inline ? 2 : 3}
        className={`block w-full bg-transparent px-3 py-2 text-[13px] text-text placeholder:text-muted outline-hidden resize-y focus-ring font-body${inline ? ' pr-8' : ''}`}
      />
      {/* An inline box closes from the field's own corner, so the footer keeps
          two actions (Add to batch, Reply now). */}
      {inline && (
        <button
          type="button"
          onClick={() => batch.close(draftKey)}
          disabled={draft.busy}
          aria-label={i18nT('apps.issueRadar.components.timelineComposer.cancel')}
          title={i18nT('apps.issueRadar.components.timelineComposer.cancel')}
          className="absolute right-1.5 top-1.5 rounded p-0.5 text-muted hover:text-text hover:bg-bg-elevated focus-ring disabled:opacity-50"
        >
          <X size={13} />
        </button>
      )}
      </div>
      {/* No hand-off: the unsent comment draft in this box's textarea. */}
      <ErrorNotice message={draft.error ?? undefined} onDismiss={() => batch.clearError(draftKey)} className="mx-3 mb-2" />
      {/* Wraps rather than clips: an inline box in a narrow pane, with a long
          translation, is narrower than the shortcut and both buttons together. */}
      <div className="flex flex-wrap items-center justify-end gap-1.5 px-3 py-2 border-t border-border bg-bg-elevated/40">
        <span className="text-[11px] text-muted mr-auto min-w-0">{shortcut}</span>
        {/* The primary button does what Ctrl/Cmd+Enter does: once a batch is
            pending, that is adding to it, and posting now is the plain one. */}
        <Btn
          primary={batching}
          onClick={() => batch.addToBatch(draftKey)}
          disabled={!body || draft.busy || batch.batchFull}
          title={batch.batchFull
            ? i18nT('apps.issueRadar.components.timelineComposer.batch_full', { max: MAX_PENDING })
            : i18nT('apps.issueRadar.components.timelineComposer.add_to_batch_hint')}
        >
          <Layers className="lucide-inline" />
          {i18nT('apps.issueRadar.components.timelineComposer.add_to_batch')}
        </Btn>
        <Btn primary={!batching} onClick={() => void batch.post(draftKey)} disabled={!body || draft.busy}>
          {draft.busy
            ? <Loader2 className="lucide-inline animate-spin" />
            : <Send className="lucide-inline" />}
          {inline
            ? i18nT('apps.issueRadar.components.timelineComposer.reply_now')
            : i18nT('apps.issueRadar.components.timelineComposer.comment_now')}
        </Btn>
      </div>
    </div>
  )
}

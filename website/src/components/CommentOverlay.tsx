import { useState, useRef, useEffect, useCallback } from 'react'
import { MessageSquare, MessageSquarePlus, X, Pencil, Check, Send } from 'lucide-react'
import { SendBtn } from './ui'
import { offlineProps } from '../utils/offline'
import { useImeGuard } from '../hooks/useImeGuard'

import { i18nT } from '../i18n/t'
import { fmtNumber, fmtPercent } from '../i18n/format'
import { shortImageLabel } from '../utils/imageComments'
export interface InlineComment {
  id: string
  /** Anchor text from the document (prefix for matching). */
  anchor: string
  /** User's comment text. */
  text: string
  /** 1-based line number where the anchor starts in the source content, if resolved. */
  line?: number
  /** 1-based column of the first char of the anchor on its source line. */
  column?: number
  /** Character offset of the anchor in the rendered text (textContent space). Used to disambiguate repeated occurrences of the same anchor text. */
  startOffset?: number
  /** Set when the comment is on a box drawn over an image rather than on text.
   *  `line`/`column` then locate the image in the source and `anchor` carries
   *  its alt text, so code that reads only `anchor` still has a label. */
  image?: InlineCommentImage
}

/** The box on an image a comment is about, as fractions (0..1) of the rendered
 *  image: `x`/`y` its top-left corner, `w`/`h` its size. Independent of display
 *  size and pixel density. */
export interface InlineCommentImage {
  /** The image destination exactly as written in the markdown, e.g. `states/a.png`. */
  src: string
  /** The file the preview resolved `src` to and loaded, decoded, which the
   *  agent payload names. Absent when the preview had no file for it. */
  path?: string
  x: number
  y: number
  w: number
  h: number
  /** Which use of the file the box was drawn on: the `nth` (0-based) of `uses`
   *  images with the same `src`, in document order. Lets a box on one of two
   *  uses of a file find the same use again after an edit moves both. */
  nth?: number
  uses?: number
}

/** Single comment row with inline edit support. */
function CommentRow({ comment, number, onEdit, onRemove, onEditingChange }: {
  comment: InlineComment; number: number; onEdit: (id: string, text: string) => void; onRemove: (id: string) => void
  onEditingChange?: (id: string, editing: boolean) => void
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(comment.text)
  const inputRef = useRef<HTMLInputElement>(null)
  const cancelledRef = useRef(false)
  const committedRef = useRef(false)
  const ime = useImeGuard()

  const commitEdit = useCallback(() => {
    if (cancelledRef.current) { cancelledRef.current = false; return }
    if (committedRef.current) return
    const trimmed = draft.trim()
    if (trimmed && trimmed !== comment.text) onEdit(comment.id, trimmed)
    committedRef.current = true
    setEditing(false)
  }, [draft, comment.id, comment.text, onEdit])

  const preventBlur = useCallback((e: React.MouseEvent) => e.preventDefault(), [])

  useEffect(() => { if (editing) { committedRef.current = false; inputRef.current?.focus() } }, [editing])
  // Let the owner know a row holds typed text that is not saved yet, so a
  // late send delivery does not remove the row out from under the edit.
  useEffect(() => {
    if (!editing || !onEditingChange) return
    onEditingChange(comment.id, true)
    return () => onEditingChange(comment.id, false)
  }, [editing, comment.id, onEditingChange])

  return (
    <div data-comment-id={comment.id} className="flex items-start gap-2 text-[13px] bg-bg-elevated rounded-md px-2.5 py-1.5">
      <span className="text-muted shrink-0"><MessageSquare className="lucide-inline" /></span>
      <div className="flex-1 min-w-0">
        {comment.image ? (
          <div className="text-muted text-[11px] font-mono truncate" title={comment.image.src}>{comment.anchor
            ? i18nT('components.commentOverlay.image_anchor', { number: fmtNumber(number), label: shortImageLabel(comment.anchor) })
            // No alt text and no file name (an inline data: image): the row
            // names the box by its spot, as the box on the image is named.
            : i18nT('components.imageComments.box_label', { number: fmtNumber(number), x: fmtPercent(comment.image.x), y: fmtPercent(comment.image.y) })}</div>
        ) : (
          <div className="text-muted text-[11px] font-mono truncate" title={comment.anchor}>"{comment.anchor.slice(0, 60)}{comment.anchor.length > 60 ? '…' : ''}"</div>
        )}
        {editing ? (
          <input ref={inputRef} value={draft} onChange={e => setDraft(e.target.value)}
            {...ime.bindComposition<HTMLInputElement>({ onBlur: commitEdit })}
            onKeyDown={e => {
              if (e.key === 'Enter' && draft.trim()) { if (ime.claimEnter(e)) commitEdit() }
              if (e.key === 'Escape') { ime.reset(); cancelledRef.current = true; setDraft(comment.text); setEditing(false) }
            }}
            className="bg-bg border border-border rounded px-1.5 py-0.5 text-text text-[13px] w-full outline-hidden focus-ring" />
        ) : (
          <div
            role="button"
            tabIndex={0}
            className="text-text cursor-pointer"
            onClick={() => { setDraft(comment.text); setEditing(true) }}
            onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setDraft(comment.text); setEditing(true) } }}
          >{comment.text}</div>
        )}
      </div>
      {editing ? (
        <button aria-label={i18nT('components.commentOverlay.save')} onMouseDown={preventBlur} className="text-ok hover:text-ok text-[12px] shrink-0 cursor-pointer bg-transparent border-none" onClick={commitEdit}><Check className="lucide-inline" /></button>
      ) : (
        <button aria-label={i18nT('components.commentOverlay.edit')} className="text-muted hover:text-accent text-[12px] shrink-0 cursor-pointer bg-transparent border-none" onClick={() => { setDraft(comment.text); setEditing(true) }}><Pencil className="lucide-inline" /></button>
      )}
      <button aria-label={i18nT('components.commentOverlay.remove')} onMouseDown={preventBlur} className="text-muted hover:text-danger text-[12px] shrink-0 cursor-pointer bg-transparent border-none" onClick={() => onRemove(comment.id)}><X className="lucide-inline" /></button>
    </div>
  )
}

/** Pending comments list with batch submit.
 *  When `enableExtraPrompt` is set, an "Add overall instruction" toggle appears in the
 *  header. The optional free-form textarea is hidden by default and only
 *  revealed when the user clicks that toggle, so the default view stays a
 *  single (comment) input box rather than two competing inputs. Its value is
 *  passed to `onSubmitAll` alongside the comments only when it was opened, and
 *  it collapses again after submit. */
function CommentList({ comments, onEdit, onRemove, onSubmitAll, enableExtraPrompt, connected = true, onEditingChange }: {
  comments: InlineComment[]; onEdit: (id: string, text: string) => void; onRemove: (id: string) => void; onSubmitAll: (extraPrompt?: string) => void; enableExtraPrompt?: boolean
  /** Told when a row opens or closes an inline edit (typed, not yet saved). */
  onEditingChange?: (id: string, editing: boolean) => void
  /** Gateway connection flag — mirrors ChatInput's Send gating so a batch
   *  submit can't fire (and clear pending comments) while the send path would
   *  silently refuse it. Defaults true for non-chat embeddings. */
  connected?: boolean
}) {
  const [extraPrompt, setExtraPrompt] = useState('')
  const [showExtraPrompt, setShowExtraPrompt] = useState(false)
  const extraPromptRef = useRef<HTMLTextAreaElement>(null)
  // Focus the textarea when the toggle reveals it.
  useEffect(() => { if (showExtraPrompt) extraPromptRef.current?.focus() }, [showExtraPrompt])
  if (comments.length === 0) return null
  return (
    <div className="border-t border-border bg-chrome px-3 py-2">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <span className="text-[13px] font-semibold text-text">{i18nT('components.commentOverlay.comment', { count: comments.length })} {i18nT('components.commentOverlay.pending')}</span>
          {enableExtraPrompt && (
            <button
              type="button"
              aria-label={i18nT('components.commentOverlay.toggle_additional_prompt')}
              title={i18nT('components.commentOverlay.overall_instruction_hint')}
              aria-pressed={showExtraPrompt}
              onClick={() => setShowExtraPrompt(v => !v)}
              className={`flex items-center gap-1 px-1.5 py-0.5 rounded-md text-[11px] font-medium border cursor-pointer transition-all ${showExtraPrompt ? 'border-accent text-accent bg-accent-subtle' : 'border-border text-muted hover:text-text hover:border-border-strong'}`}
            ><MessageSquarePlus className="lucide-inline" /> {i18nT('components.commentOverlay.add_instruction')}</button>
          )}
        </div>
        <SendBtn disabled={!connected} {...offlineProps(connected, i18nT('utils.offline.submit_comments'), i18nT('components.commentOverlay.submit_all'))} onClick={() => { onSubmitAll(enableExtraPrompt && showExtraPrompt ? extraPrompt : undefined); setExtraPrompt(''); setShowExtraPrompt(false) }}>{i18nT('components.commentOverlay.submit_all')} <Send className="lucide-inline" /></SendBtn>
      </div>
      <div className="space-y-1.5 max-h-[200px] overflow-y-auto">
        {comments.map((c, i) => <CommentRow key={c.id} comment={c} number={i + 1} onEdit={onEdit} onRemove={onRemove} onEditingChange={onEditingChange} />)}
      </div>
      {enableExtraPrompt && showExtraPrompt && (
        <textarea
          ref={extraPromptRef}
          aria-label={i18nT('components.commentOverlay.additional_prompt')}
          placeholder={i18nT('components.commentOverlay.optional_overall_feedback_or_an_extra_instructio')}
          value={extraPrompt}
          onChange={e => setExtraPrompt(e.target.value)}
          rows={2}
          className="mt-2 w-full bg-bg-elevated border border-border rounded-md px-2.5 py-1.5 text-text text-[13px] font-body outline-hidden resize-none focus-ring leading-[18px]"
        />
      )}
    </div>
  )
}

// The agent payload is model-facing text, so it lives behind the `.prompt.ts`
// boundary; re-exported here so its importers keep one import site.
export { formatCommentsMessage } from '../utils/commentsMessage.prompt'

/** Minimal shape of a durable artifact comment for chat-submission formatting.
 *  Projected from `ArtifactComment` so this formatter is independent of the
 *  full type and testable in isolation. */
export interface ArtifactCommentForChat {
  /** The comment text (durable comments store this in `body`). */
  body: string
  /** Anchor quote the comment was attached to, if any. */
  anchor?: { quote?: string } | null
  /** Whether the comment was authored by an agent. NEVER submitted to chat. */
  is_agent?: boolean
}

/** Format durable artifact comments into a structured USER message for the
 *  originating chat session. Mirrors the local-file `formatCommentsMessage`,
 *  including the hardened `esc()` (escapes `\`, `"`, `\n`, `\r`) so a comment
 *  body cannot inject a fake prompt block; keys each comment by its
 *  anchor quote rather than file line/col.
 *
 *  Agent-authored comments are filtered out structurally — the caller
 *  also gates the affordance, so this is defense in depth. */
export function formatArtifactCommentsMessage(
  slug: string,
  name: string,
  comments: ArtifactCommentForChat[],
  extraPrompt?: string,
): string {
  const esc = (s: string) => s.replace(/\\/g, '\\\\').replace(/"/g, '\\"').replace(/\n/g, '\\n').replace(/\r/g, '\\r')
  // Human-only: never submit agent comments to chat.
  const human = comments.filter(c => !c.is_agent)
  // esc() the header too: name/slug are interpolated into the
  // submitted message, so an unescaped `"`/newline here could inject a fake
  // prompt block just like a comment body could.
  const label = name ? `${esc(name)} (${esc(slug)})` : esc(slug)
  const lines = [`[Artifact feedback on ${label} — ${human.length} comment${human.length === 1 ? '' : 's'}]`]
  const instruction = extraPrompt?.trim()
  if (instruction) {
    lines.push(
      '',
      `>>> OVERALL INSTRUCTION (applies to all ${human.length} comment${human.length === 1 ? '' : 's'} below; read first):`,
      esc(instruction),
      '<<<',
    )
  }
  lines.push('')
  human.forEach((c, i) => {
    const quote = c.anchor?.quote ?? ''
    const trimmed = quote.length > 80 ? quote.slice(0, 80) + '…' : quote
    const anchorPart = trimmed ? `"${esc(trimmed)}": ` : ''
    lines.push(`${i + 1}. ${anchorPart}"${esc(c.body)}"`)
  })
  if (instruction) lines.push('', `>>> REMINDER — overall instruction: ${esc(instruction)}`)
  return lines.join('\n')
}

export { CommentList }
export type { InlineComment as Comment }

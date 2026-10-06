import { useState, useRef, useEffect, useCallback } from 'react'
import { MessageSquare, MessageSquarePlus, X, Pencil, Check, Send } from 'lucide-react'
import { SendBtn } from './ui'
import { offlineProps } from '../utils/offline'
import { useImeGuard } from '../hooks/useImeGuard'

import { i18nT } from '../i18n/t'
import { fmtNumber } from '../i18n/format'
import { shortImageLabel } from '../utils/imageComments'
import { sliceCodePoints } from '../utils/codePoints'
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
  /** The anchor was selected inside a code block, whose text is not in that space: no offset exists and the first match elsewhere would be the wrong passage. */
  inCode?: boolean
  /** For an `inCode` anchor: its block, source line and column, packed by `codePassagePosition`. Absent when it could not be packed. */
  codePosition?: number
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

/** Unfinished inline edits of pending comments, by `<list draft key>#<comment
 *  id>`: in memory for this page's lifetime only, like `extraPromptDrafts`.
 *  An edit is kept while its row is open and dropped when it is saved,
 *  cancelled or its comment removed. */
const rowEditDrafts = new Map<string, string>()

/** Single comment row with inline edit support. */
function CommentRow({ comment, number, onEdit, onRemove, onEditingChange, draftKey }: {
  comment: InlineComment; number: number; onEdit: (id: string, text: string) => void; onRemove: (id: string) => void
  onEditingChange?: (id: string, editing: boolean) => void
  /** Keeps an open edit across an unmount; see `rowEditDrafts`. */
  draftKey?: string
}) {
  const editKey = draftKey !== undefined ? `${draftKey}#${comment.id}` : undefined
  const keptEdit = editKey !== undefined ? rowEditDrafts.get(editKey) : undefined
  const [editing, setEditing] = useState(keptEdit !== undefined)
  const [draft, setDraft] = useState(keptEdit ?? comment.text)
  const inputRef = useRef<HTMLInputElement>(null)
  const cancelledRef = useRef(false)
  const committedRef = useRef(false)
  const ime = useImeGuard()
  // Dropped here, not only when `editing` turns false: a save that lands as
  // the row unmounts never re-runs the effect below.
  const dropEdit = useCallback(() => { if (editKey !== undefined) rowEditDrafts.delete(editKey) }, [editKey])
  useEffect(() => {
    if (editKey !== undefined && editing) rowEditDrafts.set(editKey, draft)
  }, [editKey, editing, draft])

  const commitEdit = useCallback(() => {
    if (cancelledRef.current) { cancelledRef.current = false; return }
    if (committedRef.current) return
    const trimmed = draft.trim()
    if (trimmed && trimmed !== comment.text) onEdit(comment.id, trimmed)
    committedRef.current = true
    dropEdit()
    setEditing(false)
  }, [draft, comment.id, comment.text, onEdit, dropEdit])

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
          <div className="text-muted text-[11px] font-mono truncate" title={comment.image.src}>{i18nT('components.commentOverlay.image_anchor', { number: fmtNumber(number), label: shortImageLabel(comment.anchor) })}</div>
        ) : (
          <div className="text-muted text-[11px] font-mono truncate" title={comment.anchor}>"{comment.anchor.slice(0, 60)}{comment.anchor.length > 60 ? '…' : ''}"</div>
        )}
        {editing ? (
          <input ref={inputRef} value={draft} onChange={e => setDraft(e.target.value)}
            {...ime.bindComposition<HTMLInputElement>({ onBlur: commitEdit })}
            onKeyDown={e => {
              if (e.key === 'Enter' && draft.trim()) { if (ime.claimEnter(e)) commitEdit() }
              if (e.key === 'Escape') { ime.reset(); cancelledRef.current = true; dropEdit(); setDraft(comment.text); setEditing(false) }
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
      <button aria-label={i18nT('components.commentOverlay.remove')} onMouseDown={preventBlur} className="text-muted hover:text-danger text-[12px] shrink-0 cursor-pointer bg-transparent border-none" onClick={() => { dropEdit(); onRemove(comment.id) }}><X className="lucide-inline" /></button>
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
/** Unsent "Add instruction" text per document, in memory for this page's
 *  lifetime only: never persisted to storage or sent anywhere. The key names
 *  the chat as well as the document (see `MarkdownPanel`), so one chat's draft
 *  never shows in another. */
const extraPromptDrafts = new Map<string, { text: string; open: boolean }>()

/** The instruction-draft key for a document a side-panel tab shows: the tab's
 *  chat-scoped identity (slot + tab id) and the gateway-local path. No tab
 *  identity, no key: such a draft is not kept. */
export function instructionDraftKeyFor(tabKey: string | undefined, filePath: string): string | undefined {
  return tabKey ? `local:${tabKey}:${filePath}` : undefined
}

function CommentList({ comments, onEdit, onRemove, onSubmitAll, enableExtraPrompt, connected = true, onEditingChange, draftKey }: {
  comments: InlineComment[]; onEdit: (id: string, text: string) => void; onRemove: (id: string) => void; onSubmitAll: (extraPrompt?: string) => void; enableExtraPrompt?: boolean
  /** Keeps the typed instruction across an unmount (the panel withholding the
   *  tab for a moment, say), keyed by the document it belongs to. */
  draftKey?: string
  /** Told when a row opens or closes an inline edit (typed, not yet saved). */
  onEditingChange?: (id: string, editing: boolean) => void
  /** Gateway connection flag — mirrors ChatInput's Send gating so a batch
   *  submit can't fire (and clear pending comments) while the send path would
   *  silently refuse it. Defaults true for non-chat embeddings. */
  connected?: boolean
}) {
  const kept = draftKey !== undefined ? extraPromptDrafts.get(draftKey) : undefined
  const [extraPrompt, setExtraPrompt] = useState(kept?.text ?? '')
  const [showExtraPrompt, setShowExtraPrompt] = useState(kept?.open ?? false)
  // Kept only while the box is open with text and comments to send it with:
  // closing the box or removing every comment is a discard, and Submit All
  // empties it.
  const keep = showExtraPrompt && extraPrompt !== '' && comments.length > 0
  useEffect(() => {
    if (draftKey === undefined) return
    if (keep) extraPromptDrafts.set(draftKey, { text: extraPrompt, open: true })
    else extraPromptDrafts.delete(draftKey)
  }, [draftKey, keep, extraPrompt])
  // An edit kept for a comment that is no longer pending (sent, or removed
  // while the list was away) has nothing to return to.
  const pendingIds = comments.map(c => c.id).join('\n')
  useEffect(() => {
    if (draftKey === undefined) return
    const live = new Set(pendingIds === '' ? [] : pendingIds.split('\n'))
    for (const key of rowEditDrafts.keys()) {
      if (key.startsWith(`${draftKey}#`) && !live.has(key.slice(draftKey.length + 1))) rowEditDrafts.delete(key)
    }
  }, [draftKey, pendingIds])
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
        {comments.map((c, i) => <CommentRow key={c.id} comment={c} number={i + 1} onEdit={onEdit} onRemove={onRemove} onEditingChange={onEditingChange} draftKey={draftKey} />)}
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

// Re-exported so its importers keep one import site.
export { sliceCodePoints }

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
    const trimmed = quote.length > 80 ? sliceCodePoints(quote, 0, 80) + '…' : quote
    const anchorPart = trimmed ? `"${esc(trimmed)}": ` : ''
    lines.push(`${i + 1}. ${anchorPart}"${esc(c.body)}"`)
  })
  if (instruction) lines.push('', `>>> REMINDER — overall instruction: ${esc(instruction)}`)
  return lines.join('\n')
}

export { CommentList }
export type { InlineComment as Comment }

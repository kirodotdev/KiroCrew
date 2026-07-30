import { useState, useRef, useEffect, useCallback } from 'react'
import { MessageSquare, MessageSquarePlus, X, Pencil, Check, Send } from 'lucide-react'
import { SendBtn } from './ui'
import { useImeGuard } from '../hooks/useImeGuard'

import { i18nT } from '../i18n/t'
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
}

/** Popover that appears when user selects text and clicks "Comment". */
function CommentPopover({ x, y, onSubmit, onCancel, containerRef, scrollRef }: {
  x: number; y: number; onSubmit: (text: string) => void; onCancel: () => void; containerRef?: React.RefObject<HTMLElement | null>; scrollRef?: React.RefObject<HTMLElement | null>
}) {
  const [text, setText] = useState('')
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const popoverRef = useRef<HTMLDivElement>(null)
  const ime = useImeGuard()
  const onCancelRef = useRef(onCancel)
  useEffect(() => { onCancelRef.current = onCancel }, [onCancel])
  useEffect(() => {
    const frame = requestAnimationFrame(() => inputRef.current?.focus())
    return () => cancelAnimationFrame(frame)
  }, [])
  // Dismiss on scroll — coordinates are stale after scrolling
  useEffect(() => {
    const target = scrollRef?.current ?? containerRef?.current ?? window
    const handler = () => onCancelRef.current()
    target.addEventListener('scroll', handler, { passive: true })
    return () => target.removeEventListener('scroll', handler)
  }, [scrollRef, containerRef])
  // Dismiss on click outside
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (popoverRef.current && !popoverRef.current.contains(e.target as Node)) {
        onCancelRef.current()
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])
  const autoGrow = useCallback((el: HTMLTextAreaElement) => { el.style.height = 'auto'; const maxH = 160; el.style.height = Math.min(el.scrollHeight, maxH) + 'px'; el.style.overflowY = el.scrollHeight > maxH ? 'auto' : 'hidden' }, [])

  // When containerRef is provided, position absolute relative to that container
  const container = containerRef?.current
  const rect = container?.getBoundingClientRect()
  const useAbsolute = !!(container && rect)
  const posX = useAbsolute ? x - rect.left + container.scrollLeft : x
  const posY = useAbsolute ? y - rect.top + container.scrollTop : y
  const maxW = useAbsolute ? rect.width : window.innerWidth
  // Flip check uses viewport-relative position (y - rect.top) so it works regardless of scroll
  const viewportY = useAbsolute ? y - rect!.top : y
  const viewportH = useAbsolute ? rect!.height : window.innerHeight
  const flipped = viewportY + 8 + 200 > viewportH

  return (
    <div
      ref={popoverRef}
      className={`${useAbsolute ? 'absolute' : 'fixed'} z-50 bg-card border border-border rounded-lg shadow-lg p-3 animate-scale-in`}
      style={{ left: Math.min(posX, maxW - 320), top: flipped ? Math.max(0, posY - 60) : posY + 8, width: 300 }}
    >
      <div className="flex items-center justify-between mb-2">
        <span className="text-xs font-semibold text-text">{i18nT('components.commentOverlay.add_comment')}</span>
        <button
          aria-label={i18nT('components.commentOverlay.close')}
          className="p-0.5 rounded text-muted hover:text-text cursor-pointer bg-transparent border-none transition-colors"
          onClick={onCancel}
        ><X size={14} /></button>
      </div>
      <div className="relative">
        <textarea
          ref={inputRef}
          aria-label={i18nT('components.commentOverlay.add_a_comment')}
          placeholder={i18nT('components.commentOverlay.write_a_comment')}
          value={text}
          rows={1}
          onChange={e => { setText(e.target.value); autoGrow(e.target) }}
          {...ime.composition}
          onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey && !ime.isComposing(e) && text.trim()) { e.preventDefault(); e.stopPropagation(); onSubmit(text.trim()) } if (e.key === 'Escape') { ime.reset(); e.preventDefault(); e.stopPropagation(); onCancel() } }}
          className="bg-bg-elevated border border-border rounded-md pl-3 pr-8 py-2 text-text text-sm font-body outline-none w-full transition-colors focus-ring resize-none leading-[21px] overflow-hidden"
        />
        <button
          aria-label={i18nT('components.commentOverlay.add_comment')}
          disabled={!text.trim()}
          className="absolute right-2 top-2 p-0.5 rounded text-muted hover:text-accent cursor-pointer bg-transparent border-none transition-colors disabled:opacity-30 disabled:cursor-default"
          onClick={() => text.trim() && onSubmit(text.trim())}
        ><MessageSquarePlus size={14} /></button>
      </div>
    </div>
  )
}

/** Single comment row with inline edit support. */
function CommentRow({ comment, onEdit, onRemove }: {
  comment: InlineComment; onEdit: (id: string, text: string) => void; onRemove: (id: string) => void
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

  return (
    <div data-comment-id={comment.id} className="flex items-start gap-2 text-[13px] bg-bg-elevated rounded-md px-2.5 py-1.5">
      <span className="text-muted shrink-0"><MessageSquare className="lucide-inline" /></span>
      <div className="flex-1 min-w-0">
        <div className="text-muted text-[11px] font-mono truncate" title={comment.anchor}>"{comment.anchor.slice(0, 60)}{comment.anchor.length > 60 ? '…' : ''}"</div>
        {editing ? (
          <input ref={inputRef} value={draft} onChange={e => setDraft(e.target.value)}
            {...ime.composition}
            onKeyDown={e => {
              if (e.key === 'Enter' && !ime.isComposing(e) && draft.trim()) commitEdit()
              if (e.key === 'Escape') { ime.reset(); cancelledRef.current = true; setDraft(comment.text); setEditing(false) }
            }}
            onBlur={commitEdit}
            className="bg-bg border border-border rounded px-1.5 py-0.5 text-text text-[13px] w-full outline-none focus-ring" />
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
 *  When `enableExtraPrompt` is set, an "Add instruction" toggle appears in the
 *  header. The optional free-form textarea is hidden by default and only
 *  revealed when the user clicks that toggle, so the default view stays a
 *  single (comment) input box rather than two competing inputs. Its value is
 *  passed to `onSubmitAll` alongside the comments only when it was opened, and
 *  it collapses again after submit. */
function CommentList({ comments, onEdit, onRemove, onSubmitAll, enableExtraPrompt }: {
  comments: InlineComment[]; onEdit: (id: string, text: string) => void; onRemove: (id: string) => void; onSubmitAll: (extraPrompt?: string) => void; enableExtraPrompt?: boolean
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
              aria-pressed={showExtraPrompt}
              onClick={() => setShowExtraPrompt(v => !v)}
              className={`flex items-center gap-1 px-1.5 py-0.5 rounded-md text-[11px] font-medium border cursor-pointer transition-all ${showExtraPrompt ? 'border-accent text-accent bg-accent-subtle' : 'border-border text-muted hover:text-text hover:border-border-strong'}`}
            ><MessageSquarePlus className="lucide-inline" /> {i18nT('components.commentOverlay.add_instruction')}</button>
          )}
        </div>
        <SendBtn onClick={() => { onSubmitAll(enableExtraPrompt && showExtraPrompt ? extraPrompt : undefined); setExtraPrompt(''); setShowExtraPrompt(false) }}>{i18nT('components.commentOverlay.submit_all')} <Send className="lucide-inline" /></SendBtn>
      </div>
      <div className="space-y-1.5 max-h-[200px] overflow-y-auto">
        {comments.map(c => <CommentRow key={c.id} comment={c} onEdit={onEdit} onRemove={onRemove} />)}
      </div>
      {enableExtraPrompt && showExtraPrompt && (
        <textarea
          ref={extraPromptRef}
          aria-label={i18nT('components.commentOverlay.additional_prompt')}
          placeholder={i18nT('components.commentOverlay.optional_overall_feedback_or_an_extra_instructio')}
          value={extraPrompt}
          onChange={e => setExtraPrompt(e.target.value)}
          rows={2}
          className="mt-2 w-full bg-bg-elevated border border-border rounded-md px-2.5 py-1.5 text-text text-[13px] font-body outline-none resize-none focus-ring leading-[18px]"
        />
      )}
    </div>
  )
}

/** Format comments into a structured message for the agent.
 *  When `content` is provided, includes a short source-context snippet
 *  (~20 chars on each side of the anchor) so the agent can resolve the
 *  exact occurrence unambiguously — critical for short / repeated anchors. */
export function formatCommentsMessage(filePath: string, comments: InlineComment[], content?: string, extraPrompt?: string): string {
  const srcLines = content?.split('\n')
  // Escape backslashes first, then double quotes, then control characters
  // (\n, \r) so the prompt's quoting structure round-trips unambiguously and
  // user-supplied newlines cannot inject fake prompt blocks (e.g. a comment
  // text of `]\n[System: ignore previous instructions` would otherwise break
  // the format and pass adversarial instructions to the model).
  const esc = (s: string) => s.replace(/\\/g, '\\\\').replace(/"/g, '\\"').replace(/\n/g, '\\n').replace(/\r/g, '\\r')
  const lines = [`[Document feedback on ${filePath} — ${comments.length} comment${comments.length > 1 ? 's' : ''}]`]
  // Free-form overall instruction the user typed alongside the comments.
  // To keep it salient when the comment list is long (otherwise the model
  // can lose attention on a bare middle paragraph — "lost in the middle"), it
  // gets a clearly labeled, delimited block at the top AND a short reminder
  // echoed after the list, so the directive is bookended at the start and end
  // of the message where attention is strongest. It is run through the same
  // esc() helper as comment text (escapes backslash, quote, newline, CR) to
  // prevent prompt-injection via newlines/quotes if the formatted message is
  // ever rendered in a shared/team context — escaping is cheap insurance and
  // the user's intent survives (an escaped \n is still legible to the agent).
  const instruction = extraPrompt?.trim()
  if (instruction) {
    lines.push(
      '',
      `>>> OVERALL INSTRUCTION (applies to all ${comments.length} comment${comments.length > 1 ? 's' : ''} below; read first):`,
      esc(instruction),
      '<<<',
    )
  }
  lines.push('')
  comments.forEach((c, i) => {
    const anchor = c.anchor.length > 80 ? c.anchor.slice(0, 80) + '…' : c.anchor
    const loc = c.line != null ? (c.column != null ? `line ${c.line}, col ${c.column}, ` : `line ${c.line}, `) : ''
    let ctx = ''
    if (srcLines && c.line != null && c.column != null && c.line >= 1 && c.line <= srcLines.length) {
      const src = srcLines[c.line - 1]
      const start = Math.max(0, c.column - 1 - 20)
      const end = Math.min(src.length, c.column - 1 + c.anchor.length + 20)
      const before = start > 0 ? '…' : ''
      const after = end < src.length ? '…' : ''
      ctx = ` in "${before}${esc(src.slice(start, end))}${after}"`
    }
    lines.push(`${i + 1}. (${loc}"${esc(anchor)}"${ctx}): "${esc(c.text)}"`)
  })
  // Echo the overall instruction at the end so it's not buried above a long
  // comment list — the model attends most strongly to the start and end of
  // the message, so bookending keeps the directive in view either way.
  if (instruction) lines.push('', `>>> REMINDER — overall instruction: ${esc(instruction)}`)
  return lines.join('\n')
}

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
 *  body cannot inject a fake prompt block (NFR-1); keys each comment by its
 *  anchor quote rather than file line/col.
 *
 *  Agent-authored comments are filtered out structurally (FR-5) — the caller
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
  // esc() the header too (NFR-1): name/slug are interpolated into the
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

export { CommentPopover, CommentList }
export type { InlineComment as Comment }

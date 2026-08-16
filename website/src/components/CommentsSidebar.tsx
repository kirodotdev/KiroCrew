import { memo, useMemo, useRef, useState, useCallback, useEffect } from 'react'
import {
  MessageSquare, X, RefreshCw, Send, Bot, CheckCircle2, Eye, CornerDownRight,
  AlertTriangle, ChevronRight, Sparkles, Plus, RotateCcw, Link2, Pencil,
} from 'lucide-react'
import type { ArtifactComment } from '../types'
import { useImeGuard } from '../hooks/useImeGuard'
import { useAutoGrowTextarea } from '../hooks/useAutoGrowTextarea'

import { i18nT } from '../i18n/t'
import { fmtDateFields } from '../i18n/format'
/** Short relative-ish timestamp for a comment row. */
function fmtTs(ts: string): string {
  if (!ts) return ''
  const d = new Date(ts)
  if (isNaN(d.getTime())) return ts
  return fmtDateFields(d, {
    month: 'numeric', day: 'numeric', hour: 'numeric', minute: '2-digit',
  })
}

/** Display name for a comment author. */
function authorName(c: ArtifactComment): string {
  if (c.author) return c.author
  return c.is_agent ? i18nT('components.commentsSidebar.agent') : i18nT('components.commentsSidebar.unknown')
}

/** Initials for the small author avatar. */
function initials(c: ArtifactComment): string {
  const n = authorName(c)
  const parts = n.split(/[\s._-]+/).filter(Boolean)
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase()
  return n.slice(0, 2).toUpperCase()
}

/**
 * Catalog KEY for each provider sync state that warrants a warning.
 *
 * Keys, not strings: this table is evaluated at module load, so an `i18nT()` call
 * here would freeze the boot language and never re-resolve on a language switch.
 * The lookup happens in `syncWarnText()`, which runs during render.
 */
const SYNC_WARN_KEY: Record<string, string> = {
  push_failed: 'components.commentsSidebar.failed_to_sync_to_provider',
  pending_push: 'components.commentsSidebar.pending_sync_to_provider',
}

/**
 * Warning for a sync state, or `undefined` when that state needs none.
 *
 * `undefined` rather than the state name: the call site joins this with the
 * orphan warning through `.filter(Boolean)`, so returning the raw state would
 * render a backend identifier as a user-facing warning for every healthy comment.
 */
function syncWarnText(state: string): string | undefined {
  // `hasOwnProperty`, not `in`: the state comes off an API response, so a backend
  // reporting `toString` would otherwise resolve to an inherited
  // Object.prototype member and hand a function to i18next.
  return Object.prototype.hasOwnProperty.call(SYNC_WARN_KEY, state)
    ? i18nT(SYNC_WARN_KEY[state])
    : undefined
}

/** Catalog KEY for the warning shown when the anchored text no longer exists in
 *  the content (backend rescans anchors on every content write —
 *  `anchor_orphaned` is a dedicated field, independent of provider
 *  `sync_state`). Resolved at the call site, which runs during render. */
const ORPHAN_WARN_KEY = 'components.commentsSidebar.anchor_text_no_longer_found_in_content'

/** A small inline reply composer used under a root thread. */
export function ReplyBox({ onSubmit, onCancel }: { onSubmit: (text: string) => void; onCancel: () => void }) {
  const [text, setText] = useState('')
  const ref = useRef<HTMLTextAreaElement>(null)
  const ime = useImeGuard()
  useAutoGrowTextarea(ref, text)
  useEffect(() => { ref.current?.focus() }, [])
  return (
    <div className="mt-1.5 pl-5">
      <textarea
        ref={ref}
        value={text}
        rows={2}
        placeholder={i18nT('components.commentsSidebar.reply')}
        onChange={e => setText(e.target.value)}
        {...ime.composition}
        onKeyDown={e => {
          if (e.key === 'Enter' && !e.shiftKey && !ime.isComposing(e) && text.trim()) {
            e.preventDefault(); onSubmit(text.trim())
          }
          if (e.key === 'Escape') { e.preventDefault(); onCancel() }
        }}
        className="w-full bg-bg-elevated border border-border rounded-md px-2 py-1.5 text-text text-[13px] font-body outline-none resize-none focus-ring leading-[18px]"
      />
      <div className="flex items-center justify-end gap-1.5 mt-1">
        <button
          type="button"
          onClick={onCancel}
          className="px-2 py-0.5 rounded text-[11px] text-muted hover:text-text bg-transparent border-none cursor-pointer"
        >{i18nT('components.commentsSidebar.cancel')}</button>
        <button
          type="button"
          disabled={!text.trim()}
          onClick={() => text.trim() && onSubmit(text.trim())}
          className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[11px] font-medium border border-accent text-accent-fg bg-accent cursor-pointer hover:bg-accent-hover disabled:opacity-40 disabled:cursor-default"
        ><Send size={11} /> {i18nT('components.commentsSidebar.reply_2')}</button>
      </div>
    </div>
  )
}

/** Inline editor for a comment body (mirrors ReplyBox, seeded with the current
 *  text). Enter saves, Escape cancels. Rendered in place of the body inside the
 *  comment card, so it carries no left indent. */
export function EditBox({ initial, onSubmit, onCancel }: { initial: string; onSubmit: (text: string) => void; onCancel: () => void }) {
  const [text, setText] = useState(initial)
  const ref = useRef<HTMLTextAreaElement>(null)
  const ime = useImeGuard()
  useAutoGrowTextarea(ref, text)
  useEffect(() => { const el = ref.current; if (el) { el.focus(); el.select() } }, [])
  return (
    <div role="presentation" onClick={e => e.stopPropagation()}>
      <textarea
        ref={ref}
        value={text}
        rows={2}
        placeholder={i18nT('components.commentsSidebar.edit_comment')}
        onChange={e => setText(e.target.value)}
        {...ime.composition}
        onKeyDown={e => {
          if (e.key === 'Enter' && !e.shiftKey && !ime.isComposing(e) && text.trim()) {
            e.preventDefault(); e.stopPropagation(); onSubmit(text.trim())
          }
          if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); onCancel() }
        }}
        className="w-full bg-bg-elevated border border-border rounded-md px-2 py-1.5 text-text text-[13px] font-body outline-none resize-none focus-ring leading-[18px]"
      />
      <div className="flex items-center justify-end gap-1.5 mt-1">
        <button
          type="button"
          onClick={onCancel}
          className="px-2 py-0.5 rounded text-[11px] text-muted hover:text-text bg-transparent border-none cursor-pointer"
        >{i18nT('components.commentsSidebar.cancel')}</button>
        <button
          type="button"
          disabled={!text.trim()}
          onClick={() => text.trim() && onSubmit(text.trim())}
          className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[11px] font-medium border border-accent text-accent-fg bg-accent cursor-pointer hover:bg-accent-hover disabled:opacity-40 disabled:cursor-default"
        ><Send size={11} /> {i18nT('components.commentsSidebar.save')}</button>
      </div>
    </div>
  )
}

/** Single comment row (root or reply). Replies render indented with no anchor. */
export function CommentRow({
  comment, isReply, active, restrictActions, hideResolve, hideDelete, onReply, onResolve, onMarkReview, onReopen, onDelete, onBodyClick,
  editing, onEdit, onEditSubmit, onEditCancel,
}: {
  comment: ArtifactComment
  isReply: boolean
  active?: boolean
  restrictActions?: boolean
  hideResolve?: boolean
  hideDelete?: boolean
  onReply: (c: ArtifactComment) => void
  onResolve: (c: ArtifactComment) => void
  onMarkReview: (c: ArtifactComment) => void
  onReopen?: (c: ArtifactComment) => void
  onDelete: (c: ArtifactComment) => void
  onBodyClick?: (c: ArtifactComment) => void
  editing?: boolean
  onEdit?: (c: ArtifactComment) => void
  onEditSubmit?: (text: string) => void
  onEditCancel?: () => void
}) {
  const quote = comment.anchor?.quote
  // Sync (push) state and anchor orphaning are independent signals — a
  // pending_push comment can also be orphaned — so surface both rather than
  // letting one clobber the other (they're separate backend fields for the
  // same reason).
  const syncWarn = [
    syncWarnText(comment.sync_state),
    comment.anchor_orphaned ? i18nT(ORPHAN_WARN_KEY) : undefined,
  ].filter(Boolean).join(' · ') || undefined
  // Comments mirrored in from an external publishing provider can't be resolved
  // or deleted from KiroCrew — those are human-only actions on the provider.
  // Hide Resolve/Reopen/Delete per-comment (Reply + Review still work). This is
  // origin-driven so a mixed thread (local + provider) hides correctly.
  const isProvider = !!comment.origin && comment.origin !== 'local'
  const hideResolveEff = hideResolve || isProvider
  const hideDeleteEff = hideDelete || isProvider
  // Edit is offered for LOCAL comments (any author, including the agent's own).
  // Provider-origin comments have no in-place remote-edit path in this edition,
  // so Edit is hidden there. Gated further on onEdit being wired by the parent.
  const canEdit = !isProvider
  const isEditing = !!editing && !!onEditSubmit
  return (
    <div className={`${isReply ? 'ml-3.5 pl-2 border-l-2 border-border' : ''} group${comment.anchor_orphaned ? ' opacity-60' : ''}`}>
      <div
        onClick={onBodyClick ? () => onBodyClick(comment) : undefined}
        role={onBodyClick ? 'button' : undefined}
        tabIndex={onBodyClick ? 0 : undefined}
        onKeyDown={onBodyClick ? (e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onBodyClick(comment) } }) : undefined}
        title={onBodyClick ? i18nT('components.commentsSidebar.scroll_to_the_highlighted_text') : undefined}
        className={`rounded-lg border px-3 py-2.5 shadow-sm transition-colors ${onBodyClick ? 'cursor-pointer' : ''} ${
          active
            ? 'border-accent bg-accent-subtle ring-1 ring-accent/50'
            : 'border-border bg-card hover:border-border-strong'
        }`}
      >
        {/* anchor preview (roots only) */}
        {!isReply && quote && (
          <div
            className="text-[11px] text-muted font-mono mb-1.5 truncate border-l-2 border-accent/40 pl-1.5"
            title={quote}
          >{quote.slice(0, 80)}{quote.length > 80 ? '…' : ''}</div>
        )}
        {/* header: avatar + author + time + lightweight source */}
        <div className="flex items-center gap-1.5 mb-1">
          <span
            className="flex items-center justify-center w-5 h-5 rounded-full bg-bg-elevated text-[10px] font-semibold text-muted shrink-0"
            aria-hidden="true"
          >{initials(comment)}</span>
          <span className="text-[12px] font-semibold text-text-strong truncate">{authorName(comment)}</span>
          <span className="text-[10px] text-muted shrink-0">{fmtTs(comment.created_at)}</span>
          {comment.is_agent && <Bot size={11} className="text-accent shrink-0" aria-label={i18nT('components.commentsSidebar.ai_agent')} />}
          {comment.scope === 'shared' && <Link2 size={11} className="text-muted shrink-0" aria-label={i18nT('components.commentsSidebar.shared_comment')} />}
          {syncWarn && <AlertTriangle size={11} className="text-warn shrink-0" aria-label={syncWarn} />}
        </div>
        {/* body (or inline editor when editing) */}
        {isEditing ? (
          <EditBox
            initial={comment.body}
            onSubmit={onEditSubmit as (text: string) => void}
            onCancel={onEditCancel || (() => {})}
          />
        ) : (
          <div className="text-[13px] text-text whitespace-pre-wrap break-words">{comment.body}</div>
        )}
      </div>
      {/* actions — always visible, comfortable touch targets */}
      <div className="flex items-center gap-1 mt-1.5 px-0.5" style={isEditing ? { display: 'none' } : undefined}>
        <button
          type="button"
          onClick={() => onReply(comment)}
          className="inline-flex items-center gap-1 text-[12px] px-2 py-1 rounded-md text-muted hover:text-accent hover:bg-accent-subtle bg-transparent border-none cursor-pointer transition-colors"
          title={i18nT('components.commentsSidebar.reply_2')}
        ><CornerDownRight size={13} /> {i18nT('components.commentsSidebar.reply_2')}</button>
        {canEdit && onEdit && (
          <button
            type="button"
            onClick={() => onEdit(comment)}
            className="inline-flex items-center gap-1 text-[12px] px-2 py-1 rounded-md text-muted hover:text-accent hover:bg-accent-subtle bg-transparent border-none cursor-pointer transition-colors"
            title={i18nT('components.commentsSidebar.edit_comment_2')}
            style={restrictActions ? { display: 'none' } : undefined}
          ><Pencil size={13} /> {i18nT('components.commentsSidebar.edit')}</button>
        )}
        {!isReply && comment.status === 'open' && (
          <button
            type="button"
            onClick={() => onMarkReview(comment)}
            className="inline-flex items-center gap-1 text-[12px] px-2 py-1 rounded-md text-muted hover:text-warn hover:bg-warn-subtle bg-transparent border-none cursor-pointer transition-colors"
            title={i18nT('components.commentsSidebar.advance_to_review')}
            style={restrictActions ? { display: 'none' } : undefined}
          ><Eye size={13} /> {i18nT('components.commentsSidebar.review')}</button>
        )}
        {!isReply && comment.status !== 'resolved' && (
          <button
            type="button"
            onClick={() => onResolve(comment)}
            className="inline-flex items-center gap-1 text-[12px] px-2 py-1 rounded-md text-muted hover:text-ok hover:bg-ok-subtle bg-transparent border-none cursor-pointer transition-colors"
            title={i18nT('components.commentsSidebar.resolve_human_only')}
            style={(restrictActions || hideResolveEff) ? { display: 'none' } : undefined}
          ><CheckCircle2 size={13} /> {i18nT('components.commentsSidebar.resolve')}</button>
        )}
        {!isReply && comment.status === 'resolved' && onReopen && (
          <button
            type="button"
            onClick={() => onReopen(comment)}
            className="inline-flex items-center gap-1 text-[12px] px-2 py-1 rounded-md text-muted hover:text-accent hover:bg-accent-subtle bg-transparent border-none cursor-pointer transition-colors"
            title={i18nT('components.commentsSidebar.reopen_this_thread')}
            style={(restrictActions || hideResolveEff) ? { display: 'none' } : undefined}
          ><RotateCcw size={13} /> {i18nT('components.commentsSidebar.reopen')}</button>
        )}
        <button
          type="button"
          onClick={() => onDelete(comment)}
          className="inline-flex items-center gap-1 text-[12px] px-2 py-1 rounded-md text-muted hover:text-danger hover:bg-danger-subtle bg-transparent border-none cursor-pointer transition-colors ml-auto"
          title={i18nT('components.commentsSidebar.delete')}
          style={(restrictActions || hideDeleteEff) ? { display: 'none' } : undefined}
        ><X size={13} /> {i18nT('components.commentsSidebar.delete')}</button>
      </div>
    </div>
  )
}

export interface CommentsSidebarProps {
  comments: ArtifactComment[]
  loading?: boolean
  /** Remote-sync failure surfaced from the GET response. */
  remoteSyncError?: string | null
  /** Doc-level add (no anchor). Anchored adds happen via the inline popover. */
  onAdd: (text: string) => void
  onReply: (parentId: string, text: string) => void
  onResolve: (id: string) => void
  onMarkReview: (id: string) => void
  onDelete: (id: string) => void
  onRefresh: () => void
  /** Optional "ask agent to address comments" — secondary, opens a chat. */
  onAskAgent?: () => void
  onClose: () => void
  /** Hide Resolve/Review/Delete (e.g. a fully read-only view). */
  restrictActions?: boolean
  /** Hide ONLY the Resolve action (shared/remote artifacts: Review + Delete
   *  propagate to the provider, but RESOLVED status has no provider write
   *  path, so we don't offer it). */
  hideResolve?: boolean
  /** Hide the Delete action too (provider-sourced comments we can't delete
   *  via the provider). */
  hideDelete?: boolean
  /** Clicking a comment scrolls its in-iframe anchor highlight into view.
   *  No-op for comments without an anchor. */
  onCommentClick?: (id: string) => void
  /** Reopen a resolved thread (sets status back to open). */
  onReopen?: (id: string) => void
  /** Edit a comment's body in place. Local comments always; provider-origin
   *  comments push the edit remotely when the provider supports it. Omit to disable the Edit affordance. */
  onEditComment?: (id: string, text: string) => void
  /** Persistently-highlighted active comment: the matching row
   *  gets a selected style and is scrolled into view. Unlike flashCommentId
   *  (a transient pulse), this stays applied until the active comment changes. */
  activeCommentId?: string | null
  /** When set, scroll that comment row into view + flash it — driven by an
   *  in-iframe highlight click. Nonce re-triggers on repeat clicks. */
  flashCommentId?: { id: string; nonce: number } | null
  /** Override the root `<aside>` sizing classes. Defaults to the full-page /
   *  fullscreen sizing (`w-[340px] shrink-0 … h-[calc(100vh-240px)] min-h-480`).
   *  The chat side panel passes a stacked, height-capped variant so a narrow
   *  480px panel stays content-primary. Behavior-preserving:
   *  callers that omit it (ArtifactDetailPage, the fullscreen overlay) are
   *  byte-for-byte unchanged. */
  containerClassName?: string
  /** Inline sizing override paired with `containerClassName`. Defaults to the
   *  full-page height. */
  containerStyle?: React.CSSProperties
}

const SIDEBAR_DEFAULT_CLASS = 'w-[340px] shrink-0 flex flex-col rounded-xl border border-border bg-card overflow-hidden'
const SIDEBAR_DEFAULT_STYLE: React.CSSProperties = { height: 'calc(100vh - 240px)', minHeight: 480 }

/** Collapsible right-hand comment sidebar. Threaded one level deep, with
 *  per-comment source/scope/agent/status badges, human-only Resolve, and a
 *  doc-level add box that works for ALL artifact kinds (including HTML /
 *  widget, where text-selection anchoring isn't available inside the
 *  sandboxed iframe — comments degrade to whole-artifact). */
export const CommentsSidebar = memo(function CommentsSidebar(props: CommentsSidebarProps) {
  const {
    comments, loading, remoteSyncError, onAdd, onReply, onResolve,
    onMarkReview, onDelete, onRefresh, onAskAgent, onClose, restrictActions, hideResolve, hideDelete,
    onCommentClick, onReopen, activeCommentId, flashCommentId,
    containerClassName, containerStyle, onEditComment,
  } = props
  // Which comment (if any) is currently being edited in place.
  const [editingId, setEditingId] = useState<string | null>(null)
  // Flash + scroll a comment row when its in-iframe highlight is clicked.
  const rowRefs = useRef<Map<string, HTMLDivElement>>(new Map())
  useEffect(() => {
    if (!flashCommentId) return
    const el = rowRefs.current.get(flashCommentId.id)
    if (!el) return
    el.scrollIntoView({ behavior: 'smooth', block: 'center' })
    const prev = el.style.backgroundColor
    el.style.transition = 'background-color 0.25s ease'
    el.style.backgroundColor = 'var(--accent-subtle)'
    const t = setTimeout(() => { el.style.backgroundColor = prev }, 1100)
    return () => clearTimeout(t)
  }, [flashCommentId])
  // Persistent active comment: scroll its row into view when the
  // active comment changes. The selected *style* is applied via className on
  // the row (stays lit until the active comment changes), not a timed flash.
  useEffect(() => {
    if (!activeCommentId) return
    rowRefs.current.get(activeCommentId)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }, [activeCommentId])
  const [adding, setAdding] = useState(false)
  const [addText, setAddText] = useState('')
  const [replyTo, setReplyTo] = useState<string | null>(null)
  const [showResolved, setShowResolved] = useState(false)
  const addRef = useRef<HTMLTextAreaElement>(null)
  const ime = useImeGuard()
  useAutoGrowTextarea(addRef, addText)
  useEffect(() => { if (adding) addRef.current?.focus() }, [adding])

  // Group into root threads + replies (one level deep). A comment is a reply
  // when parent_id points at another comment in the set; everything else is a
  // root. Orphaned replies (parent not present) are promoted to roots so they
  // never silently vanish.
  const { roots, repliesByParent } = useMemo(() => {
    const byId = new Map(comments.map(c => [c.id, c]))
    const roots: ArtifactComment[] = []
    const replies = new Map<string, ArtifactComment[]>()
    for (const c of comments) {
      if (c.parent_id && byId.has(c.parent_id)) {
        const arr = replies.get(c.parent_id) ?? []
        arr.push(c)
        replies.set(c.parent_id, arr)
      } else {
        roots.push(c)
      }
    }
    // Replies stay chronological within a thread; threads are ordered by their
    // LATEST activity with the newest at the BOTTOM (a chat-style feed — the
    // doc bubbles carry positional order, the sidebar carries time order).
    const byTs = (a: ArtifactComment, b: ArtifactComment) => (a.created_at < b.created_at ? -1 : a.created_at > b.created_at ? 1 : 0)
    for (const arr of replies.values()) arr.sort(byTs)
    const latestActivity = (r: ArtifactComment): string => {
      let t = r.created_at
      for (const rep of replies.get(r.id) ?? []) if (rep.created_at > t) t = rep.created_at
      return t
    }
    roots.sort((a, b) => {
      const ta = latestActivity(a), tb = latestActivity(b)
      return ta < tb ? -1 : ta > tb ? 1 : 0
    })
    return { roots, repliesByParent: replies }
  }, [comments])

  const submitAdd = useCallback(() => {
    const t = addText.trim()
    if (!t) return
    onAdd(t)
    setAddText('')
    setAdding(false)
  }, [addText, onAdd])

  // Resolved threads are hidden by default with a toggle to reveal them.
  // Replying to a resolved comment auto-reopens it (handled by
  // the parent's reply handler).
  const resolvedCount = roots.filter(r => r.status === 'resolved').length
  const visibleRoots = showResolved ? roots : roots.filter(r => r.status !== 'resolved')

  return (
    <aside className={containerClassName ?? SIDEBAR_DEFAULT_CLASS} style={containerStyle ?? SIDEBAR_DEFAULT_STYLE}>
      {/* header */}
      <div className="flex items-center gap-2 px-3 py-2 border-b border-border bg-bg-elevated shrink-0">
        <MessageSquare size={14} className="text-accent" />
        <span className="text-[13px] font-semibold text-text">{i18nT('components.commentsSidebar.comments')}</span>
        <span className="text-[11px] text-muted">{comments.length}</span>
        <div className="ml-auto flex items-center gap-1">
          <button
            type="button"
            onClick={onRefresh}
            className="p-1 rounded text-muted hover:text-text bg-transparent border-none cursor-pointer transition-colors"
            title={i18nT('components.commentsSidebar.refresh_comments')}
            aria-label={i18nT('components.commentsSidebar.refresh_comments')}
          ><RefreshCw size={13} className={loading ? 'animate-spin' : ''} /></button>
          <button
            type="button"
            onClick={onClose}
            className="p-1 rounded text-muted hover:text-text bg-transparent border-none cursor-pointer transition-colors"
            title={i18nT('components.commentsSidebar.collapse_comments')}
            aria-label={i18nT('components.commentsSidebar.collapse_comments')}
          ><ChevronRight size={14} /></button>
        </div>
      </div>

      {/* remote sync error */}
      {remoteSyncError && (
        <div className="px-3 py-2 border-b border-warn/30 bg-warn-subtle text-[11px] text-warn flex items-start gap-1.5 shrink-0">
          <AlertTriangle size={12} className="shrink-0 mt-0.5" />
          <span>{i18nT('components.commentsSidebar.remote_comment_sync_unavailable')} {remoteSyncError}</span>
        </div>
      )}

      {/* scrollable thread list */}
      <div className="flex-1 min-h-0 overflow-y-auto px-3 py-2 space-y-3">
        {roots.length === 0 && !loading && (
          <div className="text-[12px] text-muted py-4 text-center">
            {i18nT('components.commentsSidebar.no_comments_yet_select_text_in_the_content_to_an')}
          </div>
        )}
        {visibleRoots.map(root => (
          <div key={root.id} ref={el => { const m = rowRefs.current; if (el) m.set(root.id, el); else m.delete(root.id) }}>
            <CommentRow
              comment={root}
              isReply={false}
              active={root.id === activeCommentId}
              restrictActions={restrictActions}
              hideResolve={hideResolve}
              hideDelete={hideDelete}
              onReply={() => setReplyTo(replyTo === root.id ? null : root.id)}
              onResolve={c => onResolve(c.id)}
              onMarkReview={c => onMarkReview(c.id)}
              onReopen={onReopen ? c => onReopen(c.id) : undefined}
              onDelete={c => onDelete(c.id)}
              editing={editingId === root.id}
              onEdit={onEditComment ? c => setEditingId(c.id) : undefined}
              onEditSubmit={onEditComment ? text => { onEditComment(root.id, text); setEditingId(null) } : undefined}
              onEditCancel={() => setEditingId(null)}
              onBodyClick={() => { if (root.anchor?.quote) onCommentClick?.(root.id); else setReplyTo(root.id) }}
            />
            {(repliesByParent.get(root.id) ?? []).map(r => (
              <div key={r.id} className="mt-1.5" ref={el => { const m = rowRefs.current; if (el) m.set(r.id, el); else m.delete(r.id) }}>
                <CommentRow
                  comment={r}
                  isReply
                  active={r.id === activeCommentId}
                  restrictActions={restrictActions}
                  hideResolve={hideResolve}
              hideDelete={hideDelete}
                  onReply={() => setReplyTo(replyTo === root.id ? null : root.id)}
                  onResolve={c => onResolve(c.id)}
                  onMarkReview={c => onMarkReview(c.id)}
                  onReopen={onReopen ? c => onReopen(c.id) : undefined}
                  onDelete={c => onDelete(c.id)}
                  editing={editingId === r.id}
                  onEdit={onEditComment ? c => setEditingId(c.id) : undefined}
                  onEditSubmit={onEditComment ? text => { onEditComment(r.id, text); setEditingId(null) } : undefined}
                  onEditCancel={() => setEditingId(null)}
                  onBodyClick={() => { if (root.anchor?.quote) onCommentClick?.(root.id); else setReplyTo(root.id) }}
                />
              </div>
            ))}
            {replyTo === root.id && (
              <ReplyBox
                onSubmit={text => { onReply(root.id, text); setReplyTo(null) }}
                onCancel={() => setReplyTo(null)}
              />
            )}
          </div>
        ))}
        {resolvedCount > 0 && (
          <button
            type="button"
            onClick={() => setShowResolved(v => !v)}
            className="w-full inline-flex items-center justify-center gap-1 text-[11px] text-muted hover:text-text bg-transparent border-none cursor-pointer py-1.5 transition-colors"
          >
            <CheckCircle2 size={12} /> {showResolved ? i18nT('components.commentsSidebar.hide') : i18nT('components.commentsSidebar.show')} {resolvedCount} {i18nT('components.commentsSidebar.resolved')}
          </button>
        )}
      </div>

      {/* footer: doc-level add + optional ask-agent */}
      <div className="border-t border-border p-2 shrink-0 space-y-2">
        {adding ? (
          <div>
            <textarea
              ref={addRef}
              value={addText}
              rows={2}
              placeholder={i18nT('components.commentsSidebar.add_a_comment_on_the_whole_artifact')}
              onChange={e => setAddText(e.target.value)}
              {...ime.composition}
              onKeyDown={e => {
                if (e.key === 'Enter' && !e.shiftKey && !ime.isComposing(e) && addText.trim()) {
                  e.preventDefault(); submitAdd()
                }
                if (e.key === 'Escape') { e.preventDefault(); setAdding(false); setAddText('') }
              }}
              className="w-full bg-bg-elevated border border-border rounded-md px-2 py-1.5 text-text text-[13px] font-body outline-none resize-none focus-ring leading-[18px]"
            />
            <div className="flex items-center justify-end gap-1.5 mt-1">
              <button
                type="button"
                onClick={() => { setAdding(false); setAddText('') }}
                className="px-2 py-0.5 rounded text-[11px] text-muted hover:text-text bg-transparent border-none cursor-pointer"
              >{i18nT('components.commentsSidebar.cancel')}</button>
              <button
                type="button"
                disabled={!addText.trim()}
                onClick={submitAdd}
                className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[11px] font-medium border border-accent text-accent-fg bg-accent cursor-pointer hover:bg-accent-hover disabled:opacity-40 disabled:cursor-default"
              ><Send size={11} /> {i18nT('components.commentsSidebar.comment')}</button>
            </div>
          </div>
        ) : (
          <button
            type="button"
            onClick={() => setAdding(true)}
            className="w-full inline-flex items-center justify-center gap-1 px-2 py-1.5 rounded-md text-[12px] font-medium border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer bg-transparent transition-colors"
          ><Plus size={13} /> {i18nT('components.commentsSidebar.add_comment')}</button>
        )}
        {onAskAgent && (
          <button
            type="button"
            onClick={onAskAgent}
            className="w-full inline-flex items-center justify-center gap-1 px-2 py-1.5 rounded-md text-[12px] font-medium border border-accent/40 text-accent hover:bg-accent-subtle cursor-pointer bg-transparent transition-colors"
            title={i18nT('components.commentsSidebar.open_a_chat_asking_the_agent_to_address_these_co')}
          ><Sparkles size={13} /> {i18nT('components.commentsSidebar.ask_agent_to_address')}</button>
        )}
      </div>
    </aside>
  )
})

export default CommentsSidebar

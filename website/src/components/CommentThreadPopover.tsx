import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { X } from 'lucide-react'
import type { ArtifactComment } from '../types'
import { CommentRow } from './CommentsSidebar'
import { useImeGuard } from '../hooks/useImeGuard'
import { useAutoGrowTextarea } from '../hooks/useAutoGrowTextarea'

import { i18nT } from '../i18n/t'
/**
 * Floating thread popover (document-comment style): clicking a gutter bubble / highlight in
 * the doc opens this card right at the anchor, showing the whole conversation
 * (root + replies) with an inline reply box + actions — far more room than the
 * narrow sidebar. The sidebar stays as the chronological feed; this is the
 * positional, in-context view.
 *
 * Positioning: `position: fixed` at the anchor. For markdown the anchor is found
 * live in the DOM via `[data-mc-cid]`; for HTML/widgets the iframe posts a
 * viewport rect (the parent can't read inside the sandbox). Re-anchors on
 * scroll/resize so it tracks the text.
 */

const CARD_W = 360
const GAP = 8

interface Props {
  comments: ArtifactComment[]
  rootId: string
  /** Viewport rect of the anchor (HTML/iframe path). Markdown omits it and the
   *  popover finds the anchor element via data-mc-cid. */
  rect?: { x: number; y: number; w: number; h: number }
  hideResolve?: boolean
  onClose: () => void
  onReply: (parentId: string, text: string) => void
  onResolve: (id: string) => void
  onMarkReview: (id: string) => void
  onReopen: (id: string) => void
  onDelete: (id: string) => void
  onEditComment?: (id: string, text: string) => void
}

export function CommentThreadPopover({
  comments, rootId, rect, hideResolve, onClose, onReply, onResolve, onMarkReview, onReopen, onDelete, onEditComment,
}: Props) {
  const cardRef = useRef<HTMLDivElement>(null)
  const composerRef = useRef<HTMLTextAreaElement>(null)
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null)
  const [reply, setReply] = useState('')
  const [editingId, setEditingId] = useState<string | null>(null)
  const ime = useImeGuard()
  useAutoGrowTextarea(composerRef, reply, 160)

  const root = comments.find(c => c.id === rootId)
  const replies = comments
    .filter(c => c.parent_id === rootId)
    .sort((a, b) => (a.created_at < b.created_at ? -1 : a.created_at > b.created_at ? 1 : 0))

  const reposition = useCallback(() => {
    const vw = window.innerWidth
    const vh = window.innerHeight
    const cardH = cardRef.current?.offsetHeight ?? 320
    let aTop: number, aBottom: number, aLeft: number
    if (rect) {
      aTop = rect.y; aBottom = rect.y + rect.h; aLeft = rect.x
    } else {
      const el = document.querySelector(`[data-mc-cid="${CSS.escape(rootId)}"]`) as HTMLElement | null
      if (!el) { setPos({ top: Math.max(GAP, (vh - cardH) / 2), left: Math.max(GAP, vw - CARD_W - 24) }); return }
      const r = el.getBoundingClientRect()
      aTop = r.top; aBottom = r.bottom; aLeft = r.left
    }
    let top = aBottom + GAP
    if (top + cardH > vh - GAP) {
      const above = aTop - cardH - GAP
      top = above >= GAP ? above : Math.max(GAP, vh - cardH - GAP)
    }
    const left = Math.min(Math.max(GAP, aLeft), vw - CARD_W - GAP)
    setPos({ top, left })
  }, [rect, rootId])

  useLayoutEffect(() => {
    // Bring the markdown anchor into view before positioning.
    if (!rect) {
      const el = document.querySelector(`[data-mc-cid="${CSS.escape(rootId)}"]`) as HTMLElement | null
      el?.scrollIntoView({ block: 'center', behavior: 'smooth' })
    }
    reposition()
    const onMove = () => reposition()
    window.addEventListener('scroll', onMove, true)
    window.addEventListener('resize', onMove)
    return () => {
      window.removeEventListener('scroll', onMove, true)
      window.removeEventListener('resize', onMove)
    }
  }, [rootId, rect, reposition])

  // Close on Esc / outside click (but not when clicking another bubble/anchor or
  // the iframe — those switch threads and re-open cleanly).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    const onDown = (e: MouseEvent) => {
      const t = e.target as Element | null
      if (cardRef.current?.contains(t as Node)) return
      if (t?.closest?.('.mc-cmt-bubble, .mc-cmt-rect, iframe')) return
      onClose()
    }
    document.addEventListener('keydown', onKey)
    document.addEventListener('mousedown', onDown, true)
    return () => {
      document.removeEventListener('keydown', onKey)
      document.removeEventListener('mousedown', onDown, true)
    }
  }, [onClose])

  useEffect(() => { composerRef.current?.focus() }, [rootId])

  if (!root) return null
  const count = 1 + replies.length
  const submit = () => { const t = reply.trim(); if (!t) return; onReply(rootId, t); setReply('') }

  return (
    <div
      ref={cardRef}
      className="fixed z-[1000] w-[360px] max-h-[70vh] flex flex-col rounded-xl border-2 border-border-strong bg-bg-elevated shadow-2xl ring-1 ring-accent/30 overflow-hidden"
      style={{ top: pos?.top ?? -9999, left: pos?.left ?? -9999 }}
      role="dialog"
      aria-label={i18nT('components.commentThreadPopover.comment_thread')}
    >
      <div className="flex items-center gap-2 px-3 py-2 border-b border-border bg-bg-elevated shrink-0">
        <span className="text-[13px] font-semibold text-text">{count} {i18nT('components.commentThreadPopover.comment')}{count === 1 ? '' : 's'}</span>
        <button
          type="button"
          onClick={onClose}
          className="ml-auto p-1 rounded text-muted hover:text-text bg-transparent border-none cursor-pointer transition-colors"
          aria-label={i18nT('components.commentThreadPopover.close')}
        ><X size={14} /></button>
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto px-3 py-2 space-y-2">
        <CommentRow
          comment={root}
          isReply={false}
          active
          hideResolve={hideResolve}
          onReply={() => composerRef.current?.focus()}
          onResolve={c => { onResolve(c.id); onClose() }}
          onMarkReview={c => onMarkReview(c.id)}
          onReopen={c => onReopen(c.id)}
          onDelete={c => { onDelete(c.id); onClose() }}
          editing={editingId === root.id}
          onEdit={onEditComment ? c => setEditingId(c.id) : undefined}
          onEditSubmit={onEditComment ? text => { onEditComment(root.id, text); setEditingId(null) } : undefined}
          onEditCancel={() => setEditingId(null)}
        />
        {replies.map(r => (
          <CommentRow
            key={r.id}
            comment={r}
            isReply
            hideResolve={hideResolve}
            onReply={() => composerRef.current?.focus()}
            onResolve={() => {}}
            onMarkReview={() => {}}
            onDelete={c => onDelete(c.id)}
            editing={editingId === r.id}
            onEdit={onEditComment ? c => setEditingId(c.id) : undefined}
            onEditSubmit={onEditComment ? text => { onEditComment(r.id, text); setEditingId(null) } : undefined}
            onEditCancel={() => setEditingId(null)}
          />
        ))}
      </div>
      <div className="border-t border-border p-2 shrink-0">
        <textarea
          ref={composerRef}
          value={reply}
          rows={2}
          placeholder={i18nT('components.commentThreadPopover.reply')}
          onChange={e => setReply(e.target.value)}
          {...ime.composition}
          onKeyDown={e => {
            if (e.key === 'Enter' && !e.shiftKey && !ime.isComposing(e) && reply.trim()) { e.preventDefault(); submit() }
          }}
          className="w-full bg-bg-elevated border border-border rounded-md px-2 py-1.5 text-text text-[13px] font-body outline-none resize-none focus-ring leading-[18px]"
        />
        <div className="flex justify-end mt-1">
          <button
            type="button"
            disabled={!reply.trim()}
            onClick={submit}
            className="px-2.5 py-1 rounded text-[12px] font-medium border border-accent text-accent-fg bg-accent cursor-pointer hover:bg-accent-hover disabled:opacity-40 disabled:cursor-default"
          >{i18nT('components.commentThreadPopover.reply_2')}</button>
        </div>
      </div>
    </div>
  )
}

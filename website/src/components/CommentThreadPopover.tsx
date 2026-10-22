import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { X } from 'lucide-react'
import type { ArtifactComment } from '../types'
import { CommentDraftReportProvider, CommentRow } from './CommentsSidebar'
import { useImeGuard } from '../hooks/useImeGuard'
import { useAutoGrowTextarea } from '../hooks/useAutoGrowTextarea'
import { codeAnchorElement, onCodeAnchorsChange } from './codeComments'

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
/** Effective card width: the fixed 360px card shrinks on viewports too narrow
 * to hold it (e.g. 320px phones), so the close/reply controls stay reachable
 * for BOTH prose and code threads — the popover is shared by both surfaces. */
const cardW = (vw: number) => Math.min(CARD_W, vw - GAP * 2)

/** A prose thread's light-DOM highlight, else a code thread's chip (or the
 *  code block that owns it): both are read live, so the card follows them. */
const anchorElement = (rootId: string): HTMLElement | null =>
  (document.querySelector(`[data-mc-cid="${CSS.escape(rootId)}"]`) as HTMLElement | null) ?? codeAnchorElement(rootId)

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
  /** Whether the reply box or an in-place edit holds unsent text, so a host
   *  that is about to navigate away can ask before the popover unmounts. */
  onDraftDirtyChange?: (dirty: boolean) => void
}

export function CommentThreadPopover({
  comments, rootId, rect, hideResolve, onClose, onReply, onResolve, onMarkReview, onReopen, onDelete, onEditComment,
  onDraftDirtyChange,
}: Props) {
  const cardRef = useRef<HTMLDivElement>(null)
  const composerRef = useRef<HTMLTextAreaElement>(null)
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null)
  const [reply, setReply] = useState('')
  const [editingId, setEditingId] = useState<string | null>(null)
  const ime = useImeGuard()
  useAutoGrowTextarea(composerRef, reply, 160)

  // The reply box here plus any in-place edit box a row reports.
  const dirtyBoxesRef = useRef(new Set<string>())
  const [editDirty, setEditDirty] = useState(false)
  const reportDraft = useCallback((id: string, dirty: boolean) => {
    const boxes = dirtyBoxesRef.current
    if (dirty) boxes.add(id)
    else boxes.delete(id)
    setEditDirty(boxes.size > 0)
  }, [])
  const draftDirty = reply.trim() !== '' || editDirty
  useEffect(() => { onDraftDirtyChange?.(draftDirty) }, [draftDirty, onDraftDirtyChange])
  useEffect(() => () => onDraftDirtyChange?.(false), [onDraftDirtyChange])

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
      const el = anchorElement(rootId)
      if (!el) { setPos({ top: Math.max(GAP, (vh - cardH) / 2), left: Math.max(GAP, vw - cardW(vw) - 24) }); return }
      const r = el.getBoundingClientRect()
      aTop = r.top; aBottom = r.bottom; aLeft = r.left
    }
    let top = aBottom + GAP
    if (top + cardH > vh - GAP) {
      const above = aTop - cardH - GAP
      top = above >= GAP ? above : Math.max(GAP, vh - cardH - GAP)
    }
    const left = Math.min(Math.max(GAP, aLeft), Math.max(GAP, vw - cardW(vw) - GAP))
    setPos({ top, left })
  }, [rect, rootId])

  useLayoutEffect(() => {
    // Bring the markdown anchor into view before positioning.
    if (!rect) {
      const el = anchorElement(rootId)
      el?.scrollIntoView({ block: 'center', behavior: 'smooth' })
    }
    reposition()
    const onMove = () => reposition()
    window.addEventListener('scroll', onMove, true)
    window.addEventListener('resize', onMove)
    const offAnchors = rect ? undefined : onCodeAnchorsChange(onMove)
    return () => {
      window.removeEventListener('scroll', onMove, true)
      window.removeEventListener('resize', onMove)
      offAnchors?.()
    }
  }, [rootId, rect, reposition])

  // Close on Esc / outside click (but not when clicking another bubble/anchor or
  // the iframe — those switch threads and re-open cleanly — nor inside a menu or
  // dialog layered over the page, such as the prompt that asks whether to
  // discard this popover's unsent reply: answering it must not drop the reply).
  // While a modal is open over the page the popover stays put on every input:
  // the modal's Escape and its backdrop click answer the modal, not the
  // popover. An Escape that closes a menu belongs to the menu. A press on the
  // toolbar's More trigger keeps the popover too, so the draft guard behind
  // Send to a session still sees the reply and can ask about it.
  useEffect(() => {
    const modalOpen = () => Array.from(
      document.querySelectorAll('[role="dialog"][aria-modal="true"], [role="alertdialog"]'),
    ).some(el => !cardRef.current?.contains(el))
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      const t = e.target as Element | null
      if (t?.closest?.('[role="menu"]') || modalOpen()) return
      onClose()
    }
    const onDown = (e: MouseEvent) => {
      if (modalOpen()) return
      const t = e.target as Element | null
      if (cardRef.current?.contains(t as Node)) return
      if (t?.closest?.('.mc-cmt-bubble, .mc-cmt-rect, iframe, [role="menu"], [role="dialog"], [role="alertdialog"], [data-ui-location="artifacts.detail.more"]')) return
      // A code-block thread chip is the code twin of `.mc-cmt-bubble`, but it
      // lives in Pierre's shadow DOM, where `e.target` is retargeted to the
      // host and `closest` cannot see it: read the composed path instead, so a
      // chip click switches threads like a bubble click does, not a close.
      if (e.composedPath?.().some(n => n instanceof Element && n.hasAttribute('data-mc-code-chip'))) return
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
    <CommentDraftReportProvider value={reportDraft}>
      <div
        ref={cardRef}
        className="fixed z-[1000] w-[360px] max-w-[calc(100vw-16px)] max-h-[70vh] flex flex-col rounded-xl border-2 border-border-strong bg-bg-elevated shadow-2xl ring-1 ring-accent/30 overflow-hidden"
        style={{ top: pos?.top ?? -9999, left: pos?.left ?? -9999 }}
        role="dialog"
        aria-label={i18nT('components.commentThreadPopover.comment_thread')}
      >
        <div className="flex items-center gap-2 px-3 py-2 border-b border-border bg-bg-elevated shrink-0">
          <span className="text-[13px] font-semibold text-text">{i18nT('components.commentThreadPopover.comment', { count: count })}</span>
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
            {...ime.bindComposition()}
            onKeyDown={e => {
              // The emptiness test stays OUTSIDE the claim: on a blank box this Enter is
              // not a submit at all, and taking it would cost the newline it means there.
              if (e.key === 'Enter' && !e.shiftKey && reply.trim()) { if (ime.claimEnter(e)) submit() }
            }}
            className="w-full bg-bg-elevated border border-border rounded-md px-2 py-1.5 text-text text-[13px] font-body outline-hidden resize-none focus-ring leading-[18px]"
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
    </CommentDraftReportProvider>
  )
}

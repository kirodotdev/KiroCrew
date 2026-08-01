import { useLayoutEffect, useMemo, useState } from 'react'
import type { ArtifactComment } from '../types'
import { indexTextNodes, rangeForAnchor } from '../hooks/useMarkdownCommentHighlights'

import { i18nT } from '../i18n/t'
/**
 * Visible anchored-comment highlights for the markdown / text body, drawn as a
 * layer of absolutely-positioned <div>s OVER the rendered content (computed from
 * each anchor's `Range.getClientRects()`). Real DOM elements always paint, and
 * support box-shadow + a persistent active state — neither of which the CSS
 * Custom Highlight API could do in the dashboard.
 *
 * Per feedback:
 *  - one document-comment-style gutter bubble PER anchored thread (not per line); its count
 *    is the thread size (1 root + its replies);
 *  - same-line rect fragments (e.g. split at a comma / inline-element boundary)
 *    are merged into a single highlight box;
 *  - the bubble is FILLED when the thread has unread content, OUTLINE when read.
 *
 * Positioning: an absolutely-positioned child of the SCROLL container; rects are
 * in the scroller's content space, so the layer scrolls with the content (only a
 * ResizeObserver is needed to recompute on reflow).
 */

interface Rect { x: number; y: number; w: number; h: number }
interface Box { id: string; rects: Rect[] }
interface Bubble { id: string; x: number; y: number; count: number; unread: boolean }

/** Merge rect fragments that sit on the same visual line into one box (fixes
 *  anchors that `getClientRects()` splits at a comma / inline boundary). */
function mergeByLine(rects: Rect[]): Rect[] {
  const lines = new Map<number, Rect>()
  for (const r of rects) {
    if (r.w <= 0 || r.h <= 0) continue
    const key = Math.round(r.y / 4)
    const cur = lines.get(key)
    if (!cur) { lines.set(key, { ...r }); continue }
    const left = Math.min(cur.x, r.x)
    const right = Math.max(cur.x + cur.w, r.x + r.w)
    cur.x = left
    cur.w = right - left
    cur.y = Math.min(cur.y, r.y)
    cur.h = Math.max(cur.h, r.h)
  }
  return [...lines.values()]
}

export function InlineCommentOverlay({
  scrollRef, textRef, comments, activeId, scrollNonce, unreadRootIds, onActivate,
}: {
  /** The overflow-auto scroll container (must be position:relative). */
  scrollRef: React.RefObject<HTMLElement | null>
  /** The rendered markdown/text root used for range matching. */
  textRef: React.RefObject<HTMLElement | null>
  comments: ArtifactComment[]
  /** Persistently-highlighted comment (drives the active rect style). */
  activeId: string | null
  /** Bumping this scrolls the body to `activeId` (sidebar → body link). */
  scrollNonce?: number
  /** Root-comment ids whose thread has unread content (filled bubble). */
  unreadRootIds?: Set<string>
  onActivate: (commentId: string) => void
}) {
  const [boxes, setBoxes] = useState<Box[]>([])
  const [bubbles, setBubbles] = useState<Bubble[]>([])
  const [contentH, setContentH] = useState<number | string>('100%')

  // Anchored roots (replies have no anchor); resolved threads show no anchor
  // (feedback — their highlight + bubble are hidden, like the sidebar).
  const anchored = useMemo(
    () => comments.filter(c => c.anchor?.quote && c.status !== 'resolved'),
    [comments],
  )
  const replyCounts = useMemo(() => {
    const m = new Map<string, number>()
    for (const c of comments) if (c.parent_id) m.set(c.parent_id, (m.get(c.parent_id) ?? 0) + 1)
    return m
  }, [comments])
  const sig = useMemo(
    () => anchored.map(c => `${c.id}\u0000${c.anchor!.quote}`).join('\u0001'),
    [anchored],
  )

  useLayoutEffect(() => {
    const scroller = scrollRef.current
    const textRoot = textRef.current
    if (!scroller || !textRoot) { setBoxes([]); setBubbles([]); return }
    let raf = 0
    const compute = () => {
      const sRect = scroller.getBoundingClientRect()
      const idx = indexTextNodes(textRoot)
      const nb: Box[] = []
      const nbubbles: Bubble[] = []
      for (const c of anchored) {
        const r = rangeForAnchor(idx, {
          id: c.id, quote: c.anchor!.quote ?? '', prefix: c.anchor!.prefix ?? '', suffix: c.anchor!.suffix ?? '',
          startOffset: c.anchor!.start_offset,
        })
        if (!r) continue
        const raw = Array.from(r.getClientRects()).map(rc => ({
          x: rc.left - sRect.left + scroller.scrollLeft,
          y: rc.top - sRect.top + scroller.scrollTop,
          w: rc.width, h: rc.height,
        }))
        const rects = mergeByLine(raw)
        if (!rects.length) continue
        nb.push({ id: c.id, rects })
        // One bubble per thread, at the topmost line of its anchor.
        const top = rects.reduce((a, b) => (b.y < a.y ? b : a), rects[0])
        nbubbles.push({
          id: c.id,
          x: Math.max(0, top.x - 26),
          y: top.y,
          count: 1 + (replyCounts.get(c.id) ?? 0),
          unread: unreadRootIds?.has(c.id) ?? false,
        })
      }
      setBoxes(nb)
      setBubbles(nbubbles)
      setContentH(scroller.scrollHeight)
    }
    raf = requestAnimationFrame(compute)
    const ro = new ResizeObserver(() => { cancelAnimationFrame(raf); raf = requestAnimationFrame(compute) })
    ro.observe(textRoot)
    ro.observe(scroller)
    return () => { cancelAnimationFrame(raf); ro.disconnect() }
  }, [scrollRef, textRef, sig, anchored, replyCounts, unreadRootIds])

  // Sidebar → body: scroll the active anchor into view when asked.
  useLayoutEffect(() => {
    if (!scrollNonce || !activeId) return
    const scroller = scrollRef.current
    const box = boxes.find(b => b.id === activeId)
    if (!scroller || !box) return
    scroller.scrollTo({ top: Math.max(0, box.rects[0].y - 96), behavior: 'smooth' })
  }, [scrollNonce, activeId, boxes, scrollRef])

  const onRectClick = (id: string) => {
    const sel = window.getSelection()
    if (sel && !sel.isCollapsed) return // dragging a selection (creating a comment)
    onActivate(id)
  }

  return (
    <div
      className="mc-cmt-overlay"
      style={{ position: 'absolute', top: 0, left: 0, width: '100%', height: contentH, pointerEvents: 'none', zIndex: 1 }}
    >
      {boxes.map(b =>
        b.rects.map((r, i) => (
          <div
            key={`${b.id}-${i}`}
            data-mc-cid={b.id}
            role="button"
            tabIndex={i === 0 ? 0 : -1}
            aria-label={i18nT('components.inlineCommentOverlay.open_comment_thread')}
            onClick={() => onRectClick(b.id)}
            onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onRectClick(b.id) } }}
            className={`mc-cmt-rect${b.id === activeId ? ' active' : ''}`}
            style={{ position: 'absolute', left: r.x, top: r.y, width: r.w, height: r.h, pointerEvents: 'auto' }}
          />
        )),
      )}
      {bubbles.map(g => (
        <button
          key={g.id}
          type="button"
          onClick={() => onActivate(g.id)}
          className={`mc-cmt-bubble${g.id === activeId ? ' active' : ''}${g.unread ? ' unread' : ''}`}
          style={{ position: 'absolute', left: g.x, top: g.y - 1, pointerEvents: 'auto' }}
          title={g.unread
            ? i18nT('components.inlineCommentOverlay.comment_unread', { count: g.count })
            : i18nT('components.inlineCommentOverlay.comment', { count: g.count })}
        >
          {g.count}
        </button>
      ))}
    </div>
  )
}

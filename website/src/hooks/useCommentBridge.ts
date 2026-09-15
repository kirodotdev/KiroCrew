import { useCallback, useEffect, useMemo, useRef } from 'react'
import type { ArtifactComment } from '../types'

/** A text selection captured inside the sandboxed HTML iframe, with the
 *  popover anchor already converted to parent-viewport coordinates. */
export interface IframeSelection {
  quote: string
  prefix: string
  suffix: string
  /** Char offsets of the selection within the iframe body innerText. Required
   *  by the provider's create_comment anchor schema (post is rejected without). */
  startOffset: number
  endOffset: number
  /** Viewport-relative coords for the comment popover (fixed positioning). */
  x: number
  y: number
}

/**
 * Parent-side half of the in-iframe comment bridge.
 *
 * Pairs with the `COMMENT_BRIDGE_BODY` script injected into the sandboxed
 * widget/HTML iframe by `buildSrcdoc({ enableComments: true })`. It:
 *  - pushes the current anchored comments to the iframe as highlights (on the
 *    iframe's `mc-comment-ready` signal and whenever the comment set changes);
 *  - surfaces a text selection inside the iframe via `onSelect` (so the caller
 *    can open the comment popover) and a highlight click via `onHighlightClick`;
 *  - returns `scrollToAnchor(commentId)` so the caller can scroll + flash the
 *    matching highlight inside the iframe when a comment row is clicked.
 *
 * Cross-origin-safe: the iframe has a null origin (srcdoc + sandbox), so all
 * communication is `postMessage`. Messages are filtered to this iframe's
 * contentWindow.
 */
export function useCommentBridge(opts: {
  iframeRef: React.RefObject<HTMLIFrameElement | null>
  comments: ArtifactComment[]
  onSelect?: (sel: IframeSelection) => void
  onHighlightClick?: (commentId: string) => void
  /** Open the thread popover for an in-iframe anchor; rect is in PARENT
   *  viewport coords (iframe offset already added). */
  onOpenThread?: (commentId: string, rect?: { x: number; y: number; w: number; h: number }) => void
  /** Persistently-active comment id (drives the in-iframe active highlight). */
  activeId?: string | null
  /** Root-comment ids whose thread has unread content (filled bubble). */
  unreadRootIds?: Set<string>
  enabled?: boolean
}) {
  const { iframeRef, comments, onSelect, onHighlightClick, onOpenThread, activeId, unreadRootIds, enabled = true } = opts
  const readyRef = useRef(false)

  const replyCounts = useMemo(() => {
    const m = new Map<string, number>()
    for (const c of comments) if (c.parent_id) m.set(c.parent_id, (m.get(c.parent_id) ?? 0) + 1)
    return m
  }, [comments])

  const anchorPayload = useMemo(
    () =>
      comments
        // Resolved threads show no anchor highlight/bubble.
        .filter(c => c.anchor && c.anchor.quote && c.status !== 'resolved')
        .map(c => ({
          id: c.id,
          quote: c.anchor!.quote,
          prefix: c.anchor!.prefix ?? '',
          suffix: c.anchor!.suffix ?? '',
          count: 1 + (replyCounts.get(c.id) ?? 0),
          unread: unreadRootIds?.has(c.id) ?? false,
        })),
    [comments, replyCounts, unreadRootIds],
  )

  // Target origin is '*' by necessity: the artifact iframe is a blob:-URL
  // document with an opaque (null) origin, so a concrete targetOrigin would
  // silently drop the message. The payloads are non-sensitive UI hints (comment
  // anchor geometry + ids), and the inbound listener validates
  // `e.source === iframe.contentWindow` before trusting anything.
  const postHighlights = useCallback(() => {
    const w = iframeRef.current?.contentWindow
    if (!w) return
    // nosemgrep: javascript.browser.security.wildcard-postmessage-configuration.wildcard-postmessage-configuration
    w.postMessage({ type: 'mc-comment-highlights', anchors: anchorPayload }, '*')
  }, [iframeRef, anchorPayload])

  const postActive = useCallback(() => {
    // nosemgrep: javascript.browser.security.wildcard-postmessage-configuration.wildcard-postmessage-configuration
    iframeRef.current?.contentWindow?.postMessage({ type: 'mc-comment-active', id: activeId ?? '' }, '*')
  }, [iframeRef, activeId])

  // Keep the latest callbacks/payload in refs so the message listener can stay
  // mounted for the iframe's lifetime without re-subscribing on every change.
  const onSelectRef = useRef(onSelect)
  const onHighlightClickRef = useRef(onHighlightClick)
  const onOpenThreadRef = useRef(onOpenThread)
  const postHighlightsRef = useRef(postHighlights)
  const postActiveRef = useRef(postActive)
  useEffect(() => { onSelectRef.current = onSelect }, [onSelect])
  useEffect(() => { onHighlightClickRef.current = onHighlightClick }, [onHighlightClick])
  useEffect(() => { onOpenThreadRef.current = onOpenThread }, [onOpenThread])
  useEffect(() => { postHighlightsRef.current = postHighlights }, [postHighlights])
  useEffect(() => { postActiveRef.current = postActive }, [postActive])

  useEffect(() => {
    if (!enabled) return
    const handler = (e: MessageEvent) => {
      const w = iframeRef.current?.contentWindow
      if (!w || e.source !== w) return
      const d = (e.data || {}) as Record<string, unknown>
      if (d.type === 'mc-comment-ready') {
        readyRef.current = true
        postHighlightsRef.current()
        postActiveRef.current()
      } else if (d.type === 'mc-comment-select' && onSelectRef.current) {
        const frame = iframeRef.current?.getBoundingClientRect()
        const rect = (d.rect as { x?: number; y?: number }) || {}
        onSelectRef.current({
          quote: String(d.quote || ''),
          prefix: String(d.prefix || ''),
          suffix: String(d.suffix || ''),
          startOffset: Number(d.startOffset ?? 0),
          endOffset: Number(d.endOffset ?? 0),
          x: (frame?.left ?? 0) + (rect.x ?? 0),
          y: (frame?.top ?? 0) + (rect.y ?? 0),
        })
      } else if (d.type === 'mc-comment-highlight-click') {
        const id = String(d.id || '')
        const frame = iframeRef.current?.getBoundingClientRect()
        const rr = (d.rect as { x?: number; y?: number; w?: number; h?: number }) || {}
        const rect = frame
          ? { x: frame.left + (rr.x ?? 0), y: frame.top + (rr.y ?? 0), w: rr.w ?? 0, h: rr.h ?? 0 }
          : undefined
        if (onOpenThreadRef.current) onOpenThreadRef.current(id, rect)
        else if (onHighlightClickRef.current) onHighlightClickRef.current(id)
      }
    }
    window.addEventListener('message', handler)
    return () => window.removeEventListener('message', handler)
  }, [iframeRef, enabled])

  // Re-push highlights whenever the anchored set changes (once the iframe is
  // ready). The ready handler covers the initial push.
  useEffect(() => {
    if (enabled && readyRef.current) postHighlights()
  }, [enabled, postHighlights])

  // Push the active comment to the iframe so its <mark> lights up like markdown.
  useEffect(() => {
    if (enabled && readyRef.current) postActive()
  }, [enabled, postActive])

  const scrollToAnchor = useCallback(
    (commentId: string) => {
      // nosemgrep: javascript.browser.security.wildcard-postmessage-configuration.wildcard-postmessage-configuration
      iframeRef.current?.contentWindow?.postMessage(
        { type: 'mc-comment-scroll-to', id: commentId },
        '*',
      )
    },
    [iframeRef],
  )

  /** Wire to the <iframe> onLoad. The 'mc-comment-ready' message can be posted
   * before this parent's message listener attaches (a tiny blob document in a
   * fresh window parses faster than React commits effects) — the ready signal
   * is then lost and no highlights ever push. The load event is owned by the
   * parent, so it can't be missed: mark ready and push the current set. */
  const onIframeLoad = useCallback(() => {
    if (!enabled) return
    readyRef.current = true
    postHighlightsRef.current()
    postActiveRef.current()
  }, [enabled])

  return { scrollToAnchor, postHighlights, onIframeLoad }
}

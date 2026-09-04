/**
 * TranscriptScrollShell — the transcript scroller SKELETON for a virtualized
 * chat surface: the scroll container's full style contract, the virtualizer's
 * sentinel/spacer wiring, and the older-messages loading affordance.
 *
 * Extracted from the main chat page as one unit because these pieces only
 * work as a set: the sentinels drive the window, the spacers reserve the
 * unmounted height the sentinels are measured against, and the scroller's
 * style contract is what keeps all of it scrollable, anchored, and contained.
 * Page-owned content threads through as slots — `aboveRows` (paging bar),
 * `children` (the mounted rows), `belowRows` (footer / survey / tail spacer) —
 * so page state never leaks in here and the shell stays reusable by any host
 * that runs useVirtualChat.
 *
 * The characterization net (ChatPage.scrollShell.recipe.test.tsx, the golden
 * frames, and the mutation harness) pins this file's tokens byte-for-byte;
 * fadeClearance geometry stays with the page, which supplies its clearance
 * padding via `scrollerStyle`.
 */
import React from 'react'
import { Loader } from 'lucide-react'
import { i18nT } from '../../i18n/t'

export interface TranscriptVirtWiring {
  topSentinelRef: React.MutableRefObject<HTMLDivElement | null>
  bottomSentinelRef: React.MutableRefObject<HTMLDivElement | null>
  offsetBefore: number
  offsetAfter: number
}

export default function TranscriptScrollShell({
  scrollerRef,
  onScroll,
  virt,
  loadingOlder,
  scrollerStyle,
  aboveRows,
  belowRows,
  children,
}: {
  /** The single scroll controller's element ref (owned by the host's virtualizer). */
  scrollerRef: React.MutableRefObject<HTMLDivElement | null>
  onScroll: () => void
  virt: TranscriptVirtWiring
  loadingOlder: boolean
  /** Host-owned geometry merged onto the scroller (e.g. the fade-band clearance padding). */
  scrollerStyle?: React.CSSProperties
  /** Page content above the rows (the earlier-messages paging bar). */
  aboveRows?: React.ReactNode
  /** Page content below the rows (footer, survey, tail spacer). */
  belowRows?: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <div
      ref={scrollerRef}
      // -1 so the bar can hand focus here on unmount without adding a tab stop.
      tabIndex={-1}
      // stable theming hook 'chat-container' — see website/docs/theming-contract.md
      className="chat-container"
      style={{
        // Host geometry merges FIRST so the shell's own tokens below always
        // win: the scroll contract (overflow axes, anchoring, containment,
        // gutter, and the flex-fill that makes the scroller consume its column
        // — every current host mounts it in a flex column) is this component's
        // whole reason to exist, and a consumer
        // overriding e.g. overflowX would silently reinstate the horizontal
        // scrollbar bug documented below. Hosts may only ADD properties the
        // shell does not claim (like the fade-band clearance padding).
        ...scrollerStyle,
        flex: 1,
        overflowY: 'auto',
        // overflow-x must be pinned, not left to default `visible`: with
        // overflowY `auto`, CSS forces the `visible` axis to compute to
        // `auto`, so one over-wide child (a long path, a wide code block,
        // a widget) gives the whole list a draggable horizontal scrollbar
        // above the composer. The conversation never pans sideways —
        // wide children scroll within themselves.
        overflowX: 'hidden',
        // Reserve a stable scrollbar gutter so the 6px scrollbar always
        // occupies the same right-edge column the title overlay is inset
        // from — keeps the thumb visible and grabbable at the top instead
        // of hidden behind the header.
        scrollbarGutter: 'stable',
        // Native scroll anchoring: when items above the viewport
        // resize (e.g. widget iframes loading async), the browser
        // adjusts scrollTop to keep the user's content stable.
        // This is more precise than item-level anchoring because
        // it works at the DOM-element granularity.
        overflowAnchor: 'auto',
        // Keep wheel/touch momentum inside the message list. Without
        // this, a delta that arrives at the top or bottom edge chains
        // to the nearest scrollable ancestor — the document, which
        // `body{overflow-y:auto}` leaves scrollable — and drags the
        // whole app shell by however many pixels of slack exist
        // (a browser-extension node parked past the shell is enough).
        overscrollBehavior: 'contain',
      } as React.CSSProperties}
      aria-label={i18nT('pages.chatPage.chat_messages')}
      aria-live="polite"
      onScroll={onScroll}
    >
      {/* Header spacer */}
      <div className="h-16" />
      {aboveRows}
      {/* Top sentinel: drives upward window expansion via virtualizer's IO. */}
      <div ref={virt.topSentinelRef} aria-hidden style={{ height: 1 }} />
      {/* top-16 matches the h-16 header spacer above, so the pinned spinner
          clears the overlay header instead of sitting under it.
          overflow-anchor:none so appearing/vanishing here cannot become the
          browser's scroll anchor and jump the list mid-fetch. */}
      {loadingOlder && (
        <div className="sticky top-16 z-[1] flex justify-center py-2" data-testid="older-messages-loading" role="status" aria-label={i18nT('pages.chatPage.loading_earlier_messages')} style={{ overflowAnchor: 'none', background: 'var(--bg)' }}>
          <Loader size={16} className="animate-spin text-muted" />
        </div>
      )}
      {/* Top spacer — reserves the height of all items above the mounted
          window so the scrollbar stays accurate while only the window
          renders real DOM (keeps fast scroll cheap — O(window) nodes).
          overflow-anchor:none so the browser anchors on real content,
          not on this spacer (which resizes as the window moves). */}
      <div aria-hidden style={{ height: virt.offsetBefore, overflowAnchor: 'none' }} />
      {children}
      {/* Bottom spacer — reserves the height of all items below the
          mounted window. overflow-anchor:none (see top spacer). */}
      <div aria-hidden style={{ height: virt.offsetAfter, overflowAnchor: 'none' }} />
      {/* Bottom sentinel: drives downward window expansion when in jump mode. */}
      <div ref={virt.bottomSentinelRef} aria-hidden style={{ height: 1 }} />
      {belowRows}
    </div>
  )
}

/**
 * VirtualTranscript — the virtualized transcript unit every chat surface can
 * mount (chat-core P5-e): `useVirtualChat` over grouped `DisplayItem`s, the
 * shared scroller skeleton (`TranscriptScrollShell`), ChatPage's row-identity
 * rules (`rowKeys`), and the earlier-history paging bar, behind one component
 * with an imperative handle.
 *
 * What the host still owns: the rows themselves (`renderRow` — ChatMessageList
 * supplies the shared renderer), the data (what `items` holds, how much history
 * is loaded, how "load earlier" widens it), and the chrome around the scroller
 * (edge fades, the jump-to-bottom pill, footers via `belowRows`). What the host
 * no longer owns: the scroll container, stick-to-bottom follow, and the DOM
 * cost of a long transcript — only the viewport window (plus overscan) is
 * mounted, so a 3000-row thread costs the same DOM as a 30-row one.
 *
 * The main chat page keeps its own inline wiring for now (P5-f switches it);
 * this component is the same recipe with the page's private state removed.
 */
import React, {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import { useVirtualChat } from '../../hooks/virtualizer/useVirtualChat'
import EarlierMessagesBar from '../../pages/chat/EarlierMessagesBar'
import TranscriptScrollShell from '../../pages/chat/TranscriptScrollShell'
import type { DisplayItem } from '../../pages/chat/types'
import { anchorAltIdFor, stableAnchorIdFor, uniqueRowKeys, virtualKeyFor } from './rowKeys'
import { useStableMessageKey } from './useStableMessageKey'

/** The host's earlier-history paging state, rendered as the bar above the rows. */
export interface TranscriptEarlierPaging {
  /** Older rows exist beyond what `items` holds; the bar renders only when true. */
  hasMore: boolean
  loading: boolean
  failed: boolean
  onLoad: () => void
  /** Offer the failure's ask-the-agent hand-off (default on). Off for a host
   *  whose unsaved composer draft the hand-off's navigation would discard. */
  handOff?: boolean
}

/** Imperative surface a host reaches through `ref`. Deliberately just what
 *  today's hosts call (the jump pill, the new-reply scroll); the page's
 *  navigation needs (`scrollToIndex`, `mountIndex`, `getFollow`) join when
 *  P5-f mounts the page here — surface follows a caller, not the other way. */
export interface VirtualTranscriptHandle {
  scrollToBottom: (behavior?: ScrollBehavior) => void
}

export interface VirtualTranscriptProps {
  /** Grouped display rows in display order (the list `ChatMessageList` computes). */
  items: readonly DisplayItem[]
  /** Renders one row's content; the component supplies the measured wrapper. */
  renderRow: (item: DisplayItem, index: number) => React.ReactNode
  /** Partitions the persisted height cache and scroll anchor. Prefix it per
   *  host (`pane:`, `side:`, `embed:`) so a split pane and the main page never
   *  restore each other's anchor for the same slot. */
  sessionId: string
  /** A turn is producing output: gates the automatic bottom pin. */
  running?: boolean
  /** Index of the row receiving live growth, so its height changes apply
   *  immediately instead of through the debounced sync. */
  streamingIndex?: number
  /** Pin to the bottom on appends (chat contract). Default true. */
  followOutput?: boolean
  /** Where the list opens with no saved anchor. Default 'bottom'. */
  initialPlacement?: 'top' | 'bottom'
  /** Share the scroll container with a host hook that reads it (usePinnedPrompt). */
  scrollerRef?: React.MutableRefObject<HTMLDivElement | null>
  /** Host scroll listener, composed after the virtualizer's own. */
  onScroll?: () => void
  /** Fires when the rendered at-bottom state flips (drives a jump pill). */
  onAtBottomChange?: (atBottom: boolean) => void
  /** Host padding and geometry merged onto the scroller. */
  scrollerStyle?: React.CSSProperties
  /** Content above the rows (empty states, hydrate errors). The paging bar
   *  from `earlier` renders first. */
  aboveRows?: React.ReactNode
  /** Content below the rows (footers, working indicators). */
  belowRows?: React.ReactNode
  earlier?: TranscriptEarlierPaging
  /** Rows to hide by `visibility` (a bubble the pinned banner stands in for). */
  isRowHidden?: (item: DisplayItem, index: number) => boolean
}

/** Virtualizer tuning shared with the main chat page. */
const ESTIMATED_ROW_HEIGHT = 100
const OVERSCAN = 6
/** Widest bucket: the 900px column plus its row padding, so every wider
 *  scroller shares one height scope. */
const WIDTH_BUCKET_MAX = 944
const WIDTH_BUCKET_STEP = 16
const WIDTH_SETTLE_MS = 200

function bucketWidth(px: number): number {
  return Math.min(Math.round(px / WIDTH_BUCKET_STEP) * WIDTH_BUCKET_STEP, WIDTH_BUCKET_MAX)
}

/** Row heights depend on the column width, so the height cache is scoped by a
 *  width bucket: a pane dragged narrower re-measures instead of trusting
 *  heights recorded at the old width. Debounced — a resize drag must not thrash
 *  the height index. */
function useScrollerWidthBucket(scrollerRef: React.RefObject<HTMLDivElement | null>): number {
  const [bucket, setBucket] = useState(() =>
    bucketWidth(typeof window !== 'undefined' ? window.innerWidth : WIDTH_BUCKET_MAX))
  useLayoutEffect(() => {
    const el = scrollerRef.current
    if (!el) return
    const measure = () => setBucket(bucketWidth(el.clientWidth))
    measure()
    if (typeof ResizeObserver === 'undefined') return
    let timer: ReturnType<typeof setTimeout> | undefined
    const observer = new ResizeObserver(() => {
      clearTimeout(timer)
      timer = setTimeout(measure, WIDTH_SETTLE_MS)
    })
    observer.observe(el)
    return () => {
      observer.disconnect()
      clearTimeout(timer)
    }
  }, [scrollerRef])
  return bucket
}

const VirtualTranscript = forwardRef<VirtualTranscriptHandle, VirtualTranscriptProps>(
  function VirtualTranscript({
    items,
    renderRow,
    sessionId,
    running,
    streamingIndex,
    followOutput,
    initialPlacement,
    scrollerRef: externalScrollerRef,
    onScroll,
    onAtBottomChange,
    scrollerStyle,
    aboveRows,
    belowRows,
    earlier,
    isRowHidden,
  }, ref) {
    const ownScrollerRef = useRef<HTMLDivElement | null>(null)
    const scrollerRef = externalScrollerRef ?? ownScrollerRef
    const msgKey = useStableMessageKey()
    const widthBucket = useScrollerWidthBucket(scrollerRef)

    // Keys are computed list-wide (collision tie-break), then served per row.
    const rowKeys = useMemo(() => uniqueRowKeys(items, msgKey), [items, msgKey])
    const getKey = useCallback(
      (it: DisplayItem, i: number) => rowKeys[i] ?? virtualKeyFor(it, i, msgKey),
      [rowKeys, msgKey],
    )
    const getStableId = useCallback(
      (it: DisplayItem, i: number) => stableAnchorIdFor(it, i, msgKey),
      [msgKey],
    )
    const getAltId = useCallback(
      (it: DisplayItem, i: number) => anchorAltIdFor(it, i, msgKey),
      [msgKey],
    )

    const virt = useVirtualChat<DisplayItem>({
      items: items as DisplayItem[],
      getKey,
      getStableId,
      getAltId,
      sessionId,
      heightScopeKey: `${sessionId}@w${widthBucket}`,
      estimatedHeight: ESTIMATED_ROW_HEIGHT,
      overscan: OVERSCAN,
      eagerFirstMeasure: true,
      externalScrollerRef: scrollerRef,
      streamingIndex,
      runActive: running,
      followOutput,
      initialPlacement,
    })

    useImperativeHandle(ref, () => ({ scrollToBottom: virt.scrollToBottom }), [virt.scrollToBottom])

    const { isAtBottom } = virt
    useEffect(() => { onAtBottomChange?.(isAtBottom) }, [isAtBottom, onAtBottomChange])

    // The virtualizer listens on the element it owns; the shell's onScroll is
    // the host's slot (pinned-prompt tracking), so nothing here double-handles.
    const handleScroll = useCallback(() => { onScroll?.() }, [onScroll])

    const releaseFocusToScroller = useCallback(() => { scrollerRef.current?.focus() }, [scrollerRef])

    return (
      <TranscriptScrollShell
        scrollerRef={scrollerRef}
        onScroll={handleScroll}
        virt={virt}
        loadingOlder={earlier?.loading ?? false}
        // The bar carries its own busy state; the header-pinned overlay
        // belongs to the page's overlay header, which these hosts do not have.
        spinnerNearTop={false}
        headerSpacer={false}
        scrollerStyle={scrollerStyle}
        aboveRows={<>
          {earlier?.hasMore && (
            <EarlierMessagesBar
              loading={earlier.loading}
              failed={earlier.failed}
              onLoad={earlier.onLoad}
              onFocusRelease={releaseFocusToScroller}
              handOff={earlier.handOff}
            />
          )}
          {aboveRows}
        </>}
        belowRows={belowRows}
      >
        {virt.virtualItems.map((vi) => {
          // Unmounted rows are the spacers' job; a placeholder here would be
          // a second copy of that height.
          if (!vi.mounted) return null
          const hidden = isRowHidden?.(vi.data, vi.index) === true
          // A plain block wrapper: it takes the row's own box (padding
          // included), so its rect IS the row's rect for the geometry that
          // reads `data-display-index`, and it adds no class of its own so the
          // theming contract on the inner row is untouched.
          return (
            <div
              key={vi.key}
              ref={virt.measureRef(vi.index)}
              data-display-index={vi.index}
              style={hidden ? { visibility: 'hidden' } : undefined}
            >
              {renderRow(vi.data, vi.index)}
            </div>
          )
        })}
      </TranscriptScrollShell>
    )
  },
)

export default VirtualTranscript

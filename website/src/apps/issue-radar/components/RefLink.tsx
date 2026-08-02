// One same-repo issue/PR reference rendered inside a markdown body: the link
// itself plus its hover preview.
//
// Rendered in place of the default markdown anchor via MarkdownRenderer's
// `LinkOverrideCtx` seam (see RefMarkdown), so it covers BOTH shapes of
// reference identically — a pasted full URL and a bare `#123` that
// `linkifyIssueRefs` turned into a link.
//
// It stays a real <a href>: the click opens the in-app sheet, but a modified
// click, a middle click, and "copy link address" all still behave like a normal
// GitHub link.
import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useQuery } from '@tanstack/react-query'
import { CircleCheck, CircleDot, CircleSlash, GitMerge, GitPullRequest, GitPullRequestDraft } from 'lucide-react'
import { useIssueRadar } from '../context'
import { relativeTimeOrDate } from '../lib/format'
import { issueRadarApi, type RefSummary, type RepoRef as RepoIdentity } from '../api'
import { repoScopeKey } from '../lib/links'
import type { RepoRef } from '../lib/refLinks'
import ShimmerLine from './ShimmerLine'

import { i18nT } from '../../../i18n/t'
import { fmtDateTimeNumeric } from '../../../i18n/format'
/** Delay before a hover opens the preview, so sweeping the pointer across a
 * paragraph of references doesn't flash a card per link (or spend a request per
 * link — the fetch is gated on the same flag). */
const HOVER_OPEN_MS = 320
const CARD_WIDTH = 340
/** Gap between the link and the card, and the minimum margin to the viewport. */
const CARD_GAP = 8

/**
 * The referenced item's summary. Shared by the hover card and by the sheet's
 * issue-vs-PR resolution, so opening a `#123` you already hovered costs nothing.
 * `enabled` keeps it strictly demand-driven — nothing is fetched for a reference
 * that is merely rendered.
 */
export function useRefSummary(repoRef: RepoIdentity, number: number, enabled: boolean) {
  return useQuery({
    queryKey: ['issue-radar', 'ref', repoScopeKey(repoRef), number],
    queryFn: () => issueRadarApi.refSummary(repoRef, number),
    enabled,
    // The server owns freshness (short TTL on its own cache); re-hovering within
    // a session should be instant rather than another round trip.
    staleTime: 5 * 60_000,
    refetchOnWindowFocus: false,
    retry: false,
  })
}

/** Lifecycle of a referenced item, as icon + words. Mirrors the detail panes'
 * state pills: a PR splits merged / closed-unmerged / draft, an issue splits
 * completed / not planned. */
function lifecycle(s: RefSummary): { Icon: typeof CircleDot; tint: string; label: string } {
  if (s.is_pr) {
    if (s.merged_at) return { Icon: GitMerge, tint: 'text-aim', label: i18nT('apps.issueRadar.components.refLink.merged') }
    if (s.state === 'closed') return { Icon: CircleSlash, tint: 'text-muted', label: i18nT('apps.issueRadar.components.refLink.closed') }
    if (s.draft) return { Icon: GitPullRequestDraft, tint: 'text-muted', label: i18nT('apps.issueRadar.components.refLink.draft') }
    return { Icon: GitPullRequest, tint: 'text-ok', label: i18nT('apps.issueRadar.components.refLink.open') }
  }
  if (s.state === 'closed') {
    return s.state_reason === 'not_planned'
      ? { Icon: CircleSlash, tint: 'text-muted', label: i18nT('apps.issueRadar.components.refLink.closed_as_not_planned') }
      : { Icon: CircleCheck, tint: 'text-aim', label: i18nT('apps.issueRadar.components.refLink.closed') }
  }
  return { Icon: CircleDot, tint: 'text-ok', label: i18nT('apps.issueRadar.components.refLink.open') }
}

/** Place the card under the link, flipping above when the bottom of the box is
 * closer than the card is tall, and clamping on BOTH axes so it never hangs
 * outside. `box` is the region the card must stay inside: the reference sheet
 * when the link is inside one (a card that spilled onto the dimmed workspace
 * behind would read as belonging to the wrong surface), else the viewport.
 * Coordinates are viewport-absolute because the card is portalled to <body>, so
 * no `overflow: hidden` ancestor can clip it. */
function cardPosition(rect: DOMRect, cardHeight: number, box: Box): { top: number; left: number } {
  const below = box.bottom - rect.bottom
  const above = rect.top - box.top
  const top = below >= cardHeight + CARD_GAP || below >= above
    ? Math.min(rect.bottom + CARD_GAP, box.bottom - cardHeight - CARD_GAP)
    : rect.top - cardHeight - CARD_GAP
  const maxLeft = box.right - CARD_WIDTH - CARD_GAP
  return {
    top: Math.max(box.top + CARD_GAP, top),
    left: Math.min(Math.max(box.left + CARD_GAP, rect.left), Math.max(box.left + CARD_GAP, maxLeft)),
  }
}

/** The clamp region: viewport-absolute edges. */
interface Box { top: number; right: number; bottom: number; left: number }

const VIEWPORT_BOX = (): Box => ({ top: 0, right: window.innerWidth, bottom: window.innerHeight, left: 0 })

/** The nearest enclosing dialog (the reference sheet), or the viewport. */
function clampBox(el: HTMLElement | null): Box {
  const dialog = el?.closest('[role="dialog"]') as HTMLElement | null
  if (!dialog) return VIEWPORT_BOX()
  const r = dialog.getBoundingClientRect()
  return { top: r.top, right: r.right, bottom: r.bottom, left: r.left }
}

export default function RefLink({
  target, href, children,
}: {
  target: RepoRef
  href: string
  children: React.ReactNode
}) {
  const { active, openRef } = useIssueRadar()
  const anchorRef = useRef<HTMLAnchorElement>(null)
  const openTimer = useRef<number | null>(null)
  const cardRef = useRef<HTMLDivElement>(null)
  const cardId = useId()

  // `rect` doubles as the open flag: non-null means the card is showing, and it
  // is captured at open time so the card can be positioned without measuring on
  // every render.
  const [rect, setRect] = useState<DOMRect | null>(null)
  const [box, setBox] = useState<Box | null>(null)
  // Null until the rendered card has been measured. The flip-above decision needs
  // its real height (which grows with the title's line count), so the card is
  // rendered invisibly for one frame rather than positioned from a guess and then
  // jumped into place.
  const [cardHeight, setCardHeight] = useState<number | null>(null)
  const summary = useRefSummary(active, target.number, rect !== null)

  const clearTimer = () => {
    if (openTimer.current !== null) {
      window.clearTimeout(openTimer.current)
      openTimer.current = null
    }
  }
  useEffect(() => clearTimer, [])

  const scheduleOpen = useCallback(() => {
    clearTimer()
    openTimer.current = window.setTimeout(() => {
      openTimer.current = null
      const el = anchorRef.current
      if (!el) return
      setCardHeight(null)
      setBox(clampBox(el))
      setRect(el.getBoundingClientRect())
    }, HOVER_OPEN_MS)
  }, [])

  const close = useCallback(() => {
    clearTimer()
    setRect(null)
    setBox(null)
  }, [])

  // The card is positioned from a rect captured at open time, so any scroll or
  // resize invalidates it — dismiss rather than let it float away from its link.
  useEffect(() => {
    if (!rect) return
    window.addEventListener('scroll', close, true)
    window.addEventListener('resize', close)
    return () => {
      window.removeEventListener('scroll', close, true)
      window.removeEventListener('resize', close)
    }
  }, [rect, close])

  // Measure the rendered card before it is shown, and re-measure when its
  // content changes height (skeleton -> loaded).
  useLayoutEffect(() => {
    const el = cardRef.current
    if (!el) return
    const h = el.offsetHeight
    setCardHeight((prev) => (prev === h ? prev : h))
  }, [rect, summary.data, summary.error])

  const onClick = (e: React.MouseEvent<HTMLAnchorElement>) => {
    // Leave modified clicks to the browser: open-in-new-tab must keep working.
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
    e.preventDefault()
    close()
    openRef(target)
  }

  const data = summary.data?.summary
  const life = data ? lifecycle(data) : null
  const pos = rect && box ? cardPosition(rect, cardHeight ?? 0, box) : null
  // Hidden (but laid out) for the measuring frame, so nothing flashes at the
  // wrong place.
  const measured = cardHeight !== null

  return (
    <>
      <a
        ref={anchorRef}
        href={href}
        target="_blank"
        rel="noopener noreferrer"
        aria-describedby={rect ? cardId : undefined}
        onClick={onClick}
        onMouseEnter={scheduleOpen}
        onMouseLeave={close}
        onFocus={scheduleOpen}
        onBlur={close}
        // Dashed accent underline: the in-app affordance. A solid underline is
        // an ordinary external link, so the two must not look alike.
        className="text-accent underline decoration-dashed decoration-accent/70 underline-offset-[3px] hover:decoration-accent cursor-pointer"
      >
        {children}
      </a>
      {pos && createPortal(
        <div
          ref={cardRef}
          id={cardId}
          role="tooltip"
          style={{ top: pos.top, left: pos.left, width: CARD_WIDTH, visibility: measured ? 'visible' : 'hidden' }}
          className="fixed z-[60] overflow-hidden rounded-lg border border-border bg-card shadow-2xl px-3 py-2.5 text-[12.5px] pointer-events-none"
        >
          {!measured || summary.isLoading ? (
            <div className="flex flex-col gap-2">
              <ShimmerLine w="42%" />
              <ShimmerLine w="88%" delay={0.08} />
              <ShimmerLine w="56%" delay={0.16} />
            </div>
          ) : null}
          {measured && !summary.isLoading && summary.error && (
            <div className="text-muted">{i18nT('apps.issueRadar.components.refLink.could_not_load')}{target.number}.</div>
          )}
          {measured && !summary.isLoading && data && life && (
            <>
              <div className="flex items-center gap-1.5 text-muted">
                <life.Icon className={`lucide-inline ${life.tint}`} aria-hidden="true" />
                <span className={life.tint}>{life.label}</span>
                <span className="font-mono text-muted">#{data.number}</span>
              </div>
              <div className="mt-1 text-text-strong font-medium leading-snug line-clamp-3 break-words">
                {data.title}
              </div>
              <div className="mt-1.5 text-muted">
                {data.author ? <span className="text-text">{data.author}</span> : 'someone'}
                {' opened '}
                <span title={fmtDateTimeNumeric(data.created_at)}>
                  {relativeTimeOrDate(data.created_at)}
                </span>
              </div>
            </>
          )}
        </div>,
        document.body,
      )}
    </>
  )
}

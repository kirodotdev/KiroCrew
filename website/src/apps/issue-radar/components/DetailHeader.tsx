// The detail panes' header, shared by the issue pane and the pull-request pane.
//
// WHY THIS SHAPE
//
// The header used to sit OUTSIDE the pane's scroller, which made its height
// standing furniture: whatever it cost vertically, it cost for the whole
// session. On a phone that was 273px of an 844px viewport before any content.
// A header that lives INSIDE the narrow scroller yields that height by physics —
// the tall title scrolls away 1:1 with the finger — while two sticky rows keep
// what has to stay reachable.
//
// This is deliberately NOT a height animation. An earlier attempt kept the
// header outside and animated a collapse, which needed a hysteresis band purely
// because collapsing mutated `scrollHeight` and let the browser clamp
// `scrollTop` back down, re-expanding the header under a stationary finger.
// Sticky rows do not move the scroll height, so that whole class of problem —
// and the band, the dual-title height math, and the grid-track transition — goes
// away with it.
//
// WHY THE BACK ROW CARRIES THE TITLE
//
// The narrow back control is 44px that the pane pays for unconditionally, and at
// rest it holds one short label and a lot of empty space. Putting the compact
// title there rather than in the toolbar is what lets the toolbar be ONE row:
// state, the number and three actions fit on a 390px line only when they are not
// also sharing it with a title. Measured: 44 + 45 standing, against 44 + 76 when
// the title competed for the toolbar's row.
//
// Two sticky rows rather than one block, at `top-0` and `top-11`, so the reading
// order at rest is unchanged — back, then the tall title, then the toolbar. A
// single contiguous sticky block would have to hoist the toolbar above the title,
// which is a different pane, not a narrower one.
//
// ONE DOM SHAPE, NOT TWO
//
// The header renders in exactly one place. Which element owns `overflow` is what
// changes at the breakpoint, and that is already how these panes work: below
// `sm:` the wrapper is the single scroller (so these rows are its sticky
// children), and above it the wrapper is `overflow-visible` while the two body
// columns scroll themselves (so they are static blocks above them). Nothing is
// conditionally mounted, so there is no second structure to keep in step.
//
// SLOTS, NOT DATA
//
// The two panes disagree on their content — `StatePill` takes a close reason in
// one and draft/merged flags in the other, only the issue pane has a lock, only
// the PR pane carries a review bar — so this takes ReactNode slots and owns the
// MECHANISM only.
import type { ReactNode } from 'react'

export interface DetailHeaderProps {
  /** True once the reader has scrolled past the tall title. */
  collapsed: boolean
  /** The full title. Rendered as the pane's only `<h1>`. */
  title: string
  /** Callback ref for the tall title block — what `useTitleScrolledOut`
   *  observes to decide whether the compact echo should be showing. */
  titleRef: (node: HTMLElement | null) => void
  /** The narrow back-to-list control, or null on a desktop. Rendered by the pane
   *  rather than the shell above it so it can share a row with the echo. */
  back?: ReactNode
  /** Skeleton shown instead of the title/meta before the first real paint. */
  skeleton?: ReactNode
  /** Withhold the title and meta (a pane opened from a cross-reference has no
   *  real values yet and would render fabricated ones). */
  awaitingFirstPaint?: boolean
  /** Scrolls away with the tall title: authorship, identity, dates — anything
   *  the opening comment card repeats a few pixels below. */
  meta: ReactNode
  /** Stays in the toolbar: pane state that appears nowhere else standing. */
  identity: ReactNode
  /** Stays in the toolbar: the pane's actions. */
  actions: ReactNode
  /** Optional extra row under the toolbar (a review bar, an error line).
   *
   *  THE ONE UNBOUNDED PART OF THE PINNED REGION. Everything else here is
   *  fixed-height by construction — that is what lets the scroll-driven state be
   *  a single boolean with no hysteresis. This slot is not: the PR pane puts
   *  `PrActionsBar` here so a reviewer can approve without scrolling back up,
   *  and opening one of its composers grows a textarea inside the pinned area.
   *  So the "44px in both states" measurement is the ISSUE pane's; the PR pane's
   *  pinned height is 44px plus whatever this slot is currently showing.
   *
   *  That is a pre-existing cost rather than something this design added:
   *  `PrActionsBar` already lived in this header on the base branch, where the
   *  header never scrolled away at all, so the composer sat on top of a 273px
   *  standing block instead of an 88px one. It does NOT reintroduce the
   *  hysteresis problem either — that loop needed a SCROLL-driven height change
   *  feeding back into `scrollTop`, and opening a composer is a user action, not
   *  a scroll position. Worth revisiting whether the composer should scroll
   *  normally on a phone; that is a product call, not a correctness one. */
  extra?: ReactNode
}

export default function DetailHeader({
  collapsed, title, titleRef, back, skeleton, awaitingFirstPaint,
  meta, identity, actions, extra,
}: DetailHeaderProps) {
  // A FRAGMENT, not a wrapper element. `position: sticky` cannot escape its
  // parent's box, so wrapping these in one element made them ride away with that
  // wrapper the moment it scrolled past — measured in a pod at scrollTop=420 the
  // toolbar's top was -89px, i.e. gone. Every part is a direct child of the
  // scroller instead, which is what gives each the whole scroll length to stick
  // over. The <header> landmark sits on the toolbar, the part that persists.
  return (
    <>
      {/* Row 1: back + the compact echo. Only rendered while narrow, because the
          back control itself is narrow-only — on a desktop both panes are on
          screen and there is nothing to return from. */}
      {back && (
        <div className="sticky top-0 z-20 bg-bg flex items-center gap-2 px-4 md:px-6 min-h-11">
          {back}
          {/* Decorative: the `<h1>` below is the real heading and stays in the
              DOM, so assistive tech reads the title once. It fades in only after
              the tall title has left, or a reader at the top would see the same
              words twice. Fades, never resizes — `truncate` cannot be
              interpolated, and a size change here would move `scrollHeight`. */}
          <span
            aria-hidden="true"
            className={`min-w-0 flex-1 truncate text-[13.5px] font-semibold text-text-strong transition-opacity duration-150 motion-reduce:transition-none ${collapsed ? 'opacity-100' : 'opacity-0'}`}
          >
            {title}
          </span>
        </div>
      )}

      {/* The tall title block — ordinary scrolling content on a phone.
          All three parts of this header carry the SAME gutter (`px-2` on a phone,
          `md:px-6` from 768 up) and must keep carrying it, because they are three
          separate boxes stacked into one visual header: give the title block a
          different inset from the toolbar and the title stops sharing a left edge
          with the state pill under it. The 768px flip is `useIsMobile`'s, the one
          signal this app already treats as narrow — deliberately NOT this
          component's own `sm` shape flip, so that the 640–767px band where the
          pane is already side by side with the 236px sidebar gets the tighter
          gutter too. That band is where the reading column is narrowest and can
          least afford 32px of edge. */}
      <div ref={titleRef} data-testid="detail-title-block" className="px-4 md:px-6 pt-4 pb-3 sm:pt-5 sm:pb-0">
        {awaitingFirstPaint ? skeleton : (<>
          <h1 className="text-[27px] font-bold leading-tight text-text-strong break-words">
            {title}
          </h1>
          <div className="flex items-center gap-2 mt-3 flex-wrap text-[12.5px] text-muted">
            {meta}
          </div>
        </>)}
      </div>

      {/* Row 2: the toolbar. `top-11` so it pins UNDER row 1 rather than over it
          (11 = the 44px min-height row 1 reserves). `sm:static` because above the
          breakpoint nothing scrolls past it, and stating that keeps the intent
          explicit rather than incidental.
          Left-aligned: no `ml-auto` on the actions, so state, the number and the
          actions read as one left-anchored toolbar at rest instead of splitting
          to opposite edges. */}
      <header className="sticky sm:static top-11 sm:top-auto z-10 bg-bg border-b border-border px-4 md:px-6 py-2 sm:pt-4 sm:pb-4">
        <div className="flex items-center gap-2 flex-wrap">
          {/* Withheld until first paint, exactly like the title and meta above.
              `identity` carries the state pill, and `state` falls back to 'open'
              on a placeholder — a pane opened from a cross-reference to an item
              that is not in the list has no fetched state yet — so rendering it
              unconditionally shows a fabricated "Open" on an issue that may be
              closed, until the read lands. The base branch had this covered
              because the pill lived inside the same `awaitingFirstPaint` block as
              the title; splitting the toolbar out is what dropped the guard. */}
          {!awaitingFirstPaint && (
            <span className="flex items-center gap-2 flex-wrap text-[12.5px] text-muted">
              {identity}
            </span>
          )}
          {/* `items-stretch`, so the two controls are the same height.
              They are not the same component: the primary action is
              AgentSessionButton's own button (text + a 13px icon, so a text line
              box sets its height) and the overflow trigger is the shared `Btn`
              holding ONLY a 14px icon — with no text node its `line-height`
              never becomes a line box, so it measured 24.0px against the
              primary's 26.6px even though it is the one WITH a border.
              Stretching is what avoids hardcoding that 26.6px, which is an
              inherited-line-height product and not a token. Safe here because
              this group holds buttons only — a stretch beside a paragraph would
              grow a button to the paragraph's height, which is why the row
              itself stays `items-center`. */}
          <span className="flex-shrink-0 flex items-stretch gap-1.5">
            {actions}
          </span>
        </div>
        {extra}
      </header>
    </>
  )
}

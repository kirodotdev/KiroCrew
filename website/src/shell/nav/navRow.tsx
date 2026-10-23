import { forwardRef, useEffect, useRef, useState, type ForwardedRef } from 'react'
import { OverlayPortal } from '../../components/overlayRoot'
import { useLocation, useNavigate } from 'react-router-dom'
import { useAppSelector } from '../../store'
import { getBuiltinSurface, selectSurfaceBadgeCount, selectSurfaceActivityCount } from '../../surfaces/registry'
import { useIsMobile } from '../../hooks/useIsMobile'
import { Bot } from 'lucide-react'
import { useMayLeaveForNavigation, useIsCurrentUrl } from '../../components/NavigationLeaveGuard'
import { motion } from 'framer-motion'
import { useNavShortcutHint } from '../../hooks/useNavShortcutHint'
import { i18nT } from '../../i18n/t'
import { forwardUiLocation } from '../../uiLocations/uiLocation'
import type { UiLocationId } from '../../uiLocations/descriptors'
import type { AppRunState } from '../../appRunState'
import { useNavTip } from './navTip'
import { clearFeatureNewTag, featureNewTagRoute, useFeatureNewTag } from '../../utils/featureNewTag'
import { FeatureNewTagFlyer, useFeatureNewTagFlight } from './featureNewTagFlight'

function BadgeIndicator({ count, collapsed, label }: { count: number; collapsed: boolean; label: string }) {
  if (count <= 0) return null
  const ariaLabel = `${count} ${label}`
  return collapsed
    ? <span className="absolute top-1 right-1 w-2 h-2 bg-accent rounded-full z-10" role="status" aria-label={ariaLabel} />
    // Expanded: IN FLOW, not `absolute right-2`. The row's other right-edge
    // occupant is the hover/focus shortcut hint, which is an in-flow span — so an
    // absolutely-positioned badge sat ON TOP of it and a row with an unread count
    // advertised a chord the badge covered ("Sessions ⌥C" read as "Sessions (1)"
    // with a sliver of the modifier glyph showing). In flow the two are siblings
    // in the row's flex line and cannot overlap at any count width.
    //
    // `title` names what the number IS, for sighted users: a bare pill beside a
    // bare bot glyph does not say what either counts, and the fix above makes the
    // two reliably co-visible. It carries the LABEL ALONE, not `ariaLabel` — the
    // label is a plural phrase, so "1 unread conversations" would be visibly
    // wrong at count 1, and the count is already rendered in the pill an inch
    // away. `aria-label` keeps the count because a screen reader gets no pill.
    // The Settings rows' update dot likewise carries different title and aria strings.
    : <span className="shrink-0 bg-accent text-accent-fg text-[12px] font-bold px-1 py-[2px] rounded-full min-w-[18px] text-center inline-block leading-[12px]" title={label} aria-label={ariaLabel}>{count}</span>
}

/** Sub-agent activity belongs in the expanded rail, where the bot icon and
 *  count communicate what is active. The collapsed rail omits it: a second
 *  anonymous dot competes with the unread badge without identifying a session,
 *  while the Sessions list provides the actionable per-session status. */
function ActivityIndicator({ count, collapsed, label }: { count: number; collapsed: boolean; label: string }) {
  if (count <= 0 || collapsed) return null
  const ariaLabel = `${count} ${label}`
  return <span className="shrink-0 flex items-center gap-1 text-[11px] text-accent" role="status" title={label} aria-label={ariaLabel}>
    {/* Size and spacing are main's. A 12px glyph was tried here to make the mark
        identify its own count at a glance, and a reader of the rendered frames
        still could not tell what it depicted -- so it bought nothing and cost the
        row's last pixel of label width (the Sessions label fits in exactly 61px;
        12px takes 62 and clips it to "Sessions" minus two characters). What this
        glyph depicts is a question about the rail's iconography rather than about
        the overlap, so it is left to the follow-up rather than guessed at here.
        The `title` carries the naming for a user who hovers. */}
    <Bot size={11} aria-hidden />
    {count}
  </span>
}

/**
 * Accessible name for a rail run-state mark.
 *
 * A literal key per state rather than one interpolated from the state value:
 * `dynamicKeys.test.ts` polices composed keys, and a literal also keeps the
 * three strings findable by grep from the catalog side.
 */
function runStateLabel(state: AppRunState | undefined): string {
  if (state === 'running') return i18nT('app.app_job_running')
  if (state === 'error') return i18nT('app.app_job_failed')
  if (state === 'success') return i18nT('app.app_job_succeeded')
  return ''
}

/**
 * Run-state mark for an installed app's rail row.
 *
 * Renders in BOTH rail modes, unlike `ActivityIndicator` above. That component
 * withholds itself when collapsed because a second anonymous dot there competes
 * with the unread badge without identifying a session; this mark is the opposite
 * case on both counts -- it names the row's own app, and the collapsed rail is
 * exactly where "is that job still going?" has to be answerable without opening
 * the app, which is the whole point of having it.
 *
 * Shares the row's flex line with the other two indicators rather than claiming
 * a corner of its own. An earlier revision placed each mark at a fixed offset
 * the others were assumed not to use, which is only true while every one of them
 * stays the width it was assumed to be -- a count pill grows with its digits.
 * Siblings in one flex line cannot overlap at any width, so the arrangement no
 * longer rests on an assumption about anyone's size. Independently of geometry,
 * exactly one surface in the registry carries an `activitySelector` -- `chat`
 * (Sessions), a Main-group HOST surface. A host surface row never carries an
 * `appName`, so it never receives this mark, and an app row never receives an
 * activity count: those two cannot meet on one row regardless.
 *
 * Colour alone does not carry the state. `running` is a filled dot inside a wide
 * halo ring, `error` is a plain solid fill, and `success` is HOLLOW -- a ring
 * with no fill. The error/success pair is the one that matters: red and green
 * are the classic confusion, and a static red dot beside a static green one
 * differing only in hue reads a failing job as a fresh success, silently, every
 * time. The fill carries that distinction so hue does not have to, and `title`
 * plus the accessible name state it in words for a sighted user and a screen
 * reader.
 *
 * Nothing animates. An earlier revision pulsed `running` to mirror the Schedule
 * page's badge, but this mark lives in persistent chrome on every page, so a
 * long job would pull peripheral attention for its whole duration -- a cost the
 * Schedule page does not pay, because the user goes there to watch. The halo
 * ring distinguishes `running` without motion.
 */
function RunStateIndicator({ state, collapsed, label }: { state: AppRunState | undefined; collapsed: boolean; label: string }) {
  if (!state) return null
  const shape = state === 'running'
    ? 'bg-accent ring-2 ring-accent/30'
    : state === 'error'
      ? 'bg-danger'
      : 'bg-transparent ring-1 ring-ok'
  // Collapsed: absolute in the icon's own corner, where there is no flex line to
  // join and no chord to cover. Expanded: IN FLOW with the other two right-edge
  // indicators. Keeping this one `absolute right-8` while the count pill moved
  // into the line would have left exactly the bug this change exists to fix, one
  // component over: an app row renders this mark AND an `appBadges`-driven pill,
  // and a 2-3 digit pill reaches left past 32px to sit under it. Flex items
  // cannot overlap, so joining the line closes it for this mark too rather than
  // relying on the pill staying narrow. `z-10` goes with the absolute arm: an
  // in-flow sibling needs no stacking order to avoid a box it cannot intersect.
  return collapsed
    ? <span className={`absolute bottom-1 right-1 w-2 h-2 rounded-full z-10 ${shape}`} role="status" aria-label={label} title={label} />
    : <span className={`shrink-0 w-2 h-2 rounded-full ${shape}`} role="status" aria-label={label} title={label} />
}

/**
 * Badge slot for a nav item. Resolves the count from the surface registry
 * (built-in surfaces) and falls back to the `mc:app:badge`-driven `appBadges`
 * map (dynamic apps + bridges from non-Redux sources like global approvals)
 * when the surface itself doesn't declare a badge source. This preserves the
 * prior two-pipeline behavior without leaving per-id branches in the
 * renderer.
 */
export function NavBadge({ navId, collapsed, appBadges, runState }: { navId: string; collapsed: boolean; appBadges: Record<string, number>; runState?: AppRunState }) {
  const surface = getBuiltinSurface(navId)
  // selectSurfaceBadgeCount caches per-navId so this stays referentially
  // stable across renders inside a `.map()`.
  const builtinCount = useAppSelector(selectSurfaceBadgeCount(navId))
  // Dynamic-app badges live outside Redux (set via a window event or a
  // direct setAppBadges sync). Consult them whenever the surface itself
  // doesn't own a badge source — including stub surfaces that only exist to
  // declare nav metadata. Surfaces with their own badge source (slotMode or
  // unreadSelector) skip the fallback to avoid double-counting.
  const surfaceHasBadgeSource = surface !== undefined && (surface.unreadSelector !== undefined || surface.slotMode !== undefined)
  const appName = navId.startsWith('app-') ? navId.slice(4) : navId
  const dynamicCount = surfaceHasBadgeSource ? 0 : (appBadges[appName] || 0)
  const builtinLabel = surface?.badgeLabel ?? i18nT('app.updates')
  const activityCount = useAppSelector(selectSurfaceActivityCount(navId))
  const activityLabel = surface?.activityLabel ?? i18nT('app.subagents_in_flight_fallback')
  // The "New" tag a feature intro's "Not now" handed to this row. It clears the
  // first time the route opens, by any path (this row, a link, the address bar).
  // `arriving` while the intro's avatar is still flying here: the pill is
  // rendered, invisible, so the flight below can measure where to land.
  const newTag = useFeatureNewTag(navId)
  // State, not a ref: the flight hook must re-run when the pill mounts, and a
  // ref's `.current` changing re-runs nothing.
  const [pill, setPill] = useState<HTMLElement | null>(null)
  const { flight, landed, land } = useFeatureNewTagFlight(navId, newTag, pill)
  const { pathname, search } = useLocation()
  const navigate = useNavigate()
  const surfaceRoute = surface?.route
  const onRoute = !!surfaceRoute && (pathname === surfaceRoute || pathname.startsWith(surfaceRoute + '/'))
  // Starts false so a page load onto the route counts as arriving.
  const wasOnRoute = useRef(false)
  useEffect(() => {
    const arrived = onRoute && !wasOnRoute.current
    wasOnRoute.current = onRoute
    if (!newTag || !onRoute) return
    // Set while already on the page (here or in another tab): only clear, since a
    // redirect would act like Try now and skip an unsaved form's leave guard.
    if (!arrived) {
      clearFeatureNewTag(navId)
      return
    }
    const route = featureNewTagRoute(navId)
    // Only the plain page is redirected to the intro's route; a link to a
    // specific crewmate keeps its target.
    if (route && pathname + search !== route && pathname === surfaceRoute && !search) navigate(route, { replace: true })
    clearFeatureNewTag(navId)
  }, [newTag, onRoute, surfaceRoute, navId, pathname, search, navigate])
  // Collapsed, the only corner badge is the unread dot; the tag yields to it
  // rather than stacking two marks on one icon.
  const showNewTag = !!newTag && !(collapsed && builtinCount + dynamicCount > 0)
  return (
    <>
      {/* Collapsed, the tile is too narrow for the pill: it hangs off the
          lower-right corner so the icon stays visible. */}
      {showNewTag && (
        <motion.span
          ref={setPill}
          data-feature-new-tag={navId}
          data-feature-new-tag-state={newTag}
          // Invisible, not absent, while the avatar is on its way: the flight
          // measures this box to know where to land.
          style={newTag === 'arriving' ? { visibility: 'hidden' } : undefined}
          // The pop on landing: the avatar touches down and the pill takes its
          // place with a small bounce. A tag that arrived with no flight (reduced
          // motion, a row off screen) is simply there.
          initial={false}
          animate={landed ? { scale: [0.6, 1.18, 1] } : { scale: 1 }}
          transition={{ duration: 0.35, ease: 'easeOut' }}
          className={collapsed
            ? 'absolute -bottom-1.5 -right-[9px] z-10 bg-accent text-accent-fg text-[10px] font-bold leading-[12px] px-1 py-0 rounded-full border-2 border-bg'
            : 'shrink-0 bg-accent text-accent-fg text-[10px] font-bold leading-[12px] px-1.5 py-[2px] rounded-full'}
        >
          {i18nT('components.startupVideoModal.new_tag')}
        </motion.span>
      )}
      {flight && <FeatureNewTagFlyer navId={navId} from={flight.from} to={flight.to} onLanded={land} />}
      <ActivityIndicator count={activityCount} collapsed={collapsed} label={activityLabel} />
      <RunStateIndicator state={runState} collapsed={collapsed} label={runStateLabel(runState)} />
      <BadgeIndicator count={builtinCount} collapsed={collapsed} label={builtinLabel} />
      <BadgeIndicator count={dynamicCount} collapsed={collapsed} label={builtinLabel} />
    </>
  )
}

/** Exported for `capture/nav-badge-chord.tsx`, which measures the row's
 *  right-edge geometry in a real browser — the one check that can see the
 *  badge-over-chord overlap this row's unit tests can only pin structurally
 *  (happy-dom computes no layout). Same seam `UpdateOverlay` is exported on. */
export const NavItem = forwardRef(function NavItem({ path, label, icon, active, collapsed, badge, onClickOverride, onClick, navId, pressed, touch, replace, caption, 'data-ui-location': uiLocationId }: {
  path: string; label: string; icon: React.ReactNode; active: boolean; collapsed: boolean; badge?: React.ReactNode; onClickOverride?: () => void; onClick?: () => void; navId?: string
  /** Set on rows that TOGGLE a surface rather than navigate (e.g. the docked
   *  terminal). `active` only paints the row; without aria-pressed a screen
   *  reader announces an identical button whether the panel is open or shut. */
  pressed?: boolean
  /** Phone rail geometry for a `collapsed` row: a 64x56 `rounded-xl` tile with a
   *  10px caption under the glyph, instead of the desktop rail's pointer-sized
   *  icon-only `rounded-md` row. Same icon, same selected paint. */
  touch?: boolean
  /** Navigate with `replace` instead of a push. The phone drawer that hosts the
   *  rail holds a duplicate history entry while open (see ChatPage's
   *  `pushDrawerEntry`); a row leaving the chat page overwrites it so Back
   *  lands on the chat, not on a second copy of it. */
  replace?: boolean
  /** Phone rail tile caption when the full label is too long for a 64px tile
   *  (e.g. "Agent Capabilities" -> "Capabilities"). The accessible name stays
   *  the full label. */
  caption?: string
  /** A registered find_ui location (`{...uiLocation(id)}` spread on the row),
   *  forwarded to the row element a person taps. Typed to the registered ids,
   *  so the row takes no arbitrary attribute. */
  'data-ui-location'?: UiLocationId
}, forwardedRef: ForwardedRef<HTMLDivElement>) {
  const navigate = useNavigate()
  // On mobile this row lives inside the nav DRAWER, whose slide runs on the
  // compositor (animateDrawer) — and a framer layout-projection node under a
  // compositor-driven ancestor transform mis-attributes the panel's travel to
  // itself, compounding a corrective offset (the ChatSidebar rows measured
  // >4,000px of it). The desktop rail is framer-free motion-wise, so it keeps
  // the row-reorder glide that `layout` buys there.
  const isMobileRow = useIsMobile()
  const iconEl = <span className={`app-icon-nav w-4 h-4 flex items-center justify-center shrink-0 transition-opacity ${active ? 'opacity-100 text-accent is-lit' : 'opacity-70'}`}>{icon}</span>
  const { tip, tipOn, rowRef, showTip, hideTip, pointerProps: tipPointerProps } = useNavTip<HTMLDivElement>(collapsed)
  // Derived from the shortcut registry by route, so a row with a bound panel
  // chord advertises it and a row without one is untouched. Null when the user
  // has turned shortcuts off. See useNavShortcutHint for why this resolves per
  // render rather than being written next to each row.
  const shortcut = useNavShortcutHint(path)
  const mayLeave = useMayLeaveForNavigation()
  const isCurrentUrl = useIsCurrentUrl()
  const activate = () => {
    // Navigating swaps the whole page, and the page leaving may hold a draft the
    // user typed — `beforeunload` cannot defend it, because a client-side route
    // change never unloads the document. Ask its guard first.
    //
    // Gated on this row actually going SOMEWHERE ELSE (see `useIsCurrentUrl` for
    // why that test is the whole URL and not `active`). A row with an
    // `onClickOverride` toggles a surface — the docked terminal, the phone
    // dialog — and unmounts nothing, so it keeps its exemption; an unqualified
    // ask would pop a discard-confirm over a click that was never going to
    // destroy anything.
    const asked = !onClickOverride && !isCurrentUrl(path)
    if (asked && !mayLeave()) return
    // `leaveGranted`: the page's guards were asked just now, in this same
    // synchronous step (the guide's `enter` writes the same mark), so a landing
    // page that stays mounted through this navigation and acts on it — the
    // Crewmates page closing its open create card — must not ask the same
    // question again. A same-address click asked nothing, so it carries no
    // mark and that page asks for itself.
    onClick?.(); (onClickOverride || (() => navigate(path, { replace, state: asked ? { leaveGranted: true } : undefined })))()
  }
  return (
    <motion.div layout={isMobileRow ? undefined : 'position'}
      data-onboarding-nav={navId}
      // Forwarded, not a render site of its own: the site is the spread on <NavItem>.
      {...forwardUiLocation(uiLocationId, rowRef, forwardedRef)}
      // role+tabIndex+key handler make this a real keyboard-operable control
      // (Enter/Space activate, preventing Space page-scroll). aria-label names
      // it when collapsed (icon-only, no text).
      role="button"
      tabIndex={0}
      // Hover is a HOVER: the row paints (`hover:bg-bg-hover` / `hover:text-text`
      // below) and does not move. A scale on hover made every rail row grow a
      // couple of pixels under the cursor, nudging its neighbours and re-reading
      // as a layout change rather than as "you are pointing at this". Press still
      // scales — that one is feedback for an action the user actually took.
      whileTap={{ scale: 0.97 }}
      transition={{ duration: 0.15 }}
      // `touch`: the desktop rail dims an inactive icon to 70% (`opacity-70` on
      // the glyph span), which on the phone rail's flat 40x40 tiles measured
      // 3.4:1 against a light surface. The tile keeps the muted colour at full
      // opacity instead (>= 4.5:1); active tiles are unchanged.
      // The glyph is sized through `>*` with `!`, not `>svg`: the brand marks
      // (Crewmates, Customize) and an app's own icon (Task Runner) are spans
      // with an inline 16px `width`/`height` that a plain class cannot beat,
      // which would leave them smaller than the 20px Lucide tiles beside them.
      className={`nav-item group/nav relative flex items-center min-w-0 cursor-pointer text-sm font-medium whitespace-nowrap gap-2.5 transition-colors duration-200 ${touch ? 'w-16 h-14 px-0.5 flex-col justify-center gap-0.5 rounded-xl shrink-0 [&_.app-icon-nav]:w-5 [&_.app-icon-nav]:h-5 [&_.app-icon-nav>*]:!w-5 [&_.app-icon-nav>*]:!h-5 [&_.app-icon-nav]:opacity-100' : 'rounded-md py-2 pl-3 pr-3'} ${collapsed ? '' : 'overflow-hidden'} ${active ? 'nav-active text-text-strong bg-accent-subtle hover:brightness-110' : 'text-muted hover:text-text hover:bg-bg-hover/60'}`}
      onClick={activate}
      onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); activate() } }}
      {...tipPointerProps}
      onMouseEnter={showTip}
      onMouseLeave={hideTip}
      // Keyboard-only users (no pointer) can't trigger the mouse-driven hover
      // label, so surface it on focus too. showTip/hideTip no-op unless collapsed,
      // making these inert in expanded mode where the text label is already shown.
      onFocus={showTip}
      onBlur={hideTip}
      aria-label={collapsed ? label : undefined}
      aria-pressed={pressed}
      // The chord declared to assistive tech, in the ARIA grammar rather than the
      // display glyphs — the same split MoveUndoBar's Undo button already ships.
      // This is what makes the hint reachable without a pointer: the visible
      // badge below is hover/focus-revealed decoration and is aria-hidden, so the
      // attribute is the non-visual route rather than a duplicate of one.
      aria-keyshortcuts={shortcut?.ariaKeyshortcuts}
    >
      {iconEl}
      {/* `aria-label` carries the FULL label: this span is `whitespace-nowrap overflow-hidden`, so
          a translation longer than the rail is silently cut off with no way to read it. Surfaced by
          the render gate under the en-XA pseudolocale at 2.2x once a new app entry narrowed the
          row (`layout/clipped-without-title`), which accepts `title` OR `aria-label`. Deliberately
          `aria-label`, NOT `title`: a page-wide `getByTitle('Settings'/'Board'/…)` in another app's
          Playwright specs (ops-mission-control) matches on `title`, and a sidebar nav item titled
          the same as one of those segment names would be clicked instead of the segment. `label`
          is already the resolved, translated string. */}
      {!collapsed && (
        <span
          aria-label={typeof label === 'string' ? label : undefined}
          className="flex-1 min-w-0 truncate"
        >
          {label}
        </span>
      )}
      {/* Phone rail tile: a one-word caption under the glyph. The desktop rail's
          collapsed rows name themselves with a hover tip, which a finger cannot
          summon, and a cold reader could not tell the Artifacts and
          Capabilities glyphs apart (UX lane). 10px is this project's floor. */}
      {collapsed && touch && (
        <span aria-hidden="true" className="max-w-full whitespace-normal text-center text-[10px] leading-[1.1] font-medium tracking-tight line-clamp-2 [overflow-wrap:anywhere]">{caption ?? label}</span>
      )}
      {/* Expanded rail: the chord rides the row's existing `group/nav` seam, so it
          appears on hover AND on keyboard focus-visible rather than on hover alone
          — the row is already `tabIndex={0}`, and a hover-only hint would be
          unreachable to a keyboard or touch user, which is the defect class #4120
          was fixed for and #3626 is still open on. `aria-hidden` because the
          accessible name must stay the label: the chord is declared exactly once,
          on `aria-keyshortcuts` above, rather than read out as glyphs. */}
      {!collapsed && shortcut && (
        <span
          aria-hidden="true"
          // Keycap DATA, not prose. `[data-i18n-opaque]` is the render-time i18n
          // gate's own marker for exactly this (render-scan.mjs OPAQUE_SELECTOR,
          // whose comment names "a keycap container span"). It costs nothing
          // visually and is not currently load-bearing -- the badge is opacity-0
          // until hover, so the scan does not see it -- but without it the class is
          // declared nowhere, and whoever makes this visible by default would get a
          // pseudolocale failure with no clue why.
          data-i18n-opaque=""
          data-testid={navId ? `nav-shortcut-${navId}` : undefined}
          className="shrink-0 text-[11px] leading-none text-muted opacity-0 transition-opacity duration-150 group-hover/nav:opacity-100 group-focus-visible/nav:opacity-100"
        >
          {shortcut.chord}
        </span>
      )}
      {/* LAST in the flex line, so the expanded unread/activity indicators sit to
          the RIGHT of the chord above rather than over it. Order matters only for
          the in-flow expanded indicators: every caller-supplied badge (the dev
          dot, the update dot) and the collapsed dot are absolutely positioned
          against the row, so they render where they always did regardless of
          where in the children they appear. */}
      {badge}
      {collapsed && tip && <OverlayPortal>
        <div
          className={`fixed flex items-center gap-2.5 pl-3 pr-3 rounded-md bg-card border border-border shadow-lg text-text text-sm font-medium z-[9999] pointer-events-none whitespace-nowrap transition-opacity duration-150 ${tipOn ? 'opacity-100' : 'opacity-0'}`}
          style={{ top: tip.top, left: tip.left, height: tip.height }}
        >
          <span className={`app-icon-nav w-4 h-4 flex items-center justify-center shrink-0 ${active ? 'text-accent is-lit' : ''}`}>{icon}</span>
          {label}
          {/* Collapsed rail: the row carries no text label, so this flyout IS its
              hover affordance — and it already opens on focus as well as hover
              (see onFocus/onBlur above), which is what carries the hint to a
              keyboard user on this width. */}
          {shortcut && (
            <span aria-hidden="true" data-i18n-opaque="" className="shrink-0 text-[11px] leading-none text-muted">
              {shortcut.chord}
            </span>
          )}
        </div>
      </OverlayPortal>}
    </motion.div>
  )
})

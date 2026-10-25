import { useEffect, useState, lazy, Suspense, type ReactNode } from 'react'
import { TOPBAR_FOCUS_Z, TOPBAR_Z } from '../../lib/themeDecorLayer'
import { kiroUsageSegment } from './kiroUsageReadout'
import { metricsSegment, MetricsErrorNotice } from './metricsReadout'
import { Bell, Fullscreen, Menu, Plug, Unplug } from 'lucide-react'
import { motion } from 'framer-motion'
import { isWinElectron } from '../../lib/electron'
import { setNativeBadgeCount } from '../platform/electronBridge'
import { SELECTION_INERT_ATTR } from '../../pages/chat/useSelectionInertOverlays'
import ErrorBoundary from '../../components/ErrorBoundary'
import { useNotificationSheet, NotificationSheet } from '../notifications/notificationSheet'
import InstanceTabBar from '../../components/InstanceTabBar'
import { getTopBarWidgets } from '../../apps/topBarWidgets'
import { getCapsuleSegments } from '../../apps/capsuleSegments'
import { IS_MAC } from '../../hooks/useKeyboardShortcuts'
import { useNavShortcutHint } from '../../hooks/useNavShortcutHint'
import FeedbackPill from '../../components/FeedbackPill'
import { Glass } from '../../components/Glass'
import WindowsTitlebarMenu from '../../components/WindowsTitlebarMenu'
import { NavHistoryArrows } from '../../components/NavHistoryArrows'
import { i18nT } from '../../i18n/t'
import { uiLocation } from '../../uiLocations/uiLocation'
import { GuideRevealScope } from '../../guide/GuideRevealScope'
import type { AppShell } from '../appShell'
import { hiddenChromeProps } from '../focus/focusChrome'
import { setAgentSwitchNotice } from '../../store/chatSlice'

// Lazy, like App.tsx's update-found popup: the pill renders nothing without an update,
// so its code rides the on-demand chunk instead of the app core.
const UpdatePill = lazy(() => import('../../components/UpdatePill'))

// Corner radius, in px, of the top bar's Liquid Glass pills (the search
// trigger, the readout capsule; components/FeedbackPill.tsx carries the same
// number): `rounded-xl`, which the update pill already wears, so the row reads
// as one family of boxes.
const TOPBAR_PILL_RADIUS = 12

// The top-bar search is laid out by CSS, not measured here: `.topbar` in
// index.css is a three-track grid whose centre track is
// `clamp(240px, 22vw, 480px)` and whose side tracks are equal `minmax(0,1fr)`
// remainders, so the search is window-centred by construction and each side
// group adapts its own contents with a container query. The previous
// implementation centred an absolutely-positioned overlay on `50vw`, which
// forced it to reserve `max(left, right)` on BOTH sides and drop itself entirely
// once that mirrored gutter fell under a floor — on an asymmetric header that
// discarded twice the difference between the two clusters.

/** Glyph inside the mobile nav toggle: the product logo once it has actually
 *  loaded, the Menu hamburger at every other instant. This button is the ONLY
 *  route to the nav rail on a narrow layout, and its logo is a network-fetched
 *  <img> with `alt=""` + `aria-hidden` — so a 404 (asset missing on a proxied
 *  serving path), a blocked request, or a hung fetch used to render NOTHING:
 *  an invisible button that still toggled the rail when tapped blind. The
 *  hamburger therefore shows by default and the swap happens on the img's
 *  `load` event, never on an assumption: `loadedSrc` records WHICH src loaded,
 *  so a branding/theme change falls back to the hamburger until the new asset
 *  proves itself, and an `error` clears the record. The img stays mounted
 *  (display:none) while hidden so the browser still fetches it. The hamburger
 *  sits in a w-6 box matching the img, keeping the 40px tap target and the
 *  16px page-gutter alignment identical through the swap. */
export function MobileNavGlyph({ avatar }: { avatar: string }) {
  const [loadedSrc, setLoadedSrc] = useState<string | null>(null)
  const showLogo = !!avatar && loadedSrc === avatar
  return (
    <>
      {!showLogo && (
        <span data-testid="mobile-nav-fallback" className="w-6 h-6 flex items-center justify-center shrink-0" aria-hidden="true">
          <Menu size={20} />
        </span>
      )}
      {!!avatar && (
        <img src={avatar} alt="" aria-hidden="true" onLoad={() => setLoadedSrc(avatar)} onError={() => setLoadedSrc(null)} className={`w-6 h-6 rounded-md shrink-0 object-contain transition-transform duration-300 group-hover:rotate-[-8deg] ${showLogo ? '' : 'hidden'}`} />
      )}
    </>
  )
}

/**
 * Topbar Notifications bell. The Notifications surface is `hiddenFromNav`, so
 * this is its entry point. Click opens an Activity Feed popover
 * (portaled to the body-level overlay root to escape the topbar's
 * backdrop-filter containing block); clicking an item slides out a detail panel. The full page is
 * preserved at /notifications via the popover's "Open inbox" link.
 */
function NotificationsBellButton() {
  // The Notifications surface is `hiddenFromNav`, so this bell — not a rail row —
  // is the control Alt+N operates. Resolved through the same route-keyed helper
  // the rail uses, so the chord has exactly one derivation in the dashboard.
  const shortcut = useNavShortcutHint('/notifications')
  const sheet = useNotificationSheet()
  const { items, open, containerRef, bellRef } = sheet
  // Badge counts attention-worthy rows only (RFC Phase 3): passive and
  // muted-channel (silenced) rows are excluded, mirroring the backend's
  // _unread_count semantics.
  const unacked = items.filter(n => !n.acked && n.priority !== 'passive' && !n.silenced)

  // RFC Phase 4: mirror the unread count onto the desktop dock/taskbar badge.
  useEffect(() => {
    setNativeBadgeCount(unacked.length)
  }, [unacked.length])

  return (
    <div ref={containerRef} className="relative">
      <button
        className={`flex items-center justify-center w-7 h-7 rounded-md hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer shrink-0 relative ${open ? 'text-accent' : 'text-muted hover:text-text'}`}
        onClick={sheet.toggle}
        // Chord declared to assistive tech ONLY, deliberately not in the tooltip.
        // The render-time i18n gate scans `TEXT_ATTRS` (render-scan.mjs:293 --
        // title, aria-label, placeholder, alt, aria-placeholder) for Latin runs
        // under the en-XA pseudolocale, and its attribute branch (:499) has no
        // opaque escape: the `[data-i18n-opaque]` / `kbd` exemption applies to
        // ELEMENTS, so a keycap can be exempted in text but never inside an
        // attribute value. A chord appended here read as 220 untranslated-attribute
        // findings. `aria-keyshortcuts` is not in that list and is the standards
        // declaration anyway, so the non-visual route survives; the VISIBLE hint
        // stays a rail affordance, where it can be marked opaque.
        title={unacked.length > 0 ? i18nT('app.notification_count', { count: unacked.length }) : i18nT('app.notifications')}
        aria-label={i18nT('app.notifications')}
        {...uiLocation('shell.notifications', bellRef)}
        aria-keyshortcuts={shortcut?.ariaKeyshortcuts}
        aria-haspopup="dialog"
        aria-expanded={open}
      >
        <Bell size={15} />
        {unacked.length > 0 && (
          <span className="absolute -top-1 -right-1 min-w-[16px] h-[16px] px-1 rounded-full bg-accent text-accent-fg text-[10px] font-bold flex items-center justify-center shadow-[0_0_2px_var(--accent-glow)]" aria-hidden="true">
            {unacked.length > 99 ? '99+' : unacked.length}
          </span>
        )}
      </button>
      {/* The sheet's scope owner, outside the sheet's own open conditional. */}
      <GuideRevealScope id="menu:shell.notifications" open={open}>
        <NotificationSheet sheet={sheet} />
      </GuideRevealScope>
    </div>
  )
}

/**
 * The top bar: the left cluster (window menu, history arrows, the phone menu
 * button, the instance selector), the centre search and focus-mode toggle, the
 * readout capsule and the right cluster, and the phone chat page's single bar.
 * Markup only: it calls no hook, and every value it reads comes from
 * {@link useAppShell}.
 */
export function ShellHeader({ shell }: { shell: AppShell }) {
  const {
    connected, updateAvailable, authRequired, gatewayStatusMsg, focusModeChord, focusModeExitMsgVisible, focusChromeInert, dispatch, colorTheme, resolvedMode, branding, avatar, isMobile, focusMode,
    toggleFocusMode, focusActive, topPeek, topPeekSurface, topChromeShown, slotOwners, commandPalette,
    setKiroUsageOpen, kiroUsageState, usageSegmentHidden, metrics, capsuleCollapsed, setCapsuleCollapsed,
    capsuleLayoutPulse, pulseCapsuleLayout, sysMetrics, metricsProbeRef, metricsGroupRef, setReportProblemOpen,
    requestFeature, toggleNav, mobileSingle, membersRoute, membersBarPending, mobileNavOpen,
  } = shell
  return (
    <>
      {/* Topbar */}
      {/* stable theming hook — see website/docs/theming-contract.md */}
      <header
        ref={topPeekSurface}
        // `topbar-single` (phone chat page) swaps the three-track grid for
        // `auto minmax(0,1fr) auto` (index.css): the centre cell is the chat
        // page's title slot and takes every pixel the two side cells leave.
        // Those side cells are a plain flex div / `tb-trail`, NOT `tb-left` /
        // `tb-right`: the latter are inline-size containers, and a size
        // container in an `auto` track has no content size to give, so it
        // collapses to its padding and clips whatever it holds
        // (test/topbarMenuButtonNarrow.test.ts records the measurement).
        //
        // `tb-measured` is the desktop form: both forms keep each side group's
        // container-query ladder, and desktop adds a measured ladder that folds
        // more when those rungs still leave contents overflowing
        // (useTopbarCollapse in useAppShell). Desktop only, on purpose: below 768px the
        // phone header's icon-only search and container rungs already fit.
        className={`topbar topbar-glass relative pl-2 pr-3${mobileSingle ? ' topbar-single' : ''}${isMobile ? '' : ' tb-measured'}`}
        // On a phone the chat title is portaled into this bar, above the
        // transcript: inert while a transcript selection is held, so a dragged
        // handle cannot land on the title (useSelectionInertOverlays).
        {...{ [SELECTION_INERT_ATTR]: '' }}
        // Both z-indexes come from lib/themeDecorLayer.ts, which derives the
        // theme-overlay ceiling from them — the header must outrank pack
        // decoration in both layouts (#7377), and a literal here could drift.
        //
        // In focus mode the header leaves the grid and becomes an overlay
        // positioned against the shell (which is already `relative`), NOT the
        // viewport: `position: fixed` would be measured against whichever
        // ancestor happens to establish a containing block, and the shell is the
        // app area either way. It stays MOUNTED and slides — unmounting it would
        // tear down the notification/metrics popovers it owns and lose their
        // state on every peek. TOPBAR_FOCUS_Z (62) clears the whole chat-pane
        // stack (max 61) and the rail (50) while staying under the update banner
        // (70), side sheets (89/90) and every modal (100+).
        style={focusActive
          ? {
            position: 'absolute',
            top: 0, left: 0, right: 0, height: 42,
            zIndex: TOPBAR_FOCUS_Z,
            transform: topChromeShown ? 'translateY(0)' : 'translateY(-100%)',
            transition: 'transform 200ms cubic-bezier(0.2, 0, 0, 1)',
            // Hidden chrome must not eat clicks aimed at the content beneath it.
            pointerEvents: topChromeShown ? 'auto' : 'none',
          }
          : { gridArea: 'topbar', zIndex: TOPBAR_Z }}
        // ...nor stay a Tab stop or be announced while off screen (hiddenChromeProps).
        {...hiddenChromeProps(focusActive && !topChromeShown && focusChromeInert)}
        {...(focusActive ? topPeek.surfaceProps : {})}
      >
        {/* Left: mobile menu toggle + inline instance selector. The brand now
            lives in the sidebar (item 1.1). The selector reuses InstanceTabBar's
            visibility rule — it renders nothing unless >=1 remote instance
            exists, so the common single-instance header-left is empty (only the
            macOS traffic-light clearance remains). */}
        {/* No mobile-only `px-2` here on purpose. The icon buttons inside carry
            their own 8px, so this padding stacked on top of the header's `pl-2`
            and pushed the hamburger out past the page's own left edge. Dropping
            it lands the button's BOX at 8 + 8 = 16px, the page gutter; the glyph
            inside it then needs its own 2.5px correction because `Menu`'s artwork
            does not fill its box (see the button below). Box and glyph together
            put the hamburger, the page title and the chat session-list toggle on
            one line. Deliberately only the LEFT cluster:
            `.tb-right` carries a padding/negative-margin pair that keeps the
            notification badge's 4px overhang from being clipped, and re-tuning
            that needs a real WebKit check, not a local one. */}
        {!mobileSingle && (
        <div className="tb-left relative h-full">
          <div className="tb-measure">
          {/* Windows only: the application menu shares this cluster. It needs no
              width reservation of its own: the identity group is sized by its own
              grid track, and the menu growing from the hamburger to its six
              labels therefore consumes the GROUP's width -- which its collapse
              ladder responds to -- instead of eating the centred search's. */}
          {!isMobile && isWinElectron && <WindowsTitlebarMenu />}

          {/* Route-history Back/Forward (#8258). Desktop layout only: on mobile
              the platform owns Back (left-edge swipe), and the drill-in surfaces
              navigate by component state that pushes nothing, so arrows there
              would walk an unrelated stack. Order: after the Windows app menu,
              before the instance selector — the leftmost NAVIGATION control,
              matching where every browser puts it. */}
          {!isMobile && <NavHistoryArrows />}
          {isMobile && (
            <button className="group p-2 rounded-md bg-transparent border-none cursor-pointer text-muted hover:text-text shrink-0" onClick={toggleNav} aria-label={i18nT('app.open_menu')} {...uiLocation('shell.mobile-menu')}>
              {/* The product logo, not a generic menu glyph. A narrow layout has exactly
                  one nav affordance, and it opens the same rail whose header carries this
                  same `avatar` on a wide one -- so it is the same asset, the same
                  `rounded-md object-contain` treatment and the same hover tilt, which is
                  live here because this bar is what a NARROW WINDOW gets, not only a
                  touch device. Reading `avatar` rather than importing a file is what
                  keeps a theme-supplied or user-configured logo in step: the branding
                  registry resolves it once for the whole shell.

                  A full-colour raster mark is an <img>, which is exactly what the
                  `use-lucide-icons` rule's brand-mark exception prescribes -- a CSS mask
                  over `currentColor` would flatten the art to one colour. But an <img>
                  can FAIL, and `alt=""` + `aria-hidden` means failure renders nothing --
                  an invisible button as the page's only nav route -- so MobileNavGlyph
                  holds the Menu hamburger up until the logo's own `load` event.

                  Square box, so no optical correction exists: the art is square and
                  `object-contain` fills the box, putting the ink on the 16px page gutter
                  (topbar pl-2 + this button's p-2) that the page title and every card's
                  left edge below it sit on, with the button's own box at 24 + 16 = 40px
                  for the tap target. `narrowFirstBaseline.test.ts` re-derives that sum. */}
              <MobileNavGlyph avatar={avatar} />
            </button>
          )}
          <InstanceTabBar variant="inline" />
          </div>
        </div>
        )}
        {/* Phone chat page, leading cell: the crew switcher (renders nothing
            until a remote crew exists) and downstream widgets while they exist.
            The update pill is NOT here: with a remote crew the switcher already
            renders its chip and its dropdown, and the pill made a third action
            in the group — on this page the update is the first item of the
            chat page's overflow menu instead (UpdatePill variant="menu-item").
            The nav-drawer logo is not here either — on this page the main
            destinations are the rail inside the sessions drawer, opened by the
            toggle the chat page puts first in the centre slot, so the bar never
            offers two drawers. Sized `auto`, so the common empty cell costs no
            width and the sessions toggle stays on the gutter. */}
        {mobileSingle && (
          <div data-testid="topbar-lead" className="relative h-full flex items-center gap-1.5 min-w-0">
            <InstanceTabBar variant="inline" />
            {getTopBarWidgets().map(w => (
              <ErrorBoundary key={w.id} scope={`topbar-widget:${w.id}`} fallback={null}>
                <w.component />
              </ErrorBoundary>
            ))}
          </div>
        )}
        {/* Centre track: the ⌘K trigger. A flow item, not an overlay — its width
            is the track's width, so it can never sit under a sibling cluster and
            never has to be dropped to stay clear of one. On mobile the same
            track holds the icon-only form below.

            Wrapped with the focus-mode toggle in ONE flex cell rather than added
            as a fourth grid child: `.topbar` declares exactly three tracks, so a
            bare sibling would be auto-placed into `.tb-right` and land inside the
            readout capsule's cluster. Two controls, which is the ceiling
            website/AUTOSDE.yaml's max-two-buttons-per-row sets.

            `data-topbar-overlay` is read by the crew-pin capture harnesses
            (website/scripts/capture-crew-pin-chips.mjs, record-crew-pin-chips.mjs,
            capture-crew-chip-shrink.mjs) to find this cell; nothing in src/
            reads it any more. Keep it. */}
        {!isMobile && (
          <div data-topbar-overlay className="flex items-center gap-1.5 min-w-0">
          {/* The trigger IS a Liquid Glass pane (components/Glass.tsx, chip
              recipe) rendered as the button, the same material as the
              sidebar's search field and the composer dock: no border and no
              fill of its own, `glass-hover` for the hover step. */}
          <Glass
            as="button"
            type="button"
            variant="chip"
            radius={TOPBAR_PILL_RADIUS}
            onClick={commandPalette.openPalette}
            {...uiLocation('shell.search')}
            className="glass-shadow glass-hover h-7 flex-1 min-w-0 px-3 text-muted hover:text-text transition-colors flex items-center justify-center gap-2 cursor-pointer"
            /* The trigger has to describe the surface it actually opens. While an app
               owns the quick-search slot the gesture opens a launcher -- typing runs
               commands and does not search the corpora this label promises -- so
               naming "search for anything" there is the most visible mispromise in
               the product. */
            aria-label={
              slotOwners['quick-search']
                ? i18nT('app.open_command_bar')
                : i18nT('app.search_sessions_files_and_commands')
            }
            // Gated on the same condition as the label and aria-label above. Leaving
            // this one unconditional made the hover contradict the words under the
            // cursor and promise the corpus search the launcher deliberately omits --
            // the diff's own fix applied to two of three attributes. The owned branch
            // drops "(K)" because the chord is already printed in the visible label.
            title={
              slotOwners['quick-search']
                ? i18nT('app.open_command_bar')
                : i18nT('app.search_everywhere_k')
            }
          >
            <span className="text-[13px] truncate min-w-0">
              {slotOwners['quick-search']
                ? i18nT('app.k_run_a_command')
                : i18nT('app.k_search_for_anything')}
            </span>
          </Glass>
          {/* Focus mode. `aria-pressed` rather than a second label, so a screen
              reader gets the state from the control instead of from copy that
              would have to be kept in step with the icon. */}
          <button
            type="button"
            data-testid="focus-mode-toggle"
            // A sighted keyboard user who activates this with Enter/Space is the
            // one person the inert chrome leaves stranded: the toggle they just
            // pressed vanishes, Tab cannot bring it back, and the chord that can
            // lived only in the hover title. So on a keyboard activation that
            // turns focus mode ON, the shared shell notice (the agent-switch card)
            // shows the same sentence the live region speaks, in display
            // spelling. `detail === 0` is how a keyboard-synthesised click differs
            // from a pointer click (which counts its clicks). A pointer user can
            // hover an edge; a chord user just typed the chord; neither gets the
            // card. The sr-only region is the spoken carrier either way.
            onClick={e => {
              const turningOn = !focusMode
              toggleFocusMode()
              if (turningOn && e.detail === 0 && focusModeExitMsgVisible) dispatch(setAgentSwitchNotice(focusModeExitMsgVisible))
            }}
            className={`flex items-center justify-center w-7 h-7 rounded-md hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer shrink-0 ${focusMode ? 'text-accent' : 'text-muted hover:text-text'}`}
            aria-label={i18nT('app.focus_mode')}
            aria-pressed={focusMode}
            // The chord as currently bound (focusModeChord): the `title` below
            // names the default for sighted hover, this names the live one for
            // assistive tech. Omitted when the user unbound it.
            aria-keyshortcuts={focusModeChord ?? undefined}
            {...uiLocation('shell.focus-mode')}
            title={i18nT(IS_MAC ? 'app.focus_mode_title_mac' : 'app.focus_mode_title')}
          >
            <Fullscreen size={15} />
          </button>
          </div>
        )}
        {/* Mobile centre cell. On the chat page it is the slot the chat page
            fills through a portal: [sessions toggle][session title + menu].
            `min-w-0` + `flex` so the title inside can truncate instead of
            growing the cell. Elsewhere the cell is an empty spacer: the search
            trigger that used to sit here moved into the chat drawer's rail, and
            the header still needs its third in-flow child so the actions group
            is not auto-placed into the `auto` centre track (where, as a size
            container, it would collapse to its padding). */}
        {/* The Crewmates page puts its header here once a crewmate is open.
            Until then (`pending`, still reading) the page has no drawer bound,
            so the slot holds a nav-drawer toggle and the bar never sits empty.
            It wears the logo row's `Menu` glyph, not the roster drawer's panel
            glyph, so one icon never opens two different drawers. */}
        {isMobile && (mobileSingle
          ? (
            <div id="mobile-topbar-slot" data-testid="mobile-topbar-slot" className="flex items-center gap-1 min-w-0 h-full">
              {membersBarPending && (
                <button
                  type="button"
                  className="mc-touch-hit p-2 rounded-md text-text hover:text-text-strong hover:bg-bg-hover cursor-pointer bg-transparent border-none shrink-0"
                  onClick={toggleNav}
                  aria-label={i18nT('app.open_menu')}
                  aria-expanded={mobileNavOpen}
                  data-testid="mobile-topbar-nav-toggle"
                >
                  <Menu size={18} />
                </button>
              )}
            </div>
          )
          : <span aria-hidden="true" data-testid="topbar-centre-spacer" className="w-0" />
        )}
        {/* Theme decoration: the active theme's center top-bar element (e.g. a
            scanner sweep), chosen by resolved mode. Absent unless a registered
            theme declares one. It renders as a BACKGROUND layer rather than a
            grid cell: the header's three tracks are load-bearing now (sides are
            pure remainder), so a fourth flow item would land in an implicit
            column and shift the search off centre. A sweep/scanline is visually
            a backdrop anyway, so it is inert to pointers and sits behind the
            controls. Wrapped in a slot-level ErrorBoundary (fallback=null) so a
            faulty registered extension disables only itself instead of crashing
            the whole shell via the root boundary. */}
        {(() => {
          if (branding?.topBarHideOnMobile && isMobile) return null
          const TB = resolvedMode === 'light' ? branding?.topBar?.light : branding?.topBar?.dark
          return TB ? (
            <ErrorBoundary key={`${colorTheme}:${resolvedMode}`} scope="theme-topbar" fallback={null}>
              <div className="absolute inset-0 pointer-events-none overflow-hidden" aria-hidden="true"><TB /></div>
            </ErrorBoundary>
          ) : null
        })()}
        {/* `tb-has-update` shifts the collapse ladder's rungs (index.css): the
            update pill is a conditional, non-shrinking sibling of the ladder,
            so while it is mounted the group's fixed content is wider by the
            pill's footprint and the ≥640px rungs fire that much earlier. Below
            640px no rung shifts (#7698): a phone hands the group ≤240px
            routinely, so a shifted terminal rung blanked the readouts for the
            whole time an update was pending; the nowrap backstop clips the
            squeeze instead. The class keys off the same selector the pill
            itself reads, so they move together; during the pill's lazy-chunk
            fetch the class can lead the pill by a moment, which costs readout
            room briefly and harms nothing. */}
        {!mobileSingle && (
        <div ref={metricsGroupRef} className={`tb-right relative${updateAvailable ? ' tb-has-update' : ''}`}>
          <div className="tb-measure">
          {/* Zero-footprint probe for the metrics rung. It carries the readings'
              own class, so JS reads the LADDER's verdict rather than a copy of
              its thresholds. Out of flow and 0x0, so it costs no ladder budget
              and adds no flex gap. */}
          <span ref={metricsProbeRef} className="tb-drop-metrics tb-metrics-probe" aria-hidden="true" />

          {/* Theme decoration: extra aside control (e.g. a stardate / clock). */}
          {branding?.topBarAside && !(branding?.topBarHideOnMobile && isMobile) && (
            <ErrorBoundary key={`${colorTheme}:${resolvedMode}`} scope="theme-aside" fallback={null}>
              <branding.topBarAside />
            </ErrorBoundary>
          )}
          {/* Unified readout capsule — connection glyph . system metrics .
              kiro-credits usage pooled into one bordered pill. Offline: the
              glyph turns from a plug into an unplugged plug and the whole
              capsule tints danger (red border + subtle red bg), no "Offline"
              text — the shape change is the signal, the colour backs it. When
              auth expired the session-expired banner stays the primary signal;
              the capsule reddens quietly underneath it. (The upstream
              enterprise-SSO segment is dropped here: that SSO flow is stubbed
              in this fork. The Claude-cost usage branch is likewise dropped:
              this fork's usage pill is Kiro-credits-only.)

              Desktop only. A phone bar has room for two controls on the right
              (bell + the chat page's overflow menu) and a resource readout is
              not something a phone user acts on; the session-expired banner
              stays the offline signal there. */}
          {!isMobile && (() => {
            const offline = !connected
            // `gatewayStatusMsg` (useAppShell) is the announced cause; see its
            // comment for why auth wording wins over transport state. It mirrors
            // the branch the button `title` already uses (minus the
            // collapse-toggle suffix, which is interaction text, not a status
            // cause).
            // The connection dot doubles as the capsule collapse toggle, so its
            // accessible name must name that action -- not just the gateway
            // state. title and aria-label share this composed value; the
            // role="status" live region stays pure gatewayStatusMsg (a status
            // region announces the connection cause, not the button's toggle
            // affordance, which would speak "click to collapse" on every
            // reconnect).
            const capsuleActionMsg = `${gatewayStatusMsg} · ${capsuleCollapsed ? i18nT('app.click_to_expand_readouts') : i18nT('app.click_to_collapse_readouts')}`
            // whitespace-nowrap is the ladder's backstop for the BUILT-IN
            // segments that share this class string: if the group is ever
            // narrower than its contents (a locale wider than the measured
            // budget, the dev-only pseudolocale), a squeezed segment must clip
            // at the edge, never wrap into two lines the capsule's fixed h-7
            // then crops. Extension segments bring their own class strings and
            // are bounded by the capsule's terminal rung instead.
            const seg = `flex items-center gap-1 -my-0.5 px-1.5 py-0.5 rounded-md bg-transparent border-none cursor-pointer transition-colors hover:bg-bg-hover whitespace-nowrap ${offline ? 'opacity-70' : ''}`
            const segments: ReactNode[] = []
            // The dot doubles as the capsule's collapse toggle: click to
            // fold the readouts down to just the dot, click again to expand.
            // Padding + negative margin keep a usable hit target without
            // growing the visual dot.
            segments.push(
              <button
                key="conn"
                className="flex items-center justify-center p-1.5 -m-1.5 rounded-full bg-transparent border-none cursor-pointer shrink-0"
                onClick={() => { pulseCapsuleLayout(); setCapsuleCollapsed(c => !c) }}
                title={capsuleActionMsg}
                aria-label={capsuleActionMsg}
                aria-expanded={!capsuleCollapsed}
              >
                {/* The glyph's SHAPE carries the state (WCAG 1.4.1): a plug when
                    connected, an unplugged plug when offline. Colour is only
                    the second cue, so a red/green colour-blind reader still
                    sees the state without hovering. */}
                {offline
                  ? <Unplug aria-hidden="true" data-conn-state="offline" size={12} strokeWidth={2.25} className="text-danger transition-colors duration-300 animate-pulse [animation-iteration-count:3]! motion-reduce:animate-none" />
                  : <Plug aria-hidden="true" data-conn-state="connected" size={12} strokeWidth={2.25} className="text-ok transition-colors duration-300" />}
                {/* Live-region announcement lives in its own hidden span:
                    role="status" on the button itself would override its
                    implicit button role for screen readers. */}
                <span role="status" className="sr-only">{gatewayStatusMsg}</span>
              </button>
            )
            // Resource pressure indicator — always visible when tight/critical
            if (sysMetrics?.posture && sysMetrics.posture !== 'ample' && sysMetrics.posture !== 'unknown') {
              segments.push(
                <span
                  key="resource-health"
                  className={`${seg} flex items-center gap-1 text-[11px] ${sysMetrics.posture === 'critical' ? 'text-danger' : 'text-warn'}`}
                  title={sysMetrics.posture === 'critical'
                    ? i18nT('app.resource_posture_tooltip_critical', { gb: sysMetrics.availableGb?.toFixed(1) ?? '?' })
                    : i18nT('app.resource_posture_tooltip_tight', { gb: sysMetrics.availableGb?.toFixed(1) ?? '?' })}
                >
                  <span aria-hidden="true" className={`inline-block w-2 h-2 rounded-full ${sysMetrics.posture === 'critical' ? 'bg-danger animate-pulse [animation-iteration-count:3]! motion-reduce:animate-none' : 'bg-warn'}`} />
                  {!isMobile && <span className="font-medium">{sysMetrics.posture === 'critical' ? i18nT('app.resource_critical') : i18nT('app.resource_tight')}</span>}
                  {!isMobile && sysMetrics.subagentCap != null && <span className="text-muted text-[10px]">· {i18nT('app.subagent_cap', { cap: String(sysMetrics.subagentCap) })}</span>}
                </span>
              )
            }
            if (!capsuleCollapsed) {
            if (!isMobile) segments.push(metricsSegment(metrics, seg))
            const usage = kiroUsageSegment({ kiroUsageState, usageSegmentHidden, setKiroUsageOpen }, seg, isMobile)
            if (usage) segments.push(usage)
            }
            // Extension slot: downstream-registered capsule segments (e.g. an
            // edition credential-TTL or spend segment) join the capsule INSIDE
            // its border/dividers/offline-tint, after the core segments, in
            // `order`. Each is isolated in its own ErrorBoundary (fallback=null)
            // so a throwing segment disables only itself. Empty in stock build.
            // Gated on !capsuleCollapsed exactly like the core readouts, so
            // collapsing reduces the capsule to the bare connection dot rather
            // than leaving extension segments + their dividers visible.
            if (!capsuleCollapsed) {
              for (const cs of getCapsuleSegments()) {
                if (cs.hideOnMobile && isMobile) continue
                const SegComp = cs.component
                segments.push(
                  <ErrorBoundary key={cs.id} scope={`capsule-segment:${cs.id}`} fallback={null}>
                    <SegComp offline={offline} />
                  </ErrorBoundary>
                )
              }
            }
            return (
              /* layout + tween (not spring: springs bounced in a prior
                 attempt) animates the capsule's width as segments mount and
                 unmount on collapse/expand. The layout transition is gated to
                 a pulse: 0.25s right after an intentional collapse/expand
                 click, else 0s so header reflows (panel open/close, resize)
                 snap the capsule into place instead of sliding it. */
              <motion.div
                layout
                transition={{ layout: { duration: capsuleLayoutPulse ? 0.25 : 0, ease: 'easeOut' } }}
                className="flex items-center shrink-0"
              >
                {/* The capsule IS a Liquid Glass pane (components/Glass.tsx,
                    chip recipe) hosting the segments directly, so the
                    `.tb-capsule > …` rungs in index.css still see them as its
                    children; the motion wrapper outside only animates width.
                    Offline is a tint step (`glass-danger`), never a fill. */}
                <Glass
                  variant="chip"
                  radius={TOPBAR_PILL_RADIUS}
                  className={`tb-capsule glass-shadow flex items-center gap-2 h-7 px-2.5 ${offline ? 'glass-danger' : ''}`}
                >
                  {segments.flatMap((s, i) => (i === 0 ? [s] : [<span key={`sep-${i}`} className="w-px h-3.5 bg-border shrink-0" aria-hidden="true" />, s]))}
                </Glass>
              </motion.div>
            )
          })()}
          {!isMobile && <MetricsErrorNotice metrics={metrics} />}
          {/* Extension slot: downstream-registered top-bar widgets (e.g. a
              credential-TTL capsule or spend pill). Empty in the stock build.
              Each widget is isolated in its own ErrorBoundary (fallback=null) so
              a throwing widget disables only itself, not the shell or its
              sibling widgets. */}
          {getTopBarWidgets().map(w => (
            <ErrorBoundary key={w.id} scope={`topbar-widget:${w.id}`} fallback={null}>
              <w.component />
            </ErrorBoundary>
          ))}
          {/* Update pill — present only while an update exists; deep-links to
              Settings › About. NOT gated on viewport: it is the download's
              only progress home, and hiding it on narrow windows would make
              "Download" consent produce zero visible feedback until the
              staged-build modal fires minutes later. */}
          {updateAvailable && (
            <Suspense fallback={null}>
              <UpdatePill />
            </Suspense>
          )}
          {/* Feedback — "Request a Feature" plus, on a prerelease build, a
              channel chip that opens the same Report a Problem flow. Its own
              bordered pill (28px tall, 12px radius), separated from the readout
              capsule (item 2.3). */}
          {!isMobile && (
            <span className="tb-drop-feedback flex items-center">
              <FeedbackPill
                onRequestFeature={requestFeature}
                onReportProblem={() => setReportProblemOpen(true)}
              />
            </span>
          )}
          {/* Notifications bell — borderless icon button, rightmost control.
              (The activity-panel open toggle now lives in the session header,
              beside the pop-out control — see ChatPage — so opening the panel
              no longer narrows this full-width header.) */}
          <NotificationsBellButton />
          </div>
        </div>
        )}
        {/* Phone chat page, trailing cell: EXACTLY two controls (the
            max-two-buttons-per-row rule) — the bell, then the chat page's
            overflow menu, portaled into `#mobile-topbar-trail-slot`. The
            update pill is NOT here: with an update pending it made this a
            three-control group. On this page the update is the first item of
            that overflow menu (UpdatePill variant="menu-item"), carrying the
            same lifecycle label as the pill; downstream widgets sit in the
            leading cell. */}
        {mobileSingle && (
          <div className="tb-trail relative h-full flex items-center gap-1.5 shrink-0">
            {/* The Crewmates page has no overflow menu to carry the update,
                and this cell holds only the bell there, so the pill itself
                goes first: still two controls. It keeps its text on the
                phone, since a bare download glyph does not say "update". */}
            {membersRoute && updateAvailable && (
              <Suspense fallback={null}>
                <UpdatePill showLabel />
              </Suspense>
            )}
            <NotificationsBellButton />
            <div id="mobile-topbar-trail-slot" data-testid="mobile-topbar-trail-slot" className="flex items-center empty:hidden" />
          </div>
        )}
        {/* Phone: the generic offline signal. The readout capsule (whose dot
            turned red on a transport drop) is not rendered below 768px, so a
            phone would otherwise show nothing while the socket is down. One
            strip hanging off the bar, driven by the same `connected` state the
            capsule read; auth expiry keeps its own banner (api/client.ts), so
            this stays quiet then rather than saying "reconnecting" over a fix
            that is pasting a token. Absolute (out of the bar's grid flow) so
            the three-track invariant holds. */}
        {isMobile && !connected && !authRequired && (
          <div
            role="status"
            data-testid="mobile-offline-strip"
            // `pointer-events-none`: the strip hangs over the top 24px of
            // whatever <main> paints; a readout must not swallow the taps and
            // scroll starts that land there while the socket is down.
            className="absolute left-0 right-0 top-full z-[1] pointer-events-none flex items-center justify-center gap-1.5 h-6 text-[12px] font-medium text-danger-text bg-danger-subtle border-b border-danger/30"
          >
            <span aria-hidden="true" className="w-1.5 h-1.5 rounded-full bg-danger animate-pulse [animation-iteration-count:3]! motion-reduce:animate-none" />
            {i18nT('app.gateway_offline_reconnecting')}
          </div>
        )}
        {/* Session expired on the phone: the auth banner (api/client.ts) is the
            visible message and has no live region, and the capsule's sr-only
            carrier is not rendered below 768px -- so this is the one
            announcement a screen-reader user gets. Nothing visible: the banner
            already says it. */}
        {isMobile && !connected && authRequired && (
          <span role="status" data-testid="mobile-offline-sr" className="sr-only">{i18nT('app.gateway_offline_session_expired_see_banner_above')}</span>
        )}
      </header>
    </>
  )
}

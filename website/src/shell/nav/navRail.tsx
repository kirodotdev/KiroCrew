import { surfaceLabel } from '../../surfaces/registry'
import { SortableAppNavRow, NavToggle } from './appRail'
import { AppRailMenu } from './appRailMenu'
import { railWidthFor } from '../../hooks/useRailWidth'
import { FOCUS_INSET } from '../../hooks/useFocusMode'
import { Coins, Compass, LayoutGrid, SquareTerminal, Smartphone, Search as SearchIcon } from 'lucide-react'
import { motion } from 'framer-motion'
import { DndContext, closestCenter, DragOverlay } from '@dnd-kit/core'
import { SortableContext, verticalListSortingStrategy } from '@dnd-kit/sortable'
import { toggleBottomTerminal } from '../../hooks/useBottomTerminal'
import { focusPopout as focusTerminalPopout } from '../../utils/terminalPopout'
import { i18nT } from '../../i18n/t'
import { uiLocation } from '../../uiLocations/uiLocation'
import { isAppNavId } from '../../appNotificationBadges'
import { NAV_ITEMS } from './navItems'
import { isChatRoute } from './routeActive'
import { RailBrandToggle, RailCommunityLinks } from './railChrome'
import { appPathname } from '../../lib/basePath'
import { MOBILE_NAV_WIDTH, type AppShell } from '../appShell'
import { NavItem, NavBadge } from './navRow'
import { AppRowPopoutAction } from './appRowPopoutAction'
import { DockMatesSection, useDockMates } from './matesDock'
import { hiddenChromeProps } from '../focus/focusChrome'
import { coversPaneProps } from '../../hooks/paneVisibility'
import { useScrollEdgesY } from '../../hooks/useScrollEdges'
import { MORE_BELOW_MASK } from '../../components/ModelEffortDropdown'
import { useLayoutEffect } from 'react'

/**
 * The main navigation: the desktop rail, and the phone nav drawer with its scrim,
 * which share one body. Mostly markup: apart from the docked mates and the app
 * list's scroll-edge cue, every value it reads comes from {@link useAppShell}.
 */
export function ShellNavRail({ shell }: { shell: AppShell }) {
  const {
    settingsDotLabel, terminalEnabled, terminalPoppedOut, bottomTerminalOpen, mobileConnectOpen,
    setMobileConnectOpen, hasRenderableMobileConnect, activeSlotProject, navigate, branding, botName, avatar,
    isMobile, focusActive, focusChromeInert, railPeek, railPeekSurface, mobileNavMounted, mobileNavOpen, mobileNavX, mobileNavPanelRef,
    mobileNavScrimRef, mobileNavScrim, closeMobileNavDrawer, advertisedNavItems, sortedAppGroup,
    activeAppDragId, appDndSensors, handleAppDragStart, handleAppDragEnd, handleAppDragCancel, APPS_NAV_LIMIT,
    appsExpanded, toggleAppsExpanded, slotOwners, appBadges, discoverBadges, railAppBadges, railAppRunStates,
    commandPalette, kiroUsageOpen, setKiroUsageOpen, kiroAccountEntry,
    setReportProblemOpen, toggleNav, effectiveCollapsed, closeMobileNav, activePath, libraryNavActive,
    discoverNavActive, isChat, navRowActive,
  } = shell
  // A docked mate's row is the lit one while its chat is open; lighting the
  // Mates page row too would mark two rows as "you are here".
  const { openIsDocked, ordered: dockedMates } = useDockMates()
  // With mates docked, the app list shrinks first and can end at its first
  // row with the rest scrolled out of sight under a hidden scrollbar, so it
  // fades its bottom edge while anything is below the fold, as the Mates group
  // does. Only while mates are docked: with none, the scroller is not attached
  // and the list renders exactly as it did before the Mates group existed.
  // Rows changing inside an already-clipped box resize nothing the observer
  // watches, hence the explicit remeasure.
  const [attachAppScroller, appEdges, remeasureApps] = useScrollEdgesY<HTMLDivElement>()
  useLayoutEffect(() => { remeasureApps() }, [remeasureApps, sortedAppGroup.length, appsExpanded, effectiveCollapsed, activePath, dockedMates.length])

  // Render one standard nav row (used by the top-fixed mains, the Apps list,
  // and the bottom-fixed section). Active-state, mobile close, chat pin
  // toggle, and badge wiring are identical across sections.
  // `surfaceLabel` resolves `labelKey` against the active language at render
  // time; a surface with no key (app-contributed) falls back to its literal.

  const renderNavRow = (
    n: { path: string; id: string; label: string; labelKey?: string; icon: React.ReactNode; appName?: string },
  ) => (
    <NavItem
      navId={n.id}
      path={n.path}
      label={surfaceLabel(n)}
      icon={n.icon}
      active={navRowActive(n.path) && !(n.id === 'members' && openIsDocked)}
      collapsed={effectiveCollapsed}
      onClick={closeMobileNav}
      // The chat row, while chat is the page on screen, toggles the sessions
      // sidebar instead of navigating. Whether chat is STILL where the app is
      // going is read from the address bar at click time, not from the rendered
      // route: react-router wraps every location update in startTransition, so
      // after a click on another row history has already moved while this rail
      // keeps rendering chat until that render commits -- hundreds of ms on a
      // loaded machine. A click on the chat row inside that window means "come
      // back", and reading the rendered route there turned it into a pin toggle
      // that navigated nowhere. `appPathname()` strips the build's base so the
      // test matches the router's own base-less pathname. No leave ask on that
      // path: the page on screen is the one being returned to, and it was asked
      // when it was left.
      onClickOverride={isChat && (activePath === n.path || activePath.startsWith(n.path + '/'))
        ? () => {
          if (isChatRoute(appPathname())) window.dispatchEvent(new Event('toggle-pin-chat-sidebar'))
          else navigate(n.path)
        }
        : undefined}
      badge={<NavBadge navId={n.id} collapsed={effectiveCollapsed} appBadges={isAppNavId(n.id) ? railAppBadges : appBadges} runState={n.appName ? railAppRunStates[n.appName] : undefined} />}
    />
  )

  // An Apps-group row with its pop-out action beside it. The action is a
  // sibling of the row, never a child (the row is itself a `role="button"`),
  // and the row's trailing padding widens so its chord and badges sit clear of
  // the action. Expanded desktop rail only: the collapsed rail's icon rows and
  // the phone drawer carry no pop-out entry point. The DragOverlay ghost uses
  // `renderNavRow` directly, so the floating copy carries no action either.
  const renderAppRow = (n: Parameters<typeof renderNavRow>[0]) => (
    !effectiveCollapsed && !isMobile ? (
      <div className="relative group/approw [&>.nav-item]:pr-9">
        {renderNavRow(n)}
        <AppRowPopoutAction route={n.path} label={surfaceLabel(n)} active={navRowActive(n.path)} />
      </div>
    ) : renderNavRow(n)
  )

  return (
    <>
      {/* Mobile backdrop — opacity is animated by animateDrawer in lockstep
          with the panel (compositor), so there is no framer fade here; it
          mounts at 0 and the slide carries it. Mounted for the whole phase so
          the slide-out fade is not cut short.
          aria-hidden: the scrim is decorative — its click-to-dismiss is a
          pointer convenience, and keyboard users dismiss via Escape (handled
          where the drawer state lives). A focusable full-screen scrim would
          add a giant tab stop over the whole page, which is why this is NOT
          the Clickable component. */}
      {isMobile && mobileNavMounted && (
        <motion.div
          ref={mobileNavScrimRef}
          data-testid="nav-backdrop"
          aria-hidden="true"
          style={{ opacity: mobileNavScrim }}
          className="fixed inset-0 z-[46] bg-black/50 backdrop-blur-xs"
          onClick={closeMobileNavDrawer}
        />
      )}

      {/* Nav */}
      {/* Desktop rail and mobile drawer share one body but get DIFFERENT
          wrappers, and only the mobile drawer sits inside AnimatePresence.
          An exit animation on the desktop rail is actively wrong: when the
          viewport crosses the mobile threshold, the shell grid drops its
          `nav` area in the same render — AnimatePresence would keep the
          exiting rail mounted with its frozen `gridArea: 'nav'` style, and
          CSS auto-places that orphaned item into an implicit row BELOW the
          content (the rail visibly jumped under the chat input before
          sliding away). The desktop rail therefore unmounts instantly at
          the threshold; only the fixed-position drawer animates in/out. */}
      {(() => {
        const navBody = (<>
        {/* Top-fixed: menu row + primary destinations + Apps section header.
            The sidebar toggle lives HERE (menu row), not in the topbar. */}
        <div className="shrink-0 flex flex-col gap-0.5 px-2 pt-2">
          <RailBrandToggle effectiveCollapsed={effectiveCollapsed} toggleNav={toggleNav} avatar={avatar} branding={branding} botName={botName} />
          {/* Hairline under the expanded header (collapsed rail has none —
              the big logo alone separates well). */}
          {!effectiveCollapsed && <div aria-hidden="true" className="h-px bg-border shrink-0 mb-[7px]" />}
          {advertisedNavItems.filter(n => n.group === 'Main').map(n => <div key={n.id}>{renderNavRow(n)}</div>)}
        </div>
        {/* Starred mates sit between the fixed blocks but are not fixed: on a
            short window this group and the app list below share the room left
            over and each scrolls inside its share (see the app list's shrink
            weight below), so neither the apps nor the footer is pushed off. */}
        <DockMatesSection collapsed={effectiveCollapsed} isMobile={isMobile} onNavigate={closeMobileNav} />
        <div className="shrink-0 flex flex-col gap-0.5 px-2">
          {/* Apps section: the old single "Explore" header link split into two
              nav rows — Discover (the storefront, /apps) and Library
              (installed-app management, /apps/library). Expanded keeps the
              muted "Apps" section label above them; collapsed renders the two
              rows as regular icon rows like their neighbors. The unread-updates
              badge rides Discover (navId "apps"), matching where update
              discovery lives. NavItem carries data-onboarding-nav={navId}, so
              the onboarding anchor "apps" stays on the Discover row. */}
          {!effectiveCollapsed ? (
            <>
              <div className="nav-section flex items-center pl-3 pr-1 pt-3 pb-1">
                <span
                  // `overflow-hidden` + `whitespace-nowrap` means this clips
                  // silently once the label grows — which it does in a longer
                  // locale. The `title` keeps the full string reachable instead
                  // of losing the tail with no affordance.
                  title={i18nT('app.apps')}
                  className="text-[13px] font-medium text-muted whitespace-nowrap overflow-hidden"
                >{i18nT('app.apps')}</span>
              </div>
              <NavItem
                navId="apps"
                path="/apps"
                label={i18nT('nav.discover')}
                icon={<Compass size={20} />}
                active={discoverNavActive}
                collapsed={false}
                onClick={closeMobileNav}
                badge={<NavBadge navId="apps" collapsed={false} appBadges={discoverBadges} />}
              />
              <NavItem
                navId="apps-library"
                path="/apps/library"
                label={i18nT('nav.library')}
                icon={<LayoutGrid size={20} />}
                active={libraryNavActive}
                collapsed={false}
                onClick={closeMobileNav}
              />
            </>
          ) : (
            <motion.div
              className="mt-4"
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.2, ease: 'easeOut' }}
            >
              <NavItem
                navId="apps"
                path="/apps"
                label={i18nT('nav.discover')}
                icon={<Compass size={20} />}
                active={discoverNavActive}
                collapsed
                onClick={closeMobileNav}
                badge={<NavBadge navId="apps" collapsed appBadges={discoverBadges} />}
              />
              <NavItem
                navId="apps-library"
                path="/apps/library"
                label={i18nT('nav.library')}
                icon={<LayoutGrid size={20} />}
                active={libraryNavActive}
                collapsed
                onClick={closeMobileNav}
              />
            </motion.div>
          )}
        </div>

        {/* Apps list: scrolls in its OWN frame when many apps are enabled —
            the top (menu/mains/header) and bottom sections stay pinned.
            Collapsed hover labels are portaled to the body-level overlay root (see NavItem /
            NavToggle) so this vertical clip never chops them at the rail
            edge. overscroll-y-none kills the macOS rubber-band bounce;
            scrollbar-none + scrollbarWidth hide the scrollbar across
            Firefox, modern WebKit, and older Safari (<16). */}
        <div
          ref={dockedMates.length ? attachAppScroller : undefined}
          // No fade while an app is being dragged, as in the Mates group: the
          // drag ghost renders inside this box, and a mask would fade it.
          className={`flex-auto ${dockedMates.length ? 'min-h-[40px]' : 'min-h-0'} overflow-y-auto overflow-x-hidden overscroll-y-none scrollbar-none px-2${dockedMates.length && appEdges.bottom && activeAppDragId === null ? ` ${MORE_BELOW_MASK}` : ''}`}
          // With a Mates group above, this list gives up its room FIRST (the
          // far larger shrink weight), down to one row; only then does the
          // Mates group shrink, down to one row. Neither group, nor the
          // footer, leaves the rail.
          style={{ scrollbarWidth: 'none', flexShrink: dockedMates.length ? 1000 : undefined }}
        >
          <div className="grid gap-0.5">
            {(() => {
              const fullList = sortedAppGroup
              // Collapse a long Apps list behind a "N more" toggle (both expanded
              // and collapsed modes). Keep the active item visible even when it's
              // in the overflow, so navigation state is never hidden.
              const overflowing = !appsExpanded && fullList.length > APPS_NAV_LIMIT
              const visible = overflowing
                ? fullList.filter((n, i) => i < APPS_NAV_LIMIT || activePath === n.path || activePath.startsWith(n.path + '/'))
                : fullList
              const hiddenCount = fullList.length - visible.length
              // Apps rows are dnd-kit sortable. Rows reflow to open a gap as one
              // is dragged; the source dims and a DragOverlay renders the ghost.
              // SortableContext/DndContext add no DOM wrapper, so the parent grid
              // gap is unchanged.
              //
              // Overflow caveat: when collapsed behind "N more", the active app
              // may be PULLED IN from the overflow to keep its nav state visible
              // (`visible` keeps it past APPS_NAV_LIMIT). That pulled-in row must
              // NOT be sortable: handleAppDragEnd resolves from/to against the
              // FULL order, so dropping onto a row whose full-list index is
              // >= APPS_NAV_LIMIT would push the dragged app past the limit and
              // into the hidden overflow (it would disappear). Restrict the
              // sortable set to the always-visible window (first APPS_NAV_LIMIT)
              // and render any pulled-in overflow row as a plain static row —
              // still navigable, but it registers no droppable, so a drag can
              // never resolve to it and both endpoints stay in-window. (Trimming
              // only SortableContext.items is insufficient: useSortable registers
              // a droppable per wrapped row regardless of the items array.)
              const sortableRows = overflowing ? visible.slice(0, APPS_NAV_LIMIT) : visible
              const pulledInRows = overflowing ? visible.slice(APPS_NAV_LIMIT) : []
              const activeApp = activeAppDragId ? fullList.find(n => n.id === activeAppDragId) : null
              const dragActive = activeAppDragId !== null
              return (<>
              <DndContext sensors={appDndSensors} collisionDetection={closestCenter} onDragStart={handleAppDragStart} onDragEnd={handleAppDragEnd} onDragCancel={handleAppDragCancel}>
                <SortableContext items={sortableRows.map(n => n.id)} strategy={verticalListSortingStrategy}>
                  {sortableRows.map(n => (
                    <AppRailMenu key={n.id} row={n} dragActive={dragActive}>
                      <div role="presentation"><SortableAppNavRow id={n.id}>{renderAppRow(n)}</SortableAppNavRow></div>
                    </AppRailMenu>
                  ))}
                </SortableContext>
                {/* Pulled-in active overflow row(s): static, non-draggable. */}
                {pulledInRows.map(n => (
                  <AppRailMenu key={n.id} row={n} dragActive={dragActive}>
                    <div role="presentation">{renderAppRow(n)}</div>
                  </AppRailMenu>
                ))}
                {/* The ghost has no menu because it is a second rendering of the dragged row. */}
                <DragOverlay>{activeApp ? renderNavRow(activeApp) : null}</DragOverlay>
              </DndContext>
              {/* Show the toggle whenever the list is collapsible, NOT only when
               *  hiddenCount > 0 — otherwise navigating to an app that's the sole
               *  overflow item pulls it into `visible` (hiddenCount → 0) and the
               *  toggle vanishes, causing a jarring layout shift as you move
               *  between apps. The toggle stays put; only its label changes. */}
              {fullList.length > APPS_NAV_LIMIT && (
                <NavToggle
                  collapsed={effectiveCollapsed}
                  expanded={appsExpanded}
                  hiddenCount={hiddenCount}
                  onClick={toggleAppsExpanded}
                />
              )}
              </>)
            })()}
          </div>
        </div>

        {/* Bottom-fixed: Agent Capabilities, Settings, and the community row.
            Pinned to the rail's bottom edge — the Apps frame above absorbs
            the scroll. */}
        {(() => {
          const s = NAV_ITEMS.find(n => n.id === 'settings')!
          const cap = NAV_ITEMS.find(n => n.id === 'capabilities')!
          return (
            <div className="shrink-0 grid gap-0.5 px-2 pt-1 pb-2">
              {terminalEnabled && (
                <NavItem
                  path="#"
                  label={i18nT('app.terminal')}
                  icon={<SquareTerminal size={20} />}
                  /* This row TOGGLES the docked panel instead of navigating, so
                     "active" tracks the panel's open flag rather than the route.
                     Without it the row only lit on hover, leaving no indication
                     the panel below was open once the pointer moved away. */
                  active={bottomTerminalOpen || terminalPoppedOut}
                  pressed={bottomTerminalOpen || terminalPoppedOut}
                  collapsed={effectiveCollapsed}
                  onClick={closeMobileNav}
                  /* While popped out: focus only (a refused programmatic
                     focus is a harmless no-op). Explicit re-dock lives in the
                     TerminalDetachedBar below -- never a timing heuristic. */
                  onClickOverride={() => { if (terminalPoppedOut) focusTerminalPopout(); else toggleBottomTerminal(activeSlotProject) }}
                  {...uiLocation('shell.terminal')}
                />
              )}
              {hasRenderableMobileConnect && (
                <NavItem
                  path="#"
                  label={i18nT('app.connect_your_phone')}
                  icon={<Smartphone size={20} />}
                  /* Toggles the connect dialog instead of navigating — same
                     contract as the terminal row above. */
                  active={mobileConnectOpen}
                  pressed={mobileConnectOpen}
                  collapsed={effectiveCollapsed}
                  onClick={closeMobileNav}
                  onClickOverride={() => setMobileConnectOpen(true)}
                  {...uiLocation('shell.connect-phone')}
                />
              )}
              {/* Phone only: Search as a nav row. The bar's search square left
                  the phone (the chat page's bar has no room for it), so the nav
                  drawer — the one surface every non-chat phone page opens — is
                  where the command palette is reached; the chat page reaches it
                  from its own drawer's rail. Same label, same palette. */}
              {isMobile && (
                <NavItem
                  path="#"
                  label={slotOwners['quick-search'] ? i18nT('app.open_command_bar') : i18nT('app.search_sessions_files_and_commands')}
                  icon={<SearchIcon size={20} />}
                  active={false}
                  collapsed={effectiveCollapsed}
                  onClick={closeMobileNav}
                  onClickOverride={commandPalette.openPalette}
                  navId="search"
                  {...uiLocation('shell.menu-search')}
                />
              )}
              <div>{renderNavRow(cap)}</div>
              {/* Phone only: the account modal (balance, sign-in state) has no
                  other phone entry -- the readout capsule that opens it on the
                  desktop is not rendered on the phone. Shown on exactly the
                  readings the desktop segment shows (`kiroAccountEntry`); the
                  chat page reaches it from its own drawer's rail. */}
              {isMobile && kiroAccountEntry && (
                <NavItem
                  path="#"
                  label={i18nT('components.kiroAccountModal.kiro_account')}
                  icon={<Coins size={20} />}
                  active={kiroUsageOpen}
                  pressed={kiroUsageOpen}
                  collapsed={effectiveCollapsed}
                  onClick={closeMobileNav}
                  onClickOverride={() => setKiroUsageOpen(true)}
                  navId="account"
                  {...uiLocation('shell.kiro-account')}
                />
              )}
              <NavItem
                path={s.path}
                label={surfaceLabel(s)}
                icon={s.icon}
                active={activePath === s.path || activePath.startsWith(s.path + '/')}
                collapsed={effectiveCollapsed}
                onClick={closeMobileNav}
                badge={settingsDotLabel ? <span title={settingsDotLabel.title} role="status" aria-label={settingsDotLabel.label} className={effectiveCollapsed ? 'absolute top-1 right-1 w-2 h-2 bg-accent rounded-full z-10' : 'shrink-0 w-2 h-2 bg-accent rounded-full'} /> : undefined}
              />
              <RailCommunityLinks effectiveCollapsed={effectiveCollapsed} setReportProblemOpen={setReportProblemOpen} />
            </div>
          )
        })()}
        </>)
        return isMobile ? (
          <>
            {mobileNavMounted && (
              /* mt-2, unlike the desktop rail's mt-0: this form is `fixed` to the
                 VIEWPORT top rather than sitting in the grid row below the
                 topbar, so mt-0 pressed the card's rounded top edge flat against
                 the screen while mx-2/mb-2 inset the other three sides. Matching
                 the 8px inset on all four keeps the drawer reading as one
                 floating card. `top-0 bottom-0` with both margins resolves the
                 height to viewport-16px, so nothing is clipped. */
              /* motion.nav, like the sessions drawer and the right overlay: a
                 drag writes `mobileNavX` directly and ONLY a live binding paints
                 those frames. A plain <nav> reading `mobileNavX.get()` at render
                 time was correct while the tap was this panel's only mover —
                 a MotionValue deliberately does not re-render React, so once the
                 drawer gained a gesture the panel froze after the single
                 re-render the lock happens to cause, and moved only on release
                 when the settle took over. The settle still runs on the
                 COMPOSITOR through mobileNavPanelRef; framer and that animation
                 coexist here exactly as they do for the other two panels,
                 because `takeOverDrawer` adopts and cancels whatever is running
                 before either one writes. */
              <motion.nav
                key="mobile-nav-drawer"
                ref={mobileNavPanelRef}
                style={{ width: MOBILE_NAV_WIDTH, x: mobileNavX }}
                className="bg-bg-elevated border border-border rounded-xl flex flex-col mx-2 mt-2 mb-2 shadow-sm z-50 overflow-hidden fixed top-safe left-safe bottom-safe"
                role="navigation"
                aria-label={i18nT('app.main_navigation')}
                // Drawn over the page with a scrim behind it: while OPEN a chat
                // transcript under it is not on screen (see paneVisibility,
                // #23564). It stays mounted through its slide-out, which no
                // longer counts as covering.
                {...(mobileNavOpen ? coversPaneProps : {})}
              >
                {navBody}
              </motion.nav>
            )}
          </>
        ) : (
          <nav
            ref={railPeekSurface}
            className="focus-chrome-rail bg-bg-elevated border border-border rounded-xl flex flex-col mx-2 mt-0 mb-2 shadow-sm z-50 overflow-hidden"
            // Focus mode: same overlay treatment as the header. The rail's own
            // `mx-2` means translateX(-100%) would leave its 8px left margin
            // showing as a sliver, hence the extra 12px of travel. Width has to
            // become explicit — out of the grid there is no track to fill — and
            // it is the rail TRACK minus the 16px of horizontal margin, so the
            // overlay is exactly as wide as the docked rail would have been at
            // the user's current collapse state.
            style={focusActive
              ? {
                position: 'absolute',
                left: 0,
                top: FOCUS_INSET,
                bottom: 0,
                width: railWidthFor({ isMobile: false, collapsed: effectiveCollapsed }) - 16,
                zIndex: 62,
                transform: railPeek.open ? 'translateX(0)' : 'translateX(calc(-100% - 12px))',
                transition: 'transform 200ms cubic-bezier(0.2, 0, 0, 1)',
                pointerEvents: railPeek.open ? 'auto' : 'none',
              }
              : { gridArea: 'nav', width: 'auto' }}
            role="navigation"
            aria-label={i18nT('app.main_navigation')}
            {...hiddenChromeProps(focusActive && !railPeek.open && focusChromeInert)}
            {...(focusActive ? railPeek.surfaceProps : {})}
          >
            {navBody}
          </nav>
        )
      })()}
    </>
  )
}

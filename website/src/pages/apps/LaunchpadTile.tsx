/**
 * LaunchpadTile — one app in the Library's launchpad grid (PR3 App Store
 * split, approved mockup frame #a).
 *
 * The tile is the app's identity (the shared AppIconTile through the SAME
 * icon chain UpdatesList uses, so an app wears one mark on
 * every surface) with its name below. The icon and name are one button:
 * clicking it opens the app when it can open, else its detail page, so the
 * tile needs no separate Open control. Under the name, one row carries the
 * status and an overflow menu (Details, Pin/Unpin, Update, Disable/Enable,
 * Uninstall), overflow via DropdownMenu like SessionActionsMenu. On an
 * enabled app the status is a caption saying whether it is in the sidebar;
 * pinning lives in the menu, so the caption is the only pin indicator.
 * Disabled apps render greyscale at reduced opacity, and their row swaps the
 * caption for an Enable button, the one verb a disabled app is listed for;
 * their menu offers Details/Enable/Uninstall.
 *
 * Presentational on purpose: pin persistence, enable/disable/uninstall/
 * update behavior, and navigation are the PAGE's hooks (`useAppActions`,
 * `useAppUpdates`, the appNavHidden module) — this component only renders
 * the state it is handed and reports intent through callbacks, so it cannot
 * fork behavior from the card list it replaces.
 *
 * Reachability: the action row sits IN FLOW (reserved height, never
 * floating over the neighbouring grid row) and is ALWAYS visible, on
 * pointer and touch alike: its overflow menu is the tile's route to the
 * detail page, so no device or input mode hides it.
 */
import { ArrowUp, Info, MoreHorizontal, Pin, PinOff, Power, PowerOff, RefreshCw, Trash2 } from 'lucide-react'
import AppIconTile from '../../components/appstore/AppIconTile'
import { Btn } from '../../components/ui'
import { appDisplayName } from '../../components/appstore/appManifest'
import { installedIcon } from '../../components/appstore/useHeroArt'
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem,
  DropdownMenuSeparator,
} from '../../components/ui/dropdown-menu'
import { i18nT } from '../../i18n/t'
import { uiLocation } from '../../uiLocations/uiLocation'
import type { LibraryApp } from './useAppsData'
import { guidePick, guidePickAlias, guidePickControl, guidePickOf } from '../../uiLocations/targetRegistry'

/** Management verbs the tile can request. */
export type LaunchpadAction = 'enable' | 'disable' | 'uninstall' | 'update'

export default function LaunchpadTile({
  app,
  pinned,
  pinnable = true,
  actionLoading,
  onTogglePin,
  onAction,
  onOpen,
  onDetail,
  onMenuOpenChange,
}: {
  app: LibraryApp
  /** True when the app's page(s) show in the sidebar (id NOT in mc-app-nav-hidden). */
  pinned: boolean
  /**
   * False when the app has no sidebar destination at all (`appNavTarget`
   * returned null — enabled but no UI page). A pin control there would
   * promise a sidebar row the app cannot have, so the menu's Pin/Unpin item
   * and the pinned/unpinned caption are both suppressed.
   */
  pinnable?: boolean
  /** `${name}:${action}` while that action is in flight, else null. */
  actionLoading: string | null
  /** Flip this app's sidebar visibility (page persists via the appNavHidden module). */
  onTogglePin: (name: string) => void
  /** Management verb dispatch — the page owns the hooks behind it. */
  onAction: (name: string, action: LaunchpadAction) => void
  /** Open the app (page decides route vs openCommand). */
  onOpen: () => void
  /** Navigate to the app's detail page. */
  onDetail: () => void
  /** The ⋯ menu opened or closed (the page tells a guide which app is picked). */
  onMenuOpenChange?: (open: boolean) => void
}) {
  const m = app.manifest
  const name = app.name
  const display = appDisplayName(app)
  const canonical = app.displayName || app.name || ''
  const alias = canonical && canonical !== display ? guidePickAlias(canonical) : null
  // Icon resolution — the UpdatesList chain verbatim: a page
  // icon glyph, else the manifest's icon through `installedIcon` (iconPath
  // first, resolved against the app's own install dir; refuses external
  // hosts), else the name-hashed gradient inside AppIconTile.
  const iconUrl = installedIcon(m?.iconPath, m?.iconUrl, app.name)
  const iconUrlDark = installedIcon(m?.iconPathDark, m?.iconUrlDark, app.name)
  const pageIcon = m?.ui?.pages?.[0]?.icon || ''

  const disabled = !app.enabled
  const hasUI = !!(m?.ui?.entry) || (m?.ui?.pages?.length || 0) > 0
  const openable = app.enabled && (hasUI || !!m?.openCommand)
  const canUninstall = app.lifecycle !== 'locked'

  // Status caption under the name: an in-flight management action wins (the
  // overflow menu closes on click, so this caption is the only in-view
  // signal that Update/Disable/Enable/Uninstall is running — without it a
  // multi-second update reads as a no-op and invites a re-click), else the
  // pin state. An enabled app with no sidebar destination says so instead of
  // silently showing nothing next to captioned siblings. A disabled app shows
  // its Enable button here instead, which carries the in-flight caption as
  // its label.
  const actionInFlight = !!actionLoading && actionLoading.startsWith(`${name}:`)
  const caption = actionInFlight
    ? i18nT('apps.commandBar.working')
    : !pinnable
      ? i18nT('pages.libraryPage.tile_no_sidebar_page')
      : pinned
        ? i18nT('pages.libraryPage.tile_pinned')
        : i18nT('pages.libraryPage.tile_unpinned')

  return (
    <div
      className="group relative flex flex-col items-center gap-2 rounded-xl px-1.5 pt-3.5 pb-1.5 transition-colors hover:bg-bg-hover focus-within:bg-bg-hover"
      data-testid={`launchpad-tile-${name}`}
      // The app's name as the card shows it: a guide's pick outlines the one
      // card named exactly like this (apps.library.app-list). A built-in app
      // shows a translated name; its manifest name is the alias a pick from
      // the gateway may use.
      {...guidePick(display, alias?.ref)}
      data-guide-pick-alias={alias ? canonical : undefined}
    >
      {/* Tile face — a real button so the tile itself is keyboard focusable:
          opens the app when it can open, else its detail page. The icon is the
          shared AppIconTile with the mockup's 58px / rounded-15px geometry
          (important-override on the tile's own rounded-lg). The status row
          below is a SIBLING of this button, never a child: nested interactive
          elements get flattened or mis-announced by assistive tech, so each
          control must be its own top-level button. */}
      <button
        type="button"
        onClick={openable ? onOpen : onDetail}
        aria-label={display}
        className="bg-transparent border-0 p-0 cursor-pointer flex flex-col items-center gap-2 min-w-0 max-w-full"
      >
        <span className="relative inline-block">
          <AppIconTile
            name={name}
            icon={pageIcon}
            iconUrl={iconUrl}
            iconUrlDark={iconUrlDark}
            className={`w-[58px] h-[58px] !rounded-[15px] shadow-md ${disabled ? 'grayscale opacity-45' : ''}`}
          />
          {app.updateAvailable && !disabled && (
            <span
              aria-hidden
              title={i18nT('components.appstore.installedAppCard.update_to', { version: app._newVersion || app.version })}
              className="absolute -bottom-1 -right-1 w-[15px] h-[15px] rounded-full bg-accent text-accent-fg border-2 border-bg flex items-center justify-center"
            >
              <ArrowUp size={9} strokeWidth={3} aria-hidden />
            </span>
          )}
        </span>
        <span className="text-[12px] font-semibold text-text-strong text-center leading-tight max-w-full truncate">
          {display}
        </span>
      </button>

      {/* Status row — IN FLOW under the name (reserved height, no float over
          the next grid row): the caption, or a disabled app's Enable button,
          then the overflow menu. Always visible: the overflow menu is the
          tile's route to its detail page, so it is never hover-only. */}
      <div
        role="toolbar"
        aria-label={i18nT('pages.libraryPage.tile_actions', { name: display })}
        className="flex items-center gap-0.5 rounded-lg min-h-[24px]"
      >
        {disabled ? (
          // The button stays mounted while its own action runs and says so in
          // its label, so keyboard focus stays on it. `aria-disabled` rather than
          // `disabled`: a disabled button drops the focus it holds.
          <Btn
            type="button"
            aria-disabled={actionInFlight || undefined}
            onClick={() => { if (!actionInFlight) onAction(name, 'enable') }}
            className="text-[11px] px-2 py-0.5 whitespace-nowrap border-border-strong aria-disabled:cursor-wait"
          >
            {actionInFlight ? caption : i18nT('components.appstore.installedAppCard.enable')}
          </Btn>
        ) : (
          <span className="text-[10.5px] text-muted leading-none pl-1.5">{caption}</span>
        )}
        <DropdownMenu onOpenChange={onMenuOpenChange}>
          <DropdownMenuTrigger asChild>
            <button
              type="button"
              // What a "choose the app" step outlines on this card: the
              // control that makes the pick, not the whole card.
              {...guidePickControl('app_tile_menu_open')}
              aria-label={i18nT('pages.libraryPage.tile_more_actions', { name: display })}
              className="flex items-center text-[11px] px-1.5 py-1 rounded-md transition-colors bg-transparent border-0 cursor-pointer text-muted hover:text-text hover:bg-bg-elevated"
            >
              <MoreHorizontal size={13} aria-hidden />
            </button>
          </DropdownMenuTrigger>
          {/* The menu is drawn outside the card: it names the app it is for,
              so a guide about one app never points into another's menu. */}
          <DropdownMenuContent align="center" {...guidePickOf(display)}>
            <DropdownMenuItem onSelect={onDetail} {...uiLocation('apps.library.tile-details')}>
              <Info size={12} aria-hidden /> {i18nT('pages.libraryPage.tile_details')}
            </DropdownMenuItem>
            {!disabled && pinnable && (
              <DropdownMenuItem onSelect={() => onTogglePin(name)}>
                {pinned
                  ? <><PinOff size={12} aria-hidden /> {i18nT('pages.libraryPage.unpin')}</>
                  : <><Pin size={12} aria-hidden /> {i18nT('pages.libraryPage.pin')}</>}
              </DropdownMenuItem>
            )}
            {app.updateAvailable && !disabled && (
              <DropdownMenuItem
                disabled={actionLoading === `${name}:update`}
                onSelect={() => onAction(name, 'update')}
              >
                <ArrowUp size={12} aria-hidden /> {i18nT('components.appstore.installedAppCard.update')}
              </DropdownMenuItem>
            )}
            {app.lifecycle === 'gateway' && !app.updateAvailable && !disabled && (
              <DropdownMenuItem
                disabled={actionLoading === `${name}:update`}
                title={i18nT('pages.appDetailPage.sync_app_from_its_source_directory')}
                onSelect={() => onAction(name, 'update')}
              >
                <RefreshCw size={12} aria-hidden /> {i18nT('pages.appDetailPage.sync')}
              </DropdownMenuItem>
            )}
            <DropdownMenuSeparator />
            {disabled ? (
              <DropdownMenuItem
                disabled={actionLoading === `${name}:enable`}
                onSelect={() => onAction(name, 'enable')}
              >
                <Power size={12} aria-hidden /> {i18nT('components.appstore.installedAppCard.enable')}
              </DropdownMenuItem>
            ) : (
              <DropdownMenuItem
                disabled={actionLoading === `${name}:disable`}
                onSelect={() => onAction(name, 'disable')}
                {...uiLocation('apps.library.tile-disable')}
              >
                <PowerOff size={12} aria-hidden /> {i18nT('components.appstore.installedAppCard.disable')}
              </DropdownMenuItem>
            )}
            {canUninstall && (
              <DropdownMenuItem
                className="text-danger focus:text-danger"
                disabled={actionLoading === `${name}:uninstall`}
                onSelect={() => onAction(name, 'uninstall')}
                {...uiLocation('apps.library.tile-uninstall')}
              >
                <Trash2 size={12} aria-hidden /> {i18nT('components.appstore.installedAppCard.uninstall')}
              </DropdownMenuItem>
            )}
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </div>
  )
}

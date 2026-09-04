/**
 * LaunchpadTile — one app in the Library's launchpad grid (PR3 App Store
 * split, approved mockup frame #a).
 *
 * The tile is the app's identity (the shared AppIconTile through the SAME
 * icon chain UpdatesList uses, so an app wears one mark on
 * every surface), the name and a status caption sit below it, and the pin
 * badge on the icon's top-right corner toggles whether the app appears in
 * the sidebar — filled check = pinned, hollow plus = unpinned. An action
 * row under the caption carries at most two peers: Open (when the app can
 * open) and an overflow menu with everything else (Details, Pin/Unpin,
 * Update, Disable/Enable, Uninstall) — the repo's capped-action-row
 * contract (max-two-buttons-per-row), overflow via DropdownMenu like
 * SessionActionsMenu. Disabled apps render greyscale at reduced opacity,
 * hide the pin badge (an app absent from the sidebar has nothing to pin),
 * and their menu offers Details/Enable/Uninstall.
 *
 * Presentational on purpose: pin persistence, enable/disable/uninstall/
 * update behavior, and navigation are the PAGE's hooks (`useAppActions`,
 * `useAppUpdates`, the appNavHidden module) — this component only renders
 * the state it is handed and reports intent through callbacks, so it cannot
 * fork behavior from the card list it replaces.
 *
 * Reachability: the action row sits IN FLOW (reserved height, never
 * floating over the neighbouring grid row) and reveals on `group-hover` /
 * `group-focus-within` for pointer+keyboard, stays force-revealed while its
 * menu is open (Radix portals the menu, so focus-within alone would drop),
 * and under `(hover: none)` is ALWAYS visible — the touchActions escape
 * hatch (issues #2014/#3584): a hover-revealed affordance is otherwise
 * permanently unreachable on touch. Every management verb is also in the
 * 40px-friendly menu, so the 18px pin badge is a shortcut, not the only
 * path.
 */
import { useState } from 'react'
import { ArrowUp, Check, ExternalLink, Info, MoreHorizontal, Pin, PinOff, Plus, Power, PowerOff, RefreshCw, Trash2 } from 'lucide-react'
import AppIconTile from '../../components/appstore/AppIconTile'
import { appDisplayName } from '../../components/appstore/appManifest'
import { installedIcon } from '../../components/appstore/useHeroArt'
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem,
  DropdownMenuSeparator,
} from '../../components/ui/dropdown-menu'
import { i18nT } from '../../i18n/t'
import type { LibraryApp } from './useAppsData'

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
}: {
  app: LibraryApp
  /** True when the app's page(s) show in the sidebar (id NOT in mc-app-nav-hidden). */
  pinned: boolean
  /**
   * False when the app has no sidebar destination at all (`appNavTarget`
   * returned null — enabled but no UI page). A pin control there would
   * promise a sidebar row the app cannot have, so the badge, the menu's
   * Pin/Unpin item, and the pinned/unpinned caption are all suppressed.
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
}) {
  const m = app.manifest
  const name = app.name
  const display = appDisplayName(app)
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

  // The menu lives in a portal, so while it is open the tile loses
  // focus-within and the hover-revealed row would vanish under the open
  // menu's own trigger. Track open state and force the reveal.
  const [menuOpen, setMenuOpen] = useState(false)

  // Status caption under the name: an in-flight management action wins (the
  // overflow menu closes on click, so this caption is the only in-view
  // signal that Update/Disable/Enable/Uninstall is running — without it a
  // multi-second update reads as a no-op and invites a re-click), then
  // disabled (a disabled app is not in the sidebar regardless of the stored
  // pin), else the pin state. An enabled app with no sidebar destination
  // says so instead of silently showing nothing next to captioned siblings.
  const actionInFlight = !!actionLoading && actionLoading.startsWith(`${name}:`)
  const caption = actionInFlight
    ? i18nT('apps.commandBar.working')
    : disabled
      ? i18nT('pages.libraryPage.tile_disabled')
      : !pinnable
        ? i18nT('pages.libraryPage.tile_no_sidebar_page')
        : pinned
          ? i18nT('pages.libraryPage.tile_pinned')
          : i18nT('pages.libraryPage.tile_unpinned')

  const pinLabel = pinned
    ? i18nT('pages.libraryPage.unpin_from_sidebar', { name: display })
    : i18nT('pages.libraryPage.pin_to_sidebar', { name: display })

  return (
    <div
      className="group relative flex flex-col items-center gap-2 rounded-xl px-1.5 pt-3.5 pb-1.5 transition-colors hover:bg-bg-hover focus-within:bg-bg-hover"
      data-testid={`launchpad-tile-${name}`}
    >
      {/* Tile face — a real button so the tile itself is keyboard focusable:
          opens the app when it can open, else its detail page. The icon is the
          shared AppIconTile with the mockup's 58px / rounded-15px geometry
          (important-override on the tile's own rounded-lg). The pin badge is
          a SIBLING of this button (absolutely anchored over the icon's
          top-right corner), never a child: nested interactive elements get
          flattened or mis-announced by assistive tech, so each control must
          be its own top-level button. */}
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
      {!disabled && pinnable && (
        <button
          type="button"
          aria-pressed={pinned}
          aria-label={pinLabel}
          title={pinLabel}
          onClick={(e) => { e.stopPropagation(); onTogglePin(name) }}
          className={`absolute top-[5px] left-1/2 ml-5 w-[18px] h-[18px] rounded-full flex items-center justify-center cursor-pointer transition-colors border-0 p-0 ${
            pinned
              ? 'bg-accent text-accent-fg'
              : 'bg-bg-elevated text-muted border border-border-strong hover:text-text'
          }`}
        >
          {pinned ? <Check size={11} strokeWidth={3} aria-hidden /> : <Plus size={11} aria-hidden />}
        </button>
      )}
      <span className="text-[10.5px] text-muted leading-none">{caption}</span>

      {/* Action row — IN FLOW under the caption (reserved height, no float
          over the next grid row), at most two peers: Open + overflow menu.
          Revealed on hover / focus-within / open menu; permanently visible
          under (hover: none), where hover can never fire (touchActions
          escape hatch — the row is compact so the always-on form stays
          inside the tile). */}
      <div
        role="toolbar"
        aria-label={i18nT('pages.libraryPage.tile_actions', { name: display })}
        className={`flex items-center gap-0.5 rounded-lg transition-opacity [@media(hover:none)]:opacity-100 [@media(hover:none)]:pointer-events-auto ${
          menuOpen
            ? 'opacity-100 pointer-events-auto'
            : 'opacity-0 pointer-events-none group-hover:opacity-100 group-hover:pointer-events-auto group-focus-within:opacity-100 group-focus-within:pointer-events-auto'
        }`}
      >
        {openable && (
          <button
            type="button"
            onClick={onOpen}
            className="flex items-center gap-1 text-[11px] px-2 py-1 rounded-md whitespace-nowrap transition-colors bg-transparent border-0 cursor-pointer text-text hover:bg-bg-elevated"
          >
            <ExternalLink size={11} aria-hidden /> {i18nT('components.appstore.installedAppCard.open')}
          </button>
        )}
        <DropdownMenu open={menuOpen} onOpenChange={setMenuOpen}>
          <DropdownMenuTrigger asChild>
            <button
              type="button"
              aria-label={i18nT('pages.libraryPage.tile_more_actions', { name: display })}
              className="flex items-center text-[11px] px-1.5 py-1 rounded-md transition-colors bg-transparent border-0 cursor-pointer text-muted hover:text-text hover:bg-bg-elevated"
            >
              <MoreHorizontal size={13} aria-hidden />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="center">
            <DropdownMenuItem onSelect={onDetail}>
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
              >
                <PowerOff size={12} aria-hidden /> {i18nT('components.appstore.installedAppCard.disable')}
              </DropdownMenuItem>
            )}
            {canUninstall && (
              <DropdownMenuItem
                className="text-danger focus:text-danger"
                disabled={actionLoading === `${name}:uninstall`}
                onSelect={() => onAction(name, 'uninstall')}
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

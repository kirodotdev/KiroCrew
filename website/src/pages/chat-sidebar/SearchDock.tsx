/** The dock over the lanes. Its field is the search row's controls: the lane
 *  cycle button and the sort and filter menu (status chips with the Recent
 *  window, sort, folder order, dormant collapse, tags, models, folders), with
 *  the label tables its rows and chips name. Its shelf, under the search
 *  capsule, holds the active filter chips (tags, models, status, hides) and the
 *  list-level notices (seed, crew names, read failures, close refusal, folder
 *  writes and order, create, rename, title, peer sessions). */
import { Fragment } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { X, ArrowUpDown, EyeOff, ChevronRight, Clock, Check, List, ListTree, RotateCcw, Server, Pause, Play, WifiOff } from 'lucide-react'
import ErrorNotice from '../../components/ErrorNotice'
import FolderGlyph from '../../components/FolderGlyph'
import { useQuery } from '@tanstack/react-query'
import { useLocation } from 'react-router-dom'
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuRadioGroup, DropdownMenuRadioItem, DropdownMenuSub, DropdownMenuSubTrigger, DropdownMenuSubContent } from '../../components/ui/dropdown-menu'
import { setCloseRefused } from '../../store/chatSlice'
import { api } from '../../api/client'
import { isInstancesDisabledError } from '../../utils/instancesDisabled'
import { errMessage } from '../../utils/thunkError'
import { Btn, IconButton } from '../../components/ui'
import SimpleSelect from '../../components/SimpleSelect'
import { isEmbeddedPane } from '../../lib/embedded'
import { SearchFilterBar, FilterMenuButton, FilterChip, FILTER_CHIP_ROW_CLS, FilterMenuLabel, FilterMenuContent } from '../../components/SearchFilterBar'
import { FOLDER_SORT_MODES, folderHasCreatedStamp, type FolderSortMode } from '../../utils/folderTree'
import { type RecentUnit, RECENT_WINDOW_PRESETS, formatRecentWindow } from '../recentWindow'
import { STALE_COLLAPSE_PRESETS_MS } from '../staleCollapse'
import type { SortKey } from '../chat/sessionOrder'
import { i18nT } from '../../i18n/t'
import { fmtList } from '../../i18n/format'
import type { SessionFilterKey } from './types'
import { SESSION_FILTERS } from './filters'
import { modelFilterLabel } from './models'
import { DisclosureChevron } from './chrome'
import type { SidebarScope } from './scope'

/**
 * A pill for one duration choice, shared by the Recent window presets and — on a
 * phone, where those options render inline rather than in a flyout — the
 * dormant-collapse thresholds. One component so the two lists cannot drift, and
 * so the duplicated class/style pair does not read as copy-paste.
 */
function DurationChip({ label, selected, onSelect }: { label: string; selected: boolean; onSelect: () => void }) {
  return (
    <button
      type="button"
      aria-pressed={selected}
      className="px-2 py-0.5 rounded-full text-[11px] cursor-pointer border transition-colors"
      style={selected
        ? { background: 'color-mix(in srgb, var(--ok) 12%, transparent)', color: 'var(--ok)', borderColor: 'color-mix(in srgb, var(--ok) 35%, transparent)' }
        : { background: 'transparent', color: 'var(--muted)', borderColor: 'var(--border)' }}
      // A plain button is not a menu item, so clicking it leaves the host menu
      // open on its own — Radix dismisses on an item select or an outside
      // pointer-down, neither of which this is (verified in a browser).
      onClick={onSelect}
    >
      {label}
    </button>
  )
}

/**
 * Catalog keys for the filter rows, chips and tooltips.
 *
 * Keys, not copy: these tables are module-level, so an `i18nT()` call here would
 * resolve once at boot and never follow a language switch — the lookup happens
 * where each label renders. Shaped as flat `Record`s of full literal keys and
 * indexed inline at the `i18nT()` call, because that is the form
 * `scripts/check-i18n-keys.mjs` can resolve statically; a key it cannot resolve
 * is a key it cannot verify exists.
 */
export const FILTER_LABEL_KEY: Record<SessionFilterKey, string> = {
  unread: 'pages.chatSidebar.filter_unread',
  running: 'pages.chatSidebar.filter_running',
  pinned: 'pages.chatSidebar.filter_pinned',
  recent: 'pages.chatSidebar.filter_recent',
}
export const FILTER_DESCRIPTION_KEY: Record<SessionFilterKey, string> = {
  unread: 'pages.chatSidebar.filter_unread_description',
  running: 'pages.chatSidebar.filter_running_description',
  pinned: 'pages.chatSidebar.filter_pinned_description',
  recent: 'pages.chatSidebar.filter_recent_description',
}

/** Sort options, in menu order. The label lives in `SORT_LABEL_KEY`. */
const SORT_OPTIONS: { value: SortKey }[] = [
  { value: 'date-desc' },
  { value: 'date-asc' },
  { value: 'created-desc' },
  { value: 'created-asc' },
  { value: 'name-asc' },
  { value: 'name-desc' },
]
/** Catalog key per sort option — same resolvable shape as `FILTER_LABEL_KEY`. */
export const SORT_LABEL_KEY: Record<SortKey, string> = {
  'date-desc': 'pages.chatSidebar.sort_newest',
  'date-asc': 'pages.chatSidebar.sort_oldest',
  'created-desc': 'pages.chatSidebar.sort_created_newest',
  'created-asc': 'pages.chatSidebar.sort_created_oldest',
  'name-asc': 'pages.chatSidebar.sort_name_asc',
  'name-desc': 'pages.chatSidebar.sort_name_desc',
}
/** Catalog key per folder sort mode -- the "Folder order" rows in the same menu.
 *  Three rows because there are three modes; the list itself is
 *  `FOLDER_SORT_MODES`, so a fourth mode fails typing here rather than rendering
 *  with no label. */
export const FOLDER_SORT_LABEL_KEY: Record<FolderSortMode, string> = {
  custom: 'pages.chatSidebar.folder_order_custom',
  name: 'pages.chatSidebar.folder_order_name',
  created: 'pages.chatSidebar.folder_order_created',
}

/** The ONE notice for a failed ['instances'] read issued by the rows'
 *  runs-elsewhere chips, mounted only where InstanceTabBar is not: an embedded pane (the bar never
 *  polls there) and a top-level /embed/* route (that layout has no top bar).
 *  Everywhere else the bar shows the same failure, so a second notice would
 *  duplicate it. Both surfaces stay silent on exactly one denial, decided by
 *  the shared `isInstancesDisabledError`: the gateway's own `instances_disabled`
 *  403 (the feature is simply off). The route's other 403s (non-owner,
 *  Slack-origin) are real authorization failures and show on both. */
function RemoteCrewNamesError() {
  const { pathname } = useLocation()
  const topBarAbsent = isEmbeddedPane() || pathname.startsWith('/embed/')
  // Same key and fetcher as the chips, so this observer adds no request.
  const { error } = useQuery({
    queryKey: ['instances'],
    queryFn: () => api.listInstances(),
    enabled: topBarAbsent,
  })
  if (!topBarAbsent || !error || isInstancesDisabledError(error)) return null
  // Lead with what failed in product terms; the server's text follows it.
  const detail = errMessage(error)
  return (
    <div className="mx-2 mt-2 shrink-0">
      <ErrorNotice
        variant="inline"
        className="flex-wrap w-full"
        title={detail ? i18nT('components.instanceTabBar.instances_load_failed') : undefined}
        message={detail || i18nT('components.instanceTabBar.instances_load_failed')}
        askAgent
        testId="remote-crew-names-error"
      />
    </div>
  )
}

/** The dock's two slots, field first, as `ListDock` takes them. */
export function renderSearchDock(shell: SidebarScope): { field: React.ReactNode; shelf: React.ReactNode } {
  const { activeFilters, activeModelKeys, activeModelRows, activeTagIds, activeTagNames, autoTitleError,
    availableLanes, boardLaneActive, changeRecentUnit, clearHiddenFilters, clearModelFilter, clearTagFilter,
    closeRefused, columnsError, columnsFailed, commitRecentAmount, conductorView, connected, cycleLane, dismissSortError, dispatch,
    filterCounts, filterHiddenFolders, filterSortOpen, filtersPaused, flatLaneActive, folderActionError,
    folderFilterRows, folderOfflineNotice, folderReorderHint, folderReorderHintRef, folderRowsDrawn, folderSortMode,
    folderSortMut, folderSortRead, folders, foldersError, foldersFailed, foldersShelved,
    hasRemoteExecutedRow, hiddenFilters, hiddenFolderCount, instanceSessions, isMobile, lane,
    laneSwitchLabel, listNarrowed, modelFilterInverted, modelFilterRows, newChatError, nextLane, pinnedOrderError,
    recentAmountDraft, recentUnitDraft, recentWindowMs, refetchColumns, refetchFolders, remoteSessionsError,
    renameError, seedError, seedStateLanesMutation, selectRecentPreset, setAllFiltersPaused,
    setAutoTitleError, setFilterSortOpen, setFolderActionError, setFolderOfflineNotice, setModelFilterExcluded, setNewChatError,
    setPinnedOrderError, setRecentAmountDraft, setRenameError, setSeedError, setSlotFilter, setSortKey, setStaleCollapseMs,
    setToggleSaveFailed, showAllFolders, slotFilter, sortError, sortKey, staleCollapseMs, tagFilterRows, toggleFilter,
    toggleFolderFilter, toggleFoldersShelved, toggleHideFilter, toggleModelFilter, toggleSaveFailed,
    toggleTagFilter } = shell
  const field = (
        // Search with inline sort/filter control — the shared list-panel
        // search row (components/SearchFilterBar), also mounted by the Crew
        // Members roster.
      <SearchFilterBar
        placeholder={i18nT('pages.chatSidebar.search_sessions')}
        clearLabel={i18nT('pages.chatSidebar.clear_search')}
        value={slotFilter}
        onChange={setSlotFilter}
        trailingCount={availableLanes.length > 1 ? 2 : 1}
        trailing={(
          <>
            {/* ONE button cycling tree -> conductor -> flat, skipping any lane that
             *  cannot render (see `availableLanes`). Deliberately not a segmented
             *  control: the sidebar's chrome stays Raycast-plain, so the icon shows
             *  the lane you are IN and the copy names where the next press goes.
             *  Hidden entirely when only the tree is available, which is the
             *  pre-existing "no folders, nothing to flatten" case. */}
            {availableLanes.length > 1 && (
            <button
              type="button"
              className={`relative w-6 h-6 rounded flex items-center justify-center cursor-pointer transition-colors border-none ${lane !== 'tree' ? 'text-accent bg-accent-subtle' : 'text-muted hover:text-text hover:bg-bg-hover bg-transparent'}`}
              onClick={cycleLane}
              /* Both strings describe the ACTION and are derived from `nextLane`, not
               * from the lane in view: this is the feature's only entry point, so a
               * label naming the current lane tells every user -- and every screen
               * reader -- that the press goes somewhere it does not.
               *
               * With a board configured the toggle flattens INSIDE each column rather
               * than producing the single flat lane, so the copy must not promise
               * "all chats without folders" (one combined list). */
              title={laneSwitchLabel}
              aria-label={laneSwitchLabel}
              /* NOT `aria-pressed`. This cycles three positions, and a boolean would
               * announce the same "pressed" for conductor and for flat -- two different
               * states told apart by nothing a screen reader hears. The lane in view is
               * named instead, which is the fact a reader actually wants. */
              data-lane={lane}
              data-next-lane={nextLane}
              data-testid="flat-view-toggle"
            >
              {/* Crossfaded rather than hard-swapped. One persistent button showing two
                * different drawings on press reads as two different buttons -- a blind
                * read of this control reported exactly that confusion -- and a short
                * dissolve is what says "the same button changed" instead. */}
              <AnimatePresence mode="wait" initial={false}>
                <motion.span
                  key={conductorView ? 'conductor' : 'list'}
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  exit={{ opacity: 0 }}
                  transition={{ duration: 0.12 }}
                  className="flex items-center justify-center"
                >
                  {conductorView ? <ListTree size={14} /> : <List size={14} />}
                </motion.span>
              </AnimatePresence>
            </button>
            )}
            <DropdownMenu open={filterSortOpen} onOpenChange={setFilterSortOpen}>
              <DropdownMenuTrigger asChild>
                {/* The funnel holds the way back, so while a hide withholds rows its
                    title says both that something is withheld and how much.

                    Warn, not accent: the view toggle immediately beside it tints accent
                    to mean "this lane is active", so one accent doing both jobs reads as
                    the toggle's own state rather than as a population kept off screen.

                    The count does NOT go in the accessible name. A button's name names
                    the button; a count that changes under the reader belongs in content,
                    and every lane draws it as content — a reveal row where there are
                    folder headers to hang one from, the lane notice in a board. That also
                    keeps the name stable for a reader navigating by control name. Both
                    numbers read `hiddenFolderCount`, so they cannot disagree. */}
                <FilterMenuButton
                  title={hiddenFolderCount > 0
                    ? i18nT('pages.chatSidebar.sort_filter_sessions_hidden', { count: hiddenFolderCount })
                    : i18nT('pages.chatSidebar.sort_filter_sessions')}
                  aria-label={i18nT('pages.chatSidebar.sort_and_filter_sessions')}
                  badge={filterCounts['unread']}
                  className={hiddenFolderCount > 0 ? 'text-warn' : undefined}
                  data-folder-hide-active={hiddenFolderCount > 0 ? String(hiddenFolderCount) : undefined}
                />
              </DropdownMenuTrigger>
              <FilterMenuContent align="end">
                <FilterMenuLabel>{i18nT('pages.chatSidebar.filter')}</FilterMenuLabel>
                {SESSION_FILTERS.map(filterDef => {
                  const active = activeFilters.has(filterDef.key)
                  const hidden = hiddenFilters.has(filterDef.key)
                  const slotCount = filterCounts[filterDef.key] ?? 0
                  const isRecent = filterDef.key === 'recent'
                  if (isRecent) {
                    // The window picker is a NESTED FLYOUT on a wide viewport and
                    // renders INLINE on a phone. Radix hardcodes a submenu to
                    // side="right" and only lets its popper shift on the cross
                    // axis, so at phone width neither side fits: the flyout lands
                    // on whichever side overflows less and is cut off by the
                    // viewport (measured at 390px: 249px wide, 192px of it past
                    // the right edge, --radix-popper-available-width: 57px). No
                    // width or padding tuning can recover that — the flyout has
                    // nowhere to go beside a menu that already spans most of the
                    // screen, so on a phone the options come inline instead.
                    const picker = (
                      // Non-menu-item controls: stop click/keydown from reaching
                      // Radix so choosing a window doesn't dismiss the menu
                      // (mirrors the folder-rename input pattern).
                      // eslint-disable-next-line jsx-a11y/no-static-element-interactions -- the three handlers only stopPropagation, so this wrapper has no action of its own for a keyboard to reach; the chips and the number input inside are the real controls and each is separately focusable
                      <div
                        onClick={e => e.stopPropagation()}
                        onMouseDown={e => e.stopPropagation()}
                        onKeyDown={e => e.stopPropagation()}
                      >
                        <div className="px-1 pb-1 text-[11px] text-muted">{i18nT('pages.chatSidebar.within')}</div>
                        <div className="flex flex-wrap gap-1 px-1 mb-2">
                          {RECENT_WINDOW_PRESETS.map(preset => (
                            <DurationChip
                              key={preset.ms}
                              label={preset.label}
                              selected={recentWindowMs === preset.ms}
                              onSelect={() => selectRecentPreset(preset.ms)}
                            />
                          ))}
                        </div>
                        <div className="px-1 text-[12px] text-muted">
                          <div className="mb-1">{i18nT('pages.chatSidebar.custom')}</div>
                          <div className="flex items-center gap-1.5">
                            {/* Draft-string value so the field can be cleared
                                / partially typed; commit + clamp on blur or
                                Enter. Unit changes commit immediately but keep
                                the amount as-typed (no re-derivation flip). */}
                            <input
                              type="number"
                              min={1}
                              max={9999}
                              value={recentAmountDraft}
                              onChange={e => setRecentAmountDraft(e.target.value)}
                              onBlur={commitRecentAmount}
                              onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); commitRecentAmount() } }}
                              aria-label={i18nT('pages.chatSidebar.custom_recency_amount')}
                              className="w-12 shrink-0 px-1.5 py-0.5 rounded border border-border bg-bg-elevated text-text text-[12px]"
                            />
                            <SimpleSelect
                              value={recentUnitDraft}
                              onChange={v => changeRecentUnit(v as RecentUnit)}
                              className="px-1.5 py-0.5 text-[12px] rounded"
                              options={['minutes', 'hours', 'days']}
                              optionLabels={[i18nT('pages.chatSidebar.min'), i18nT('pages.chatSidebar.hours'), i18nT('pages.chatSidebar.days')]}
                              aria-label={i18nT('pages.chatSidebar.custom_recency_unit')}
                              // Was `flex-1 min-w-0` on the old <select>; the
                              // trigger's chrome is fixed inside ui/select.tsx,
                              // but the flex sizing has to survive on the
                              // wrapper div that replaces it as the flex item.
                              style={{ flex: '1 1 0%', minWidth: 0 }}
                            />
                          </div>
                        </div>
                      </div>
                    )
                    const rowBody = (
                      <>
                        {filterDef.icon(active)}
                        <span className="flex-1 truncate">
                          {i18nT(FILTER_LABEL_KEY[filterDef.key])}
                          <span className="text-muted"> · {formatRecentWindow(recentWindowMs)}</span>
                          {slotCount > 0 ? ` (${slotCount})` : ''}
                        </span>
                        {active && <Check size={14} className="text-accent shrink-0" />}
                      </>
                    )
                    if (isMobile) {
                      // Inline: the row keeps its only job (toggle the filter) and
                      // the window options sit under it, one tap each. No chevron
                      // — there is nothing left to open.
                      //
                      // The picker is NOT gated on the filter being active. It was,
                      // on the reasoning that picking a window did not enable the
                      // filter so an always-visible picker reported an effect it
                      // was not having — and picking now DOES enable it, at the
                      // commit seam, for every viewport. Keeping the gate would be
                      // the per-modality thinking that caused this defect.
                      return (
                        <Fragment key={filterDef.key}>
                          <DropdownMenuItem
                            title={i18nT(FILTER_DESCRIPTION_KEY[filterDef.key])}
                            onSelect={e => { e.preventDefault(); toggleFilter('recent') }}
                          >
                            {rowBody}
                          </DropdownMenuItem>
                          <div className="px-2 pb-1">{picker}</div>
                        </Fragment>
                      )
                    }
                    // Flyout. The whole row is a single SubTrigger (one focusable
                    // menu item with correct roving-tabindex). Toggling the
                    // filter must be reachable by every input modality:
                    //  - pointer: onClick toggles; we deliberately do NOT
                    //    preventDefault so Radix's own click-to-open still fires.
                    //  - keyboard: Radix routes Enter/Space/ArrowRight to open the
                    //    submenu and the SubTrigger is a div (no synthetic click),
                    //    so onClick never fires for keys. onKeyDown toggles on
                    //    Enter/Space (preventDefault suppresses Radix's open for
                    //    just those keys); ArrowRight falls through and opens.
                    return (
                      <DropdownMenuSub key={filterDef.key}>
                        <DropdownMenuSubTrigger
                          title={i18nT(FILTER_DESCRIPTION_KEY[filterDef.key])}
                          onClick={() => toggleFilter('recent')}
                          onKeyDown={e => {
                            if (e.key === 'Enter' || e.key === ' ') {
                              e.preventDefault()
                              toggleFilter('recent')
                            }
                          }}
                        >
                          {rowBody}
                          <ChevronRight size={13} className="text-muted shrink-0" />
                        </DropdownMenuSubTrigger>
                        <DropdownMenuSubContent className="min-w-[190px] p-2">
                          {picker}
                        </DropdownMenuSubContent>
                      </DropdownMenuSub>
                    )
                  }
                  const filterLabel = i18nT(FILTER_LABEL_KEY[filterDef.key])
                  const canHide = !!filterDef.hideStorageKey
                  const showRow = (
                    <DropdownMenuItem
                      key={filterDef.key}
                      title={i18nT(FILTER_DESCRIPTION_KEY[filterDef.key])}
                      // Keep the menu open so multiple filters can be toggled.
                      onSelect={e => { e.preventDefault(); toggleFilter(filterDef.key) }}
                      className={canHide ? 'flex-1 min-w-0' : undefined}
                    >
                      {filterDef.icon(active)}
                      <span className="flex-1 truncate">{filterLabel}{slotCount > 0 ? ` (${slotCount})` : ''}</span>
                      {active && <Check size={14} className="text-accent shrink-0" />}
                    </DropdownMenuItem>
                  )
                  if (!canHide) return showRow
                  // Two menu items drawn as one row, not a button nested in one: a
                  // focusable control inside a menuitem is skipped by the menu's
                  // roving focus and read as part of the row's name. The second
                  // item sits where the check mark would go and is offered only
                  // while show-only is OFF: with it on, the row carries its check
                  // like every other filter row, so a click anywhere on it, the
                  // check included, turns show-only off as it always did, and going
                  // from show-only straight to a hide is two clicks on purpose.
                  // While the chip is hiding the item draws the lit hide icon;
                  // otherwise the icon appears on row hover or focus, and always on
                  // touch screens, which have no hover. The wrapper stays in place
                  // either way so the row is not remounted, which would drop the
                  // menu's focus from it, when show-only flips. The styling sits on
                  // the wrapper and the icon slot, not on the menu primitives.
                  // The wrapper fades with the same `transition-colors` the menu
                  // items carry: an instant wrapper over fading items leaves the
                  // eye item's own padded focus box behind for a beat when the
                  // pointer leaves the row.
                  const hideLabel = hidden
                    ? i18nT('pages.chatSidebar.filter_unhide_named', { filter: filterLabel })
                    : i18nT('pages.chatSidebar.filter_hide_named', { filter: filterLabel })
                  return (
                    <div
                      key={filterDef.key}
                      role="group"
                      className="group/filter flex items-center rounded-md transition-colors hover:bg-bg-hover focus-within:bg-bg-hover"
                    >
                      {showRow}
                      {!active && (
                        <DropdownMenuItem
                          aria-label={hideLabel}
                          title={hideLabel}
                          onSelect={e => { e.preventDefault(); toggleHideFilter(filterDef.key) }}
                          className="group/hide shrink-0"
                          data-testid={`filter-hide-${filterDef.key}`}
                          data-filter-hidden={hidden ? 'true' : undefined}
                        >
                          <span
                            className={hidden
                              ? 'inline-flex size-3.5 items-center justify-center rounded bg-accent-subtle text-accent ring-4 ring-accent-subtle'
                              : 'inline-flex size-3.5 items-center justify-center text-muted group-focus/hide:text-text'}
                          >
                            <EyeOff
                              size={12}
                              className={hidden ? undefined : 'opacity-0 group-hover/filter:opacity-100 group-focus-within/filter:opacity-100 [@media(hover:none)]:opacity-100'}
                            />
                          </span>
                        </DropdownMenuItem>
                      )}
                    </div>
                  )
                })}
                {/* Lift every active status filter at once, for a look at the
                    whole list without losing the filter setup, then put them
                    back. A menu row, not a third control in the chip row:
                    AUTOSDE max-two-buttons-per-row grandfathers that row but
                    forbids growing it, and the chip click keeps its one meaning
                    (clear this filter). Hidden with no active filter, when
                    there is nothing to pause. */}
                {activeFilters.size > 0 && (
                  <DropdownMenuItem
                    data-testid="filter-pause-all"
                    aria-pressed={filtersPaused}
                    onSelect={e => { e.preventDefault(); setAllFiltersPaused(!filtersPaused) }}
                  >
                    {filtersPaused ? <Play size={12} className="text-muted" aria-hidden="true" /> : <Pause size={12} className="text-muted" aria-hidden="true" />}
                    <span className="flex-1 truncate">{filtersPaused ? i18nT('pages.chatSidebar.resume_all_filters') : i18nT('pages.chatSidebar.pause_all_filters')}</span>
                  </DropdownMenuItem>
                )}
                <DropdownMenuSeparator />
                {/* Names its object: this menu also carries "Folder order" two
                    sections down, and a bare "Sort by" over one list beside an
                    order over another read as sorting twice, with a guess about
                    which list each one changes. The members page keeps the bare
                    key -- it has no second list. */}
                <FilterMenuLabel>{i18nT('pages.chatSidebar.sort_sessions_by')}</FilterMenuLabel>
                {/* A switcher over mutually exclusive sorts, so each row is a
                    `menuitemradio` reporting `aria-checked` -- a screen reader
                    says WHICH sort is in effect, which a plain item's check icon
                    (kept, sighted-only) cannot convey. The write stays on each
                    row's `onSelect`, NOT the group's `onValueChange`: `sortKey`
                    is the OPTIMISTIC overlay value (a pick shows at once, before
                    its save lands), so the group's `value` already reflects an
                    in-flight pick -- and Radix fires `onValueChange` only on a
                    change against that value. `setSortKey` must run on every
                    pick regardless, because it also decides this load's
                    carry-over (`carryChecked`) and queues the pick against the
                    one in flight; routing through `onValueChange` would drop a
                    re-pick of the shown-but-not-yet-saved sort. */}
                <DropdownMenuRadioGroup value={sortKey}>
                  {SORT_OPTIONS.map(o => (
                    <DropdownMenuRadioItem key={o.value} value={o.value} onSelect={() => setSortKey(o.value)}>
                      <span className="flex-1">{i18nT(SORT_LABEL_KEY[o.value])}</span>
                      {sortKey === o.value && <Check size={14} className="text-accent shrink-0" aria-hidden="true" />}
                    </DropdownMenuRadioItem>
                  ))}
                </DropdownMenuRadioGroup>
                {/* Folder order: the same row grammar as Sort by, one section down,
                    because it answers the same kind of question about this list.
                    Custom is the person's own arrangement (drag, or an agent's
                    chat_folder_move); Name and Created are views over it that
                    never rewrite a stored position. Offered in EVERY lane, unlike
                    the stale control below: the flat lane explodes chats out of
                    their folders and the conductor lane nests by lineage, but the
                    mode is not idle there -- every row menu's "Move to folder"
                    picker, the history search's folder groups, the Command Bar,
                    the job form and the MCP tree all list in it -- and this is the
                    only control that writes it. A mode a person cannot change from
                    the lane they are in is the trap. */}
                <DropdownMenuSeparator />
                <FilterMenuLabel>{i18nT('pages.chatSidebar.folder_order')}</FilterMenuLabel>
                {/* A switcher like Sort by, so the rows are `menuitemradio`s that
                    report `aria-checked` too. The group's `value` is the
                    COMMITTED mode (the cache), so the checked row tracks what is
                    actually in effect. The write stays on each row's `onSelect`,
                    NOT the group's `onValueChange`: a re-pick of a mode equal to
                    the group's committed value fires no change event, yet it must
                    still go out when a different pick is in flight (pick Name,
                    reopen, pick Custom while Name is still saving -- Custom must
                    be sent, not read as a no-op against the still-Custom cache). */}
                <DropdownMenuRadioGroup value={folderSortMode}>
                  {FOLDER_SORT_MODES.map(mode => (
                    <DropdownMenuRadioItem
                      key={mode}
                      value={mode}
                      data-testid={`folder-order-${mode}`}
                      // "Already the mode" is the value in flight while a save is
                      // pending (the newest pick, queued or on the wire), not the
                      // cache (which moves only when a save lands): pick Name,
                      // reopen, pick Custom -- Custom must go out, not be read as
                      // a no-op against the still-Custom cache. A pick that is a
                      // change goes into `folderSortMut`'s one-at-a-time queue.
                      onSelect={() => {
                        const current = folderSortMut.isPending ? folderSortMut.variables : folderSortMode
                        if (mode !== current) folderSortMut.mutate(mode)
                      }}
                    >
                      <span className="flex-1">{i18nT(FOLDER_SORT_LABEL_KEY[mode])}</span>
                      {folderSortMode === mode && <Check size={14} className="text-accent shrink-0" aria-hidden="true" />}
                    </DropdownMenuRadioItem>
                  ))}
                </DropdownMenuRadioGroup>
                {/* Outside Custom a folder drag can re-parent but not reorder
                    (the rows' droppable side is off). Said here, where the mode
                    is chosen, the same caption grammar as the stale control's
                    paused hint below -- and as a FACT about the modes, not the
                    sidebar hint's "Switch to Custom" sentence: that one sits
                    beside a button that does the switching, and the same words
                    here, two rows under the Custom row itself, read as an action
                    that does nothing. The drop itself speaks again when a drag
                    ends with nothing moved (`folderReorderHint`). Only where a
                    folder row is drawn to drag (`folderRowsDrawn`) -- a note
                    promising a reorder the flat and conductor lanes cannot offer
                    would mislead. */}
                {folderSortMode !== 'custom' && folderRowsDrawn && (
                  <div className="px-2 pt-0.5 pb-1.5 text-[11px] text-muted italic whitespace-normal" data-testid="folder-order-reorder-note">
                    {i18nT('pages.chatSidebar.folder_order_reorder_note')}
                  </div>
                )}
                {/* A folder from before the `created_at` stamp existed has no date, so
                    the created comparator puts it after every stamped row, in the
                    stored order -- on an existing tree that is the order the person
                    already had, and picking the mode looks like nothing happened.
                    The same fact-line pattern as the drag note, shown only while the
                    mode is active and such a folder is in the list (a fresh install
                    never sees it); every folder creator has stamped since the mode
                    shipped, so the line retires as those folders go. */}
                {folderSortMode === 'created' && folders.some(f => !folderHasCreatedStamp(f)) && (
                  // Bounded: the menu sizes to its widest child, and a one-line
                  // sentence this long would widen every row to it. Wrapped at the
                  // width the rows already have, it reads as the caption it is.
                  <div className="px-2 pt-0.5 pb-1.5 text-[11px] text-muted italic whitespace-normal max-w-[300px]" data-testid="folder-order-unstamped-note">
                    {i18nT('pages.chatSidebar.folder_order_unstamped_note')}
                  </div>
                )}
                <DropdownMenuSeparator />
                {/* Stale-session collapse threshold. Lives beside Sort rather than
                    in Settings: it shapes how this list reads, exactly like the
                    sort order, and the Recent filter's window picker set the
                    precedent for a duration control in this menu. Hidden in the
                    flat lane and on the board — those views render rows through
                    paths the collapse does not touch, and a control that
                    displays an active setting while doing nothing is a lie. */}
                {!flatLaneActive && !boardLaneActive && (() => {
                  // The trigger must not advertise "· 2d" while the collapse
                  // is inert (narrowed list / non-date sort): a control that
                  // displays an active setting while doing nothing is a lie.
                  const stalePaused = staleCollapseMs > 0 && (listNarrowed || sortKey !== 'date-desc')
                  const staleRowBody = (
                    <>
                      <Clock size={14} className="text-muted shrink-0" />
                      <span className="flex-1 truncate">
                        {i18nT('pages.chatSidebar.stale_collapse_menu')}
                        <span className="text-muted"> · {staleCollapseMs > 0
                          ? (stalePaused
                            ? i18nT('pages.chatSidebar.stale_collapse_paused')
                            : formatRecentWindow(staleCollapseMs))
                          : i18nT('pages.chatSidebar.stale_collapse_off')}</span>
                      </span>
                    </>
                  )
                  const staleLabel = (ms: number) => (ms > 0 ? formatRecentWindow(ms) : i18nT('pages.chatSidebar.stale_collapse_off'))
                  // Caption saying what the durations mean, mirroring the Recent
                  // submenu's "Within" caption above its presets. While paused the
                  // WHY must be readable without hover — the trigger's title
                  // tooltip is invisible to keyboard and touch users, so the hint
                  // renders in the picker too.
                  const staleCaption = (
                    <>
                      <div className="px-2 pt-1 pb-1 text-[11px] text-muted whitespace-normal">{i18nT('pages.chatSidebar.stale_collapse_caption')}</div>
                      {stalePaused && (
                        <div className="px-2 pb-1.5 text-[11px] text-muted italic whitespace-normal">{i18nT('pages.chatSidebar.stale_collapse_paused_hint')}</div>
                      )}
                    </>
                  )
                  if (isMobile) {
                    // Same reason as the Recent picker above: a flyout cannot fit
                    // beside a phone-width menu. The thresholds render inline as
                    // chips rather than as seven more menu rows, so the menu stays
                    // scannable and every option is one tap away.
                    return (
                      <>
                        {/* A section caption, NOT a menu row: inline, there is
                            nothing to tap here (the chips below carry the action),
                            so styling it like the tappable rows above would invite
                            a tap that does nothing. Matches FILTER / SORT BY. */}
                        <DropdownMenuLabel
                          className="text-[11px] uppercase tracking-[.04em] flex items-center gap-2"
                          data-testid="stale-collapse-menu"
                        >
                          {staleRowBody}
                        </DropdownMenuLabel>
                        {staleCaption}
                        <div className="flex flex-wrap gap-1 px-2 pb-1.5">
                          {STALE_COLLAPSE_PRESETS_MS.map(ms => (
                            <DurationChip
                              key={ms}
                              label={staleLabel(ms)}
                              selected={staleCollapseMs === ms}
                              onSelect={() => setStaleCollapseMs(ms)}
                            />
                          ))}
                        </div>
                      </>
                    )
                  }
                  return (
                <DropdownMenuSub>
                  <DropdownMenuSubTrigger data-testid="stale-collapse-menu"
                    title={stalePaused ? i18nT('pages.chatSidebar.stale_collapse_paused_hint') : undefined}>
                    {staleRowBody}
                    <ChevronRight size={13} className="text-muted shrink-0" />
                  </DropdownMenuSubTrigger>
                  <DropdownMenuSubContent className="min-w-[150px] max-w-[240px]">
                    {staleCaption}
                    {STALE_COLLAPSE_PRESETS_MS.map(ms => (
                      <DropdownMenuItem
                        key={ms}
                        onSelect={() => setStaleCollapseMs(ms)}
                      >
                        <span className="flex-1">{staleLabel(ms)}</span>
                        {staleCollapseMs === ms && <Check size={14} className="text-accent shrink-0" />}
                      </DropdownMenuItem>
                    ))}
                  </DropdownMenuSubContent>
                </DropdownMenuSub>
                  )
                })()}
                {/* Tags. Placed above Folders and NOT gated on the lane: tags are
                    a property of the session, so they mean the same thing in the
                    flat list, the folder tree and the board — and the board is
                    exactly where a phone user is most likely to want this, since
                    the columns scroll sideways there. Folders, by contrast, are a
                    list-view structure and stay hidden on the board. */}
                {tagFilterRows.length > 0 && (
                  <>
                    <DropdownMenuSeparator />
                    <FilterMenuLabel>
                      {i18nT('pages.chatSidebar.tags')}
                    </FilterMenuLabel>
                    {tagFilterRows.map(({ tag: t, count, selected }) => (
                      <DropdownMenuItem
                        key={t.id}
                        title={selected
                          ? i18nT('pages.chatSidebar.stop_filtering_by_tag', { name: t.name })
                          : i18nT('pages.chatSidebar.show_only_sessions_tagged', { name: t.name })}
                        // Keep the menu open so several tags can be selected.
                        onSelect={e => { e.preventDefault(); toggleTagFilter(t.id) }}
                        data-testid={`tag-filter-${t.id}`}
                        role="menuitemcheckbox"
                        aria-checked={selected}
                      >
                        <span
                          aria-hidden="true"
                          className="w-3.5 h-3.5 shrink-0 rounded-[3px] border flex items-center justify-center"
                          style={selected
                            ? { borderColor: t.color, background: t.color }
                            : { borderColor: 'var(--border)', background: 'transparent' }}
                        >
                          {selected && <Check size={10} strokeWidth={3} style={{ color: t.color === '#ffffff' ? '#000' : '#fff' }} />}
                        </span>
                        <span className="flex-1 truncate">{t.name}</span>
                        {/* 0 is rendered, not omitted: a zero-count tag is exactly
                            the one that blanks the list when selected. */}
                        <span className="text-muted text-[11px] shrink-0">{count}</span>
                      </DropdownMenuItem>
                    ))}
                  </>
                )}
                {/* Models. Lists only the models some session uses, most used
                    first, so the section is also the quickest read of what the
                    sessions run on. A session property like tags, so it is not
                    gated on the lane either. */}
                {modelFilterRows.length > 0 && (
                  <>
                    <DropdownMenuSeparator />
                    <FilterMenuLabel>
                      {i18nT('pages.chatSidebar.models')}
                    </FilterMenuLabel>
                    {/* The invert row sits FIRST, under the heading, so the mode
                        that changes what every tick below it means is read before
                        the ticks, not discovered at the bottom of a menu that may
                        be clipped on a short window. Always listed so the "every
                        session not on model X" option can be found before anything
                        is ticked, but disabled until a model is selected:
                        with nothing selected it would flip a flag that changes
                        nothing on screen. Enabled on the RESOLVED selection, so
                        a stored key no session uses cannot arm it either.
                        "Hide" names what the list does. */}
                    <>
                      <DropdownMenuItem
                        disabled={activeModelKeys.size === 0}
                        title={i18nT('pages.chatSidebar.hide_sessions_on_these_models_hint')}
                        onSelect={e => { e.preventDefault(); setModelFilterExcluded(!modelFilterInverted) }}
                        data-testid="model-filter-exclude"
                        role="menuitemcheckbox"
                        aria-checked={modelFilterInverted}
                      >
                        {/* The same checkbox square as the model rows: the on
                            state must be legible as a shape, not only as a
                            colour, and a reader who sees a tick knows a second
                            click clears it. */}
                        <span
                          aria-hidden="true"
                          className={`w-3.5 h-3.5 shrink-0 rounded-[3px] border flex items-center justify-center ${modelFilterInverted ? 'border-accent bg-accent text-accent-fg' : 'border-border bg-transparent'}`}
                        >
                          {modelFilterInverted && <Check size={10} strokeWidth={3} />}
                        </span>
                        <span className="flex-1 truncate">{i18nT('pages.chatSidebar.hide_sessions_on_these_models')}</span>
                        {/* A disabled row says why, in the row: a title tooltip is
                            not shown reliably on a disabled item. Once a model is
                            ticked the note is hidden, not removed: it is the widest
                            row in the menu, and dropping it would shrink the menu
                            and slide every row sideways under the pointer. */}
                        <span
                          className={`text-muted text-[11px] shrink-0 ${activeModelKeys.size === 0 ? '' : 'invisible'}`}
                          aria-hidden={activeModelKeys.size === 0 ? undefined : true}
                          data-testid="model-filter-exclude-note"
                        >{i18nT('pages.chatSidebar.tick_a_model_first')}</span>
                      </DropdownMenuItem>
                      {/* Set apart from the model rows: it is a mode, not another
                          model. */}
                      <DropdownMenuSeparator />
                    </>
                    {modelFilterRows.map(row => {
                      const name = modelFilterLabel(row)
                      // The tooltip names what the click DOES: with the selection
                      // inverted, ticking a row hides its sessions.
                      const unselectedTitle = modelFilterInverted
                        ? i18nT('pages.chatSidebar.hide_sessions_using_model', { name })
                        : i18nT('pages.chatSidebar.show_only_sessions_using_model', { name })
                      return (
                        <DropdownMenuItem
                          key={row.key}
                          title={row.selected ? i18nT('pages.chatSidebar.stop_filtering_by_model', { name }) : unselectedTitle}
                          // Keep the menu open so several models can be selected.
                          onSelect={e => { e.preventDefault(); toggleModelFilter(row.key, modelFilterRows.map(r => r.key)) }}
                          data-testid={`model-filter-row-${row.key}`}
                          role="menuitemcheckbox"
                          aria-checked={row.selected}
                        >
                          {/* The square always shows the tick for a selected row, so
                              a click's effect stays legible. Once the selection is
                              inverted the row reads as "hidden" through a muted name
                              and a trailing eye-off glyph (not a strike-through,
                              which reads as "deleted"). */}
                          <span
                            aria-hidden="true"
                            className={`w-3.5 h-3.5 shrink-0 rounded-[3px] border flex items-center justify-center ${row.selected ? 'border-accent bg-accent text-accent-fg' : 'border-border bg-transparent'}`}
                          >
                            {row.selected && <Check size={10} strokeWidth={3} />}
                          </span>
                          <span
                            className={`flex-1 truncate ${row.selected && modelFilterInverted ? 'text-muted' : ''}`}
                            data-excluded={row.selected && modelFilterInverted ? 'true' : undefined}
                          >{name}</span>
                          {row.selected && modelFilterInverted && <EyeOff size={12} className="text-muted shrink-0" aria-hidden="true" />}
                          <span className="text-muted text-[11px] shrink-0">{row.count}</span>
                        </DropdownMenuItem>
                      )
                    })}
                  </>
                )}
                {/* Folders sit LAST on purpose: the list grows with the user's
                    folder count, so anything below it would get pushed out of
                    easy reach. Being last, it can simply overflow into the
                    menu's own scroll (the DropdownMenuContent primitive caps to
                    the available viewport height and scrolls) with no inner
                    scroll region of its own. */}
                {/* Reachable in EVERY view, the board included. The folder hide applies
                    to the board's own population, so a person who hides a folder and
                    then switches to a board needs the control that reverses it where
                    they are standing: a board column has no folder header, so it carries
                    no reveal row either, and without this section the hide would have no
                    way back short of leaving board view. */}
                {folderFilterRows.length > 0 && (
                  <>
                    <DropdownMenuSeparator />
                    {/* The heading doubles as the shelve control: activating it
                        rolls the folder list up or down. It stays a menu item so
                        keyboard users reach it with the same arrow keys as every
                        other row, and preventDefault keeps the menu open. */}
                    <DropdownMenuItem
                      onSelect={e => { e.preventDefault(); toggleFoldersShelved() }}
                      data-testid="folder-filter-shelve"
                      aria-expanded={!foldersShelved}
                      title={foldersShelved ? i18nT('pages.chatSidebar.show_the_folder_list') : i18nT('pages.chatSidebar.roll_the_folder_list_up')}
                      className="text-[11px] uppercase tracking-[.04em] text-muted"
                    >
                      <DisclosureChevron open={!foldersShelved} size={12} />
                      <span className="flex-1">
                        {i18nT('pages.chatSidebar.folders')}
                        {hiddenFolderCount > 0 && (
                          <span className="normal-case tracking-normal"> &middot; {i18nT('pages.chatSidebar.hidden_folder_count', { count: hiddenFolderCount })}</span>
                        )}
                      </span>
                    </DropdownMenuItem>
                    {!foldersShelved && (
                      <>
                    {filterHiddenFolders.size > 0 && (
                      <DropdownMenuItem onSelect={e => { e.preventDefault(); showAllFolders() }} data-testid="folder-filter-show-all">
                        <RotateCcw size={12} className="text-muted shrink-0" />
                        <span className="flex-1">{i18nT('pages.chatSidebar.show_all_folders')}</span>
                      </DropdownMenuItem>
                    )}
                    {folderFilterRows.map(({ folder: f, depth, count, hidden, hiddenByAncestor }) => (
                      <DropdownMenuItem
                        key={f.id}
                        style={{ paddingLeft: `${8 + depth * 14}px` }}
                        title={hiddenByAncestor
                          ? i18nT('pages.chatSidebar.hidden_because_parent_hidden', { name: f.name })
                          : hidden ? i18nT('pages.chatSidebar.show_folder') : i18nT('pages.chatSidebar.hide_folder')}
                        // Keep the menu open so several folders can be toggled.
                        onSelect={e => { e.preventDefault(); toggleFolderFilter(f.id) }}
                        data-testid={`folder-filter-${f.id}`}
                        role="menuitemcheckbox"
                        aria-checked={!hidden && !hiddenByAncestor}
                      >
                        <span
                          aria-hidden="true"
                          className="w-3.5 h-3.5 shrink-0 rounded-[3px] border flex items-center justify-center"
                          style={hidden || hiddenByAncestor
                            ? { borderColor: 'var(--border)', background: 'transparent' }
                            : { borderColor: 'var(--accent)', background: 'var(--accent)' }}
                        >
                          {!hidden && !hiddenByAncestor && <Check size={10} className="text-accent-fg" strokeWidth={3} />}
                        </span>
                        <FolderGlyph color={f.color} icon={f.icon} size={12} className="shrink-0 text-muted" />
                        <span className={`flex-1 truncate${hiddenByAncestor ? ' opacity-50' : ''}`}>{f.name}</span>
                        {count > 0 && <span className="text-muted text-[11px] shrink-0">{count}</span>}
                      </DropdownMenuItem>
                    ))}
                      </>
                    )}
                  </>
                )}
              </FilterMenuContent>
            </DropdownMenu>
          </>
        )}
      />
  )
  const shelf = (
        <>
      {/* One aggregate chip in its OWN row, never per-tag chips in the row below.
          AUTOSDE max-two-buttons-per-row grandfathers that row's existing filter
          chips but forbids growing it, and per-tag chips grow it without bound.
          Tag colours survive as spans inside this single control. */}
      {activeTagIds.size > 0 && (
        <div className="px-3 pb-1">
          <button
            type="button"
            data-testid="tag-filter-chip"
            className="inline-flex items-center gap-1 max-w-full pl-2 pr-1 py-0.5 rounded-full text-[11px] cursor-pointer transition-colors bg-bg-elevated/60 border border-border text-muted hover:text-text"
            onClick={clearTagFilter}
            title={i18nT('pages.chatSidebar.clear_named_filter', { filter: fmtList(activeTagNames, { type: 'disjunction' }) })}
            aria-label={i18nT('pages.chatSidebar.clear_named_filter', { filter: fmtList(activeTagNames, { type: 'disjunction' }) })}
          >
            {/* Swatch carries the colour, the name stays in body text: a pale
                tag on this surface can fall near 2:1 contrast at 11px. */}
            <span className="truncate inline-flex items-center gap-1.5">
              {tagFilterRows.filter(({ tag: t }) => activeTagIds.has(t.id)).map(({ tag: t }) => (
                <span key={t.id} className="inline-flex items-center gap-1">
                  <span
                    aria-hidden="true"
                    className="w-2 h-2 shrink-0 rounded-full border border-border"
                    style={{ background: t.color }}
                  />
                  {t.name}
                </span>
              ))}
            </span>
            <X size={11} className="shrink-0" />
          </button>
        </div>
      )}
      {/* The model filter's one chip, in its own row for the same AUTOSDE reason
          as the tag chip above. The label carries the direction: "Not Fable 5"
          when the selection is inverted, else the model names. */}
      {activeModelRows.length > 0 && (() => {
        const models = fmtList(activeModelRows.map(row => modelFilterLabel(row)), { type: 'disjunction' })
        const label = modelFilterInverted ? i18nT('pages.chatSidebar.not_models', { models }) : models
        return (
          <div className="px-3 pb-1">
            <FilterChip
              aggregate
              testId="model-filter-chip"
              label={label}
              clearLabel={i18nT('pages.chatSidebar.clear_named_filter', { filter: label })}
              onClear={clearModelFilter}
            />
          </div>
        )
      })()}
      {activeFilters.size > 0 && (
        <div className={FILTER_CHIP_ROW_CLS}>
          {SESSION_FILTERS.filter(filterDef => activeFilters.has(filterDef.key)).map(filterDef => {
            const slotCount = filterCounts[filterDef.key] ?? 0
            const filterLabel = i18nT(FILTER_LABEL_KEY[filterDef.key])
            // The label goes in as-is. It used to be `.toLowerCase()`d to read as
            // mid-sentence English, which does not survive translation: German
            // nouns are capitalised, CJK has no case, and Turkish lowercases `I`
            // to a dotless `ı`.
            const clearLabel = i18nT('pages.chatSidebar.clear_named_filter', { filter: filterLabel })
            return (
              <FilterChip
                key={filterDef.key}
                testId={`filter-chip-${filterDef.key}`}
                label={`${filterLabel}${filterDef.key === 'recent' ? ` · ${formatRecentWindow(recentWindowMs)}` : ''}${slotCount > 0 ? ` (${slotCount})` : ''}`}
                color={filterDef.color}
                paused={filtersPaused}
                clearLabel={clearLabel}
                onClear={() => toggleFilter(filterDef.key)}
              />
            )
          })}
        </div>
      )}
      {/* The hides, as ONE aggregate chip in their own row, for the same reason as
          the tag chip: the row above holds the base branch's filter chips and must
          not grow, and a chip per hidden status would add up to three more buttons
          to it. Neutral like every clear-several pill (`FilterChip` doc): the one
          click drops every hide at once. Counts ride inside the label the way the
          roster's chip carries its own, and the accessible name says what the
          click does. After the include chips so the strip reads what is kept,
          then what is dropped. */}
      {hiddenFilters.size > 0 && (
        <div className="px-3 pb-1">
          <FilterChip
            aggregate
            label={i18nT('pages.chatSidebar.hiding_named_filter', {
              filter: fmtList(
                SESSION_FILTERS.filter(filterDef => hiddenFilters.has(filterDef.key)).map(filterDef => {
                  const slotCount = filterCounts[filterDef.key] ?? 0
                  return `${i18nT(FILTER_LABEL_KEY[filterDef.key])}${slotCount > 0 ? ` (${slotCount})` : ''}`
                }),
                { type: 'unit', style: 'short' },
              ),
            })}
            clearLabel={i18nT('pages.chatSidebar.filter_unhide_named', {
              filter: fmtList(SESSION_FILTERS.filter(filterDef => hiddenFilters.has(filterDef.key)).map(filterDef => i18nT(FILTER_LABEL_KEY[filterDef.key]))),
            })}
            onClear={clearHiddenFilters}
            testId="hidden-filter-chip"
          />
        </div>
      )}
      {seedError && (
        /* Outside the layout branches on purpose. A TOTAL seed failure leaves
         * zero columns, so the board branch never renders — a banner inside it
         * would be invisible in exactly the case it exists for, while the
         * toggle has already flipped and the user is looking at a list.
         * askAgent: the seed writes derived lanes, no draft in the sidebar.
         * Retry is a separate button, not the notice itself. */
        <div className="mx-2 mt-2 flex flex-col gap-1 shrink-0">
          <ErrorNotice
            title={toggleSaveFailed ? undefined : i18nT('pages.chatSidebar.lane_seed_failed')}
            message={seedError}
            askAgent
            onDismiss={() => { setSeedError(''); setToggleSaveFailed(false) }}
            testId="lane-seed-error"
          />
          {/* The Retry button seeds state lanes — the recovery for a SEED
           * failure. It must not show under a toggle-SAVE failure: there its
           * "Try again" label would promise to retry the save but instead seed
           * lanes into a board the user never switched to (UX Review). The
           * save-failure copy already carries its own next step. */}
          {!toggleSaveFailed && (
            <div>
              <Btn
                type="button"
                className="text-[12px] px-2 py-0.5"
                onClick={() => { setSeedError(''); seedStateLanesMutation.mutate() }}
              >
                {i18nT('pages.chatSidebar.lane_seed_retry')}
              </Btn>
            </div>
          )}
        </div>
      )}
      {/* Yields to the preview's instance-sessions banner, which already reports
       *  a failed ['instances'] read when the preview flag is on. */}
      {hasRemoteExecutedRow && !remoteSessionsError && <RemoteCrewNamesError />}
      {/* Read failures for the two lists this pane is built from. Same placement
       *  rationale as the seed banner: a failed folders query means no folder
       *  tree, a failed columns query means no board, so neither branch can host
       *  its own notice. Both are pure reads — askAgent on, Retry via refetch. */}
      {foldersFailed && (
        <div className="mx-2 mt-2 flex flex-col gap-1 shrink-0">
          <ErrorNotice
            title={i18nT('pages.chatSidebar.folders_load_failed')}
            message={(errMessage(foldersError) || i18nT('components.errorBoundary.something_went_wrong'))}
            askAgent
            testId="chat-folders-error"
          />
          <div>
            <Btn type="button" className="text-[12px] px-2 py-0.5" onClick={() => void refetchFolders()}>
              {i18nT('pages.chatSidebar.retry')}
            </Btn>
          </div>
        </div>
      )}
      {columnsFailed && (
        <div className="mx-2 mt-2 flex flex-col gap-1 shrink-0">
          <ErrorNotice
            title={i18nT('pages.chatSidebar.columns_load_failed')}
            message={(errMessage(columnsError) || i18nT('components.errorBoundary.something_went_wrong'))}
            askAgent
            testId="tag-columns-error"
          />
          <div>
            <Btn type="button" className="text-[12px] px-2 py-0.5" onClick={() => void refetchColumns()}>
              {i18nT('pages.chatSidebar.retry')}
            </Btn>
          </div>
        </div>
      )}
      {/* Folder writes (create / delete / update) and local "New chat" creates.
       *  Inputs are already persisted or were never typed (a create menu pick),
       *  so the hand-off loses nothing. Dismissable: the failure is a moment, not
       *  a state — the caches have already been re-synced. */}
      {/* A refused single-session close. Dismissable: the session is simply still
       *  open, and closing it again is the retry. A "still saving" refusal clears
       *  on its own, so it offers no agent hand-off; any other failure does. */}
      <ErrorNotice
        title={i18nT('pages.chatSidebar.close_refused_title')}
        message={closeRefused
          ? i18nT(closeRefused.reason === 'historyWrite' ? 'pages.chatSidebar.close_refused' : 'pages.chatSidebar.close_refused_other', { title: closeRefused.title })
          : null}
        askAgent={closeRefused?.reason === 'failed'}
        onDismiss={() => dispatch(setCloseRefused(null))}
        className="mx-2 mt-2 shrink-0"
        testId="close-refused"
      />
      {/* An offline refusal is not a failure: no request was sent, so the danger
        *  surface below would dress a non-event as one. Neutral, and
        *  `role="status"` so it is announced without an alert's interrupt. */}
      {folderOfflineNotice && (
        <div role="status" className="mx-2 mt-2 shrink-0 rounded-lg border border-border bg-bg-elevated px-3 py-2 flex items-start gap-2 text-[13px] text-muted" data-testid="folder-action-offline">
          <WifiOff size={14} className="mt-[2px] shrink-0" aria-hidden="true" />
          {/* Same wrapping row as the notice beside it: at sidebar width an
            *  inline dismiss squeezed the sentence to a word per line. */}
          <div className="min-w-0 flex-1 flex flex-wrap items-start gap-x-2 gap-y-1">
            <div className="min-w-0 grow basis-[12rem] whitespace-pre-wrap" style={{ overflowWrap: 'anywhere' }}>
              {folderOfflineNotice}
            </div>
          </div>
          <IconButton
            className="shrink-0"
            aria-label={i18nT('components.errorNotice.dismiss')}
            onClick={() => setFolderOfflineNotice('')}
          >
            <X size={14} aria-hidden="true" />
          </IconButton>
        </div>
      )}
      {/* The Ask-the-agent hand-off is withheld offline: its composer cannot send. */}
      <ErrorNotice
        title={i18nT('pages.chatSidebar.folder_update_failed')}
        message={folderActionError}
        askAgent={connected}
        onDismiss={() => setFolderActionError('')}
        className="mx-2 mt-2 shrink-0"
        testId="folder-action-error"
      />
      {/* A session sort pick the gateway refused. The list is already back on the
          stored order, so the footer names the refused sort and the order now
          drawn (`sortKey`); the server's text sits under the title as the detail. The
          hand-off is withheld offline and stacked below in this narrow column. */}
      <ErrorNotice
        title={i18nT('pages.chatSidebar.sort_update_failed')}
        message={sortError?.message ?? ''}
        messagePlacement="below"
        footer={sortError ? i18nT('pages.chatSidebar.sort_update_failed_detail', { sort: i18nT(SORT_LABEL_KEY[sortError.key]), restored: i18nT(SORT_LABEL_KEY[sortKey]) }) : null}
        askAgent={connected}
        actionPlacement="below"
        onDismiss={dismissSortError}
        className="mx-2 mt-2 shrink-0"
        testId="session-sort-error"
      />
      {/* The folder order (dashboard.folder_sort) could not be read AND there is
       *  no body to fall back on: the tree is drawn in the stored order meanwhile
       *  and sibling drags are withdrawn, so the person is told why the order they
       *  chose is not the one they see -- and that nothing is asked of them (the
       *  read retries on its own). Not dismissable: it is a state, not a moment.
       *  Keyed on the LATCHED failure, not the query status, so a retry's pending
       *  phase does not unmount it; it clears when a body arrives. A failed
       *  background refetch with a body on hand says nothing here. */}
      {folderSortRead.error !== null && (
        <ErrorNotice
          title={i18nT('pages.chatSidebar.folder_order_unavailable')}
          message={folderSortRead.error}
          // The server's own words ("config store", "gateway") stay the message --
          // they are the journal key the hand-off reads -- but under the title as
          // a smaller line, not as the lead: the title says what happened.
          messagePlacement="below"
          footer={i18nT('pages.chatSidebar.folder_order_unavailable_detail')}
          askAgent
          // Under the text, not beside it: a sibling column in this ~300px
          // panel left the title and server string one or two words a line.
          actionPlacement="below"
          className="mx-2 mt-2 shrink-0"
          testId="folder-order-unavailable"
        />
      )}
      {/* A folder drag just ended with nothing moved because the drawn order is
       *  not the stored one (see `folderReorderHint`). Status, not error: no
       *  request failed and nothing is broken -- the gesture is withdrawn in this
       *  mode, and this line says where it comes back and offers the way there:
       *  the action writes Custom through the same path the menu row does, so the
       *  person who just tried to reorder does not have to find the menu. It stays
       *  until the person does something else (no clock takes the action away).
       *  Announced live so a keyboard drag gets the same answer a pointer drag does. */}
      {folderReorderHint && (
        <div
          ref={folderReorderHintRef}
          role="status"
          className="mx-2 mt-2 shrink-0 rounded-lg border border-border bg-bg-elevated px-3 py-2 text-[12px] text-muted"
          data-testid="folder-reorder-hint"
        >
          <div className="flex items-center gap-2">
            <ArrowUpDown size={14} className="shrink-0" aria-hidden="true" />
            <span className="min-w-0 flex-1">{i18nT('pages.chatSidebar.folder_order_reorder_hint')}</span>
          </div>
          {/* On its own line, aligned with the sentence: beside it, the two fight
              for the sidebar's width and the sentence wraps to a word a line.
              The shared primitive, restyled to a link: its disabled state (the
              PATCH in flight) then reads disabled instead of keeping the accent
              and the pointer cursor. */}
          <Btn
            className="mt-1 ml-[22px] p-0 border-none bg-transparent rounded-none text-accent hover:bg-transparent hover:underline text-[12px] font-medium active:scale-100"
            data-testid="folder-reorder-hint-switch"
            disabled={folderSortMut.isPending}
            onClick={() => { if (folderSortMode !== 'custom') folderSortMut.mutate('custom') }}
          >
            {i18nT('pages.chatSidebar.folder_order_switch_custom')}
          </Btn>
        </div>
      )}
      <ErrorNotice
        title={i18nT('pages.chatSidebar.pinned_order_save_failed')}
        message={pinnedOrderError}
        // The message (what happened to the order) sits under the title and
        // the action under both, as on folder-order-unavailable: side by side
        // in this ~300px panel they wrapped to one or two words a line.
        messagePlacement="below"
        askAgent
        actionPlacement="below"
        onDismiss={() => setPinnedOrderError('')}
        className="mx-2 mt-2 shrink-0"
        testId="pinned-order-error"
      />
      <ErrorNotice
        message={newChatError}
        askAgent
        onDismiss={() => setNewChatError('')}
        className="mx-2 mt-2 shrink-0"
        testId="new-chat-error"
      />
      {/* A refused rename: the editor is already closed and the title has been
       *  reverted to the server value by the recovery refetch, so there is no
       *  unsaved draft left to lose and the hand-off is safe. Dismissable: the
       *  failure is a moment, not a state. */}
      <ErrorNotice
        title={i18nT('pages.chatPage.could_not_rename_session')}
        message={renameError}
        askAgent
        onDismiss={() => setRenameError('')}
        className="mx-2 mt-2 shrink-0"
        testId="rename-error"
      />
      <ErrorNotice
        title={i18nT('pages.chatPage.could_not_generate_title')}
        message={autoTitleError}
        askAgent
        // Below, not beside: at sidebar width a side link squeezes the
        // message into a one-word column.
        actionPlacement="below"
        onDismiss={() => setAutoTitleError('')}
        className="mx-2 mt-2 shrink-0"
        testId="auto-title-error"
      />
        {/* An instance that is CONNECTED but did not answer contributes no rows.
          *  Saying so is the difference between "that instance has nothing open" and
          *  "we could not ask": without this line the list silently claims a
          *  completeness it does not have, which is worse than showing fewer rows.
          *  Placed ABOVE the view branches so it appears in the flat lane, the board
          *  and the folder tree alike — an unreachable peer is not a property of one
          *  layout. Non-blocking by design: local rows are unaffected. */}
        {instanceSessions.loading && (
          /* The same honesty rule as the notice below, for the window BEFORE any
           *  peer has answered: remote rows land seconds after mount, so a list
           *  that stays silent until then reads as complete while it is not, and
           *  the arriving rows shift the list under a scan already in progress.
           *  Muted single line in the same slot, so the two states cannot stack
           *  into competing banners. */
          <div className="mx-2 mt-2 px-2 py-1.5 text-[11px] text-muted flex items-center gap-1.5">
            <Server size={11} aria-hidden="true" className="shrink-0" />
            <span className="min-w-0 truncate">
              {i18nT('pages.chatSidebar.checking_remote_instances')}
            </span>
          </div>
        )}
        {remoteSessionsError && (
          /* A peer read that failed, through the one shared error surface — see
           *  `remoteSessionsError` for how the two failing reads collapse into it.
           *  `askAgent` is ON: this is a LIST read, so the hand-off's navigation
           *  destroys no unsaved state, and a tunnel that stopped answering is
           *  exactly the class of failure the user cannot fix by hand but the
           *  agent often can. Not dismissible: the condition is live, so a
           *  dismissed banner would reappear on the next 15s refetch.
           *  `shrink-0` matches the new-chat notice above it — the rail is a
           *  flex column and a growable banner would eat the list's height. */
          <ErrorNotice
            title={remoteSessionsError.title}
            message={remoteSessionsError.message}
            askAgent
            actionPlacement="below"
            className="mx-2 mt-2 shrink-0"
            testId="instance-sessions-error"
          />
        )}
        </>
  )
  return { field, shelf }
}

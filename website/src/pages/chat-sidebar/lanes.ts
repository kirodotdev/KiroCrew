/** Which lane renders: the persisted lane preference, the lane actually drawn, and the
 *  header's lane cycle. */
import { useState, useCallback, useMemo } from 'react'
import { safeSetItem } from '../../utils/safeStorage'
import type { SidebarLane } from './types'
import { readStoredLane, SIDEBAR_LANE_LS_KEY, FLAT_VIEW_LS_KEY } from './persistence'
import { i18nT } from '../../i18n/t'
import type { ChatFolder } from '../../types'

/** The persisted lane preference and its two setters (remembered, per visit). */
export function useSidebarLane() {
  // Flat view: temporarily explode every chat out of its folder into one
  // recency-sorted list, for working temporally across many folders ("what's
  // the latest?"). Pure view projection — folder membership is untouched, and
  // toggling back restores the folder tree exactly as it was.
  const [lane, setLane] = useState<SidebarLane>(readStoredLane)
  /** Change the lane AND remember it. The ordinary path. */
  const setLanePersisted = useCallback((next: SidebarLane) => {
    setLane(next)
    safeSetItem(SIDEBAR_LANE_LS_KEY, next)
    // The legacy key is kept in step so a rollback to a build that only reads it
    // lands the user in the same lane rather than a surprising one.
    safeSetItem(FLAT_VIEW_LS_KEY, next === 'flat' ? '1' : '0')
  }, [])
  /** Change the lane for THIS VISIT only, leaving the preference alone. Used by the
   *  folder reveal, which has to leave a lane that renders no folder rows without
   *  rewriting which lane the user opens the app in. */
  const setLaneForVisit = useCallback((next: SidebarLane) => { setLane(next) }, [])
  /** Back-compat shim for the reveal effect, which only ever turns flat view OFF. */
  const setFlatView = useCallback((on: boolean) => {
    if (!on) setLaneForVisit('tree')
  }, [setLaneForVisit])
  return { lane, setLanePersisted, setFlatView }
}

/** What the session list actually renders. The preference picks among the
 *  `SidebarLane`s; the tag-column board is a separate axis that PREEMPTS that choice
 *  whenever any column is configured, and reads the flat preference as "no folder
 *  blocks inside the columns". */
export type RenderedLane = SidebarLane | 'board' | 'board-flat'

/** The lane on screen, from the preference and what each lane needs to draw something:
 *  the board needs a column, `conductor` an edge (no row carries a creator otherwise,
 *  and the lane would be the flat list with a chevron nowhere), and `flat` a folder to
 *  explode. A preference whose condition is missing falls back to the folder tree, the
 *  one lane that can always render. */
export function renderedLane({ lane, boardColumns, folders, lineageAvailable }: {
  lane: SidebarLane
  boardColumns: number
  folders: number
  lineageAvailable: boolean
}): RenderedLane {
  if (boardColumns > 0) return lane === 'flat' ? 'board-flat' : 'board'
  if (lane === 'conductor' && lineageAvailable) return 'conductor'
  if (lane === 'flat' && folders > 0) return 'flat'
  return 'tree'
}

/** The lanes that can render, the next one, and the header button that cycles them. */
export function useLaneCycle({ lineageAvailable, boardLaneActive, folders, lane, setLanePersisted }: {
  lineageAvailable: boolean
  boardLaneActive: boolean
  folders: ChatFolder[]
  lane: SidebarLane
  setLanePersisted: (next: SidebarLane) => void
}) {
  /**
   * The lanes that can actually render something, in cycle order.
   *
   * `tree` always can. `conductor` needs at least one edge -- the crew log can be off,
   * and then no row will ever carry a parent, so the lane would be the flat list with
   * a chevron nowhere. `flat` needs folders, which is the pre-existing rule. Offering
   * a lane that renders identically to another is a dead position in the cycle, and the
   * user has to press through it.
   */
  const availableLanes = useMemo<SidebarLane[]>(() => {
    const out: SidebarLane[] = ['tree']
    // Gated on the board too. `conductorLaneActive` is `!boardLaneActive && ...`, so
    // with a board configured the lane cannot render and the press would change only
    // the button's icon -- a position in the cycle that visibly does nothing. Flat is
    // different and stays offered: it has a real in-column meaning.
    if (lineageAvailable && !boardLaneActive) out.push('conductor')
    if (folders.length > 0) out.push('flat')
    return out
  }, [lineageAvailable, boardLaneActive, folders.length])

  /** Where the next press goes. The button's copy is derived from THIS rather than
   *  from the current lane: the control's job is to say what it will do, and naming
   *  the lane you are already in sends a screen-reader user somewhere else.
   *
   *  Advanced from the lane actually RENDERED, which is not always the stored one. A
   *  stored lane whose conditions went away (a board got configured, the edges or the
   *  folders went) is not in `availableLanes`, and indexing it directly gives -1, whose
   *  successor is position 0 -- `tree`, the very thing already on screen. That is the
   *  no-op this derivation exists to avoid, so an unavailable stored lane advances from
   *  `tree` instead and the press lands somewhere visibly different. */
  const nextLane = useMemo<SidebarLane>(() => {
    const effective: SidebarLane = availableLanes.includes(lane) ? lane : 'tree'
    const at = availableLanes.indexOf(effective)
    return availableLanes[(at + 1) % availableLanes.length]
  }, [availableLanes, lane])

  /** The toggle's tooltip and aria-label: what the next press DOES.
   *
   *  Recomputed per render rather than memoized on purpose -- the strings come from
   *  `i18nT`, and a memo keyed on the lane alone would keep serving the previous
   *  language's copy after a switch. */
  const laneSwitchLabel = nextLane === 'conductor'
    ? i18nT('pages.chatSidebar.switch_to_conductor_view_nested_by_creator')
    : nextLane === 'flat'
      ? (boardLaneActive
        ? i18nT('pages.chatSidebar.switch_to_flat_view_hide_folders_in_board_columns')
        : i18nT('pages.chatSidebar.switch_to_flat_view_all_chats_without_folders'))
      : (boardLaneActive
        ? i18nT('pages.chatSidebar.show_folders_in_board_columns')
        : i18nT('pages.chatSidebar.switch_to_folder_view'))

  /** The single header button: tree -> conductor -> flat -> tree, skipping any lane
   *  that cannot render. Named for what it does now; there is no segmented control,
   *  because the sidebar's chrome stays Raycast-plain. */
  const cycleLane = useCallback(() => {
    setLanePersisted(nextLane)
  }, [nextLane, setLanePersisted])
  return { availableLanes, nextLane, laneSwitchLabel, cycleLane }
}

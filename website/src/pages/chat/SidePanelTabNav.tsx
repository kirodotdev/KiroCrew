/**
 * The menu is built from the same lists as the chips so it cannot drift from them.
 * Chips are found by `TAB_CHIP_ID_ATTR` and not a ref map, because the
 * `Reorder.Item` and close-menu wrappers around dynamic chips do not forward refs.
 */
import { useCallback, useLayoutEffect, useMemo, useRef, type ReactNode } from 'react'
import type { AllTabsGroup, AllTabsRow } from '../../components/AllTabsMenu'
import { useWheelToHorizontal } from '../../hooks/useWheelToHorizontal'
import type { PanelTab } from '../../hooks/usePanelTabs'
import { useTerminalTitle } from '../../utils/terminalRegistry'
import { revealChip } from './sidePanelReveal'
import { makeRelative } from '../../components/FilePickerMenu'

export const TAB_CHIP_ID_ATTR = 'data-tab-chip-id'

/** Live terminal tab title: subscribes to the session's title (running command
 *  or cwd basename) and falls back to the tab's default title until the first
 *  frame arrives. Module-scope so it isn't redefined per render. */
export function TerminalTabTitle({ sessionId, fallback }: { sessionId: string; fallback: string }) {
  const live = useTerminalTitle(sessionId)
  return <>{live || fallback}</>
}

export interface NavLeadingTab {
  id: string
  title: string
  icon: ReactNode
  badge?: ReactNode
}

interface Args {
  leadingTabs: readonly NavLeadingTab[] | undefined
  pinnedTabs: readonly PanelTab[]
  dynamicTabs: readonly PanelTab[]
  activeId: string | null
  /** Switches tab through the leave guard (`onBeforeLeave`). */
  requestActive: (id: string, currentId: string | null) => Promise<void>
  glyphFor: (tab: PanelTab) => ReactNode
  /** Existing edge-cue refs from `useScrollEdges`, composed rather than replaced. */
  attachFixedEdges: (node: HTMLDivElement | null) => void
  attachTabEdges: (node: HTMLUListElement | null) => void
  /** Root that row paths are shown relative to. */
  projectDir?: string
}

export function useSidePanelTabNav({
  leadingTabs, pinnedTabs, dynamicTabs, activeId, requestActive, glyphFor, attachFixedEdges, attachTabEdges, projectDir,
}: Args) {
  const fixedEl = useRef<HTMLDivElement | null>(null)
  const dynamicEl = useRef<HTMLUListElement | null>(null)
  const wheelFixed = useWheelToHorizontal<HTMLDivElement>()
  const wheelDynamic = useWheelToHorizontal<HTMLUListElement>()

  const fixedScrollerRef = useCallback((node: HTMLDivElement | null) => {
    fixedEl.current = node
    attachFixedEdges(node)
    wheelFixed(node)
  }, [attachFixedEdges, wheelFixed])
  const dynamicScrollerRef = useCallback((node: HTMLUListElement | null) => {
    dynamicEl.current = node
    attachTabEdges(node)
    wheelDynamic(node)
  }, [attachTabEdges, wheelDynamic])

  const closableIds = useMemo(() => new Set(dynamicTabs.map(t => t.id)), [dynamicTabs])

  const reveal = useCallback((id: string) => {
    for (const scroller of [fixedEl.current, dynamicEl.current]) {
      if (!scroller) continue
      // An id is a file path and can hold any character, so match in JS instead of building a selector.
      for (const chip of scroller.querySelectorAll<HTMLElement>(`[${TAB_CHIP_ID_ATTR}]`)) {
        if (chip.getAttribute(TAB_CHIP_ID_ATTR) !== id) continue
        revealChip(scroller, chip, closableIds.has(id))
        return
      }
    }
  }, [closableIds])

  // Runs on every change of the effective active id, whatever caused it. A
  // no-op for a chip already inside, and keyed on the id alone so a list change
  // that leaves the active tab as it is never undoes the user's own scrolling.
  const revealRef = useRef(reveal)
  revealRef.current = reveal
  useLayoutEffect(() => {
    if (activeId != null) revealRef.current(activeId)
  }, [activeId])

  const groups = useMemo<AllTabsGroup[]>(() => {
    const fromTab = (t: PanelTab): AllTabsRow => {
      // A file at the project root has a relative path equal to its title; a second line would repeat it.
      const relative = t.path ? makeRelative(t.path, projectDir ?? '') : undefined
      return {
        id: t.id,
        glyph: glyphFor(t),
        title: t.kind === 'terminal' && t.sessionId ? <TerminalTabTitle sessionId={t.sessionId} fallback={t.title} /> : t.title,
        secondary: relative !== t.title ? relative : undefined,
        tooltip: t.path ?? t.title,
      }
    }
    return [
      {
        id: 'leading',
        rows: (leadingTabs ?? []).map(lt => ({ id: lt.id, glyph: lt.icon, title: lt.title, badge: lt.badge, tooltip: lt.title })),
      },
      { id: 'pinned', rows: pinnedTabs.map(fromTab) },
      { id: 'dynamic', rows: dynamicTabs.map(fromTab) },
    ]
  }, [leadingTabs, pinnedTabs, dynamicTabs, glyphFor, projectDir])

  // `requestActive` returns early on the same id, so a pick of the current tab
  // reveals its chip directly: a resize or manual scroll may have clipped it.
  const onSelect = useCallback((id: string) => {
    if (id === activeId) { reveal(id); return }
    void requestActive(id, activeId)
  }, [activeId, reveal, requestActive])

  return { fixedScrollerRef, dynamicScrollerRef, groups, onSelect }
}

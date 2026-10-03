import { useContext, useEffect, type RefObject, type KeyboardEvent } from 'react'
import { WorkspaceFullscreenContext } from '../components/WorkspacePanelContext'
import { useDocumentImeLatch } from './useImeGuard'
import { useFocusMode } from './useFocusMode'

/**
 * Shell children painted above workspace fullscreen, so they keep taking input.
 * `data-above-workspace-fullscreen` marks a floating window that must stay in
 * reach over the panel: the computer-use live view, which shows and controls an
 * agent driving the real desktop. It is non-modal, so it never claims Escape.
 */
function paintsAboveFullscreen(element: HTMLElement): boolean {
  if (element.matches('header') || element.hasAttribute('data-above-workspace-fullscreen')) return true
  return element.classList.contains('focus-peek-top') || element.classList.contains('focus-peek-rail')
}

/** The shell's nav rail: covered by fullscreen when docked, painted above it in focus mode. */
function isShellRail(element: HTMLElement): boolean {
  return element.classList.contains('focus-chrome-rail')
}

/** Hidden background tabs and covered chrome cannot own the active Escape. */
function hasNestedWorkspaceOverlay(panel: HTMLElement | null): boolean {
  return Array.from(document.querySelectorAll<HTMLElement>(
    '[role="dialog"]:not([data-above-workspace-fullscreen]), [role="alertdialog"], [role="menu"], [data-workspace-escape-owner]',
  )).some(overlay => {
    const marker = overlay.hasAttribute('data-workspace-escape-owner')
    const ownerId = overlay.getAttribute('data-workspace-escape-owner')
    // Annotation composers portal to body, so DOM containment alone cannot
    // associate them with the workspace that must yield its Escape handling.
    if (marker && !panel?.contains(overlay) && (!ownerId || ownerId !== panel?.id)) return false
    // Empty markers are hidden sentinels inside a preview. An explicitly owned
    // portal is a real element whose own visibility must also be checked.
    let element: HTMLElement | null = marker && !ownerId ? overlay.parentElement : overlay
    while (element) {
      if (element.hidden || element.inert || element.getAttribute('aria-hidden') === 'true') return false
      const style = getComputedStyle(element)
      if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false
      element = element.parentElement
    }
    return true
  })
}

/** Expand the existing panel without moving its editors or terminal DOM. */
export function useWorkspaceFullscreenPanel(ref: RefObject<HTMLDivElement | null>) {
  const controls = useContext(WorkspaceFullscreenContext)
  const fullscreen = controls?.fullscreen ?? false
  const exit = controls?.exit
  const toggle = controls?.toggle
  const ime = useDocumentImeLatch(fullscreen)
  const { enabled: focusMode } = useFocusMode()

  useEffect(() => {
    const panel = ref.current
    if (!fullscreen || !panel) return
    const previousFocus = document.activeElement as HTMLElement | null
    const hidden: { element: HTMLElement; inert: boolean }[] = []
    // Keep the panel controls reachable while removing covered chrome and chat
    // from keyboard navigation. Walking ancestors also covers the mobile host.
    // At the dashboard shell the panel's host is a sibling of the content grid
    // AND of the topbar. Fullscreen covers the content rows, never the 42px
    // topbar, so the shell's header keeps taking input while the covered
    // content grid, rail and decor go inert; the computer-use live view floats
    // above the panel and stays in reach too. In focus mode the header and the
    // rail are overlays painted above the panel, summoned by the peek
    // triggers, so those stay reachable too. The rail is one element across a
    // focus-mode flip, so the effect below decides it and re-runs on the flip.
    let branch: HTMLElement = panel
    while (branch.parentElement) {
      const parent = branch.parentElement
      const atShell = parent.matches('[data-testid="dashboard-shell"]')
      for (const sibling of Array.from(parent.children)) {
        if (!(sibling instanceof HTMLElement) || sibling === branch) continue
        if (atShell && (paintsAboveFullscreen(sibling) || isShellRail(sibling))) continue
        hidden.push({ element: sibling, inert: sibling.inert })
        sibling.inert = true
      }
      if (atShell) break
      branch = parent
    }
    if (!panel.contains(previousFocus)) panel.focus({ preventScroll: true })
    return () => {
      hidden.forEach(({ element, inert }) => { element.inert = inert })
      const currentFocus = document.activeElement
      // A click on another control chooses the next focus target deliberately.
      if (previousFocus?.isConnected && (currentFocus === document.body || panel.contains(currentFocus))) {
        previousFocus.focus({ preventScroll: true })
      }
    }
  }, [fullscreen, ref])

  useEffect(() => {
    const shell = ref.current?.closest('[data-testid="dashboard-shell"]')
    if (!fullscreen || focusMode || !shell) return
    const rails = Array.from(shell.children).filter((child): child is HTMLElement => child instanceof HTMLElement && isShellRail(child))
    const previous = rails.map(rail => rail.inert)
    rails.forEach(rail => { rail.inert = true })
    return () => rails.forEach((rail, i) => { rail.inert = previous[i] })
  }, [fullscreen, focusMode, ref])

  useEffect(() => {
    if (!fullscreen) return
    // Escape inside the panel reaches the panel's own handler after nested
    // editors and menus decline it. A focused terminal consumes Escape before
    // this listener, preserving the key for vim, less, and other PTY programs.
    const onOutsideKey = (event: globalThis.KeyboardEvent) => {
      if (event.key !== 'Escape' || event.defaultPrevented || hasNestedWorkspaceOverlay(ref.current)
        || (event.target instanceof Node && ref.current?.contains(event.target))) return
      if (!ime.claimKey(event)) return
      event.preventDefault()
      event.stopPropagation()
      exit?.()
    }
    window.addEventListener('keydown', onOutsideKey)
    return () => window.removeEventListener('keydown', onOutsideKey)
  }, [fullscreen, exit, ime, ref])

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!fullscreen || event.key !== 'Escape' || event.defaultPrevented || hasNestedWorkspaceOverlay(ref.current)) return
    if (!ime.claimSyntheticKey(event)) return
    event.preventDefault()
    event.stopPropagation()
    exit?.()
  }

  return { fullscreen, onKeyDown, toggle, exit }
}

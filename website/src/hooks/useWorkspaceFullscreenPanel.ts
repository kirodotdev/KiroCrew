import { useContext, useEffect, type RefObject, type KeyboardEvent } from 'react'
import { WorkspaceFullscreenContext } from '../components/WorkspacePanelContext'
import { useDocumentImeLatch } from './useImeGuard'

/** Hidden background tabs and covered chrome cannot own the active Escape. */
function hasNestedWorkspaceOverlay(panel: HTMLElement | null): boolean {
  return Array.from(document.querySelectorAll<HTMLElement>(
    '[role="dialog"], [role="alertdialog"], [role="menu"], [data-workspace-escape-owner]',
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

  useEffect(() => {
    const panel = ref.current
    if (!fullscreen || !panel) return
    const previousFocus = document.activeElement as HTMLElement | null
    const hidden: { element: HTMLElement; inert: boolean }[] = []
    // Keep the control cluster reachable, but remove covered chrome and chat
    // from keyboard navigation. Walking ancestors also covers the mobile host.
    let branch: HTMLElement = panel
    while (branch.parentElement) {
      const parent = branch.parentElement
      for (const sibling of Array.from(parent.children)) {
        if (!(sibling instanceof HTMLElement) || sibling === branch
          || sibling.matches('[data-workspace-panel-controls]')) continue
        hidden.push({ element: sibling, inert: sibling.inert })
        sibling.inert = true
      }
      if (parent.matches('[data-testid="dashboard-shell"]')) break
      branch = parent
    }
    if (!panel.contains(previousFocus)) panel.focus({ preventScroll: true })
    return () => {
      hidden.forEach(({ element, inert }) => { element.inert = inert })
      const currentFocus = document.activeElement
      // A click on a shell control chooses the next focus target deliberately.
      // Restore only when focus still belongs to the panel being restored.
      if (previousFocus?.isConnected && (currentFocus === document.body || panel.contains(currentFocus))) {
        previousFocus.focus({ preventScroll: true })
      }
    }
  }, [fullscreen, ref])

  useEffect(() => {
    if (!fullscreen) return
    // Escape pressed outside the panel exits; Escape inside it reaches the
    // panel's own onKeyDown after nested editors and menus decline it. A
    // focused terminal is deliberately NOT intercepted: xterm consumes Escape
    // (preventDefault + stopPropagation) and forwards it to the PTY, where it
    // belongs to the running program — vim's insert mode, less, a TUI. Taking
    // it here would leave the program in the state the user believes it left,
    // so fullscreen exits from a terminal via the panel's exit control.
    const onOutsideKey = (event: globalThis.KeyboardEvent) => {
      if (event.key !== 'Escape' || event.defaultPrevented || hasNestedWorkspaceOverlay(ref.current)
        || (event.target instanceof Node && ref.current?.contains(event.target))) return
      if (!ime.claimKey(event)) return
      event.preventDefault()
      event.stopPropagation()
      exit?.()
    }
    window.addEventListener('keydown', onOutsideKey)
    return () => {
      window.removeEventListener('keydown', onOutsideKey)
    }
  }, [fullscreen, exit, ime, ref])

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!fullscreen || event.key !== 'Escape' || event.defaultPrevented || hasNestedWorkspaceOverlay(ref.current)) return
    if (!ime.claimSyntheticKey(event)) return
    event.preventDefault()
    event.stopPropagation()
    exit?.()
  }
  /** `toggle` is undefined outside the dashboard, where no workspace fullscreen exists. */
  return { fullscreen, onKeyDown, toggle }
}

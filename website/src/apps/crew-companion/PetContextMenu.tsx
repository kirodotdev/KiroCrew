/**
 * Pet overlay context menu — uses the shared ContextMenu component.
 * Provides pet-specific menu items with i18n.
 */
import { useCallback, useMemo } from 'react'
import { ContextMenu, type ContextMenuEntry } from './ContextMenu'
import { i18nT } from '../../i18n/t'
import { petBridge } from './petBridge'

// Same method names as the desktop app's IPC bridge, re-implemented over
// Kiro Crew's gateway — so everything below is unchanged.
const api = petBridge

interface Props {
  x: number
  y: number
  isHidden: boolean
  onClose: () => void
}

export function PetContextMenu({ x, y, onClose }: Props) {

  const items: ContextMenuEntry[] = useMemo(() => {
    return [
      // The gallery is its own window, so this is its entry point. It is NOT in
      // Settings: importing and authoring packs are creation flows that don't
      // belong in a preferences column.
      { label: i18nT('apps.crewCompanion.menu.change_avatar'), action: 'gallery' },
      { separator: true },
      // Quit names the APP, not the pet: "Quit Kiro" read as dismissing the
      // character rather than closing Crew Companion.
      { label: i18nT('apps.crewCompanion.menu.quit'), action: 'quit', danger: true },
    ]
    // Removed deliberately:
    //  • the 🎬 motion list and the 🔔 "Test notify" items — developer ingress that
    //    let anyone fire a reaction or a fake notification, so a celebration or an
    //    approval bubble no longer always meant something had happened;
  }, [])

  const handleAction = useCallback((action: string) => {
    api?.contextMenuAction?.(action)
  }, [])

  return (
    <ContextMenu
      x={x} y={y}
      items={items}
      reportHitbox
      onAction={handleAction}
      onClose={onClose}
    />
  )
}

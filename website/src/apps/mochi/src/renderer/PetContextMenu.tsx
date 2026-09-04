/**
 * Pet overlay context menu — uses the shared ContextMenu component.
 * Provides pet-specific menu items with i18n.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Bell, BellOff, Eye, EyeOff, LayoutDashboard, Palette, Settings } from 'lucide-react'
import { ContextMenu, type ContextMenuEntry } from './ContextMenu'
import { api } from '../mochiApi'
import { formatShortcut } from '../shared/shortcut'
import { i18nT } from '../../../../i18n/t'

interface Props {
  x: number
  y: number
  isHidden: boolean
  onClose: () => void
}

/** "HH:MM" in the user's locale, for the quiet-until label. */
function formatUntil(ms: number): string {
  return new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit' }).format(
    new Date(ms),
  )
}

export function PetContextMenu({ x, y, isHidden, onClose }: Props) {
  // Quiet-mode expiry, read fresh each time the menu opens. 0 = not quiet.
  // The menu is transient, so a pull on open beats threading live state
  // through the overlay; natural expiry (backend tick) is picked up the same
  // way. Until the pull lands the item renders as "not quiet" — flashing the
  // wrong toggle for a frame is better than a menu with a hole in it.
  const [quietUntil, setQuietUntil] = useState(0)
  useEffect(() => {
    let alive = true
    void api?.getQuietUntil?.().then((v) => {
      if (alive) setQuietUntil(v)
    })
    return () => {
      alive = false
    }
  }, [])

  // The hideAll accelerator (rebindable) shown on the Hide row so restore is
  // discoverable: a hidden pet cannot be right-clicked to bring itself back, so
  // the global shortcut is the only recovery — name it where the user hides.
  const [hideShortcut, setHideShortcut] = useState('')
  useEffect(() => {
    let alive = true
    void api?.getConfig?.().then((c: { shortcuts?: { hideAll?: string } }) => {
      if (!alive) return
      const s = c?.shortcuts?.hideAll || 'CommandOrControl+Shift+H'
      setHideShortcut(formatShortcut(s))
    })
    return () => {
      alive = false
    }
  }, [])

  // SUBTRACTED: screenshot (capture not ported), soul (each avatar carries its
  // own persona), quit (KiroCrew owns the app lifecycle — the pet is disabled
  // from the app store). A row with no handler is a silent no-op, so the rows
  // go rather than the handlers being faked.
  const items: ContextMenuEntry[] = useMemo(() => [
    { label: i18nT('apps.mochi.menu.gallery'), action: 'gallery', icon: Palette },
    { label: i18nT('apps.mochi.menu.settings'), action: 'settings', icon: Settings },
    { label: i18nT('apps.mochi.menu.dashboard'), action: 'dashboard', icon: LayoutDashboard },
    { separator: true },
    quietUntil > 0
      ? {
          // The time in the label doubles as the quiet-mode indicator — the
          // user who forgot they silenced the pet sees when it wakes.
          label: i18nT('apps.mochi.menu.quiet_resume', { time: formatUntil(quietUntil) }),
          action: 'quiet-off',
          icon: Bell,
        }
      : { label: i18nT('apps.mochi.menu.quiet_1h'), action: 'quiet-1h', icon: BellOff },
    { label: i18nT(isHidden ? 'apps.mochi.menu.show' : 'apps.mochi.menu.hide'), action: 'hide', icon: isHidden ? Eye : EyeOff, shortcut: hideShortcut || undefined },
  ], [isHidden, quietUntil, hideShortcut])

  const handleAction = useCallback((action: string) => {
    if (action === 'quiet-1h') {
      void api?.setQuiet?.(60)
      return
    }
    if (action === 'quiet-off') {
      void api?.setQuiet?.(0)
      return
    }
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

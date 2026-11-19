import { useCallback, useSyncExternalStore, type ComponentType } from 'react'
import { createPortal } from 'react-dom'
import ErrorBoundary from '../components/ErrorBoundary'
import { useThemeDecorKeepClear } from '../lib/themeDecorKeepClear'
import { useAppSelector } from '../store'
import { REGISTERED_OVERLAYS_PACK, readExperience, subscribeExperience } from '../utils/themeConsent'

/**
 * Theme decoration: the decorative overlays (widgets, transitions) a registered
 * theme's branding contributes, and the composer clip on the decor slot that
 * every theme overlay, a pack's included, sits in.
 *
 * The overlays are absent unless the theme declares them, while the user has
 * turned that theme's Overlays part off, and while a remote Crew is shown: they
 * belong to THIS dashboard's theme, and the remote pane draws its own. They
 * portal into the shell's decor slot like a pack's overlays, so the top bar
 * outranks them and the composer stays uncovered (lib/themeDecorKeepClear.ts);
 * inline only where no shell exists. Each overlay is isolated in its own
 * ErrorBoundary (fallback=null) so a throwing overlay disables only itself.
 */
export default function RegisteredThemeOverlays({ colorTheme, overlays, slot }: {
  colorTheme: string
  overlays: readonly ComponentType[] | undefined
  /** The shell's decor slot, or null where no shell is mounted. */
  slot: HTMLElement | null
}) {
  // Read during render, so turning the part off never mounts an overlay for one frame.
  const overlaysOn = useSyncExternalStore(
    useCallback((onChange: () => void) => subscribeExperience(colorTheme, onChange), [colorTheme]),
    () => readExperience(colorTheme, REGISTERED_OVERLAYS_PACK).overlays,
  )
  const remoteShown = useAppSelector(s => s.instances.activeId) !== null
  useThemeDecorKeepClear(slot)
  if (!overlaysOn || remoteShown || !overlays?.length) return null
  const nodes = overlays.map((Overlay, i) => (
    <ErrorBoundary key={`${colorTheme}:${i}`} scope={`theme-overlay:${i}`} fallback={null}>
      <Overlay />
    </ErrorBoundary>
  ))
  return slot ? createPortal(nodes, slot) : <>{nodes}</>
}

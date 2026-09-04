import { Component, Suspense, type ReactNode } from 'react'

import CommandPalette from './CommandPalette'
import { getBuiltinOverlay } from '../apps/overlayRegistry'
import type { SlotOwners } from '../apps/overlaySlots'

/**
 * Render `children`, falling back to `fallback` if they throw.
 *
 * A contributed overlay is a lazily imported chunk, and `Suspense` does not catch a
 * REJECTED import -- only a pending one. A tab left open across a deploy asks for a
 * chunk hash that no longer exists, so without this the next press of the gesture
 * would take the whole dashboard down to the root boundary. Falling back to the
 * shell's own palette keeps the gesture working instead of showing an error card.
 */
class OverlayBoundary extends Component<
  { fallback: ReactNode; children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false }

  static getDerivedStateFromError() {
    return { failed: true }
  }

  componentDidCatch(error: unknown) {
    // eslint-disable-next-line no-console -- a silently swapped surface is worse
    console.warn('[QuickSearchSurface] contributed overlay failed to render:', error)
  }

  render() {
    return this.state.failed ? this.props.fallback : this.props.children
  }
}

/**
 * What the quick-search gesture opens.
 *
 * The gesture itself stays in the shell; the surface behind it is a table read. While
 * an enabled app owns the `quick-search` slot its overlay serves the gesture; with no
 * owner the shell's own palette renders exactly as before, untouched. The shell never
 * names a specific app, so a second overlay-contributing app needs no change here.
 *
 * The overlay is mounted only while open. Its chunk therefore loads on first use, and
 * -- more importantly -- a closed bar holds no live subscriptions: the scoped session
 * view's query would otherwise stay enabled behind a closed surface and refetch on the
 * next window focus, turning a dismissed search into a background corpus scan.
 */
export default function QuickSearchSurface({
  owners,
  open,
  onClose,
  openShortcuts,
}: {
  owners: SlotOwners
  open: boolean
  onClose: () => void
  openShortcuts?: () => void
}) {
  const owner = owners['quick-search']
  const Overlay = owner ? getBuiltinOverlay(owner.overlayId) : undefined
  const palette = (
    <CommandPalette open={open} onClose={onClose} openShortcuts={openShortcuts} />
  )
  if (!Overlay) return palette
  if (!open) return null
  return (
    <OverlayBoundary fallback={palette}>
      <Suspense fallback={null}>
        <Overlay open={open} onClose={onClose} />
      </Suspense>
    </OverlayBoundary>
  )
}

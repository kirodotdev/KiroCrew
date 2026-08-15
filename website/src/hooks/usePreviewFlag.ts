import { useEffect, useState } from 'react'

import { PREVIEW_FLAG_EVENT, PREVIEW_FLAG_PREFIX, readPreviewFlag } from '../utils/previewFlags'
import type { PreviewFlagChange } from '../utils/previewFlags'

/**
 * Reactive read of a preview flag (see `utils/previewFlags.ts`).
 *
 * Subscribing matters here, not just reading: the nav rail is rendered by the
 * app shell, which does not remount when the Developer > Feature Previews toggle flips.
 * Without this hook the rail would keep its pre-toggle contents until a reload,
 * and a user who just enabled a surface would conclude the toggle is broken.
 *
 * Updates on the same-tab `mc-preview-flag-changed` event and on cross-tab
 * `storage` events, mirroring `useDevMode`.
 */
export function usePreviewFlag(flag: string): boolean {
  const [on, setOn] = useState(() => readPreviewFlag(flag))
  useEffect(() => {
    // Re-read on mount as well: `flag` can change between renders, and the
    // initializer above only ran for the first one.
    setOn(readPreviewFlag(flag))
    const onEvent = (e: Event) => {
      const detail = (e as CustomEvent<PreviewFlagChange>).detail
      if (detail?.key === flag) setOn(detail.on)
    }
    const onStorage = (e: StorageEvent) => {
      if (e.key === flag) setOn(e.newValue === '1')
    }
    window.addEventListener(PREVIEW_FLAG_EVENT, onEvent)
    window.addEventListener('storage', onStorage)
    return () => {
      window.removeEventListener(PREVIEW_FLAG_EVENT, onEvent)
      window.removeEventListener('storage', onStorage)
    }
  }, [flag])
  return on
}

/**
 * Subscribe to preview-flag changes and get a revision counter to hang memo
 * dependencies off — WITHOUT naming a flag.
 *
 * For consumers that render a whole list of surfaces and decide visibility per
 * item: they need a re-render when any flag flips, but hardcoding which flags
 * exist would push per-surface knowledge into the nav rail, which is exactly
 * what the registry field exists to avoid. The returned number changes on every
 * preview-flag change and is otherwise meaningless — pass it as a `useMemo` dep
 * so a memoized list recomputes too (a plain re-render does not recompute a
 * memo whose deps did not change).
 */
export function usePreviewFlagRevision(): number {
  const [rev, bump] = useState(0)
  useEffect(() => {
    const onChange = () => bump(n => n + 1)
    window.addEventListener(PREVIEW_FLAG_EVENT, onChange)
    // Cross-tab: a `storage` event carries the key that changed, and only
    // preview keys are relevant. `mc-preview-` prefix rather than a list of
    // known flags, so a new flag needs no change here.
    const onStorage = (e: StorageEvent) => {
      if (e.key?.startsWith(PREVIEW_FLAG_PREFIX)) bump(n => n + 1)
    }
    window.addEventListener('storage', onStorage)
    return () => {
      window.removeEventListener(PREVIEW_FLAG_EVENT, onChange)
      window.removeEventListener('storage', onStorage)
    }
  }, [])
  return rev
}

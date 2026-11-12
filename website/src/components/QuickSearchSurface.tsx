import { Component, Suspense, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'

import CommandPalette from './CommandPalette'
import { OverlayPortal } from './overlayRoot'
import { getBuiltinOverlay } from '../apps/overlayRegistry'
import type { SlotOwners } from '../apps/overlaySlots'
import { useImeGuard } from '../hooks/useImeGuard'
import { useVisualViewport } from '../hooks/useVisualViewport'
import { i18nT } from '../i18n/t'

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
 * Holds the keys typed while the overlay's chunk loads.
 *
 * The first open fetches the overlay's chunk, and until it resolves nothing on the
 * page is the bar's input. Keys pressed in that window would go to whatever has focus
 * (the trigger button, or the composer behind a ⌘K) and be lost, so a fast
 * "new session" + Enter would run the bar on an empty query. This input takes focus in
 * the same commit as the gesture, and its committed text becomes `initialQuery` for
 * whichever surface renders next: the overlay, or the palette if the chunk fails.
 *
 * The field is visible, at the place the bar opens, so focus never lands on a control
 * the reader cannot see. A chunk that stalls leaves a working field rather than an
 * empty page with focus somewhere invisible.
 *
 * Enter is left alone on purpose: the reader cannot see what it would pick yet, so a
 * surface acts only on an Enter pressed after it has rendered the typed query.
 *
 * IME composition is respected in both directions. Escape goes through the shared
 * guard, so an Escape that cancels a candidate list does not close the bar. Text still
 * being composed is never handed over: the composition dies when this field unmounts,
 * so only text the IME has committed counts as typed.
 *
 * Focus goes back to where the gesture started when the field unmounts. An Escape
 * here then leaves the keyboard user where they were, and the shell palette that
 * replaces this field when the chunk fails records `document.activeElement` as its
 * restore target after the field is gone, so without the hand-back it would record
 * `<body>`. The overlay gets its opener from the host instead (`opener`).
 */
function TypeAheadInput({
  onCommit,
  onClose,
}: {
  onCommit: (value: string) => void
  onClose: () => void
}) {
  const ime = useImeGuard()
  const vv = useVisualViewport()
  const [draft, setDraft] = useState('')
  // Whether the IME holds uncommitted text. Read only to decide what to hand over;
  // key decisions go through `ime`, whose latch also covers the post-composition window.
  const composingRef = useRef(false)
  const inputRef = useRef<HTMLInputElement>(null)
  // Read during the first render, before `autoFocus` moves focus into the field.
  const [opener] = useState(() => document.activeElement)
  // A layout cleanup runs while React removes this field, before the replacing
  // surface's mount effects read `document.activeElement`.
  // The setup takes focus as well as `autoFocus`: StrictMode replays setup and cleanup
  // without rerunning `autoFocus`, and the cleanup has just handed focus back.
  useLayoutEffect(() => {
    const input = inputRef.current
    input?.focus({ preventScroll: true })
    return () => {
      if (document.activeElement !== input && document.activeElement !== document.body) return
      if (!(opener instanceof HTMLElement) || opener === document.body || !opener.isConnected) return
      opener.focus({ preventScroll: true })
    }
  }, [opener])
  return (
    <OverlayPortal>
      <div
        className="fixed left-safe right-safe z-[9999] flex items-start justify-center pointer-events-none"
        style={{ top: vv.offsetTop, height: vv.height }}
      >
        {/* Composition events bubble here after the input's own latch handlers have run. */}
        <div
          className="pointer-events-auto w-full max-w-[680px] mx-4 bg-card border border-border rounded-xl shadow-xl flex items-center px-3 py-2.5 focus-within:border-accent"
          style={{ marginTop: Math.round(vv.height * 0.12) }}
          onCompositionStart={() => {
            composingRef.current = true
          }}
          onCompositionEnd={e => {
            composingRef.current = false
            onCommit((e.target as HTMLInputElement).value)
          }}
        >
          <input
            ref={inputRef}
            autoFocus
            // focus-cue-ok: the panel above paints focus-within:border-accent.
            className="flex-1 min-w-0 bg-transparent border-none outline-hidden text-[14px] text-text placeholder:text-muted"
            aria-label={i18nT('app.k_run_a_command')}
            placeholder={i18nT('app.k_run_a_command')}
            value={draft}
            onChange={e => {
              setDraft(e.target.value)
              if (!composingRef.current) onCommit(e.target.value)
            }}
            {...ime.bindComposition<HTMLInputElement>({
              onBlur: () => {
                composingRef.current = false
              },
            })}
            onKeyDown={e => {
              if (e.key !== 'Escape') return
              if (!ime.claimKey(e)) return
              // Consumed, as the loaded bar does, so document-level Escape handlers
              // such as voice capture and the notification banner leave it alone.
              e.preventDefault()
              onClose()
            }}
          />
        </div>
      </div>
    </OverlayPortal>
  )
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
  opener,
  openShortcuts,
}: {
  owners: SlotOwners
  open: boolean
  onClose: () => void
  /** The control the gesture came from; see `OverlayProps.opener`. */
  opener?: HTMLElement | null
  openShortcuts?: () => void
}) {
  const owner = owners['quick-search']
  const Overlay = owner ? getBuiltinOverlay(owner.overlayId) : undefined
  const [typeAhead, setTypeAhead] = useState('')
  // Type-ahead belongs to one open. Cleared on the close edge so the next open
  // starts empty, the same as the overlay's own query.
  useEffect(() => {
    if (!open) setTypeAhead('')
  }, [open])
  // The palette also renders when the overlay's chunk fails to load, and keys caught
  // before that failure belong to it just as they would to the overlay.
  const palette = (
    <CommandPalette open={open} onClose={onClose} openShortcuts={openShortcuts} initialQuery={typeAhead} />
  )
  if (!Overlay) return palette
  if (!open) return null
  return (
    <OverlayBoundary fallback={palette}>
      <Suspense fallback={<TypeAheadInput onCommit={setTypeAhead} onClose={onClose} />}>
        <Overlay open={open} onClose={onClose} initialQuery={typeAhead} opener={opener} />
      </Suspense>
    </OverlayBoundary>
  )
}

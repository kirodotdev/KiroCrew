/**
 * Module-level registry that tracks which inline tool pills are currently
 * visible in the viewport. Pending-approval pills register their DOM node
 * here; the approval bar in ChatInput subscribes via {@link useToolPillVisible}
 * so it can grow a "ghost pill" when the inline pill scrolls out of sight.
 *
 * Why a module-level store and not Redux? This is purely ephemeral viewport
 * state with no need for time-travel/devtools, and updates fire on every
 * scroll frame — far too noisy for the Redux dev experience.
 *
 * Default visibility when no entry is registered is `false` ("we cannot see
 * the pill, so show the ghost"). This is the correct fallback for the most
 * common no-entry case: Virtuoso has virtualised the pill out of the DOM
 * because the user scrolled far away, so the inline view is unreachable
 * and the ghost must take over.
 *
 * The brief no-entry window during the very first mount of a pending pill
 * is bridged by `useSyncExternalStore`: the layout-effect registration in
 * `ToolCallLine` notifies the store synchronously within the same commit,
 * so the snapshot flips to the real visibility value before the browser
 * paints — no flicker.
 */
import { useSyncExternalStore } from 'react'

type Entry = { el: HTMLElement; visible: boolean }

const entries = new Map<string, Entry>()
const subscribers = new Set<() => void>()

const observer: IntersectionObserver | null = typeof IntersectionObserver !== 'undefined'
  ? new IntersectionObserver((events) => {
      let changed = false
      for (const ev of events) {
        // Reverse-lookup the id by element — Map keyed by id keeps
        // registrations stable across re-renders, but IntersectionObserver
        // emits events keyed by the observed `target`, so we walk values.
        for (const [, entry] of entries) {
          if (entry.el !== ev.target) continue
          if (entry.visible !== ev.isIntersecting) {
            entry.visible = ev.isIntersecting
            changed = true
          }
          break
        }
      }
      if (changed) notify()
    }, {
      // Treat the pill as "out of view" as soon as it clips 40px past the
      // viewport top — feels more responsive than waiting for full exit.
      rootMargin: '-40px 0px 0px 0px',
      threshold: 0,
    })
  : null

function notify() {
  for (const fn of subscribers) fn()
}

/** Register a tool pill's DOM node under its `tool_call_id`. The returned
 *  cleanup unobserves and deletes the entry. Safe to call from a layout
 *  effect — performs a synchronous bbox check so the initial visibility
 *  state is known on the same frame as registration. */
export function registerToolPill(id: string, el: HTMLElement): () => void {
  // Synchronous initial visibility check matches the IO `rootMargin`.
  const r = el.getBoundingClientRect()
  const vh = window.innerHeight || document.documentElement.clientHeight
  const visible = r.bottom > 40 && r.top < vh
  entries.set(id, { el, visible })
  observer?.observe(el)
  notify()
  return () => {
    observer?.unobserve(el)
    entries.delete(id)
    notify()
  }
}

/** Smoothly scroll the registered pill into the centre of the viewport. */
export function scrollToToolPill(id: string): boolean {
  const entry = entries.get(id)
  if (!entry) return false
  entry.el.scrollIntoView({ behavior: 'smooth', block: 'center' })
  return true
}

function subscribe(cb: () => void) {
  subscribers.add(cb)
  return () => { subscribers.delete(cb) }
}

/** Subscribe to a tool pill's viewport visibility. Returns `false` while no
 *  entry is registered (see file header for rationale) — i.e. show the ghost
 *  whenever we genuinely cannot see the pill. When the caller passes a
 *  null/undefined id (no approval pending), returns `true` so the ghost is
 *  never shown. */
export function useToolPillVisible(id: string | null | undefined): boolean {
  return useSyncExternalStore(
    subscribe,
    () => (id ? (entries.get(id)?.visible ?? false) : true),
    () => (id ? false : true),
  )
}

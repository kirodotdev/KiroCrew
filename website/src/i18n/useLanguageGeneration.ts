/**
 * Language-change subscription for `memo()` boundaries.
 *
 * ## The gap this closes
 *
 * `LanguageProvider` repaints the tree on a language switch with
 * `cloneElement(children)`, which defeats React's referential-equality bailout
 * for the ROOT element only. React then reconciles downward, and any `memo()`
 * boundary whose props are shallow-equal short-circuits its subtree. Standalone
 * `i18nT()` reads the catalog at call time but subscribes to nothing, so a
 * bailed-out subtree keeps rendering the PREVIOUS language until one of its
 * props happens to change.
 *
 * ## How this fixes it
 *
 * The active-language *generation* — a counter bumped on every i18next
 * `languageChanged` — is published as an external store. A memoized component
 * calls `useLanguageGeneration()` once at the top of its body; the
 * `useSyncExternalStore` subscription schedules that component's own re-render
 * when the catalog swaps, which `memo()`'s props comparison cannot suppress.
 * Between switches the snapshot is an unchanged primitive, so `Object.is`
 * skips every no-op notification for free (the same stable-snapshot-identity
 * rule as `useCrewPins` / `useArtifactPopouts` / `useBottomTerminal`).
 *
 * ## Where it belongs — and where it does not
 *
 * Call it once per `memo()`-wrapped component body. Do NOT sprinkle it at
 * `i18nT()` call sites: many of those are render callbacks or plain helpers
 * where a hook is a rules-of-hooks violation — which is exactly why the
 * standalone `i18nT()` exists (see `./t.ts`). Non-memoized components never
 * need it; the provider's `cloneElement` repaint already reaches them.
 *
 * The subscription is wired at module load, not on first hook use: a switch
 * that lands between a component's first render and its store subscription
 * must still bump the generation, or that component would render the old
 * catalog and never learn about the swap.
 */

import { useSyncExternalStore } from 'react'

import { i18next } from './index'

let generation = 0

const listeners = new Set<() => void>()

// `languageChanged` fires strictly AFTER the catalog swap (the same ordering
// LanguageProvider relies on), so a re-render triggered here always reads the
// NEW strings.
i18next.on('languageChanged', () => {
  generation++
  listeners.forEach(l => l())
})

function subscribe(cb: () => void): () => void {
  listeners.add(cb)
  return () => { listeners.delete(cb) }
}

function getSnapshot(): number {
  return generation
}

/**
 * Subscribe this component to language switches. Returns the current
 * generation (rarely useful in itself — the subscription is the point).
 */
export function useLanguageGeneration(): number {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
}

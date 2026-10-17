/**
 * The composer's editor text as an external store (chat-core RFC §3, P3-f).
 *
 * A host that keeps the draft in `useState` re-renders itself on every
 * keystroke, and on the main chat that host is the whole page: sidebar,
 * transcript rows, menus and all. Holding the text here instead means a
 * keystroke re-renders only what subscribes to it -- the composer's editor --
 * while the host keeps the same `setInput` call shape and reads the text
 * through `get()` when it acts (send, splice, persist).
 *
 * A host that needs a FACT about the text in its render (is it blank, which
 * `@dir/` tokens it holds) selects that fact with `useComposerDraftSelector`,
 * so it re-renders when the fact changes, not on every character.
 *
 * Store-free by design (#8651): no Redux, no React import beyond the hooks.
 */
import { useCallback, useRef, useSyncExternalStore } from 'react'

export interface ComposerDraftStore {
  get: () => string
  /** Same contract as a `useState` setter: a value, or an updater of the
   *  current text. Writing the current value is a no-op. */
  set: (next: string | ((prev: string) => string)) => void
  subscribe: (fn: () => void) => () => void
}

export function createComposerDraftStore(initial: string): ComposerDraftStore {
  let value = initial
  const subs = new Set<() => void>()
  return {
    get: () => value,
    set: (next) => {
      const resolved = typeof next === 'function' ? next(value) : next
      if (resolved === value) return
      value = resolved
      for (const fn of subs) fn()
    },
    subscribe: (fn) => { subs.add(fn); return () => { subs.delete(fn) } },
  }
}

/** The live draft text. Re-renders the caller on every change. */
export function useComposerDraft(store: ComposerDraftStore): string {
  return useSyncExternalStore(store.subscribe, store.get, store.get)
}

/**
 * One fact derived from the draft. The caller re-renders only when the
 * selected value changes (`Object.is`), so `select` must return a primitive or
 * a value that is stable for equal input -- a string key, a boolean, a count.
 * The last text and its result are cached, so `select` runs once per change.
 */
export function useComposerDraftSelector<T>(store: ComposerDraftStore, select: (text: string) => T): T {
  const cache = useRef<{ text: string; value: T } | null>(null)
  const selectRef = useRef(select); selectRef.current = select
  const getSnapshot = useCallback(() => {
    const text = store.get()
    const hit = cache.current
    if (hit && hit.text === text) return hit.value
    const value = selectRef.current(text)
    cache.current = { text, value }
    return value
  }, [store])
  return useSyncExternalStore(store.subscribe, getSnapshot, getSnapshot)
}

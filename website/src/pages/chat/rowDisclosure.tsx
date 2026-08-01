/**
 * Durable disclosure state for rows in the virtualised transcript.
 *
 * The chat log is virtualised: useVirtualChat renders a row only while
 * `item.mounted` holds and unmounts it once the row leaves the window plus
 * overscan band, which streaming does routinely as it scrolls content past. Any
 * expand/collapse state held in `useState` inside a row is therefore destroyed
 * every time that happens, and the user's choice is lost. Guarding the
 * collapse cannot help, because a remount discards the guard along with the
 * state it protects: the state has to outlive the row.
 *
 * This is an external store rather than context state so a toggle notifies only
 * the ONE consumer subscribed to that key. Holding the map in provider state
 * would re-render every consumer on every toggle, which matters because these
 * are the rows whose render cost is already the most sensitive in the app.
 *
 * `useRowDisclosure` degrades to plain local state when no provider is present,
 * so hosts that render transcript components outside ChatPage (split-view
 * ChatPane, app-sdk's ChatMessageList) keep working untouched.
 */
import {
  createContext, useCallback, useContext, useEffect, useRef, useState,
  useSyncExternalStore, type ReactNode,
} from 'react'

type Listener = () => void

export class RowDisclosureStore {
  private values = new Map<string, boolean>()
  private listeners = new Map<string, Set<Listener>>()

  get = (key: string): boolean | undefined => this.values.get(key)

  set = (key: string, value: boolean): void => {
    if (this.values.get(key) === value) return
    this.values.set(key, value)
    const ls = this.listeners.get(key)
    // Copy before iterating: a listener may unsubscribe during notification.
    if (ls) for (const l of Array.from(ls)) l()
  }

  subscribe = (key: string, listener: Listener): (() => void) => {
    let ls = this.listeners.get(key)
    if (!ls) { ls = new Set(); this.listeners.set(key, ls) }
    ls.add(listener)
    return () => {
      const cur = this.listeners.get(key)
      if (!cur) return
      cur.delete(listener)
      if (cur.size === 0) this.listeners.delete(key)
    }
  }

  /** Drop every recorded choice, notifying anything currently mounted. */
  reset = (): void => {
    const keys = Array.from(this.values.keys())
    this.values.clear()
    for (const k of keys) {
      const ls = this.listeners.get(k)
      if (ls) for (const l of Array.from(ls)) l()
    }
  }
}

const RowDisclosureContext = createContext<RowDisclosureStore | null>(null)

/**
 * Supplies the store to a transcript. `resetKey` is the slot key: row keys are
 * only unique within a slot, so carrying choices across a switch would apply
 * one session's state to another's rows.
 */
export function RowDisclosureProvider({ resetKey, children }: { resetKey?: string | null; children: ReactNode }) {
  const storeRef = useRef<RowDisclosureStore | null>(null)
  if (!storeRef.current) storeRef.current = new RowDisclosureStore()
  const store = storeRef.current
  // Reset in an effect, not during render: reset() notifies subscribers, and
  // doing that mid-render would set state on other components while rendering.
  useEffect(() => { store.reset() }, [resetKey, store])
  return <RowDisclosureContext.Provider value={store}>{children}</RowDisclosureContext.Provider>
}

/**
 * Disclosure state that survives the row being recycled.
 *
 * `key` identifies the control across unmounts and must be stable for the same
 * logical row (a tool_call_id, a message key). Pass `undefined` when no stable
 * identity is available and the hook falls back to local state. `fallback` is
 * the value used until the user has made an explicit choice, so a control that
 * was never touched keeps whatever default behaviour it had.
 */
export function useRowDisclosure(
  key: string | undefined,
  fallback: boolean,
): [boolean, (next: boolean | ((prev: boolean) => boolean)) => void] {
  const store = useContext(RowDisclosureContext)
  const [local, setLocal] = useState(fallback)

  const subscribe = useCallback((listener: Listener) => {
    if (!store || !key) return () => {}
    return store.subscribe(key, listener)
  }, [store, key])

  const getSnapshot = useCallback(
    () => (store && key ? store.get(key) : undefined),
    [store, key],
  )
  const stored = useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
  // When a store backs this control it is the ONLY source of truth. Falling
  // back to the local shadow here would defeat reset(): the store clears but a
  // stale local `true` would keep the control open across a slot switch.
  // Without a store (unprovided host) the local value is all there is.
  const backed = !!(store && key)
  const expanded = backed ? (stored ?? fallback) : local

  // Accepts the updater form as well, so migrating a `useState` call site is a
  // one-line change rather than a rewrite of every toggle handler.
  const expandedRef = useRef(expanded)
  expandedRef.current = expanded
  const set = useCallback((next: boolean | ((prev: boolean) => boolean)) => {
    const value = typeof next === 'function' ? next(expandedRef.current) : next
    if (store && key) store.set(key, value)
    else setLocal(value)
  }, [store, key])

  return [expanded, set]
}

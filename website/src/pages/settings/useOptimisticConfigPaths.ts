import { useEffect, useRef, useState } from 'react'
import type { QueryClient, QueryKey } from '@tanstack/react-query'

/**
 * Return a copy of `cfg` with the dot-separated `path` set to `value`,
 * shallow-cloning only the objects along the path. Used on a config PATCH's
 * success to write the ACCEPTED value into the query cache at that one path,
 * so a transiently failed settle-time refetch cannot leave the display on a
 * pre-PATCH value the server no longer holds.
 */
export function setConfigPathValue<T>(cfg: T, path: string, value: unknown): T {
  const keys = path.split('.')
  const next: Record<string, unknown> = { ...(cfg as Record<string, unknown>) }
  let cursor = next
  for (let i = 0; i < keys.length - 1; i++) {
    const child = cursor[keys[i]]
    cursor[keys[i]] = typeof child === 'object' && child !== null ? { ...(child as Record<string, unknown>) } : {}
    cursor = cursor[keys[i]] as Record<string, unknown>
  }
  cursor[keys[keys.length - 1]] = value
  return next as T
}

type PendingEntry = { value: unknown; token: number }

export type OptimisticPathMutation<TVars, TData> = {
  /** Query key whose cached object this mutation's settle reconciles. */
  queryKey: QueryKey
  mutationFn: (vars: TVars) => Promise<TData>
  /** The overlay path these vars write — one display slot per path. */
  path: (vars: TVars) => string
  /** The value the overlay shows at that path while the request is in flight. */
  displayValue: (vars: TVars) => unknown
  /** Token-guarded success write of the ACCEPTED value into the cached object. */
  applyToCache: (cached: unknown, vars: TVars) => unknown
  /** Failure report, invoked only when this mutation still owns its path. */
  onFailure?: (err: unknown, vars: TVars) => void
  /** A fresh attempt began on `path` — clear that path's stale failure state. */
  onSupersede?: (path: string) => void
}

/**
 * Per-config-path optimistic display for settings mutations that share a
 * query key. Each control renders `shown(path, server)` — the pending value
 * while a save is in flight, the server value otherwise — so concurrent
 * saves on different paths can never transiently revert each other's
 * display, which is exactly what a whole-object onMutate snapshot/rollback
 * does: the snapshot captures another save's in-flight optimistic value and
 * an error restores it, and an unconditional settle-time refetch returns
 * server state that does not yet include the other save's un-applied write.
 *
 * Ownership is a monotonic token, not the written value: `mutationOpts`
 * returns the token from `onMutate` (react-query hands it back to the settle
 * callbacks as the mutation context), and only the entry's own token may
 * clear it. Guarding on the value instead would let save A → save B → save A
 * on one path have A₁'s settle clear the entry A₃ owns, flashing the stale
 * cache value while A₃ is still in flight.
 *
 * The settle lifecycle mirrors the model pickers this was extracted from:
 * on success the ACCEPTED value is written into the cache at this path only
 * (token-guarded, so a superseded save never overwrites a newer settled one),
 * then the returned `invalidateQueries` promise is awaited by react-query
 * before `onSettled` — by the time the pending entry clears, a completed
 * refetch has already replaced that write with the server's authoritative
 * answer. On error the cache was never written, but the refetch still runs:
 * a request can fail after persisting (5xx after apply, proxy timeout), and
 * only the server can say which value survived. `''` and `false` are
 * meaningful values, hence the explicit entry check in `shown` rather than
 * truthiness or `??`.
 */
export function useOptimisticConfigPaths(qc: QueryClient) {
  const pendingSeqRef = useRef(0)
  // Latest token per path, readable synchronously from mutation callbacks
  // (state would be a stale closure there): lets a superseded mutation's
  // late settle recognise it no longer owns the path's display.
  const latestTokenRef = useRef<Record<string, number>>({})
  // Whether THIS hook instance is still mounted. The token refs above are
  // per instance, but the query cache is global: after an unmount → remount
  // (tab switch and back), a save begun by the OLD instance still passes its
  // own ownership check when it settles — its ref never saw the new
  // instance's saves — and would write a stale value over one the new
  // instance's save already persisted. A dead instance therefore never
  // writes the cache; it still invalidates, so the server's answer lands
  // either way. The effect body re-arms the flag because a Strict-Mode
  // mount runs the previous cleanup first.
  const mountedRef = useRef(true)
  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])
  const [pending, setPending] = useState<Record<string, PendingEntry | undefined>>({})

  const beginPending = (path: string, value: unknown): number => {
    const token = ++pendingSeqRef.current
    latestTokenRef.current[path] = token
    setPending(prev => ({ ...prev, [path]: { value, token } }))
    return token
  }
  const clearPending = (path: string, token: unknown) =>
    setPending(prev => {
      // No token (onMutate threw) or a newer save on the same path owns the
      // display now; leave the map untouched — returning the same reference
      // also skips a pointless re-render.
      if (token === undefined || prev[path]?.token !== token) return prev
      const next = { ...prev }
      delete next[path]
      return next
    })

  /** The value a control displays: the path's pending save, else the server's. */
  const shown = <T,>(path: string, server: T): T => {
    const entry = pending[path]
    return entry === undefined ? server : (entry.value as T)
  }

  /**
   * Build `useMutation` options wiring a mutation into the overlay. Call
   * sites needing an extra side effect (clear a local draft on success, drop
   * a dependent query on settle) spread the result and wrap the callback,
   * delegating back to the wrapped one.
   */
  const mutationOpts = <TVars, TData = unknown>(cfg: OptimisticPathMutation<TVars, TData>) => ({
    mutationFn: cfg.mutationFn,
    onMutate: (vars: TVars): number => {
      const path = cfg.path(vars)
      const token = beginPending(path, cfg.displayValue(vars))
      cfg.onSupersede?.(path)
      return token
    },
    onSuccess: (_data: TData, vars: TVars, token: number) => {
      const path = cfg.path(vars)
      // Only the path's LATEST save may write its accepted value: with A→B
      // in flight on one path and B settling first, A's later settle would
      // otherwise overwrite B in the cache, and a failed refetch would leave
      // stale A displayed while the server holds B.
      if (mountedRef.current && latestTokenRef.current[path] === token) {
        const cached = qc.getQueryData(cfg.queryKey)
        if (cached !== undefined) qc.setQueryData(cfg.queryKey, cfg.applyToCache(cached, vars))
      }
      return qc.invalidateQueries({ queryKey: cfg.queryKey })
    },
    onError: (err: unknown, vars: TVars, token: number | undefined) => {
      // Stop masking immediately (token-guarded and idempotent with the
      // onSettled clear) so no intermediate frame shows the failed value
      // beside its own failure report.
      const path = cfg.path(vars)
      clearPending(path, token)
      if (token !== undefined && latestTokenRef.current[path] === token) cfg.onFailure?.(err, vars)
    },
    onSettled: (_data: TData | undefined, err: unknown, vars: TVars, token: number | undefined) => {
      clearPending(cfg.path(vars), token)
      if (err) qc.invalidateQueries({ queryKey: cfg.queryKey })
    },
  })

  return { shown, mutationOpts }
}

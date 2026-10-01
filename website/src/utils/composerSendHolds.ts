/**
 * Composer holds and attachment arrivals belong to one browser window.
 * Attachments are per-window composer state, and completed files land in the
 * producing window's inbox, so another window's composer has nothing to wait for.
 */
import { useLayoutEffect, useRef, useSyncExternalStore } from 'react'

type Slot = string | null | undefined

const ARRIVALS_KEY = 'mc-composer-arrivals'
/** This browsing context's id, kept beside the arrivals in sessionStorage. A
 *  window opened by another starts with a copy of its opener's sessionStorage,
 *  id included, which is how a clone is recognised. */
const WINDOW_ID_KEY = 'mc-composer-window-id'

function browserStorage(): Storage | null {
  try { return typeof sessionStorage === 'undefined' ? null : sessionStorage } catch { return null }
}

function newWindowId(): string {
  try { if (typeof crypto !== 'undefined' && crypto.randomUUID) return crypto.randomUUID() } catch { /* fall through */ }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
}

/** The id the opener's sessionStorage holds, or null when there is no live
 *  same-origin opener to read. */
function openerWindowId(): string | null {
  try {
    if (typeof window === 'undefined') return null
    const opener = window.opener as Window | null
    if (!opener || opener.closed) return null
    return opener.sessionStorage.getItem(WINDOW_ID_KEY)
  } catch { return null }
}

/** Whether this window's stored arrivals are its own to hydrate. They are a
 *  clone exactly when this window still carries its opener's id: that is a
 *  popout's first load, before it has written anything of its own. The window
 *  then takes a fresh id, so every later load (reload, back/forward, or an
 *  ordinary navigation inside the popout) reads as its own. A window with no
 *  id yet takes one and keeps whatever is stored. */
function ownsStoredArrivals(): boolean {
  const store = browserStorage()
  if (!store) return true
  try {
    const mine = store.getItem(WINDOW_ID_KEY)
    const cloned = !!mine && mine === openerWindowId()
    if (!mine || cloned) store.setItem(WINDOW_ID_KEY, newWindowId())
    return !cloned
  } catch { return true }
}

class ComposerSync {
  private readonly holds = new Map<string, number>()
  private readonly controllers = new Map<string, Set<AbortController>>()
  private readonly arrivals = new Map<string, string[]>()
  private readonly listeners = new Set<() => void>()
  private readonly arrivalListeners = new Map<string, Set<() => void>>()
  private closed = false

  constructor() {
    if (ownsStoredArrivals()) this.seedArrivals()
    else this.dropClonedArrivals()
  }

  /** A popout's first load: its sessionStorage is its own copy of the
   *  opener's, so the cloned arrivals are removed from that copy. Later loads
   *  of the popout then hydrate only arrivals it wrote itself, never a clone
   *  of files the opener has since sent. The opener's storage is a separate
   *  copy and is untouched. */
  private dropClonedArrivals() {
    try { browserStorage()?.removeItem(ARRIVALS_KEY) } catch { /* unavailable storage holds no clone */ }
  }

  private seedArrivals() {
    const store = browserStorage()
    if (!store) return
    try {
      const parsed = JSON.parse(store.getItem(ARRIVALS_KEY) || '{}') as Record<string, unknown>
      if (!parsed || typeof parsed !== 'object') return
      for (const [slot, paths] of Object.entries(parsed)) {
        if (Array.isArray(paths) && paths.every(path => typeof path === 'string')) {
          this.arrivals.set(slot, paths)
        }
      }
    } catch { /* malformed or unavailable storage leaves the in-memory inbox empty */ }
  }

  private mirrorArrivals() {
    const store = browserStorage()
    if (!store) return
    try {
      const next = Object.fromEntries(this.arrivals)
      if (this.arrivals.size) store.setItem(ARRIVALS_KEY, JSON.stringify(next))
      else store.removeItem(ARRIVALS_KEY)
    } catch { /* quota or disabled storage cannot change the authoritative map */ }
  }

  private notify() {
    for (const listener of this.listeners) listener()
  }

  subscribe = (listener: () => void) => {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  holdComposerSend(slot: Slot) {
    if (!slot) return
    this.holds.set(slot, (this.holds.get(slot) ?? 0) + 1)
    this.notify()
  }

  releaseComposerSend(slot: Slot) {
    if (!slot) return
    const count = this.holds.get(slot) ?? 0
    if (count === 0) return
    if (count === 1) this.holds.delete(slot)
    else this.holds.set(slot, count - 1)
    this.notify()
  }

  isComposerSendHeld(slot: Slot): boolean {
    return !!slot && (this.holds.get(slot) ?? 0) > 0
  }

  landComposerAttachments(slot: Slot, paths: string[]) {
    if (!slot || !paths.length) return
    const current = this.arrivals.get(slot) ?? []
    const added = paths.filter((path, index) => !current.includes(path) && paths.indexOf(path) === index)
    if (!added.length) return
    this.arrivals.set(slot, [...current, ...added])
    this.mirrorArrivals()
    this.arrivalListeners.get(slot)?.forEach(listener => listener())
  }

  finishComposerAttachment(slot: Slot, paths: string[] = []) {
    if (!slot) return
    this.landComposerAttachments(slot, paths)
    this.releaseComposerSend(slot)
  }

  takeComposerArrivals(slot: Slot): string[] {
    if (!slot) return []
    const paths = this.arrivals.get(slot) ?? []
    if (this.arrivals.delete(slot)) this.mirrorArrivals()
    return paths
  }

  private restoreComposerArrivals(slot: string, paths: string[]) {
    const current = this.arrivals.get(slot) ?? []
    const restored = paths.filter((path, index) => !current.includes(path) && paths.indexOf(path) === index)
    if (!restored.length) return
    this.arrivals.set(slot, [...restored, ...current])
    this.mirrorArrivals()
  }

  subscribeComposerArrivals(
    slot: Slot,
    onArrive: (paths: string[], slot: string) => boolean | void,
  ): () => void {
    if (!slot) return () => {}
    const drain = () => {
      const paths = this.takeComposerArrivals(slot)
      if (paths.length && onArrive(paths, slot) === false) {
        this.restoreComposerArrivals(slot, paths)
      }
    }
    let subscriptions = this.arrivalListeners.get(slot)
    if (!subscriptions) {
      subscriptions = new Set()
      this.arrivalListeners.set(slot, subscriptions)
    }
    subscriptions.add(drain)
    drain()
    return () => {
      subscriptions!.delete(drain)
      if (subscriptions!.size === 0) this.arrivalListeners.delete(slot)
    }
  }

  registerComposerUpload(slot: Slot, controller: AbortController) {
    if (!slot) return
    const live = this.controllers.get(slot)
    if (live) live.add(controller)
    else this.controllers.set(slot, new Set([controller]))
    this.notify()
  }

  unregisterComposerUpload(slot: Slot, controller: AbortController) {
    if (!slot) return
    const live = this.controllers.get(slot)
    if (!live?.delete(controller)) return
    if (live.size === 0) this.controllers.delete(slot)
    this.notify()
  }

  cancelComposerUploads(slot: Slot) {
    if (!slot) return
    this.controllers.get(slot)?.forEach(controller => controller.abort())
  }

  isComposerUploadCancellable(slot: Slot): boolean {
    return !!slot && (this.controllers.get(slot)?.size ?? 0) > 0
  }

  close() {
    if (this.closed) return
    this.closed = true
    const hadHolds = this.holds.size > 0
    this.holds.clear()
    if (hadHolds) this.notify()
  }
}

function createProductionSync(): ComposerSync {
  return new ComposerSync()
}

let composerSync = createProductionSync()

export function holdComposerSend(slot: Slot) { composerSync.holdComposerSend(slot) }
export function releaseComposerSend(slot: Slot) { composerSync.releaseComposerSend(slot) }
export function isComposerSendHeld(slot: Slot) { return composerSync.isComposerSendHeld(slot) }
export function landComposerAttachments(slot: Slot, paths: string[]) { composerSync.landComposerAttachments(slot, paths) }
export function takeComposerArrivals(slot: Slot) { return composerSync.takeComposerArrivals(slot) }
export function finishComposerAttachment(slot: Slot, paths: string[] = []) { composerSync.finishComposerAttachment(slot, paths) }
export function registerComposerUpload(slot: Slot, controller: AbortController) { composerSync.registerComposerUpload(slot, controller) }
export function unregisterComposerUpload(slot: Slot, controller: AbortController) { composerSync.unregisterComposerUpload(slot, controller) }
export function cancelComposerUploads(slot: Slot) { composerSync.cancelComposerUploads(slot) }

export function useComposerSendHeld(slot: Slot) {
  return useSyncExternalStore(
    composerSync.subscribe,
    () => composerSync.isComposerSendHeld(slot),
    () => false,
  )
}

export function useComposerArrivals(
  slot: Slot,
  onArrive: (paths: string[], slot: string) => boolean | void,
  enabled = true,
) {
  const onArriveRef = useRef(onArrive)
  onArriveRef.current = onArrive
  useLayoutEffect(() => {
    if (!slot || !enabled) return
    return composerSync.subscribeComposerArrivals(slot, (paths, arrivalSlot) =>
      onArriveRef.current(paths, arrivalSlot))
  }, [slot, enabled])
}

export function useComposerUploadCancellable(slot: Slot) {
  return useSyncExternalStore(
    composerSync.subscribe,
    () => composerSync.isComposerUploadCancellable(slot),
    () => false,
  )
}

/** Independent logical window for deterministic tests. */
export function __createComposerSyncForTests() {
  return new ComposerSync()
}

/** Restore the module singleton and clear its reload mirror between tests. */
export function __resetComposerSendHoldsForTests() {
  composerSync.close()
  try { browserStorage()?.removeItem(ARRIVALS_KEY); browserStorage()?.removeItem(WINDOW_ID_KEY) } catch { /* unavailable test storage */ }
  composerSync = createProductionSync()
}

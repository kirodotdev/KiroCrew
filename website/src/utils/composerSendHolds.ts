/**
 * Composer holds and attachment arrivals belong to one browser window.
 * Attachments are per-window composer state. A completed file goes to a composer
 * of the producing window that shows its slot, or else into that slot's saved
 * draft, so another window's composer has nothing to wait for.
 */
import { useLayoutEffect, useRef, useSyncExternalStore } from 'react'

type Slot = string | null | undefined

/** Where a finished attachment goes when no composer showing its slot takes it:
 *  the producing host's persisted draft for that slot, the same store a draft
 *  waits in across a session switch, a pane or popout reload, and a remount. */
export type ParkAttachments = (paths: string[], slot: string) => void

type ArrivalListener = (paths: string[], slot: string) => boolean | void

class ComposerSync {
  private readonly holds = new Map<string, number>()
  private readonly controllers = new Map<string, Set<AbortController>>()
  private readonly listeners = new Set<() => void>()
  private readonly arrivalListeners = new Map<string, Set<ArrivalListener>>()
  private closed = false

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

  /** Hand finished paths to a composer showing the slot, or park them, then
   *  release the slot's hold. A composer in the middle of a switch answers
   *  false and the paths are parked too: nothing waits in memory, where a
   *  reload or a closed window would lose it. */
  finishComposerAttachment(slot: Slot, paths: string[] = [], park?: ParkAttachments) {
    if (!slot) return
    const unique = paths.filter((path, index) => paths.indexOf(path) === index)
    if (unique.length) {
      let taken = false
      for (const onArrive of this.arrivalListeners.get(slot) ?? []) {
        if (onArrive(unique, slot) !== false) { taken = true; break }
      }
      if (!taken) park?.(unique, slot)
    }
    this.releaseComposerSend(slot)
  }

  subscribeComposerArrivals(slot: Slot, onArrive: ArrivalListener): () => void {
    if (!slot) return () => {}
    let subscriptions = this.arrivalListeners.get(slot)
    if (!subscriptions) {
      subscriptions = new Set()
      this.arrivalListeners.set(slot, subscriptions)
    }
    subscriptions.add(onArrive)
    return () => {
      subscriptions!.delete(onArrive)
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

let composerSync = new ComposerSync()

export function holdComposerSend(slot: Slot) { composerSync.holdComposerSend(slot) }
export function releaseComposerSend(slot: Slot) { composerSync.releaseComposerSend(slot) }
export function isComposerSendHeld(slot: Slot) { return composerSync.isComposerSendHeld(slot) }
export function finishComposerAttachment(slot: Slot, paths: string[] = [], park?: ParkAttachments) {
  composerSync.finishComposerAttachment(slot, paths, park)
}
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

export function useComposerArrivals(slot: Slot, onArrive: ArrivalListener, enabled = true) {
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

/** Restore the module singleton between tests. */
export function __resetComposerSendHoldsForTests() {
  composerSync.close()
  composerSync = new ComposerSync()
}

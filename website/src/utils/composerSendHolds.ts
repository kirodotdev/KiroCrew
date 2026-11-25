/**
 * Composer holds and attachment arrivals belong to one browser window.
 * Attachments are per-window composer state, and completed files land in the
 * producing window's inbox, so another window's composer has nothing to wait for.
 */
import { useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react'

type Slot = string | null | undefined

/** One running upload as the composer shows it: its own chip, status and cancel. */
export type ComposerUpload = {
  controller: AbortController
  /** The file's name, or '' when the starter named none. */
  name: string
  /** Share of bytes sent, null until a positive total is reported. */
  ratio: number | null
  /** Bytes sent so far; a change restarts the stall window. */
  loaded: number
  /** Started on the wire with bytes left to send or an unknown total. */
  sending: boolean
}

const NO_UPLOADS: ComposerUpload[] = []

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
  /** Each running upload's current slot, so a hand-over moves exactly the
   *  uploads running at that moment and none started later. */
  private readonly uploadSlots = new Map<AbortController, string>()
  /** Bytes sent and the body's size, per running upload that has reported any. */
  private readonly sent = new Map<AbortController, { loaded: number; total: number }>()
  /** Each running upload's file name and start order. Keyed by controller, so
   *  a hand-over to another slot carries both. */
  private readonly uploadNames = new Map<AbortController, string>()
  private readonly uploadOrder = new Map<AbortController, number>()
  private uploadSeq = 0
  /** Per-slot upload snapshots, recomputed only after a notify. */
  private version = 0
  private readonly uploadSnapshots = new Map<string, { version: number; value: ComposerUpload[] }>()
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
    this.version++
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

  registerComposerUpload(slot: Slot, controller: AbortController, name = '') {
    if (!slot) return
    const live = this.controllers.get(slot)
    if (live) live.add(controller)
    else this.controllers.set(slot, new Set([controller]))
    this.uploadSlots.set(controller, slot)
    this.uploadNames.set(controller, name)
    this.uploadOrder.set(controller, ++this.uploadSeq)
    this.notify()
  }

  unregisterComposerUpload(slot: Slot, controller: AbortController) {
    if (!slot) return
    const live = this.controllers.get(slot)
    if (!live?.delete(controller)) return
    if (live.size === 0) this.controllers.delete(slot)
    this.uploadSlots.delete(controller)
    this.sent.delete(controller)
    this.uploadNames.delete(controller)
    this.uploadOrder.delete(controller)
    this.notify()
  }

  reportComposerUploadProgress(controller: AbortController, loaded: number, total: number) {
    if (!this.uploadSlots.has(controller)) return
    this.sent.set(controller, { loaded, total })
    this.notify()
  }

  /** The slot's running uploads in start order. The same array comes back
   *  until a notify, and after one while every entry is unchanged, so it is a
   *  stable `useSyncExternalStore` snapshot. */
  composerUploads(slot: Slot): ComposerUpload[] {
    if (!slot) return NO_UPLOADS
    const cached = this.uploadSnapshots.get(slot)
    if (cached?.version === this.version) return cached.value
    const live = [...(this.controllers.get(slot) ?? [])]
      .sort((a, b) => (this.uploadOrder.get(a) ?? 0) - (this.uploadOrder.get(b) ?? 0))
    if (!live.length) {
      this.uploadSnapshots.delete(slot)
      return NO_UPLOADS
    }
    const next = live.map((controller): ComposerUpload => {
      const sent = this.sent.get(controller)
      return {
        controller,
        name: this.uploadNames.get(controller) ?? '',
        ratio: sent && sent.total > 0 ? sent.loaded / sent.total : null,
        loaded: sent?.loaded ?? 0,
        sending: !!sent && (sent.total === 0 || sent.loaded < sent.total),
      }
    })
    const prev = cached?.value
    const same = !!prev && prev.length === next.length && prev.every((p, i) => {
      const n = next[i]
      return p.controller === n.controller && p.name === n.name && p.ratio === n.ratio && p.loaded === n.loaded && p.sending === n.sending
    })
    const value = same ? prev! : next
    this.uploadSnapshots.set(slot, { version: this.version, value })
    return value
  }

  /** Whether the slot has any upload registered, waiting or on the wire. */
  hasComposerUploads(slot: Slot): boolean {
    return !!slot && (this.controllers.get(slot)?.size ?? 0) > 0
  }

  /** The slot this upload belongs to now: where it started, or where a
   *  hand-over moved it. Undefined once it is unregistered. */
  composerUploadSlot(controller: AbortController): string | undefined {
    return this.uploadSlots.get(controller)
  }

  cancelComposerUploads(slot: Slot) {
    if (!slot) return
    this.controllers.get(slot)?.forEach(controller => controller.abort())
  }

  /** Abort one running upload, leaving its siblings in the slot running. */
  cancelComposerUpload(controller: AbortController) {
    if (this.uploadSlots.has(controller)) controller.abort()
  }

  /** A slot replaced by another (a memory-mode switch) hands the uploads
   *  running right now, each with its own Send hold, to the replacement, so
   *  Send stays held and each upload's chip (name, progress, cancel) stays
   *  where the user now is. Uploads
   *  started in `from` later stay in `from`, and so do holds no upload owns. */
  handOverComposerUploads(from: Slot, to: Slot) {
    if (!from || !to || from === to) return
    const live = this.controllers.get(from)
    if (!live) return
    this.controllers.delete(from)
    const target = this.controllers.get(to)
    if (target) live.forEach(controller => target.add(controller))
    else this.controllers.set(to, live)
    live.forEach(controller => this.uploadSlots.set(controller, to))
    const held = this.holds.get(from) ?? 0
    const moved = Math.min(held, live.size)
    if (moved) {
      if (held === moved) this.holds.delete(from)
      else this.holds.set(from, held - moved)
      this.holds.set(to, (this.holds.get(to) ?? 0) + moved)
    }
    this.notify()
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
export function handOverComposerUploads(from: Slot, to: Slot) { composerSync.handOverComposerUploads(from, to) }
export function finishComposerAttachment(slot: Slot, paths: string[] = []) { composerSync.finishComposerAttachment(slot, paths) }
export function registerComposerUpload(slot: Slot, controller: AbortController, name?: string) { composerSync.registerComposerUpload(slot, controller, name) }
export function unregisterComposerUpload(slot: Slot, controller: AbortController) { composerSync.unregisterComposerUpload(slot, controller) }
export function composerUploadSlot(controller: AbortController) { return composerSync.composerUploadSlot(controller) }
export function cancelComposerUploads(slot: Slot) { composerSync.cancelComposerUploads(slot) }
export function cancelComposerUpload(controller: AbortController) { composerSync.cancelComposerUpload(controller) }
export function reportComposerUploadProgress(controller: AbortController, loaded: number, total: number) {
  composerSync.reportComposerUploadProgress(controller, loaded, total)
}

/** How long an upload with bytes left to send may move none before it reads as stalled. */
export const UPLOAD_STALL_MS = 20_000

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

/** The slot's running uploads, one entry per request in start order, as a
 *  stable snapshot: a progress step of one upload re-renders its readers, and
 *  a hold change elsewhere does not. */
export function useComposerUploads(slot: Slot): ComposerUpload[] {
  return useSyncExternalStore(composerSync.subscribe, () => composerSync.composerUploads(slot), () => NO_UPLOADS)
}

/** Whether the slot has a running upload. A boolean snapshot, so a progress
 *  step does not re-render its reader: only the first upload starting and the
 *  last one ending do. */
export function useComposerUploadsRunning(slot: Slot): boolean {
  return useSyncExternalStore(composerSync.subscribe, () => composerSync.hasComposerUploads(slot), () => false)
}

/** Whether one upload is stalled: `sending` and no byte moved for
 *  `UPLOAD_STALL_MS`. A request reports its start, at zero bytes, when it is
 *  sent, so one that never reaches the server stalls too; a file still being
 *  prepared is not sending yet and does not. A new `key` restarts the window. */
export function useUploadStalled(key: unknown, sending: boolean, loaded: number): boolean {
  const [stalled, setStalled] = useState(false)
  useEffect(() => {
    setStalled(false)
    if (!sending) return
    const timer = setTimeout(() => setStalled(true), UPLOAD_STALL_MS)
    return () => clearTimeout(timer)
  }, [key, sending, loaded])
  return sending && stalled
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

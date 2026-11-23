/**
 * The store behind kept app pages: an installed app's routed page that stays
 * mounted, hidden, after the person opens another dashboard page, for as long as
 * the app says it is busy (`useKeepAlive` in the app SDK).
 *
 * Every routed app page is rendered by `KeptAppPagesLayer` into a stable `host`
 * element, and the route only lends it a `slot` to sit in. Leaving the route moves
 * `host` out of the slot; React never sees the move, so the app is not unmounted
 * and its microphone, sockets and timers keep running. A page that is not busy is
 * dropped when its route goes, exactly as an inline page would be.
 *
 * An app may call `useKeepAlive` from several components. Each call is a holder,
 * and the page is busy while any holder is: a caller going false, or unmounting,
 * releases only its own hold, and a caller that holds nothing changes nothing.
 * When the last holder lets go the store settles after the current commit, so a
 * caller that remounts (React runs the old cleanup before the new effect) keeps
 * the page. The holder sets stay bounded: one entry per mounted `useKeepAlive`
 * call, removed by its own cleanup, and the set goes with its page.
 */
import type { KeepAliveControl, KeepAliveStatus } from '../app-sdk/keepAlive'
import type { AppHostProps } from './AppHost'

/** How many apps may hold a keep-alive at once. Each kept page holds its whole
 *  React tree and whatever the app opened (a microphone, sockets), so the number
 *  stays small; an app asking past it is told `refused` and can say so. */
export const MAX_KEPT_APP_PAGES = 2

export type KeptAppRecord = AppHostProps['app']

export interface KeptAppPage {
  app: KeptAppRecord
  /** The element the app's AppHost is portalled into, for the page's whole life. */
  host: HTMLDivElement
  /** The routed page's element while the page is on screen; null while hidden. */
  slot: HTMLElement | null
  /** The dashboard path the page was opened at, for the indicator's Open button. */
  path: string
  /** Whether the app holds a keep-alive: some `useKeepAlive` caller is busy, or the
   *  last one let go in this commit and the store has not settled yet. */
  busy: boolean
}

export class KeptAppPagesStore {
  private pages = new Map<string, KeptAppPage>()
  private snapshot: readonly KeptAppPage[] = []
  private listeners = new Set<() => void>()
  private controls = new Map<string, KeepAliveControl>()
  private holders = new Map<string, Set<object>>()
  private settling = new Set<string>()
  /** Where a hidden page's host waits; the layer sets it while mounted. */
  park: HTMLElement | null = null

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener)
    return () => { this.listeners.delete(listener) }
  }

  getSnapshot = (): readonly KeptAppPage[] => this.snapshot

  /** The routed page for *app* is on screen in *slot*. */
  attach(app: KeptAppRecord, slot: HTMLElement, path: string): void {
    const existing = this.pages.get(app.name)
    const host = existing?.host ?? createHost()
    slot.appendChild(host)
    this.pages.set(app.name, { app, host, slot, path, busy: existing?.busy ?? false })
    this.emit()
  }

  /** The route that showed *name* in *slot* is gone. */
  detach(name: string, slot: HTMLElement): void {
    const page = this.pages.get(name)
    if (!page || page.slot !== slot) return
    if (!page.busy) {
      this.drop(name)
      return
    }
    if (this.park) this.park.appendChild(page.host)
    else page.host.remove()
    this.pages.set(name, { ...page, slot: null })
    this.emit()
  }

  /** A newer record for an app the store holds, such as a re-read after an update. */
  update(app: KeptAppRecord): void {
    const page = this.pages.get(app.name)
    if (!page || page.app === app) return
    this.pages.set(app.name, { ...page, app })
    this.emit()
  }

  /** One `useKeepAlive` call's request; *holder* is that call's own token. */
  request(name: string, holder: object, busy: boolean): KeepAliveStatus {
    const page = this.pages.get(name)
    if (!page) return 'unsupported'
    const held = this.holders.get(name)
    if (!busy) {
      if (held?.delete(holder) && held.size === 0) this.scheduleSettle(name)
      return 'off'
    }
    if (held?.has(holder)) return 'on'
    if (!page.busy) {
      const busyApps = [...this.pages.values()].filter(p => p.busy).length
      if (busyApps >= MAX_KEPT_APP_PAGES) return 'refused'
    }
    if (held) held.add(holder)
    else this.holders.set(name, new Set([holder]))
    if (!page.busy) this.set(name, { ...page, busy: true })
    return 'on'
  }

  /** A hidden page goes: the person pressed Stop, or the app was turned off or
   *  uninstalled. A page on screen belongs to its route and is left alone. */
  releaseHidden(name: string): void {
    if (this.pages.get(name)?.slot === null) this.drop(name)
  }

  /** The app threw. A visible page keeps its crash notice; a hidden one goes. */
  crashed(name: string): void {
    const page = this.pages.get(name)
    if (!page) return
    if (page.slot === null) {
      this.drop(name)
      return
    }
    this.holders.delete(name)
    if (page.busy) this.set(name, { ...page, busy: false })
  }

  /** The stable control an app's `useKeepAlive` calls; one object per app name. */
  controlFor(name: string): KeepAliveControl {
    let control = this.controls.get(name)
    if (!control) {
      control = { request: (holder: object, busy: boolean) => this.request(name, holder, busy) }
      this.controls.set(name, control)
    }
    return control
  }

  private scheduleSettle(name: string): void {
    if (this.settling.has(name)) return
    this.settling.add(name)
    queueMicrotask(() => {
      this.settling.delete(name)
      this.settle(name)
    })
  }

  /** The last holder let go: a hidden page closes, a page on screen stops holding. */
  private settle(name: string): void {
    const page = this.pages.get(name)
    if (!page?.busy || (this.holders.get(name)?.size ?? 0) > 0) return
    this.holders.delete(name)
    if (page.slot === null) this.drop(name)
    else this.set(name, { ...page, busy: false })
  }

  private set(name: string, page: KeptAppPage): void {
    this.pages.set(name, page)
    this.emit()
  }

  private drop(name: string): void {
    const page = this.pages.get(name)
    this.pages.delete(name)
    this.holders.delete(name)
    page?.host.remove()
    this.emit()
  }

  private emit(): void {
    this.snapshot = [...this.pages.values()]
    for (const listener of this.listeners) listener()
  }
}

function createHost(): HTMLDivElement {
  const host = document.createElement('div')
  // `contents`, so the app's page lays out against the route's parent exactly as
  // it would with no host element in between.
  host.style.display = 'contents'
  host.dataset.keptAppHost = ''
  return host
}

/**
 * Where the agent's browser is shown BIG: the "stage".
 *
 * The chat card is small on purpose. Expand (and a finished card's Replay)
 * moves the picture to one big view: the side panel's Preview tab, in its
 * "Agent browser" mode, on a page whose host can open the side panel (the chat
 * page registers itself with `registerBrowserStageHost`). With no host (the
 * app-sdk embed, the Members page) or on a phone, the same view opens as a
 * full-screen sheet instead (`BrowserLiveSheet`).
 *
 * One stage per session, held here as a module store for the same reason the
 * live store is one: `TurnBlock` is store-free, and the card and the panel sit
 * in two unrelated subtrees. The card reads the stage to fold itself to a
 * one-line "Showing in side panel" placeholder; the Preview tab reads it to pick
 * its mode. Clearing the stage (Bring back, switching to Local preview, the
 * panel closing) puts the picture back in the chat.
 */

import { useSyncExternalStore } from 'react'

export type BrowserStageMode = 'live' | 'replay'
/** Where the big view shows: the side panel (a host), or the full-screen sheet. */
export type BrowserStageSurface = 'panel' | 'sheet'

export interface BrowserStage {
  sessionKey: string
  /**
   * The turn shown. `null` follows the session's current turn: Expand on a live
   * card means "show me what the agent is doing now", so a new turn moves the
   * big view with it.
   */
  turnId: string | null
  mode: BrowserStageMode
  surface: BrowserStageSurface
  /** Bumped on every open, so a second Replay click restarts from the start. */
  nonce: number
}

export interface BrowserStageOpen {
  turnId: string | null
  mode: BrowserStageMode
  /** Force the sheet (a phone); otherwise the panel when a host is registered. */
  preferSheet?: boolean
}

/** Opens the side panel on the Preview tab for `sessionKey`. */
export type BrowserStageHost = (sessionKey: string) => void

type Listener = () => void

class BrowserStageStore {
  private stages = new Map<string, BrowserStage>()
  private hosts: BrowserStageHost[] = []
  private listeners = new Set<Listener>()
  private nonce = 0

  subscribe = (l: Listener): (() => void) => {
    this.listeners.add(l)
    return () => { this.listeners.delete(l) }
  }

  private notify(): void { for (const l of this.listeners) l() }

  get(sessionKey: string | null): BrowserStage | null {
    return sessionKey ? this.stages.get(sessionKey) ?? null : null
  }

  hasHost(): boolean { return this.hosts.length > 0 }

  /** The chat page registers here while mounted; the newest host wins. */
  registerHost(host: BrowserStageHost): () => void {
    this.hosts.push(host)
    this.notify()
    return () => {
      this.hosts = this.hosts.filter(h => h !== host)
      // A page that goes away takes its panel with it: a panel stage with no
      // panel would leave the card folded to a line pointing at nothing.
      if (this.hosts.length === 0) {
        for (const [k, s] of this.stages) if (s.surface === 'panel') this.stages.delete(k)
      }
      this.notify()
    }
  }

  open(sessionKey: string, req: BrowserStageOpen): BrowserStage {
    const host: BrowserStageHost | undefined = this.hosts[this.hosts.length - 1]
    const surface: BrowserStageSurface = !req.preferSheet && this.hosts.length > 0 ? 'panel' : 'sheet'
    this.nonce += 1
    const stage: BrowserStage = { sessionKey, turnId: req.turnId, mode: req.mode, surface, nonce: this.nonce }
    this.stages.set(sessionKey, stage)
    this.notify()
    if (surface === 'panel' && host) host(sessionKey)
    return stage
  }

  /** Change what the open stage shows (the timeline's turn picker, Live). */
  update(sessionKey: string, patch: Partial<Pick<BrowserStage, 'turnId' | 'mode'>>): void {
    const s = this.stages.get(sessionKey)
    if (!s) return
    const next = { ...s, ...patch }
    if (next.turnId === s.turnId && next.mode === s.mode) return
    this.stages.set(sessionKey, next)
    this.notify()
  }

  close(sessionKey: string): void {
    if (!this.stages.delete(sessionKey)) return
    this.notify()
  }

  __resetForTests(): void {
    this.stages.clear()
    this.hosts = []
    this.nonce = 0
    this.notify()
  }
}

export const browserStageStore = new BrowserStageStore()

export function registerBrowserStageHost(host: BrowserStageHost): () => void {
  return browserStageStore.registerHost(host)
}

/** The session's open stage, or null. */
export function useBrowserStage(sessionKey: string | null): BrowserStage | null {
  return useSyncExternalStore(
    browserStageStore.subscribe,
    () => browserStageStore.get(sessionKey),
    () => null,
  )
}

/** The stage's turn: the pinned one, else the session's current one. */
export function stageTurnId(stage: BrowserStage | null, currentTurnId: string | null): string | null {
  if (!stage) return null
  return stage.turnId ?? currentTurnId
}

import { useEffect, useSyncExternalStore } from 'react'

/**
 * Which surface shows an ask's sheet, and whether the person folded it away.
 *
 * The same ask can be on screen twice: the chat card and the big view (side
 * panel, or the phone's full-screen sheet). Two open sheets would mean two
 * password boxes and two focus traps fighting, so exactly one surface OWNS
 * the sheet and every other shows the one-line hint. The owner is the surface
 * the person last opened it from, else the one with the highest priority
 * (the big view, while it is showing, outranks the card).
 *
 * `answered` hides an ask the person already answered until the gateway's next
 * event clears it, so a click is not followed by the same sheet for a beat.
 */
type Listener = () => void

interface Surface { askId: string; priority: number }

class AskUi {
  private surfaces = new Map<string, Surface>()
  private claimed = new Map<string, string>()
  private collapsed = new Set<string>()
  private answered = new Set<string>()
  private listeners = new Set<Listener>()
  private version = 0

  subscribe = (l: Listener) => { this.listeners.add(l); return () => { this.listeners.delete(l) } }
  snapshot = () => this.version
  private notify() { this.version += 1; for (const l of this.listeners) l() }

  register(instanceId: string, askId: string, priority: number): void {
    this.surfaces.set(instanceId, { askId, priority })
    this.notify()
  }

  unregister(instanceId: string): void {
    if (!this.surfaces.delete(instanceId)) return
    for (const [ask, inst] of this.claimed) if (inst === instanceId) this.claimed.delete(ask)
    this.notify()
  }

  owner(askId: string): string | null {
    const c = this.claimed.get(askId)
    if (c && this.surfaces.get(c)?.askId === askId) return c
    let best: string | null = null
    let bestP = -Infinity
    for (const [id, s] of this.surfaces) if (s.askId === askId && s.priority > bestP) { best = id; bestP = s.priority }
    return best
  }

  /** Open the sheet on this surface. */
  open(instanceId: string, askId: string): void {
    this.claimed.set(askId, instanceId)
    this.collapsed.delete(askId)
    this.notify()
  }

  collapse(askId: string): void { this.collapsed.add(askId); this.notify() }
  isCollapsed(askId: string): boolean { return this.collapsed.has(askId) }

  markAnswered(askId: string): void { this.answered.add(askId); this.notify() }
  isAnswered(askId: string): boolean { return this.answered.has(askId) }

  __resetForTests(): void {
    this.surfaces.clear(); this.claimed.clear(); this.collapsed.clear(); this.answered.clear(); this.notify()
  }
}

export const askUi = new AskUi()

/** This surface's view of the ask: hidden, the hint, or the open sheet. */
export function useAskSurface(instanceId: string, askId: string | null, priority: number): 'none' | 'hint' | 'sheet' {
  useEffect(() => {
    if (!askId) return
    askUi.register(instanceId, askId, priority)
    return () => askUi.unregister(instanceId)
  }, [instanceId, askId, priority])
  useSyncExternalStore(askUi.subscribe, askUi.snapshot, askUi.snapshot)
  if (!askId || askUi.isAnswered(askId)) return 'none'
  if (askUi.isCollapsed(askId)) return 'hint'
  // Before the register effect has run, a lone surface is its own owner.
  const owner = askUi.owner(askId)
  return owner === null || owner === instanceId ? 'sheet' : 'hint'
}

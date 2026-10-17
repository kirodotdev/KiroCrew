/**
 * The containers a `ui.find` probe may open: only those a shared primitive
 * registered here, each with a paired open / restore that drives the
 * primitive's OWN React state (never a click, a key or any other DOM event).
 *
 * A primitive registers an instance only when it declares opening it has no
 * effect of its own (see each primitive's `guideProbe` prop):
 *
 * - `DropdownMenu` and `Popover`: an uncontrolled instance with no
 *   `onOpenChange` (nothing outside the primitive hears it open), or one whose
 *   caller passes `guideProbe`; `guideProbe={false}` keeps any instance out.
 * - `Tabs`: only a rail declared `guideProbe="local"`, whose selection lives
 *   in component state and never in the address or history.
 *
 * While a probe holds an instance open (`useProbeHold`), the primitive moves
 * no focus and dismisses on nothing outside it: its content skips the
 * open and close autofocus and ignores focus or presses outside, so nobody's
 * field blurs (a blur can save a setting) and no other open layer closes.
 * A real press, key or wheel outside the guide's own layer ends the hold at
 * once, before the primitive hears that input, and the instance behaves as
 * if the person had opened it.
 *
 * Anything else (a hand-rolled menu, a disclosure button, a native
 * `<details>`, a destructive confirm toggle, a tab that navigates) is never
 * opened by the probe: the guide asks the person to open it.
 */
import { createContext, useCallback, useContext, useEffect, useRef, useState, type Context, type MutableRefObject, type Ref } from 'react'

export type ProbeKind = 'popup' | 'tab'

export interface ProbeTarget {
  kind: ProbeKind
  /** The control the person presses to open it; the guide points at this. */
  trigger: () => HTMLElement | null
  isOpen: () => boolean
  /** Opens it through its own state and returns the paired restore. */
  open: () => () => void
}

export interface ProbeEntry extends ProbeTarget {
  id: number
}

const entries = new Map<number, ProbeEntry>()
let nextId = 1

export function registerProbeTarget(target: ProbeTarget): () => void {
  const id = nextId++
  entries.set(id, { ...target, id })
  return () => { entries.delete(id) }
}

/** Every registered container, in registration order. */
export function probeTargets(): ProbeEntry[] {
  return [...entries.values()]
}

export function probeTargetById(id: number): ProbeEntry | undefined {
  return entries.get(id)
}

/**
 * Register *target* while *enabled*. The callbacks are read through a ref,
 * so the registration sees the latest state without re-registering.
 */
export function useProbeTarget(enabled: boolean, target: ProbeTarget): void {
  const ref = useRef(target)
  ref.current = target
  useEffect(() => {
    if (!enabled) return
    return registerProbeTarget({
      get kind() { return ref.current.kind },
      trigger: () => ref.current.trigger(),
      isOpen: () => ref.current.isOpen(),
      open: () => ref.current.open(),
    })
  }, [enabled])
}

/** Where a primitive's trigger reports its element to its own root. */
export type TriggerSink = (el: HTMLElement | null) => void

/** One context per primitive, so a trigger never reports to another kind of root. */
export function createTriggerContext() {
  return createContext<TriggerSink | null>(null)
}

/** A trigger's ref: the caller's own, plus its root's sink (from *ctx*). */
export function useTriggerRef<T extends HTMLElement>(ctx: Context<TriggerSink | null>, forwarded: Ref<T> | undefined): (el: T | null) => void {
  const sink = useContext(ctx)
  return useCallback((el: T | null) => {
    sink?.(el)
    if (typeof forwarded === 'function') forwarded(el)
    else if (forwarded) (forwarded as MutableRefObject<T | null>).current = el
  }, [sink, forwarded])
}

/** The guide's own layer: input there is the guide's, never the person's on the page. */
export const GUIDE_LAYER_SELECTOR = '[data-testid="guide-pill"], [data-guide-layer]'

/** The person's own input, heard in the capture phase before any primitive's. */
export const PERSON_INPUTS = ['pointerdown', 'keydown', 'wheel'] as const

/** Whether *e* is the person's input on the page (outside the guide's own layer). */
export function isPersonInput(e: Event): boolean {
  const t = e.target
  return !(t instanceof Element && t.closest(GUIDE_LAYER_SELECTOR))
}

export interface ProbeHold {
  /** True from a probe's open until the person's next input (read by the content's handlers). */
  held: MutableRefObject<boolean>
  /** True from a probe's open until the instance next closes (state: a render can depend on it). */
  heldOpen: boolean
  /** A probe is opening the instance: hold it until the person's next input. */
  hold: () => void
}

/** A primitive root's hold (see the module comment); *open* is its open state. */
export function useProbeHold(open: boolean): ProbeHold {
  const held = useRef(false)
  const [heldOpen, setHeldOpen] = useState(false)
  const off = useRef<(() => void) | null>(null)
  const release = useCallback(() => {
    held.current = false
    off.current?.()
    off.current = null
  }, [])
  const hold = useCallback(() => {
    release()
    held.current = true
    setHeldOpen(true)
    const onInput = (e: Event) => { if (isPersonInput(e)) release() }
    for (const type of PERSON_INPUTS) window.addEventListener(type, onInput, true)
    off.current = () => { for (const type of PERSON_INPUTS) window.removeEventListener(type, onInput, true) }
  }, [release])
  useEffect(() => { if (!open) setHeldOpen(false) }, [open])
  useEffect(() => release, [release])
  return { held, heldOpen, hold }
}

/** The hold of the primitive whose content renders here, for that content's handlers. */
export const ProbeHoldContext = createContext<MutableRefObject<boolean> | null>(null)

/**
 * *own*, except while the probe holds the instance: then the event is
 * prevented (no autofocus, no dismissal) and *own* does not run.
 */
export function heldGuard<E extends Event>(held: MutableRefObject<boolean> | null, own: ((e: E) => void) | undefined): ((e: E) => void) | undefined {
  if (!held) return own
  return (e: E) => {
    if (held.current) { e.preventDefault(); return }
    own?.(e)
  }
}

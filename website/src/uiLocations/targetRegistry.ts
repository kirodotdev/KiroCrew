/**
 * The trusted target registry: which DOM elements React itself rendered as a
 * guide target, and under which identity.
 *
 * A guide finds its controls by identity, never by text: a registered UI
 * location (`uiLocation(id)`), a build-stamped auto site (`data-ui-auto` on a
 * reviewed primitive), a shared primitive's row (`guideTarget(id)`), a
 * guide anchor (`guideAnchor(name)`), a picker item (`guidePick(name)`, with
 * `guidePickAlias(name)` for the name the gateway knows it by), the
 * container that belongs to one item (`guidePickOf(name)`), an item's own
 * pick control (`guidePickControl(kind)`) and a removal's final control
 * (`guideConfirm()`). Each of those helpers returns the
 * debug attribute AND a React callback ref; only the ref writes here. Any
 * other DOM (an SVG artifact, a markdown reply, a file preview) can copy the
 * attribute strings, but it cannot run a ref, so a resolver that reads only
 * this registry never mistakes it for a control. The attributes stay for
 * tests, e2e selectors and debugging; nothing in the guide reads them.
 *
 * A leaf module (React types only), so a shared primitive can register
 * itself without importing the guide.
 */
import type { MutableRefObject, Ref, RefCallback } from 'react'

/** The identities an element can be registered under. */
export type TargetKind = 'location' | 'auto' | 'target' | 'anchor' | 'pick' | 'pickAlias' | 'pickOf' | 'pickControl' | 'confirm'

type Meta = Partial<Record<TargetKind, string>>

const metaOf = new WeakMap<Element, Meta>()
/** `[kind, id]` -> the elements registered under it (connected or awaiting prune). */
const byKey = new Map<string, Set<Element>>()
/** Every element registered under each kind, for scans inside a container. */
const byKind = new Map<TargetKind, Set<Element>>()
const callbacks = new Map<string, RefCallback<Element>>()

const keyOf = (kind: TargetKind, id: string) => JSON.stringify([kind, id])

function add(kind: TargetKind, id: string, el: Element): void {
  const meta = metaOf.get(el) ?? {}
  const was = meta[kind]
  if (was !== undefined && was !== id) byKey.get(keyOf(kind, was))?.delete(el)
  meta[kind] = id
  metaOf.set(el, meta)
  const key = keyOf(kind, id)
  let set = byKey.get(key)
  if (!set) byKey.set(key, (set = new Set()))
  set.add(el)
  let all = byKind.get(kind)
  if (!all) byKind.set(kind, (all = new Set()))
  all.add(el)
}

function dropEl(kind: TargetKind, el: Element): void {
  const meta = metaOf.get(el)
  const id = meta?.[kind]
  if (id !== undefined) {
    byKey.get(keyOf(kind, id))?.delete(el)
    delete meta![kind]
  }
  byKind.get(kind)?.delete(el)
}

/**
 * Drop every element of *kind* and *id* React no longer has in the document. Run
 * after the commit that detached the ref: React clears a ref before it
 * removes the node, so at `ref(null)` time the node is still connected.
 */
function prune(kind: TargetKind, id: string): void {
  const set = byKey.get(keyOf(kind, id))
  if (!set) return
  for (const el of [...set]) {
    if (!el.isConnected) dropEl(kind, el)
    else if (metaOf.get(el)?.[kind] !== id) set.delete(el)
  }
  if (set.size === 0) byKey.delete(keyOf(kind, id))
}

const schedule = typeof queueMicrotask === 'function' ? queueMicrotask : (f: () => void) => { void Promise.resolve().then(f) }

/**
 * The one stable callback ref for *kind* and *id*: the same function on every
 * render, so React attaches it once per mount and detaches it on unmount
 * (a StrictMode double mount re-adds the same element to a set).
 */
export function registerRef(kind: TargetKind, id: string): RefCallback<Element> {
  const key = keyOf(kind, id)
  let cb = callbacks.get(key)
  if (!cb) {
    cb = (el: Element | null) => {
      if (el) add(kind, id, el)
      else schedule(() => prune(kind, id))
    }
    callbacks.set(key, cb)
  }
  return cb
}

/** How many mounted uses hold each element's `[kind, id]` registration (`ownerRef`). */
const holds = new WeakMap<Element, Map<string, number>>()

function release(kind: TargetKind, id: string, el: Element): void {
  const per = holds.get(el)
  const key = keyOf(kind, id)
  const n = (per?.get(key) ?? 0) - 1
  if (n > 0) { per!.set(key, n); return }
  per?.delete(key)
  if (metaOf.get(el)?.[kind] === id) dropEl(kind, el)
  const set = byKey.get(key)
  if (set && set.size === 0) byKey.delete(key)
}

/**
 * A registration owned by one mounted use: the callback remembers the element
 * it was attached to, so its `ref(null)` removes exactly that entry whether
 * or not the element is still in the document (a conditional spread dropped
 * while React keeps the node). A fresh owner per render: React detaches the
 * previous one and attaches the next in the same commit, and no reader runs
 * in between.
 */
export function ownerRef(kind: TargetKind, id: string): RefCallback<Element> {
  return trackingRef(kind, id)
}

function trackingRef(kind: TargetKind, id: string, own?: Ref<Element>): RefCallback<Element> {
  let mine: Element | null = null
  return (el: Element | null) => {
    if (el === mine) return
    if (mine) release(kind, id, mine)
    mine = el
    if (el) {
      const key = keyOf(kind, id)
      let per = holds.get(el)
      if (!per) holds.set(el, (per = new Map()))
      per.set(key, (per.get(key) ?? 0) + 1)
      add(kind, id, el)
    }
    assignRef(own, el)
  }
}

const ownedBy = new WeakMap<object, Map<string, RefCallback<Element>>>()

/**
 * The registration for *kind* and *id* merged with the element's own ref.
 * With an own ref the callback is the same every render (keyed on that ref,
 * which belongs to one mounted use), so *own* hears only real attaches and
 * detaches: a callback ref that sets state on its node would otherwise loop.
 * Without one it is a fresh `ownerRef`. Either way its `ref(null)` removes
 * exactly the entry it added.
 */
export function ownedRef<T extends Element>(kind: TargetKind, id: string, own?: Ref<T>): RefCallback<T> {
  if (!own) return trackingRef(kind, id) as RefCallback<T>
  let byKey2 = ownedBy.get(own as object)
  if (!byKey2) ownedBy.set(own as object, (byKey2 = new Map()))
  const key = keyOf(kind, id)
  let cb = byKey2.get(key)
  if (!cb) byKey2.set(key, (cb = trackingRef(kind, id, own as Ref<Element>)))
  return cb as RefCallback<T>
}

function assignRef<T>(ref: Ref<T> | undefined, value: T | null): void {
  if (!ref) return
  if (typeof ref === 'function') ref(value)
  else (ref as MutableRefObject<T | null>).current = value
}

const singles = new WeakMap<object, RefCallback<unknown>>()
const pairs = new WeakMap<object, WeakMap<object, RefCallback<unknown>>>()

/**
 * Several refs as one callback (a forwarding component's own ref and the one
 * it was handed). Two stable refs yield the same callback every render, so
 * React does not detach and reattach the element on each one.
 */
export function mergeRefs<T>(...refs: Array<Ref<T> | undefined>): RefCallback<T> {
  const live = refs.filter((r): r is Ref<T> => !!r)
  if (live.length === 1) {
    const own = live[0] as unknown as object
    let cb = singles.get(own) as RefCallback<T> | undefined
    if (!cb) {
      cb = (el: T | null) => assignRef(live[0], el)
      singles.set(own, cb as RefCallback<unknown>)
    }
    return cb
  }
  if (live.length === 2) {
    const [a, b] = live as unknown as [object, object]
    let inner = pairs.get(a)
    if (!inner) pairs.set(a, (inner = new WeakMap()))
    let cb = inner.get(b) as RefCallback<T> | undefined
    if (!cb) {
      cb = (el: T | null) => { for (const r of live) assignRef(r, el) }
      inner.set(b, cb as RefCallback<unknown>)
    }
    return cb
  }
  return (el: T | null) => { for (const r of live) assignRef(r, el) }
}

/**
 * The ref a reviewed primitive puts on the element it renders (`Btn`,
 * `IconButton`, `Toggle`, ...): registers it under the build-stamped auto
 * site id it was handed as the `data-ui-auto` prop, merged with its own ref.
 * A prop comes from the source the stamp was cut from, never from the page.
 */
export function autoSiteRef<T extends Element>(site: unknown, own?: Ref<T>): Ref<T> | undefined {
  return typeof site === 'string' && site ? ownedRef<T>('auto', site, own) : own
}

/** Every connected element registered as *kind* and *id*. */
export function registeredCopies(kind: TargetKind, id: string): HTMLElement[] {
  const set = byKey.get(keyOf(kind, id))
  if (!set) return []
  return [...set].filter((el): el is HTMLElement => el.isConnected && el instanceof HTMLElement && metaOf.get(el)?.[kind] === id)
}

/** Every connected element registered under *kind* inside *root* (itself included). */
export function registeredWithin(kind: TargetKind, root: Element | Document): HTMLElement[] {
  const all = byKind.get(kind)
  if (!all) return []
  const out: HTMLElement[] = []
  for (const el of all) {
    if (!el.isConnected || !(el instanceof HTMLElement)) continue
    if (root === el || root.contains(el)) out.push(el)
  }
  return out
}

/** The id *el* itself is registered under as *kind*, if any. */
export function registeredId(el: Element | null | undefined, kind: TargetKind): string | undefined {
  return el && el.isConnected ? metaOf.get(el)?.[kind] : undefined
}

/** The nearest ancestor of *el* (itself included) registered under *kind*, with its id. */
export function closestRegistered(el: Element | null | undefined, kind: TargetKind): { el: HTMLElement; id: string } | null {
  for (let n: Element | null = el ?? null; n; n = n.parentElement) {
    const id = metaOf.get(n)?.[kind]
    if (id !== undefined && n.isConnected && n instanceof HTMLElement) return { el: n, id }
  }
  return null
}

/** How many entries the registry holds, connected or not yet pruned (tests: nothing lingers after an unmount). */
export function registrySize(): number {
  let n = 0
  for (const set of byKey.values()) n += set.size
  return n
}

/** The props one helper hands its element: the debug attribute and the registering ref. */
export type Registered<A extends string, V extends string = string> = { [K in A]: V } & { ref: RefCallback<Element> }

function marker<A extends string, V extends string>(attr: A, kind: TargetKind, id: V, own?: Ref<Element>): Registered<A, V> {
  return { [attr]: id, ref: ownedRef(kind, id, own) } as Registered<A, V>
}

export const GUIDE_TARGET_ATTR = 'data-guide-target'
export const GUIDE_ANCHOR_ATTR = 'data-guide-anchor'
export const GUIDE_PICK_ATTR = 'data-guide-pick'
export const GUIDE_PICK_OF_ATTR = 'data-guide-pick-of'
export const GUIDE_PICK_CONTROL_ATTR = 'data-guide-pick-control'
export const GUIDE_PICK_ALIAS_ATTR = 'data-guide-pick-alias'
export const GUIDE_CONFIRM_ATTR = 'data-guide-confirm'

/** The registered location a row drawn by a shared primitive answers to (`./trustRoot.ts` in the guide). */
export function guideTarget(id: string, own?: Ref<Element>): Registered<typeof GUIDE_TARGET_ATTR> {
  return marker(GUIDE_TARGET_ATTR, 'target', id, own)
}

/** A guide anchor (`GUIDE_ANCHORS`) on the element a guide step points at. */
export function guideAnchor(name: string, own?: Ref<Element>): Registered<typeof GUIDE_ANCHOR_ATTR> {
  return marker(GUIDE_ANCHOR_ATTR, 'anchor', name, own)
}

/** A picker item: the row or card of one entity, named as the person sees it. */
export function guidePick(name: string, own?: Ref<Element>): Registered<typeof GUIDE_PICK_ATTR> {
  return marker(GUIDE_PICK_ATTR, 'pick', name, own)
}

/**
 * A picker item's other name: the one the gateway knows it by (a built-in
 * app's manifest name) when the card shows a translated one. A pick naming
 * either finds the item; the panel always says the name the card shows.
 */
export function guidePickAlias(name: string, own?: Ref<Element>): Registered<typeof GUIDE_PICK_ALIAS_ATTR> {
  return marker(GUIDE_PICK_ALIAS_ATTR, 'pickAlias', name, own)
}

/** A container (a tile's menu) that belongs to the entity *name*, though it portals out of its row. */
export function guidePickOf(name: string, own?: Ref<Element>): Registered<typeof GUIDE_PICK_OF_ATTR> {
  return marker(GUIDE_PICK_OF_ATTR, 'pickOf', name, own)
}

/** The control inside a picker item a pick step outlines instead of the item (a row's checkbox). */
export function guidePickControl(kind: string, own?: Ref<Element>): Registered<typeof GUIDE_PICK_CONTROL_ATTR> {
  return marker(GUIDE_PICK_CONTROL_ATTR, 'pickControl', kind, own)
}

/**
 * The control that carries out a removal: a confirm dialog's own Delete, or
 * the final button of an inline confirm (a dialog that swaps in "Keep" and
 * the real Delete). A guide's destructive step completes only on a press of
 * one of these, never because the pressed control or its dialog went away.
 */
export function guideConfirm(own?: Ref<Element>): Registered<typeof GUIDE_CONFIRM_ATTR, ''> {
  return marker(GUIDE_CONFIRM_ATTR, 'confirm', '', own)
}

/** An optional marker: the helper's props when *value* is set, else nothing. */
export function maybe<P>(value: string | undefined | null | false, make: (v: string) => P): P | Record<string, never> {
  return value ? make(value) : {}
}

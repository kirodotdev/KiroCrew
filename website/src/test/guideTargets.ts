/**
 * Test fixtures for the trusted target registry: a JSX element in a test
 * stands in for a real render site by registering itself the way the
 * production helpers do (`uiLocation`, `guidePick`, a reviewed primitive's
 * `data-ui-auto`, ...). Each mark keeps its debug attribute; the merged ref
 * is what makes the guide see it. A fixture that should NOT count (content
 * copying the attribute strings) writes the plain attribute instead.
 */
import type { Ref } from 'react'
import { mergeRefs, registeredId, registerRef, type TargetKind } from '../uiLocations/targetRegistry'

const ATTR: Record<TargetKind, string> = {
  location: 'data-ui-location',
  auto: 'data-ui-auto',
  target: 'data-guide-target',
  anchor: 'data-guide-anchor',
  pick: 'data-guide-pick',
  pickOf: 'data-guide-pick-of',
  pickAlias: 'data-guide-pick-alias',
  confirm: 'data-guide-confirm',
  pickControl: 'data-guide-pick-control',
}

export type Marks = Partial<Record<TargetKind, string | null | undefined>>

export function marks(m: Marks, own?: Ref<never>): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  const refs: Array<Ref<Element>> = []
  for (const [kind, id] of Object.entries(m) as Array<[TargetKind, string | null | undefined]>) {
    if (id === undefined || id === null) continue
    out[ATTR[kind]] = id
    refs.push(registerRef(kind, id))
  }
  if (own) refs.push(own as Ref<Element>)
  if (refs.length) out.ref = mergeRefs<Element>(...refs)
  return out
}

/**
 * Every identity marker in the document (a location, an auto site, a
 * shared-row target, an anchor, a pick, its alias, a pick owner, a pick
 * control, a confirm) sits on an element registered under that same kind and
 * id: the forwarding proof for a component that receives one of the
 * registering helpers' spreads. Call it while the render is still mounted;
 * after an unmount there is nothing left to check.
 */
export function unregisteredMarkers(): string[] {
  const out: string[] = []
  for (const [kind, attr] of Object.entries(ATTR) as Array<[TargetKind, string]>) {
    for (const el of Array.from(document.querySelectorAll(`[${attr}]`))) {
      // Agent-authored content may copy the strings; it is never a target.
      if (el.closest('[data-guide-untrusted]')) continue
      const id = el.getAttribute(attr) ?? ''
      if (registeredId(el, kind) !== id) out.push(`${el.tagName.toLowerCase()}[${attr}="${id}"]`)
    }
  }
  return out
}

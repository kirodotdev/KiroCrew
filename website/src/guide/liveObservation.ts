/**
 * This tab's answer to a `guide_observe` frame: what it shows, right now, of
 * the curated locations and reveal scopes the gateway named.
 *
 * The frame goes to every owner socket and names ONE tab; any other tab
 * ignores it. The reply carries the bundle's build digest, this page load's
 * epoch, a rising sequence number, the gateway's own request id, and for each
 * asked id an enum status (`liveRegistry.liveTarget`), a scope state
 * (`open` / `closed` / `unknown`: no owner mounted, from
 * `liveRegistry.scopeOpen`) or a predicate state (`met` / `unmet` / `unknown`,
 * from `guidePredicates.predicateState`). Nothing else: no text, no labels, no
 * routes, no entity ids. A malformed or oversized request is dropped unanswered, and the
 * gateway then reports `not_observed`. Nothing is kept here.
 */
import { guideApi, type GuideObservationReply } from '../api/guide'
import { TAB_ID } from '../api/tabId'
import { GUIDE_BUILD_DIGEST, GUIDE_REVEAL_SCOPES } from '../uiLocations/guidePlans.gen'
import { predicateState } from './guidePredicates'
import { liveTarget, scopeOpen } from './liveRegistry'

/** The gateway's own caps (`guide_observe.MAX_TARGETS` / `MAX_SCOPES` / `MAX_PREDICATES`). */
export const OBSERVE_MAX_TARGETS = 16
export const OBSERVE_MAX_SCOPES = 8
export const OBSERVE_MAX_PREDICATES = 8

/** This page load: a reload is a new document whose answers never follow the old one's. */
export const DOCUMENT_EPOCH: string = `e${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`

let sequence = 0

const ID_RE = /^[A-Za-z0-9_.:-]{1,120}$/

function idList(v: unknown, max: number): string[] | null {
  if (!Array.isArray(v) || v.length > max) return null
  if (!v.every(x => typeof x === 'string' && ID_RE.test(x))) return null
  return v as string[]
}

/** The reply to *frame*, or null when it is not this tab's or not well formed. */
export function buildObservationReply(frame: unknown): GuideObservationReply | null {
  if (!frame || typeof frame !== 'object') return null
  const f = frame as Record<string, unknown>
  if (f.tab_id !== TAB_ID || typeof f.request_id !== 'string' || f.request_id.length > 64) return null
  const targets = idList(f.targets, OBSERVE_MAX_TARGETS)
  const scopes = idList(f.scopes, OBSERVE_MAX_SCOPES)
  // An older gateway sends no predicates: answer none.
  const predicates = f.predicates === undefined ? [] : idList(f.predicates, OBSERVE_MAX_PREDICATES)
  if (!targets || !scopes || !predicates) return null
  sequence += 1
  return {
    tab_id: TAB_ID,
    request_id: f.request_id,
    build_digest: GUIDE_BUILD_DIGEST,
    document_epoch: DOCUMENT_EPOCH,
    sequence,
    targets: targets.map(id => ({ id, status: liveTarget(id).status })),
    // A scope this build does not compile is `unknown`; so is one whose owner
    // is not mounted (nothing is known about it, which is not "closed").
    scopes: scopes.map(id => {
      const open = Object.hasOwn(GUIDE_REVEAL_SCOPES, id) ? scopeOpen(id) : null
      return { id, state: open === null ? 'unknown' as const : open ? 'open' as const : 'closed' as const }
    }),
    predicates: predicates.map(id => ({ id, state: predicateState(id) })),
  }
}

/** Handle one owner `guide_observe` frame. A failed reply only means not_observed. */
export function handleGuideObserveFrame(frame: unknown): void {
  const reply = buildObservationReply(frame)
  if (!reply) return
  guideApi.observe(reply).catch(() => undefined)
}

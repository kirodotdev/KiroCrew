/**
 * Registered-action guide API client (`/api/guide/*`).
 *
 * The agent PROPOSES a guide (an ordered list of product-owned actions); the
 * gateway holds it as a small state machine and this tab drives it only after
 * the human presses Start. Every write carries `{guide_id, tab_id, revision}`:
 * `tab_id` is this browser tab (`TAB_ID`), and `revision` is the one this tab
 * last read, so a write against a guide another tab has moved is refused (409)
 * instead of silently racing it.
 *
 * Routed through the blessed shared transport, like `pins.ts`, so a refusal is a
 * journaled `ApiError` with the backend's `code`.
 */
import type { QueryClient } from '@tanstack/react-query'
import { apiTransport } from './apiTransport'
import { TAB_ID } from './tabId'
import type { UiGuidePlan } from '../uiLocations/types'

export type GuideStatus = 'offered' | 'active' | 'target_missing' | 'completed' | 'cancelled' | 'expired'

/** Statuses after which nothing about the guide moves again. */
export const GUIDE_TERMINAL_STATUSES: ReadonlySet<GuideStatus> = new Set<GuideStatus>(['completed', 'cancelled', 'expired'])

export interface GuideAction {
  id: string
  params: Record<string, unknown>
  /** Evidence supplied by the gateway after an actual successful operation. */
  result?: Record<string, unknown>
  /** `ui.show`: the digest of the index the gateway accepted the guide against;
   *  a bundle carrying another refuses the guide (`build_mismatch`). */
  build_digest?: string
  /** `ui.show` (plan version 2): the plan format the gateway accepted. */
  plan_version?: number
  /** `ui.show`: each placement id the plan has -> its step ids, from the gateway's index. */
  placements?: Record<string, string[]>
  /** `ui.show`, once claimed: the placement the owning tab claimed with. */
  placement?: string
  /** `ui.show`, once claimed: that placement's step ids; reports may name only these. */
  step_ids?: string[]
  /** `ui.show` of an AUTO location: its single-step plan, from the build-time
   *  auto tier the gateway read. Used only when `build_digest` is this bundle's
   *  own auto digest (`UI_AUTO_BUILD_DIGEST`), i.e. the same build stamped it. */
  auto_plan?: UiGuidePlan
  /** `ui.find`: what this tab last reported its search found. */
  find?: GuideFindReport
  /** The offering agent's own words for this action, shown under its FINAL
   *  step; plain text the gateway checked (no links, no markup). */
  note?: string
}

export interface Guide {
  guide_id: string
  slot_key: string
  status: GuideStatus
  revision: number
  owner_tab: string | null
  action_index: number
  step_index: number
  actions: GuideAction[]
  reason: string | null
  expires_at: string | number | null
  lease_expires_at: string | number | null
  /** When it ended (terminal guides only). */
  finished_at?: string | number | null
  /** The owner hid its result line; the gateway stops serving it. */
  dismissed?: boolean
  /** The offering agent's own words for the whole guide, shown on the offer
   *  card and the first step; plain text the gateway checked. */
  intro?: string
}

/** `target_found` is recovery, not progress: the step whose target went
 *  missing is shown again, and the gateway returns it to `active` in place. */
export type GuideOutcome = 'observed' | 'target_missing' | 'target_found'

/** Why a target is missing, when the page can tell: several copies were shown
 *  at once, a runtime predicate its control needs is unmet, a gate step's
 *  gate is off, or a select step's picker has nothing to choose. */
export type GuideMissingDetail = 'ambiguous' | 'predicate_unmet' | 'gate_off' | 'selection_empty' | 'not_found'

/** What a `ui.find` search found, as the gateway may hear it: never page text. */
export interface GuideFindReport {
  result: 'found' | 'ambiguous' | 'none'
  count: number
  role?: string
  location_id?: string
  label_key?: string
}

/** A live state, as an observation reports it: never page text. */
export type GuideLiveState = 'open' | 'closed' | 'unknown'
export type GuidePredicateLiveState = 'met' | 'unmet' | 'unknown'

/** A tab's reply to one `guide_observe` frame: ids and enum states only. */
export interface GuideObservationReply {
  tab_id: string
  request_id: string
  build_digest: string
  document_epoch: string
  sequence: number
  targets: { id: string; status: string }[]
  scopes: { id: string; state: GuideLiveState }[]
  predicates: { id: string; state: GuidePredicateLiveState }[]
}

/** What every write identifies itself with. */
export interface GuideWriteBody {
  guide_id: string
  tab_id: string
  revision: number
}

export const GUIDE_PENDING_QUERY_KEY = ['guide-pending'] as const

const body = (g: Pick<Guide, 'guide_id' | 'revision'>): GuideWriteBody => ({
  guide_id: g.guide_id,
  tab_id: TAB_ID,
  revision: g.revision,
})

/** A write answers with the guide as it now stands; tolerate either envelope. */
const unwrap = (r: unknown): Guide | null => {
  if (!r || typeof r !== 'object') return null
  const o = r as { guide?: unknown }
  if (o.guide && typeof o.guide === 'object') return o.guide as Guide
  if (typeof (r as Guide).guide_id === 'string') return r as Guide
  return null
}

export const guideApi = {
  pending: (slot?: string): Promise<{ guides: Guide[] }> => {
    const { get, j } = apiTransport
    const q = slot ? `?slot=${encodeURIComponent(slot)}` : ''
    return get(`/api/guide/pending${q}`).then(j) as Promise<{ guides: Guide[] }>
  },
  /** `takeOver` is sent ONLY for an explicit human takeover from another tab.
   *  `placements`: per action, the `ui.show` placement this tab walks (null for
   *  any other action); the gateway records that placement's step ids. */
  claim: (g: Guide, takeOver = false, placements?: readonly (string | null)[]): Promise<Guide | null> => {
    const { post, j } = apiTransport
    const withPlacements = placements && placements.some(p => p !== null) ? { placements: [...placements] } : {}
    return post('/api/guide/claim', { ...body(g), ...(takeOver ? { take_over: true } : {}), ...withPlacements }).then(j).then(unwrap)
  },
  /** A `ui.show` step is named by its recorded step id as well as its index;
   *  the gateway refuses a report naming any other id. */
  progress: (g: Guide, outcome: GuideOutcome, resumeStepIndex?: number, detail?: GuideMissingDetail, find?: GuideFindReport): Promise<Guide | null> => {
    const { post, j } = apiTransport
    const ids = g.actions[g.action_index]?.step_ids
    const resuming = outcome === 'target_found' && resumeStepIndex !== undefined
    return post('/api/guide/progress', {
      ...body(g),
      action_index: g.action_index,
      step_index: g.step_index,
      ...(ids ? { step_id: ids[g.step_index] } : {}),
      outcome,
      ...(resuming ? { resume_step_index: resumeStepIndex } : {}),
      ...(resuming && ids ? { resume_step_id: ids[resumeStepIndex] } : {}),
      ...(outcome === 'target_missing' && detail ? { detail } : {}),
      ...(find ? { find } : {}),
    }).then(j).then(unwrap)
  },
  /** This tab cannot show the guide at all: its bundle is another build's. Only
   *  the guide's reason moves, so a tab of the matching build can still take it. */
  refuse: (g: Guide, reason: 'build_mismatch'): Promise<Guide | null> => {
    const { post, j } = apiTransport
    return post('/api/guide/refuse', { ...body(g), reason }).then(j).then(unwrap)
  },
  /** The viewport class changed mid-guide: walk the current `ui.show` action
   *  by *placement* from here on. The gateway allows it only at a step boundary
   *  both placements share (409 otherwise: the guide shows a missing target). */
  replan: (g: Guide, placement: string): Promise<Guide | null> => {
    const { post, j } = apiTransport
    return post('/api/guide/replan', { ...body(g), action_index: g.action_index, placement }).then(j).then(unwrap)
  },
  /** Answer one `guide_observe` frame. */
  observe: (reply: GuideObservationReply): Promise<unknown> => {
    const { post, j } = apiTransport
    return post('/api/guide/observe', reply).then(j)
  },
  heartbeat: (g: Guide): Promise<Guide | null> => {
    const { post, j } = apiTransport
    return post('/api/guide/heartbeat', body(g)).then(j).then(unwrap)
  },
  /** `reason` 'saved_without_guide': the action's save went through without
   *  the guide, which therefore ends saying so rather than "cancelled". */
  cancel: (g: Guide, reason?: 'saved_without_guide'): Promise<Guide | null> => {
    const { post, j } = apiTransport
    return post('/api/guide/cancel', reason ? { ...body(g), reason } : body(g)).then(j).then(unwrap)
  },
  /** Hide an ENDED guide's result line, for every tab (the gateway records it). */
  dismiss: (g: Pick<Guide, 'guide_id'>): Promise<Guide | null> => {
    const { post, j } = apiTransport
    return post('/api/guide/dismiss', { guide_id: g.guide_id }).then(j).then(unwrap)
  },
  /** Offer a COMPLETED show-me guide again from its first step. */
  replay: (g: Pick<Guide, 'guide_id' | 'revision'>): Promise<Guide | null> => {
    const { post, j } = apiTransport
    return post('/api/guide/replay', { guide_id: g.guide_id, revision: g.revision }).then(j).then(unwrap)
  },
}

/**
 * The three headers that tie ONE real save request to the guide step it
 * completes. Attached per request by the owning call site, never to the shared
 * transport: every other request this tab makes must stay unattributed.
 */
export function guideRequestHeaders(g: Pick<Guide, 'guide_id' | 'revision'>): Record<string, string> {
  return {
    'X-Guide-Id': g.guide_id,
    'X-Guide-Tab': TAB_ID,
    'X-Guide-Revision': String(g.revision),
  }
}

/** Fold one guide into a list: the higher revision wins; an equal one replaces. */
export function mergeGuide(list: readonly Guide[] | undefined, g: Guide): Guide[] {
  const out = [...(list ?? [])]
  const i = out.findIndex(x => x.guide_id === g.guide_id)
  if (i === -1) out.push(g)
  else if (g.revision >= out[i].revision) out[i] = g
  return out
}

export function isGuide(v: unknown): v is Guide {
  if (!v || typeof v !== 'object') return false
  const g = v as Partial<Guide>
  return typeof g.guide_id === 'string' && typeof g.slot_key === 'string'
    && typeof g.status === 'string' && typeof g.revision === 'number' && Array.isArray(g.actions)
}

/** Fold one owner `guide_update` frame into the pending-guides cache. */
export function applyGuideUpdate(queryClient: QueryClient, guide: unknown): void {
  if (!isGuide(guide)) return
  queryClient.setQueryData<Guide[]>(GUIDE_PENDING_QUERY_KEY, prev => mergeGuide(prev, guide))
}

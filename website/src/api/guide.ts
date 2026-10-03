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

export type GuideStatus = 'offered' | 'active' | 'target_missing' | 'completed' | 'cancelled' | 'expired'

/** Statuses after which nothing about the guide moves again. */
export const GUIDE_TERMINAL_STATUSES: ReadonlySet<GuideStatus> = new Set<GuideStatus>(['completed', 'cancelled', 'expired'])

export interface GuideAction {
  id: string
  params: Record<string, unknown>
  /** Evidence supplied by the gateway after an actual successful operation. */
  result?: Record<string, unknown>
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
}

export type GuideOutcome = 'observed' | 'target_missing'

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
  /** `takeOver` is sent ONLY for an explicit human takeover from another tab. */
  claim: (g: Guide, takeOver = false): Promise<Guide | null> => {
    const { post, j } = apiTransport
    return post('/api/guide/claim', takeOver ? { ...body(g), take_over: true } : body(g)).then(j).then(unwrap)
  },
  progress: (g: Guide, outcome: GuideOutcome): Promise<Guide | null> => {
    const { post, j } = apiTransport
    return post('/api/guide/progress', {
      ...body(g),
      action_index: g.action_index,
      step_index: g.step_index,
      outcome,
    }).then(j).then(unwrap)
  },
  heartbeat: (g: Guide): Promise<Guide | null> => {
    const { post, j } = apiTransport
    return post('/api/guide/heartbeat', body(g)).then(j).then(unwrap)
  },
  cancel: (g: Guide): Promise<Guide | null> => {
    const { post, j } = apiTransport
    return post('/api/guide/cancel', body(g)).then(j).then(unwrap)
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

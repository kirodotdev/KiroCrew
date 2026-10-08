/**
 * Change-card API client (`/api/cards/*`).
 *
 * Mate PROPOSES a change; the gateway holds it as a card with a computed
 * diff, risk and request plan. Nothing here can confirm a card by itself: apply
 * and undo replay the card's own `plan` steps against the EXISTING routes the
 * settings pages use, each request tagged with the card headers so the gateway
 * hook can match it to the step it previewed. The final status always comes
 * back from the gateway (`card_update` or a re-read), never from this tab.
 */
import type { QueryClient } from '@tanstack/react-query'
import { apiTransport } from './apiTransport'

export type CardStatus = 'pending' | 'applying' | 'applied' | 'partial' | 'failed' | 'cancelled' | 'expired' | 'undone'
export type CardRisk = 'normal' | 'tighten' | 'widen' | 'code_exec'

/** Every kind the gateway can propose. The registry must cover each one. */
export const CARD_KINDS = [
  'setting.change',
  'schedule.create',
  'schedule.update',
  'crewmate.create',
  'crewmate.update',
  'crewmate.capabilities',
  'template.update',
  'mcp.install',
  'mcp.add_custom',
  'mcp.toggle',
  'connection.connect',
  'secret.save',
  'trust.app',
  'denied_command',
] as const
export type CardKind = typeof CARD_KINDS[number]

/** Statuses after which the card no longer accepts an apply. */
export const CARD_FINISHED_STATUSES: ReadonlySet<CardStatus> = new Set<CardStatus>(['applied', 'partial', 'failed', 'cancelled', 'expired', 'undone'])

export interface CardChange {
  /** The card param this row shows, when it maps to one (e.g. `cron_expr`). */
  field?: string
  label: string
  before?: unknown
  after?: unknown
  add?: unknown[]
  remove?: unknown[]
}

/** A body field the gateway cannot know at proposal time. `user` is typed by
 *  the person (a secret's value); `step` is a field an earlier step's real
 *  response returned. The step's body carries a `{{...}}` placeholder there. */
export type CardFill =
  | { field: string; source: 'user' }
  | { field: string; source: 'step'; step: number; key: string }

export interface CardPlanStep {
  method: string
  /** May carry a query string (a repeat poll). */
  path: string
  body?: unknown
  fill?: CardFill[]
  /** A read the browser resends until the gateway stops waiting on it. */
  repeat?: boolean
}

export type CardUndoLabel = 'undo' | 'disconnect' | 'delete_secret'

export interface Card {
  id: string
  slot_key: string
  kind: string
  revision: number
  status: CardStatus
  risk: CardRisk
  title: string
  changes: CardChange[]
  scope?: { members: string[]; count: number }
  reason?: string
  editable: string[]
  params: Record<string, unknown>
  plan: { apply: CardPlanStep[]; undo: CardPlanStep[] | null }
  undo_unavailable_reason?: string | null
  undo_label?: CardUndoLabel
  progress?: { op: CardOp; done: number; total: number; waiting?: boolean } | null
  /** Present while `applying`: where an interrupted apply continues, and what earlier steps returned. */
  resume?: { step: number; responses: unknown[] }
  /** Present when an Undo stopped between two of its steps: where it continues. */
  undo_resume?: { step: number; responses: unknown[] }
  result?: { summary: string; details?: string } | null
  error?: { code: string; message: string; step?: number; op?: CardOp } | null
  created_at: string | number
  expires_at: string | number
  next_run_at?: string | number
  timezone?: string
  /** A schedule that runs once, at `params.at`, rather than on a cron. */
  once?: boolean
}

export type CardOp = 'apply' | 'undo'

/** The queries every slot's cards live under, for a reconnect re-read. */
export const CHANGE_CARDS_QUERY_PREFIX = ['change-cards'] as const
export const cardsQueryKey = (slot: string | null) => [...CHANGE_CARDS_QUERY_PREFIX, slot ?? ''] as const

const unwrap = (r: unknown): Card | null => {
  if (!r || typeof r !== 'object') return null
  const o = r as { card?: unknown }
  if (o.card && isCard(o.card)) return o.card
  return isCard(r) ? r : null
}

const cardPath = (id: string, verb: string) => `/api/cards/${encodeURIComponent(id)}/${verb}`

export const cardsApi = {
  pending: async (slot: string): Promise<Card[]> => {
    const { get, j } = apiTransport
    const r = await get(`/api/cards/pending?slot=${encodeURIComponent(slot)}`).then(j) as { cards?: unknown }
    return Array.isArray(r?.cards) ? r.cards.filter(isCard) : []
  },
  preview: (card: Pick<Card, 'id' | 'revision'>, params: Record<string, unknown>): Promise<Card | null> => {
    const { post, j } = apiTransport
    return post(cardPath(card.id, 'preview'), { params, revision: card.revision }).then(j).then(unwrap)
  },
  cancel: (card: Pick<Card, 'id' | 'revision'>): Promise<Card | null> => {
    const { post, j } = apiTransport
    return post(cardPath(card.id, 'cancel'), { revision: card.revision }).then(j).then(unwrap)
  },
  dismiss: (card: Pick<Card, 'id'>): Promise<Card | null> => {
    const { post, j } = apiTransport
    return post(cardPath(card.id, 'dismiss')).then(j).then(unwrap)
  },
}

/** The headers that tie ONE real request to the plan step it executes.
 *  Attached per request, never to the shared transport. */
export function cardStepHeaders(card: Pick<Card, 'id' | 'revision'>, op: CardOp, step: number): Record<string, string> {
  return {
    'X-Card-Id': card.id,
    'X-Card-Revision': String(card.revision),
    'X-Card-Op': op,
    'X-Card-Step': String(step),
  }
}

/** Send one plan step to the existing route it names, exactly as planned.
 *  `body` is the step's body after its declared fills. Resolves to the route's
 *  parsed JSON response. */
export async function sendCardStep(
  card: Pick<Card, 'id' | 'revision'>,
  op: CardOp,
  index: number,
  step: CardPlanStep,
  body: unknown = step.body,
): Promise<unknown> {
  const method = step.method.toUpperCase()
  const hasBody = body !== undefined && body !== null && method !== 'GET'
  const r = await fetch(step.path, {
    method,
    headers: {
      ...(hasBody ? { 'Content-Type': 'application/json' } : {}),
      // Same placeholder session key the shared helpers send, so the server's
      // session gate runs for this request too.
      'X-Session-Key': 'dashboard:ui',
      ...cardStepHeaders(card, op, index),
    },
    body: hasBody ? JSON.stringify(body) : undefined,
  })
  return apiTransport.jNullable(r)
}

/** A repeated apply of a finished card: the gateway answers with the card
 *  instead of running the route again. */
export function replayedCard(response: unknown): Card | null {
  if (!response || typeof response !== 'object') return null
  const r = response as { card_replay?: unknown; card?: unknown }
  return r.card_replay === true && isCard(r.card) ? r.card : null
}

/** The gateway's refusal code carried by a failed request, when it sent one. */
export function errorCode(e: unknown): string | null {
  const body = (e as { body?: unknown } | null)?.body
  if (typeof body !== 'string' || !body) return null
  try {
    const code = (JSON.parse(body) as { code?: unknown }).code
    return typeof code === 'string' ? code : null
  } catch {
    return null
  }
}

export function isCard(v: unknown): v is Card {
  if (!v || typeof v !== 'object') return false
  const c = v as Partial<Card>
  return typeof c.id === 'string' && typeof c.slot_key === 'string' && typeof c.kind === 'string'
    && typeof c.status === 'string' && typeof c.revision === 'number' && Array.isArray(c.changes)
}

/** Fold one card into a list: the higher revision wins; an equal one replaces. */
export function mergeCard(list: readonly Card[] | undefined, c: Card): Card[] {
  const out = [...(list ?? [])]
  const i = out.findIndex(x => x.id === c.id)
  if (i === -1) out.push(c)
  else if (c.revision >= out[i].revision) out[i] = c
  return out
}

/** Fold one owner `card_update` frame into that slot's cards cache. */
export function applyCardUpdate(queryClient: QueryClient, card: unknown): void {
  if (!isCard(card)) return
  queryClient.setQueryData<Card[]>(cardsQueryKey(card.slot_key), prev => mergeCard(prev, card))
}

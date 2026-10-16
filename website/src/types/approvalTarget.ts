/** WHICH pending approval a control decides, retires or adopts.
 *
 *  Two registries raise approvals and their ids can collide: the coordinator
 *  (background sources, subagent spawns, channel-relayed asks) mints nothing
 *  stable itself, because its id is the caller's and recurs, and a chat runner's
 *  native request id is connection-scoped. A bare id therefore names no request
 *  at all. Every approval a client shows carries one of these targets instead,
 *  built from what the server sent with it, and every decide, retire and
 *  adopt is matched on the whole target:
 *
 *  - a coordinator target is bound by the server-issued `instance` (one request
 *    under a recurring id) and its owning `slot` ('' for a slotless approval,
 *    such as a cron job's), which the decide route verifies against the record;
 *  - a native target is bound by the slot that holds the request and `mid`, the
 *    delivery identity of the permission row the runner wrote for it.
 *
 *  The two shapes never compare equal, so a coordinator frame cannot settle a
 *  native row (or the reverse) whatever their ids are. `id` is carried for the
 *  route and for display only. */
export interface CoordinatorApprovalTarget {
  readonly origin: 'coordinator'
  readonly id: string
  readonly slot: string
  readonly instance: string
}

export interface NativeApprovalTarget {
  readonly origin: 'native'
  readonly id: string
  readonly slot: string
  readonly mid: string
}

export type ApprovalTarget = CoordinatorApprovalTarget | NativeApprovalTarget

/** A coordinator target from server-sent fields, or null when one is missing:
 *  without an instance the record cannot be named, and nothing may stand in
 *  for it. */
export function coordinatorTarget(id: unknown, slot: unknown, instance: unknown): CoordinatorApprovalTarget | null {
  if (typeof id !== 'string' || !id || typeof instance !== 'string' || !instance) return null
  return { origin: 'coordinator', id, slot: typeof slot === 'string' ? slot : '', instance }
}

/** A native target for a permission row held in *slot*, or null when the row
 *  carries no delivery identity (a row restored without one has no live
 *  request behind it). */
export function nativeTarget(id: unknown, slot: unknown, mid: unknown): NativeApprovalTarget | null {
  if (typeof id !== 'string' || !id || typeof slot !== 'string' || !slot || typeof mid !== 'string' || !mid) return null
  return { origin: 'native', id, slot, mid }
}

export function isCoordinatorTarget(value: unknown): value is CoordinatorApprovalTarget {
  if (!value || typeof value !== 'object') return false
  const t = value as Record<string, unknown>
  return t.origin === 'coordinator' && typeof t.id === 'string' && !!t.id
    && typeof t.slot === 'string' && typeof t.instance === 'string' && !!t.instance
}

/** One string per request, for maps and claim keys. */
export function approvalTargetKey(t: ApprovalTarget): string {
  // Opens with the origin, so a coordinator key never equals a native one.
  return t.origin === 'coordinator'
    ? [t.origin, t.id, t.instance].join('\u0000')
    : [t.origin, t.slot, t.id, t.mid].join('\u0000')
}

/** Whether two targets name the same request. A coordinator instance is minted
 *  per request, so it (with the id) is the identity; the slot is the owner
 *  binding the server checks, not part of it. */
export function sameApprovalTarget(a: ApprovalTarget | null | undefined, b: ApprovalTarget | null | undefined): boolean {
  return !!a && !!b && approvalTargetKey(a) === approvalTargetKey(b)
}

/** The target of a chat permission row in *slot*. A row the coordinator
 *  injected carries its target in `meta.approval_target`; any other row is the
 *  chat runner's own and is named by its slot and `meta.mid`. */
export function permissionRowTarget(meta: Record<string, unknown> | undefined, slot: string | null | undefined): ApprovalTarget | null {
  if (!meta) return null
  if (meta.registry === 'coordinator') return isCoordinatorTarget(meta.approval_target) ? meta.approval_target : null
  return nativeTarget(meta.approval_id, slot, meta.mid)
}

/** The key a control records "this approval is gone" under: the request's
 *  target key when it has one, so a refusal on request A never withdraws a
 *  replacement B that reuses A's recurring id. A control with no target is
 *  keyed by its id alone, which no targeted replacement can equal. */
export function approvalGoneKey(id: string | null | undefined, target: ApprovalTarget | null | undefined): string {
  // A target key opens with its origin, so one opening with the separator
  // itself can never equal it.
  return target ? approvalTargetKey(target) : '\u0000' + (id ?? '')
}

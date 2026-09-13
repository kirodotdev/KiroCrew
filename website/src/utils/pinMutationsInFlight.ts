/**
 * The keys of pin mutations this tab is awaiting the server's answer on.
 *
 * Pins are applied optimistically, so between the click and the acknowledgement the server still
 * reports the old membership. A slot frame arriving then says "nothing pinned" about rows it does not
 * yet know are pinned, and settling on it would drop the manual-apply flag for an arrangement the
 * user just made. Once the batch settles the same report is simply true and is honoured.
 *
 * In memory and per-tab, deliberately. A cross-tab version of this lived in localStorage with a
 * per-load owner key, a 2s heartbeat and a liveness expiry; it guarded a two-tab frame-timing race
 * whose whole cost is a display preference one re-drag restores, and every layer added to keep it
 * honest produced the next defect — a live request expiring on a throttled timer, a dead owner's
 * entry outliving its page, a deferred replay racing reconciliation. A record that cannot outlive
 * the page that wrote it has none of those failure modes, and the shipped "Follow sort order
 * (all pinned)" action is the in-product remedy for the residual cross-tab race.
 *
 * It lives in its own module rather than beside the batch that owns it because `useSessionActions`
 * imports the store, so the store-side listener cannot import from there without a cycle.
 */
let inFlightKeys: readonly string[] = []

export function publishPinMutationKeysInFlight(keys: readonly string[]): void {
  inFlightKeys = [...keys]
}

export function readPinMutationKeysInFlight(): string[] {
  return [...inFlightKeys]
}

export function pinMutationsAreInFlight(): boolean {
  return inFlightKeys.length > 0
}

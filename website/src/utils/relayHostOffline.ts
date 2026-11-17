import { parseErrorField } from './errorReport'

/**
 * True for the relay's own answer while the Crew's host is not connected:
 * `503` with `{"reason": "crew_not_connected"}` (relay protocol v1). The
 * relay window's reconnecting cover and top bar already say the Crew is
 * offline, so a panel whose read failed this way stays quiet instead of
 * adding the relay's raw sentence, and reads again on reconnect.
 *
 * Checked by shape (`status` + `body`), the fields every api client error
 * carries.
 */
export function isRelayHostOffline(error: unknown): boolean {
  if (typeof error !== 'object' || error === null) return false
  const { status, body } = error as { status?: unknown; body?: unknown }
  return status === 503 && typeof body === 'string' && parseErrorField(body, 'reason') === 'crew_not_connected'
}

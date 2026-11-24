/**
 * The switch for every write the XR page can make but the board's stamp and its no (`writes.boardDecide`, on in
 * every build).
 *
 * Apart from those the default build is read-only: `VITE_XR_WRITES` is not set, the switch is
 * false, and `tryWrite` returns "disabled" without calling anything. A build
 * made with `VITE_XR_WRITES=1` (for a throwaway dev gateway) lets a write
 * through, and then only one that `PressRouter` committed.
 */
import { performWrite, type XrWritePayload } from './writes'

export const XR_WRITES_ENABLED: boolean = import.meta.env.VITE_XR_WRITES === '1'

export type XrWriteResult = { ok: true } | { ok: false; reason: 'disabled' | 'failed'; detail: string }

/** Every write goes through here. With the switch off it only reports, and calls nothing. */
export async function tryWrite(
  payload: XrWritePayload,
  enabled: boolean = XR_WRITES_ENABLED,
  perform: (p: XrWritePayload) => Promise<void> = performWrite,
): Promise<XrWriteResult> {
  if (!enabled) return { ok: false, reason: 'disabled', detail: '' }
  try {
    await perform(payload)
    return { ok: true }
  } catch (e) {
    return { ok: false, reason: 'failed', detail: e instanceof Error ? e.message : String(e) }
  }
}

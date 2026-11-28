/**
 * Rewind helper — wraps `api.rewind` with a rollback callback that fires on
 * failure. Separate from ChatPage so the success/failure branches can be
 * unit-tested without mounting the full chat page.
 */

import { api } from '../api/client'

/**
 * Call `/api/chat/slots/{slot}/rewind` and invoke `rollback` with the error if
 * the request rejects, so the caller can show the server's reason.
 */
export async function rewindWithRollback(
  slot: string,
  ts: string,
  content: string,
  rollback: (e: unknown) => void,
): Promise<void> {
  try {
    await api.rewind(slot, ts, content)
  } catch (e) {
    rollback(e)
  }
}

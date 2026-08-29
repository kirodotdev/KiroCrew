/** Did the server call this close failure DEFINITIVE (refused and rolled back)?
 *
 *  Forwarded on the rejection payload, since `miniSerializeError` drops booleans;
 *  a raw `ApiError` carries only `body`. `undefined` means the server never said. */
export const closeDefinitive = (e: unknown): boolean | undefined => {
  const direct = (e as { definitive?: unknown } | null)?.definitive
  if (typeof direct === 'boolean') return direct
  const body = (e as { body?: unknown } | null)?.body
  if (typeof body !== 'string' || body === '') return undefined
  try {
    const parsed: unknown = JSON.parse(body)
    const flag = (parsed as { definitive?: unknown } | null)?.definitive
    return typeof flag === 'boolean' ? flag : undefined
  } catch {
    return undefined
  }
}

/** Is the DELETE's outcome UNKNOWABLE from this rejection?
 *
 *  Keyed on the server's `definitive` flag, never the status: every close failure
 *  is a literal 500, so a status test read refusals as unknown. No flag = unknown.
 *
 *  ONE predicate for both consumers, so the row and the notice cannot drift. */
export const isCloseOutcomeUnknown = (e: unknown): boolean => {
  const definitive = closeDefinitive(e)
  if (definitive !== undefined) return !definitive
  const status = (e as { status?: unknown } | null)?.status
  if (typeof status !== 'number') return true
  if (status === 408 || status === 429) return true
  return status >= 500
}
